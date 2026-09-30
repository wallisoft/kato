#!/usr/bin/env python3
"""
AirShare (working title) - one keyboard and mouse across your machines.

Arrange your machines in a 3x3 grid; push the pointer off any edge that has a
neighbour and it carries on onto that machine, keyboard and scrolling
included. Push it back and it comes home. The layout is shared by the whole
group: change it on any machine and every machine follows. No master.

X11 and Windows for now; Wayland comes later. Discovery, trust and TLS come
from clickboard_core (passphrase mode for now).

  Emergency: Scroll Lock, or Esc three times quickly, brings the pointer home.
"""
import argparse
import json
import platform
import queue
import secrets
import socket
import sys
import threading
import time

from pynput import keyboard, mouse
from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
                               QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

from clickboard_core import MIN_PASSPHRASE, ClipboardSync, Config, Mesh, log, set_app
from clickboard_core import common as C

VERSION = "0.5.0"
PORT, BEACON_PORT = 47810, 47812      # separate from Clickboard, so both can run
CONTROL_PORT = 47811                  # localhost only: a second launch talks to the running copy
FLUSH_EVERY = 0.008                   # send gathered mouse movement ~120 times a second
BACK_OFF = 24                         # pixels inside the edge when the pointer comes home
RETURN_KEY = keyboard.Key.scroll_lock
ESCAPE_PRESSES, ESCAPE_WINDOW = 3, 1.2
ALIVE_EVERY, ALIVE_TIMEOUT = 1.0, 3.0
LAYOUT_EVERY = 5                      # re-share layout and screen size so late joiners pick them up
GRID = 3
OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}
STEP = {"left": (0, -1), "right": (0, 1), "up": (-1, 0), "down": (1, 0)}

GREEN, BLUE, ORANGE = QColor(34, 197, 94), QColor(59, 130, 246), QColor(249, 115, 22)

WORDS = ("amber", "anchor", "apple", "badger", "banjo", "beacon", "bramble", "cactus", "castle", "cobalt",
         "comet", "copper", "dingo", "ember", "falcon", "fern", "garnet", "harbour", "hazel", "island",
         "jigsaw", "kettle", "lantern", "lemon", "maple", "marble", "meadow", "nutmeg", "orbit", "otter",
         "pebble", "pepper", "piano", "quartz", "raven", "ribbon", "saffron", "tango", "thistle", "tulip",
         "velvet", "walnut", "willow", "yonder", "zephyr", "biscuit", "puffin", "saucer")


def screen_bounds():
    """(x, y, width, height) of the whole desktop, across all monitors."""
    if platform.system() == "Windows":
        import ctypes
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    # X11: ask the X server for the whole desktop (all monitors), in real pixels.
    from Xlib import display
    d = display.Display()
    scr = d.screen()
    w, h = scr.width_in_pixels, scr.height_in_pixels
    d.close()
    return (0, 0, w, h)


class CursorHider:
    """Hides this machine's pointer while it's driving another one (X11 via XFixes)."""

    def __init__(self):
        self.d = None
        self._lock = threading.Lock()
        if platform.system() != "Linux":
            return
        try:
            from Xlib import display
            self.d = display.Display()
            self.root = self.d.screen().root
            self.d.xfixes_query_version()
        except Exception as exc:
            log(f"can't hide the pointer here: {exc}")
            self.d = None

    def hide(self):
        with self._lock:
            if self.d:
                try:
                    self.root.xfixes_hide_cursor()
                    self.d.sync()
                except Exception:
                    pass

    def show(self):
        with self._lock:
            if self.d:
                try:
                    self.root.xfixes_show_cursor()
                    self.d.sync()
                except Exception:
                    pass


def input_blocked() -> bool:
    """True when this machine can't take injected input: Windows lock screen, UAC or login screen."""
    if platform.system() != "Windows":
        return False
    import ctypes
    u = ctypes.windll.user32
    h = u.OpenInputDesktop(0, False, 0x0100)       # DESKTOP_SWITCHDESKTOP
    if not h:
        return True                                 # the secure desktop is showing
    u.CloseDesktop(h)
    return False


def ensure_pointer_visible() -> bool:
    """Windows hides the pointer when it thinks no mouse is in use, and injected movement doesn't
    count. Switching on Mouse Keys makes Windows show it. Returns True if it changed anything."""
    if platform.system() != "Windows":
        return False
    import ctypes

    class MOUSEKEYS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint) for n in ("cbSize", "dwFlags", "iMaxSpeed", "iTimeToMaxSpeed",
                                                 "iCtrlSpeed", "dwReserved1", "dwReserved2")]
    mk = MOUSEKEYS()
    mk.cbSize = ctypes.sizeof(mk)
    spi = ctypes.windll.user32.SystemParametersInfoW
    if not spi(0x36, mk.cbSize, ctypes.byref(mk), 0):        # SPI_GETMOUSEKEYS
        return False
    if mk.dwFlags & 0x1:
        return False                                          # already on
    mk.dwFlags |= 0x3                                         # available + on
    return bool(spi(0x37, mk.cbSize, ctypes.byref(mk), 3))    # SPI_SETMOUSEKEYS, save + broadcast


