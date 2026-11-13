"""Update check and download.

The latest release is read from the GitHub Releases API (or a compatible URL set in
the config, for organisations that host updates themselves). Only a full release is
considered - never a draft or pre-release. The installer asset is downloaded over
HTTPS and must match the SHA-256 digest the API publishes for it; without a digest
nothing is installed automatically.

Nothing is installed without the user pressing "Обновить".
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import VERSION

DEFAULT_SOURCE = "https://api.github.com/repos/F1sine/minideck/releases/latest"
SETUP_ASSET = "MinideckSetup.exe"
USER_AGENT = f"Minideck-updater/{VERSION}"
CHECK_EVERY = 6 * 3600        # seconds between automatic checks
TIMEOUT = 15


class UpdateError(Exception):
    pass


def parse_version(text):
    """'v0.4.0' / '0.4.0' -> (0, 4, 0); None if it is not a plain dotted version."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+){0,3})", (text or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_newer(remote, local=VERSION):
    r, l = parse_version(remote), parse_version(local)
    return bool(r and l and r + (0,) * (4 - len(r)) > l + (0,) * (4 - len(l)))


def _get(url, accept="application/json"):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def check(source=None):
    """Latest release as a dict, or None when this copy is up to date.

    {version, page, notes, setup: {url, size, sha256} | None}
    """
    try:
        with _get(source or DEFAULT_SOURCE, "application/vnd.github+json") as response:
            release = json.load(response)
    except (OSError, ValueError) as exc:
        raise UpdateError(f"не удалось проверить обновления: {exc}") from None
    if release.get("draft") or release.get("prerelease"):
        return None
    version = str(release.get("tag_name") or "")
    if not is_newer(version):
        return None
    setup = None
    for asset in release.get("assets") or []:
        if asset.get("name") == SETUP_ASSET:
            digest = str(asset.get("digest") or "")
            setup = {"url": asset.get("browser_download_url"), "size": int(asset.get("size") or 0),
                     "sha256": digest[7:].lower() if digest.startswith("sha256:") else None}
    return {"version": version.lstrip("v"), "page": release.get("html_url") or "",
            "notes": release.get("body") or "", "setup": setup}


def download(setup, progress=lambda done, total: None):
    """Download the installer to a fresh temp folder and verify it. Returns the path."""
    if not setup or not setup.get("url"):
        raise UpdateError("в релизе нет установщика")
    if not setup.get("sha256"):
        raise UpdateError("для установщика не опубликована контрольная сумма")
    folder = Path(tempfile.mkdtemp(prefix="minideck-update-"))
    path = folder / SETUP_ASSET
    digest, done = hashlib.sha256(), 0
    try:
        with _get(setup["url"], "application/octet-stream") as response, open(path, "wb") as out:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done, setup.get("size") or 0)
    except OSError as exc:
        raise UpdateError(f"ошибка загрузки: {exc}") from None
    if setup.get("size") and done != setup["size"]:
        raise UpdateError("файл загрузился не полностью")
    if digest.hexdigest() != setup["sha256"]:
        path.unlink(missing_ok=True)
        raise UpdateError("контрольная сумма не совпала — файл повреждён или подменён, установка отменена")
    return path


def installed_copy():
    """True when this program is the copy the installer put in place (only that copy self-updates)."""
    if not getattr(sys, "frozen", False):
        return False
    from .installer import installed_info
    info = installed_info()
    if not info:
        return False
    try:
        return Path(info["location"]).resolve() == Path(sys.executable).resolve().parent
    except OSError:
        return False


def start_update(setup_path):
    """Run the installer in update mode; it waits for this process to exit, then relaunches Minideck."""
    subprocess.Popen([str(setup_path), "--update", f"--wait-pid={os.getpid()}"],
                     close_fds=True, creationflags=0x00000008 | 0x00000200)  # DETACHED, NEW_PROCESS_GROUP
