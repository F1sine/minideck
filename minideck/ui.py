"""Shared look and small helpers for the Tk windows."""
import sys
import tkinter as tk

from . import resource_path
from .proto import AuthError, IdentityError, IdentityMismatch, ProtocolError, VersionMismatch
from .serverapi import CertificateMismatch, CertificateRejected, ServerError

MUTED = ("#5f6b7a", "#9aa4b2")
CARD = ("#ffffff", "#2b2d31")
PAGE = ("#eef1f5", "#1e1f22")
FIELD = ("#f3f5f8", "#232428")
TEXT = ("#1f2937", "#e5e7eb")
HOVER = ("#e8ecf1", "#3a3c42")
GREEN, ORANGE, RED, GREY = "#22a35a", "#e39a1c", "#d9423a", "#8a93a0"
RED_HOVER, GREEN_HOVER = "#b8342d", "#1b8549"

# keyword arguments for a quiet outlined button
GHOST = dict(fg_color="transparent", border_width=1, text_color=TEXT, hover_color=HOVER)

ROLE_NAMES = {"admin": "Администратор", "operator": "Оператор", "viewer": "Наблюдатель"}

SERVER_ERRORS = {
    "bad credentials": "Неверный логин или пароль.",
    "too many attempts": "Слишком много попыток входа — подождите минуту.",
    "offline": "Этот компьютер сейчас не в сети.",
    "no access": "У вашей учётной записи нет доступа к этому компьютеру.",
    "forbidden": "Недостаточно прав для этого действия.",
    "weak password": "Пароль должен быть не короче 8 символов.",
    "bad name or role": "Логин: 2–32 символа — латинские буквы, цифры, точка, дефис, подчёркивание.",
    "last admin": "Нельзя убрать последнего администратора.",
    "cannot delete yourself": "Нельзя удалить собственную учётную запись.",
    "no such device": "Такого компьютера уже нет на сервере.",
}


def describe_error(exc):
    text = str(exc)
    if isinstance(exc, ServerError):
        return SERVER_ERRORS.get(exc.code, f"Сервер отклонил запрос: {exc.code}")
    if isinstance(exc, CertificateRejected):
        return "Вход отменён: сертификат сервера не подтверждён, пароль не отправлялся."
    if isinstance(exc, CertificateMismatch):
        return "Сертификат сервера не совпадает с сохранённым. Если сервер переустанавливали — отметьте «Доверять новому сертификату»."
    if isinstance(exc, AuthError):
        return "Неверный пароль (или хост временно блокирует попытки входа)."
    if text == "rejected":
        return "Удалённая сторона отклонила подключение."
    if text == "session refused":
        return "Сервер не смог соединить с компьютером — попробуйте ещё раз."
    if text == "no relay configured":
        return "Для подключения по ID укажите relay-сервер в настройках."
    if "host offline" in text:
        return "Компьютер с таким ID не в сети."
    if "host did not answer" in text:
        return "Удалённый компьютер не ответил."
    if isinstance(exc, IdentityMismatch):
        return "Подключение отменено: удалённый компьютер назвался другим ключом, чем в прошлый раз."
    if isinstance(exc, IdentityError):
        return ("Удалённый компьютер не подтвердил, какой он: соединение могло быть подменено "
                "или на той стороне старая версия Minideck.")
    if isinstance(exc, VersionMismatch):
        return ("На другом компьютере установлена более старая версия Minideck. "
                "Обновите программу на обоих компьютерах: версии 0.4.0 и старше "
                "соединяться не умеют.")
    if isinstance(exc, ProtocolError):
        return "На этом адресе отвечает не Minideck."
    if isinstance(exc, (ConnectionAbortedError, ConnectionResetError)) or text == "connection closed":
        return "Соединение разорвано удалённой стороной (возможна блокировка после неверных паролей — подождите минуту)."
    if isinstance(exc, ConnectionRefusedError):
        return "По этому адресу никто не слушает: проверьте адрес и порт."
    if isinstance(exc, TimeoutError) or "timed out" in text:
        return "Нет ответа: проверьте адрес, порт и брандмауэр."
    if "getaddrinfo" in text:
        return "Адрес не найден: проверьте, что он введён правильно."
    return f"Не удалось подключиться: {text}"


def keep_on_screen(window, margin=48):
    """Move a window up when its bottom would fall off the screen (small or scaled displays).

    Uses tkinter's own wm_geometry, in real pixels: customtkinter rescales geometry strings.
    """
    import re
    try:
        window.update_idletasks()
        match = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", tk.Wm.wm_geometry(window))
        if not match:
            return
        _, height, x, y = map(int, match.groups())
        title = max(0, window.winfo_rooty() - y)           # the frame above the client area
        limit = window.winfo_screenheight() - margin        # leave room for the taskbar
        if y + title + height > limit:
            tk.Wm.wm_geometry(window, f"+{x}+{max(0, limit - title - height)}")
    except tk.TclError:
        pass                                                # the window is already gone


def set_window_icon(window):
    """CTk replaces the icon shortly after a window is created, so set it now and again later."""
    def apply():
        try:
            if sys.platform == "win32":
                window.iconbitmap(resource_path("icon.ico"))
        except tk.TclError:
            pass
    apply()
    window.after(300, apply)


def raise_window(window):
    window.after(150, lambda: (window.lift(), window.focus_force()))