LINUX = platform.system() == "Linux"
WINDOWS = platform.system() == "Windows"


class XGrabber:
    """Linux/X11: AirShare's own explicit hold on the keyboard and pointer, on a dedicated
    X connection, with an explicit release. (Creating and destroying capturing listeners
    for every crossing could leave the keyboard held; this can't.)"""

    def __init__(self):
        from Xlib import X, display
        self.X = X
        self.d = display.Display()
        self.root = self.d.screen().root
        self.lock = threading.Lock()
        self.grabbed = False
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        # Events delivered to our grab are never used; read and drop them so nothing backs up.
        while True:
            try:
                self.d.next_event()
            except Exception:
                time.sleep(0.1)

    def grab(self) -> bool:
        X = self.X
        with self.lock:
            p = self.root.grab_pointer(True, X.ButtonPressMask | X.ButtonReleaseMask | X.PointerMotionMask,
                                       X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE, X.CurrentTime)
            k = self.root.grab_keyboard(True, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
            self.d.sync()
            self.grabbed = True
            if p != X.GrabSuccess or k != X.GrabSuccess:
                self._release_locked()
                return False
            return True

    def release(self):
        with self.lock:
            self._release_locked()

    def _release_locked(self):
        X = self.X
        try:
            self.d.ungrab_pointer(X.CurrentTime)
            self.d.ungrab_keyboard(X.CurrentTime)
            self.d.sync()
        except Exception as exc:
            log(f"release failed: {exc}")
        self.grabbed = False


_WIN_INPUT = None


def win_move_abs(x, y):
    """Windows: move the pointer the way a real mouse does (SendInput), so Windows treats it
    as mouse activity and shows the pointer even on a machine with no mouse attached."""
    global _WIN_INPUT
    import ctypes
    if _WIN_INPUT is None:
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("mi", MOUSEINPUT)]
        _WIN_INPUT = INPUT
    u = ctypes.windll.user32
    vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    inp = _WIN_INPUT()
    inp.type = 0                                            # INPUT_MOUSE
    inp.mi.dx = int((x - vx) * 65535 / max(vw - 1, 1))
    inp.mi.dy = int((y - vy) * 65535 / max(vh - 1, 1))
    inp.mi.dwFlags = 0x0001 | 0x8000 | 0x4000               # MOVE | ABSOLUTE | VIRTUALDESK
    u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_WIN_INPUT))


def key_to_wire(key):
    if isinstance(key, keyboard.Key):
        return {"name": key.name}
    char, vk = getattr(key, "char", None), getattr(key, "vk", None)
    if char and ord(char[0]) < 32 and vk is not None and (48 <= vk <= 57 or 65 <= vk <= 90):
        char = chr(vk).lower()          # Windows reports Ctrl+C as '\x03'; send the letter
    if char:
        return {"char": char}
    if vk is not None:
        return {"vk": vk}
    return None


def wire_to_key(d):
    if "name" in d:
        return getattr(keyboard.Key, d["name"], None)
    if "char" in d:
        return d["char"]
    if "vk" in d:
        return keyboard.KeyCode.from_vk(d["vk"])
    return None


class Bridge(QObject):
    """Engine threads talk to the Qt UI only through these signals."""
    refresh = Signal()
    notify = Signal(str)
    open_settings = Signal()


