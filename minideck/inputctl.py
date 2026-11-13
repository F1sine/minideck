"""Injects remote mouse and keyboard events into the local machine."""
import sys
import threading

# Tk keysym -> pynput Key attribute name
SPECIAL = {
    "Return": "enter", "KP_Enter": "enter", "Escape": "esc", "BackSpace": "backspace",
    "Tab": "tab", "ISO_Left_Tab": "tab", "space": "space", "Delete": "delete",
    "Insert": "insert", "Home": "home", "End": "end", "Prior": "page_up",
    "Next": "page_down", "Left": "left", "Right": "right", "Up": "up", "Down": "down",
    "Shift_L": "shift_l", "Shift_R": "shift_r", "Control_L": "ctrl_l",
    "Control_R": "ctrl_r", "Alt_L": "alt_l", "Alt_R": "alt_r",
    "Win_L": "cmd", "Win_R": "cmd_r", "Super_L": "cmd", "Super_R": "cmd_r",
    "Meta_L": "cmd", "Meta_R": "cmd_r", "Caps_Lock": "caps_lock",
    "Num_Lock": "num_lock", "Scroll_Lock": "scroll_lock", "Pause": "pause",
    "Print": "print_screen", "App": "menu", "Menu": "menu",
}
SPECIAL.update({f"F{i}": f"f{i}" for i in range(1, 21)})


class InputInjector:
    def __init__(self):
        from pynput import keyboard, mouse  # imported lazily: needs a display
        self._kb_mod, self._mouse_mod = keyboard, mouse
        self._kb = keyboard.Controller()
        self._mouse = mouse.Controller()
        self._buttons = {1: mouse.Button.left, 2: mouse.Button.middle, 3: mouse.Button.right}
        self._down_keys = set()
        self._down_buttons = set()
        self._lock = threading.Lock()

    def _resolve(self, ev):
        name = SPECIAL.get(ev.get("sym") or "")
        if name:
            key = getattr(self._kb_mod.Key, name, None)
            if key is not None:
                return key
        vk = ev.get("vk")
        if vk and sys.platform == "win32":  # layout-independent, keeps hotkeys working
            return self._kb_mod.KeyCode.from_vk(int(vk))
        ch = ev.get("ch") or ""
        if len(ch) == 1 and ch.isprintable():
            return self._kb_mod.KeyCode.from_char(ch)
        sym = ev.get("sym") or ""
        if len(sym) == 1:
            return self._kb_mod.KeyCode.from_char(sym)
        return None

    def handle(self, ev, geom):
        """Apply one input event. geom = (left, top, width, height) of the shared screen."""
        kind = ev.get("t")
        with self._lock:
            try:
                if kind == "key":
                    key = self._resolve(ev)
                    if key is None:
                        return
                    if ev.get("down"):
                        self._kb.press(key)
                        self._down_keys.add(key)
                    else:
                        self._kb.release(key)
                        self._down_keys.discard(key)
                    return
                if "x" in ev:
                    left, top, width, height = geom
                    fx = min(1.0, max(0.0, float(ev["x"])))
                    fy = min(1.0, max(0.0, float(ev["y"])))
                    self._mouse.position = (left + int(fx * (width - 1)),
                                            top + int(fy * (height - 1)))
                if kind == "btn":
                    button = self._buttons.get(ev.get("b"))
                    if button is None:
                        return
                    if ev.get("down"):
                        self._mouse.press(button)
                        self._down_buttons.add(button)
                    else:
                        self._mouse.release(button)
                        self._down_buttons.discard(button)
                elif kind == "wheel":
                    self._mouse.scroll(int(ev.get("dx", 0)), int(ev.get("dy", 0)))
            except Exception:
                pass  # a single bad event must not kill the session

    def release_all(self):
        """Let go of anything still held, so a dropped session leaves no stuck keys."""
        with self._lock:
            for key in list(self._down_keys):
                try:
                    self._kb.release(key)
                except Exception:
                    pass
            for button in list(self._down_buttons):
                try:
                    self._mouse.release(button)
                except Exception:
                    pass
            self._down_keys.clear()
            self._down_buttons.clear()
