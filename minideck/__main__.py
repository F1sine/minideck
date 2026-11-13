"""Command line entry point: python -m minideck [gui|host|relay] ..."""
import argparse
import sys
import time

from . import DEFAULT_PORT, DEFAULT_RELAY_PORT, DEFAULT_SERVER_PORT, VERSION, enable_dpi_awareness


def run_host(args):
    """Headless host: no window, no confirmation prompt, password only."""
    from . import config
    from .host import Host, RelayClient
    from .netutil import format_id, local_addresses

    cfg = config.load()
    password = args.password or config.new_password()
    host = Host(password, port=args.port, allow_control=not args.view_only)
    if not host.start():
        return 1
    print(f"Minideck host {VERSION}")
    print("  address : " + ", ".join(f"{ip}:{args.port}" for ip in local_addresses()))
    relay = args.relay or cfg["relay"]
    client = None
    if relay:
        client = RelayClient(host, relay, cfg["id"], cfg["id_key"],
                             on_status=lambda s: print(f"[relay] {s}", flush=True))
        client.start()
        print(f"  id      : {format_id(cfg['id'])} (relay {relay})")
    if not args.password:
        print(f"  password: {password}")
    print("Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    if client:
        client.stop()
    host.stop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="minideck", description="Minideck remote desktop")
    sub = parser.add_subparsers(dest="cmd")

    gui = sub.add_parser("gui", help="open the main window (default)")
    gui.add_argument("address", nargs="?", help="prefill the remote address")
    gui.add_argument("--minimized", action="store_true", help="start minimized to the taskbar")

    host = sub.add_parser("host", help="share this computer without a window")
    host.add_argument("--password", help="access password (random if omitted)")
    host.add_argument("--port", type=int, default=DEFAULT_PORT)
    host.add_argument("--relay", help="relay server host[:port]")
    host.add_argument("--view-only", action="store_true", help="do not accept input or files")

    relay = sub.add_parser("relay", help="run a rendezvous relay server")
    relay.add_argument("--port", type=int, default=DEFAULT_RELAY_PORT)
    relay.add_argument("--bind", default="0.0.0.0")

    sub.add_parser("uninstall", help="remove the installed program (used by Settings -> Apps)")

    server = sub.add_parser("server", help="run the management server (accounts, roles, managed PCs)")
    server.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT)
    server.add_argument("--bind", default="0.0.0.0")
    server.add_argument("--data", help="data directory (default: ~/.minideck-server)")
    server.add_argument("--public", help="public host name, used in the printed enrollment code")
    server.add_argument("--reset-admin", action="store_true", help="set a new random password for 'admin'")

    args = parser.parse_args(argv)
    if args.cmd == "server":
        from .server import main as server_main
        server_main(args.port, args.bind, args.data, args.public, args.reset_admin)
        return 0
    if args.cmd == "relay":
        from .relay import main as relay_main
        relay_main(args.port, args.bind)
        return 0
    enable_dpi_awareness()
    if args.cmd == "uninstall":
        from .setupui import run_uninstall
        run_uninstall()
        return 0
    if args.cmd == "host":
        return run_host(args)
    from .app import main as app_main
    app_main(getattr(args, "address", None), getattr(args, "minimized", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