class AirShare(ClipboardSync, Mesh):
    """One MAIN computer captures its keyboard and mouse and drives the others.

    The main computer keeps a virtual pointer for whichever screen is active and
    decides every crossing itself, sending absolute positions. The other computers
    install no input hooks at all: they report their screen size, and move, click
    and type as told. (The same shape Synergy uses, for the same reasons.)

    Clipboard, images and files are shared between all machines too, via
    ClipboardSync from clickboard_core.
    """

    def __init__(self, layout: dict, bridge: Bridge):
        Mesh.__init__(self, Config.load(), service="airshare")
        self.init_clipboard()
        self.cfg.paused = False
        self.layout = layout            # {"cells": {name: [r, c]}, "master": name, "version": t}
        self.bridge = bridge
        self.bounds = screen_bounds()
        self.mouse = mouse.Controller()
        self.kb = keyboard.Controller()
        self.cursor = CursorHider()
        self.paused = False
        self.sizes = {}                 # machine name -> (w, h), as reported by each machine
        # main-computer state
        self.active = None              # name of the machine being driven, or None when at home
        self.vpos = [0, 0]              # virtual pointer on the active machine's screen
        self._home_side = "right"       # which of our edges we left from
        self._centre = (0, 0)
        self._acc = [0, 0]
        self._acc_lock = threading.Lock()
        self._switch_lock = threading.Lock()
        self._sent_down = {}
        self._grabs, self._watchers = [], []
        self._esc_times = []
        self._last_alive = 0.0
        self._busy = False              # a crossing is being set up
        self._input_on = False
        # driven-machine state
        self.driven_by = None           # link to the main computer while it's driving us
        self._held_keys = {}
        self._outq = queue.Queue()      # input callbacks never touch the network themselves
        threading.Thread(target=self._send_loop, daemon=True).start()
        self.xgrab = XGrabber() if LINUX else None
        if LINUX:
            threading.Thread(target=self._self_heal, daemon=True).start()

    # ---- plumbing ----
    def _send(self, link, header):
        self._outq.put((link, header))

    def _send_loop(self):
        while True:
            link, header = self._outq.get()
            if link.alive:
                link.send(header)

    def notify(self, msg):
        log(msg)
        self.bridge.notify.emit(msg)

    def refresh_ui(self):
        self.bridge.refresh.emit()

    def _self_heal(self):
        while True:
            time.sleep(0.5)
            if self.xgrab and self.xgrab.grabbed and not self.active:
                log("held input while not driving: released it")
                self.xgrab.release()

    # ---- layout and roles ----
    def cells(self):
        return self.layout.get("cells", {})

    def master(self):
        return self.layout.get("master")

    def is_master(self):
        return self.master() == self.cfg.name

    def neighbour_of(self, name, side):
        pos = self.cells().get(name)
        if not pos:
            return None
        dr, dc = STEP[side]
        want = [pos[0] + dr, pos[1] + dc]
        return next((n for n, p in self.cells().items() if list(p) == want), None)

    def neighbour(self, side):
        return self.neighbour_of(self.cfg.name, side)

    def link_to(self, name):
        for link in list(self.links.values()):
            if link.alive and self.peers.get(link.peer_id, {}).get("name", "").lower() == (name or "").lower():
                return link
        return None

    def link_for(self, side):
        return self.link_to(self.neighbour(side))

    def set_layout(self, cells, master, share=True):
        self.layout = {"cells": cells, "master": master, "version": time.time()}
        self._save_layout()
        self.apply_role()
        if share:
            self.share_state()
        self.refresh_ui()

    def _save_layout(self):
        (C.CONFIG_DIR / "layout.json").write_text(json.dumps(self.layout, indent=2))

    def share_state(self):
        bx, by, bw, bh = self.bounds
        for link in list(self.links.values()):
            if link.alive:
                self._send(link, {"type": "screen", "name": self.cfg.name, "w": bw, "h": bh})
                if self.cells():
                    self._send(link, {"type": "layout", "layout": self.layout})

    def layout_loop(self):
        while self.running:
            self.share_state()
            time.sleep(LAYOUT_EVERY)

    def apply_role(self):
        """Only the main computer listens to input. The others install no hooks at all."""
        if self.is_master() and not self._input_on:
            self._start_input()
        elif not self.is_master() and self._input_on:
            self.go_home()
            for w in self._watchers:
                w.stop()
            self._watchers, self._input_on = [], False
            log("not the main computer: input listening off")

    # ---- main computer: listening ----
    def _start_input(self):
        def on_move(x, y):
            if self.active:
                self._g_move(x, y)
            else:
                self._check_edges(x, y)

        def on_click(x, y, button, pressed):
            if self.active and LINUX:
                self._g_click(x, y, button, pressed)

        def on_scroll(x, y, dx, dy):
            if self.active and LINUX:
                self._g_scroll(x, y, dx, dy)
        self._watchers = [mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll)]
        if LINUX:
            self._watchers.append(keyboard.Listener(
                on_press=lambda k: self._g_press(k) if self.active else None,
                on_release=lambda k: self._g_release(k) if self.active else None))
        for w in self._watchers:
            w.start()
        self._input_on = True
        log("main computer: listening for the screen edges")

    def _check_edges(self, x, y):
        if self.paused or self._busy:
            return
        bx, by, bw, bh = self.bounds
        if x >= bx + bw - 1:
            side, ratio = "right", (y - by) / max(bh, 1)
        elif x <= bx:
            side, ratio = "left", (y - by) / max(bh, 1)
        elif y <= by:
            side, ratio = "up", (x - bx) / max(bw, 1)
        elif y >= by + bh - 1:
            side, ratio = "down", (x - bx) / max(bw, 1)
        else:
            return
        target = self.neighbour(side)
        if target and self.link_to(target) and target in self.sizes:
            self._busy = True           # hooks must return at once: do the work elsewhere
            threading.Thread(target=self._go_safely, args=(target, OPPOSITE[side], ratio, side), daemon=True).start()

    # ---- main computer: moving between screens ----
    def _go_safely(self, target, entry_side, ratio, home_side=None):
        try:
            self.go_to(target, entry_side, ratio, home_side)
        except Exception as exc:
            log(f"couldn't cross over: {exc}")
            self.go_home()
        finally:
            self._busy = False

    def go_to(self, target, entry_side, ratio, home_side=None):
        """Make `target` the active screen, arriving on its `entry_side` edge at `ratio` along it."""
        with self._switch_lock:
            link = self.link_to(target)
            if not link or target not in self.sizes:
                return
            if self.active is None:                           # leaving home: take the input
                bx, by, bw, bh = self.bounds
                self._home_side = home_side or "right"
                self._centre = (bx + bw // 2, by + bh // 2)
                self.mouse.position = self._centre
                if LINUX:
                    if not self.xgrab.grab():
                        log("couldn't take the keyboard and pointer (another app has them): not crossing")
                        return
                else:
                    self._grabs = [
                        mouse.Listener(on_move=self._g_move, on_click=self._g_click, on_scroll=self._g_scroll, suppress=True),
                        keyboard.Listener(on_press=self._g_press, on_release=self._g_release, suppress=True),
                    ]
                    for g in self._grabs:
                        g.start()
                self.cursor.hide()
            else:                                             # hopping between two other machines
                self._exit_active()
            w, h = self.sizes[target]
            ratio = min(max(float(ratio), 0.0), 1.0)
            self.vpos = {"left": [1, ratio * h], "right": [w - 2, ratio * h],
                         "up": [ratio * w, 1], "down": [ratio * w, h - 2]}[entry_side]
            self.active = target
            self._last_alive = time.time()
            link.send({"type": "kvm_enter"})
            link.send({"type": "kvm_pos", "x": int(self.vpos[0]), "y": int(self.vpos[1])})
            if not getattr(self, "_flushing", False):
                threading.Thread(target=self._flush_loop, daemon=True).start()
            log(f"now controlling {target}")
        self.refresh_ui()

    def _exit_active(self):
        link = self.link_to(self.active)
        if link:
            for k in list(self._sent_down.values()):
                self._send(link, {"type": "kvm_key", "key": k, "down": False})
            self._send(link, {"type": "kvm_exit"})
        self._sent_down.clear()

    def go_home(self, ratio=None):
        with self._switch_lock:
            if not self.active:
                return
            self._exit_active()
            self.active = None
            if self.xgrab:
                self.xgrab.release()
            for g in self._grabs:
                g.stop()
            self._grabs = []
            time.sleep(0.05)
            bx, by, bw, bh = self.bounds
            r = 0.5 if ratio is None else min(max(float(ratio), 0.0), 1.0)
            side = self._home_side
            self.mouse.position = {"right": (bx + bw - 1 - BACK_OFF, by + int(r * bh)),
                                   "left": (bx + BACK_OFF, by + int(r * bh)),
                                   "up": (bx + int(r * bw), by + BACK_OFF),
                                   "down": (bx + int(r * bw), by + bh - 1 - BACK_OFF)}[side]
            self.cursor.show()
            log("back home")
        self.refresh_ui()

    def _home_safely(self, ratio=None):
        try:
            self.go_home(ratio)
        except Exception as exc:
            log(f"problem coming home: {exc}")

    # ---- main computer: while driving another machine ----
    def _g_move(self, x, y):
        try:
            if not self.active:
                return True
            cx, cy = self._centre
            dx, dy = x - cx, y - cy
            if dx or dy:
                with self._acc_lock:
                    self._acc[0] += dx
                    self._acc[1] += dy
                if not WINDOWS:
                    self.mouse.position = self._centre       # X11: keep the real pointer parked
        except Exception as exc:
            log(f"pointer handling failed, coming home to be safe: {exc}")
            threading.Thread(target=self._home_safely, daemon=True).start()
        return True

    def _flush_loop(self):
        self._flushing = True
        try:
            while self.active:
                if time.time() - self._last_alive > ALIVE_TIMEOUT:
                    log(f"{self.active} went quiet (asleep, locked or gone): coming home")
                    threading.Thread(target=self._home_safely, daemon=True).start()
                    return
                if any(not g.running for g in self._grabs + self._watchers):
                    log("input listening stopped unexpectedly: coming home")
                    threading.Thread(target=self._home_safely, daemon=True).start()
                    return
                if input_blocked():
                    log("secure screen on this machine: coming home")
                    threading.Thread(target=self._home_safely, daemon=True).start()
                    return
                with self._acc_lock:
                    dx, dy = self._acc
                    self._acc = [0, 0]
                if dx or dy:
                    self._move_virtual(dx, dy)
                time.sleep(FLUSH_EVERY)
        finally:
            self._flushing = False

    def _move_virtual(self, dx, dy):
        name = self.active
        if not name or name not in self.sizes:
            return
        w, h = self.sizes[name]
        x, y = self.vpos[0] + dx, self.vpos[1] + dy
        side = "left" if x < 0 else "right" if x >= w else "up" if y < 0 else "down" if y >= h else None
        if side:
            ratio = (y / h) if side in ("left", "right") else (x / w)
            nxt = self.neighbour_of(name, side)
            if nxt == self.cfg.name:
                self.go_home(ratio)
                return
            if nxt and self.link_to(nxt) and nxt in self.sizes:
                self.go_to(nxt, OPPOSITE[side], ratio)
                return
            x, y = min(max(x, 0), w - 1), min(max(y, 0), h - 1)      # a wall: stay on this screen
        self.vpos = [x, y]
        link = self.link_to(name)
        if link:
            self._send(link, {"type": "kvm_pos", "x": int(x), "y": int(y)})

    def _g_click(self, x, y, button, pressed):
        try:
            link = self.link_to(self.active)
            if link:
                self._send(link, {"type": "kvm_btn", "button": button.name, "down": pressed})
        except Exception as exc:
            log(f"click handling failed: {exc}")

    def _g_scroll(self, x, y, dx, dy):
        try:
            link = self.link_to(self.active)
            if link:
                self._send(link, {"type": "kvm_scroll", "dx": dx, "dy": dy})
        except Exception as exc:
            log(f"scroll handling failed: {exc}")

    def _g_press(self, key):
        try:
            if key == RETURN_KEY:
                threading.Thread(target=self._home_safely, daemon=True).start()
                return
            if key == keyboard.Key.esc:
                now = time.time()
                self._esc_times = [t for t in self._esc_times if now - t < ESCAPE_WINDOW] + [now]
                if len(self._esc_times) >= ESCAPE_PRESSES:
                    self._esc_times = []
                    threading.Thread(target=self._home_safely, daemon=True).start()
                    return
            wire = key_to_wire(key)
            link = self.link_to(self.active)
            if wire and link:
                self._sent_down[json.dumps(wire, sort_keys=True)] = wire
                self._send(link, {"type": "kvm_key", "key": wire, "down": True})
        except Exception as exc:
            log(f"key handling failed, coming home to be safe: {exc}")
            threading.Thread(target=self._home_safely, daemon=True).start()

    def _g_release(self, key):
        try:
            wire = key_to_wire(key)
            link = self.link_to(self.active)
            if wire and link:
                self._sent_down.pop(json.dumps(wire, sort_keys=True), None)
                self._send(link, {"type": "kvm_key", "key": wire, "down": False})
        except Exception as exc:
            log(f"key handling failed, coming home to be safe: {exc}")
            threading.Thread(target=self._home_safely, daemon=True).start()

    # ---- frames from other machines ----
    def on_frame(self, link, header, payload):
        t = header.get("type")
        if t in ClipboardSync.CLIP_FRAMES:
            ClipboardSync.on_frame(self, link, header, payload)
            return
        bx, by, bw, bh = self.bounds
        if t == "screen":
            name = self.peers.get(link.peer_id, {}).get("name") or str(header.get("name", ""))
            if name:
                self.sizes[name] = (int(header.get("w", 1920)), int(header.get("h", 1080)))
        elif t == "layout":
            theirs = header.get("layout") or {}
            if float(theirs.get("version", 0)) > float(self.layout.get("version", 0)):
                self.layout = {"cells": dict(theirs.get("cells", {})), "master": theirs.get("master"),
                               "version": float(theirs["version"])}
                self._save_layout()
                log(f"layout updated by another machine (main computer: {self.master()})")
                self.apply_role()
                self.refresh_ui()
        elif t == "kvm_alive":
            if self.active and self.link_to(self.active) is link:
                self._last_alive = time.time()
        elif t == "kvm_leave":                                # a driven machine asks us to stop (locked, paused)
            if self.active and self.link_to(self.active) is link:
                threading.Thread(target=self._home_safely, daemon=True).start()
        # ---- being driven ----
        elif t == "kvm_enter":
            if input_blocked() or self.paused:
                link.send({"type": "kvm_leave"})
                log("refused control: this machine is " + ("paused" if self.paused else "locked"))
                return
            if ensure_pointer_visible():
                self.notify("Turned on Windows Mouse Keys so the pointer stays visible when another machine drives this one.")
            self.driven_by = link
            threading.Thread(target=self._alive_loop, args=(link,), daemon=True).start()
            self.refresh_ui()
        elif t == "kvm_exit":
            if self.driven_by is link:
                self._release_all()
                self.driven_by = None
                self.refresh_ui()
        elif t == "kvm_pos" and self.driven_by is link:
            x, y = bx + int(header.get("x", 0)), by + int(header.get("y", 0))
            if WINDOWS:
                try:
                    win_move_abs(x, y)
                    return
                except Exception as exc:
                    log(f"real-mouse move failed, placing the pointer instead: {exc}")
            self.mouse.position = (x, y)
        elif t == "kvm_btn" and self.driven_by is link:
            btn = getattr(mouse.Button, str(header.get("button")), None)
            if btn:
                (self.mouse.press if header.get("down") else self.mouse.release)(btn)
        elif t == "kvm_scroll" and self.driven_by is link:
            self.mouse.scroll(int(header.get("dx", 0)), int(header.get("dy", 0)))
        elif t == "kvm_key" and self.driven_by is link:
            k = wire_to_key(header.get("key") or {})
            if k is None:
                return
            ident = json.dumps(header.get("key"), sort_keys=True)
            try:
                if header.get("down"):
                    self.kb.press(k)
                    self._held_keys[ident] = k
                else:
                    self.kb.release(k)
                    self._held_keys.pop(ident, None)
            except Exception as exc:
                log(f"couldn't type {header.get('key')}: {exc}")

    def _alive_loop(self, link):
        while self.driven_by is link and link.alive:
            if input_blocked():
                self._release_all()
                self.driven_by = None
                link.send({"type": "kvm_leave"})
                log("locked: handed control back")
                self.refresh_ui()
                return
            link.send({"type": "kvm_alive"})
            time.sleep(ALIVE_EVERY)

    def _release_all(self):
        for k in list(self._held_keys.values()):
            try:
                self.kb.release(k)
            except Exception:
                pass
        self._held_keys.clear()

    def on_link_closed(self, link, why):
        if self.active and self.link_to(self.active) in (None, link):
            threading.Thread(target=self._home_safely, daemon=True).start()
        if self.driven_by is link:
            self._release_all()
            self.driven_by = None
        Mesh.on_link_closed(self, link, why)

    # ---- status ----
    def status_line(self):
        if self.status:
            return self.status
        if self.paused:
            return "Paused"
        if not self.master():
            return "Choose the main computer in Layout"
        if self.active:
            return f"Controlling {self.active}"
        if self.driven_by:
            return f"Controlled by {self.master()}"
        n = self.connected_count()
        if not n:
            return "Looking for your other machines..."
        role = "Main computer" if self.is_master() else f"Main computer: {self.master()}"
        return f"{role} - {n} machine{'s' if n != 1 else ''} connected"

    def colour(self):
        if self.paused or not self.connected_count():
            return ORANGE
        return BLUE if (self.active or self.driven_by) else GREEN

    def toggle_pause(self):
        self.paused = not self.paused
        self.cfg.paused = self.paused              # ClipboardSync honours this too
        if self.paused:
            threading.Thread(target=self._home_safely, daemon=True).start()
        log("sharing paused" if self.paused else "sharing resumed")
        self.refresh_ui()

    def leave_remote(self):
        """Used by the tray's Quit: make sure we're home and nothing is held."""
        self._home_safely()

    def control_loop(self):
        try:
            cs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            cs.bind(("127.0.0.1", CONTROL_PORT))
            cs.listen(4)
        except OSError:
            return
        while self.running:
            try:
                conn, _ = cs.accept()
            except OSError:
                continue
            with conn:
                try:
                    cmd = conn.recv(64).decode(errors="ignore").strip()
                    conn.sendall(b"ok\n")
                except OSError:
                    continue
            if cmd == "settings":
                self.bridge.open_settings.emit()
            elif cmd == "quit":
                QTimer.singleShot(0, QApplication.instance().quit)


# ============================== Qt UI ==============================

def dot_icon(colour: QColor) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(255, 255, 255, 200), 3))
    p.setBrush(colour)
    p.drawEllipse(8, 8, 48, 48)
    p.end()
    return QIcon(pm)


