#!/usr/bin/env python3
"""
Kato (working title) - one keyboard and mouse across your machines.

Arrange your machines in a 4x4 grid; push the pointer off any edge that has a
neighbour and it carries on onto that machine, keyboard and scrolling
included. Push it back and it comes home. The layout is shared by the whole
group: change it on any machine and every machine follows. No master.

X11, Windows and Wayland. A Wayland machine is driven through virtual input devices
(kato_uinput.py), and is the main computer through the desktop's own input-capture
service (kato_capture.py), which recent GNOME and KDE provide.
Discovery, trust and TLS come from clickboard_core (passphrase mode for now).

  Emergency: Scroll Lock, or Esc three times quickly, brings the pointer home.
"""
import argparse
import json
import os
import re
import shutil
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

try:
    from pynput import keyboard, mouse
except Exception:                      # a headless box has no display; the input service injects via uinput only
    keyboard = mouse = None
from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QDesktopServices, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap, QTextOption
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)

from clickboard_core import MIN_PASSPHRASE, ClipboardSync, Config, Mesh, log, set_app
from clickboard_core import common as C

import kato_licence as licence
from kato_files import BigFiles

VERSION = "1.1.0"
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

RELEASES_API = "https://api.github.com/repos/wallisoft/kato-releases/releases/latest"
SETUP_URL = "https://github.com/wallisoft/kato-releases/releases/latest/download/Kato-Setup.exe"
LINUX_INSTALL = "curl -fsSL https://github.com/wallisoft/kato-releases/releases/latest/download/install.sh | bash"
STORE_URL = "ms-windows-store://pdp/?productid=9MSPVD4001W5"     # Kato in the Microsoft Store
WEBSITE = "https://juska.uk/#download"
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


TROUBLE = "\u26a0\ufe0f Check log"         # shown in the menu and the window; the menu item opens the log


def adopt_old_settings():
    """Kato was called AirShare until 0.10. The first time it runs under the new name, bring the old settings
    across: this machine's name and identity, the passphrase, the layout and the licence, so nothing has to be set
    up again. The old folder is left as it was. Returns True if settings were brought across."""
    new = C.CONFIG_DIR
    old = new.parent / ("AirShare" if platform.system() == "Windows" else "kato")
    if new.exists() or not old.is_dir():
        return False
    try:
        shutil.copytree(old, new, ignore=shutil.ignore_patterns("*.log", "start.log"))
        return True
    except Exception:
        return False


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def latest_version():
    try:
        req = urllib.request.Request(RELEASES_API, headers={"User-Agent": f"Kato/{VERSION}",
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


_elevated = {"at": 0.0, "is": False}


def elevated_target() -> bool:
    """True when the window in front, or the one under the pointer, belongs to a program running as
    administrator. Windows silently drops input that an ordinary program sends to those, so it has to go
    through the input service instead (when that is installed)."""
    if platform.system() != "Windows":
        return False
    now = time.time()
    if now - _elevated["at"] < 0.25:
        return _elevated["is"]
    _elevated["at"], found = now, False
    try:
        import ctypes
        from ctypes import wintypes
        u, k, a = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.advapi32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        a.OpenProcessToken.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        a.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        for hwnd in {u.GetForegroundWindow(), u.WindowFromPoint(pt)}:
            if not hwnd or found:
                continue
            pid = wintypes.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value or pid.value == os.getpid():
                continue
            h = k.OpenProcess(0x1000, False, pid.value)              # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                found = True                                         # can't even look at it: it is above us
                continue
            try:
                token = ctypes.c_void_p()
                if not a.OpenProcessToken(h, 0x0008, ctypes.byref(token)):   # TOKEN_QUERY
                    found = True
                    continue
                try:
                    elevated, size = wintypes.DWORD(0), wintypes.DWORD(0)
                    if a.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size)) and elevated.value:
                        found = True                                 # TokenElevation
                finally:
                    k.CloseHandle(token)
            finally:
                k.CloseHandle(h)
    except Exception:
        found = False
    _elevated["is"] = found
    return found


def needs_service() -> bool:
    """Input that only the Windows input service can deliver: the lock screen and security prompts, and any
    window belonging to a program running as administrator."""
    return input_blocked() or elevated_target()


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

# The headless input service (drive the login screen and machines with nobody logged in): it runs as root at
# boot, so it always has /dev/uinput and never has a display. It forces the uinput path and skips everything
# that needs a desktop (pynput, the cursor, the tray, clipboard sync).
INPUT_SERVICE = ("--input-service" in sys.argv)


def drm_screen_size():
    """The panel's real resolution straight from the kernel (no display server needed), for the input service.
    Falls back to 1920x1080 when nothing is connected yet (a truly headless box): the pointer is sent as a
    fraction of the screen, so an exact match only affects aspect, never whether a click lands."""
    import glob
    for modes in sorted(glob.glob("/sys/class/drm/*/modes")):
        try:
            status = os.path.join(os.path.dirname(modes), "status")
            if os.path.exists(status) and open(status).read().strip() != "connected":
                continue
            first = open(modes).read().split("\n")[0].strip().lower()
            if "x" in first:
                w, h = first.split("x", 1)
                return (int(re.sub(r"\D", "", w) or 0), int(re.sub(r"\D", "", h) or 0))
        except Exception:
            pass
    return (1920, 1080)


