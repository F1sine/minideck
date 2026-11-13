"""Host side: shares this computer's screen and accepts remote input."""
import json
import os
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import DEFAULT_PORT, DEFAULT_RELAY_PORT, VERSION
from .journal import human_duration, human_size
from .netutil import LineReader, connect_tcp, tune
from .proto import AUTH_MAX_MESSAGE, MAX_MESSAGE, AuthError, Msg, ProtocolError, handshake
from .screen import ScreenGrabber, changed_rects, encode_frame

DEFAULT_FPS = 30

DOWNLOAD_DIR = Path.home() / "Downloads" / "Minideck"
MAX_FAILS = 5              # wrong passwords tolerated from one address ...
FAIL_WINDOW = 60.0         # ... within this many seconds
MAX_HANDSHAKES = 4         # at once: each one costs scrypt memory, so this bounds the hit
HANDSHAKE_WAIT = 10.0      # seconds a connection waits for a free slot
MAX_FILE = 2 * 1024 ** 3         # largest single file a viewer may send to this computer
MAX_FILES_TOTAL = 8 * 1024 ** 3  # ... and the total one session may write


class HostSession:
    def __init__(self, host, conn, peer, name, control=True, account=None):
        self.host = host
        self.conn = conn
        self.peer = peer
        self.name = name
        self.control = control   # per-session cap, e.g. a view-only role on the server
        self.started = time.time()
        self.account = account   # server account behind a brokered session, else None
        self.kicked = False      # ended by the person at this computer
        self._file_size = 0
        self._files_total = 0    # bytes written by this session, against MAX_FILES_TOTAL
        self.running = True
        self.quality = 60
        self.scale = 1.0
        self.fps = DEFAULT_FPS
        self.capture_backend = ""
        self.force_full = True
        self.geom = None
        self._file = None
        self._file_path = None

    # -- screen streaming ---------------------------------------------------
    def _capture_loop(self):
        try:
            grabber = ScreenGrabber()
        except Exception as exc:
            self.host.log(f"screen capture unavailable: {exc}")
            self.stop()
            return
        self.geom = (grabber.left, grabber.top, grabber.width, grabber.height)
        self.capture_backend = grabber.backend
        from .inputctl import keep_display_on, wake_display
        keep_display_on(True)            # a blanked monitor would freeze the picture
        if self.can_control:             # view-only sessions never inject input, not even this
            wake_display()
        self.host.log(f"screen capture: {grabber.backend}")
        prev = None
        next_at = time.monotonic()
        try:
            while self.running:
                force = self.force_full
                frame = grabber.grab(force=force or prev is None)
                if frame is not None:
                    if force:
                        self.force_full = False
                        prev = None
                    rects = changed_rects(prev, frame)
                    if rects:
                        # sendall blocks on a slow link: that is the back-pressure that
                        # lowers the frame rate instead of piling frames up in memory
                        self.conn.send(Msg.FRAME, encode_frame(frame, rects, self.quality, self.scale))
                    prev = frame
                # fixed cadence: the next frame is due 1/fps after the previous one was due
                next_at += 1.0 / self.fps
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_at = time.monotonic()   # running behind: don't try to catch up
        except (OSError, ValueError):
            pass                             # connection closed
        except Exception as exc:             # never leave the viewer staring at a frozen picture
            self.host.log(f"screen capture stopped: {exc!r}")
        finally:
            keep_display_on(False)
            grabber.close()
            self.stop()

    # -- incoming messages --------------------------------------------------
    @property
    def can_control(self):
        return self.control and self.host.allow_control

    def _on_input(self, payload):
        if not self.can_control or self.geom is None:
            return
        injector = self.host.injector()
        if injector is not None:
            injector.handle(json.loads(payload), self.geom)

    def _on_settings(self, payload):
        cfg = json.loads(payload)
        if "quality" in cfg:
            self.quality = min(95, max(10, int(cfg["quality"])))
        if "scale" in cfg:
            self.scale = min(1.0, max(0.25, float(cfg["scale"])))
        if "fps" in cfg:
            self.fps = min(60, max(1, int(cfg["fps"])))
        self.force_full = True

    def _file_begin(self, payload):
        self._file_close(False)
        if not self.can_control:
            return
        info = json.loads(payload)
        name = os.path.basename(str(info.get("name", "file")).replace("\\", "/")) or "file"
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = DOWNLOAD_DIR / name
        stem, suffix, n = path.stem, path.suffix, 1
        while path.exists():
            path = DOWNLOAD_DIR / f"{stem} ({n}){suffix}"
            n += 1
        if self._files_total >= MAX_FILES_TOTAL:
            self._refuse_file(path.name, "сеанс исчерпал лимит на передачу файлов")
            return
        self._file = open(path, "wb")
        self._file_path = path
        self._file_size = 0

    def _refuse_file(self, name, reason):
        """Refuse a file instead of letting it fill the disk, and say so in the journal."""
        self.host.log(f"file refused: {name} - {reason}")
        self.host.event("file_refused", self, f"{name}: {reason}")

    def _file_chunk(self, payload):
        """Write one chunk, dropping the file once it outgrows a limit."""
        if self._file is None:
            return
        size = self._file_size + len(payload)
        if size > MAX_FILE or self._files_total + size > MAX_FILES_TOTAL:
            name = self._file_path.name
            self._file_close(False)
            self._refuse_file(name, f"больше {human_size(MAX_FILE)}" if size > MAX_FILE
                              else "сеанс исчерпал лимит на передачу файлов")
            return
        self._file.write(payload)
        self._file_size = size

    def _file_close(self, complete):
        if self._file is None:
            return
        self._file.close()
        self._file = None
        if complete:
            self._files_total += self._file_size
            self.host.log(f"file received: {self._file_path}")
            self.host.event("file_received", self, f"{self._file_path.name} ({human_size(self._file_size)})")
        else:
            try:
                self._file_path.unlink()
            except OSError:
                pass

    def run(self):
        self.conn.send_json(Msg.HELLO, {
            "name": socket.gethostname(), "platform": sys.platform,
            "control": self.can_control, "v": VERSION,
        })
        threading.Thread(target=self._capture_loop, daemon=True).start()
        try:
            while self.running:
                mtype, payload = self.conn.recv()
                if mtype == Msg.INPUT:
                    self._on_input(payload)
                elif mtype == Msg.SETTINGS:
                    self._on_settings(payload)
                elif mtype == Msg.REFRESH:
                    self.force_full = True
                elif mtype == Msg.CLIPBOARD:
                    # a view-only session never writes to this computer, the clipboard included
                    if self.can_control and self.host.on_clipboard:
                        text = payload.decode("utf-8", "replace")
                        self.host.on_clipboard(text)
                        self.host.event("clipboard_received", self, f"{len(text)} симв.")
                elif mtype == Msg.FILE_BEGIN:
                    self._file_begin(payload)
                elif mtype == Msg.FILE_CHUNK:
                    self._file_chunk(payload)
                elif mtype == Msg.FILE_END:
                    self._file_close(True)
                elif mtype == Msg.BYE:
                    break
        except (OSError, ValueError, AuthError, ProtocolError):
            pass
        finally:
            self._file_close(False)
            self.stop()

    def stop(self):
        self.running = False
        self.conn.close()

    def kick(self):
        """End the session from this computer's side (recorded as such in the journal)."""
        self.kicked = True
        self.stop()