class GridWidget(QWidget):
    """A 3x3 grid of cells; machines are tiles you drag into place. Unplaced ones wait below."""
    CELL_W, CELL_H, GAP = 132, 84, 10

    def __init__(self, me: str):
        super().__init__()
        self.me = me
        self.cells = {}           # name -> [row, col]
        self.pool = []            # names not placed yet
        self.drag = None          # (name, offset QPoint)
        self.drag_pos = QPoint()
        self.connected = set()
        w = GRID * self.CELL_W + (GRID + 1) * self.GAP
        self.setMinimumSize(w, GRID * self.CELL_H + (GRID + 1) * self.GAP + self.CELL_H + 40)
        self.setMouseTracking(True)

    def set_machines(self, names, cells, connected):
        self.cells = {n: list(p) for n, p in cells.items()}
        self.pool = [n for n in names if n not in self.cells]
        self.connected = set(connected)
        self.update()

    # geometry
    def cell_rect(self, r, c):
        return QRect(self.GAP + c * (self.CELL_W + self.GAP), self.GAP + r * (self.CELL_H + self.GAP),
                     self.CELL_W, self.CELL_H)

    def pool_rect(self, i):
        top = self.GAP + GRID * (self.CELL_H + self.GAP) + 30
        return QRect(self.GAP + i * (self.CELL_W + self.GAP), top, self.CELL_W, self.CELL_H)

    def tile_rect(self, name):
        if name in self.cells:
            return self.cell_rect(*self.cells[name])
        if name in self.pool:
            return self.pool_rect(self.pool.index(name))
        return None

    def cell_at(self, pt):
        for r in range(GRID):
            for c in range(GRID):
                if self.cell_rect(r, c).contains(pt):
                    return [r, c]
        return None

    # painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        base, text = pal.window().color(), pal.windowText().color()
        dark = base.lightness() < 128
        empty = QColor(255, 255, 255, 18) if dark else QColor(0, 0, 0, 12)
        for r in range(GRID):
            for c in range(GRID):
                p.setPen(QPen(QColor(128, 128, 128, 90), 1, Qt.DashLine))
                p.setBrush(empty)
                p.drawRoundedRect(self.cell_rect(r, c), 10, 10)
        p.setPen(text)
        p.drawText(QRect(self.GAP, self.GAP + GRID * (self.CELL_H + self.GAP), 400, 26),
                   Qt.AlignVCenter, "Not placed yet (drag into the grid):" if self.pool else "")
        for name in list(self.cells) + self.pool:
            if self.drag and self.drag[0] == name:
                continue
            self.paint_tile(p, self.tile_rect(name), name, text)
        if self.drag:
            r = QRect(self.drag_pos - self.drag[1], self.cell_rect(0, 0).size())
            p.setOpacity(0.85)
            self.paint_tile(p, r, self.drag[0], text)
        p.end()

    def paint_tile(self, p, rect, name, text):
        mine = name == self.me
        fill = QColor(59, 130, 246) if mine else QColor(34, 197, 94) if name in self.connected else QColor(120, 120, 120)
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(rect.adjusted(4, 4, -4, -4), 10, 10)
        p.setPen(Qt.white)
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.drawText(rect.adjusted(6, 6, -6, -24), Qt.AlignCenter | Qt.TextWordWrap, name)
        f.setBold(False)
        f.setPointSizeF(max(f.pointSizeF() - 1.5, 7))
        p.setFont(f)
        label = "this machine" if mine else ("connected" if name in self.connected else "not connected")
        if name == getattr(self, "master", None):
            label = "main · " + label
        p.drawText(rect.adjusted(6, 0, -6, -10), Qt.AlignHCenter | Qt.AlignBottom, label)
        p.setFont(self.font())

    # dragging
    def mousePressEvent(self, e):
        pt = e.position().toPoint()
        for name in list(self.cells) + self.pool:
            r = self.tile_rect(name)
            if r and r.contains(pt):
                self.drag = (name, pt - r.topLeft())
                self.drag_pos = pt
                self.update()
                return

    def mouseMoveEvent(self, e):
        if self.drag:
            self.drag_pos = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if not self.drag:
            return
        name = self.drag[0]
        target = self.cell_at(e.position().toPoint())
        if target:
            other = next((n for n, pos in self.cells.items() if pos == target and n != name), None)
            if other:                               # swap with whoever was there
                if name in self.cells:
                    self.cells[other] = self.cells[name]
                else:
                    del self.cells[other]
                    self.pool.append(other)
            if name in self.pool:
                self.pool.remove(name)
            self.cells[name] = target
        elif name in self.cells:                    # dropped outside the grid: unplace it
            del self.cells[name]
            self.pool.append(name)
        self.drag = None
        self.update()


