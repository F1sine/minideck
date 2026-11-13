"""What a session is and is not allowed to do to the host computer.

These are the guard rails that are easy to take away by accident, so each one has a
check here: a view-only session must not write to this computer, wrong passwords must
only lock out the address that sent them, and nothing may fill the disk or the memory.

Run from the repository root, no test framework needed:

    python tests/test_security.py
"""
import json
import socket
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import minideck.host as H                                            # noqa: E402
from minideck.proto import (AUTH_MAX_MESSAGE, MAX_MESSAGE, Msg, handshake,   # noqa: E402
                            load_identity)

PASSWORD = "test-password-123"
failures = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f" - {extra}" if extra else ""))
    if not ok:
        failures.append(name)


class DummyGrabber:
    """A screen that never produces a frame, so a session can run without a display."""
    backend = "dummy"
    left = top = 0
    width = height = 100

    def grab(self, force=False):
        return None

    def close(self):
        pass


def temp_identity():
    """A throwaway host key, so a test never touches the key in the user's own profile."""
    return load_identity(Path(tempfile.mkdtemp(prefix="minideck-key-")) / "host_key")


def start_host(control):
    """A host listening on a free port. Returns (host, port, clipboard texts it accepted)."""
    accepted = []
    host = H.Host(PASSWORD, port=0, allow_control=True, on_clipboard=accepted.append,
                  log=lambda msg: None, identity=temp_identity())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def accept():
        sock, addr = listener.accept()
        host.handle_socket(sock, addr[0], control=control)

    threading.Thread(target=accept, daemon=True).start()
    return host, listener.getsockname()[1], accepted


def connect(port, password=PASSWORD):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn = handshake(sock, password, False)
    conn.send_json(Msg.AUTH, {"name": "test"})
    return sock, conn


def session(control=True):
    """A HostSession on its own, for the parts that need no socket."""
    host = H.Host(PASSWORD, port=0, log=lambda msg: None)
    events = []
    host.on_event = lambda action, who, detail, account: events.append((action, detail))
    return H.HostSession(host, conn=None, peer="127.0.0.1", name="test", control=control), events


# -- a view-only session never writes to this computer -------------------------
def test_clipboard_needs_control():
    for control, expected in ((True, 1), (False, 0)):
        host, port, accepted = start_host(control)
        sock, conn = connect(port)
        conn.recv()                                   # IDENT: the host proves which computer it is
        conn.send(Msg.CLIPBOARD, "secret".encode("utf-8"))
        time.sleep(0.4)
        check("clipboard " + ("with control" if control else "view-only"),
              len(accepted) == expected, f"accepted={accepted!r}")
        conn.close()
        host.stop()


def test_file_needs_control():
    view_only, _ = session(control=False)
    folder = Path(tempfile.mkdtemp(prefix="minideck-test-"))
    saved, H.DOWNLOAD_DIR = H.DOWNLOAD_DIR, folder
    try:
        view_only._file_begin(json.dumps({"name": "x.bin", "size": 4}).encode())
        view_only._file_chunk(b"data")
        view_only._file_close(True)
        check("view-only session cannot send a file", not any(folder.iterdir()))
    finally:
        H.DOWNLOAD_DIR = saved


# -- wrong passwords are counted per address -----------------------------------
def test_lockout_is_per_address():
    host = H.Host(PASSWORD, port=0, log=lambda msg: None)
    now = time.monotonic()
    host._fails["1.2.3.4"] = deque([now] * H.MAX_FAILS)
    check("the address that used up its attempts is locked out", host._locked_out("1.2.3.4"))
    check("another address is not locked out", not host._locked_out("5.6.7.8"))
    host._fails["9.9.9.9"] = deque([now - H.FAIL_WINDOW - 1] * H.MAX_FAILS)
    check("failures older than the window expire", not host._locked_out("9.9.9.9"))
    check("an address with no failures left is forgotten", "9.9.9.9" not in host._fails)


def test_wrong_password_is_recorded():
    host, port, _ = start_host(True)
    try:
        sock, conn = connect(port, "wrong-password")
        conn.close()
    except OSError:
        pass
    time.sleep(0.4)
    check("a wrong password is counted against the address that sent it",
          list(host._fails) == ["127.0.0.1"], f"fails={dict(host._fails)!r}")
    host.stop()


# -- nothing may fill the memory or the disk ------------------------------------
def test_pre_auth_message_limit():
    check("the pre-auth cap is smaller than the session cap", AUTH_MAX_MESSAGE < MAX_MESSAGE)
    host, port, _ = start_host(True)
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn = handshake(sock, PASSWORD, False)
    conn.send(Msg.AUTH, b"x" * (AUTH_MAX_MESSAGE + 1))
    time.sleep(0.4)
    try:
        sock.settimeout(2)
        closed = sock.recv(1) == b""
    except OSError:
        closed = True
    check("an oversized message before the password drops the connection", closed)
    conn.close()
    host.stop()


def test_file_limits():
    incoming, events = session()
    folder = Path(tempfile.mkdtemp(prefix="minideck-test-"))
    saved = H.MAX_FILE, H.MAX_FILES_TOTAL, H.DOWNLOAD_DIR
    H.MAX_FILE, H.MAX_FILES_TOTAL, H.DOWNLOAD_DIR = 1000, 2500, folder
    try:
        incoming._file_begin(json.dumps({"name": "big.bin", "size": 5000}).encode())
        incoming._file_chunk(b"x" * 600)
        incoming._file_chunk(b"x" * 600)                     # 1200 > MAX_FILE
        incoming._file_close(True)
        check("a file over the limit is not kept", not (folder / "big.bin").exists())
        check("the refusal reaches the journal", any(a == "file_refused" for a, _ in events),
              f"events={events!r}")

        incoming._file_begin(json.dumps({"name": "ok.bin", "size": 900}).encode())
        incoming._file_chunk(b"y" * 900)
        incoming._file_close(True)
        check("a file within the limit is kept", (folder / "ok.bin").read_bytes() == b"y" * 900)
        check("it counts against the session total", incoming._files_total == 900)

        incoming._files_total = H.MAX_FILES_TOTAL
        events.clear()
        incoming._file_begin(json.dumps({"name": "late.bin", "size": 10}).encode())
        incoming._file_chunk(b"z" * 10)
        incoming._file_close(True)
        check("the session quota stops further files", not (folder / "late.bin").exists())
        check("that refusal reaches the journal too",
              any(a == "file_refused" for a, _ in events), f"events={events!r}")
    finally:
        H.MAX_FILE, H.MAX_FILES_TOTAL, H.DOWNLOAD_DIR = saved


def test_path_traversal():
    incoming, _ = session()
    folder = Path(tempfile.mkdtemp(prefix="minideck-test-"))
    saved, H.DOWNLOAD_DIR = H.DOWNLOAD_DIR, folder
    try:
        incoming._file_begin(json.dumps({"name": r"..\..\evil.txt", "size": 4}).encode())
        incoming._file_chunk(b"evil")
        incoming._file_close(True)
        check("a file name cannot point outside the download folder",
              (folder / "evil.txt").exists() and not (folder.parent / "evil.txt").exists())
    finally:
        H.DOWNLOAD_DIR = saved


def main():
    H.ScreenGrabber = DummyGrabber          # no display is needed to run a session
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
    print()
    print("FAILED: " + ", ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