class XGrabber:
    """Linux/X11: Kato's own explicit hold on the keyboard and pointer, on a dedicated
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
    """Talks to the optional Kato Input Service (SYSTEM) so we can inject onto Windows' secure
    desktops (UAC, the login screen) too. Silently does nothing if the service isn't installed, so
    the feature is simply absent unless the user chose it in the installer."""
    PIPE = r"\\.\pipe\KatoInput"

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
    """X11 (GNOME on Xorg): when you click Kato's tray menu, GNOME starts a "launching"
    sequence (the spinning pointer) and waits to be told the app's window has arrived. Qt
    doesn't report back on X11, so the spinner lingers. We listen for those announcements
    on the root window and complete any that are about Kato once our window is up."""

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
            if msg.startswith("new:") and "kato" in msg.lower():
                m = re.search(r'ID=("(?:[^"\\]|\\.)*"|\S+)', msg)
                if m:
                    with self._lock:
                        self.pending.add(m.group(1).strip('"'))
                    log("startup sequence seen: " + m.group(1)[:60])

    def complete(self):
        """Tell the window manager every Kato launch sequence is finished."""
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


class Kato(BigFiles, ClipboardSync, Mesh):
    """One MAIN computer captures its keyboard and mouse and drives the others.

    The main computer keeps a virtual pointer for whichever screen is active and
    decides every crossing itself, sending absolute positions. The other computers
    install no input hooks at all: they report their screen size, and move, click
    and type as told. (The same shape Synergy uses, for the same reasons.)

    Clipboard, images and files are shared between all machines too, via
    ClipboardSync from clickboard_core.
    """

    def __init__(self, layout: dict, bridge: Bridge):
        Mesh.__init__(self, Config.load(), service="kato")      # the name on the network, from when Kato was AirShare: unchanged so every version still finds the others
        self.init_clipboard()
        self.cfg.paused = False
        self.layout = layout            # {"cells": {name: [r, c]}, "master": name, "version": t}
        self.bridge = bridge
        if INPUT_SERVICE:
            w, h = drm_screen_size()
            self.bounds = (0, 0, w, h)       # driven-only: we report our size, we never read a display
            self.mouse = self.kb = None      # the service injects only through uinput, never pynput
            self.cursor = None
        else:
            self.bounds = screen_bounds()
            self.mouse = mouse.Controller()
            self.kb = keyboard.Controller()
            self.cursor = CursorHider()
        self.paused = False
        self.sizes = {}                 # machine name -> (w, h), as reported by each machine
        self.versions = {}              # machine name -> Kato version it reports
        self.peer_paused = {}           # machine name -> it has said its sharing is paused
        self.hw = hardware_ids()        # this machine's network-card addresses
        self.machine_hw = {}            # machine name -> its addresses, as reported
        self._stand_in = False          # Windows: our own pointer is showing (Windows hid its own)
        self.latest = None              # newest release on GitHub, once checked
        self._check_now = False         # look for an update at the next opportunity (the settings window opened)
        # Kato Pro: the group's licence key and when it was last changed. The newest change wins on every
        # machine, so entering, changing or removing the key on one computer does the same on all of them.
        self._licence_key, self._licence_at = self._load_licence()
        self._licence_cache = (None, None)
        self._sec_mods, self._sec_down = set(), {}   # Windows input service: shortcut keys held, keys it has down
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
        self._xfer_init()                    # large copies, see kato_files.py
        self._last_alive = 0.0
        self._busy = False              # a crossing is being set up
        self._input_on = False
        # driven-machine state
        self.driven_by = None           # link to the main computer while it's driving us
        self._held_keys = {}
        self._outq = queue.Queue()      # input callbacks never touch the network themselves
        threading.Thread(target=self._send_loop, daemon=True).start()
        self.xgrab = None if INPUT_SERVICE else (XGrabber() if LINUX else None)
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
        if not (WAYLAND or INPUT_SERVICE) or self.uinput:
            return bool(self.uinput)
        try:
            import importlib
            kato_uinput = importlib.import_module("kato_uinput")     # Linux-only: kept out of the Windows build
            if not kato_uinput.available():
                raise PermissionError("no access to /dev/uinput yet - run the Kato installer again to grant it")
            self.uinput = kato_uinput.UinputInjector(log)
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
        # One thread sends everything, in order. It must never die (nothing would be sent again, with no sign of
        # it), and a send that hangs holds up every machine behind it, so both are caught and said out loud.
        while True:
            link, header = self._outq.get()
            try:
                if link.alive:
                    t0 = time.time()
                    link.send(header)
                    if time.time() - t0 > 1.0:
                        log(f"a send to another machine took {time.time() - t0:.1f}s: everything queued behind it waited")
            except Exception as exc:
                log(f"a send to another machine failed: {exc}")

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
        if INPUT_SERVICE:
            return False       # the input service is driven-only: it is never the main computer
        return self.master() == self.cfg.name

    def kvm_on(self):
        return self.layout.get("kvm", True)

    def reachable(self, name):
        """Can the pointer go there right now? This machine always; another if it is connected, has reported
        its screen, and hasn't paused its sharing."""
        if name == self.cfg.name:
            return True
        return bool(self.link_to(name) and name in self.sizes and not self.peer_paused.get(name))

    def neighbour_of(self, name, side, skip=True):
        """The machine beyond that edge of a machine's screen.

        One that isn't sharing right now (switched off, disconnected or paused) is passed over, so the pointer
        carries on to the next machine in the same direction: with A, B and C in a row and B off, A's edge leads
        to C and C's leads back to A. An empty square still ends the row. If nothing that way can be reached,
        the nearest machine is returned, as it always was, and the caller finds it has no connection.
        """
        cells = self.cells()
        pos = cells.get(name)
        if not pos:
            return None
        dr, dc = STEP[side]
        at = {(p[0], p[1]): n for n, p in cells.items()}
        nearest, r, c = None, pos[0] + dr, pos[1] + dc
        while (r, c) in at:
            found = at[(r, c)]
            nearest = nearest or found
            if not skip or self.reachable(found):
                return found
            r, c = r + dr, c + dc
        return nearest

    def neighbour(self, side, skip=True):
        return self.neighbour_of(self.cfg.name, side, skip)

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

    # ---- Kato Pro: one licence for the whole group; the newest change wins everywhere ----
    def _load_licence(self):
        try:
            saved = json.loads((C.CONFIG_DIR / "licence.json").read_text())
            return licence.tidy(saved.get("key", "")), float(saved.get("at", 0))
        except (OSError, ValueError, AttributeError):
            pass
        try:                                              # 0.9.8 kept just the key
            old = C.CONFIG_DIR / "licence.txt"
            return licence.tidy(old.read_text()), old.stat().st_mtime
        except OSError:
            return "", 0.0

    def _save_licence(self):
        try:
            (C.CONFIG_DIR / "licence.json").write_text(json.dumps({"key": self._licence_key, "at": self._licence_at}))
            (C.CONFIG_DIR / "licence.txt").unlink(missing_ok=True)
        except OSError as exc:
            log(f"couldn't save the licence key: {exc}")

    def licence_info(self):
        """What the group's licence says, or None (Kato Free)."""
        if self._licence_cache[0] != self._licence_key:
            self._licence_cache = (self._licence_key, licence.read_licence(self._licence_key))
        return self._licence_cache[1]

    def allowed_computers(self):
        info = self.licence_info()
        return info["computers"] if info else licence.FREE_COMPUTERS

    def _licence_changed(self, key, at):
        self._licence_key, self._licence_at = key, at
        self._save_licence()
        self.share_state()
        self.refresh_ui()

    def set_licence(self, key):
        """A licence key typed in here. Returns what it says, or None if it isn't genuine (nothing changes)."""
        info = licence.read_licence(key)
        if not info:
            return None
        log(f"Kato Pro licence entered: up to {info['computers']} computers")
        self._licence_changed(info["key"], max(time.time(), self._licence_at + 1))
        return info

    def remove_licence(self):
        """Back to Kato Free, here and on the other computers."""
        log("Kato Pro licence removed: back to Kato Free")
        self._licence_changed("", max(time.time(), self._licence_at + 1))

    def _licence_from_peer(self, header):
        """A neighbour's view of the licence. Take it if it's newer than ours; put them right if ours is."""
        if "licence" not in header:
            return
        theirs = licence.tidy(header.get("licence") or "")
        try:
            their_at = float(header.get("licence_at") or 0)
        except (TypeError, ValueError):
            their_at = 0.0
        if theirs == self._licence_key:
            self._licence_at = max(self._licence_at, their_at)
            return
        newer = their_at > self._licence_at
        first = their_at == 0 and self._licence_at == 0 and not self._licence_key     # from a 0.9.8 machine
        if (newer or first) and (theirs == "" or licence.read_licence(theirs)):
            log("Kato Pro licence " + ("picked up from" if theirs else "removed on") + " another computer")
            self._licence_changed(theirs, their_at)
        elif self._licence_at > their_at:
            self.share_state()

    def share_state(self):
        bx, by, bw, bh = self.bounds
        for link in list(self.links.values()):
            if link.alive:
                self._send(link, {"type": "screen", "name": self.cfg.name, "w": bw, "h": bh, "version": VERSION,
                                  "hw": self.hw, "licence": self._licence_key, "licence_at": self._licence_at,
                                  "paused": bool(self.paused)})
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
        if INPUT_SERVICE:
            return             # driven-only: no edge-watching, no capture, ever
        want = self.is_master() and self.kvm_on()
        if WAYLAND:
            # Wayland hides the keyboard and mouse from ordinary programs; the desktop's own capture service
            # stands in for the X11 listeners (see kato_capture.py).
            if want and not self.capture:
                import importlib
                cap = importlib.import_module("kato_capture").WaylandCapture(self, log, BACK_OFF, (ESCAPE_PRESSES, ESCAPE_WINDOW))
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
            self._ctl_since, self._ctl_moves = time.time(), 0
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
            was, since, moves = self.active, getattr(self, "_ctl_since", time.time()), getattr(self, "_ctl_moves", 0)
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
            log(f"back home ({time.time() - since:.0f}s on {was}, {moves} pointer updates sent)")
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
                    self._ctl_moves = getattr(self, "_ctl_moves", 0) + 1
                    try:
                        self._move_virtual(dx, dy)
                    except Exception as exc:                  # never leave the input held with nothing forwarding it
                        log(f"pointer forwarding failed, coming home to be safe: {exc}")
                        threading.Thread(target=self._home_safely, daemon=True).start()
                        return
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
        if self.on_transfer_frame(link, header):              # two machines agreeing a large copy
            return
        bx, by, bw, bh = self.bounds
        if t == "screen":
            name = self.peers.get(link.peer_id, {}).get("name") or str(header.get("name", ""))
            if name:
                self.sizes[name] = (int(header.get("w", 1920)), int(header.get("h", 1080)))
                self.versions[name] = str(header.get("version", "old"))
                self._licence_from_peer(header)
                self.peer_paused[name] = bool(header.get("paused"))
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
                # Older versions could stamp a stale layout as brand new when a machine changed name, and
                # push it over everyone's. Their layouts are left alone until they are updated.
                if sender not in self._layout_ignored:
                    self._layout_ignored.add(sender)
                    log(f"not taking the layout from {sender or 'an older machine'} until it is updated "
                        f"(it runs Kato {self.versions.get(sender, 'an old version')})")
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
                        f"refused. Open Layout on the one you want as main and press Apply.")
                    self.notify(f"{who} and this machine are both set as the main computer. "
                                f"Open Layout on the one you want as main and press Apply.")
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
                log(f"no longer being driven ({getattr(self, '_drv_moves', 0)} pointer updates received)")
                self._drv_moves = 0
                self._release_all()
                self.driven_by = None
                self.refresh_ui()
        elif t == "kvm_pos" and self.driven_by is link:
            self._drv_moves = getattr(self, "_drv_moves", 0) + 1
            x, y = bx + int(header.get("x", 0)), by + int(header.get("y", 0))
            if WINDOWS:
                try:
                    if needs_service() and SECURE_INPUT.send({"t": "move", "x": x, "y": y}):
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
            if WINDOWS and needs_service() and SECURE_INPUT.send(
                    {"t": "btn", "button": name, "down": bool(header.get("down"))}):
                return
            btn = getattr(mouse.Button, name, None)
            if btn:
                (self.mouse.press if header.get("down") else self.mouse.release)(btn)
        elif t == "kvm_scroll" and self.driven_by is link:
            dx, dy = int(header.get("dx", 0)), int(header.get("dy", 0))
            if WINDOWS and needs_service() and SECURE_INPUT.send({"t": "scroll", "dx": dx, "dy": dy}):
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
            # a key comes up by the same route it went down, even if the window in front has changed since
            if WINDOWS and ((needs_service() and ident not in self._held_keys) or ident in self._sec_down) \
                    and self._secure_key(header.get("key") or {}, bool(header.get("down")), ident):
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

    _SHORTCUT_KEYS = (0x11, 0x12, 0x5B, 0x5C)                       # Ctrl, Alt, either Windows key

    def _secure_key(self, wire, down, ident=""):
        """Relay one key press/release to the input service. wire is the on-the-wire key dict:
        {"name": "enter"} a named key, {"char": "a"} a character, or {"vk": 65} a raw virtual key.

        Characters are normally typed as themselves, which is right whatever keyboard layout this machine has.
        But Windows only recognises a shortcut (Ctrl+C, Alt+F...) from a real key code, so while Ctrl, Alt or a
        Windows key is held the character goes as the key that produces it here. A key goes up the same way it
        went down, whatever has been released in between."""
        if not down and ident in self._sec_down:
            return SECURE_INPUT.send(dict(self._sec_down.pop(ident), down=False))
        cmd = None
        if "name" in wire:
            name = str(wire["name"]).lower()
            vk = self._VK.get(name)
            if name.startswith(("ctrl", "alt", "cmd")) and name != "alt_gr":
                (self._sec_mods.add if down else self._sec_mods.discard)(name)
            cmd = {"t": "key", "vk": vk} if vk else None
        elif wire.get("char"):
            ch = wire["char"][0]
            cmd = {"t": "key", "char": ord(ch)}
            try:
                import ctypes
                u = ctypes.windll.user32
                held = {vk for vk in self._SHORTCUT_KEYS if u.GetAsyncKeyState(vk) & 0x8000}
                alt_gr = 0x11 in held and 0x12 in held                # AltGr is Ctrl+Alt: that is typing, not a shortcut
                if (held or self._sec_mods) and not alt_gr:
                    found = u.VkKeyScanW(ord(ch)) & 0xFFFF
                    if found != 0xFFFF and found & 0xFF:
                        cmd = {"t": "key", "vk": found & 0xFF}
            except Exception:
                pass
        elif "vk" in wire:
            cmd = {"t": "key", "vk": int(wire["vk"])}
        if cmd is None:
            return False
        if down and ident:
            self._sec_down[ident] = cmd
        return SECURE_INPUT.send(dict(cmd, down=down))

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
        for cmd in list(self._sec_down.values()):                 # and anything the Windows input service has down
            SECURE_INPUT.send(dict(cmd, down=False))
        self._sec_down.clear()
        self._sec_mods.clear()

    def on_link_closed(self, link, why):
        if self.active and self.link_to(self.active) in (None, link):
            threading.Thread(target=self._home_safely, daemon=True).start()
        if self.driven_by is link:
            log(f"the machine driving this one disconnected ({getattr(self, '_drv_moves', 0)} pointer updates received)")
            self._drv_moves = 0
            self._release_all()
            self.driven_by = None
        Mesh.on_link_closed(self, link, why)

    # ---- status ----
    def trouble(self):
        """Something has stopped this computer sharing its keyboard and mouse. The log has the details."""
        return bool(WAYLAND and self.is_master() and self.kvm_on() and self.capture and self.capture.error
                    and not self.paused)

    def status_line(self):
        if self.transfer_line():
            return self.transfer_line()
        if self.status:
            return self.status
        if self.paused:
            return "Paused"
        if time.time() - self._two_mains_at < 20:
            return "Two machines are set as the main computer - open Layout on the one you want and press Apply"
        if self.trouble():
            return TROUBLE
        if WAYLAND and not self.uinput and self.kvm_on():
            return "Run the Kato installer again so this computer can be controlled"
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
        if self.update_available() or self.newer_peers():
            newer = self.newer_peers()
            return ("A newer Kato is available" + (f" ({', '.join(newer)} already has it)" if newer else "")
                    + ": open Layout and press Upgrade")
        if not self.kvm_on():
            return f"Clipboard sharing - {n} machine{'s' if n != 1 else ''} connected"
        role = "Main computer" if self.is_master() else f"Main computer: {self.master()}"
        return f"{role} - {n} machine{'s' if n != 1 else ''} connected"

    def connected_names(self):
        return [p["name"] for pid, p in self.peers.items() if self.links.get(pid) and self.links[pid].alive]

    def out_of_date(self):
        """Connected machines running a different Kato version from the newest one in the group."""
        names = self.connected_names()
        everyone = {self.cfg.name: VERSION, **{n: self.versions.get(n, "old") for n in names}}
        newest = max(everyone.values(), key=vtuple)
        return sorted(n for n, v in everyone.items() if vtuple(v) < vtuple(newest))

    def update_available(self):
        return bool(self.latest) and vtuple(self.latest) > vtuple(VERSION)

    def newer_peers(self):
        """Machines on this network already running a newer Kato than this one."""
        return sorted(n for n, v in list(self.versions.items()) if vtuple(v) > vtuple(VERSION))

    def update_loop(self):
        checked = 0.0
        while self.running:
            since = time.time() - checked
            peers_ahead = any(vtuple(v) > vtuple(self.latest or VERSION) for v in list(self.versions.values()))
            if since > UPDATE_CHECK_EVERY or (since > 60 and (self._check_now or peers_ahead)):
                checked, self._check_now = time.time(), False
                v = latest_version()
                if v:
                    if v != self.latest and vtuple(v) > vtuple(VERSION):
                        log(f"update available: {v}")
                    self.latest = v
                    self.refresh_ui()
            time.sleep(5)

    def upgrade(self):
        kind = install_kind()
        log(f"upgrade requested ({kind} install)")
        if kind == "store":
            webbrowser.open(STORE_URL)
        elif kind == "windows":
            try:
                path = os.path.join(tempfile.gettempdir(), "Kato-Setup.exe")
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
        self.share_state()                         # so the others pass this machine over while it is paused
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


