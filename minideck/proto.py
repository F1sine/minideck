"""Wire protocol: authenticated key exchange + encrypted, length-prefixed messages.

Handshake (both sides send their greeting at once):
    MAGIC(4) + nonce(16) + ephemeral X25519 public key(32)

The session keys come from two things at once: the X25519 shared secret and
scrypt(password). Both are fed to HKDF together with the whole greeting
transcript:

    km = HKDF-SHA256(shared || scrypt(password, viewer_nonce + host_nonce),
                     salt = transcript, length = 64)

Each side keeps one key per direction. Every later message is
    uint32 length + ChaCha20-Poly1305(type byte + payload)
with a per-direction counter as nonce, so a wrong password shows up as a failed
decryption of the very first message, exactly as before.

Why the exchange is there: with keys derived from the password alone, anyone who
recorded a session - a relay operator, a management server, someone on the same
Wi-Fi - could try passwords against the recording offline for as long as they
liked. The ephemeral keys are thrown away when the session ends, so a recording
is no longer enough: a passive observer has nothing to test a guess against, and
an old recording cannot be opened by learning the password later.

What it does not do: an attacker who sits in the middle of the handshake and
speaks to both sides can still run that offline attack on its own exchange. Host
identity keys are what would close that off; until then, treat a relay or a
management server as able to attempt it.
"""
import hashlib
import json
import os
import socket
import struct
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"MDK2"
LEGACY_MAGIC = b"MDK1"           # 0.4.0 and older: password-only keys, no exchange
GREETING = 4 + 16 + 32           # magic + nonce + ephemeral public key
MAX_MESSAGE = 64 * 1024 * 1024
AUTH_MAX_MESSAGE = 64 * 1024     # nothing big is expected before the password is accepted
SCRYPT_N = 2 ** 15               # ~32 MB per attempt, paid once per session
SCRYPT_MAXMEM = 96 * 1024 * 1024  # OpenSSL refuses the above with its default limit


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


class VersionMismatch(ProtocolError):
    """The peer speaks a different version of this protocol."""


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

    def __init__(self, sock, send_key, recv_key, max_message=MAX_MESSAGE):
        self.sock = sock
        self.max_message = max_message   # largest message accepted from the peer
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
        if length < 17 or length > self.max_message:
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
    """Exchange greetings, derive the session keys, return the Connection.

    Raises VersionMismatch for a peer running an older Minideck, ProtocolError for
    something that is not Minideck at all.
    """
    private = X25519PrivateKey.generate()
    mine = os.urandom(16) + private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sock.sendall(MAGIC + mine)
    magic = recv_exact(sock, 4)
    if magic == LEGACY_MAGIC:
        # 0.4.0 greets with 20 bytes, not 52: say so now instead of waiting for the rest
        raise VersionMismatch("peer speaks the older Minideck protocol")
    if magic != MAGIC:
        raise ProtocolError("not a Minideck peer")
    theirs = recv_exact(sock, GREETING - 4)
    viewer, host = (theirs, mine) if is_host else (mine, theirs)
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(theirs[16:]))
    except ValueError:
        raise ProtocolError("bad key exchange") from None
    # the password still decides whether the two sides can talk at all
    secret = hashlib.scrypt(password.encode("utf-8"), salt=viewer[:16] + host[:16],
                            n=SCRYPT_N, r=8, p=1, dklen=32, maxmem=SCRYPT_MAXMEM)
    km = HKDF(algorithm=hashes.SHA256(), length=64, salt=MAGIC + viewer + host,
              info=b"minideck session keys").derive(shared + secret)
    key_v2h, key_h2v = km[:32], km[32:]
    if is_host:
        return Connection(sock, key_h2v, key_v2h)
    return Connection(sock, key_v2h, key_h2v)
