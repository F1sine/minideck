"""Rendezvous relay: lets a viewer reach a host by 9-digit ID.

Run it on a machine both sides can reach (e.g. a VPS). It only pairs sockets and
copies bytes; sessions are encrypted end to end, so it never sees the screen,
the input or the password.

Text preamble, one line each:
    host control:  REGISTER <id> <key>   -> OK | ERR ...   then PING -> PONG,
                                            and "INCOMING <token>" pushed per viewer
    viewer:        CONNECT <id>          -> OK | ERR ...   then raw bytes
    host data:     ACCEPT <token>        -> OK             then raw bytes
"""
import asyncio
import hashlib
import re
import secrets

from . import DEFAULT_RELAY_PORT

ID_RE = re.compile(r"\d{9}")


class Relay:
    def __init__(self, log=print):
        self.hosts = {}     # id -> control StreamWriter
        self.owners = {}    # id -> sha256 of the key that first claimed it
        self.pending = {}   # token -> Future resolving to (reader, writer, done)
        self.log = log

    async def handle(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            parts = line.decode("utf-8", "replace").split()
            if len(parts) == 3 and parts[0] == "REGISTER":
                await self._register(reader, writer, parts[1], parts[2])
            elif len(parts) == 2 and parts[0] == "CONNECT":
                await self._connect(reader, writer, parts[1])
            elif len(parts) == 2 and parts[0] == "ACCEPT":
                await self._accept(reader, writer, parts[1])
            else:
                writer.write(b"ERR bad request\n")
                await writer.drain()
        except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()

    async def _register(self, reader, writer, ident, key):
        digest = hashlib.sha256(key.encode()).hexdigest()
        if not ID_RE.fullmatch(ident) or self.owners.setdefault(ident, digest) != digest:
            writer.write(b"ERR id taken\n")
            await writer.drain()
            return
        old = self.hosts.get(ident)
        if old is not None:
            old.close()
        self.hosts[ident] = writer
        writer.write(b"OK\n")
        await writer.drain()
        self.log(f"host {ident} online")
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), 90)
                if not line:
                    break
                if line.strip() == b"PING":
                    writer.write(b"PONG\n")
                    await writer.drain()
        finally:
            if self.hosts.get(ident) is writer:
                del self.hosts[ident]
                self.log(f"host {ident} offline")

    async def _connect(self, reader, writer, ident):
        control = self.hosts.get(ident)
        if control is None:
            writer.write(b"ERR host offline\n")
            await writer.drain()
            return
        token = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[token] = future
        try:
            control.write(f"INCOMING {token}\n".encode())
            await control.drain()
            h_reader, h_writer, done = await asyncio.wait_for(future, 15)
        except (asyncio.TimeoutError, OSError):
            writer.write(b"ERR host did not answer\n")
            await writer.drain()
            return
        finally:
            self.pending.pop(token, None)
        try:
            writer.write(b"OK\n")
            h_writer.write(b"OK\n")
            await writer.drain()
            await h_writer.drain()
            self.log(f"session to {ident} started")
            await asyncio.gather(_pipe(reader, h_writer), _pipe(h_reader, writer))
        finally:
            h_writer.close()
            if not done.done():
                done.set_result(None)
            self.log(f"session to {ident} ended")

    async def _accept(self, reader, writer, token):
        future = self.pending.get(token)
        if future is None or future.done():
            return
        done = asyncio.get_running_loop().create_future()
        future.set_result((reader, writer, done))
        await done  # keep this handler (and its socket) alive while bytes are piped


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def serve(port=DEFAULT_RELAY_PORT, bind="0.0.0.0", log=print):
    relay = Relay(log)
    server = await asyncio.start_server(relay.handle, bind, port)
    log(f"Minideck relay listening on {bind}:{port}")
    async with server:
        await server.serve_forever()


def main(port=DEFAULT_RELAY_PORT, bind="0.0.0.0"):
    try:
        asyncio.run(serve(port, bind))
    except KeyboardInterrupt:
        pass
