"""Setup wizard (MinideckSetup.exe) and the uninstall window (Minideck.exe uninstall)."""
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image

from . import VERSION, config, installer, resource_path
from .serverapi import parse_enroll_code
from .ui import FIELD, GHOST, GREEN, MUTED, PAGE, RED, RED_HOVER, set_window_icon

CODE_FILE = "minideck-server.txt"   # put next to MinideckSetup.exe to pre-fill the server code


def preset_code():
    """Enrollment code supplied by the administrator: --code=... or a file next to the setup."""
    for arg in sys.argv[1:]:
        if arg.startswith("--code="):
            return arg.split("=", 1)[1].strip()
    here = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve().parent
    try:
        return (here / CODE_FILE).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


class Wizard:
    def __init__(self):
        ctk.set_appearance_mode("system")
        self.root = ctk.CTk(fg_color=PAGE)
        self.root.title("Установка Minideck")
        self.root.geometry("600x500")
        self.root.resizable(False, False)
        set_window_icon(self.root)
        self.f_title = ctk.CTkFont(size=22, weight="bold")
        self.f_head = ctk.CTkFont(size=14, weight="bold")
        self.f_small = ctk.CTkFont(size=12)

        self.existing = installer.installed_info()
        self.target = Path(self.existing["location"]) if self.existing else installer.default_dir()
        cfg = config.load()
        self.enrolled = (cfg.get("server") or {}).get("address", "")
        code = preset_code()
        self.code_preset = bool(code) and parse_enroll_code(code) is not None

        self.v_desktop = tk.BooleanVar(value=True)
        self.v_autostart = tk.BooleanVar(value=True)
        self.v_join = tk.BooleanVar(value=self.code_preset and not self.enrolled)
        self.v_launch = tk.BooleanVar(value=True)
        self._code_text = code if self.code_preset else ""
        self.exe = None

        self.body = ctk.CTkFrame(self.root, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=28, pady=(22, 0))
        self.buttons = ctk.CTkFrame(self.root, fg_color="transparent")
        self.buttons.pack(fill="x", padx=28, pady=20)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        self.page_welcome()

    # -- helpers ------------------------------------------------------------
    def _clear(self):
        for frame in (self.body, self.buttons):
            for child in frame.winfo_children():
                child.destroy()

    def _button(self, text, command, primary=True, side="right", **kw):
        style = {} if primary else GHOST
        button = ctk.CTkButton(self.buttons, text=text, width=130, height=38, command=command, **style, **kw)
        button.pack(side=side, padx=(8, 0) if side == "right" else (0, 8))
        return button

    def _text(self, text, muted=False, **pack):
        label = ctk.CTkLabel(self.body, text=text, justify="left", anchor="w", wraplength=540,
                             text_color=MUTED if muted else ("#1f2937", "#e5e7eb"))
        label.pack(fill="x", **pack)
        return label

    def _cancel(self):
        if getattr(self, "_busy", False):
            return
        self.root.destroy()

    # -- pages --------------------------------------------------------------
    def page_welcome(self):
        self._clear()
        head = ctk.CTkFrame(self.body, fg_color="transparent")
        head.pack(fill="x", pady=(0, 16))
        try:
            logo = ctk.CTkImage(Image.open(resource_path("icon.png")), size=(56, 56))
            ctk.CTkLabel(head, image=logo, text="").pack(side="left")
        except OSError:
            pass
        title = ctk.CTkFrame(head, fg_color="transparent")
        title.pack(side="left", padx=14)
        ctk.CTkLabel(title, text="Установка Minideck", font=self.f_title).pack(anchor="w")
        ctk.CTkLabel(title, text=f"версия {VERSION}", text_color=MUTED).pack(anchor="w")

        self._text("Minideck — программа удалённого доступа: она позволяет специалистам вашей организации "
                   "подключаться к этому компьютеру, чтобы помочь с работой.", pady=(0, 12))
        ctk.CTkLabel(self.body, text="Что важно знать", font=self.f_head, anchor="w").pack(fill="x")
        for line in ("Программа работает открыто: её окно и значок всегда доступны.",
                     "Перед подключением вы увидите запрос с именем подключающегося.",
                     "Во время сеанса вверху экрана видна красная плашка с кнопкой «Отключить».",
                     "Все подключения записываются в журнал, который вы можете открыть сами.",
                     "Программу можно удалить через «Параметры → Приложения»."):
            self._text("•  " + line, pady=1)
        if self.existing:
            self._text(f"Уже установлена версия {self.existing['version']} — она будет обновлена.",
                       muted=True, pady=(14, 0))
        self._button("Далее", self.page_options)
        self._button("Отмена", self._cancel, primary=False)

    def page_options(self):
        self._clear()
        ctk.CTkLabel(self.body, text="Параметры установки", font=self.f_title, anchor="w").pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(self.body, text="Папка установки", font=self.f_head, anchor="w").pack(fill="x")
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=(4, 12))
        self.dir_label = ctk.CTkEntry(row, height=34, fg_color=FIELD, border_width=1)
        self.dir_label.insert(0, str(self.target))
        self.dir_label.configure(state="readonly")
        self.dir_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Изменить…", width=100, height=34, command=self._pick_dir,
                      **GHOST).pack(side="left", padx=(8, 0))

        for text, var in (("Ярлык на рабочем столе", self.v_desktop),
                          ("Запускать Minideck при входе в Windows", self.v_autostart)):
            ctk.CTkSwitch(self.body, text=text, variable=var, switch_width=38, switch_height=20).pack(anchor="w", pady=3)

        ctk.CTkLabel(self.body, text="Сервер организации", font=self.f_head, anchor="w").pack(fill="x", pady=(14, 0))
        self.code_error = None
        if self.enrolled:
            self._text(f"Этот компьютер уже подключён к серверу {self.enrolled}.", muted=True)
        else:
            ctk.CTkSwitch(self.body, text="Подключить компьютер к серверу организации", variable=self.v_join,
                          command=self._toggle_code, switch_width=38, switch_height=20).pack(anchor="w", pady=(4, 4))
            self.code_box = ctk.CTkTextbox(self.body, height=58, fg_color=FIELD, wrap="char",
                                           font=ctk.CTkFont(family="Consolas", size=12))
            self.code_hint = ctk.CTkLabel(self.body, text="Код регистрации выдал администратор." if self.code_preset
                                          else "Вставьте код регистрации от администратора (minideck://…).",
                                          text_color=MUTED, font=self.f_small, anchor="w")
            self.code_error = ctk.CTkLabel(self.body, text="", text_color=RED, font=self.f_small, anchor="w")
            self.code_box.insert("1.0", self._code_text)
            self._toggle_code()

        self._button("Установить", self._start_install)
        self._button("Назад", self._back_to_welcome, primary=False)

    def _back_to_welcome(self):
        self._save_code()
        self.page_welcome()

    def _save_code(self):
        if getattr(self, "code_box", None) is not None and self.code_box.winfo_exists():
            self._code_text = "".join(self.code_box.get("1.0", "end").split())

    def _toggle_code(self):
        if self.v_join.get():
            self.code_box.pack(fill="x")
            self.code_hint.pack(fill="x")
            self.code_error.pack(fill="x")
        else:
            for widget in (self.code_box, self.code_hint, self.code_error):
                widget.pack_forget()

    def _pick_dir(self):
        folder = filedialog.askdirectory(parent=self.root, initialdir=str(self.target.parent),
                                         title="Папка для Minideck")
        if folder:
            chosen = Path(folder)
            self.target = chosen if chosen.name.lower() == "minideck" else chosen / "Minideck"
            self.dir_label.configure(state="normal")
            self.dir_label.delete(0, "end")
            self.dir_label.insert(0, str(self.target))
            self.dir_label.configure(state="readonly")

    def _start_install(self):
        self._save_code()
        code = None
        if not self.enrolled and self.v_join.get():
            if not parse_enroll_code(self._code_text):
                self.code_error.configure(text="Это не похоже на код регистрации (minideck://…).")
                return
            code = self._code_text
        self.page_progress(code)

    def page_progress(self, code, close_running=False):
        self._clear()
        self._busy = True
        ctk.CTkLabel(self.body, text="Установка…", font=self.f_title, anchor="w").pack(fill="x", pady=(0, 18))
        status = self._text("Подготовка…")
        bar = ctk.CTkProgressBar(self.body, mode="indeterminate", height=6)
        bar.pack(fill="x", pady=12)
        bar.start()

        def report(text):
            self.root.after(0, lambda: status.winfo_exists() and status.configure(text=text))

        def work():
            try:
                if close_running:
                    report("Закрытие запущенного Minideck…")
                    installer.close_running()
                    import time
                    time.sleep(1.5)
                exe = installer.install(self.target, self.v_desktop.get(), self.v_autostart.get(), report)
                if code:
                    installer.write_enrollment(code)
            except installer.FileBusy:
                self.root.after(0, lambda: self.page_busy(code))
            except Exception as exc:  # show the reason instead of crashing the wizard
                self.root.after(0, lambda: self.page_failed(exc))
            else:
                self.root.after(0, lambda: self.page_done(exe, code))

        threading.Thread(target=work, daemon=True).start()

    def page_busy(self, code):
        self._clear()
        self._busy = False
        ctk.CTkLabel(self.body, text="Minideck сейчас запущен", font=self.f_title, anchor="w").pack(fill="x", pady=(0, 12))
        self._text("Чтобы обновить программу, её нужно закрыть. Если к этому компьютеру сейчас кто-то подключён, "
                   "сеанс прервётся.")
        self._button("Закрыть и продолжить", lambda: self.page_progress(code, close_running=True), width=180)
        self._button("Отмена", self._cancel, primary=False)

    def page_failed(self, exc):
        self._clear()
        self._busy = False
        ctk.CTkLabel(self.body, text="Установка не завершена", font=self.f_title, text_color=RED, anchor="w").pack(fill="x", pady=(0, 12))
        self._text(f"Причина: {exc}")
        self._button("Повторить", self.page_options)
        self._button("Закрыть", self._cancel, primary=False)

    def page_done(self, exe, code):
        self._clear()
        self._busy = False
        self.exe = exe
        ctk.CTkLabel(self.body, text="✓  Minideck установлен", font=self.f_title, text_color=GREEN,
                     anchor="w").pack(fill="x", pady=(0, 12))
        self._text(f"Папка: {exe.parent}")
        self._text("Ярлык добавлен в меню «Пуск»" + (" и на рабочий стол." if self.v_desktop.get() else "."))
        if self.v_autostart.get():
            self._text("Программа будет запускаться при входе в Windows.")
        if code:
            self._text("При первом запуске компьютер подключится к серверу организации — программа покажет, "
                       "что это значит.", pady=(8, 0))
        ctk.CTkCheckBox(self.body, text="Запустить Minideck", variable=self.v_launch).pack(anchor="w", pady=(18, 0))
        self._button("Готово", self._finish)

    def _finish(self):
        if self.exe and self.v_launch.get():
            installer.launch(self.exe)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_setup():
    Wizard().run()


