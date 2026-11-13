"""Screen capture, change detection and frame encoding.

Frames travel inside the program as BGRA numpy arrays (h, w, 4) - the format both
capture back ends produce - so nothing is converted unless it is about to be sent.

Capture back ends, fastest first:
  * DXGI Desktop Duplication (dxcam, Windows 8+): ~5 ms per frame and it reports
    "nothing changed" for free. One instance per process, shared by all sessions.
  * GDI via mss: ~25-30 ms per frame at 1080p, which caps the rate near 30 fps.
"""
import io
import math
import os
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

try:
    import mss
except ImportError:
    mss = None

TILE = 96
_POOL = ThreadPoolExecutor(max_workers=max(2, min(4, (os.cpu_count() or 2))))  # JPEG encode releases the GIL


class _DXGISource:
    """Process-wide DXGI duplicator. Thread-safe; every new desktop image gets a sequence number."""

    _instance = None
    _create_lock = threading.Lock()

    def __init__(self):
        import dxcam  # Windows only; ImportError -> caller falls back to mss
        self._cam = dxcam.create(output_color="BGRA")
        if self._cam is None:
            raise RuntimeError("DXGI duplication unavailable")
        self.width, self.height = self._cam.width, self._cam.height
        self._lock = threading.Lock()
        self.frame = None
        self.seq = 0

    @classmethod
    def get(cls):
        with cls._create_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def drop(cls):
        """Forget a broken duplicator (lock screen, display change) so the next get() makes a new one."""
        with cls._create_lock:
            if cls._instance is not None:
                try:
                    cls._instance._cam.release()   # lets dxcam.create() build a fresh camera
                except Exception:
                    pass
            cls._instance = None

    def latest(self):
        """(seq, frame). Polls the duplicator; seq changes only when the desktop changed."""
        with self._lock:
            frame = self._cam.grab()
            if frame is not None:
                self.frame, self.seq = frame, self.seq + 1
            return self.seq, self.frame


class ScreenGrabber:
    """Captures the primary monitor. grab() -> BGRA array, or None when nothing changed.

    Create and use it on one thread (an mss requirement); DXGI grabbers share one source.
    """

    RETRY_DXGI = 3.0   # seconds between attempts to get back from GDI to DXGI

    def __init__(self, backend=None):
        self.backend = None
        self._sct = None
        self._seen = -1
        self._retry_at = None     # when to try DXGI again after it failed for a reason that may pass
        want = backend or os.environ.get("MINIDECK_CAPTURE", "auto")
        if want in ("auto", "dxgi") and sys.platform == "win32":
            try:
                self._use_dxgi()
            except ImportError:
                if want == "dxgi":
                    raise
            except Exception:
                if want == "dxgi":
                    raise
                self._retry_at = time.monotonic() + self.RETRY_DXGI   # e.g. started on the lock screen
        if self.backend is None:
            self._open_mss()

    def _use_dxgi(self):
        self._dx = _DXGISource.get()
        self.left = self.top = 0          # DXGI output 0 is the primary monitor
        self.width, self.height = self._dx.width, self._dx.height
        self._seen = -1
        self.backend = "dxgi"

    def _try_back_to_dxgi(self):
        try:
            self._use_dxgi()
        except Exception:
            self._retry_at = time.monotonic() + self.RETRY_DXGI
            return False
        self._retry_at = None
        self.close()
        self._sct = None
        return True

    def _open_mss(self):
        if mss is None:
            raise RuntimeError("no screen capture back end (install mss)")
        self._sct = (getattr(mss, "MSS", None) or mss.mss)()
        mon = self._sct.monitors[1]
        self._mon = mon
        self.left, self.top = mon["left"], mon["top"]
        self.width, self.height = mon["width"], mon["height"]
        self.backend = "mss"

    def grab(self, force=False):
        """BGRA (h, w, 4) uint8 frame. None means 'same as last time' (unless force=True)."""
        if self.backend == "mss" and self._retry_at is not None and time.monotonic() >= self._retry_at:
            if self._try_back_to_dxgi():
                force = True              # first DXGI frame must be sent in full
        if self.backend == "dxgi":
            try:
                seq, frame = self._dx.latest()
            except Exception:
                # lock screen, UAC prompt, display mode change: carry on with GDI capture
                # and keep trying to get DXGI back
                _DXGISource.drop()
                self._open_mss()
                self._retry_at = time.monotonic() + self.RETRY_DXGI
                return self.grab(force=True)
            if frame is None:            # nothing captured yet on a perfectly static screen
                return None
            if seq == self._seen and not force:
                return None
            self._seen = seq
            return frame
        shot = self._sct.grab(self._mon)
        return np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)

    def close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass


