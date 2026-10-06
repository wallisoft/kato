#!/usr/bin/env python3
"""
AirShare (working title) - one keyboard and mouse across your machines.

Arrange your machines in a 3x3 grid; push the pointer off any edge that has a
neighbour and it carries on onto that machine, keyboard and scrolling
included. Push it back and it comes home. The layout is shared by the whole
group: change it on any machine and every machine follows. No master.

X11, Windows and Wayland. A Wayland machine is driven through virtual input devices
(airshare_uinput.py), and is the main computer through the desktop's own input-capture
service (airshare_capture.py), which recent GNOME and KDE provide.
Discovery, trust and TLS come from clickboard_core (passphrase mode for now).

  Emergency: Scroll Lock, or Esc three times quickly, brings the pointer home.
"""
import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.request
import webbrowser
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
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPushButton, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)

from clickboard_core import MIN_PASSPHRASE, ClipboardSync, Config, Mesh, log, set_app
from clickboard_core import common as C

VERSION = "0.9.1"
PORT, BEACON_PORT = 47810, 47812      # separate from Clickboard, so both can run
CONTROL_PORT = 47811                  # localhost only: a second launch talks to the running copy
FLUSH_EVERY = 0.008                   # send gathered mouse movement ~120 times a second
BACK_OFF = 24                         # pixels inside the edge when the pointer comes home
RETURN_KEY = keyboard.Key.scroll_lock
ESCAPE_PRESSES, ESCAPE_WINDOW = 3, 1.2
ALIVE_EVERY, ALIVE_TIMEOUT = 1.0, 3.0
LAYOUT_EVERY = 5                      # re-share layout and screen size so late joiners pick them up
LAYOUT_TRUST_FROM = "0.9.0"           # layouts sent by machines older than this are not adopted (see on_frame)
GRID = 4
OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}
STEP = {"left": (0, -1), "right": (0, 1), "up": (-1, 0), "down": (1, 0)}

GREEN, BLUE, ORANGE = QColor(34, 197, 94), QColor(59, 130, 246), QColor(249, 115, 22)

RELEASES_API = "https://api.github.com/repos/wallisoft/airshare-releases/releases/latest"
SETUP_URL = "https://github.com/wallisoft/airshare-releases/releases/latest/download/AirShare-Setup.exe"
LINUX_INSTALL = "curl -fsSL https://github.com/wallisoft/airshare-releases/releases/latest/download/install.sh | bash"
STORE_URL = "ms-windows-store://pdp/?productid=9NK09P1L0KTJ"     # AirShare KVM in the Microsoft Store
WEBSITE = "https://airshare.eur.bz/#download"
UPDATE_CHECK_EVERY = 6 * 3600


def hardware_ids():
    """This computer's physical network-card addresses. A dual-boot machine has the same card under
    both systems, so these say "same computer" even when the name differs."""
    found = set()
    try:
        if platform.system() == "Linux":
            for dev in os.listdir("/sys/class/net"):
                if dev == "lo" or not os.path.exists(f"/sys/class/net/{dev}/device"):
                    continue                                  # virtual: docker, bridges, VPNs
                with open(f"/sys/class/net/{dev}/address") as f:
                    found.add(f.read().strip().lower())
        elif platform.system() == "Windows":
            out = subprocess.run(["getmac", "/fo", "csv", "/nh"], capture_output=True, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5).stdout
            for m in re.findall(r"([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})", out):
                found.add(m.replace("-", ":").lower())
    except Exception as exc:
        log(f"couldn't read network addresses: {exc}")
    if not found:
        import uuid
        node = uuid.getnode()
        if not (node >> 40) & 1:                              # a real address, not uuid's random fallback
            found.add(":".join(f"{(node >> s) & 0xff:02x}" for s in range(40, -8, -8)))
    found.discard("00:00:00:00:00:00")
    return sorted(found)


