"""Screen capture and change detection."""
import io
import struct

import numpy as np
from PIL import Image

try:
    import mss
except ImportError:  # fall back to Pillow's slower grabber
    mss = None

TILE = 96


class ScreenGrabber:
    """Grabs the primary monitor as an RGB numpy array.

    Must be created and used on the same thread (an mss requirement).
    """

    def __init__(self):
        if mss is not None:
            self._sct = mss.mss()
            mon = self._sct.monitors[1]
            self._mon = mon
            self.left, self.top = mon["left"], mon["top"]
            self.width, self.height = mon["width"], mon["height"]
        else:
            from PIL import ImageGrab
            self._sct = None
            self._grab = ImageGrab.grab
            img = self._grab()
            self.left = self.top = 0
            self.width, self.height = img.size

    def grab(self):
        if self._sct is not None:
            shot = self._sct.grab(self._mon)
            arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
            return np.ascontiguousarray(arr[:, :, 2::-1])  # BGRA -> RGB
        return np.asarray(self._grab().convert("RGB"))

    def close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass


def scale_frame(frame, scale):
    if scale >= 0.999:
        return frame
    h, w = frame.shape[:2]
    size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return np.asarray(Image.fromarray(frame).resize(size, Image.BILINEAR))


def changed_rects(prev, cur, tile=TILE):
    """Return [(x, y, w, h)] covering the tiles that differ between two frames."""
    h, w = cur.shape[:2]
    if prev is None or prev.shape != cur.shape:
        return [(0, 0, w, h)]
    diff = (prev != cur).any(axis=2)
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


def encode_frame(frame, rects, quality):
    """Pack changed rectangles of a frame as JPEG tiles into one FRAME payload."""
    h, w = frame.shape[:2]
    parts = [struct.pack("<HHH", w, h, len(rects))]
    for x, y, rw, rh in rects:
        buf = io.BytesIO()
        tile = np.ascontiguousarray(frame[y:y + rh, x:x + rw])
        Image.fromarray(tile).save(buf, "JPEG", quality=quality)
        data = buf.getvalue()
        parts.append(struct.pack("<HHHHI", x, y, rw, rh, len(data)))
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
