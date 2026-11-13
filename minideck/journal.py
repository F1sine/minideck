"""Activity journal.

Two places keep records:
  * every computer keeps a local journal file of what happened on it and from it;
  * a management server keeps the organisation-wide journal (see server.py).

Recorded: who connected and when, in which mode, how the session ended, files and
clipboard text that crossed the session (names and sizes only), refused and failed
attempts, changes of access settings. Not recorded: screen contents, keystrokes,
clipboard or file contents.
"""
import csv
import json
import threading
import time
from pathlib import Path

PATH = Path.home() / ".minideck" / "journal.jsonl"
MAX_BYTES = 2_000_000   # when the file outgrows this, the older half is dropped

# action code -> text shown in the journal (local and server codes share one table)
ACTIONS = {
    # this computer was accessed
    "session_start": "начало сеанса",
    "session_end": "конец сеанса",
    "rejected": "подключение отклонено пользователем",
    "wrong_password": "неверный пароль",
    "kicked": "сеанс разорван с этого компьютера",
    "file_received": "получен файл",
    "file_refused": "передача файла отклонена",
    "clipboard_received": "получен текст буфера обмена",
    "clipboard_sent": "отправлен текст буфера обмена",
    "setting_changed": "изменена настройка доступа",
    "server_joined": "компьютер подключён к серверу организации",
    "server_left": "компьютер отключён от сервера организации",
    "update_started": "запущено обновление программы",
    "program_updated": "программа обновлена",
    # this computer accessed another one
    "out_connect": "подключение к удалённому компьютеру",
    "out_disconnect": "отключение от удалённого компьютера",
    "out_file": "отправлен файл на удалённый компьютер",
    "out_failed": "не удалось подключиться",
    "out_identity": "запомнен ключ удалённого компьютера",
    "out_identity_changed": "принят новый ключ удалённого компьютера",
    "identities_forgotten": "запомненные ключи удалённых компьютеров забыты",
    # server side
    "login": "вход на сервер",
    "login_failed": "неудачный вход на сервер",
    "connect_denied": "отказано в подключении",
    "device_enrolled": "компьютер зарегистрирован",
    "device_updated": "компьютер изменён",
    "device_key_changed": "ключ компьютера изменился",
    "device_deleted": "компьютер удалён",
    "user_saved": "пользователь сохранён",
    "user_deleted": "пользователь удалён",
    "password_changed": "смена пароля",
    "enroll_code_rotated": "код регистрации сменён",
    "server_initialized": "сервер создан",
    "admin_password_reset": "сброс пароля admin",
    "journal_exported": "журнал выгружен в файл",
}
# events a managed computer reports to its server (stored there with a "pc_" prefix)
REPORTED = ("session_accepted", "rejected", "kicked", "file_received",
            "clipboard_received", "clipboard_sent")
ACTIONS.update({
    "pc_session_accepted": "ПК: сеанс принят",
    "pc_rejected": "ПК: пользователь отклонил подключение",
    "pc_kicked": "ПК: пользователь разорвал сеанс",
    "pc_file_received": "ПК: получен файл",
    "pc_clipboard_received": "ПК: получен текст буфера обмена",
    "pc_clipboard_sent": "ПК: отправлен текст буфера обмена",
})


def action_name(code):
    return ACTIONS.get(code, code)


def human_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


def human_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60} с"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


class Journal:
    """Append-only local journal (JSON lines). Safe to call from any thread."""

    def __init__(self, path=PATH):
        self.path = Path(path)
        self._lock = threading.Lock()

    def add(self, action, who="", detail=""):
        record = {"ts": int(time.time()), "action": action, "who": who, "detail": detail}
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    lines = self.path.read_text(encoding="utf-8").splitlines()
                    self.path.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return record

    def read(self, limit=2000):
        """Newest first."""
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        records = []
        for line in reversed(lines[-limit:]):
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
        return records


def export_csv(path, rows, columns):
    """rows: iterable of sequences; columns: header. Semicolons + BOM so Excel opens it directly."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(columns)
        writer.writerows(rows)
