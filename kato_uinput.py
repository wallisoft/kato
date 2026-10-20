"""
Kato on Wayland: being driven, through the kernel's uinput.

Wayland stops one program placing the pointer or pressing keys in another, which is how Kato is
driven on X11. So here Kato creates two virtual devices one level below the desktop - an absolute
pointer (the kind a virtual machine's "tablet" mouse is) and a keyboard. The desktop treats them as
real hardware, on any Wayland compositor, with no permission pop-ups.

That needs write access to /dev/uinput, which the installer grants to the logged-in user with one
udev rule. No extra Python packages: plain ioctls, and libxkbcommon (present wherever Wayland is)
to turn characters into the right keys for this machine's keyboard layout.
"""
import ctypes
import ctypes.util
import fcntl
import json
import os
import re
import struct
import subprocess
import threading
import time

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
SYN_REPORT = 0
REL_HWHEEL, REL_WHEEL = 6, 8
ABS_X, ABS_Y = 0, 1
BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}
BUS_VIRTUAL = 0x06
ABS_TOP = 65535                      # pointer positions are sent as a fraction of the screen, not in pixels
UINPUT = "/dev/uinput"


def _iow(nr, size=4):
    return (1 << 30) | (size << 16) | (ord("U") << 8) | nr


UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT, UI_SET_ABSBIT = _iow(100), _iow(101), _iow(102), _iow(103)
UI_DEV_CREATE, UI_DEV_DESTROY = (ord("U") << 8) | 1, (ord("U") << 8) | 2

KEY_LEFTSHIFT, KEY_RIGHTSHIFT, KEY_RIGHTALT = 42, 54, 100
SHIFTS = (KEY_LEFTSHIFT, KEY_RIGHTSHIFT)
SHORTCUT_MODS = (29, 97, 56, 125, 126)          # ctrl, ctrl_r, alt, super, super_r: a shortcut is being typed

# pynput's key names (what travels on the wire) -> kernel key codes
NAMED = {
    "alt": 56, "alt_l": 56, "alt_r": 100, "alt_gr": 100, "backspace": 14, "caps_lock": 58,
    "cmd": 125, "cmd_l": 125, "cmd_r": 126, "ctrl": 29, "ctrl_l": 29, "ctrl_r": 97,
    "delete": 111, "down": 108, "end": 107, "enter": 28, "esc": 1, "home": 102, "left": 105,
    "page_down": 109, "page_up": 104, "right": 106, "shift": 42, "shift_l": 42, "shift_r": 54,
    "space": 57, "tab": 15, "up": 103, "insert": 110, "menu": 127, "num_lock": 69, "pause": 119,
    "print_screen": 99, "scroll_lock": 70,
    "media_play_pause": 164, "media_volume_mute": 113, "media_volume_down": 114,
    "media_volume_up": 115, "media_previous": 165, "media_next": 163,
    **{f"f{n}": 58 + n for n in range(1, 11)}, "f11": 87, "f12": 88,
    **{f"f{n}": 170 + n for n in range(13, 21)},
}
KP = {0: 82, 1: 79, 2: 80, 3: 81, 4: 75, 5: 76, 6: 77, 7: 71, 8: 72, 9: 73}
# keys that arrive as a bare number: X11 keysyms from a Linux main computer...
X_KEYSYM = {
    **{0xFFB0 + n: code for n, code in KP.items()},
    0xFFAE: 83, 0xFFAB: 78, 0xFFAD: 74, 0xFFAA: 55, 0xFFAF: 98, 0xFF8D: 96,
    0xFF95: 71, 0xFF96: 75, 0xFF97: 72, 0xFF98: 77, 0xFF99: 80, 0xFF9A: 73, 0xFF9B: 81, 0xFF9C: 79,
    0xFF9D: 76, 0xFF9E: 82, 0xFF9F: 83, 0xFE03: 100,
}
# ...or Windows virtual-key codes from a Windows one (the number pad)
WIN_VK = {**{0x60 + n: code for n, code in KP.items()}, 0x6A: 55, 0x6B: 78, 0x6D: 74, 0x6E: 83, 0x6F: 98}