def edge_colours(widget):
    """The frame colour and a fainter one for disabled things: dark grey on a light desktop, lighter on a dark one."""
    dark = widget.palette().window().color().lightness() < 128
    return ("#9a9a9a", "#666666") if dark else ("#555555", "#b5b5b5")


def button_style(widget):
    edge, faint = edge_colours(widget)
    return (f"QPushButton {{ border: 1px solid {edge}; border-radius: 6px; padding: 5px 14px; background: palette(button); }} "
            "QPushButton:hover { background: palette(midlight); } QPushButton:pressed { background: palette(mid); } "
            f"QPushButton:disabled {{ color: {faint}; border-color: {faint}; }}")


class Framed:
    """Mixed into every Kato window and pop-up: draws a thin, slightly rounded frame just inside the edge.
    On a white desktop a white window otherwise has no visible boundary at all."""
    RADIUS = 8

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(edge_colours(self)[0]), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)
        painter.end()


class Box(Framed, QMessageBox):
    def __init__(self, parent=None, title="Kato", text="", icon=QMessageBox.NoIcon):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setText(text)
        self.setIcon(icon)
        self.setStyleSheet(button_style(self))


def tell(parent, text, title="Kato", icon=QMessageBox.Information):
    Box(parent, title, text, icon).exec()