class SettingsWindow(QWidget):
    def __init__(self, app: AirShare):
        super().__init__()
        self.app = app
        self.setWindowTitle("AirShare - layout and settings")
        v = QVBoxLayout(self)
        title = QLabel("Layout")
        f = title.font()
        f.setPointSizeF(f.pointSizeF() + 3)
        f.setBold(True)
        title.setFont(f)
        v.addWidget(title)
        hint = QLabel("Drag your machines into the grid to match your desk. The pointer crosses to the machine "
                      "next to it on any side, arriving at the same point along the edge. Every machine shares "
                      "this layout.")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.grid = GridWidget(app.cfg.name)
        v.addWidget(self.grid)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("<b>Main computer</b> (its keyboard and mouse control the others):"))
        self.master_box = QComboBox()
        mrow.addWidget(self.master_box, 1)
        v.addLayout(mrow)

        v.addSpacing(8)
        v.addWidget(QLabel(f"<b>Shared passphrase</b> (the same on every machine, {MIN_PASSPHRASE}+ characters)"))
        row = QHBoxLayout()
        self.passphrase = QLineEdit(app.cfg.passphrase)
        gen = QPushButton("Generate")
        gen.clicked.connect(lambda: self.passphrase.setText("-".join(secrets.choice(WORDS) for _ in range(4))))
        row.addWidget(self.passphrase)
        row.addWidget(gen)
        v.addLayout(row)

        self.status = QLabel()
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self.save)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        buttons.addWidget(close)
        buttons.addWidget(save)
        v.addLayout(buttons)
        self._dirty_names = None
        self.reload()

    def names(self):
        return sorted({self.app.cfg.name} | {p["name"] for p in self.app.peers.values()} | set(self.app.cells()),
                      key=lambda n: (n != self.app.cfg.name, n.lower()))

    def reload(self):
        connected = [p["name"] for pid, p in self.app.peers.items()
                     if self.app.links.get(pid) and self.app.links[pid].alive]
        self.grid.set_machines(self.names(), self.app.cells(), connected)
        self.grid.master = self.app.master()
        self.master_box.clear()
        self.master_box.addItems(self.names())
        self.master_box.setCurrentText(self.app.master() or self.app.cfg.name)
        self._dirty_names = self.names()
        self.status.setText(self.app.status_line())

    def tick(self):
        """Keep status fresh; add newly seen machines to the pool without disturbing edits."""
        self.status.setText(self.app.status_line())
        names = self.names()
        if names != self._dirty_names:
            for n in names:
                if n not in self.grid.cells and n not in self.grid.pool:
                    self.grid.pool.append(n)
            self.grid.connected = {p["name"] for pid, p in self.app.peers.items()
                                   if self.app.links.get(pid) and self.app.links[pid].alive}
            self._dirty_names = names
            self.grid.update()

    def save(self):
        passphrase = self.passphrase.text().strip()
        if len(passphrase) < MIN_PASSPHRASE:
            QMessageBox.warning(self, "AirShare", f"The passphrase needs at least {MIN_PASSPHRASE} characters.")
            return
        if self.app.cfg.name not in self.grid.cells:
            QMessageBox.warning(self, "AirShare", "Place this machine in the grid too, so AirShare knows where it sits.")
            return
        master = self.master_box.currentText()
        if master not in self.grid.cells:
            QMessageBox.warning(self, "AirShare", f"Place the main computer ({master}) in the grid too.")
            return
        self.app.set_layout(dict(self.grid.cells), master)
        self.grid.master = master
        if passphrase != self.app.cfg.passphrase:
            self.app.cfg.mode, self.app.cfg.passphrase = "local", passphrase
            self.app.cfg.save()
            self.app.reset_for_mode_change()
        self.status.setText("Saved, and shared with your other machines.")


