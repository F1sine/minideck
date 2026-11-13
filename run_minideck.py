"""Entry point for the packaged Minideck.exe (see build.bat)."""
import sys


def _attach_console():
    """A windowed exe has no stdout; borrow the parent console for 'host'/'relay'/--help."""
    if sys.platform != "win32" or sys.stdout is not None:
        return
    import ctypes
    import os
    if ctypes.windll.kernel32.AttachConsole(-1):
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
            return
        except OSError:
            pass
    sys.stdout = sys.stderr = open(os.devnull, "w")


if __name__ == "__main__":
    _attach_console()
    from minideck.__main__ import main
    sys.exit(main())
