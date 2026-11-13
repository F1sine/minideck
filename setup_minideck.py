"""Entry point for MinideckSetup.exe (see build.bat)."""
import sys

if __name__ == "__main__":
    from minideck import enable_dpi_awareness
    enable_dpi_awareness()
    from minideck.setupui import run_setup
    run_setup()
    sys.exit(0)
