"""Install / uninstall Minideck for the current Windows user (no administrator rights needed).

What an installation consists of, all of it visible and removable:
  * %LOCALAPPDATA%\\Programs\\Minideck\\Minideck.exe
  * Start menu shortcut, optional desktop shortcut
  * optional 'start with Windows' entry (HKCU Run)
  * an entry in Settings -> Apps (HKCU Uninstall), which runs 'Minideck.exe uninstall'
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import VERSION, autostart

APP_NAME = "Minideck"
EXE_NAME = "Minideck.exe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Minideck"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def default_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def payload_path():
    """Minideck.exe bundled inside the setup program (or dist/ when run from source)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "payload" / EXE_NAME
    return Path(__file__).resolve().parent.parent / "dist" / EXE_NAME


def _powershell(script):
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-Command", script], check=True, capture_output=True, creationflags=NO_WINDOW)


def special_folder(name):
    """'Desktop' or 'Programs' (Start menu), resolved the way Explorer does (OneDrive aware)."""
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          f"[Environment]::GetFolderPath('{name}')"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout.strip()
    return Path(out) if out else None


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(link, target, arguments="", description=""):
    link = Path(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    _powershell(
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(link)});"
        f"$s.TargetPath={_ps_quote(target)};$s.Arguments={_ps_quote(arguments)};"
        f"$s.WorkingDirectory={_ps_quote(Path(target).parent)};$s.IconLocation={_ps_quote(str(target) + ',0')};"
        f"$s.Description={_ps_quote(description)};$s.Save()")


def shortcut_paths():
    paths = []
    for folder in ("Programs", "Desktop"):
        base = special_folder(folder)
        if base:
            paths.append(base / f"{APP_NAME}.lnk")
    return paths


# -- registry ---------------------------------------------------------------
def installed_info():
    """{'location', 'version'} of an existing installation, or None."""
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return {"location": winreg.QueryValueEx(key, "InstallLocation")[0],
                    "version": winreg.QueryValueEx(key, "DisplayVersion")[0]}
    except OSError:
        return None


def _register(target_dir, exe):
    import winreg
    size_kb = max(1, exe.stat().st_size // 1024)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in (("DisplayName", APP_NAME), ("DisplayVersion", VERSION),
                            ("Publisher", APP_NAME), ("DisplayIcon", f"{exe},0"),
                            ("InstallLocation", str(target_dir)),
                            ("UninstallString", f'"{exe}" uninstall')):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name, value in (("EstimatedSize", size_kb), ("NoModify", 1), ("NoRepair", 1)):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def _unregister():
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


# -- running copies ---------------------------------------------------------
def close_running():
    """Close other Minideck processes (never this one or its launcher)."""
    subprocess.run(["taskkill", "/F", "/IM", EXE_NAME, "/FI", f"PID ne {os.getpid()}",
                    "/FI", f"PID ne {os.getppid()}"], capture_output=True, creationflags=NO_WINDOW)


class FileBusy(Exception):
    """Minideck.exe is in use (the program is running)."""


# -- install ----------------------------------------------------------------
def install(target_dir, desktop=True, start_with_windows=True, progress=lambda text: None):
    target_dir = Path(target_dir)
    exe = target_dir / EXE_NAME
    source = payload_path()
    if not source.exists():
        raise FileNotFoundError(f"нет файла программы: {source}")

    progress("Копирование файлов…")
    target_dir.mkdir(parents=True, exist_ok=True)
    temp = target_dir / (EXE_NAME + ".new")
    shutil.copyfile(source, temp)
    try:
        os.replace(temp, exe)
    except PermissionError:
        temp.unlink(missing_ok=True)
        raise FileBusy() from None

    progress("Создание ярлыков…")
    start_menu = special_folder("Programs")
    if start_menu:
        create_shortcut(start_menu / f"{APP_NAME}.lnk", exe, description="Удалённый рабочий стол")
    desktop_dir = special_folder("Desktop")
    if desktop_dir:
        link = desktop_dir / f"{APP_NAME}.lnk"
        if desktop:
            create_shortcut(link, exe, description="Удалённый рабочий стол")
        else:
            link.unlink(missing_ok=True)

    progress("Настройка запуска…")
    autostart.set_enabled(start_with_windows, exe=str(exe))

    progress("Регистрация в списке программ…")
    _register(target_dir, exe)
    return exe


def write_enrollment(code):
    """Leave an enrollment code for the program to use on its next start."""
    from . import config
    cfg = config.load()
    cfg["pending_enroll"] = code
    config.save(cfg)


def launch(exe):
    subprocess.Popen([str(exe)], close_fds=True, cwd=str(Path(exe).parent),
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP


# -- uninstall --------------------------------------------------------------
def uninstall(remove_data=False):
    """Remove everything install() created. The program folder itself is deleted
    a few seconds after this process exits (a running exe cannot delete itself)."""
    info = installed_info()
    close_running()
    for link in shortcut_paths():
        link.unlink(missing_ok=True)
    autostart.set_enabled(False)
    _unregister()
    if remove_data:
        shutil.rmtree(Path.home() / ".minideck", ignore_errors=True)
    folder = Path(info["location"]) if info else (Path(sys.executable).parent if getattr(sys, "frozen", False) else None)
    if folder and folder.exists() and (folder / EXE_NAME).exists():
        subprocess.Popen(f'cmd /c ping -n 4 127.0.0.1 >nul & rmdir /s /q "{folder}"',
                         creationflags=NO_WINDOW | 0x00000008, close_fds=True)