class Tray:
    def __init__(self, app: AirShare, qapp: QApplication):
        self.app, self.qapp = app, qapp
        self.settings = None
        self.icon = QSystemTrayIcon(dot_icon(ORANGE))
        self.menu = QMenu()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self.on_activated)
        app.bridge.refresh.connect(self.refresh)
        app.bridge.notify.connect(lambda m: self.icon.showMessage("AirShare", m, QSystemTrayIcon.Information, 6000))
        app.bridge.open_settings.connect(self.open_settings)
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()
        self.icon.show()

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.app.toggle_pause()

    def refresh(self):
        a = self.app
        self.icon.setIcon(dot_icon(a.colour()))
        self.icon.setToolTip(f"AirShare - {a.status_line()}")
        self.menu.clear()
        act = QAction("Resume sharing" if a.paused else "Pause sharing", self.menu)
        act.triggered.connect(a.toggle_pause)
        self.menu.addAction(act)
        s = QAction(a.status_line(), self.menu)
        s.setEnabled(False)
        self.menu.addAction(s)
        self.menu.addSeparator()
        for side in ("left", "right", "up", "down"):
            name = a.neighbour(side)
            if name:
                mark = "\u25cf" if a.link_for(side) else "\u25cb"
                n = QAction(f"{mark} {name} ({'above' if side == 'up' else 'below' if side == 'down' else side})", self.menu)
                n.setEnabled(False)
                self.menu.addAction(n)
        self.menu.addSeparator()
        lay = QAction("Layout and settings...", self.menu)
        lay.triggered.connect(self.open_settings)
        self.menu.addAction(lay)
        q = QAction("Quit", self.menu)
        q.triggered.connect(self.quit)
        self.menu.addAction(q)
        if self.settings and self.settings.isVisible():
            self.settings.tick()

    def open_settings(self):
        if not self.settings:
            self.settings = SettingsWindow(self.app)
        else:
            self.settings.reload()
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def quit(self):
        self.app.leave_remote()
        self.app.stop()
        self.icon.hide()
        self.qapp.quit()


