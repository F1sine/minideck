"""Management server: accounts with roles, enrolled computers, session brokering, audit log.

Everything runs over TLS on one port. The first line of a connection is a JSON
object whose "op" says what the connection is:

    device  a managed computer's permanent control channel (enrolled with a code)
    login   an administrator's API channel (request/response JSON lines)
    data    one half of a remote session; two halves with the same ticket are piped

Roles:  admin    - everything, including users, computers and the audit log
        operator - view and control the computers in their groups
        viewer   - view only
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import ssl
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DEFAULT_SERVER_PORT
from .journal import REPORTED
from .relay import _pipe

ROLES = ("admin", "operator", "viewer")
PASSWORD_FILE = "ADMIN-PASSWORD.txt"   # first/reset admin password; removed once it is changed
ID_RE = re.compile(r"\d{9}")
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{2,32}")
MAX_FAILS, FAIL_WINDOW = 5, 60.0


def _scrypt(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    """SQLite persistence. Passwords and device keys are stored only as hashes."""

    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, salt BLOB, hash BLOB,
                role TEXT, groups TEXT, created INTEGER);
            CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, key_hash TEXT, name TEXT,
                host TEXT, platform TEXT, grp TEXT, enrolled INTEGER, last_seen INTEGER);
            CREATE TABLE IF NOT EXISTS audit(ts INTEGER, user TEXT, action TEXT, target TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
        # the computer's own fingerprint, so a viewer can check it on the first connection
        if "fingerprint" not in {r["name"] for r in self.db.execute("PRAGMA table_info(devices)")}:
            self.db.execute("ALTER TABLE devices ADD COLUMN fingerprint TEXT DEFAULT ''")
            self.db.commit()
        if self.setting("enroll_token") is None:
            self.set_setting("enroll_token", secrets.token_urlsafe(18))

    # settings
    def setting(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))
        self.db.commit()

    # users
    def users(self):
        return [dict(name=r["name"], role=r["role"], groups=r["groups"], created=r["created"])
                for r in self.db.execute("SELECT * FROM users ORDER BY name")]

    def user(self, name):
        row = self.db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def save_user(self, name, password, role, groups):
        existing = self.user(name)
        if password:
            salt = os.urandom(16)
            digest = _scrypt(password, salt)
        elif existing:
            salt, digest = existing["salt"], existing["hash"]
        else:
            raise ValueError("password required")
        created = existing["created"] if existing else int(time.time())
        self.db.execute("INSERT OR REPLACE INTO users VALUES(?,?,?,?,?,?)",
                        (name, salt, digest, role, groups, created))
        self.db.commit()

    def delete_user(self, name):
        self.db.execute("DELETE FROM users WHERE name=?", (name,))
        self.db.commit()

    def verify(self, name, password):
        user = self.user(name)
        if user is None:
            _scrypt(password, b"0" * 16)  # same work either way: no user-name oracle
            return None
        return user if hmac.compare_digest(_scrypt(password, user["salt"]), user["hash"]) else None

    # devices
    def devices(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM devices ORDER BY name COLLATE NOCASE")]

    def device(self, ident):
        row = self.db.execute("SELECT * FROM devices WHERE id=?", (ident,)).fetchone()
        return dict(row) if row else None

    def add_device(self, ident, key_hash, name, platform, fingerprint=""):
        now = int(time.time())
        self.db.execute(
            "INSERT INTO devices(id, key_hash, name, host, platform, grp, enrolled, last_seen,"
            " fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
            (ident, key_hash, name, name, platform, "", now, now, fingerprint))
        self.db.commit()

    def update_device(self, ident, **fields):
        for column, value in fields.items():
            if column in ("name", "grp", "host", "platform", "last_seen", "fingerprint"):
                self.db.execute(f"UPDATE devices SET {column}=? WHERE id=?", (value, ident))
        self.db.commit()

    def delete_device(self, ident):
        self.db.execute("DELETE FROM devices WHERE id=?", (ident,))
        self.db.commit()

    # audit
    def log(self, user, action, target="", detail=""):
        self.db.execute("INSERT INTO audit VALUES(?,?,?,?,?)",
                        (int(time.time()), user, action, target, detail))
        self.db.commit()

    def audit(self, limit, since=0):
        rows = self.db.execute("SELECT * FROM audit WHERE ts>=? ORDER BY rowid DESC LIMIT ?", (since, limit))
        return [dict(r) for r in rows]


def ensure_cert(data_dir):
    """Create a self-signed certificate on first run. Clients pin its SHA-256 fingerprint."""
    crt, key = data_dir / "server.crt", data_dir / "server.key"
    if not crt.exists() or not key.exists():
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        private = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "minideck-server")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(private.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
                .sign(private, hashes.SHA256()))
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                                              serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        try:
            os.chmod(key, 0o600)
        except OSError:
            pass
    der = ssl.PEM_cert_to_DER_cert(crt.read_text())
    return crt, key, hashlib.sha256(der).hexdigest()


class Ticket:
    def __init__(self, user, device, control):
        self.user, self.device, self.control = user, device, control
        self.sides = {}
        self.done = asyncio.Event()


class Server:
    def __init__(self, store, fingerprint, log=print, data_dir=None):
        self.store = store
        self.password_file = Path(data_dir) / PASSWORD_FILE if data_dir else None
        self.fingerprint = fingerprint
        self.log = log
        self.online = {}    # device id -> control StreamWriter
        self.tickets = {}   # token -> Ticket
        self.fails = {}     # ip -> deque of failure times

    # -- helpers ------------------------------------------------------------
    @staticmethod
    async def _send(writer, obj):
        writer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()

    def _blocked(self, ip):
        fails = self.fails.get(ip)
        now = time.monotonic()
        while fails and now - fails[0] > FAIL_WINDOW:
            fails.popleft()
        return bool(fails) and len(fails) >= MAX_FAILS

    def _fail(self, ip):
        self.fails.setdefault(ip, deque()).append(time.monotonic())

    @staticmethod
    def _allowed(user, device):
        if user["role"] == "admin" or user["groups"].strip() == "*":
            return True
        groups = {g.strip().lower() for g in user["groups"].split(",") if g.strip()}
        return (device["grp"] or "").strip().lower() in groups

    # -- connection entry ---------------------------------------------------
    async def handle(self, reader, writer):
        ip = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            msg = json.loads(line)
            op = msg.get("op")
            if op == "device":
                await self._device(reader, writer, msg, ip)
            elif op == "login":
                await self._admin(reader, writer, msg, ip)
            elif op == "data":
                await self._data(reader, writer, msg)
        except (asyncio.TimeoutError, OSError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    # -- managed computers --------------------------------------------------
    async def _device(self, reader, writer, msg, ip):
        ident, key = str(msg.get("id", "")), str(msg.get("key", ""))
        name = str(msg.get("name", "?"))[:64]
        platform = str(msg.get("platform", ""))[:16]
        fingerprint = str(msg.get("fingerprint", ""))[:64]
        if self._blocked(ip) or not ID_RE.fullmatch(ident) or len(key) < 16:
            await self._send(writer, {"ok": False, "error": "bad request"})
            return
        device = self.store.device(ident)
        if device is None:
            token = str(msg.get("enroll") or "")
            if not token or not hmac.compare_digest(token, self.store.setting("enroll_token")):
                self._fail(ip)
                await self._send(writer, {"ok": False, "error": "enrollment required"})
                return
            self.store.add_device(ident, _sha(key), name, platform, fingerprint)
            self.store.log("-", "device_enrolled", ident, f"{name} ({ip})")
            self.log(f"enrolled {ident} {name}")
        elif not hmac.compare_digest(device["key_hash"], _sha(key)):
            self._fail(ip)
            await self._send(writer, {"ok": False, "error": "id taken"})
            return
        else:
            self.store.update_device(ident, host=name, platform=platform)
            known = device["fingerprint"] or ""
            if fingerprint and fingerprint != known:
                # a reinstall changes it; so does someone answering in this computer's place
                self.store.update_device(ident, fingerprint=fingerprint)
                if known:
                    self.store.log("-", "device_key_changed", ident, f"{known} -> {fingerprint}")
                    self.log(f"device {ident} changed its key")
        old = self.online.get(ident)
        if old is not None:
            old.close()
        self.online[ident] = writer
        await self._send(writer, {"ok": True})
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), 90)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("op") == "ping":
                    await self._send(writer, {"op": "pong"})
                elif msg.get("op") == "event" and msg.get("action") in REPORTED:
                    # what the computer itself saw during a brokered session
                    self.store.log(str(msg.get("user", "?"))[:32], "pc_" + msg["action"], ident,
                                   str(msg.get("detail", ""))[:200])
        finally:
            if self.online.get(ident) is writer:
                del self.online[ident]
                self.store.update_device(ident, last_seen=int(time.time()))

    # -- administrators -----------------------------------------------------
    async def _admin(self, reader, writer, msg, ip):
        name, password = str(msg.get("user", "")), str(msg.get("password", ""))
        if self._blocked(ip):
            await self._send(writer, {"ok": False, "error": "too many attempts"})
            return
        user = await asyncio.to_thread(self._verify, name, password)
        if user is None:
            self._fail(ip)
            self.store.log(name[:32], "login_failed", "", ip)
            await self._send(writer, {"ok": False, "error": "bad credentials"})
            return
        self.store.log(name, "login", "", ip)
        await self._send(writer, {"ok": True, "user": name, "role": user["role"]})
        while True:
            line = await asyncio.wait_for(reader.readline(), 120)
            if not line:
                break
            request = json.loads(line)
            user = self.store.user(name)      # role changes and deletion apply immediately
            if user is None:
                break
            try:
                reply = await self._api(user, request)
            except (KeyError, ValueError, TypeError) as exc:
                reply = {"ok": False, "error": f"bad request: {exc}"}
            await self._send(writer, reply)

    def _verify(self, name, password):
        store = Store(self.store.path)           # scrypt off the event loop, own connection
        try:
            return store.verify(name, password)
        finally:
            store.db.close()

    async def _api(self, user, req):
        op, me, admin = req.get("op"), user["name"], user["role"] == "admin"
        store = self.store
        if op == "ping":
            return {"ok": True}
        if op == "devices":
            items = []
            for d in store.devices():
                if self._allowed(user, d):
                    items.append({"id": d["id"], "name": d["name"], "host": d["host"],
                                  "group": d["grp"], "platform": d["platform"],
                                  "online": d["id"] in self.online, "last_seen": d["last_seen"],
                                  "fingerprint": d["fingerprint"] or ""})
            return {"ok": True, "devices": items}
        if op == "connect":
            device = store.device(str(req["id"]))
            if device is None or not self._allowed(user, device):
                store.log(me, "connect_denied", str(req["id"]), "нет доступа")
                return {"ok": False, "error": "no access"}
            control_channel = self.online.get(device["id"])
            if control_channel is None:
                store.log(me, "connect_denied", device["id"], f"{device['name']}: не в сети")
                return {"ok": False, "error": "offline"}
            control = user["role"] in ("admin", "operator")
            token, secret = secrets.token_hex(16), secrets.token_urlsafe(24)
            self.tickets[token] = Ticket(me, device, control)
            asyncio.get_running_loop().call_later(30, self._expire, token)
            await self._send(control_channel, {"op": "incoming", "token": token, "secret": secret,
                                               "user": me, "role": user["role"], "control": control})
            return {"ok": True, "token": token, "secret": secret, "control": control,
                    "name": device["name"], "fingerprint": device["fingerprint"] or ""}
        if op == "passwd":
            if not hmac.compare_digest(_scrypt(str(req["old"]), user["salt"]), user["hash"]):
                return {"ok": False, "error": "bad credentials"}
            if len(str(req["new"])) < 8:
                return {"ok": False, "error": "weak password"}
            store.save_user(me, str(req["new"]), user["role"], user["groups"])
            store.log(me, "password_changed", me)
            if me == "admin":
                self._forget_initial_password()
            return {"ok": True}
        if op == "audit":       # the journal is open to every account, for accountability
            return {"ok": True, "audit": store.audit(min(5000, int(req.get("limit", 500))),
                                                     int(req.get("since", 0)))}
        if op == "audit_exported":
            store.log(me, "journal_exported", "", f"{int(req.get('rows', 0))} записей")
            return {"ok": True}
        if not admin:
            return {"ok": False, "error": "forbidden"}

        # ---- administrator-only operations ----
        if op == "users":
            return {"ok": True, "users": store.users()}
        if op == "user_save":
            name, role = str(req["name"]), str(req["role"])
            password, groups = str(req.get("password") or ""), str(req.get("groups") or "*")[:200]
            existing = store.user(name)
            if not NAME_RE.fullmatch(name) or role not in ROLES:
                return {"ok": False, "error": "bad name or role"}
            if (password or not existing) and len(password) < 8:
                return {"ok": False, "error": "weak password"}
            if existing and existing["role"] == "admin" and role != "admin" and self._last_admin(name):
                return {"ok": False, "error": "last admin"}
            store.save_user(name, password, role, groups)
            if name == "admin" and password:
                self._forget_initial_password()
            store.log(me, "user_saved", name, f"role={role} groups={groups}"
                      + (" password changed" if password and existing else ""))
            return {"ok": True}
        if op == "user_delete":
            name = str(req["name"])
            if name == me:
                return {"ok": False, "error": "cannot delete yourself"}
            if self._last_admin(name):
                return {"ok": False, "error": "last admin"}
            store.delete_user(name)
            store.log(me, "user_deleted", name)
            return {"ok": True}
        if op == "device_update":
            ident = str(req["id"])
            if store.device(ident) is None:
                return {"ok": False, "error": "no such device"}
            fields = {}
            if "name" in req:
                fields["name"] = str(req["name"])[:64] or "?"
            if "group" in req:
                fields["grp"] = str(req["group"])[:32]
            store.update_device(ident, **fields)
            store.log(me, "device_updated", ident, json.dumps(fields, ensure_ascii=False))
            return {"ok": True}
        if op == "device_delete":
            ident = str(req["id"])
            store.delete_device(ident)
            channel = self.online.pop(ident, None)
            if channel is not None:
                try:
                    await self._send(channel, {"op": "revoked"})
                except OSError:
                    pass
                channel.close()
            store.log(me, "device_deleted", ident)
            return {"ok": True}
        if op == "enroll_info":
            return {"ok": True, "token": store.setting("enroll_token"), "fingerprint": self.fingerprint}
        if op == "enroll_rotate":
            store.set_setting("enroll_token", secrets.token_urlsafe(18))
            store.log(me, "enroll_code_rotated")
            return {"ok": True, "token": store.setting("enroll_token"), "fingerprint": self.fingerprint}
        return {"ok": False, "error": "unknown op"}

    def _forget_initial_password(self):
        if self.password_file is not None:
            try:
                self.password_file.unlink()
                self.log("initial admin password file removed")
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.log(f"could not remove {self.password_file}: {exc}")

    def _last_admin(self, name):
        admins = [u["name"] for u in self.store.users() if u["role"] == "admin"]
        return admins == [name]

    # -- sessions -----------------------------------------------------------
    def _expire(self, token):
        ticket = self.tickets.pop(token, None)
        if ticket is not None:
            for _reader, writer in ticket.sides.values():
                writer.close()
            ticket.done.set()

    async def _data(self, reader, writer, msg):
        token, side = str(msg.get("token", "")), msg.get("side")
        ticket = self.tickets.get(token)
        if ticket is None or side not in ("viewer", "host") or side in ticket.sides:
            return
        ticket.sides[side] = (reader, writer)
        if len(ticket.sides) < 2:
            await ticket.done.wait()   # keep this half open until its partner arrives and finishes
            return
        del self.tickets[token]
        (v_reader, v_writer), (h_reader, h_writer) = ticket.sides["viewer"], ticket.sides["host"]
        target = ticket.device["id"]
        mode = "управление" if ticket.control else "только просмотр"
        started = time.time()
        self.store.log(ticket.user, "session_start", target, f"{ticket.device['name']} ({mode})")
        self.log(f"session {ticket.user} -> {target} ({mode})")
        try:
            await self._send(v_writer, {"ok": True})
            await self._send(h_writer, {"ok": True})
            await asyncio.gather(_pipe(v_reader, h_writer), _pipe(h_reader, v_writer))
        finally:
            v_writer.close()
            h_writer.close()
            self.store.log(ticket.user, "session_end", target,
                           f"{ticket.device['name']}, {int(time.time() - started)} с")
            ticket.done.set()


def default_data_dir():
    return Path(os.environ.get("MINIDECK_DATA") or Path.home() / ".minideck-server")


def write_password_file(data, password, fingerprint=""):
    """Keep the new admin password where a service or container operator can read it,
    together with the certificate fingerprint the console asks to compare on first login."""
    path = data / PASSWORD_FILE
    path.write_text(
        "Minideck server - administrator account\n"
        f"login   : admin\npassword: {password}\n"
        + (f"certificate SHA-256: {fingerprint}\n" if fingerprint else "")
        + "\nCompare the certificate with what the console shows on the first login.\n"
        "This file is deleted automatically when the admin password is changed.\n",
        encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def main(port=DEFAULT_SERVER_PORT, bind="0.0.0.0", data_dir=None, public=None, reset_admin=False):
    from .config import new_password
    data = Path(data_dir) if data_dir else default_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "minideck.db")
    crt, key, fingerprint = ensure_cert(data)

    if not store.users() or reset_admin:
        password = new_password(14)
        store.save_user("admin", password, "admin", "*")
        store.log("-", "admin_password_reset" if reset_admin else "server_initialized")
        path = write_password_file(data, password, fingerprint)
        print("Administrator account:\n  login   : admin\n  password: " + password)
        print(f"  (also saved to {path} until the password is changed)")
    elif (data / PASSWORD_FILE).exists():
        print(f"Initial admin password is still in {data / PASSWORD_FILE} - change it in the console.")
    address = f"{public or '<server-address>'}:{port}"
    print(f"Data directory : {data}")
    print(f"Certificate    : SHA-256 {fingerprint}")
    print(f"Enrollment code: minideck://{address}/{store.setting('enroll_token')}#{fingerprint}", flush=True)

    async def serve():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(crt, key)
        server = Server(store, fingerprint, log=lambda m: print(f"[server] {m}", flush=True), data_dir=data)
        listener = await asyncio.start_server(server.handle, bind, port, ssl=context)
        print(f"Minideck server listening on {bind}:{port} (TLS)", flush=True)
        stop = asyncio.Event()
        try:  # docker stop / systemctl stop send SIGTERM: shut down cleanly
            import signal
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        except (NotImplementedError, AttributeError, ValueError):
            pass  # Windows: Ctrl+C / task end only
        await stop.wait()
        # stop accepting; open sessions are cancelled by asyncio.run on return
        # (waiting for them, as Server.wait_closed does, would hang on idle device links)
        listener.close()
        print("Minideck server stopped", flush=True)

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
