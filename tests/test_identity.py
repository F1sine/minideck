"""Host identity: the viewer must be able to tell one computer from an impostor.

Run from the repository root, no test framework needed:

    python tests/test_identity.py
"""
import json
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import minideck.config as C                                            # noqa: E402
import minideck.host as H                                              # noqa: E402
from minideck.proto import (IdentityError, IdentityMismatch, Msg, handshake,   # noqa: E402
                            human_fingerprint, identity_of, load_identity, sign_identity)
from minideck.viewer import ViewerSession                              # noqa: E402

PASSWORD = "test-password-123"
failures = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f" - {extra}" if extra else ""))
    if not ok:
        failures.append(name)


class DummyGrabber:
    backend = "dummy"
    left = top = 0
    width = height = 100

    def grab(self, force=False):
        return None

    def close(self):
        pass


def temp_identity():
    return load_identity(Path(tempfile.mkdtemp(prefix="minideck-key-")) / "host_key")


def start_host(identity):
    """A real Host on a free port. Returns (host, factory for the viewer's socket)."""
    host = H.Host(PASSWORD, port=0, allow_control=True, log=lambda msg: None, identity=identity)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def accept():
        sock, addr = listener.accept()
        host.handle_socket(sock, addr[0])

    threading.Thread(target=accept, daemon=True).start()
    return host, lambda: socket.create_connection(("127.0.0.1", port), timeout=10)


def start_fake_host(behaviour):
    """A host that speaks the protocol by hand, to play the part of an impostor."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def accept():
        sock, _ = listener.accept()
        try:
            conn = handshake(sock, PASSWORD, True)
            conn.recv()                      # the viewer's AUTH
            behaviour(conn)
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    return lambda: socket.create_connection(("127.0.0.1", port), timeout=10)


def connect(factory, **kwargs):
    session = ViewerSession("test-host", PASSWORD, stream_factory=factory, **kwargs)
    session.connect()
    return session


# -- the key itself ------------------------------------------------------------
def test_key_is_created_once_and_kept():
    folder = Path(tempfile.mkdtemp(prefix="minideck-key-"))
    first = load_identity(folder / "host_key")
    check("the key file is written", (folder / "host_key").exists())
    check("the key is 32 raw bytes", len((folder / "host_key").read_bytes()) == 32)
    second = load_identity(folder / "host_key")
    check("the same key comes back next time", identity_of(first) == identity_of(second))
    check("two computers get different keys", identity_of(first) != identity_of(temp_identity()))
    fingerprint = identity_of(first)
    check("the fingerprint is 32 hex characters",
          len(fingerprint) == 32 and all(c in "0123456789abcdef" for c in fingerprint), fingerprint)
    check("it is shown in groups of four",
          human_fingerprint(fingerprint).split() == [fingerprint[i:i + 4] for i in range(0, 32, 4)])


# -- what the viewer learns and remembers --------------------------------------
def test_first_connection_learns_the_key():
    identity = temp_identity()
    host, factory = start_host(identity)
    session = connect(factory, identity_slot="addr:test")
    check("the viewer learns the host's fingerprint", session.identity == identity_of(identity),
          f"{session.identity} vs {identity_of(identity)}")
    session.close()
    host.stop()


def test_matching_key_connects():
    identity = temp_identity()
    host, factory = start_host(identity)
    session = connect(factory, identity_slot="addr:test", expect_identity=identity_of(identity))
    check("a host that proves the remembered key connects", session.identity == identity_of(identity))
    session.close()
    host.stop()


def test_changed_key_is_refused():
    host, factory = start_host(temp_identity())
    raised = None
    try:
        connect(factory, identity_slot="addr:test", expect_identity="0" * 32)
    except BaseException as exc:
        raised = exc
    check("a different key is refused", isinstance(raised, IdentityMismatch), repr(raised))
    check("the refusal names both keys",
          getattr(raised, "expected", None) == "0" * 32 and len(getattr(raised, "got", "")) == 32)
    host.stop()


def test_changed_key_can_be_accepted_by_the_user():
    identity = temp_identity()
    host, factory = start_host(identity)
    asked = []

    def answer(got, expected):
        asked.append((got, expected))
        return True                     # as if the person pressed "trust the new key"

    session = connect(factory, identity_slot="addr:test", expect_identity="0" * 32,
                      on_identity_changed=answer)
    check("the user is asked about the new key",
          asked and asked[0] == (identity_of(identity), "0" * 32), f"asked={asked!r}")
    check("saying yes lets the session through", session.identity == identity_of(identity))
    session.close()
    host.stop()


# -- what a man in the middle can and cannot do --------------------------------
def test_signature_is_bound_to_this_exchange():
    """Forwarding the real host's signature from another exchange must not verify."""
    identity = temp_identity()

    def behaviour(conn):
        stolen = sign_identity(identity, b"some other exchange" + b"\0" * 13)
        conn.send_json(Msg.IDENT, stolen)
        conn.send_json(Msg.HELLO, {"name": "impostor", "control": True})

    raised = None
    try:
        connect(start_fake_host(behaviour), identity_slot="addr:test",
                expect_identity=identity_of(identity))
    except BaseException as exc:
        raised = exc
    check("a signature from another exchange is rejected", isinstance(raised, IdentityError),
          repr(raised))


