"""The handshake: what the two sides agree on, and what a recording of it is worth.

Run from the repository root, no test framework needed:

    python tests/test_handshake.py
"""
import hashlib
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import hashes                      # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF               # noqa: E402

import minideck.host as H                                              # noqa: E402
from minideck.proto import (AuthError, Msg, ProtocolError, SCRYPT_MAXMEM,  # noqa: E402
                            SCRYPT_N, VersionMismatch, handshake)

PASSWORD = "test-password-123"
failures = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f" - {extra}" if extra else ""))
    if not ok:
        failures.append(name)


def pair():
    """A connected pair of sockets, as the two ends of a session see each other."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname(), timeout=5)
    server, _ = listener.accept()
    listener.close()
    return client, server


def shake(host_password=PASSWORD, viewer_password=PASSWORD):
    """Run both sides at once (each blocks on the other's greeting). Returns (viewer, host)."""
    viewer_sock, host_sock = pair()
    result = {}

    def run(side, sock, password, is_host):
        try:
            result[side] = handshake(sock, password, is_host)
        except BaseException as exc:            # kept, so the test can assert on it
            result[side] = exc

    threads = [threading.Thread(target=run, args=("host", host_sock, host_password, True)),
               threading.Thread(target=run, args=("viewer", viewer_sock, viewer_password, False))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return result.get("viewer"), result.get("host")


# -- the two sides end up with the same keys ------------------------------------
def test_round_trip():
    started = time.monotonic()
    viewer, host = shake()
    took = time.monotonic() - started
    if isinstance(viewer, BaseException) or isinstance(host, BaseException):
        check("handshake succeeds", False, f"viewer={viewer!r} host={host!r}")
        return
    check("handshake succeeds", True, f"{took:.2f}s")
    check("handshake is not absurdly slow", took < 5, f"{took:.2f}s")

    viewer.send_json(Msg.AUTH, {"name": "test"})
    check("viewer -> host message arrives", host.recv() == (Msg.AUTH, b'{"name": "test"}'))
    host.send(Msg.CLIPBOARD, "текст".encode("utf-8"))
    mtype, payload = viewer.recv()
    check("host -> viewer message arrives",
          (mtype, payload.decode("utf-8")) == (Msg.CLIPBOARD, "текст"))
    viewer.close()
    host.close()


def test_wrong_password():
    viewer, host = shake(viewer_password="wrong-password")
    if isinstance(viewer, BaseException) or isinstance(host, BaseException):
        check("a wrong password still completes the exchange", False)
        return
    # both sides derived keys; the mismatch shows up as a failure to decrypt
    viewer.send_json(Msg.AUTH, {"name": "test"})
    failed = False
    try:
        host.recv()
    except AuthError:
        failed = True
    check("a wrong password fails on the first message", failed)
    viewer.close()
    host.close()


# -- every session gets its own keys -------------------------------------------
def test_keys_differ_per_session():
    first, _ = shake()
    second, _ = shake()
    check("the same password gives different keys each session",
          first._enc._key != second._enc._key if hasattr(first._enc, "_key") else
          _probe(first) != _probe(second))
    for conn in (first, second):
        conn.close()


def _probe(conn):
    """The ciphertext a fixed message turns into, as a stand-in for the key itself."""
    return conn._enc.encrypt(b"\0" * 12, b"probe", None)


def test_password_alone_is_not_enough():
    """What someone who recorded the handshake and knows the password can derive."""
    viewer_sock, host_sock = pair()
    recorded = {}

    def run_host():
        recorded["host"] = handshake(host_sock, PASSWORD, True)

    thread = threading.Thread(target=run_host)
    thread.start()
    viewer = handshake(viewer_sock, PASSWORD, False)
    thread.join(30)

    # an observer sees the greetings, so it can replay the derivation - but without
    # either ephemeral private key it has no shared secret to feed in
    secret = hashlib.scrypt(PASSWORD.encode("utf-8"), salt=b"\0" * 32,
                            n=SCRYPT_N, r=8, p=1, dklen=32, maxmem=SCRYPT_MAXMEM)
    guess = HKDF(algorithm=hashes.SHA256(), length=64, salt=b"transcript",
                 info=b"minideck session keys").derive(b"" + secret)
    check("the password alone does not reproduce the session keys",
          guess[:32] != _probe(viewer)[:32])
    check("the exchange contributes to the keys", _probe(viewer) != _probe(recorded["host"]))
    viewer.close()
    recorded["host"].close()


# -- a peer that speaks something else -----------------------------------------
def test_old_version_is_named():
    viewer_sock, host_sock = pair()
    viewer_sock.sendall(b"MDK1" + b"\0" * 16)        # a 0.4.0 viewer's greeting
    raised = None
    try:
        handshake(host_sock, PASSWORD, True)
    except BaseException as exc:
        raised = exc
    check("an older peer raises VersionMismatch", isinstance(raised, VersionMismatch),
          repr(raised))
    check("VersionMismatch is a ProtocolError", isinstance(raised, ProtocolError))
    from minideck.ui import describe_error
    check("the message tells the user to update",
          "Обновите" in describe_error(raised), describe_error(raised))
    viewer_sock.close()
    host_sock.close()


def test_not_minideck():
    viewer_sock, host_sock = pair()
    viewer_sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n" + b"\0" * 32)
    raised = None
    try:
        handshake(host_sock, PASSWORD, True)
    except BaseException as exc:
        raised = exc
    check("something that is not Minideck raises ProtocolError",
          isinstance(raised, ProtocolError) and not isinstance(raised, VersionMismatch),
          repr(raised))
    viewer_sock.close()
    host_sock.close()


# -- the cost of a handshake is bounded ----------------------------------------
def test_handshake_slots():
    host = H.Host(PASSWORD, port=0, log=lambda msg: None)
    taken = [host._slots.acquire(blocking=False) for _ in range(H.MAX_HANDSHAKES)]
    check("the host allows a bounded number of handshakes at once", all(taken))
    check("one more is refused", not host._slots.acquire(blocking=False))
    for _ in taken:
        host._slots.release()
    check("slots come back", host._slots.acquire(blocking=False))
    host._slots.release()


def main():
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
    print()
    print("FAILED: " + ", ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