class Host:
    """Listens for viewers (directly and/or through a relay) and runs their sessions."""

    def __init__(self, password, port=DEFAULT_PORT, allow_control=True,
                 on_confirm=None, on_change=None, on_clipboard=None, log=None, on_event=None):
        self.password = password
        self.port = port
        self.allow_control = allow_control
        self.on_confirm = on_confirm      # (peer, name) -> bool, may block
        self.on_change = on_change        # called when the session list changes
        self.on_clipboard = on_clipboard  # (text) from a viewer
        self.on_event = on_event          # (action, who, detail, account) for the journal
        self.log = log or (lambda msg: print(f"[host] {msg}", flush=True))
        self.sessions = []
        self.listen_error = None
        self._lock = threading.Lock()
        self._fails = {}         # address -> when it recently got the password wrong
        self._slots = threading.Semaphore(MAX_HANDSHAKES)
        self._server = None
        self._injector = None
        self._injector_failed = False
        self._stopped = False

    def event(self, action, session=None, detail="", who=""):
        """Record something for the journal. Never raises."""
        if self.on_event is None:
            return
        if session is not None:
            who = f"{session.name} · {session.peer}"
        try:
            self.on_event(action, who, detail, session.account if session is not None else None)
        except Exception:
            pass

    def injector(self):
        if self._injector is None and not self._injector_failed:
            try:
                from .inputctl import InputInjector
                self._injector = InputInjector()
            except Exception as exc:
                self._injector_failed = True
                self.log(f"remote control unavailable: {exc}")
        return self._injector

    # -- listening ----------------------------------------------------------
    def start(self):
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if sys.platform != "win32":
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", self.port))
            server.listen(8)
        except OSError as exc:
            self.listen_error = str(exc)
            self.log(f"cannot listen on port {self.port}: {exc}")
            return False
        self._server = server
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return True

    def _accept_loop(self):
        while not self._stopped:
            try:
                sock, addr = self._server.accept()
            except OSError:
                break
            tune(sock)
            threading.Thread(target=self.handle_socket, args=(sock, addr[0]),
                             daemon=True).start()

    def stop(self):
        self._stopped = True
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        self.disconnect_all()

    def disconnect_all(self, kick=False):
        with self._lock:
            sessions = list(self.sessions)
        for session in sessions:
            session.kick() if kick else session.stop()

    def send_clipboard(self, text):
        with self._lock:
            sessions = list(self.sessions)
        for session in sessions:
            try:
                session.conn.send(Msg.CLIPBOARD, text.encode("utf-8"))
                self.event("clipboard_sent", session, f"{len(text)} симв.")
            except OSError:
                pass

    # -- one viewer ---------------------------------------------------------
    def _locked_out(self, peer):
        """True when this address has used up its attempts within the window.

        Counted per address: someone hammering the port cannot shut everyone else out.
        """
        now = time.monotonic()
        with self._lock:
            for address, fails in list(self._fails.items()):
                while fails and now - fails[0] > FAIL_WINDOW:
                    fails.popleft()
                if not fails:
                    del self._fails[address]
            return len(self._fails.get(peer, ())) >= MAX_FAILS

    def handle_socket(self, sock, peer, password=None, control=True, label=None, account=None):
        """Authenticate a raw stream and run the session until it ends.

        Sessions brokered by a management server pass a one-time password, the
        permission granted by the server and the server-verified account name.
        """
        if self._locked_out(peer):
            sock.close()
            return
        if not self._slots.acquire(timeout=HANDSHAKE_WAIT):
            # too many handshakes in flight: better to turn one away than to run out of memory
            self.log(f"busy, refused {peer}")
            sock.close()
            return
        conn = None
        authenticated = False
        try:
            sock.settimeout(15)
            conn = handshake(sock, password or self.password, True)
            conn.max_message = AUTH_MAX_MESSAGE
            try:
                mtype, payload = conn.recv()
            except AuthError:
                with self._lock:
                    self._fails.setdefault(peer, deque()).append(time.monotonic())
                self.log(f"wrong password from {peer}")
                self.event("wrong_password", who=str(peer))
                return
            if mtype != Msg.AUTH:
                return
            name = label or str(json.loads(payload).get("name", "?"))[:64]
            conn.max_message = MAX_MESSAGE   # frames and file chunks are big; the password is in
            sock.settimeout(None)
            self._slots.release()            # the expensive part is over
            authenticated = True
            if self.on_confirm and not self.on_confirm(peer, name):
                conn.send_json(Msg.BYE, {"reason": "rejected"})
                self.log(f"rejected {name} ({peer})")
                self.event("rejected", HostSession(self, conn, peer, name, control, account))
                return
            session = HostSession(self, conn, peer, name, control, account)
            with self._lock:
                self.sessions.append(session)
            self.log(f"connected: {name} ({peer})")
            self.event("session_start", session, "управление" if session.can_control else "только просмотр")
            self._changed()
            try:
                session.run()
            finally:
                with self._lock:
                    self.sessions.remove(session)
                    last = not self.sessions
                if last and self._injector is not None:
                    self._injector.release_all()
                self.log(f"disconnected: {name} ({peer})")
                if session.kicked:
                    self.event("kicked", session)
                self.event("session_end", session, "длительность " + human_duration(time.time() - session.started))
                self._changed()
        except (OSError, ValueError, ProtocolError):
            pass
        finally:
            if not authenticated:
                self._slots.release()
            if conn is not None:
                conn.close()
            else:
                sock.close()

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass


class RelayClient:
    """Keeps this host registered at a relay under its ID and accepts viewers from it."""

    def __init__(self, host, relay, ident, key, on_status=None):
        self.host = host
        self.relay = relay
        self.ident = ident
        self.key = key
        self.on_status = on_status
        self.status = "connecting"
        self._stopped = False
        self._sock = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stopped = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _set(self, status):
        if status != self.status:
            self.status = status
            if self.on_status:
                self.on_status(status)

    def _run(self):
        while not self._stopped:
            wait = 5
            try:
                sock = connect_tcp(self.relay, DEFAULT_RELAY_PORT)
                self._sock = sock
                sock.settimeout(15)
                sock.sendall(f"REGISTER {self.ident} {self.key}\n".encode())
                lines = LineReader(sock)
                reply = lines.readline()
                if reply != "OK":
                    self._set(f"error: {reply}")
                    wait = 30
                    raise ConnectionError(reply)
                self._set("online")
                sock.settimeout(25)
                while not self._stopped:
                    try:
                        line = lines.readline()
                    except socket.timeout:
                        sock.sendall(b"PING\n")
                        continue
                    if line.startswith("INCOMING "):
                        token = line.split()[1]
                        threading.Thread(target=self._accept, args=(token,),
                                         daemon=True).start()
            except (OSError, ValueError, IndexError):
                if not self.status.startswith("error"):
                    self._set("offline")
            finally:
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
            for _ in range(wait * 10):
                if self._stopped:
                    return
                time.sleep(0.1)

    def _accept(self, token):
        try:
            sock = connect_tcp(self.relay, DEFAULT_RELAY_PORT)
            sock.settimeout(15)
            sock.sendall(f"ACCEPT {token}\n".encode())
            if LineReader(sock).readline() != "OK":
                sock.close()
                return
        except OSError:
            return
        self.host.handle_socket(sock, "relay")
