"""Password guessing on the management server: per address, per account, and bounded memory.

Run from the repository root, no test framework needed:

    python tests/test_server_limits.py
"""
import asyncio
import json
import sys
import tempfile
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import minideck.server as S                                            # noqa: E402

PASSWORD = "admin-password-123"
failures = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f" - {extra}" if extra else ""))
    if not ok:
        failures.append(name)


class FakeWriter:
    """Collects what the server answers; the session ends as soon as the reader is empty."""

    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(json.loads(data))

    async def drain(self):
        pass

    def close(self):
        pass


class EmptyReader:
    async def readline(self):
        return b""


def new_server():
    data = Path(tempfile.mkdtemp(prefix="minideck-server-"))
    store = S.Store(data / "minideck.db")
    store.save_user("admin", PASSWORD, "admin", "*")
    return S.Server(store, "ab" * 32, log=lambda msg: None, data_dir=data), store


def login(server, user, password, ip):
    """One login attempt from the given address; returns the server's answer."""
    writer = FakeWriter()
    asyncio.run(server._admin(EmptyReader(), writer, {"user": user, "password": password}, ip))
    return writer.sent[0]


def actions(store):
    return [row["action"] for row in store.audit(500)]


# -- one address --------------------------------------------------------------
def test_one_address_is_slowed_down():
    server, _ = new_server()
    for _ in range(S.MAX_FAILS):
        login(server, "admin", "wrong", "10.0.0.1")
    answer = login(server, "admin", PASSWORD, "10.0.0.1")
    check("after five wrong answers an address waits, even with the right password",
          answer.get("error") == "too many attempts", repr(answer))
    check("another address is not affected", login(server, "admin", PASSWORD, "10.0.0.2").get("ok"))


# -- one account, many addresses ----------------------------------------------
def test_account_locks_across_addresses():
    server, store = new_server()
    for i in range(S.ACCOUNT_FAILS):
        login(server, "admin", f"guess-{i}", f"10.1.0.{i}")     # each guess from a new address
    answer = login(server, "admin", PASSWORD, "10.2.0.1")
    check("ten wrong passwords from ten addresses lock the account",
          answer.get("error") == "too many attempts", repr(answer))
    check("the lock is in the journal, once", actions(store).count("account_locked") == 1,
          f"audit={actions(store)}")
    check("guesses while locked are not logged one by one",
          actions(store).count("login_failed") == S.ACCOUNT_FAILS)

    server.locked["admin"] = 0                    # as if the lock time had run out
    check("when the lock ends the right password works again",
          login(server, "admin", PASSWORD, "10.2.0.1").get("ok"))


def test_unknown_account_behaves_the_same():
    server, _ = new_server()
    for i in range(S.ACCOUNT_FAILS):
        login(server, "nobody", "guess", f"10.3.0.{i}")
    answer = login(server, "nobody", "guess", "10.4.0.1")
    check("a name that does not exist locks the same way, so a lock reveals nothing",
          answer.get("error") == "too many attempts", repr(answer))


def test_success_resets_the_count():
    server, _ = new_server()
    for i in range(S.ACCOUNT_FAILS - 1):
        login(server, "admin", "typo", f"10.5.0.{i}")
    check("a correct password still works just below the limit",
          login(server, "admin", PASSWORD, "10.6.0.1").get("ok"))
    check("and it starts the count again", "admin" not in server.account_fails)
    login(server, "admin", "typo", "10.6.0.2")
    check("so one more typo does not lock the account", login(server, "admin", PASSWORD, "10.6.0.3").get("ok"))


# -- memory --------------------------------------------------------------------
def test_tables_stay_bounded():
    server, _ = new_server()
    old = S.MAX_TRACKED
    S.MAX_TRACKED = 50
    try:
        for i in range(200):
            server._fail(f"10.7.{i // 250}.{i % 250}")
        check("the per-address table is capped", len(server.fails) <= 50, f"size={len(server.fails)}")
        server.fails = {"10.8.0.1": deque([0.0])}            # a failure long ago
        server._blocked("10.8.0.1")
        check("an address with nothing recent is forgotten", "10.8.0.1" not in server.fails)
        for i in range(200):
            server._account_failed(f"user{i}", "10.9.0.1")
        check("the per-account table is capped too", len(server.account_fails) <= 50,
              f"size={len(server.account_fails)}")
    finally:
        S.MAX_TRACKED = old


def main():
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
    print()
    print("FAILED: " + ", ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