def ask(parent, text, title="Kato"):
    box = Box(parent, title, text, QMessageBox.Question)
    yes = box.addButton("Yes", QMessageBox.YesRole)
    box.setDefaultButton(box.addButton("No", QMessageBox.NoRole))
    box.exec()
    return box.clickedButton() is yes


class KeyDialog(Framed, QDialog):
    """Where a licence key is pasted. A key is one long unbroken run of characters, so it wraps anywhere."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kato Pro")
        self.setStyleSheet(button_style(self))
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.addWidget(QLabel("Paste the licence key from your email:"))
        self.edit = QPlainTextEdit()
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.edit.setFont(mono)
        self.edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.edit.setWordWrapMode(QTextOption.WrapAnywhere)
        self.edit.setTabChangesFocus(True)
        metrics = self.edit.fontMetrics()
        self.edit.setFixedSize(metrics.horizontalAdvance("M") * 46 + 28, metrics.lineSpacing() * 4 + 18)
        v.addWidget(self.edit)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)

    def key(self):
        return self.edit.toPlainText()


def about_dialog(parent=None):
    box = Box(parent)
    box.setWindowTitle("About Kato")
    box.setTextFormat(Qt.RichText)
    box.setText(
        f"<h3>Kato {VERSION}</h3>"
        "<p>One keyboard and mouse across all your computers, with a shared clipboard.<br>"
        "Windows and Linux, X11 and Wayland. Free for up to three computers.</p>"
        "<p>Created by Steve Wallis, Wallisoft.<br>"
        "Designed and built with Claude, by Anthropic.</p>"
        '<p><a href="https://juska.uk">juska.uk</a> &nbsp;\u00b7&nbsp; '
        '<a href="https://tiny-web.uk">tiny-web.uk</a></p>'
        "<p><small>The interface uses Qt under the LGPL; see the third-party notices installed with Kato."
        "</small></p>")
    box.exec()


def enter_licence_dialog(app, parent=None):
    """Ask for a licence key and apply it. True if a genuine key was entered."""
    dialog = KeyDialog(parent)
    if dialog.exec() != QDialog.Accepted or not dialog.key().strip():
        return False
    info = app.set_licence(dialog.key())
    if not info:
        tell(parent, "That isn't a valid Kato licence key.\n\n"
             "Paste the whole key from your email. It starts with ASP1-.", icon=QMessageBox.Warning)
        return False
    tell(parent, f"Kato Pro is active for up to {info['computers']} computers.\n\n"
         "Your other computers will pick it up by themselves.", "Kato Pro")
    return True


def licence_dialog(app, parent=None):
    """Show the licence key, with the choice to copy it, change it, or remove it and go back to Free."""
    info = app.licence_info()
    if not info:
        enter_licence_dialog(app, parent)
        return
    issued = info.get("issued", "")
    if len(issued) == 8 and issued.isdigit():
        issued = f"{issued[6:8]}/{issued[4:6]}/{issued[0:4]}"
    box = Box(parent, "Kato Pro")
    box.setTextFormat(Qt.RichText)
    box.setTextInteractionFlags(Qt.TextSelectableByMouse)
    box.setText(f"<b>Kato Pro</b>: up to {info['computers']} computers<br>"
                f"Licence {info.get('id', '')}" + (f", issued {issued}" if issued else "")
                + f"<p style='font-family:monospace'>{'<br>'.join(info['key'][i:i + 44] for i in range(0, len(info['key']), 44))}</p>")
    copy = box.addButton("Copy key", QMessageBox.ActionRole)
    change = box.addButton("Change key...", QMessageBox.ActionRole)
    remove = box.addButton("Remove key", QMessageBox.DestructiveRole)
    box.addButton("Close", QMessageBox.RejectRole)
    box.exec()
    clicked = box.clickedButton()
    if clicked is copy:
        QApplication.clipboard().setText(info["key"])
    elif clicked is change:
        enter_licence_dialog(app, parent)
    elif clicked is remove:
        if ask(parent, "Remove the licence key from all your computers and go back to Kato Free "
               f"(up to {licence.FREE_COMPUTERS} computers)?\n\nKeep a copy of the key: you can enter it again at any time."):
            app.remove_licence()


def view_log():
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(C.LOG_FILE)))


class SettingsWindow(Framed, QWidget):
    def __init__(self, app: Kato, extras=None):
        super().__init__()
        self.app = app
        self.extras = extras              # the tray's extra menu items, offered here on desktops with no tray
        self.setWindowTitle("Kato - layout and settings")
        body = QWidget()
        v = QVBoxLayout(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)                     # room for the rounded frame Framed draws
        self.setStyleSheet(button_style(self))
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

        self.licence_label = QLabel()
        v.addWidget(self.licence_label)
        self.status = QLabel()
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        buttons = QHBoxLayout()
        self.more_btn = QPushButton("More")                      # only where there is no tray menu to hold these
        self.more_btn.clicked.connect(self.show_more)
        self.more_btn.setVisible(bool(extras) and not QSystemTrayIcon.isSystemTrayAvailable())
        buttons.addWidget(self.more_btn)
        buttons.addStretch(1)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setToolTip("Use this layout now and share it with your other machines. The window stays open.")
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self.save)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        buttons.addWidget(close)
        buttons.addWidget(self.apply_btn)
        v.addLayout(buttons)
        self._dirty_names = None
        self.reload()
        self.watch = QTimer(self)                                # Apply is only live when there is something to apply
        self.watch.timeout.connect(self.update_apply)
        self.watch.start(250)

    def changed(self):
        a = self.app
        placed = lambda cells: {n: tuple(pos) for n, pos in cells.items()}
        return (placed(self.grid.cells) != placed(a.cells())
                or self.kvm_box.isChecked() != bool(a.kvm_on())
                or (self.kvm_box.isChecked() and self.master_box.currentText() != (a.master() or a.cfg.name))
                or self.passphrase.text().strip() != a.cfg.passphrase)

    def update_apply(self):
        if self.isVisible():
            self.apply_btn.setEnabled(self.changed())

    def show_more(self):
        menu = QMenu(self)
        for kind, text, enabled, action in (self.extras() if self.extras else []):
            if kind == "sep":
                menu.addSeparator()
            else:
                menu.addAction(text, action).setEnabled(enabled)
        menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().topLeft()))

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
        if hasattr(self, "apply_btn"):
            self.apply_btn.setEnabled(self.changed())

    def fit_to_screen(self):
        """Open at the size the content wants, but never taller than the screen (it scrolls instead)."""
        want = self.body.sizeHint()
        avail = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        margins = self.layout().contentsMargins()
        w = min(want.width() + margins.left() + margins.right() + 24, avail.width() - 40)
        h = min(want.height() + margins.top() + margins.bottom() + 20, avail.height() - 60)
        self.resize(max(w, 420), max(h, 300))

    def show_version(self):
        a = self.app
        kind = {"store": "Microsoft Store", "windows": "Windows installer", "linux": "Linux",
                "source": "running from source"}[install_kind()]
        latest = a.latest or "checking..."
        newer = a.newer_peers()
        if newer and not a.update_available():                   # another machine has it before GitHub has told us
            latest = f"{max((a.versions[n] for n in newer), key=vtuple)} (on {', '.join(newer)})"
        self.version_label.setText(f"This machine: <b>{VERSION}</b> ({kind}) \u00b7 Latest: <b>{latest}</b>")
        self.upgrade_btn.setVisible(a.update_available())
        self.upgrade_btn.setText("Open the Store" if install_kind() == "store" else f"Upgrade to {a.latest}")
        pro = a.licence_info()
        self.licence_label.setText(f"<b>Kato Pro</b>: up to {pro['computers']} computers" if pro
                                   else f"Kato Free: up to {licence.FREE_COMPUTERS} computers")

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
            tell(self, f"The passphrase needs at least {MIN_PASSPHRASE} characters.", icon=QMessageBox.Warning)
            return
        if self.app.cfg.name not in self.grid.cells:
            tell(self, "Place this machine in the grid too, so Kato knows where it sits.", icon=QMessageBox.Warning)
            return
        master = self.master_box.currentText()
        if master not in self.grid.cells:
            tell(self, f"Place the main computer ({master}) in the grid too.", icon=QMessageBox.Warning)
            return
        allowed, placed = self.app.allowed_computers(), len(self.grid.cells)
        if placed > allowed:
            pro = self.app.licence_info()
            box = Box(self, icon=QMessageBox.Information)
            box.setText(f"You have placed {placed} computers.\n\n" + (
                f"Your licence covers up to {allowed}." if pro else
                f"Kato Free shares a keyboard and mouse between up to {licence.FREE_COMPUTERS} computers. "
                "Kato Pro covers up to 15, for a one-off payment."))
            buy = None if pro else box.addButton("Get Pro", QMessageBox.AcceptRole)
            key = box.addButton("Enter licence key", QMessageBox.ActionRole)
            box.addButton("Not now", QMessageBox.RejectRole)
            box.exec()
            if buy is not None and box.clickedButton() is buy:
                webbrowser.open(licence.BUY_URL)
            elif box.clickedButton() is key:
                enter_licence_dialog(self.app, self)
                self.show_version()
            return
        self.app.set_layout(dict(self.grid.cells), master, self.kvm_box.isChecked())
        self.grid.master = master
        if passphrase != self.app.cfg.passphrase:
            self.app.cfg.mode, self.app.cfg.passphrase = "local", passphrase
            self.app.cfg.save()
            self.app.reset_for_mode_change()
        self.status.setText("Applied, and shared with your other machines.")
        self.apply_btn.setEnabled(False)


class StandInPointer(QWidget):
    """Windows hides its pointer on a PC with no mouse attached. While another machine drives
    this one, Kato draws its own arrow there instead: always on top, click-through,
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
    def __init__(self, app: Kato, qapp: QApplication):
        self.app, self.qapp = app, qapp
        self.settings = None
        self.icon = QSystemTrayIcon(dot_icon(ORANGE))
        self.menu = QMenu()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self.on_activated)
        app.bridge.refresh.connect(self.refresh)
        app.bridge.notify.connect(lambda m: self.icon.showMessage("Kato", m, QSystemTrayIcon.Information, 6000))
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
            log("this desktop has no tray: opening the settings window instead (open Kato from the app menu to see it again)")
            QTimer.singleShot(1200, self.open_settings)

    def on_activated(self, reason):
        # A plain click shows the menu, the same as a right-click. Linux desktops do this themselves; on Windows a
        # left click reaches us and used to pause sharing, which was easy to do by accident. Pause is in the menu.
        if reason == QSystemTrayIcon.Trigger:
            self.refresh()
            self.menu.popup(QCursor.pos())
            self.menu.activateWindow()                 # so clicking elsewhere closes it

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
        colour, tip = a.colour(), f"Kato - {a.status_line()}"
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
                    item.triggered.connect(self.launch_done)       # (whatever it does, GNOME's spinner stops)
                self.menu.addAction(item)
        if self.settings and self.settings.isVisible():
            self.settings.tick()

    def menu_spec(self):
        """What the tray menu should show right now: (kind, text, enabled, action)."""
        a = self.app
        status = a.status_line()
        spec = [("item", "Resume sharing" if a.paused else "Pause sharing", True, a.toggle_pause),
                ("item", status, True, view_log) if status == TROUBLE else ("item", status, False, None),
                ("sep", "", False, None)]
        for side in ("left", "right", "up", "down"):
            where = "above" if side == "up" else "below" if side == "down" else side
            nearest, used = a.neighbour(side, skip=False), a.neighbour(side)
            for name in dict.fromkeys(n for n in (nearest, used) if n):
                on = a.reachable(name)
                note = " \u00b7 paused" if a.peer_paused.get(name) and a.link_to(name) else ""
                past = f", past {nearest}" if name != nearest else ""
                ver = a.versions.get(name)
                mark = "\u25cf" if on else "\u25cb"
                spec.append(("item", f"{mark} {name} ({where}{past}){note}" + (f" \u00b7 {ver}" if ver else ""), False, None))
        spec.append(("sep", "", False, None))
        if a.update_available():
            spec.append(("item", f"Update available: {a.latest}...", True, a.upgrade))
        spec.append(("item", "Layout and settings...", True, self.open_settings))
        spec += self.extra_spec()
        spec += [("sep", "", False, None), ("item", "Quit", True, self.quit)]
        return spec

    def extra_spec(self):
        """Licence, log and About: in the tray menu, and behind the window's More button where there is no tray."""
        a = self.app
        parent = lambda: self.settings if self.settings and self.settings.isVisible() else None
        if a.licence_info():
            spec = [("item", "View key...", True, lambda: licence_dialog(a, parent()))]
        else:
            spec = [("item", "Get Pro...", True, lambda: webbrowser.open(licence.BUY_URL)),
                    ("item", "Enter key...", True, lambda: enter_licence_dialog(a, parent()))]
        return spec + [("item", "View log", True, view_log), ("item", "About Kato", True, lambda: about_dialog(parent()))]

    def open_settings(self):
        if not self.settings:
            self.settings = SettingsWindow(self.app, self.extra_spec)
        else:
            self.settings.reload()
        self.app._check_now = True                                # a good moment to look for an update
        self.settings.fit_to_screen()
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()
        x11_activate(self.settings.winId())
        # GNOME's tray starts a "launching" spinner when its menu opens a window; finish it,
        # now and again shortly after in case the announcement arrives a moment late.
        self.startup.complete()
        QTimer.singleShot(1500, self.startup.complete)

    def launch_done(self):
        """Picking anything on the tray menu makes GNOME (on Xorg) start a "launching" spinner that waits for a
        window; most items open none (Pause, Quit...), so tell it we're done: now, and again in case its
        announcement arrives a moment later."""
        self.startup.complete()
        QTimer.singleShot(400, self.startup.complete)
        QTimer.singleShot(1500, self.startup.complete)

    def quit(self):
        self.app.leave_remote()
        if self.app.capture:
            self.app.capture.stop()
        self.app.stop()
        self.icon.hide()
        # finish GNOME's spinner before going (the menu pick that started it is ours to answer), then quit a
        # moment later, so the panel hears back about the click and doesn't spin until it gives up
        self.startup.complete()
        QTimer.singleShot(250, self.startup.complete)
        QTimer.singleShot(400, self.qapp.quit)


