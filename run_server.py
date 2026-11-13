"""Entry point for MinideckServer.exe: a console program that only runs the management server.

    MinideckServer.exe [--port 7072] [--public host] [--data DIR] [--reset-admin]

Imports only the server modules, so the exe carries no GUI libraries.
"""
import argparse
import sys

from minideck import DEFAULT_SERVER_PORT, VERSION
from minideck.server import main as server_main


def main(argv=None):
    parser = argparse.ArgumentParser(prog="MinideckServer",
                                     description=f"Minideck management server {VERSION}")
    parser.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--data", help="data directory (default: $MINIDECK_DATA or ~/.minideck-server)")
    parser.add_argument("--public", help="public host name, used in the printed enrollment code")
    parser.add_argument("--reset-admin", action="store_true", help="set a new random password for 'admin'")
    args = parser.parse_args(argv)
    server_main(args.port, args.bind, args.data, args.public, args.reset_admin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
