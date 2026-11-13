"""Journal viewer: period and text filters, per-person filter, CSV export.

Used twice: for the local journal of this computer and for the server-wide journal
in the administrator console.
"""
import time
from tkinter import filedialog

import customtkinter as ctk

from .journal import action_name, export_csv
from .ui import FIELD, GHOST, MUTED, PAGE, RED, raise_window, set_window_icon

PERIODS = {"Сегодня": 0, "7 дней": 7, "30 дней": 30, "Всё время": None}
EVERYONE = "Все участники"


def period_start(name):
    days = PERIODS[name]
    if days is None:
        return 0
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    return int(midnight - days * 86400)


class JournalView(ctk.CTkFrame):
    """fetch(since, done) must call done(rows) on the UI thread; a row is a dict with
    ts, who, action, detail and optionally target (already human-readable)."""

    def __init__(self, master, app, fetch, show_target=False, on_export=None, auto_refresh=0):
        super().__init__(master, fg_color="transparent")
        self.app, self.fetch = app, fetch
        self.show_target, self.on_export, self.auto_refresh = show_target, on_export, auto_refresh
        self.rows, self.visible = [], []
        self._rendered = None

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", pady=(2, 6))
        self.period = ctk.CTkSegmentedButton(top, values=list(PERIODS), height=32, command=lambda _: self.reload())
        self.period.set("7 дней")
        self.period.pack(side="left")
        self.who = ctk.CTkOptionMenu(top, values=[EVERYONE], width=170, height=32, command=lambda _: self.render(),
                                     fg_color=("#e2e6ec", "#3a3c42"), button_color=("#cfd5dd", "#4a4d55"),
                                     button_hover_color=("#bcc4ce", "#5a5d66"), text_color=("#1f2937", "#e5e7eb"))
        self.who.pack(side="left", padx=8)
        self.search = ctk.CTkEntry(top, placeholder_text="Поиск по журналу", height=32, fg_color=FIELD, border_width=1)
        self.search.pack(side="left", fill="x", expand=True)
        self.search.bind("<KeyRelease>", lambda e: self.render())
        ctk.CTkButton(top, text="Экспорт в CSV", width=120, height=32, command=self.export).pack(side="right")
        ctk.CTkButton(top, text="Обновить", width=96, height=32, command=self.reload, **GHOST).pack(side="right", padx=8)

        self.info = ctk.CTkLabel(self, text="", text_color=MUTED, anchor="w", font=app.f_small)
        self.info.pack(side="bottom", fill="x", pady=(4, 0))
        self.text = ctk.CTkTextbox(self, fg_color=FIELD, font=ctk.CTkFont(family="Consolas", size=12), wrap="none")
        self.text.pack(fill="both", expand=True)
        self.text.configure(state="disabled")
        self.reload()
        if auto_refresh:
            self.after(auto_refresh, self._tick)

    def _tick(self):
        if self.winfo_exists():
            self.reload()
            self.after(self.auto_refresh, self._tick)

    def reload(self):
        self.fetch(period_start(self.period.get()), self._loaded)

    def _loaded(self, rows):
        if not self.winfo_exists():
            return
        self.rows = rows
        people = sorted({r["who"] for r in rows if r.get("who") and r["who"] != "-"}, key=str.lower)
        current = self.who.get()
        self.who.configure(values=[EVERYONE] + people)
        if current != EVERYONE and current not in people:
            self.who.set(EVERYONE)
        self.render()

    def _line(self, row):
        when = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(row["ts"]))
        width = 14 if self.show_target else 32
        parts = [when, f"{row.get('who') or '-':<{width}}", f"{action_name(row['action']):<38}"]
        if self.show_target:
            parts.append(f"{row.get('target') or '':<30}")
        parts.append(row.get("detail") or "")
        return "  ".join(parts).rstrip()

    def render(self):
        who, query = self.who.get(), self.search.get().strip().lower()
        self.visible, lines = [], []
        for row in self.rows:
            if who != EVERYONE and row.get("who") != who:
                continue
            line = self._line(row)
            if query and query not in line.lower():
                continue
            self.visible.append(row)
            lines.append(line)
        content = "\n".join(lines) or "За выбранный период записей нет."
        if content != self._rendered:   # untouched text keeps its scroll position on auto-refresh
            self._rendered = content
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content)
            self.text.configure(state="disabled")
        self.info.configure(text=f"Показано записей: {len(self.visible)} из {len(self.rows)}", text_color=MUTED)

    def export(self):
        if not self.visible:
            self.info.configure(text="Нечего выгружать: нет записей по выбранным условиям.", text_color=RED)
            return
        name = "minideck-journal-" + time.strftime("%Y-%m-%d") + ".csv"
        path = filedialog.asksaveasfilename(parent=self.winfo_toplevel(), title="Сохранить журнал",
                                            defaultextension=".csv", initialfile=name,
                                            filetypes=[("CSV (Excel)", "*.csv")])
        if not path:
            return
        columns = ["Дата", "Время", "Кто", "Действие"] + (["Компьютер"] if self.show_target else []) + ["Подробности"]
        rows = []
        for row in self.visible:
            moment = time.localtime(row["ts"])
            record = [time.strftime("%d.%m.%Y", moment), time.strftime("%H:%M:%S", moment),
                      row.get("who") or "", action_name(row["action"])]
            if self.show_target:
                record.append(row.get("target") or "")
            rows.append(record + [row.get("detail") or ""])
        try:
            export_csv(path, rows, columns)
        except OSError as exc:
            self.info.configure(text=f"Не удалось сохранить файл: {exc}", text_color=RED)
            return
        self.info.configure(text=f"Выгружено записей: {len(rows)} → {path}", text_color=MUTED)
        if self.on_export:
            self.on_export(len(rows))


class LocalJournalWindow(ctk.CTkToplevel):
    """What happened on this computer and from it. Open to anyone sitting at it."""

    def __init__(self, app):
        super().__init__(app.root, fg_color=PAGE)
        self.title("Minideck — журнал этого компьютера")
        self.geometry("980x560")
        self.minsize(760, 400)
        set_window_icon(self)
        ctk.CTkLabel(self, text="Журнал этого компьютера", font=app.f_title).pack(anchor="w", padx=18, pady=(14, 0))
        ctk.CTkLabel(self, text="Кто и когда подключался, что передавалось, что менялось в настройках доступа. "
                                "Содержимое экрана, нажатия клавиш и содержимое буфера не записываются.",
                     text_color=MUTED, font=app.f_small, wraplength=900, justify="left").pack(anchor="w", padx=18, pady=(0, 8))
        fetch = lambda since, done: done([r for r in app.journal.read(5000) if r["ts"] >= since])
        JournalView(self, app, fetch, auto_refresh=4000).pack(fill="both", expand=True, padx=18, pady=(0, 12))
        raise_window(self)