def send_to_running(cmd: str) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=1) as s:
            s.sendall((cmd + "\n").encode())
            return s.recv(8).startswith(b"ok")
    except OSError:
        return False


def run_input_service(a):
    """Headless, usually as root at boot: create the virtual keyboard and mouse and be driven, so the login
    screen and machines with nobody logged in can be controlled. No tray, no clipboard, offscreen Qt. The
    passphrase and name come from the unit's environment (KATO_PASSPHRASE, KATO_NAME) or the saved config."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    set_app("Kato", VERSION)
    C.DEFAULT_PORT, C.BEACON_PORT = PORT, BEACON_PORT
    C.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    layout_file = C.CONFIG_DIR / "layout.json"
    layout = json.loads(layout_file.read_text()) if layout_file.exists() else {}
    if "cells" not in layout:
        layout = {"cells": {}, "version": 0}
    layout.setdefault("master", None)
    layout.setdefault("kvm", True)

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    bridge = Bridge()
    app = Kato(layout, bridge)
    app.cfg.name = a.name or os.environ.get("KATO_NAME") or app.cfg.name
    pw = a.passphrase or os.environ.get("KATO_PASSPHRASE")
    if pw:
        app.cfg.passphrase = pw
    app.cfg.mode, app.cfg.port = "local", PORT
    app.cfg.save()
    app._set_local_key()

    app.start()
    app.apply_role()                     # a client installs no listeners; this is a no-op here
    threading.Thread(target=app.layout_loop, daemon=True).start()
    if app.uinput:
        log(f"input service: ready as '{app.cfg.name}', virtual keyboard and mouse up, screen {app.bounds[2]}x{app.bounds[3]}")
    else:
        log(f"input service: WARNING - no /dev/uinput, nothing can be injected ({app.uinput_error or 'is the uinput module loaded?'})")

    import signal
    def _bye(*_):
        try:
            if app.uinput:
                app.uinput.release_all()
        except Exception:
            pass
        QTimer.singleShot(0, qapp.quit)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _bye)
    tick = QTimer()                       # let Python notice the signals
    tick.timeout.connect(lambda: None)
    tick.start(250)
    sys.exit(qapp.exec())


def main():
    def thread_crashed(args):                                 # a background thread that dies must not die silently
        log(f"internal error in a background thread ({getattr(args.thread, 'name', '?')}): "
            f"{getattr(args.exc_type, '__name__', args.exc_type)}: {args.exc_value}")
    threading.excepthook = thread_crashed
    ap = argparse.ArgumentParser(description="Kato: one keyboard and mouse across your machines")
    ap.add_argument("--passphrase", help="shared passphrase (same on every machine, 8+ characters)")
    ap.add_argument("--name", help="this machine's name (default: its hostname)")
    ap.add_argument("--quit", action="store_true", help="quit the running copy")
    ap.add_argument("--input-service", action="store_true",
                    help="run headless as the input service (drive the login screen); for the system service")
    a = ap.parse_args()

    if a.input_service:
        run_input_service(a)
        return

    if send_to_running("quit" if a.quit else "settings") or a.quit:
        return

    set_app("Kato", VERSION)
    C.DEFAULT_PORT, C.BEACON_PORT = PORT, BEACON_PORT
    adopted = adopt_old_settings()
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
    app = Kato(layout, bridge)
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
    log(f"Kato {VERSION} as '{app.cfg.name}', screen {app.bounds[2]}x{app.bounds[3]}")
    if adopted:
        log("settings, layout and licence carried over from AirShare, as Kato used to be called")
    if not app._local_key or not app.cells() or not app.master():
        QTimer.singleShot(1500, tray.open_settings)
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