def send_to_running(cmd: str) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=1) as s:
            s.sendall((cmd + "\n").encode())
            return s.recv(8).startswith(b"ok")
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description="AirShare: one keyboard and mouse across your machines")
    ap.add_argument("--passphrase", help="shared passphrase (same on every machine, 8+ characters)")
    ap.add_argument("--name", help="this machine's name (default: its hostname)")
    ap.add_argument("--quit", action="store_true", help="quit the running copy")
    a = ap.parse_args()

    if send_to_running("quit" if a.quit else "settings") or a.quit:
        return

    set_app("AirShare", VERSION)
    C.DEFAULT_PORT, C.BEACON_PORT = PORT, BEACON_PORT
    C.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    layout_file = C.CONFIG_DIR / "layout.json"
    layout = json.loads(layout_file.read_text()) if layout_file.exists() else {}
    if "cells" not in layout:
        layout = {"cells": {}, "version": 0}      # older left/right layouts are replaced by the grid
    layout.setdefault("master", None)

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    bridge = Bridge()
    app = AirShare(layout, bridge)
    if a.name:
        app.cfg.name = a.name
    if a.passphrase:
        app.cfg.passphrase = a.passphrase
    app.cfg.mode, app.cfg.port = "local", PORT
    app.cfg.save()
    app._set_local_key()

    app.start()
    app.apply_role()
    threading.Thread(target=app.control_loop, daemon=True).start()
    threading.Thread(target=app.layout_loop, daemon=True).start()
    threading.Thread(target=app.watch_clipboard, daemon=True).start()
    tray = Tray(app, qapp)
    log(f"AirShare {VERSION} as '{app.cfg.name}', screen {app.bounds[2]}x{app.bounds[3]}")
    if not app._local_key or not app.cells() or not app.master():
        QTimer.singleShot(1500, tray.open_settings)
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
