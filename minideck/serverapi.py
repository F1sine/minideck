"""Client side of the management server: TLS with a pinned certificate,
the administrator API client and the agent that keeps a managed computer registered."""
import hashlib
import hmac
import json
import re
import socket
import ssl
import sys
import threading
import time
from collections import deque

from . import DEFAULT_SERVER_PORT
from .netutil import LineReader, parse_hostport, tune

CODE_RE = re.compile(r"minideck://([^/\s]+)/([A-Za-z0-9_-]+)#([0-9a-fA-F]{64})")


class ServerError(Exception):
    """The server answered with an error code (see .code)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class CertificateMismatch(Exception):
    """The server's certificate is not the one that was pinned."""


def parse_enroll_code(text):
    """'minideck://host:port/token#fingerprint' -> (address, token, fingerprint) or None."""
    match = CODE_RE.fullmatch((text or "").strip())
    return (match.group(1), match.group(2), match.group(3).lower()) if match else None


def make_enroll_code(address, token, fingerprint):
    return f"minideck://{address}/{token}#{fingerprint}"


def tls_connect(server, pin=None, timeout=10):
    """Connect and return (socket, fingerprint). The certificate is self-signed, so it is
    checked against the pinned SHA-256 fingerprint instead of a certificate authority."""
    host, port = parse_hostport(server, DEFAULT_SERVER_PORT)
    raw = tune(socket.create_connection((host, port), timeout=timeout))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        sock = context.wrap_socket(raw)
    except BaseException:
        raw.close()
        raise
    fingerprint = hashlib.sha256(sock.getpeercert(True)).hexdigest()
    if pin and not hmac.compare_digest(fingerprint, pin.lower()):
        sock.close()
        raise CertificateMismatch(fingerprint)
    return sock, fingerprint


class Channel:
    """JSON lines over a socket (control channels only: reads are buffered)."""

    def __init__(self, sock):
        self.sock = sock
        self._file = sock.makefile("rb")
        self._lock = threading.Lock()

    def send(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._lock:
            self.sock.sendall(data)

    def recv(self):
        line = self._file.readline()
        if not line:
            raise ConnectionError("connection closed")
        return json.loads(line)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def open_data(server, pin, token, side, timeout=30):
    """Open one half of a brokered session; returns once the other half has joined."""
    sock, _ = tls_connect(server, pin)
    try:
        sock.settimeout(timeout)
        sock.sendall(json.dumps({"op": "data", "token": token, "side": side}).encode() + b"\n")
        reply = json.loads(LineReader(sock, 4096).readline())
        if not reply.get("ok"):
            raise ConnectionError("session refused")
        return sock
    except BaseException:
        sock.close()
        raise


class AdminClient:
    """An administrator's logged-in API connection. call() is thread-safe."""

    def __init__(self, server, pin=None):
        self.server = server
        self.pin = pin
        self.user = self.role = ""
        self.fingerprint = None
        self._channel = None
        self._lock = threading.Lock()
        self._closed = False

    def login(self, user, password):
        sock, self.fingerprint = tls_connect(self.server, self.pin)
        sock.settimeout(30)
        self._channel = Channel(sock)
        self._channel.send({"op": "login", "user": user, "password": password})
        reply = self._channel.recv()
        if not reply.get("ok"):
            self._channel.close()
            raise ServerError(reply.get("error", "error"))
        self.user, self.role = reply["user"], reply["role"]
        threading.Thread(target=self._keepalive, daemon=True).start()

    def call(self, op, **params):
        with self._lock:
            self._channel.send(dict(params, op=op))
            reply = self._channel.recv()
        if not reply.get("ok"):
            raise ServerError(reply.get("error", "error"))
        return reply

    def open_session(self, token):
        return open_data(self.server, self.fingerprint, token, "viewer")

    def _keepalive(self):
        while not self._closed:
            time.sleep(30)
            try:
                self.call("ping")
            except (OSError, ValueError, ServerError):
                break

    def close(self):
        self._closed = True
        if self._channel is not None:
            self._channel.close()


class ServerAgent:
    """Runs on a managed computer: keeps it registered and hands brokered sessions to the Host.

    status: 'connecting' | 'online' | 'offline' | 'revoked' | 'error: <code>'
    """

    FATAL = ("enrollment required", "id taken", "bad request")

    def __init__(self, host, server, pin, ident, key, enroll_token=None, on_status=None):
        self.host = host
        self.server = server
        self.pin = pin
        self.ident = ident
        self.key = key
        self.enroll_token = enroll_token
        self.on_status = on_status
        self.status = "connecting"
        self._stopped = False
        self._channel = None
        self._unsent = deque(maxlen=300)   # journal events waiting for the server link

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stopped = True
        if self._channel is not None:
            self._channel.close()

    def report(self, action, account, detail=""):
        """Send a journal event about a server-brokered session to the server."""
        self._unsent.append({"op": "event", "action": action, "user": account,
                             "detail": detail, "ts": int(time.time())})
        self._flush()

    def _flush(self):
        channel = self._channel
        if self.status != "online" or channel is None:
            return
        try:
            while self._unsent:
                channel.send(self._unsent[0])
                self._unsent.popleft()
        except (OSError, IndexError):
            pass

    def _set(self, status):
        if status != self.status:
            self.status = status
            if self.on_status:
                self.on_status(status)

    def _run(self):
        while not self._stopped:
            try:
                sock, _ = tls_connect(self.server, self.pin)
                sock.settimeout(75)   # the server answers a ping every 25 s; silence means it is gone
                channel = self._channel = Channel(sock)
                channel.send({"op": "device", "id": self.ident, "key": self.key,
                              "name": socket.gethostname(), "platform": sys.platform,
                              "enroll": self.enroll_token})
                reply = channel.recv()
                if not reply.get("ok"):
                    code = reply.get("error", "error")
                    if code in self.FATAL:
                        # a previously enrolled computer that the server no longer knows was removed
                        revoked = code == "enrollment required" and not self.enroll_token
                        self._set("revoked" if revoked else f"error: {code}")
                        return
                    raise ConnectionError(code)
                self.enroll_token = None
                self._set("online")
                self._flush()
                threading.Thread(target=self._ping, args=(channel,), daemon=True).start()
                while not self._stopped:
                    msg = channel.recv()
                    if msg.get("op") == "incoming":
                        threading.Thread(target=self._accept, args=(msg,), daemon=True).start()
                    elif msg.get("op") == "revoked":
                        self._set("revoked")
                        return
            except CertificateMismatch:
                self._set("error: certificate")
            except (OSError, ValueError):
                if not self._stopped:
                    self._set("offline")
            finally:
                if self._channel is not None:
                    self._channel.close()
            for _ in range(50):
                if self._stopped:
                    return
                time.sleep(0.1)

    def _ping(self, channel):
        while not self._stopped and channel is self._channel:
            time.sleep(25)
            try:
                channel.send({"op": "ping"})
            except OSError:
                break

    def _accept(self, msg):
        try:
            sock = open_data(self.server, self.pin, msg["token"], "host")
        except (OSError, ValueError, KeyError, CertificateMismatch):
            return
        role = {"admin": "администратор", "operator": "оператор", "viewer": "наблюдатель"}.get(
            msg.get("role"), "")
        label = f"{msg.get('user', '?')} ({role})" if role else str(msg.get("user", "?"))
        self.host.handle_socket(sock, "сервер", password=str(msg["secret"]),
                                control=bool(msg.get("control")), label=label,
                                account=str(msg.get("user", "?")))
