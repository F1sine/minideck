"""Persistent settings in ~/.minideck/config.json."""
import json
import secrets
from pathlib import Path

from . import DEFAULT_PORT

PATH = Path.home() / ".minideck" / "config.json"
IDENTITY_PATH = PATH.parent / "host_key"   # this computer's long-term key
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike characters

DEFAULTS = {
    "port": DEFAULT_PORT,
    "relay": "",
    "id": "",
    "id_key": "",
    "password": "",          # only stored when "permanent password" is on
    "permanent_password": False,
    "allow_control": True,
    "confirm": True,
    "clipboard": True,
    "theme": "system",
    "server": {},            # management server this PC is enrolled in: {address, pin}
    "pending_enroll": "",    # enrollment code left by the installer, used once on next start
    "admin_server": {},      # last server the administrator console signed in to (no password)
    "update_check": True,    # look for a newer release on start and every few hours
    "update_source": "",     # releases API URL; empty = github.com/F1sine/minideck
    "last_version": "",      # version that ran last time, to record updates in the journal
    "recent": [],            # addresses of past connections; never passwords
    "known_hosts": {},       # how a remote computer identified itself last time: slot -> fingerprint
}


def host_identity():
    """This computer's long-term key, so a viewer can tell it apart from an impostor."""
    from .proto import load_identity
    return load_identity(IDENTITY_PATH)


def identity_slot(address):
    """Under which name the fingerprint of this remote computer is remembered."""
    address = (address or "").strip()
    return f"id:{address}" if address.isdigit() else f"addr:{address.lower()}"


def known_identity(cfg, slot):
    return (cfg.get("known_hosts") or {}).get(slot)


def remember_identity(cfg, slot, value):
    hosts = cfg.setdefault("known_hosts", {})
    if slot and value and hosts.get(slot) != value:
        hosts[slot] = value
        save(cfg)
        return True
    return False


def new_password(length=10):
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_id():
    return str(secrets.randbelow(900_000_000) + 100_000_000)


def load():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    changed = False
    if not cfg["id"]:
        cfg["id"] = new_id()
        changed = True
    if not cfg["id_key"]:
        cfg["id_key"] = secrets.token_hex(16)
        changed = True
    if changed:
        save(cfg)
    return cfg


def save(cfg):
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
