"""Socket helpers: direct connections and rendezvous through a relay."""
import re
import socket

from . import DEFAULT_PORT, DEFAULT_RELAY_PORT

ID_RE = re.compile(r"\d{9}")


class LineReader:
    """Reads newline-terminated lines without consuming bytes past the newline."""

    def __init__(self, sock, limit=512):
        self.sock = sock
        self.limit = limit
        self.buf = bytearray()

    def readline(self):
        while True:
            b = self.sock.recv(1)  # may raise socket.timeout; buffer is kept
            if not b:
                raise ConnectionError("connection closed")
            if b == b"\n":
                line = self.buf.decode("utf-8", "replace").strip()
                self.buf.clear()
                return line
            self.buf += b
            if len(self.buf) > self.limit:
                raise ConnectionError("line too long")


def normalize_id(text):
    """Return a 9-digit ID if text looks like one ('123 456 789'), else None."""
    digits = re.sub(r"[\s-]", "", text or "")
    return digits if ID_RE.fullmatch(digits) else None


def format_id(ident):
    return " ".join(ident[i:i + 3] for i in range(0, len(ident), 3))


def parse_hostport(text, default_port):
    text = text.strip()
    if text.startswith("["):  # [ipv6]:port
        host, _, rest = text[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if text.count(":") == 1:
        host, port = text.split(":")
        return host, int(port)
    return text, default_port


def tune(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    return sock


def connect_tcp(text, default_port, timeout=10):
    host, port = parse_hostport(text, default_port)
    return tune(socket.create_connection((host, port), timeout=timeout))


def connect_via_relay(relay, ident, timeout=25):
    sock = connect_tcp(relay, DEFAULT_RELAY_PORT)
    try:
        sock.settimeout(timeout)
        sock.sendall(f"CONNECT {ident}\n".encode())
        reply = LineReader(sock).readline()
        if reply != "OK":
            raise ConnectionError(reply or "relay error")
        return sock
    except BaseException:
        sock.close()
        raise


def open_stream(address, relay=None):
    """Open a raw stream to a host given 'ip[:port]' or a 9-digit ID."""
    ident = normalize_id(address)
    if ident:
        if not relay:
            raise ConnectionError("no relay configured")
        return connect_via_relay(relay, ident)
    return connect_tcp(address, DEFAULT_PORT)


def local_addresses():
    """Best-effort list of this machine's LAN IPv4 addresses."""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found or ["127.0.0.1"]
