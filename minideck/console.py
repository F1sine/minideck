"""Administrator console: sign in to a management server, browse computers, manage access."""
import threading
import time

import customtkinter as ctk

from . import DEFAULT_SERVER_PORT, config
from .journalui import JournalView
from .netutil import format_id
from .serverapi import AdminClient, CertificateMismatch, make_enroll_code
from .ui import (CARD, FIELD, GHOST, GREEN, GREY, HOVER, MUTED, PAGE, RED, RED_HOVER, ROLE_NAMES,
                 describe_error, raise_window, set_window_icon)
from .viewer import ViewerSession

AUDIT_NAMES = {
    "login": "вход", "login_failed": "неудачный вход", "session_start": "начало сеанса",
    "session_end": "конец сеанса", "device_enrolled": "компьютер зарегистрирован",
    "device_updated": "компьютер изменён", "device_deleted": "компьютер удалён",
    "user_saved": "пользователь сохранён", "user_deleted": "пользователь удалён",
    "password_changed": "смена пароля", "enroll_code_rotated": "код регистрации сменён",
    "server_initialized": "сервер создан", "admin_password_reset": "сброс пароля admin",
}


def ago(ts):
    if not ts:
        return "ещё не был в сети"
    minutes = int((time.time() - ts) / 60)
    if minutes < 1:
        return "был в сети только что"
    if minutes < 60:
        return f"был в сети {minutes} мин назад"
    if minutes < 48 * 60:
        return f"был в сети {minutes // 60} ч назад"
    return "был в сети " + time.strftime("%d.%m.%Y", time.localtime(ts))


class LoginDialog(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app.root, fg_color=PAGE)
        self.app = app
        saved = app.cfg.get("admin_server") or {}
        self.title("Вход на сервер")
        self.resizable(False, False)
        self.transient(app.root)
        set_window_icon(self)
        ctk.CTkLabel(self, text="Сервер управления", font=app.f_title).pack(anchor="w", padx=24, pady=(20, 2))
        ctk.CTkLabel(self, text="Вход для администраторов и операторов.", text_color=MUTED,
                     font=app.f_small).pack(anchor="w", padx=24, pady=(0, 10))
        self.server = self._field("Адрес сервера", f"server.example.com:{DEFAULT_SERVER_PORT}", saved.get("address", ""))
        self.user = self._field("Логин", "admin", saved.get("user", ""))
        self.password = self._field("Пароль", "", "", show="•")
        self.trust = ctk.CTkCheckBox(self, text="Доверять новому сертификату сервера")
        self.error = ctk.CTkLabel(self, text="", text_color=RED, font=app.f_small, wraplength=320, justify="left")
        self.button = ctk.CTkButton(self, text="Войти", height=38, command=self._login)
        self.button.pack(fill="x", padx=24, pady=(14, 20))
        for entry in (self.server, self.user, self.password):
            entry.bind("<Return>", lambda e: self._login())
        (self.password if saved.get("address") and saved.get("user") else self.server).focus_set()
        raise_window(self)

    def _field(self, label, placeholder, value, show=""):
        ctk.CTkLabel(self, text=label, text_color=MUTED, font=self.app.f_small).pack(anchor="w", padx=24)
        entry = ctk.CTkEntry(self, width=320, height=36, placeholder_text=placeholder, show=show,
                             fg_color=FIELD, border_width=1)
        entry.pack(padx=24, pady=(0, 8))
        if value:
            entry.insert(0, value)
        return entry

    def _fail(self, exc):
        self.button.configure(state="normal", text="Войти")
        self.error.configure(text=describe_error(exc))
        self.error.pack(anchor="w", padx=24, before=self.button)
        if isinstance(exc, CertificateMismatch):
            self.trust.pack(anchor="w", padx=24, pady=(6, 0), before=self.button)

    def _login(self):
        server, user, password = self.server.get().strip(), self.user.get().strip(), self.password.get()
        if not server or not user or not password:
            self.error.configure(text="Заполните адрес, логин и пароль.")
            self.error.pack(anchor="w", padx=24, before=self.button)
            return
        saved = self.app.cfg.get("admin_server") or {}
        pin = saved.get("pin") if saved.get("address") == server and not self.trust.get() else None
        client = AdminClient(server, pin)
        self.button.configure(state="disabled", text="Вход…")

        def work():
            try:
                client.login(user, password)
            except Exception as exc:
                self.app.call_ui(self._fail, exc)
            else:
                self.app.call_ui(self._done, client)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, client):
        # first successful login pins the certificate for later logins
        self.app.cfg["admin_server"] = {"address": client.server, "user": client.user,
                                        "pin": client.fingerprint}
        config.save(self.app.cfg)
        self.destroy()
        self.app.console = Console(self.app, client)


