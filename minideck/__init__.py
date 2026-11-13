"""Minideck - a small self-hosted remote desktop tool."""
import sys

VERSION = "0.2.0"
DEFAULT_PORT = 7070
DEFAULT_RELAY_PORT = 7071
DEFAULT_SERVER_PORT = 7072


def resource_path(name):
    """Path to a bundled file from assets/, both from source and inside the exe."""
    import os
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", name)


def enable_dpi_awareness():
    """Make screen coordinates match physical pixels on Windows."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