def _as_cells(frame):
    """One comparable value per pixel: uint32 view for BGRA, bool-able array otherwise."""
    if frame.ndim == 3 and frame.shape[2] == 4 and frame.dtype == np.uint8 and frame.flags.c_contiguous:
        return frame.view(np.uint32).reshape(frame.shape[:2])
    return frame


def changed_rects(prev, cur, tile=TILE):
    """Return [(x, y, w, h)] covering the tiles that differ between two frames."""
    h, w = cur.shape[:2]
    if prev is None or prev.shape != cur.shape:
        return [(0, 0, w, h)]
    a, b = _as_cells(prev), _as_cells(cur)
    diff = a != b
    if diff.ndim == 3:
        diff = diff.any(axis=2)
    th, tw = -(-h // tile), -(-w // tile)
    padded = np.zeros((th * tile, tw * tile), dtype=bool)
    padded[:h, :w] = diff
    grid = padded.reshape(th, tile, tw, tile).any(axis=(1, 3))
    if not grid.any():
        return []

    # Merge changed tiles into horizontal runs, then stack identical runs vertically.
    done, open_runs = [], {}
    for ty in range(th):
        row, runs, tx = grid[ty], [], 0
        while tx < tw:
            if row[tx]:
                start = tx
                while tx < tw and row[tx]:
                    tx += 1
                runs.append((start, tx))
            else:
                tx += 1
        still_open = {}
        for run in runs:
            y0 = open_runs.pop(run, ty)
            still_open[run] = y0
        for (x0, x1), y0 in open_runs.items():
            done.append((x0, y0, x1, ty))
        open_runs = still_open
    for (x0, x1), y0 in open_runs.items():
        done.append((x0, y0, x1, th))

    rects = []
    for x0, y0, x1, y1 in done:
        px, py = x0 * tile, y0 * tile
        rects.append((px, py, min(x1 * tile, w) - px, min(y1 * tile, h) - py))
    return rects


def to_image(frame):
    """BGRA or RGB numpy frame -> PIL RGB image (BGRA conversion runs in C)."""
    if frame.ndim == 3 and frame.shape[2] == 4:
        h, w = frame.shape[:2]
        return Image.frombuffer("RGB", (w, h), np.ascontiguousarray(frame), "raw", "BGRX", 0, 1)
    return Image.fromarray(frame)


def scaled_image(img, scale):
    if scale >= 0.999:
        return img
    if abs(scale - 0.5) < 1e-3:
        return img.reduce(2)
    size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    return img.resize(size, Image.BILINEAR, reducing_gap=2.0)


def _scale_rect(rect, scale, size):
    """Map a full-resolution rect onto the scaled image, rounding outwards."""
    x, y, w, h = rect
    sw, sh = size
    x0, y0 = math.floor(x * scale), math.floor(y * scale)
    x1, y1 = min(sw, math.ceil((x + w) * scale)), min(sh, math.ceil((y + h) * scale))
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def _jpeg(img, quality):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def encode_frame(frame, rects, quality, scale=1.0):
    """Pack the changed rectangles (full-resolution coordinates) into one FRAME payload.

    The picture is scaled first when scale < 1, then the tiles are cut from the scaled
    image (no seams) and JPEG-compressed in parallel.
    """
    img = scaled_image(to_image(frame), scale)
    if img.size != (frame.shape[1], frame.shape[0]):
        rects = [_scale_rect(r, scale, img.size) for r in rects]
    crops = [img.crop((x, y, x + w, y + h)) for x, y, w, h in rects]
    blobs = list(_POOL.map(lambda c: _jpeg(c, quality), crops)) if len(crops) > 1 else \
        [_jpeg(c, quality) for c in crops]
    parts = [struct.pack("<HHH", img.width, img.height, len(rects))]
    for (x, y, w, h), data in zip(rects, blobs):
        parts.append(struct.pack("<HHHHI", x, y, w, h, len(data)))
        parts.append(data)
    return b"".join(parts)


def decode_frame(payload):
    """Yield (width, height) first, then (x, y, PIL.Image) for each tile."""
    w, h, count = struct.unpack_from("<HHH", payload, 0)
    yield w, h
    off = 6
    for _ in range(count):
        x, y, _rw, _rh, n = struct.unpack_from("<HHHHI", payload, off)
        off += 12
        tile = Image.open(io.BytesIO(payload[off:off + n]))
        tile.load()
        off += n
        yield x, y, tile