def test_impostors_own_key_is_caught_by_the_pin():
    """A man in the middle can only sign with a key the viewer has never seen."""
    real, impostor = temp_identity(), temp_identity()

    def behaviour(conn):
        conn.send_json(Msg.IDENT, sign_identity(impostor, conn.transcript))
        conn.send_json(Msg.HELLO, {"name": "impostor", "control": True})

    raised = None
    try:
        connect(start_fake_host(behaviour), identity_slot="addr:test",
                expect_identity=identity_of(real))
    except BaseException as exc:
        raised = exc
    check("its own key does not match the remembered one", isinstance(raised, IdentityMismatch),
          repr(raised))


def test_missing_identity_is_refused():
    def behaviour(conn):
        conn.send_json(Msg.HELLO, {"name": "silent", "control": True})   # no IDENT at all

    raised = None
    try:
        connect(start_fake_host(behaviour), identity_slot="addr:test")
    except BaseException as exc:
        raised = exc
    check("a host that says nothing about its key is refused",
          isinstance(raised, IdentityError), repr(raised))


# -- where the fingerprints are kept -------------------------------------------
def test_config_helpers():
    check("a 9-digit ID gets its own slot", C.identity_slot("123456789") == "id:123456789")
    check("an address is lower-cased", C.identity_slot(" Host.Example:7070 ") == "addr:host.example:7070")

    saved = C.PATH
    C.PATH = Path(tempfile.mkdtemp(prefix="minideck-cfg-")) / "config.json"
    try:
        cfg = dict(C.DEFAULTS)
        check("nothing is known at first", C.known_identity(cfg, "addr:x") is None)
        check("the first fingerprint is stored", C.remember_identity(cfg, "addr:x", "ab" * 16))
        check("it is found again", C.known_identity(cfg, "addr:x") == "ab" * 16)
        check("storing the same one again changes nothing",
              not C.remember_identity(cfg, "addr:x", "ab" * 16))
        check("it reached the file",
              json.loads(C.PATH.read_text(encoding="utf-8"))["known_hosts"]["addr:x"] == "ab" * 16)
        check("a changed fingerprint replaces it", C.remember_identity(cfg, "addr:x", "cd" * 16))
    finally:
        C.PATH = saved


def test_server_keeps_the_fingerprint():
    """A management server stores it, including in a database made before this column existed."""
    import sqlite3

    from minideck.server import Store

    folder = Path(tempfile.mkdtemp(prefix="minideck-db-"))
    fresh = Store(folder / "fresh.db")
    fresh.add_device("123456789", "hash", "PC", "win32", "ab" * 16)
    check("a new database keeps the fingerprint",
          fresh.device("123456789")["fingerprint"] == "ab" * 16)
    fresh.update_device("123456789", fingerprint="cd" * 16)
    check("it can be replaced", fresh.device("123456789")["fingerprint"] == "cd" * 16)
    fresh.db.close()

    old = sqlite3.connect(folder / "old.db")       # the 0.4.0 schema, without the column
    old.executescript("""
        CREATE TABLE devices(id TEXT PRIMARY KEY, key_hash TEXT, name TEXT, host TEXT,
            platform TEXT, grp TEXT, enrolled INTEGER, last_seen INTEGER);
        INSERT INTO devices VALUES('987654321','hash','Old','Old','win32','',0,0);
    """)
    old.commit()
    old.close()
    migrated = Store(folder / "old.db")
    device = migrated.device("987654321")
    check("an existing database gains the column", "fingerprint" in device,
          f"columns={list(device)}")
    check("the computer is still there, with no fingerprint yet", not device["fingerprint"])
    migrated.add_device("123456789", "hash", "PC", "win32", "ef" * 16)
    check("and new computers can still be enrolled",
          migrated.device("123456789")["fingerprint"] == "ef" * 16)
    migrated.db.close()


def main():
    H.ScreenGrabber = DummyGrabber
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
    print()
    print("FAILED: " + ", ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