class Console(ctk.CTkToplevel):
    def __init__(self, app, client):
        super().__init__(app.root, fg_color=PAGE)
        self.app, self.client = app, client
        self.admin = client.role == "admin"
        self.devices, self._shown = [], None
        self._busy = False
        self.title(f"Minideck — консоль ({client.server})")
        self.geometry("880x600")
        self.minsize(760, 480)
        set_window_icon(self)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkLabel(header, text="Консоль администратора" if self.admin else "Консоль сервера", font=app.f_title).pack(side="left")
        ctk.CTkLabel(header, text=f"{client.user} · {ROLE_NAMES.get(client.role, client.role)} · {client.server}",
                     text_color=MUTED).pack(side="left", padx=12, pady=(4, 0))
        ctk.CTkButton(header, text="Выйти", width=80, height=30, command=self.close, **GHOST).pack(side="right")
        ctk.CTkButton(header, text="Сменить пароль", width=130, height=30, command=self._change_password,
                      **GHOST).pack(side="right", padx=8)

        self.status = ctk.CTkLabel(self, text="", text_color=MUTED, anchor="w")
        self.status.pack(side="bottom", fill="x", padx=20, pady=(0, 10))
        self.tabs = ctk.CTkTabview(self, fg_color=CARD, corner_radius=14, command=self._tab_changed)
        self.tabs.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        self._build_devices(self.tabs.add("Компьютеры"))
        if self.admin:
            self._build_users(self.tabs.add("Пользователи"))
        self._build_audit(self.tabs.add("Журнал"))     # open to every role
        if self.admin:
            self._build_enroll(self.tabs.add("Добавить компьютер"))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh_devices()
        raise_window(self)

    # -- plumbing -----------------------------------------------------------
    def bg(self, fn, done=None, quiet=False):
        """Run a server call on a thread; deliver the result (or the error) on the UI thread."""
        def work():
            try:
                result = fn()
            except Exception as exc:
                if not quiet or not isinstance(exc, OSError):
                    self.app.call_ui(self.say, describe_error(exc), True)
                else:
                    self.app.call_ui(self.say, "Нет связи с сервером.", True)
            else:
                if done:
                    self.app.call_ui(done, result)
        threading.Thread(target=work, daemon=True).start()

    def say(self, text, error=False):
        if self.winfo_exists():
            self.status.configure(text=text, text_color=RED if error else MUTED)

    def close(self):
        self.client.close()
        self.app.console = None
        self.destroy()

    def _tab_changed(self):
        tab = self.tabs.get()
        if tab == "Пользователи":
            self._load_users()
        elif tab == "Журнал":
            self.journal.reload()
        elif tab == "Добавить компьютер":
            self._load_enroll()

    def _dialog(self, title):
        dialog = ctk.CTkToplevel(self, fg_color=PAGE)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.transient(self)
        set_window_icon(dialog)
        ctk.CTkLabel(dialog, text=title, font=self.app.f_title).pack(anchor="w", padx=24, pady=(20, 10))
        raise_window(dialog)
        return dialog

    def _entry(self, parent, label, value="", placeholder="", show="", hint=""):
        ctk.CTkLabel(parent, text=label, font=self.app.f_head).pack(anchor="w", padx=24)
        if hint:
            ctk.CTkLabel(parent, text=hint, text_color=MUTED, font=self.app.f_small, wraplength=340,
                         justify="left").pack(anchor="w", padx=24)
        entry = ctk.CTkEntry(parent, width=340, height=36, placeholder_text=placeholder, show=show,
                             fg_color=FIELD, border_width=1)
        entry.pack(padx=24, pady=(4, 12))
        if value:
            entry.insert(0, value)
        return entry

    def _dialog_buttons(self, dialog, save, danger=None):
        error = ctk.CTkLabel(dialog, text="", text_color=RED, font=self.app.f_small, wraplength=340, justify="left")
        error.pack(anchor="w", padx=24)
        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(6, 20))
        ctk.CTkButton(row, text="Сохранить", width=120, height=36, command=save).pack(side="right")
        ctk.CTkButton(row, text="Отмена", width=100, height=36, command=dialog.destroy, **GHOST).pack(side="right", padx=8)
        if danger:
            text, action = danger
            button = ctk.CTkButton(row, text=text, width=110, height=36, fg_color="transparent", border_width=1,
                                   border_color=RED, text_color=RED, hover_color=HOVER)

            def click():   # two clicks: the first one only arms the button
                if button.cget("text") == text:
                    button.configure(text="Точно удалить?", fg_color=RED, text_color="#ffffff", hover_color=RED_HOVER)
                else:
                    action()
            button.configure(command=click)
            button.pack(side="left")
        return error

    def _submit(self, dialog, error, call, after):
        """Send a change; close the dialog on success, show the reason inside it on failure."""
        def work():
            try:
                call()
            except Exception as exc:
                self.app.call_ui(lambda: error.winfo_exists() and error.configure(text=describe_error(exc)))
            else:
                self.app.call_ui(lambda: (dialog.destroy(), after()))
        threading.Thread(target=work, daemon=True).start()

    # -- computers ----------------------------------------------------------
    def _build_devices(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=6, pady=(4, 6))
        self.search = ctk.CTkEntry(top, placeholder_text="Поиск по имени, ID или группе", height=34,
                                   fg_color=FIELD, border_width=1)
        self.search.pack(side="left", fill="x", expand=True)
        self.search.bind("<KeyRelease>", lambda e: self._show_devices())
        self.count = ctk.CTkLabel(top, text="", text_color=MUTED)
        self.count.pack(side="left", padx=12)
        self.device_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.device_list.pack(fill="both", expand=True)

    def _refresh_devices(self):
        if not self.winfo_exists():
            return
        self.bg(lambda: self.client.call("devices")["devices"], self._got_devices, quiet=True)
        self.after(5000, self._refresh_devices)

    def _got_devices(self, devices):
        if not self.winfo_exists():
            return
        if self.status.cget("text") == "Нет связи с сервером.":
            self.say("")
        self.devices = sorted(devices, key=lambda d: (not d["online"], d["name"].lower()))
        self._show_devices()

    def _show_devices(self):
        query = self.search.get().strip().lower()
        shown = [d for d in self.devices
                 if query in f"{d['name']} {d['id']} {format_id(d['id'])} {d['group']} {d['host']}".lower()]
        online = sum(d["online"] for d in self.devices)
        self.count.configure(text=f"в сети {online} из {len(self.devices)}")
        signature = [(d["id"], d["name"], d["group"], d["online"], d["host"], ago(d["last_seen"])) for d in shown]
        if signature == self._shown:
            return          # nothing changed: don't rebuild (keeps scrolling and hover steady)
        self._shown = signature
        for child in self.device_list.winfo_children():
            child.destroy()
        if not shown:
            text = "Ничего не найдено" if self.devices else (
                "Компьютеров пока нет. Откройте вкладку «Добавить компьютер»." if self.admin
                else "Вам пока не назначено ни одного компьютера.")
            ctk.CTkLabel(self.device_list, text=text, text_color=MUTED).pack(pady=30)
            return
        for device in shown:
            row = ctk.CTkFrame(self.device_list, fg_color=FIELD, corner_radius=10)
            row.pack(fill="x", pady=3, padx=2)
            ctk.CTkLabel(row, text="●", text_color=GREEN if device["online"] else GREY).pack(side="left", padx=(12, 8))
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True, pady=6)
            ctk.CTkLabel(text, text=device["name"], font=ctk.CTkFont(size=14, weight="bold"),
                         anchor="w", height=20).pack(fill="x")
            details = [f"ID {format_id(device['id'])}"]
            if device["group"]:
                details.append(f"группа «{device['group']}»")
            if device["host"] and device["host"] != device["name"]:
                details.append(device["host"])
            details.append("в сети" if device["online"] else ago(device["last_seen"]))
            ctk.CTkLabel(text, text="  ·  ".join(details), text_color=MUTED, font=self.app.f_small,
                         anchor="w", height=16).pack(fill="x")
            ctk.CTkButton(row, text="Подключиться", width=120, height=32,
                          state="normal" if device["online"] else "disabled",
                          command=lambda d=device: self._connect(d)).pack(side="right", padx=(6, 10))
            if self.admin:
                ctk.CTkButton(row, text="Изменить", width=90, height=32,
                              command=lambda d=device: self._edit_device(d), **GHOST).pack(side="right")

    def _connect(self, device):
        if self._busy:
            return
        self._busy = True
        self.say(f"Подключение к «{device['name']}»… если там включено подтверждение, ждём ответа пользователя.")

        def work():
            try:
                ticket = self.client.call("connect", id=device["id"])
                session = ViewerSession(
                    device["name"], ticket["secret"],
                    on_clipboard=lambda text: self.app.call_ui(self.app._set_clipboard, text),
                    stream_factory=lambda: self.client.open_session(ticket["token"]))
                session.connect()
                return session
            finally:
                self._busy = False

        def opened(session):
            self.say("")
            self.app.open_viewer(session, remember=False)

        self.bg(work, opened)

    def _edit_device(self, device):
        dialog = self._dialog("Компьютер")
        ctk.CTkLabel(dialog, text=f"ID {format_id(device['id'])}  ·  {device['host']}", text_color=MUTED).pack(
            anchor="w", padx=24, pady=(0, 10))
        name = self._entry(dialog, "Название", device["name"])
        group = self._entry(dialog, "Группа", device["group"], "например: office",
                            hint="По группам операторам и наблюдателям выдаётся доступ.")
        reload = lambda: (setattr(self, "_shown", None), self.bg(lambda: self.client.call("devices")["devices"], self._got_devices))
        error = self._dialog_buttons(
            dialog,
            lambda: self._submit(dialog, error, lambda: self.client.call(
                "device_update", id=device["id"], name=name.get().strip(), group=group.get().strip()), reload),
            danger=("Удалить", lambda: self._submit(dialog, error, lambda: self.client.call(
                "device_delete", id=device["id"]), reload)))

    # -- users --------------------------------------------------------------
    def _build_users(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=6, pady=(4, 6))
        ctk.CTkLabel(top, text="Администратор — всё, включая пользователей и журнал.  Оператор — просмотр и управление.  "
                               "Наблюдатель — только просмотр.", text_color=MUTED, font=self.app.f_small,
                     wraplength=560, justify="left").pack(side="left")
        ctk.CTkButton(top, text="+ Добавить", width=110, height=32, command=lambda: self._edit_user(None)).pack(side="right")
        self.user_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.user_list.pack(fill="both", expand=True)

    def _load_users(self):
        self.bg(lambda: self.client.call("users")["users"], self._show_users)

    def _show_users(self, users):
        for child in self.user_list.winfo_children():
            child.destroy()
        for user in users:
            row = ctk.CTkFrame(self.user_list, fg_color=FIELD, corner_radius=10)
            row.pack(fill="x", pady=3, padx=2)
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True, padx=14, pady=6)
            me = "  (это вы)" if user["name"] == self.client.user else ""
            ctk.CTkLabel(text, text=user["name"] + me, font=ctk.CTkFont(size=14, weight="bold"),
                         anchor="w", height=20).pack(fill="x")
            access = "все компьютеры" if user["role"] == "admin" or user["groups"].strip() == "*" \
                else f"группы: {user['groups']}"
            ctk.CTkLabel(text, text=f"{ROLE_NAMES.get(user['role'], user['role'])}  ·  {access}", text_color=MUTED,
                         font=self.app.f_small, anchor="w", height=16).pack(fill="x")
            ctk.CTkButton(row, text="Изменить", width=90, height=32,
                          command=lambda u=user: self._edit_user(u), **GHOST).pack(side="right", padx=10)

    def _edit_user(self, user):
        dialog = self._dialog("Пользователь" if user else "Новый пользователь")
        name = self._entry(dialog, "Логин", user["name"] if user else "", "ivanov")
        if user:
            name.configure(state="disabled")
        password = self._entry(dialog, "Пароль", "", "оставьте пустым, чтобы не менять" if user else "не короче 8 символов",
                               show="•")
        ctk.CTkLabel(dialog, text="Роль", font=self.app.f_head).pack(anchor="w", padx=24)
        role = ctk.CTkSegmentedButton(dialog, values=list(ROLE_NAMES.values()))
        role.set(ROLE_NAMES[user["role"] if user else "operator"])
        role.pack(anchor="w", padx=24, pady=(4, 12))
        groups = self._entry(dialog, "Доступ к группам", user["groups"] if user else "*", "office, warehouse",
                             hint="Через запятую. Звёздочка * — все компьютеры. Для администратора не учитывается.")
        codes = {v: k for k, v in ROLE_NAMES.items()}
        login = user["name"] if user else None

        def save():
            self._submit(dialog, error, lambda: self.client.call(
                "user_save", name=login or name.get().strip(), password=password.get(),
                role=codes[role.get()], groups=groups.get().strip() or "*"), self._load_users)

        danger = None
        if user and user["name"] != self.client.user:
            danger = ("Удалить", lambda: self._submit(dialog, error, lambda: self.client.call(
                "user_delete", name=user["name"]), self._load_users))
        error = self._dialog_buttons(dialog, save, danger)

    def _change_password(self):
        dialog = self._dialog("Смена пароля")
        old = self._entry(dialog, "Текущий пароль", show="•")
        new = self._entry(dialog, "Новый пароль", placeholder="не короче 8 символов", show="•")
        error = self._dialog_buttons(dialog, lambda: self._submit(
            dialog, error, lambda: self.client.call("passwd", old=old.get(), new=new.get()),
            lambda: self.say("Пароль изменён.")))

    # -- journal ------------------------------------------------------------
    def _build_audit(self, tab):
        ctk.CTkLabel(tab, text="Общий журнал сервера: входы, сеансы, передача файлов и буфера, изменения прав. "
                               "Виден всем учётным записям.", text_color=MUTED, font=self.app.f_small,
                     anchor="w").pack(fill="x", padx=6, pady=(2, 2))
        self.journal = JournalView(tab, self.app, self._fetch_audit, show_target=True,
                                   on_export=lambda rows: self.bg(lambda: self.client.call("audit_exported", rows=rows)))
        self.journal.pack(fill="both", expand=True, padx=2, pady=(0, 2))

    def _fetch_audit(self, since, done):
        names = {d["id"]: d["name"] for d in self.devices}

        def target(value):
            if value.isdigit() and len(value) == 9:
                return f"{names[value]} ({format_id(value)})" if value in names else format_id(value)
            return value

        def convert(rows):
            done([{"ts": r["ts"], "who": r["user"], "action": r["action"],
                   "target": target(r["target"]), "detail": r["detail"]} for r in rows])

        self.bg(lambda: self.client.call("audit", since=since, limit=5000)["audit"], convert)

    # -- enrollment ---------------------------------------------------------
    def _build_enroll(self, tab):
        ctk.CTkLabel(tab, text="Как добавить компьютер", font=self.app.f_head).pack(anchor="w", padx=12, pady=(8, 4))
        ctk.CTkLabel(tab, justify="left", wraplength=780, text=(
            "1. Установите и запустите Minideck на компьютере пользователя.\n"
            "2. В блоке «Этот компьютер», в строке «Сервер организации», нажмите «Подключить» и вставьте код ниже.\n"
            "3. Компьютер появится на вкладке «Компьютеры». Пользователь видит, что ПК управляется сервером,\n"
            "    получает запрос при подключении (если не отключил его) и видит плашку на экране во время сеанса.")
        ).pack(anchor="w", padx=12)
        ctk.CTkLabel(tab, text="Код регистрации", font=self.app.f_head).pack(anchor="w", padx=12, pady=(16, 4))
        self.code = ctk.CTkTextbox(tab, height=70, fg_color=FIELD, font=ctk.CTkFont(family="Consolas", size=12), wrap="char")
        self.code.pack(fill="x", padx=12)
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=10)
        self.copy_code = ctk.CTkButton(row, text="Копировать код", width=150, height=34, command=self._copy_code)
        self.copy_code.pack(side="left")
        ctk.CTkButton(row, text="Сменить код", width=120, height=34, command=self._rotate, **GHOST).pack(side="left", padx=8)
        ctk.CTkLabel(tab, text="Код подходит для любого числа компьютеров. Если он попал не в те руки — смените его: "
                               "уже добавленные компьютеры продолжат работать, а старый код перестанет действовать.",
                     text_color=MUTED, font=self.app.f_small, wraplength=780, justify="left").pack(anchor="w", padx=12)

    def _load_enroll(self):
        self.bg(lambda: self.client.call("enroll_info"), self._show_code)

    def _rotate(self):
        self.bg(lambda: self.client.call("enroll_rotate"), lambda info: (self._show_code(info), self.say("Код регистрации сменён.")))

    def _show_code(self, info):
        self._code = make_enroll_code(self.client.server, info["token"], info["fingerprint"])
        self.code.configure(state="normal")
        self.code.delete("1.0", "end")
        self.code.insert("1.0", self._code)
        self.code.configure(state="disabled")

    def _copy_code(self):
        if getattr(self, "_code", None):
            self.app._copy(self._code)
            self.copy_code.configure(text="✓ Скопировано")
            self.after(1300, lambda: self.copy_code.configure(text="Копировать код"))
