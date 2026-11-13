"""Main window: 'this computer' (host) on the left, 'remote computer' (viewer) on the right."""
import queue
import sys
import threading
import time
import tkinter as tk

import customtkinter as ctk
from PIL import Image

from . import DEFAULT_PORT, VERSION, autostart, config, resource_path, updater
from .console import LoginDialog
from .host import Host, RelayClient
from .journal import Journal
from .journalui import LocalJournalWindow
from .netutil import format_id, local_addresses, normalize_id
from .proto import AuthError
from .serverapi import ServerAgent, parse_enroll_code
from .ui import (CARD, FIELD, GHOST, GREEN, GREY, MUTED, ORANGE, PAGE, RED, describe_error,
                 raise_window, set_window_icon)
from .viewer import ViewerSession, ViewerWindow

MAX_CLIPBOARD = 1_000_000
MAX_RECENT = 6
THEMES = {"Системная": "system", "Светлая": "light", "Тёмная": "dark"}

RELAY_STATUS = {
    "connecting": ("подключение…", ORANGE),
    "online": ("в сети", GREEN),
    "offline": ("нет связи с relay", RED),
    "none": ("relay не задан", GREY),
}
AGENT_STATUS = {
    "connecting": ("подключение…", ORANGE),
    "online": ("в сети", GREEN),
    "offline": ("нет связи с сервером", RED),
    "revoked": ("доступ отозван сервером", RED),
    "error: enrollment required": ("код неверный или устарел", RED),
    "error: id taken": ("этот ID уже занят на сервере", RED),
    "error: certificate": ("сертификат сервера не совпадает", RED),
}


