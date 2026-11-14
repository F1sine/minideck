"""The administrator console: a password only ever goes to a server whose certificate was checked.

Run from the repository root, no test framework needed:

    python tests/test_admin_login.py
"""
import asyncio
import socket
import ssl
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minideck.server import Server, Store, ensure_cert, write_password_file   # noqa: E402
from minideck.serverapi import (AdminClient, CertificateMismatch,              # noqa: E402
                                CertificateRejected, fetch_fingerprint,
                                make_enroll_code, parse_server_address)

PASSWORD = "admin-password-123"
failures = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f" - {extra}" if extra else ""))
    if not ok:
        failures.append(name)


def start_server():
    """A real management server on a free port. Returns (address, fingerprint, store)."""
    data = Path(tempfile.mkdtemp(prefix="minideck-server-"))
    store = Store(data / "minideck.db")
    store.save_user("admin", PASSWORD, "admin", "*")
    crt, key, fingerprint = ensure_cert(data)
    ready, holder = threading.Event(), {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(crt, key)
        # the server talks to SQLite from its own thread, so it gets its own connection
        server = Server(Store(data / "minideck.db"), fingerprint, log=lambda msg: None, data_dir=data)
        listener = loop.run_until_complete(
            asyncio.start_server(server.handle, "127.0.0.1", 0, ssl=context))
        holder["port"] = listener.sockets[0].getsockname()[1]
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(10)
    return f"127.0.0.1:{holder['port']}", fingerprint, store


def audit_actions(store):
    return [row["action"] for row in store.audit(100)]


# -- what the administrator may type ------------------------------------------
def test_parse_server_address():
    fp = "ab" * 32
    check("a plain address has no fingerprint",
          parse_server_address(" srv.example:7072 ") == ("srv.example:7072", None))
    check("address#fingerprint gives both",
          parse_server_address(f"srv.example:7072#{fp.upper()}") == ("srv.example:7072", fp))
    check("a fingerprint may be grouped with spaces or colons",
          parse_server_address("srv:7072#" + ":".join(fp[i:i + 2] for i in range(0, 64, 2)))
          == ("srv:7072", fp))
    check("a whole enrollment code works too",
          parse_server_address(make_enroll_code("srv:7072", "token_1", fp)) == ("srv:7072", fp))
    raised = False
    try:
        parse_server_address("srv:7072#not-a-fingerprint")
    except ValueError:
        raised = True
    check("a malformed fingerprint is refused", raised)


# -- nothing is sent to an unchecked server ------------------------------------
def test_no_pin_no_password():
    # an address nobody listens on: if login tried to connect, this would be an OSError
    unused = socket.socket()
    unused.bind(("127.0.0.1", 0))
    port = unused.getsockname()[1]
    unused.close()
    raised = None
    try:
        AdminClient(f"127.0.0.1:{port}", None).login("admin", PASSWORD)
    except BaseException as exc:
        raised = exc
    check("login without a checked certificate refuses before connecting",
          isinstance(raised, CertificateRejected), repr(raised))


def test_against_a_real_server():
    address, fingerprint, store = start_server()

    seen = fetch_fingerprint(address)
    check("the console reads the certificate the server really has", seen == fingerprint,
          f"{seen[:16]}… vs {fingerprint[:16]}…")
    check("reading it sends no login", not any(a.startswith("login") for a in audit_actions(store)),
          f"audit={audit_actions(store)}")

    client = AdminClient(address, fingerprint)
    client.login("admin", PASSWORD)
    check("with the right certificate the admin gets in", client.role == "admin")
    client.close()

    raised = None
    try:
        AdminClient(address, "cd" * 32).login("admin", "wrong-on-purpose")
    except BaseException as exc:
        raised = exc
    check("a different certificate stops the login", isinstance(raised, CertificateMismatch),
          repr(raised))
    check("and that password never reached the server",
          "login_failed" not in audit_actions(store), f"audit={audit_actions(store)}")


def test_password_file_names_the_certificate():
    data = Path(tempfile.mkdtemp(prefix="minideck-server-"))
    path = write_password_file(data, "secret-password", "ef" * 32)
    text = path.read_text(encoding="utf-8")
    check("ADMIN-PASSWORD.txt carries the fingerprint to compare", ("ef" * 32) in text)


def main():
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
    print()
    print("FAILED: " + ", ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