def available():
    """Can this user create virtual input devices? (False until the installer's udev rule is in place.)"""
    return os.access(UINPUT, os.W_OK)


def detect_layout():
    """This machine's keyboard layout as (layout, variant), e.g. ("gb", ""). Falls back to US."""
    def split(s):
        s = s.strip().split(",")[0]
        layout, _, variant = s.partition("+")
        return (layout or "us", variant)

    env = os.environ.get("XKB_DEFAULT_LAYOUT")
    if env:
        return split(env)[0], os.environ.get("XKB_DEFAULT_VARIANT", "").split(",")[0]
    for key in ("mru-sources", "sources"):                     # GNOME and its relatives
        try:
            out = subprocess.run(["gsettings", "get", "org.gnome.desktop.input-sources", key],
                                 capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"\('xkb',\s*'([^']+)'\)", out)
            if m:
                return split(m.group(1))
        except Exception:
            pass
    try:                                                        # systemd's record (KDE and most others)
        out = subprocess.run(["localectl", "status"], capture_output=True, text=True, timeout=3).stdout
        lay = re.search(r"X11 Layout:\s*(\S+)", out)
        var = re.search(r"X11 Variant:\s*(\S+)", out)
        if lay:
            return lay.group(1).split(",")[0], (var.group(1).split(",")[0] if var else "")
    except Exception:
        pass
    try:
        text = open("/etc/default/keyboard", encoding="utf-8").read()
        lay = re.search(r'^XKBLAYOUT="?([^"\n]+)', text, re.M)
        var = re.search(r'^XKBVARIANT="?([^"\n]*)', text, re.M)
        if lay:
            return lay.group(1).split(",")[0], (var.group(1).split(",")[0] if var else "")
    except Exception:
        pass
    return "us", ""


class _RuleNames(ctypes.Structure):
    _fields_ = [(n, ctypes.c_char_p) for n in ("rules", "model", "layout", "variant", "options")]


def build_keymap(layout="us", variant=""):
    """{character: (kernel key code, level)} for a keyboard layout. Level 0 = the key alone, 1 = with Shift,
    2 = with AltGr, 3 = with both. Each character gets the simplest way of typing it."""
    path = ctypes.util.find_library("xkbcommon")
    if not path:
        raise RuntimeError("libxkbcommon is not installed")
    xkb = ctypes.CDLL(path)
    xkb.xkb_context_new.restype = ctypes.c_void_p
    xkb.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    xkb.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RuleNames), ctypes.c_int]
    xkb.xkb_keymap_key_get_syms_by_level.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                                     ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32))]
    xkb.xkb_keysym_to_utf32.argtypes = [ctypes.c_uint32]
    xkb.xkb_keysym_to_utf32.restype = ctypes.c_uint32
    xkb.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
    xkb.xkb_context_unref.argtypes = [ctypes.c_void_p]
    ctx = xkb.xkb_context_new(0)
    if not ctx:
        raise RuntimeError("xkbcommon: no context")
    names = _RuleNames(None, None, layout.encode(), variant.encode() or None, None)
    keymap = xkb.xkb_keymap_new_from_names(ctx, ctypes.byref(names), 0)
    if not keymap:
        xkb.xkb_context_unref(ctx)
        raise RuntimeError(f"xkbcommon doesn't know the layout '{layout}{'+' + variant if variant else ''}'")
    chars = {}
    syms = ctypes.POINTER(ctypes.c_uint32)()
    for level in range(4):
        for keycode in range(9, 256):                           # XKB key codes are the kernel's plus 8
            if xkb.xkb_keymap_key_get_syms_by_level(keymap, keycode, 0, level, ctypes.byref(syms)) != 1:
                continue
            cp = xkb.xkb_keysym_to_utf32(syms[0])
            if cp >= 32 and cp != 127:
                chars.setdefault(chr(cp), (keycode - 8, level))
    xkb.xkb_keymap_unref(keymap)
    xkb.xkb_context_unref(ctx)
    return chars


