"""Viewer side: network session plus the Tk window that shows the remote screen."""
import hmac
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

from . import VERSION
from .journal import human_size
from .netutil import open_stream
from .proto import (AuthError, IdentityError, IdentityMismatch, Msg, ProtocolError,
                    check_identity, handshake)
from .screen import decode_frame

CHUNK = 256 * 1024


class ViewerSession:
    """Connection to a host. Frames are painted into self.image by a reader thread."""

    def __init__(self, address, password, relay=None, on_clipboard=None, stream_factory=None,
                 identity_slot=None, expect_identity=None, on_identity_changed=None):
        self.stream_factory = stream_factory   # () -> connected socket, for server-brokered sessions
        self.address = address
        self.password = password
        self.relay = relay
        self.on_clipboard = on_clipboard
        self.identity_slot = identity_slot          # name to remember the fingerprint under
        self.expect_identity = expect_identity      # fingerprint seen last time, if any
        self.on_identity_changed = on_identity_changed   # (got, expected) -> bool, may ask the user
        self.identity = ""                          # what the host proved this time
        self.conn = None
        self.hello = {}
        self.image = None           # full remote picture
        self.shown = None           # copy scaled to fit self.target (built off the UI thread)
        self.view = (0, 0, 1, 1)    # where self.shown sits on the canvas: x, y, w, h
        self.target = None          # canvas size (w, h), set by the window
        self._shown_for = None      # (target, image size) self.shown was last fully built for
        self.lock = threading.Lock()
        self.dirty = False
        self.frames = 0
        self.closed = False
        self.close_reason = ""
        self.transfer = ""          # human-readable file transfer status
        self.on_event = None        # (action, detail) for the local journal
        self._out = queue.Queue(maxsize=256)

    def connect(self):
        """Blocking. Raises ConnectionError/AuthError/OSError with a readable message."""
        sock = self.stream_factory() if self.stream_factory else open_stream(self.address, self.relay)
        try:
            sock.settimeout(60)
            conn = handshake(sock, self.password, False)
            conn.send_json(Msg.AUTH, {"name": socket.gethostname(),
                                      "platform": sys.platform, "v": VERSION})
            try:
                mtype, payload = conn.recv()
            except (AuthError, ConnectionError):
                raise AuthError("wrong password") from None
            if mtype == Msg.BYE:
                raise ConnectionError("rejected")
            if mtype != Msg.IDENT:
                raise IdentityError("the host did not say which computer it is")
            self._check_identity(conn, json.loads(payload))
            mtype, payload = conn.recv()            # the host answers after the person there agrees
            if mtype == Msg.BYE:
                raise ConnectionError("rejected")
            if mtype != Msg.HELLO:
                raise ProtocolError("unexpected reply")
            self.hello = json.loads(payload)
            sock.settimeout(None)
        except BaseException:
            sock.close()
            raise
        self.conn = conn
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._write_loop, daemon=True).start()

    def _check_identity(self, conn, info):
        """Verify the host's key and compare it with the one seen last time."""
        got = check_identity(info, conn.transcript)
        expected = self.expect_identity
        if expected and not hmac.compare_digest(got, expected):
            # someone in the middle cannot sign for the key this computer remembers
            if not (self.on_identity_changed and self.on_identity_changed(got, expected)):
                raise IdentityMismatch(expected, got)
        self.identity = got

    # -- threads ------------------------------------------------------------
    def _read_loop(self):
        reason = "connection lost"
        try:
            while not self.closed:
                mtype, payload = self.conn.recv()
                if mtype == Msg.FRAME:
                    self._on_frame(payload)
                elif mtype == Msg.CLIPBOARD:
                    if self.on_clipboard:
                        self.on_clipboard(payload.decode("utf-8", "replace"))
                elif mtype == Msg.BYE:
                    reason = "closed by host"
                    break
        except (OSError, ValueError, AuthError, ProtocolError):
            pass
        self.close(reason)

    def _on_frame(self, payload):
        tiles = decode_frame(payload)
        size = next(tiles)
        with self.lock:
            if self.image is None or self.image.size != size:
                self.image = Image.new("RGB", size)
        boxes = []
        for x, y, tile in tiles:
            with self.lock:
                self.image.paste(tile, (x, y))
            boxes.append((x, y, x + tile.width, y + tile.height))
        if not self._rescale_boxes(boxes):
            self.rescale()
        self.frames += 1
        self.dirty = True

    def _rescale_boxes(self, boxes):
        """Update only the changed parts of self.shown. False if a full rescale is needed."""
        target = self.target
        with self.lock:
            shown = self.shown
            if shown is None or self._shown_for != (target, self.image.size):
                return False           # window or remote resolution changed since the last full rescale
            iw, ih = self.image.size
            w, h = shown.size
            if sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes) > iw * ih * 0.6:
                return False           # most of the screen changed: one full resize is cheaper
            if (w, h) == (iw, ih):
                for box in boxes:
                    shown.paste(self.image.crop(box), box[:2])
                return True
            sx, sy = w / iw, h / ih
            for x0, y0, x1, y1 in boxes:
                # destination area, grown by a pixel so bilinear edges blend with neighbours
                dx0, dy0 = max(0, int(x0 * sx) - 1), max(0, int(y0 * sy) - 1)
                dx1, dy1 = min(w, int(x1 * sx + 0.999) + 1), min(h, int(y1 * sy + 0.999) + 1)
                src = (dx0 / sx, dy0 / sy, dx1 / sx, dy1 / sy)
                shown.paste(self.image.resize((dx1 - dx0, dy1 - dy0), Image.BILINEAR, box=src), (dx0, dy0))
            return True

    def rescale(self):
        """Fit the picture into self.target. Runs on the reader thread for every frame,
        so the UI thread only has to hand the ready image to Tk."""
        target = self.target
        if target is None or self.image is None:
            return
        cw, ch = target
        with self.lock:
            iw, ih = self.image.size
            ratio = min(cw / iw, ch / ih)
            w, h = max(1, int(iw * ratio)), max(1, int(ih * ratio))
            if (w, h) == (iw, ih):
                shown = self.image.copy()
            elif ratio <= 0.5 + 1e-6 and (iw // 2, ih // 2) == (w, h):
                shown = self.image.reduce(2)
            else:
                shown = self.image.resize((w, h), Image.BILINEAR, reducing_gap=2.0)
            self.shown = shown
            self.view = ((cw - w) // 2, (ch - h) // 2, w, h)
            self._shown_for = (target, (iw, ih))

    def _write_loop(self):
        try:
            while not self.closed:
                item = self._out.get()
                if item is None:
                    break
                self.conn.send(*item)
        except OSError:
            pass
        self.close("connection lost")

    # -- sending ------------------------------------------------------------
    def send(self, mtype, payload=b"", block=False):
        if self.closed:
            return
        try:
            self._out.put((mtype, payload), block=block, timeout=30 if block else None)
        except queue.Full:
            pass  # drop input rather than freeze the UI

    def send_input(self, event):
        self.send(Msg.INPUT, json.dumps(event).encode())

    def send_settings(self, **settings):
        self.send(Msg.SETTINGS, json.dumps(settings).encode())

    def send_clipboard(self, text):
        self.send(Msg.CLIPBOARD, text.encode("utf-8"))

    def send_file(self, path):
        threading.Thread(target=self._send_file, args=(path,), daemon=True).start()

    def _send_file(self, path):
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
            self.send(Msg.FILE_BEGIN, json.dumps({"name": name, "size": size}).encode(), True)
            sent = 0
            with open(path, "rb") as f:
                while not self.closed:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    self.send(Msg.FILE_CHUNK, chunk, True)
                    sent += len(chunk)
                    self.transfer = f"{name}: {100 * sent // max(1, size)}%"
            self.send(Msg.FILE_END, b"", True)
            self.transfer = f"{name}: отправлен"
            if self.on_event:
                self.on_event("out_file", f"{name} ({human_size(size)})")
        except OSError as exc:
            self.transfer = f"{name}: ошибка ({exc})"

    def close(self, reason="closed"):
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        try:
            self._out.put_nowait(None)
        except queue.Full:
            pass
        if self.conn is not None:
            self.conn.close()


QUALITY = [("Низкое", 35), ("Среднее", 60), ("Высокое", 85)]
SCALE = [("100%", 1.0), ("75%", 0.75), ("50%", 0.5)]
FPS = [("15 к/с", 15), ("30 к/с", 30), ("45 к/с", 45)]
DEFAULT_FPS = "30 к/с"
COMBOS = [
    ("Win", ["Win_L"]),
    ("Alt+Tab", ["Alt_L", "Tab"]),
    ("Alt+F4", ["Alt_L", "F4"]),
    ("Ctrl+Shift+Esc", ["Control_L", "Shift_L", "Escape"]),
    ("Win+D", ["Win_L", "d"]),
    ("Win+R", ["Win_L", "r"]),
    ("Print Screen", ["Print"]),
]


class ViewerWindow(ctk.CTkToplevel):
    def __init__(self, master, session, on_closed=None):
        super().__init__(master, fg_color="#1e1e1e")
        self.session = session
        self.on_closed = on_closed
        self.control = bool(session.hello.get("control", True))
        host = session.hello.get("name", session.address)
        self.title(f"{host} — Minideck" + ("" if self.control else " (только просмотр)"))
        self.geometry("1100x700")
        self._bar_shown = True
        self._hint_job = None

        self._photo = None
        self._view = (0, 0, 1, 1)       # where the image sits on the canvas
        self._last_size = None
        self._pending_move = None
        self._keys_down = {}             # keycode -> event dict sent on press
        self._fullscreen = False
        self._user_closed = False
        self._finished = False
        self._stat_time = time.monotonic()
        self._stat_frames = 0
        self._stat_bytes = 0

        self._build_toolbar()
        self.canvas = tk.Canvas(self, bg="#1e1e1e", highlightthickness=0, takefocus=1)
        self.canvas.pack(fill="both", expand=True)
        self._item = self.canvas.create_image(0, 0, anchor="nw")
        self._bind_input()
        self._hint = ctk.CTkLabel(self, text="", corner_radius=8, fg_color="#111827",
                                  text_color="#ffffff", height=34)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(15, self._tick)
        self.after(50, self._apply_settings)
        self.after(200, lambda: (self.lift(), self.focus_force(), self.canvas.focus_set()))

    # -- toolbar ------------------------------------------------------------
    def _build_toolbar(self):
        bar = self.bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("#f3f5f8", "#2b2d31"))
        bar.pack(fill="x")
        ghost = dict(fg_color="transparent", border_width=1, height=30,
                     text_color=("#1f2937", "#e5e7eb"), hover_color=("#dfe4ea", "#3a3c42"))
        menu = dict(height=30, fg_color=("#e2e6ec", "#3a3c42"), button_color=("#cfd5dd", "#4a4d55"),
                    button_hover_color=("#bcc4ce", "#5a5d66"), text_color=("#1f2937", "#e5e7eb"))

        ctk.CTkLabel(bar, text="Качество", text_color=("#5f6b7a", "#9aa4b2")).pack(side="left", padx=(12, 6), pady=8)
        self._quality_seg = ctk.CTkSegmentedButton(bar, values=[q[0] for q in QUALITY], height=30,
                                                   command=lambda _: self._apply_settings())
        self._quality_seg.set("Среднее")
        self._quality_seg.pack(side="left")
        ctk.CTkLabel(bar, text="Размер", text_color=("#5f6b7a", "#9aa4b2")).pack(side="left", padx=(14, 6))
        self._scale_menu = ctk.CTkOptionMenu(bar, values=[s[0] for s in SCALE], width=84,
                                             command=lambda _: self._apply_settings(), **menu)
        self._scale_menu.pack(side="left")
        self._fps_menu = ctk.CTkOptionMenu(bar, values=[f[0] for f in FPS], width=92,
                                           command=lambda _: self._apply_settings(), **menu)
        self._fps_menu.set(DEFAULT_FPS)
        self._fps_menu.pack(side="left", padx=(8, 0))
        if self.control:
            self._keys_menu = ctk.CTkOptionMenu(bar, values=[c[0] for c in COMBOS], width=120,
                                                command=self._on_combo, **menu)
            self._keys_menu.set("Клавиши")
            self._keys_menu.pack(side="left", padx=(14, 0))
            ctk.CTkButton(bar, text="Отправить файл", width=120, command=self._pick_file,
                          **ghost).pack(side="left", padx=(8, 0))
        self._fs_button = ctk.CTkButton(bar, text="Во весь экран", width=120,
                                        command=self._toggle_fullscreen, **ghost)
        self._fs_button.pack(side="left", padx=(8, 0))
        ctk.CTkButton(bar, text="Отключиться", width=110, height=30, fg_color="#d9423a",
                      hover_color="#b8342d", command=self.close).pack(side="right", padx=12)
        self._status = ctk.CTkLabel(bar, text="", text_color=("#5f6b7a", "#9aa4b2"))
        self._status.pack(side="right", padx=4)

    def _apply_settings(self):
        quality = dict(QUALITY)[self._quality_seg.get()]
        scale = dict(SCALE)[self._scale_menu.get()]
        self.session.send_settings(quality=quality, scale=scale, fps=dict(FPS)[self._fps_menu.get()])
        self.canvas.focus_set()

    def _on_combo(self, label):
        self._keys_menu.set("Клавиши")
        self._send_combo(dict(COMBOS)[label])

    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)
        self._fs_button.configure(text="Обычный режим" if self._fullscreen else "Во весь экран")
        if self._fullscreen:
            self._show_bar(False)
            self.hint("Панель появится, если подвести мышь к верхнему краю экрана")
        else:
            self.bar.place_forget()
            self.bar.pack(fill="x", before=self.canvas)
            self._bar_shown = True
        self.canvas.focus_set()

    def _show_bar(self, show):
        """Fullscreen only: the toolbar floats over the picture and hides itself."""
        self.bar.pack_forget()
        if show:
            self.bar.place(x=0, y=0, relwidth=1)
            self.bar.lift()
        else:
            self.bar.place_forget()
        self._bar_shown = show

    def _autohide_bar(self):
        y = self.winfo_pointery() - self.winfo_rooty()
        if not self._bar_shown and y <= 2:
            self._show_bar(True)
        elif self._bar_shown and y > self.bar.winfo_height() + 40:
            self._show_bar(False)

    def hint(self, text, ms=3500):
        self._hint.configure(text=f"   {text}   ")
        self._hint.place(relx=0.5, y=18, anchor="n")
        self._hint.lift()
        if self._hint_job:
            self.after_cancel(self._hint_job)
        self._hint_job = self.after(ms, self._hint.place_forget)

    def _pick_file(self):
        path = filedialog.askopenfilename(parent=self, title="Файл для отправки")
        if path:
            self.session.send_file(path)
            self.hint("Файл отправляется в папку «Загрузки\\Minideck» на удалённом компьютере")
        self.canvas.focus_set()

    def _send_combo(self, keys):
        for sym in keys:
            self.session.send_input({"t": "key", "down": True, "sym": sym})
        for sym in reversed(keys):
            self.session.send_input({"t": "key", "down": False, "sym": sym})
        self.canvas.focus_set()

    # -- input --------------------------------------------------------------
    def _bind_input(self):
        if not self.control:
            return
        c = self.canvas
        c.bind("<Enter>", lambda e: c.focus_set())  # keys go remote whenever the pointer is on the picture
        c.bind("<Motion>", self._on_motion)
        for n in (1, 2, 3):
            c.bind(f"<ButtonPress-{n}>", lambda e, n=n: self._on_button(e, n, True))
            c.bind(f"<ButtonRelease-{n}>", lambda e, n=n: self._on_button(e, n, False))
        c.bind("<MouseWheel>", lambda e: self._on_wheel(e, 0, int(e.delta / 120) or (1 if e.delta > 0 else -1)))
        c.bind("<Shift-MouseWheel>", lambda e: self._on_wheel(e, int(e.delta / 120) or (1 if e.delta > 0 else -1), 0))
        if sys.platform.startswith("linux"):
            c.bind("<ButtonPress-4>", lambda e: self._on_wheel(e, 0, 1))
            c.bind("<ButtonPress-5>", lambda e: self._on_wheel(e, 0, -1))
        c.bind("<KeyPress>", self._on_key_press)
        c.bind("<KeyRelease>", self._on_key_release)
        c.bind("<FocusOut>", lambda e: self._release_keys())

    def _norm(self, event):
        ox, oy, w, h = self._view
        return (round(min(1.0, max(0.0, (event.x - ox) / w)), 5),
                round(min(1.0, max(0.0, (event.y - oy) / h)), 5))

    def _on_motion(self, event):
        self._pending_move = self._norm(event)  # coalesced, sent from _tick

    def _on_button(self, event, number, down):
        self.canvas.focus_set()
        self._pending_move = None
        x, y = self._norm(event)
        self.session.send_input({"t": "btn", "b": number, "down": down, "x": x, "y": y})

    def _on_wheel(self, event, dx, dy):
        x, y = self._norm(event)
        self.session.send_input({"t": "wheel", "dx": dx, "dy": dy, "x": x, "y": y})

    def _on_key_press(self, event):
        ev = {"t": "key", "down": True, "sym": event.keysym, "ch": event.char}
        if sys.platform == "win32":
            ev["vk"] = event.keycode
        self._keys_down[event.keycode] = ev
        self.session.send_input(ev)
        return "break"

    def _on_key_release(self, event):
        ev = self._keys_down.pop(event.keycode, None)
        if ev is None:
            ev = {"t": "key", "sym": event.keysym, "ch": event.char}
            if sys.platform == "win32":
                ev["vk"] = event.keycode
        self.session.send_input(dict(ev, down=False))
        return "break"

    def _release_keys(self):
        for ev in self._keys_down.values():
            self.session.send_input(dict(ev, down=False))
        self._keys_down.clear()

    # -- rendering ----------------------------------------------------------
    def _tick(self):
        s = self.session
        if s.closed:
            self._finish()
            return
        if self._pending_move is not None:
            x, y = self._pending_move
            self._pending_move = None
            s.send_input({"t": "move", "x": x, "y": y})
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if (s.dirty or size != self._last_size) and s.image is not None and size[0] > 1 and size[1] > 1:
            s.dirty = False
            self._last_size = size
            self._render(size)
        if self._fullscreen:
            self._autohide_bar()
        self._update_status()
        self.after(8, self._tick)

    def _render(self, size):
        s = self.session
        if size != s.target:            # window resized: rescale once here, later frames come ready
            s.target = size
            s.rescale()
        with s.lock:                    # shown is updated in place by the reader thread
            shown, view = s.shown, s.view
            if shown is None:
                return
            self._view = view
            if self._photo is not None and (self._photo.width(), self._photo.height()) == shown.size:
                self._photo.paste(shown)    # ~1.5 ms, versus ~5-11 ms for a new PhotoImage
                fresh = False
            else:
                self._photo = ImageTk.PhotoImage(shown)
                fresh = True
        if fresh:
            self.canvas.itemconfigure(self._item, image=self._photo)
        self.canvas.coords(self._item, view[0], view[1])

    def _update_status(self):
        now = time.monotonic()
        elapsed = now - self._stat_time
        if elapsed < 1.0:
            return
        s = self.session
        fps = (s.frames - self._stat_frames) / elapsed
        kbps = (s.conn.bytes_in - self._stat_bytes) * 8 / 1000 / elapsed
        self._stat_time, self._stat_frames, self._stat_bytes = now, s.frames, s.conn.bytes_in
        rate = f"{kbps / 1000:.1f} Мбит/с" if kbps >= 1000 else f"{kbps:.0f} кбит/с"
        text = f"{fps:.0f} к/с · {rate}"
        if s.transfer:
            text = f"{s.transfer} · {text}"
        self._status.configure(text=text)

    # -- closing ------------------------------------------------------------
    def close(self):
        self._user_closed = True
        if not self.session.closed:
            self._release_keys()
            self.session.send(Msg.BYE, b"{}")
            self.after(100, lambda: self.session.close("closed"))
            self.after(120, self._finish)
        else:
            self._finish()

    def _finish(self):
        reason = self.session.close_reason
        self.session.close()
        if self._finished:
            return
        self._finished = True
        try:
            if not self._user_closed and reason in ("connection lost", "closed by host"):
                text = "Соединение потеряно." if reason == "connection lost" else "Хост завершил сеанс."
                messagebox.showinfo("Minideck", text, parent=self)
            self.destroy()
        except tk.TclError:
            pass
        if self.on_closed:
            self.on_closed(self)