def install_kind():
    """How this copy was installed: store, windows, linux, or source."""
    if "__compiled__" not in globals():
        return "source"
    if "windowsapps" in sys.executable.lower():
        return "store"
    return "windows" if platform.system() == "Windows" else "linux"


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def latest_version():
    try:
        req = urllib.request.Request(RELEASES_API, headers={"User-Agent": f"AirShare/{VERSION}",
                                                           "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("tag_name", "").lstrip("v") or None
    except Exception as exc:
        log(f"update check failed: {exc}")
        return None


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
# A Wayland session: the X11 ways of watching and driving input only reach X11 programs here.
WAYLAND = LINUX and (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY")))


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


class SecureInput:
    """Talks to the optional AirShare Input Service (SYSTEM) so we can inject onto Windows' secure
    desktops (UAC, the login screen) too. Silently does nothing if the service isn't installed, so
    the feature is simply absent unless the user chose it in the installer."""
    PIPE = r"\\.\pipe\AirShareInput"

    def __init__(self):
        self.handle = None
        self.available = False
        self.checked = 0.0

    def _open(self):
        import ctypes
        GENERIC = 0xC0000000
        OPEN_EXISTING = 3
        h = ctypes.windll.kernel32.CreateFileW(self.PIPE, GENERIC, 0, None, OPEN_EXISTING, 0, None)
        if h == -1 or h == 0xFFFFFFFFFFFFFFFF:
            return None
        mode = ctypes.c_ulong(0x00000002)                 # PIPE_READMODE_MESSAGE
        ctypes.windll.kernel32.SetNamedPipeHandleState(h, ctypes.byref(mode), None, None)
        return h

    def present(self):
        """Is the service installed and reachable? Re-checked occasionally, cheaply."""
        if not WINDOWS:
            return False
        now = time.time()
        if self.handle is None and now - self.checked > 3:
            self.checked = now
            self.handle = self._open()
            self.available = self.handle is not None
            if self.available:
                log("secure-desktop input service connected")
        return self.handle is not None

    def send(self, cmd):
        if not self.present():
            return False
        import ctypes
        data = (json.dumps(cmd) + "\n").encode("utf-8")
        written = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.WriteFile(self.handle, data, len(data), ctypes.byref(written), None)
        if not ok:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None                             # make the next call reconnect
            self.available = False
            return False
        return True


SECURE_INPUT = SecureInput()


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


class StartupSequences:
    """X11 (GNOME on Xorg): when you click AirShare's tray menu, GNOME starts a "launching"
    sequence (the spinning pointer) and waits to be told the app's window has arrived. Qt
    doesn't report back on X11, so the spinner lingers. We listen for those announcements
    on the root window and complete any that are about AirShare once our window is up."""

    def __init__(self):
        self.pending = set()
        self._parts = {}
        self._lock = threading.Lock()
        self.d = None
        if platform.system() != "Linux" or not os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            return
        try:
            from Xlib import X, display
            self.X = X
            self.d = display.Display()
            self.root = self.d.screen().root
            self.a_begin = self.d.intern_atom("_NET_STARTUP_INFO_BEGIN")
            self.a_more = self.d.intern_atom("_NET_STARTUP_INFO")
            self.sender = self.root.create_window(-10, -10, 1, 1, 0, X.CopyFromParent, override_redirect=True)
            listen = display.Display()
            listen.screen().root.change_attributes(event_mask=X.PropertyChangeMask)
            threading.Thread(target=self._listen, args=(listen,), daemon=True).start()
        except Exception as exc:
            log(f"startup notification helper unavailable: {exc}")
            self.d = None

    def _listen(self, listen):
        while True:
            try:
                ev = listen.next_event()
            except Exception:
                return
            if ev.type != self.X.ClientMessage or ev.client_type not in (self.a_begin, self.a_more):
                continue
            key = ev.window.id
            chunk = bytes(ev.data[1])
            buf = (b"" if ev.client_type == self.a_begin else self._parts.get(key, b"")) + chunk
            if b"\0" not in buf:
                self._parts[key] = buf
                continue
            self._parts.pop(key, None)
            msg = buf.split(b"\0", 1)[0].decode(errors="ignore")
            if msg.startswith("new:") and "airshare" in msg.lower():
                m = re.search(r'ID=("(?:[^"\\]|\\.)*"|\S+)', msg)
                if m:
                    with self._lock:
                        self.pending.add(m.group(1).strip('"'))
                    log("startup sequence seen: " + m.group(1)[:60])

    def complete(self):
        """Tell the window manager every AirShare launch sequence is finished."""
        if not self.d:
            return
        with self._lock:
            ids, self.pending = list(self.pending), set()
        for sid in ids:
            try:
                from Xlib.protocol import event
                data = ('remove: ID="' + sid.replace("\\", "\\\\").replace('"', '\\"') + '"').encode() + b"\0"
                for i in range(0, len(data), 20):
                    chunk = data[i:i + 20].ljust(20, b"\0")
                    ev = event.ClientMessage(window=self.sender, client_type=self.a_begin if i == 0 else self.a_more,
                                             data=(8, chunk))
                    self.root.send_event(ev, event_mask=self.X.PropertyChangeMask)
                self.d.flush()
                log("startup sequence completed: " + sid[:60])
            except Exception as exc:
                log(f"couldn't complete startup sequence: {exc}")


def x11_activate(win_id):
    """Ask the window manager to bring a window to the front the way a taskbar does,
    which GNOME honours (a plain raise from the app gets 'is ready' and stays behind)."""
    if platform.system() != "Linux" or not os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return
    try:
        from Xlib import X, display
        from Xlib.protocol import event
        d = display.Display()
        root = d.screen().root
        ev = event.ClientMessage(window=d.create_resource_object("window", int(win_id)),
                                 client_type=d.intern_atom("_NET_ACTIVE_WINDOW"),
                                 data=(32, [2, X.CurrentTime, 0, 0, 0]))     # 2 = request from a pager/taskbar
        root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        d.flush()
        d.close()
    except Exception as exc:
        log(f"couldn't bring the window to the front: {exc}")


def win_cursor_hidden():
    """Windows: True when Windows isn't showing its pointer (typically: no mouse attached)."""
    import ctypes

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("flags", ctypes.c_ulong),
                    ("hCursor", ctypes.c_void_p), ("pt", POINT)]
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(ci)):
        return False
    return not (ci.flags & 0x1)                             # CURSOR_SHOWING


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
    pointer = Signal(int, int, bool)    # stand-in pointer: x, y (screen pixels), show


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
        self.versions = {}              # machine name -> AirShare version it reports
        self.hw = hardware_ids()        # this machine's network-card addresses
        self.machine_hw = {}            # machine name -> its addresses, as reported
        self._stand_in = False          # Windows: our own pointer is showing (Windows hid its own)
        self.latest = None              # newest release on GitHub, once checked
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
        self.uinput = None              # Wayland: the virtual mouse and keyboard we are driven through
        self.uinput_error = ""
        self.capture = None             # Wayland: the desktop's input capture, when this is the main computer
        self._two_mains_at = 0.0        # when another machine last tried to drive this main computer
        self._enter_grace = 0.0         # Wayland: just arrived on another machine; don't bounce straight home
        self._layout_ignored = set()    # older machines whose layouts we have declined (logged once each)
        self._ensure_uinput()

    def _ensure_uinput(self):
        """Wayland only: create the virtual devices if they aren't there yet. Tried again each time another
        machine wants to drive this one, so it starts working as soon as the installer has granted access."""
        if not WAYLAND or self.uinput:
            return bool(self.uinput)
        try:
            import importlib
            airshare_uinput = importlib.import_module("airshare_uinput")     # Linux-only: kept out of the Windows build
            if not airshare_uinput.available():
                raise PermissionError("no access to /dev/uinput yet - run the AirShare installer again to grant it")
            self.uinput = airshare_uinput.UinputInjector(log)
            self.uinput_error = ""
        except Exception as exc:
            if str(exc) != self.uinput_error:
                log(f"Wayland: can't be driven yet: {exc}")
            self.uinput_error = str(exc)
        return bool(self.uinput)

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

    def kvm_on(self):
        return self.layout.get("kvm", True)

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

    def set_layout(self, cells, master, kvm=True, share=True, version=None):
        known = {**self.layout.get("hw", {}), **self.machine_hw, self.cfg.name: self.hw}
        hw = {n: known[n] for n in cells if known.get(n)}
        self.layout = {"cells": cells, "master": master, "kvm": bool(kvm), "hw": hw,
                       "version": time.time() if version is None else version}
        self._save_layout()
        self.apply_role()
        self._home_if_stranded()
        if share:
            self.share_state()
        self.refresh_ui()

    def _save_layout(self):
        (C.CONFIG_DIR / "layout.json").write_text(json.dumps(self.layout, indent=2))

    def share_state(self):
        bx, by, bw, bh = self.bounds
        for link in list(self.links.values()):
            if link.alive:
                self._send(link, {"type": "screen", "name": self.cfg.name, "w": bw, "h": bh, "version": VERSION,
                                  "hw": self.hw})
                if self.cells():
                    self._send(link, {"type": "layout", "layout": self.layout})

    def adopt_by_hardware(self, name, hw):
        """A machine that isn't in the grid, but shares a network card with a placed machine that's
        offline, is the same computer under another name (dual boot): it takes that machine's place."""
        cells = self.cells()
        if not hw or not name or name in cells:
            return
        known = self.layout.get("hw", {})
        for placed in list(cells):
            if placed == self.cfg.name and name != self.cfg.name:
                continue
            if placed != self.cfg.name and self.link_to(placed) is not None:
                continue                                      # it's online: a different computer
            if not set(known.get(placed, [])) & set(hw):
                continue
            new_cells = dict(cells)
            new_cells[name] = new_cells.pop(placed)
            master = name if self.master() == placed else self.master()
            self.machine_hw[name] = list(hw)
            # Only a whisker newer than the layout it amends: enough to reach machines holding the same
            # layout, never enough to beat a layout somebody actually saved later. (Stamping it "now" let a
            # machine that had just been switched on push its stale grid, and its idea of the main computer,
            # over everyone else's.)
            self.set_layout(new_cells, master, self.kvm_on(), version=float(self.layout.get("version", 0)) + 0.001)
            log(f"{name} has taken {placed}'s place in the grid (same computer)")
            self.notify(f"{name} has taken {placed}'s place (same computer)")
            return

    def _can_get_home(self):
        """While driving another machine: is there still a path of neighbours from it back to this one?"""
        cells, me = self.cells(), self.cfg.name
        if self.active not in cells or me not in cells:
            return False
        seen, todo = {self.active}, [self.active]
        while todo:
            here = todo.pop()
            for side in STEP:
                nxt = self.neighbour_of(here, side)
                if nxt == me:
                    return True
                if nxt and nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return False

    def _home_if_stranded(self):
        """The grid changed while we were on another machine. If there is no longer a way to walk the pointer
        back, every edge of that screen is a wall and this machine's keyboard and mouse would be stuck there."""
        if self.active and not self._can_get_home():
            log(f"the layout changed and {self.active} no longer leads back here: coming home")
            threading.Thread(target=self._home_safely, daemon=True).start()

    def _revive_capture(self):
        """Wayland main computer: if the capture helper has stopped, start it again. Promptly when it had been
        working (it was killed, or the desktop restarted), once a minute when it never got going (nothing is
        likely to have changed, but an installed package or a newer desktop would fix it)."""
        cap = self.capture
        if not (WAYLAND and cap) or cap.running or not (self.is_master() and self.kvm_on()):
            return
        if time.time() - cap.stopped_at < (3 if cap.was_ready else 60):
            return
        log("Wayland: starting the capture helper again")
        self.capture, self._input_on = None, False
        self.apply_role()

    def layout_loop(self):
        while self.running:
            self.adopt_by_hardware(self.cfg.name, self.hw)
            self.share_state()
            try:
                self._revive_capture()
            except Exception as exc:
                log(f"couldn't restart the Wayland helper: {exc}")
            time.sleep(LAYOUT_EVERY)

    def apply_role(self):
        """Only the main computer listens to input. The others install no hooks at all."""
        want = self.is_master() and self.kvm_on()
        if WAYLAND:
            # Wayland hides the keyboard and mouse from ordinary programs; the desktop's own capture service
            # stands in for the X11 listeners (see airshare_capture.py).
            if want and not self.capture:
                import importlib
                cap = importlib.import_module("airshare_capture").WaylandCapture(self, log, BACK_OFF, (ESCAPE_PRESSES, ESCAPE_WINDOW))
                self.capture = cap
                if cap.start():
                    self._input_on = True
                    log("main computer (Wayland): asking the desktop to watch the screen edges")
                else:
                    log(f"Wayland: can't be the main computer: {cap.error}")
            elif not want and self.capture:
                self.go_home()
                self.capture.stop()
                self.capture, self._input_on = None, False
                log("keyboard and mouse sharing off here: input listening off")
            return
        if want and not self._input_on:
            self._start_input()
        elif not want and self._input_on:
            self.go_home()
            for w in self._watchers:
                w.stop()
            self._watchers, self._input_on = [], False
            log("keyboard and mouse sharing off here: input listening off")

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
        if self.paused or self._busy or self.driven_by:      # being driven: that pointer movement isn't ours to act on
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
                if WAYLAND:
                    pass                                      # the desktop has already handed us the input
                elif LINUX:
                    self.mouse.position = self._centre
                    if not self.xgrab.grab():
                        log("couldn't take the keyboard and pointer (another app has them): not crossing")
                        return
                else:
                    self.mouse.position = self._centre
                    self._grabs = [
                        mouse.Listener(on_move=self._g_move, on_click=self._g_click, on_scroll=self._g_scroll, suppress=True),
                        keyboard.Listener(on_press=self._g_press, on_release=self._g_release, suppress=True),
                    ]
                    for g in self._grabs:
                        g.start()
                if not WAYLAND:
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
            r = 0.5 if ratio is None else min(max(float(ratio), 0.0), 1.0)
            side = self._home_side
            if WAYLAND:
                if self.capture:                              # hand the input back; the pointer returns inside our edge
                    self.capture.release_home(side, r)
            else:
                if self.xgrab:
                    self.xgrab.release()
                for g in self._grabs:
                    g.stop()
                self._grabs = []
                time.sleep(0.05)
                bx, by, bw, bh = self.bounds
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
                if input_blocked() and not SECURE_INPUT.present():
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
                if time.time() >= self._enter_grace:          # (a wobble in the first instant doesn't send us home)
                    self.go_home(ratio)
                    return
            elif nxt and self.link_to(nxt) and nxt in self.sizes:
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
                self.versions[name] = str(header.get("version", "old"))
                hw = [str(h).lower() for h in header.get("hw", []) if h]
                if hw:
                    self.machine_hw[name] = hw
                    self.adopt_by_hardware(name, hw)
        elif t == "layout":
            theirs = header.get("layout") or {}
            sender = self.peers.get(link.peer_id, {}).get("name", "")
            if sender not in self.versions:
                return                                        # it hasn't said which version it runs yet; it will resend
            if vtuple(self.versions[sender]) < vtuple(LAYOUT_TRUST_FROM):
                # Older AirShares could stamp a stale layout as brand new when a machine changed name, and
                # push it over everyone's. Their layouts are left alone until they are updated.
                if sender not in self._layout_ignored:
                    self._layout_ignored.add(sender)
                    log(f"not taking the layout from {sender or 'an older machine'} until it is updated "
                        f"(it runs AirShare {self.versions.get(sender, 'an old version')})")
                return
            if float(theirs.get("version", 0)) > float(self.layout.get("version", 0)):
                self.layout = {"cells": dict(theirs.get("cells", {})), "master": theirs.get("master"),
                               "kvm": bool(theirs.get("kvm", True)), "hw": dict(theirs.get("hw", {})),
                               "version": float(theirs["version"])}
                self._save_layout()
                log(f"layout updated by another machine (main computer: {self.master()})")
                self.apply_role()
                self._home_if_stranded()
                self.refresh_ui()
        elif t == "kvm_alive":
            if self.active and self.link_to(self.active) is link:
                self._last_alive = time.time()
        elif t == "kvm_leave":                                # a driven machine asks us to stop (locked, paused)
            if self.active and self.link_to(self.active) is link:
                if self.capture:                              # Wayland: don't let the edge catch us straight back
                    self.capture.refused(f"{self.active} handed control back")
                threading.Thread(target=self._home_safely, daemon=True).start()
        # ---- being driven ----
        elif t == "kvm_enter":
            if self.is_master() and self.kvm_on():
                # Two machines each believe they are the main computer (their layouts disagree). If this one
                # let itself be driven, each would capture the input the other injects and pass it straight back:
                # an endless loop that locks both. Refuse, and say what to do.
                link.send({"type": "kvm_leave"})
                who = self.peers.get(link.peer_id, {}).get("name", "another machine")
                if time.time() - self._two_mains_at > 20:
                    log(f"{who} tried to take control, but this machine is set as the main computer too: "
                        f"refused. Open Layout on the one you want as main and press Save.")
                    self.notify(f"{who} and this machine are both set as the main computer. "
                                f"Open Layout on the one you want as main and press Save.")
                self._two_mains_at = time.time()
                self.refresh_ui()
                return
            if (input_blocked() and not SECURE_INPUT.present()) or self.paused:
                link.send({"type": "kvm_leave"})
                log("refused control: this machine is " + ("paused" if self.paused else "locked"))
                return
            if WAYLAND and not self._ensure_uinput():
                link.send({"type": "kvm_leave"})
                self.refresh_ui()
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
                    if input_blocked() and SECURE_INPUT.send({"t": "move", "x": x, "y": y}):
                        return
                    win_move_abs(x, y)
                    hidden = win_cursor_hidden()
                    if hidden or self._stand_in:
                        self.bridge.pointer.emit(x, y, hidden)
                        self._stand_in = hidden
                    return
                except Exception as exc:
                    log(f"real-mouse move failed, placing the pointer instead: {exc}")
            if self.uinput:
                self.uinput.move(int(header.get("x", 0)), int(header.get("y", 0)), bw, bh)
                return
            self.mouse.position = (x, y)
        elif t == "kvm_btn" and self.driven_by is link:
            name = str(header.get("button"))
            if self.uinput:
                self.uinput.button(name, bool(header.get("down")))
                return
            if WINDOWS and input_blocked() and SECURE_INPUT.send(
                    {"t": "btn", "button": name, "down": bool(header.get("down"))}):
                return
            btn = getattr(mouse.Button, name, None)
            if btn:
                (self.mouse.press if header.get("down") else self.mouse.release)(btn)
        elif t == "kvm_scroll" and self.driven_by is link:
            dx, dy = int(header.get("dx", 0)), int(header.get("dy", 0))
            if WINDOWS and input_blocked() and SECURE_INPUT.send({"t": "scroll", "dx": dx, "dy": dy}):
                return
            if self.uinput:
                self.uinput.scroll(dx, dy)
                return
            self.mouse.scroll(dx, dy)
        elif t == "kvm_key" and self.driven_by is link:
            if self.uinput:
                try:
                    self.uinput.key(header.get("key") or {}, bool(header.get("down")))
                except Exception as exc:
                    log(f"couldn't type {header.get('key')}: {exc}")
                return
            k = wire_to_key(header.get("key") or {})
            if k is None:
                return
            ident = json.dumps(header.get("key"), sort_keys=True)
            if WINDOWS and input_blocked() and self._secure_key(header.get("key") or {}, bool(header.get("down"))):
                return
            try:
                if header.get("down"):
                    self.kb.press(k)
                    self._held_keys[ident] = k
                else:
                    self.kb.release(k)
                    self._held_keys.pop(ident, None)
            except Exception as exc:
                log(f"couldn't type {header.get('key')}: {exc}")

    # Windows virtual-key codes for the non-character keys we send
    _VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
           "esc": 0x1B, "escape": 0x1B, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
           "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22, "insert": 0x2D,
           "shift": 0x10, "shift_r": 0x10, "ctrl": 0x11, "ctrl_l": 0x11, "ctrl_r": 0x11,
           "alt": 0x12, "alt_l": 0x12, "alt_r": 0x12, "alt_gr": 0x12, "cmd": 0x5B, "cmd_r": 0x5C,
           "caps_lock": 0x14, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
           "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}

    def _secure_key(self, wire, down):
        """Relay one key press/release to the secure-desktop service. wire is the on-the-wire key dict:
        {"name": "enter"} a named key, {"char": "a"} a character, or {"vk": 65} a raw virtual key."""
        if "name" in wire:
            vk = self._VK.get(str(wire["name"]).lower())
            return SECURE_INPUT.send({"t": "key", "vk": vk, "down": down}) if vk else False
        if "char" in wire and wire["char"]:
            return SECURE_INPUT.send({"t": "key", "char": ord(wire["char"][0]), "down": down})
        if "vk" in wire:
            return SECURE_INPUT.send({"t": "key", "vk": int(wire["vk"]), "down": down})
        return False

    def secure_available(self):
        return WINDOWS and SECURE_INPUT.present()

    def _alive_loop(self, link):
        while self.driven_by is link and link.alive:
            if input_blocked() and not SECURE_INPUT.present():
                self._release_all()
                self.driven_by = None
                link.send({"type": "kvm_leave"})
                log("locked: handed control back")
                self.refresh_ui()
                return
            link.send({"type": "kvm_alive"})
            time.sleep(ALIVE_EVERY)

    def _release_all(self):
        if self.uinput:
            try:
                self.uinput.release_all()
            except Exception as exc:
                log(f"release failed: {exc}")
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
        if time.time() - self._two_mains_at < 20:
            return "Two machines are set as the main computer - open Layout on the one you want and press Save"
        if WAYLAND and self.is_master() and self.kvm_on() and self.capture and self.capture.error:
            return f"Wayland: can't be the main computer here - {self.capture.error}"
        if WAYLAND and not self.uinput and self.kvm_on():
            return "Wayland: run the AirShare installer again so this machine can be driven"
        if not self.master():
            return "Choose the main computer in Layout"
        if self.active:
            return f"Controlling {self.active}"
        if self.driven_by:
            return f"Controlled by {self.master()}"
        n = self.connected_count()
        if not n:
            return "Looking for your other machines..."
        behind = self.out_of_date()
        if behind:
            return "Update needed on: " + ", ".join(behind)
        if not self.kvm_on():
            return f"Clipboard sharing - {n} machine{'s' if n != 1 else ''} connected"
        role = "Main computer" if self.is_master() else f"Main computer: {self.master()}"
        return f"{role} - {n} machine{'s' if n != 1 else ''} connected"

    def connected_names(self):
        return [p["name"] for pid, p in self.peers.items() if self.links.get(pid) and self.links[pid].alive]

    def out_of_date(self):
        """Connected machines running a different AirShare version from the newest one in the group."""
        names = self.connected_names()
        everyone = {self.cfg.name: VERSION, **{n: self.versions.get(n, "old") for n in names}}
        newest = max(everyone.values(), key=vtuple)
        return sorted(n for n, v in everyone.items() if vtuple(v) < vtuple(newest))

    def update_available(self):
        return bool(self.latest) and vtuple(self.latest) > vtuple(VERSION)

    def update_loop(self):
        while self.running:
            v = latest_version()
            if v:
                self.latest = v
                if self.update_available():
                    log(f"update available: {v}")
                self.refresh_ui()
            time.sleep(UPDATE_CHECK_EVERY)

    def upgrade(self):
        kind = install_kind()
        log(f"upgrade requested ({kind} install)")
        if kind == "store":
            webbrowser.open(STORE_URL)
        elif kind == "windows":
            try:
                path = os.path.join(tempfile.gettempdir(), "AirShare-Setup.exe")
                urllib.request.urlretrieve(SETUP_URL, path)
                os.startfile(path)                     # the installer closes this copy and replaces it
            except Exception as exc:
                self.notify(f"Couldn't download the update ({exc}). Opening the website instead.")
                webbrowser.open(WEBSITE)
        elif kind == "linux":
            cmd = LINUX_INSTALL + "; echo; read -p 'Done. Press Enter to close.'"
            for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["konsole", "-e"], ["xterm", "-e"]):
                try:
                    subprocess.Popen(term + ["bash", "-c", cmd])
                    return
                except FileNotFoundError:
                    continue
            webbrowser.open(WEBSITE)
        else:
            webbrowser.open(WEBSITE)

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
            cs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)     # so a quick restart can listen again at once
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
    """A GRID x GRID grid of cells; machines are tiles you drag into place. Unplaced ones wait below."""
    CELL_W, CELL_H, GAP = 120, 62, 8     # compact, so the window fits a 768-pixel laptop screen

    def __init__(self, me: str):
        super().__init__()
        self.me = me
        self.cells = {}           # name -> [row, col]
        self.pool = []            # names not placed yet
        self.drag = None          # (name, offset QPoint)
        self.drag_pos = QPoint()
        self.connected = set()
        w = GRID * self.CELL_W + (GRID + 1) * self.GAP
        self.setMinimumSize(w, GRID * self.CELL_H + (GRID + 1) * self.GAP + 26 + self.CELL_H + self.GAP)
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
        top = self.GAP + GRID * (self.CELL_H + self.GAP) + 26
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
        fm = p.fontMetrics()
        p.drawText(rect.adjusted(8, 6, -8, -22), Qt.AlignCenter, fm.elidedText(name, Qt.ElideRight, rect.width() - 16))
        f.setBold(False)
        f.setPointSizeF(max(f.pointSizeF() - 1.5, 7))
        p.setFont(f)
        label = "this machine" if mine else ("connected" if name in self.connected else "not connected")
        if name == getattr(self, "master", None):
            label = "main · " + ("this one" if mine else label)
        fm = p.fontMetrics()
        p.drawText(rect.adjusted(8, 0, -8, -9), Qt.AlignHCenter | Qt.AlignBottom,
                   fm.elidedText(label, Qt.ElideRight, rect.width() - 16))
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
        body = QWidget()
        v = QVBoxLayout(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll)
        self.body = body
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
        self.kvm_box = QCheckBox("Share keyboard and mouse (untick to share only the clipboard, images and files)")
        v.addWidget(self.kvm_box)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("<b>Main computer</b> (its keyboard and mouse control the others):"))
        self.master_box = QComboBox()
        mrow.addWidget(self.master_box, 1)
        v.addLayout(mrow)
        self.kvm_box.toggled.connect(lambda on: self.master_box.setEnabled(on))

        v.addSpacing(8)
        v.addWidget(QLabel(f"<b>Shared passphrase</b> (the same on every machine, {MIN_PASSPHRASE}+ characters)"))
        row = QHBoxLayout()
        self.passphrase = QLineEdit(app.cfg.passphrase)
        gen = QPushButton("Generate")
        gen.clicked.connect(lambda: self.passphrase.setText("-".join(secrets.choice(WORDS) for _ in range(4))))
        row.addWidget(self.passphrase)
        row.addWidget(gen)
        v.addLayout(row)

        vrow = QHBoxLayout()
        self.version_label = QLabel()
        self.upgrade_btn = QPushButton("Upgrade")
        self.upgrade_btn.clicked.connect(self.app.upgrade)
        vrow.addWidget(self.version_label, 1)
        vrow.addWidget(self.upgrade_btn)
        v.addLayout(vrow)
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
        self.kvm_box.setChecked(self.app.kvm_on())
        self.master_box.setEnabled(self.app.kvm_on())
        self._dirty_names = self.names()
        self.show_version()
        self.status.setText(self.app.status_line())

    def fit_to_screen(self):
        """Open at the size the content wants, but never taller than the screen (it scrolls instead)."""
        want = self.body.sizeHint()
        avail = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        w = min(want.width() + self.scroll.verticalScrollBar().sizeHint().width() + 4, avail.width() - 40)
        h = min(want.height() + 4, avail.height() - 60)
        self.resize(max(w, 420), max(h, 300))

    def show_version(self):
        a = self.app
        kind = {"store": "Microsoft Store", "windows": "Windows installer", "linux": "Linux",
                "source": "running from source"}[install_kind()]
        latest = a.latest or "checking..."
        self.version_label.setText(f"This machine: <b>{VERSION}</b> ({kind}) \u00b7 Latest: <b>{latest}</b>")
        self.upgrade_btn.setVisible(a.update_available())
        self.upgrade_btn.setText("Open the Store" if install_kind() == "store" else f"Upgrade to {a.latest}")

    def tick(self):
        """Keep status fresh; add newly seen machines to the pool without disturbing edits."""
        self.show_version()
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
        self.app.set_layout(dict(self.grid.cells), master, self.kvm_box.isChecked())
        self.grid.master = master
        if passphrase != self.app.cfg.passphrase:
            self.app.cfg.mode, self.app.cfg.passphrase = "local", passphrase
            self.app.cfg.save()
            self.app.reset_for_mode_change()
        self.status.setText("Saved, and shared with your other machines.")