def run_uninstall():
    ctk.set_appearance_mode("system")
    root = ctk.CTk(fg_color=PAGE)
    root.title("Удаление Minideck")
    root.resizable(False, False)
    set_window_icon(root)
    remove_data = tk.BooleanVar(value=False)
    ctk.CTkLabel(root, text="Удалить Minideck?", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w", padx=24, pady=(20, 6))
    ctk.CTkLabel(root, justify="left", wraplength=400, text_color=MUTED, text=(
        "Программа будет закрыта, все текущие сеансы прервутся. Ярлыки, автозапуск и запись в списке "
        "программ будут удалены. На сервере организации компьютер останется в списке как «не в сети» — "
        "администратор может удалить его в консоли.")).pack(anchor="w", padx=24)
    ctk.CTkCheckBox(root, text="Также удалить настройки и журнал этого компьютера",
                    variable=remove_data).pack(anchor="w", padx=24, pady=(14, 0))
    status = ctk.CTkLabel(root, text="", text_color=MUTED)
    status.pack(anchor="w", padx=24)
    row = ctk.CTkFrame(root, fg_color="transparent")
    row.pack(fill="x", padx=24, pady=(6, 20))

    def remove():
        for child in row.winfo_children():
            child.configure(state="disabled")
        status.configure(text="Удаление…")
        root.update()
        try:
            installer.uninstall(remove_data.get())
        except Exception as exc:
            status.configure(text=f"Не удалось удалить: {exc}", text_color=RED)
            return
        for child in row.winfo_children():
            child.destroy()
        status.configure(text="Minideck удалён.", text_color=GREEN)
        ctk.CTkButton(row, text="Закрыть", width=120, height=36, command=root.destroy).pack(side="right")

    ctk.CTkButton(row, text="Удалить", width=120, height=36, fg_color=RED, hover_color=RED_HOVER,
                  command=remove).pack(side="right")
    ctk.CTkButton(row, text="Отмена", width=100, height=36, command=root.destroy, **GHOST).pack(side="right", padx=8)
    root.after(150, lambda: (root.lift(), root.focus_force()))
    root.mainloop()