class App:
    def __init__(self):
        self.cfg = config.load()
        self.journal = Journal()
        self._journal_window = None
        ctk.set_appearance_mode(self.cfg.get("theme", "system"))
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk(fg_color=PAGE)
        self.root.title("Minideck")
        self.root.resizable(False, False)
        set_window_icon(self.root)
        self.ui_queue = queue.Queue()
        self.viewers = []
        self.relay_client = None
        self.agent = None          # link to a management server, when this PC is managed
        self.console = None        # administrator console window
        self._banner = None
        self._enroll = None        # pending enrollment: (address, fingerprint)
        self._clip_last = None
        self._toast_job = None
        self._connecting = False

        password = self.cfg["password"] if self.cfg["permanent_password"] and self.cfg["password"] \
            else config.new_password()
        self.v_password = tk.StringVar(value=password)
        self.v_permanent = tk.BooleanVar(value=self.cfg["permanent_password"])
        self.v_control = tk.BooleanVar(value=self.cfg["allow_control"])
        self.v_confirm = tk.BooleanVar(value=self.cfg["confirm"])
        self.v_clipboard = tk.BooleanVar(value=self.cfg["clipboard"])

        self.f_title = ctk.CTkFont(size=20, weight="bold")
        self.f_head = ctk.CTkFont(size=15, weight="bold")
        self.f_id = ctk.CTkFont(size=30, weight="bold")
        self.f_mono = ctk.CTkFont(family="Consolas", size=17)
        self.f_small = ctk.CTkFont(size=12)

        self.host = Host(password, port=self.cfg["port"], allow_control=self.cfg["allow_control"],
                         on_confirm=self._confirm,
                         on_change=lambda: self.call_ui(self._refresh_sessions),
                         on_clipboard=lambda text: self.call_ui(self._set_clipboard, text),
                         log=lambda msg: self.call_ui(self._on_host_log, msg),
                         on_event=self._on_host_event)
        self._build()
        self._start_host()
        self._start_relay()
        self._start_agent()
        self._force_show = self._use_pending_enrollment()
        self._refresh_sessions()
        self._refresh_recent()
        self.v_password.trace_add("write", lambda *_: self._on_password())
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.after(50, self._pump)
        self.root.after(1000, self._poll_clipboard)
        self._note_version()
        self._update = None              # newer release found by the last check
        if self.cfg.get("update_check", True):
            self.root.after(10_000, self._auto_check)

    # -- layout -------------------------------------------------------------
    def _build(self):
        root = self.root
        header = ctk.CTkFrame(root, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 8))
        try:
            logo = ctk.CTkImage(Image.open(resource_path("icon.png")), size=(36, 36))
            ctk.CTkLabel(header, image=logo, text="").pack(side="left")
        except OSError:
            pass
        ctk.CTkLabel(header, text="Minideck", font=self.f_title).pack(side="left", padx=(10, 6))
        ctk.CTkLabel(header, text=f"v{VERSION}", text_color=MUTED, font=self.f_small).pack(side="left", pady=(6, 0))
        ctk.CTkButton(header, text="⚙  Настройки", width=120, fg_color="transparent", border_width=1,
                      text_color=("#1f2937", "#e5e7eb"), hover_color=("#dfe4ea", "#33353a"),
                      command=self._open_settings).pack(side="right")
        ctk.CTkButton(header, text="Сервер управления", width=160, command=self._open_console).pack(
            side="right", padx=(0, 8))
        ctk.CTkButton(header, text="Журнал", width=90, command=self._open_journal, **GHOST).pack(
            side="right", padx=(0, 8))

        # "new version" strip, shown only when an update is available
        self._update_bar = ctk.CTkFrame(root, fg_color=("#dbeafe", "#1e3a5f"), corner_radius=10)
        self._update_text = ctk.CTkLabel(self._update_bar, text="", anchor="w",
                                         text_color=("#1e3a8a", "#dbeafe"))
        self._update_text.pack(side="left", padx=(14, 8), pady=8)
        ctk.CTkButton(self._update_bar, text="✕", width=30, height=30, fg_color="transparent",
                      text_color=("#1e3a8a", "#dbeafe"), hover_color=("#bfdbfe", "#27507f"),
                      command=self._update_bar_hide).pack(side="right", padx=(0, 6))
        self._update_notes = ctk.CTkButton(self._update_bar, text="Что нового", width=100, height=30,
                                           command=self._open_release_page, **GHOST)
        self._update_notes.pack(side="right", padx=4)
        self._update_btn = ctk.CTkButton(self._update_bar, text="Обновить", width=110, height=30,
                                         command=self._install_update)
        self._update_btn.pack(side="right", padx=4)

        body = self._body = ctk.CTkFrame(root, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        self._build_local(body)
        self._build_remote(body)

        self._toast = ctk.CTkLabel(root, text="", corner_radius=8, fg_color=("#1f2937", "#e5e7eb"),
                                   text_color=("#ffffff", "#111827"), height=34)
        footer = ctk.CTkFrame(root, fg_color="transparent", height=14)
        footer.pack(fill="x")

    def _card(self, parent, title, **grid):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14)
        card.grid(**grid)
        ctk.CTkLabel(card, text=title, font=self.f_head).pack(anchor="w", padx=18, pady=(14, 6))
        return card

    def _copy_button(self, parent, getter):
        button = ctk.CTkButton(parent, text="Копировать", width=104, height=30, fg_color="transparent",
                               border_width=1, text_color=("#1f2937", "#e5e7eb"),
                               hover_color=("#e8ecf1", "#3a3c42"))

        def copy():
            self._copy(getter())
            button.configure(text="✓ Скопировано")
            button.after(1300, lambda: button.configure(text="Копировать"))

        button.configure(command=copy)
        return button

    def _build_local(self, body):
        card = self._card(body, "Этот компьютер", row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 14))

        # ID
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18)
        ctk.CTkLabel(row, text="Ваш ID", text_color=MUTED, font=self.f_small).pack(side="left")
        self._relay_dot = ctk.CTkLabel(row, text="●", text_color=GREY, font=self.f_small)
        self._relay_dot.pack(side="left", padx=(10, 3))
        self._relay_text = ctk.CTkLabel(row, text="", text_color=MUTED, font=self.f_small)
        self._relay_text.pack(side="left")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(0, 2))
        ctk.CTkLabel(row, text=format_id(self.cfg["id"]), font=self.f_id).pack(side="left")
        self._copy_button(row, lambda: self.cfg["id"]).pack(side="right")
        self._relay_hint = ctk.CTkLabel(card, text="Чтобы подключаться по ID, укажите relay-сервер  →",
                                        text_color=("#2563eb", "#7aa7ff"), font=self.f_small, cursor="hand2")
        self._relay_hint.bind("<Button-1>", lambda e: self._open_settings())

        # LAN address
        self._addr_block = ctk.CTkFrame(card, fg_color="transparent")
        self._addr_block.pack(fill="x", padx=18, pady=(8, 0))
        ctk.CTkLabel(self._addr_block, text="Адрес в локальной сети", text_color=MUTED,
                     font=self.f_small).pack(anchor="w")
        row = ctk.CTkFrame(self._addr_block, fg_color="transparent")
        row.pack(fill="x")
        self._addr_label = ctk.CTkLabel(row, text="", font=self.f_mono)
        self._addr_label.pack(side="left")
        self._copy_button(row, lambda: self._primary_address).pack(side="right")
        self._addr_more = ctk.CTkLabel(self._addr_block, text="", text_color=MUTED, font=self.f_small)

        # password
        block = ctk.CTkFrame(card, fg_color="transparent")
        block.pack(fill="x", padx=18, pady=(10, 0))
        ctk.CTkLabel(block, text="Пароль", text_color=MUTED, font=self.f_small).pack(anchor="w")
        row = ctk.CTkFrame(block, fg_color="transparent")
        row.pack(fill="x")
        self._pwd_entry = ctk.CTkEntry(row, textvariable=self.v_password, font=self.f_mono, width=150,
                                       height=34, fg_color=FIELD, border_width=1)
        self._pwd_entry.pack(side="left")
        self._eye = ctk.CTkButton(row, text="Скрыть", width=70, height=30, fg_color="transparent",
                                  border_width=1, text_color=("#1f2937", "#e5e7eb"),
                                  hover_color=("#e8ecf1", "#3a3c42"), command=self._toggle_password)
        self._eye.pack(side="left", padx=(6, 0))
        ctk.CTkButton(row, text="⟳", width=34, height=30, fg_color="transparent", border_width=1,
                      text_color=("#1f2937", "#e5e7eb"), hover_color=("#e8ecf1", "#3a3c42"),
                      command=self._new_password).pack(side="left", padx=(6, 0))
        self._copy_button(row, self.v_password.get).pack(side="right")

        # management server
        block = ctk.CTkFrame(card, fg_color="transparent")
        block.pack(fill="x", padx=18, pady=(10, 0))
        ctk.CTkLabel(block, text="Сервер организации", text_color=MUTED, font=self.f_small).pack(anchor="w")
        row = ctk.CTkFrame(block, fg_color="transparent")
        row.pack(fill="x")
        self._agent_dot = ctk.CTkLabel(row, text="●", text_color=GREY, font=self.f_small)
        self._agent_text = ctk.CTkLabel(row, text="", anchor="w")
        self._agent_button = ctk.CTkButton(row, text="", width=104, height=30, **GHOST)
        self._agent_button.pack(side="right")
        self._agent_dot.pack(side="left", padx=(0, 6))
        self._agent_text.pack(side="left")

        # options
        options = ctk.CTkFrame(card, fg_color="transparent")
        options.pack(fill="x", padx=18, pady=(14, 0))
        for text, var in (("Разрешить управление мышью и клавиатурой", self.v_control),
                          ("Спрашивать перед подключением", self.v_confirm),
                          ("Общий буфер обмена (текст)", self.v_clipboard),
                          ("Постоянный пароль", self.v_permanent)):
            ctk.CTkSwitch(options, text=text, variable=var, command=self._save,
                          switch_width=38, switch_height=20).pack(anchor="w", pady=3)

        # sessions
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(14, 2))
        ctk.CTkLabel(row, text="Кто подключён", font=self.f_head).pack(side="left")
        self._kick = ctk.CTkButton(row, text="Отключить всех", width=120, height=28, fg_color=RED,
                                   hover_color="#b8342d", command=lambda: self.host.disconnect_all(kick=True))
        self._sessions_box = ctk.CTkFrame(card, fg_color="transparent")
        self._sessions_box.pack(fill="x", padx=18, pady=(0, 16))

    def _build_remote(self, body):
        card = self._card(body, "Удалённый компьютер", row=0, column=1, sticky="new")
        self._address = ctk.CTkEntry(card, placeholder_text="ID или адрес, например 123 456 789",
                                     width=320, height=40, font=ctk.CTkFont(size=16),
                                     fg_color=FIELD, border_width=1)
        self._address.pack(fill="x", padx=18)
        self._remote_pwd = ctk.CTkEntry(card, placeholder_text="Пароль", show="•", height=40,
                                        font=ctk.CTkFont(size=16), fg_color=FIELD, border_width=1)
        self._remote_pwd.pack(fill="x", padx=18, pady=(8, 0))
        self._connect_btn = ctk.CTkButton(card, text="Подключиться", height=40,
                                          font=ctk.CTkFont(size=15, weight="bold"), command=self._connect)
        self._connect_btn.pack(fill="x", padx=18, pady=(12, 0))
        self._progress = ctk.CTkProgressBar(card, mode="indeterminate", height=4)
        self._error = ctk.CTkLabel(card, text="", text_color=RED, font=self.f_small,
                                   wraplength=320, justify="left")
        self._remote_tail = ctk.CTkFrame(card, fg_color="transparent", height=2)
        self._remote_tail.pack(fill="x", padx=18, pady=8)  # anchor for the progress bar / error text
        self._address.bind("<Return>", lambda e: self._remote_pwd.focus_set())
        self._remote_pwd.bind("<Return>", lambda e: self._connect())
        self._address.bind("<KeyRelease>", lambda e: self._show_error(""))
        self._remote_pwd.bind("<KeyRelease>", lambda e: self._show_error("") if e.keysym != "Return" else None)

        self._recent_card = self._card(body, "Недавние подключения", row=1, column=1, sticky="nsew", pady=(14, 0))
        self._recent_box = ctk.CTkFrame(self._recent_card, fg_color="transparent")
        self._recent_box.pack(fill="both", expand=True, padx=10, pady=(0, 12))
        body.grid_rowconfigure(1, weight=1)

    # -- small UI helpers ---------------------------------------------------
    def toast(self, text, ms=2600):
        self._toast.configure(text=f"   {text}   ")
        self._toast.place(relx=0.5, rely=1.0, y=-14, anchor="s")
        self._toast.lift()
        if self._toast_job:
            self.root.after_cancel(self._toast_job)
        self._toast_job = self.root.after(ms, self._toast.place_forget)

    def _copy(self, text):
        self._clip_last = text  # never broadcast our own ID/password through clipboard sync
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _toggle_password(self):
        hidden = self._pwd_entry.cget("show") == ""
        self._pwd_entry.configure(show="•" if hidden else "")
        self._eye.configure(text="Показать" if hidden else "Скрыть")

    def _new_password(self):
        self.v_password.set(config.new_password())
        self.toast("Пароль изменён")

    def _show_error(self, text):
        if text:
            self._error.configure(text=text)
            self._error.pack(fill="x", padx=18, pady=(8, 0), before=self._remote_tail)
        else:
            self._error.pack_forget()

    def _set_relay_status(self, status):
        text, color = RELAY_STATUS.get(status, (status, RED))
        self._relay_dot.configure(text_color=color)
        self._relay_text.configure(text=text)
        if status == "none":
            self._relay_hint.pack(anchor="w", padx=18, before=self._addr_block)
        else:
            self._relay_hint.pack_forget()

    # -- host ---------------------------------------------------------------
    def _start_host(self):
        if self.host.start():
            suffix = "" if self.host.port == DEFAULT_PORT else f":{self.host.port}"
            addresses = [ip + suffix for ip in local_addresses()]
            # private LAN ranges first: that is the one people actually need
            addresses.sort(key=lambda ip: not ip.startswith(("192.168.", "10.")))
            self._primary_address = addresses[0]
            self._addr_label.configure(text=addresses[0])
            if len(addresses) > 1:
                self._addr_more.configure(text="также: " + ", ".join(addresses[1:]))
                self._addr_more.pack(anchor="w")
        else:
            self._primary_address = ""
            self._addr_label.configure(text=f"порт {self.host.port} занят", text_color=RED)

    def _start_relay(self):
        if self.relay_client is not None:
            self.relay_client.stop()
            self.relay_client = None
        relay = self.cfg["relay"].strip()
        if not relay:
            self._set_relay_status("none")
            return
        self._set_relay_status("connecting")
        self.relay_client = RelayClient(
            self.host, relay, self.cfg["id"], self.cfg["id_key"],
            on_status=lambda s: self.call_ui(self._set_relay_status, s))
        self.relay_client.start()

    def _on_password(self):
        self.host.password = self.v_password.get()
        if self.v_permanent.get():
            self._save()

    def _on_host_event(self, action, who, detail, account):
        """Host thread: write the local journal; pass server-brokered events on to the server."""
        self.journal.add(action, who, detail)
        agent = self.agent
        if account and agent is not None:
            agent.report("session_accepted" if action == "session_start" else action, account, detail)

    def _open_journal(self):
        if self._journal_window is not None and self._journal_window.winfo_exists():
            self._journal_window.lift()
            self._journal_window.focus_force()
            return
        self._journal_window = LocalJournalWindow(self)

    def _save(self):
        for key, var, label in (("allow_control", self.v_control, "управление мышью и клавиатурой"),
                                ("confirm", self.v_confirm, "запрос перед подключением"),
                                ("clipboard", self.v_clipboard, "общий буфер обмена"),
                                ("permanent_password", self.v_permanent, "постоянный пароль")):
            if bool(self.cfg.get(key)) != var.get():
                self.journal.add("setting_changed", "этот компьютер", f"{label}: {'вкл' if var.get() else 'выкл'}")
        self.host.allow_control = self.v_control.get()
        self.cfg.update(
            permanent_password=self.v_permanent.get(),
            password=self.v_password.get() if self.v_permanent.get() else "",
            allow_control=self.v_control.get(), confirm=self.v_confirm.get(),
            clipboard=self.v_clipboard.get())
        config.save(self.cfg)

    def _on_host_log(self, msg):
        if msg.startswith("file received: "):
            self.toast("Получен файл: " + msg.split(": ", 1)[1], 5000)
        elif msg.startswith("wrong password"):
            self.toast("Попытка входа с неверным паролем")

    def _confirm(self, peer, name):
        """Called on a host thread; asks the user on the UI thread."""
        if not self.v_confirm.get():
            return True
        done, answer = threading.Event(), [False]
        self.call_ui(self._ask_incoming, peer, name, done, answer)
        done.wait(40)
        return answer[0]

    def _ask_incoming(self, peer, name, done, answer):
        dialog = ctk.CTkToplevel(self.root, fg_color=PAGE)
        dialog.title("Входящее подключение")
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)
        set_window_icon(dialog)
        ctk.CTkLabel(dialog, text="Входящее подключение", font=self.f_title).pack(padx=28, pady=(22, 4))
        ctk.CTkLabel(dialog, text=f"{name}", font=self.f_head).pack(padx=28)
        ctk.CTkLabel(dialog, text=f"{'через relay' if peer == 'relay' else peer}", text_color=MUTED).pack(padx=28)
        what = "видеть экран и управлять этим компьютером" if self.v_control.get() else "видеть экран этого компьютера"
        ctk.CTkLabel(dialog, text=f"Сможет {what}.", wraplength=320).pack(padx=28, pady=(10, 0))
        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(padx=28, pady=20)
        state = {"left": 30, "closed": False}

        def finish(allow):
            if state["closed"]:
                return
            state["closed"] = True
            answer[0] = allow
            done.set()
            dialog.destroy()

        deny = ctk.CTkButton(buttons, text="Отклонить (30)", width=150, height=38, fg_color=RED,
                             hover_color="#b8342d", command=lambda: finish(False))
        deny.pack(side="left", padx=(0, 10))
        ctk.CTkButton(buttons, text="Разрешить", width=150, height=38, fg_color=GREEN,
                      hover_color="#1b8549", command=lambda: finish(True)).pack(side="left")

        def countdown():
            if state["closed"]:
                return
            state["left"] -= 1
            if state["left"] <= 0:
                finish(False)
                return
            deny.configure(text=f"Отклонить ({state['left']})")
            dialog.after(1000, countdown)

        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dialog.after(1000, countdown)
        dialog.after(150, lambda: (dialog.lift(), dialog.focus_force()))

    def _refresh_sessions(self):
        for child in self._sessions_box.winfo_children():
            child.destroy()
        sessions = list(self.host.sessions)
        self._update_banner(sessions)
        if not sessions:
            self._kick.pack_forget()
            ctk.CTkLabel(self._sessions_box, text="Сейчас никто не подключён", text_color=MUTED).pack(anchor="w")
            return
        if len(sessions) > 1:
            self._kick.pack(side="right")
        else:
            self._kick.pack_forget()
        for session in sessions:
            row = ctk.CTkFrame(self._sessions_box, fg_color=FIELD, corner_radius=8)
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text="●", text_color=GREEN).pack(side="left", padx=(10, 6))
            where = "через relay" if session.peer == "relay" else session.peer
            ctk.CTkLabel(row, text=f"{session.name}  ·  {where}").pack(side="left", pady=5)
            ctk.CTkButton(row, text="Отключить", width=90, height=26, fg_color=RED, hover_color="#b8342d",
                          command=session.kick).pack(side="right", padx=6)

    # -- management server (this PC as a managed computer) --------------------
    def _use_pending_enrollment(self):
        """A code left by the installer: enroll now and tell the user, in the open, what it means."""
        code = self.cfg.pop("pending_enroll", "") or ""
        if not code:
            return False
        config.save(self.cfg)
        parsed = parse_enroll_code(code)
        if not parsed or (self.cfg.get("server") or {}).get("address"):
            return False
        address, token, fingerprint = parsed
        self._enroll = (address, fingerprint)
        self._start_agent(enroll_token=token)
        self.root.after(700, lambda: self._managed_notice(address))
        return True

    def _managed_notice(self, address):
        dialog = ctk.CTkToplevel(self.root, fg_color=PAGE)
        dialog.title("Сервер организации")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        set_window_icon(dialog)
        ctk.CTkLabel(dialog, text="Компьютер подключается\nк серверу организации", font=self.f_title,
                     justify="left").pack(anchor="w", padx=24, pady=(20, 4))
        ctk.CTkLabel(dialog, text=address, text_color=MUTED).pack(anchor="w", padx=24)
        for line in ("Специалисты вашей организации смогут подключаться к этому компьютеру через сервер.",
                     "Перед каждым подключением вы увидите запрос с именем подключающегося. "
                     "Его можно отключить переключателем «Спрашивать перед подключением».",
                     "Во время сеанса вверху экрана видна красная плашка с кнопкой «Отключить».",
                     "Все подключения записываются в журнал — кнопка «Журнал» в окне программы.",
                     "Отключить компьютер от сервера можно кнопкой «Отключить» в строке «Сервер организации»."):
            ctk.CTkLabel(dialog, text="•  " + line, justify="left", anchor="w", wraplength=420).pack(
                fill="x", padx=24, pady=(8, 0))
        ctk.CTkButton(dialog, text="Понятно", width=130, height=36, command=dialog.destroy).pack(
            anchor="e", padx=24, pady=20)
        raise_window(dialog)

    def _start_agent(self, enroll_token=None):
        if self.agent is not None:
            self.agent.stop()
            self.agent = None
        saved = self.cfg.get("server") or {}
        address, pin = self._enroll or (saved.get("address"), saved.get("pin"))
        if not address:
            self._show_agent(None)
            return
        self.agent = ServerAgent(self.host, address, pin, self.cfg["id"], self.cfg["id_key"],
                                 enroll_token=enroll_token,
                                 on_status=lambda s: self.call_ui(self._on_agent_status, s))
        self._show_agent("connecting")
        self.agent.start()

    def _show_agent(self, status):
        if status is None:
            self._agent_dot.configure(text_color=GREY)
            self._agent_text.configure(text="не подключён", text_color=MUTED)
            self._agent_button.configure(text="Подключить", command=self._ask_enroll_code)
            return
        text, color = AGENT_STATUS.get(status, (status, RED))
        address = self._enroll[0] if self._enroll else (self.cfg.get("server") or {}).get("address", "")
        self._agent_dot.configure(text_color=color)
        self._agent_text.configure(text=f"{address} · {text}", text_color=("#1f2937", "#e5e7eb"))
        self._agent_button.configure(text="Отключить", command=self._leave_server)

    def _on_agent_status(self, status):
        if self.agent is None:
            return
        if status == "online" and self._enroll:        # enrollment succeeded: remember the server
            self.cfg["server"] = {"address": self._enroll[0], "pin": self._enroll[1]}
            self.journal.add("server_joined", "этот компьютер", self._enroll[0])
            self._enroll = None
            config.save(self.cfg)
            self.toast("Компьютер подключён к серверу организации", 4000)
        self._show_agent(status)
        if status == "revoked" or (status.startswith("error") and self._enroll):
            text = AGENT_STATUS.get(status, (status,))[0]
            self._leave_server(silent=True)
            self.toast(f"Сервер организации: {text}", 5000)

    def _leave_server(self, silent=False):
        if self.agent is not None:
            self.agent.stop()
            self.agent = None
        self._enroll = None
        if (self.cfg.get("server") or {}).get("address"):
            self.journal.add("server_left", "этот компьютер", self.cfg["server"]["address"])
        self.cfg["server"] = {}
        config.save(self.cfg)
        self._show_agent(None)
        if not silent:
            self.toast("Компьютер отключён от сервера организации")

    def _ask_enroll_code(self):
        dialog = ctk.CTkToplevel(self.root, fg_color=PAGE)
        dialog.title("Сервер организации")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        set_window_icon(dialog)
        ctk.CTkLabel(dialog, text="Подключить к серверу организации", font=self.f_title).pack(anchor="w", padx=24, pady=(20, 6))
        ctk.CTkLabel(dialog, justify="left", wraplength=400, text_color=MUTED, text=(
            "После подключения администраторы вашей организации смогут открывать сеансы с этим компьютером "
            "без пароля из окна программы. Вы по-прежнему будете видеть, кто подключён, сможете разорвать сеанс "
            "и отключить компьютер от сервера в любой момент.")).pack(anchor="w", padx=24)
        ctk.CTkLabel(dialog, text="Код регистрации от администратора", font=self.f_head).pack(anchor="w", padx=24, pady=(14, 4))
        code = ctk.CTkTextbox(dialog, width=400, height=74, fg_color=FIELD, wrap="char",
                              font=ctk.CTkFont(family="Consolas", size=12))
        code.pack(padx=24)
        error = ctk.CTkLabel(dialog, text="", text_color=RED, font=self.f_small)
        error.pack(anchor="w", padx=24)

        def join():
            parsed = parse_enroll_code("".join(code.get("1.0", "end").split()))
            if not parsed:
                error.configure(text="Это не похоже на код регистрации (minideck://…).")
                return
            address, token, fingerprint = parsed
            dialog.destroy()
            self._enroll = (address, fingerprint)
            self._start_agent(enroll_token=token)

        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(6, 20))
        ctk.CTkButton(row, text="Подключить", width=130, height=36, command=join).pack(side="right")
        ctk.CTkButton(row, text="Отмена", width=100, height=36, command=dialog.destroy, **GHOST).pack(side="right", padx=8)
        raise_window(dialog)
        dialog.after(200, code.focus_set)

    def _open_console(self):
        if self.console is not None and self.console.winfo_exists():
            self.console.lift()
            self.console.focus_force()
            return
        LoginDialog(self)

    # -- on-screen notice while someone is connected ---------------------------
    def _update_banner(self, sessions):
        """An always-on-top strip so the person at this computer knows a session is active."""
        if not sessions:
            if self._banner is not None:
                self._banner.destroy()
                self._banner = None
            return
        if self._banner is None:
            banner = self._banner = tk.Toplevel(self.root)
            banner.overrideredirect(True)
            banner.attributes("-topmost", True)
            banner.configure(bg="#b3261e")
            banner.label = tk.Label(banner, bg="#b3261e", fg="#ffffff", font=("Segoe UI", 10, "bold"), padx=14, pady=6)
            banner.label.pack(side="left")
            tk.Button(banner, text="Отключить", command=lambda: self.host.disconnect_all(kick=True), relief="flat", bg="#ffffff",
                      fg="#b3261e", activebackground="#f3d6d4", font=("Segoe UI", 9, "bold"), padx=10,
                      cursor="hand2").pack(side="left", padx=(0, 8), pady=4)
        names = ", ".join(s.name + ("" if s.can_control else " — только просмотр") for s in sessions)
        self._banner.label.configure(text=f"●  Minideck: к этому компьютеру подключён {names}")
        self._banner.update_idletasks()
        x = (self._banner.winfo_screenwidth() - self._banner.winfo_reqwidth()) // 2
        self._banner.geometry(f"+{x}+0")
        self._banner.lift()

    # -- viewer -------------------------------------------------------------
    def _connect(self):
        if self._connecting:
            return
        address = self._address.get().strip()
        if not address:
            self._show_error("Введите ID или адрес удалённого компьютера.")
            self._address.focus_set()
            return
        relay = self.cfg["relay"].strip() or None
        session = ViewerSession(address, self._remote_pwd.get(), relay,
                                on_clipboard=lambda text: self.call_ui(self._set_clipboard, text))
        self._set_connecting(True)

        def work():
            try:
                session.connect()
            except Exception as exc:
                self.call_ui(self._connect_failed, exc)
            else:
                self.call_ui(self._connected, session)

        threading.Thread(target=work, daemon=True).start()

    def _set_connecting(self, active):
        self._connecting = active
        if active:
            self._show_error("")
            self._connect_btn.configure(state="disabled", text="Подключение…")
            self._progress.pack(fill="x", padx=18, pady=(8, 0), before=self._remote_tail)
            self._progress.start()
        else:
            self._progress.stop()
            self._progress.pack_forget()
            self._connect_btn.configure(state="normal", text="Подключиться")

    def _connect_failed(self, exc):
        self._set_connecting(False)
        self._show_error(describe_error(exc))
        self.journal.add("out_failed", self._address.get().strip(), describe_error(exc))
        if isinstance(exc, AuthError):
            self._remote_pwd.focus_set()
            self._remote_pwd.select_range(0, "end")

    def _connected(self, session):
        self._set_connecting(False)
        self._remote_pwd.delete(0, "end")
        self.open_viewer(session)

    def open_viewer(self, session, remember=True):
        if remember:
            self._remember(session)
        target = session.hello.get("name") or session.address
        if session.address and session.address != target:
            target = f"{target} · {session.address}"
        mode = "управление" if session.hello.get("control", True) else "только просмотр"
        self.journal.add("out_connect", target, mode + (", через сервер" if session.stream_factory else ""))
        session.on_event = lambda action, detail: self.journal.add(action, target, detail)

        def closed(window):
            self.viewers.remove(window)
            self.journal.add("out_disconnect", target)

        window = ViewerWindow(self.root, session, on_closed=closed)
        set_window_icon(window)
        self.viewers.append(window)

    # -- recent connections (addresses only, never passwords) ----------------
    def _remember(self, session):
        ident = normalize_id(session.address)
        address = format_id(ident) if ident else session.address
        recent = [r for r in self.cfg.get("recent", []) if r.get("address") != address]
        recent.insert(0, {"address": address, "name": session.hello.get("name", ""),
                          "time": int(time.time())})
        self.cfg["recent"] = recent[:MAX_RECENT]
        config.save(self.cfg)
        self._refresh_recent()

    def _forget(self, address):
        self.cfg["recent"] = [r for r in self.cfg.get("recent", []) if r.get("address") != address]
        config.save(self.cfg)
        self._refresh_recent()

    def _pick_recent(self, address):
        self._address.delete(0, "end")
        self._address.insert(0, address)
        self._show_error("")
        self._remote_pwd.focus_set()

    def _refresh_recent(self):
        for child in self._recent_box.winfo_children():
            child.destroy()
        recent = self.cfg.get("recent", [])
        if not recent:
            ctk.CTkLabel(self._recent_box, text="Здесь появятся компьютеры,\nк которым вы подключались",
                         text_color=MUTED, justify="left").pack(anchor="w", padx=8, pady=4)
            return
        for item in recent:
            address = item.get("address", "")
            row = ctk.CTkFrame(self._recent_box, fg_color="transparent", corner_radius=8, cursor="hand2")
            row.pack(fill="x", pady=1)
            ctk.CTkButton(row, text="✕", width=30, height=30, fg_color="transparent", text_color=MUTED,
                          hover_color=("#d5dbe3", "#4a4d55"),
                          command=lambda a=address: self._forget(a)).pack(side="right", padx=(0, 6))
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True, padx=10, pady=5)
            parts = [text, ctk.CTkLabel(text, text=item.get("name") or address, height=18,
                                        font=ctk.CTkFont(size=13, weight="bold"), anchor="w")]
            parts[1].pack(fill="x")
            if item.get("name"):
                parts.append(ctk.CTkLabel(text, text=address, height=16, text_color=MUTED,
                                          font=self.f_small, anchor="w"))
                parts[-1].pack(fill="x")
            for widget in [row] + parts:  # the whole row behaves like one button
                widget.bind("<Button-1>", lambda e, a=address: self._pick_recent(a))
                widget.bind("<Enter>", lambda e, r=row: r.configure(fg_color=("#e8ecf1", "#3a3c42")))
                widget.bind("<Leave>", lambda e, r=row: r.configure(fg_color="transparent"))

    # -- settings -----------------------------------------------------------
    def _open_settings(self):
        if getattr(self, "_settings", None) is not None and self._settings.winfo_exists():
            self._settings.lift()
            self._settings.focus_force()
            return
        dialog = self._settings = ctk.CTkToplevel(self.root, fg_color=PAGE)
        dialog.title("Настройки")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        set_window_icon(dialog)
        ctk.CTkLabel(dialog, text="Настройки", font=self.f_title).pack(anchor="w", padx=24, pady=(20, 10))

        ctk.CTkLabel(dialog, text="Relay-сервер", font=self.f_head).pack(anchor="w", padx=24)
        ctk.CTkLabel(dialog, text="Нужен для подключения по ID через интернет. Формат: host или host:порт.",
                     text_color=MUTED, font=self.f_small, wraplength=360, justify="left").pack(anchor="w", padx=24)
        relay = ctk.CTkEntry(dialog, width=360, height=36, placeholder_text="relay.example.com:7071",
                             fg_color=FIELD, border_width=1)
        relay.pack(padx=24, pady=(6, 14))
        if self.cfg["relay"]:
            relay.insert(0, self.cfg["relay"])

        ctk.CTkLabel(dialog, text="Порт для прямых подключений", font=self.f_head).pack(anchor="w", padx=24)
        ctk.CTkLabel(dialog, text="Изменение вступит в силу после перезапуска программы.",
                     text_color=MUTED, font=self.f_small).pack(anchor="w", padx=24)
        port = ctk.CTkEntry(dialog, width=110, height=36, fg_color=FIELD, border_width=1)
        port.pack(anchor="w", padx=24, pady=(6, 14))
        port.insert(0, str(self.cfg["port"]))

        ctk.CTkLabel(dialog, text="Оформление", font=self.f_head).pack(anchor="w", padx=24)
        names = {v: k for k, v in THEMES.items()}
        theme = ctk.CTkSegmentedButton(dialog, values=list(THEMES),
                                       command=lambda name: ctk.set_appearance_mode(THEMES[name]))
        theme.set(names.get(self.cfg.get("theme", "system"), "Системная"))
        theme.pack(anchor="w", padx=24, pady=(6, 6))

        startup = tk.BooleanVar(value=autostart.is_enabled())
        if autostart.supported():
            ctk.CTkLabel(dialog, text="Запуск", font=self.f_head).pack(anchor="w", padx=24, pady=(8, 0))
            ctk.CTkSwitch(dialog, text="Запускать Minideck при входе в Windows (свёрнутым)", variable=startup,
                          switch_width=38, switch_height=20).pack(anchor="w", padx=24, pady=(4, 6))

        ctk.CTkLabel(dialog, text="Обновления", font=self.f_head).pack(anchor="w", padx=24, pady=(8, 0))
        auto_update = tk.BooleanVar(value=self.cfg.get("update_check", True))
        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(4, 6))
        ctk.CTkSwitch(row, text="Проверять новые версии", variable=auto_update,
                      switch_width=38, switch_height=20).pack(side="left")
        ctk.CTkButton(row, text="Проверить сейчас", width=140, height=30,
                      command=lambda: self._check_updates(manual=True), **GHOST).pack(side="right")

        error = ctk.CTkLabel(dialog, text="", text_color=RED, font=self.f_small)
        error.pack(anchor="w", padx=24)

        def save():
            self.cfg["update_check"] = auto_update.get()
            try:
                if startup.get() != autostart.is_enabled():
                    autostart.set_enabled(startup.get())
            except OSError:
                error.configure(text="Не удалось изменить автозапуск.")
                return
            try:
                number = int(port.get())
                if not 1 <= number <= 65535:
                    raise ValueError
            except ValueError:
                error.configure(text="Порт должен быть числом от 1 до 65535.")
                return
            relay_changed = relay.get().strip() != self.cfg["relay"]
            self.cfg.update(relay=relay.get().strip(), port=number, theme=THEMES[theme.get()])
            config.save(self.cfg)
            if relay_changed:
                self._start_relay()
            dialog.destroy()
            self.toast("Настройки сохранены")

        def cancel():
            ctk.set_appearance_mode(self.cfg.get("theme", "system"))
            dialog.destroy()

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=24, pady=(4, 20))
        ctk.CTkButton(buttons, text="Сохранить", width=130, height=36, command=save).pack(side="right")
        ctk.CTkButton(buttons, text="Отмена", width=110, height=36, fg_color="transparent", border_width=1,
                      text_color=("#1f2937", "#e5e7eb"), hover_color=("#e8ecf1", "#3a3c42"),
                      command=cancel).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.after(150, lambda: (dialog.lift(), dialog.focus_force()))

    # -- updates --------------------------------------------------------------
    def _note_version(self):
        """Record in the journal that the program was updated since it last ran."""
        last = self.cfg.get("last_version") or ""
        if last != VERSION:
            if last:
                self.journal.add("program_updated", "этот компьютер", f"{last} → {VERSION}")
            self.cfg["last_version"] = VERSION
            config.save(self.cfg)

    def _auto_check(self):
        if self.cfg.get("update_check", True):
            self._check_updates()
        self.root.after(updater.CHECK_EVERY * 1000, self._auto_check)

    def _check_updates(self, manual=False):
        source = (self.cfg.get("update_source") or "").strip() or None

        def work():
            try:
                found = updater.check(source)
            except updater.UpdateError as exc:
                if manual:
                    self.call_ui(self.toast, str(exc).capitalize(), 5000)
                return
            if found:
                self.call_ui(self._update_bar_show, found)
            elif manual:
                self.call_ui(self.toast, f"Установлена последняя версия ({VERSION})")

        threading.Thread(target=work, daemon=True).start()

    def _update_bar_show(self, found):
        self._update = found
        auto = updater.installed_copy() and found.get("setup") and found["setup"].get("sha256")
        self._update_text.configure(text=f"Доступна новая версия Minideck {found['version']} "
                                         f"(у вас {VERSION})")
        self._update_btn.configure(state="normal", text="Обновить" if auto else "Скачать")
        self._update_bar.pack(fill="x", padx=20, pady=(0, 8), before=self._body)

    def _update_bar_hide(self):
        self._update_bar.pack_forget()

    def _open_release_page(self):
        import webbrowser
        if self._update and self._update.get("page"):
            webbrowser.open(self._update["page"])

    def _install_update(self):
        found = self._update
        if not found:
            return
        if not (updater.installed_copy() and found.get("setup") and found["setup"].get("sha256")):
            self._open_release_page()    # portable copy or run from source: just open the download page
            return
        if self.host.sessions:
            self.toast("Сейчас к этому компьютеру подключены — обновите после окончания сеанса", 5000)
            return
        self._update_btn.configure(state="disabled", text="Загрузка…")

        def progress(done, total):
            if total:
                percent = done * 100 // total
                self.call_ui(lambda: self._update_btn.configure(text=f"Загрузка {percent}%"))

        def work():
            try:
                path = updater.download(found["setup"], progress)
            except updater.UpdateError as exc:
                self.call_ui(self._update_failed, str(exc))
                return
            self.call_ui(self._run_update, path, found["version"])

        threading.Thread(target=work, daemon=True).start()

    def _update_failed(self, text):
        self._update_btn.configure(state="normal", text="Обновить")
        self.toast(f"Обновление не удалось: {text}", 6000)

    def _run_update(self, path, version):
        if self.host.sessions:          # someone connected while the file was downloading
            self._update_btn.configure(state="normal", text="Обновить")
            self.toast("К компьютеру подключились — обновите после окончания сеанса", 5000)
            return
        self.journal.add("update_started", "этот компьютер", f"{VERSION} → {version}")
        updater.start_update(path)
        self._shutdown()

    # -- clipboard ----------------------------------------------------------
    def _poll_clipboard(self):
        self.root.after(1000, self._poll_clipboard)
        if not self.v_clipboard.get():
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        if self._clip_last is None:      # don't leak whatever was copied before start
            self._clip_last = text
            return
        if text == self._clip_last or not text or len(text) > MAX_CLIPBOARD:
            return
        self._clip_last = text
        self.host.send_clipboard(text)
        for window in self.viewers:
            window.session.send_clipboard(text)

    def _set_clipboard(self, text):
        if not self.v_clipboard.get() or len(text) > MAX_CLIPBOARD:
            return
        self._clip_last = text
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # -- plumbing -----------------------------------------------------------
    def call_ui(self, fn, *args):
        """Run fn on the Tk thread; safe to call from any thread."""
        self.ui_queue.put((fn, args))

    def _pump(self):
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                try:
                    fn(*args)
                except tk.TclError:
                    pass
        except queue.Empty:
            pass
        self.root.after(30, self._pump)

    def _quit(self):
        if self.host.sessions and not self._ask_quit():
            return
        self._shutdown()

    def _shutdown(self):
        for window in list(self.viewers):
            window.session.close()
        if self.console is not None and self.console.winfo_exists():
            self.console.close()
        if self.agent is not None:
            self.agent.stop()
        if self.relay_client is not None:
            self.relay_client.stop()
        self.host.stop()
        self.root.destroy()

    def _ask_quit(self):
        from tkinter import messagebox
        return messagebox.askyesno("Minideck", "К этому компьютеру сейчас подключены. Закрыть программу?")

    def run(self, connect_to=None, minimized=False):
        if connect_to:
            self._address.insert(0, connect_to)
            self._remote_pwd.focus_set()
        if minimized and not self._force_show:
            self.root.after(400, self.root.iconify)   # stays visible in the taskbar
        self.root.mainloop()


def main(connect_to=None, minimized=False):
    App().run(connect_to, minimized)
