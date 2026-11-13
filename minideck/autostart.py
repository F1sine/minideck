"""'Start with Windows' for the current user (a normal, visible Run entry)."""
import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "Minideck"


def supported():
    return sys.platform == "win32"


def _command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" gui --minimized'
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_minideck.py")
    python = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{python if os.path.exists(python) else sys.executable}" "{script}" gui --minimized'


def is_enabled():
    if not supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled, exe=None):
    """exe: path of an installed Minideck.exe (used by the installer); default is this program."""
    if not supported():
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            command = f'"{exe}" gui --minimized' if exe else _command()
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, NAME)
            except OSError:
                pass