class StandInPointer(QWidget):
    """Windows hides its pointer on a PC with no mouse attached. While another machine drives
    this one, AirShare draws its own arrow there instead: always on top, click-through,
    never takes focus. No driver, no dongle."""
    SIZE = 26

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(self.SIZE, self.SIZE)

    def paintEvent(self, _):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        arrow = QPolygonF([QPointF(x, y) for x, y in
                           ((1.5, 1.5), (1.5, 19), (6, 14.5), (9.5, 22), (13, 20.5), (9.5, 13.5), (15.5, 13.5))])
        p.setPen(QPen(Qt.white, 1.6))
        p.setBrush(Qt.black)
        p.drawPolygon(arrow)
        p.end()

    def place(self, x, y, show):
        if not show:
            self.hide()
            return
        if not self.isVisible():
            self.show()
        try:
            import ctypes
            # screen pixels, topmost, no resize, never activate
            ctypes.windll.user32.SetWindowPos(int(self.winId()), -1, int(x), int(y), 0, 0, 0x0001 | 0x0010)
        except Exception:
            self.move(int(x), int(y))


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
        self.startup = StartupSequences()
        self.stand_in = None
        if WINDOWS:
            self.stand_in = StandInPointer()
            app.bridge.pointer.connect(self.stand_in.place)
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        # Only show the icon where there is a tray to show it in: asking a desktop without one (GNOME with no
        # tray extension) just prints a confusing D-Bus error. refresh() shows it later if a tray turns up.
        self._icon_shown = QSystemTrayIcon.isSystemTrayAvailable()
        self.refresh()
        if self._icon_shown:
            self.icon.show()
        else:
            log("this desktop has no tray: opening the settings window instead (open AirShare from the app menu to see it again)")
            QTimer.singleShot(1200, self.open_settings)

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.app.toggle_pause()

    def refresh(self):
        """Runs every 2 s. Only touches the tray when something has actually changed, and never
        rebuilds the menu while it's open: rebuilding unconditionally made it flicker."""
        if not self._icon_shown and QSystemTrayIcon.isSystemTrayAvailable():
            self._icon_shown = True
            self.icon.show()
        a = self.app
        if self.stand_in and a.driven_by is None and self.stand_in.isVisible():
            self.stand_in.hide()
            a._stand_in = False
        colour, tip = a.colour(), f"AirShare - {a.status_line()}"
        if colour != getattr(self, "_colour", None):
            self._colour = colour
            self.icon.setIcon(dot_icon(colour))
        if tip != getattr(self, "_tip", None):
            self._tip = tip
            self.icon.setToolTip(tip)
        spec = self.menu_spec()
        signature = [(kind, text, enabled) for kind, text, enabled, _ in spec]
        if signature != getattr(self, "_menu_sig", None) and not self.menu.isVisible():
            self._menu_sig = signature
            self.menu.clear()
            for kind, text, enabled, action in spec:
                if kind == "sep":
                    self.menu.addSeparator()
                    continue
                item = QAction(text, self.menu)
                item.setEnabled(enabled)
                if action:
                    item.triggered.connect(action)
                self.menu.addAction(item)
        if self.settings and self.settings.isVisible():
            self.settings.tick()

    def menu_spec(self):
        """What the tray menu should show right now: (kind, text, enabled, action)."""
        a = self.app
        spec = [("item", "Resume sharing" if a.paused else "Pause sharing", True, a.toggle_pause),
                ("item", a.status_line(), False, None), ("sep", "", False, None)]
        for side in ("left", "right", "up", "down"):
            name = a.neighbour(side)
            if name:
                mark = "\u25cf" if a.link_for(side) else "\u25cb"
                ver = a.versions.get(name)
                where = "above" if side == "up" else "below" if side == "down" else side
                spec.append(("item", f"{mark} {name} ({where})" + (f" \u00b7 {ver}" if ver else ""), False, None))
        spec.append(("sep", "", False, None))
        if a.update_available():
            spec.append(("item", f"Update available: {a.latest}...", True, a.upgrade))
        spec += [("item", "Layout and settings...", True, self.open_settings), ("item", "Quit", True, self.quit)]
        return spec

    def open_settings(self):
        if not self.settings:
            self.settings = SettingsWindow(self.app)
        else:
            self.settings.reload()
        self.settings.fit_to_screen()
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()
        x11_activate(self.settings.winId())
        # GNOME's tray starts a "launching" spinner when its menu opens a window; finish it,
        # now and again shortly after in case the announcement arrives a moment late.
        self.startup.complete()
        QTimer.singleShot(1500, self.startup.complete)

    def quit(self):
        self.app.leave_remote()
        if self.app.capture:
            self.app.capture.stop()
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
    try:                                          # a power cut can leave empty bytes in the log, which makes tools
        raw = C.LOG_FILE.read_bytes()             # treat it as binary and refuse to show it: take them out
        if b"\x00" in raw:
            C.LOG_FILE.write_bytes(raw.replace(b"\x00", b""))
    except Exception:
        pass
    layout_file = C.CONFIG_DIR / "layout.json"
    layout = json.loads(layout_file.read_text()) if layout_file.exists() else {}
    if "cells" not in layout:
        layout = {"cells": {}, "version": 0}      # older left/right layouts are replaced by the grid
    layout.setdefault("master", None)
    layout.setdefault("kvm", True)

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
    threading.Thread(target=app.update_loop, daemon=True).start()
    tray = Tray(app, qapp)
    # Ctrl+C in a terminal (and a polite "please stop" from the system) quit properly: come home, let go of
    # everything, then leave. Qt only lets Python notice such signals when Python gets a turn, hence the timer.
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: QTimer.singleShot(0, tray.quit))
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(250)
    log(f"AirShare {VERSION} as '{app.cfg.name}', screen {app.bounds[2]}x{app.bounds[3]}")
    if not app._local_key or not app.cells() or not app.master():
        QTimer.singleShot(1500, tray.open_settings)
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
