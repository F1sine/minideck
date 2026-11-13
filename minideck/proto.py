"""Wire protocol: password handshake + encrypted, length-prefixed messages.

Handshake (both directions at once):
    MAGIC(4) + nonce(16)
Key material = scrypt(password, salt = viewer_nonce + host_nonce) -> 64 bytes,
split into one key per direction. Every later message is
    uint32 length + ChaCha20-Poly1305(type byte + payload)
with a per-direction counter as nonce. A wrong password therefore shows up as
a failed decryption of the very first message.
"""
import hashlib
import json
import os
import socket
import struct
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

MAGIC = b"MDK1"
MAX_MESSAGE = 64 * 1024 * 1024


class Msg:
    AUTH = 1         # viewer -> host, json
    HELLO = 2        # host -> viewer, json
    FRAME = 3        # host -> viewer, binary tiles
    INPUT = 4        # viewer -> host, json
    SETTINGS = 5     # viewer -> host, json
    CLIPBOARD = 6    # both ways, utf-8 text
    FILE_BEGIN = 7   # viewer -> host, json {name, size}
    FILE_CHUNK = 8   # viewer -> host, bytes
    FILE_END = 9     # viewer -> host
    BYE = 10         # both ways, json {reason}
    REFRESH = 11     # viewer -> host, request a full frame


class AuthError(Exception):
    """Decryption failed: wrong password or corrupted stream."""


class ProtocolError(Exception):
    pass


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


class Connection:
    """Encrypted message stream. send() is thread-safe; recv() is single-reader."""

    def __init__(self, sock, send_key, recv_key):
        self.sock = sock
        self._enc = ChaCha20Poly1305(send_key)
        self._dec = ChaCha20Poly1305(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0
        self._send_lock = threading.Lock()
        self.bytes_in = 0
        self.closed = False

    def send(self, mtype, payload=b""):
        with self._send_lock:
            nonce = struct.pack("<4xQ", self._send_ctr)
            self._send_ctr += 1
            ct = self._enc.encrypt(nonce, bytes([mtype]) + payload, None)
            self.sock.sendall(struct.pack("<I", len(ct)) + ct)

    def send_json(self, mtype, obj):
        self.send(mtype, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def recv(self):
        (length,) = struct.unpack("<I", recv_exact(self.sock, 4))
        if length < 17 or length > MAX_MESSAGE:
            raise ProtocolError("bad message length")
        ct = recv_exact(self.sock, length)
        nonce = struct.pack("<4xQ", self._recv_ctr)
        self._recv_ctr += 1
        try:
            pt = self._dec.decrypt(nonce, ct, None)
        except InvalidTag:
            raise AuthError("decryption failed") from None
        self.bytes_in += length + 4
        return pt[0], pt[1:]

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def handshake(sock, password, is_host):
    mine = os.urandom(16)
    sock.sendall(MAGIC + mine)
    data = recv_exact(sock, 20)
    if data[:4] != MAGIC:
        raise ProtocolError("not a Minideck peer")
    theirs = data[4:]
    nonce_v, nonce_h = (theirs, mine) if is_host else (mine, theirs)
    km = hashlib.scrypt(password.encode("utf-8"), salt=nonce_v + nonce_h,
                        n=2 ** 14, r=8, p=1, dklen=64)
    key_v2h, key_h2v = km[:32], km[32:]
    if is_host:
        return Connection(sock, key_h2v, key_v2h)
    return Connection(sock, key_v2h, key_h2v)