def build_levels(layout="us", variant=""):
    """The other way round from build_keymap: {(kernel key code, level): character}, for working out which
    character a physical key produces on this machine's layout (a Wayland main computer is told keys, not text)."""
    path = ctypes.util.find_library("xkbcommon")
    if not path:
        raise RuntimeError("libxkbcommon is not installed")
    xkb = ctypes.CDLL(path)
    xkb.xkb_context_new.restype = ctypes.c_void_p
    xkb.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    xkb.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RuleNames), ctypes.c_int]
    xkb.xkb_keymap_key_get_syms_by_level.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                                     ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32))]
    xkb.xkb_keysym_to_utf32.argtypes = [ctypes.c_uint32]
    xkb.xkb_keysym_to_utf32.restype = ctypes.c_uint32
    xkb.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
    xkb.xkb_context_unref.argtypes = [ctypes.c_void_p]
    ctx = xkb.xkb_context_new(0)
    names = _RuleNames(None, None, layout.encode(), variant.encode() or None, None)
    keymap = xkb.xkb_keymap_new_from_names(ctx, ctypes.byref(names), 0) if ctx else None
    if not keymap:
        raise RuntimeError(f"xkbcommon doesn't know the layout '{layout}'")
    levels = {}
    syms = ctypes.POINTER(ctypes.c_uint32)()
    for keycode in range(9, 256):
        for level in range(4):
            if xkb.xkb_keymap_key_get_syms_by_level(keymap, keycode, 0, level, ctypes.byref(syms)) == 1:
                cp = xkb.xkb_keysym_to_utf32(syms[0])
                if cp >= 32 and cp != 127:
                    levels[(keycode - 8, level)] = chr(cp)
    xkb.xkb_keymap_unref(keymap)
    xkb.xkb_context_unref(ctx)
    return levels


class _Device:
    """One virtual input device."""

    def __init__(self, name, keys, rel=(), absolute=()):
        self.fd = os.open(UINPUT, os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for k in keys:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)
            if rel:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                for r in rel:
                    fcntl.ioctl(self.fd, UI_SET_RELBIT, r)
            absmax = [0] * 64
            if absolute:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
                for a in absolute:
                    fcntl.ioctl(self.fd, UI_SET_ABSBIT, a)
                    absmax[a] = ABS_TOP
            # struct uinput_user_dev: name, bus/vendor/product/version, ff_effects_max, then abs max/min/fuzz/flat
            os.write(self.fd, struct.pack("80sHHHHI", name.encode(), BUS_VIRTUAL, 0x1209, 0xA1A5, 1, 0)
                     + struct.pack("64i", *absmax) + bytes(4 * 64 * 3))
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except Exception:
            os.close(self.fd)
            raise

    def emit(self, etype, code, value):
        os.write(self.fd, struct.pack("llHHi", 0, 0, etype, code, value))

    def syn(self):
        self.emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass


class UinputInjector:
    """Applies the main computer's pointer, button, scroll and key messages on this machine."""

    def __init__(self, log=print, device=_Device):
        self.log = log
        self.lock = threading.Lock()
        self.pointer = device("Kato pointer", tuple(BTN.values()), rel=(REL_WHEEL, REL_HWHEEL), absolute=(ABS_X, ABS_Y))
        self.keyboard = device("Kato keyboard", range(1, 249))
        self.layout = detect_layout()
        try:
            self.chars = build_keymap(*self.layout)
        except Exception as exc:
            log(f"keyboard layout '{self.layout[0]}' not usable ({exc}); using US")
            self.layout = ("us", "")
            self.chars = build_keymap("us", "")
        self.down = {}                 # what is held: wire key (as text) -> kernel key code
        self.mods = set()              # modifier key codes held right now
        self.buttons = set()
        self._unknown = set()
        log(f"Wayland: ready to be driven through virtual devices (keyboard layout {'+'.join(x for x in self.layout if x)})")
        time.sleep(0.4)                # give the desktop a moment to adopt the new devices

    # ---- pointer ----
    def move(self, x, y, width, height):
        ax = max(0, min(ABS_TOP, int(x) * ABS_TOP // max(int(width) - 1, 1)))
        ay = max(0, min(ABS_TOP, int(y) * ABS_TOP // max(int(height) - 1, 1)))
        with self.lock:
            self.pointer.emit(EV_ABS, ABS_X, ax)
            self.pointer.emit(EV_ABS, ABS_Y, ay)
            self.pointer.syn()

    def button(self, name, down):
        code = BTN.get(str(name))
        if code is None:
            return
        with self.lock:
            (self.buttons.add if down else self.buttons.discard)(code)
            self.pointer.emit(EV_KEY, code, 1 if down else 0)
            self.pointer.syn()

    def scroll(self, dx, dy):
        with self.lock:
            if dy:
                self.pointer.emit(EV_REL, REL_WHEEL, int(dy))
            if dx:
                self.pointer.emit(EV_REL, REL_HWHEEL, int(dx))
            self.pointer.syn()

    # ---- keyboard ----
    def _resolve(self, wire):
        """wire key -> (kernel key code, level or None). Level None means 'just this key, as it is'."""
        if "name" in wire:
            return NAMED.get(str(wire["name"])), None
        if "char" in wire:
            return self.chars.get(str(wire["char"])[:1], (None, None))
        if "vk" in wire:
            vk = int(wire["vk"])
            if vk in X_KEYSYM:
                return X_KEYSYM[vk], None
            if vk in WIN_VK:
                return WIN_VK[vk], None
            if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:        # a digit or letter key, sent by number
                code, _ = self.chars.get(chr(vk).lower(), (None, None))
                return code, None
        return None, None

    def _tap(self, code, value):
        self.keyboard.emit(EV_KEY, code, value)
        self.keyboard.syn()

    def key(self, wire, down):
        ident = json.dumps(wire, sort_keys=True)
        with self.lock:
            if not down:
                code = self.down.pop(ident, None)
                if code is not None:
                    self.mods.discard(code)
                    self._tap(code, 0)
                return
            code, level = self._resolve(wire)
            if code is None:
                if ident not in self._unknown:
                    self._unknown.add(ident)
                    self.log(f"no key on this keyboard layout for {wire}")
                return
            if ident in self.down:                              # held: the kernel repeats it by itself
                return
            self.down[ident] = code
            if level is None:
                if code in SHIFTS or code in SHORTCUT_MODS or code == KEY_RIGHTALT:
                    self.mods.add(code)
                self._tap(code, 1)
                return
            # A character. The main computer sent WHAT was typed, not which key; this machine's layout may
            # need Shift or AltGr held differently to produce it (" is Shift+2 on a UK keyboard, Shift+' on a
            # US one). Adjust those for the instant of the press, then put them back as they were.
            if self.mods & set(SHORTCUT_MODS):                  # Ctrl+C and friends: the key matters, not the level
                self._tap(code, 1)
                return
            want_shift, want_altgr = level in (1, 3), level in (2, 3)
            held_shifts = [m for m in SHIFTS if m in self.mods]
            altgr_held = KEY_RIGHTALT in self.mods
            undo = []
            if want_shift and not held_shifts:
                self._tap(KEY_LEFTSHIFT, 1); undo.append((KEY_LEFTSHIFT, 0))
            elif not want_shift and held_shifts:
                for m in held_shifts:
                    self._tap(m, 0); undo.append((m, 1))
            if want_altgr and not altgr_held:
                self._tap(KEY_RIGHTALT, 1); undo.append((KEY_RIGHTALT, 0))
            elif not want_altgr and altgr_held:
                self._tap(KEY_RIGHTALT, 0); undo.append((KEY_RIGHTALT, 1))
            self._tap(code, 1)
            for c, v in reversed(undo):
                self._tap(c, v)

    def release_all(self):
        """Let go of everything (the main computer left, or the link dropped)."""
        with self.lock:
            for code in list(self.down.values()):
                self._tap(code, 0)
            self.down.clear()
            self.mods.clear()
            for code in list(self.buttons):
                self.pointer.emit(EV_KEY, code, 0)
            if self.buttons:
                self.pointer.syn()
            self.buttons.clear()

    def close(self):
        self.release_all()
        self.pointer.close()
        self.keyboard.close()
