#!/usr/bin/env python3
"""
AirShare (working title) - one keyboard and mouse across your machines.

MVP: push the pointer off one screen's edge and it carries on onto the
neighbouring machine, keyboard and scrolling included. Push it back off the
far side and it comes home. X11 and Windows; Wayland comes later.

Discovery, trust and TLS come from clickboard_core (passphrase mode for now).

  First run on each machine, e.g. with A9-Max on the left of slim2:
    A9-Max:  python airshare.py --passphrase walnut-otter-comet-fern --right slim2
    slim2:   python airshare.py --passphrase walnut-otter-comet-fern --left A9-Max
  Later runs remember it:  python airshare.py

  Emergency: Scroll Lock brings the pointer home instantly.
"""
import argparse
import json
import platform
import queue
import secrets
import socket
import threading
import time

import pystray
from PIL import Image, ImageDraw
from pynput import keyboard, mouse

from clickboard_core import MIN_PASSPHRASE, Config, Mesh, log, set_app
from clickboard_core import common as C

VERSION = "0.2.0"
PORT, BEACON_PORT = 47810, 47812      # separate from Clickboard, so both can run
FLUSH_EVERY = 0.008                   # send gathered mouse movement ~120 times a second
BACK_OFF = 24                         # pixels inside the edge when the pointer comes home
RETURN_KEY = keyboard.Key.scroll_lock
ESCAPE_PRESSES, ESCAPE_WINDOW = 3, 1.2  # Esc three times within 1.2 s also brings the pointer home
ALIVE_EVERY = 1.0                       # a driven machine says "still here" this often...
ALIVE_TIMEOUT = 3.0                     # ...and the driver takes control back after this much silence
OPPOSITE = {"left": "right", "right": "left"}
CONTROL_PORT = 47811                  # localhost only: lets a second launch talk to the running copy

COLOUR_READY = (34, 197, 94, 255)     # green: connected, ready to cross over
COLOUR_ACTIVE = (59, 130, 246, 255)   # blue: sharing right now (driving, or being driven)
COLOUR_OFF = (249, 115, 22, 255)      # orange: nobody connected, or paused

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
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    w, h = root.winfo_screenwidth(), root.winfo_screenheight()
    root.destroy()
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


class AirShare(Mesh):
    def __init__(self, layout: dict):
        Mesh.__init__(self, Config.load(), service="airshare")
        self.layout = layout                      # {"right": "slim2", "left": ...}
        self.bounds = screen_bounds()
        self.mouse = mouse.Controller()
        self.kb = keyboard.Controller()
        self.remote = None       # (link, side) while we drive another machine
        self.driven_by = None    # (link, side we were entered from) while another machine drives us
        self._acc = [0, 0]
        self._acc_lock = threading.Lock()
        self._held_keys = {}     # keys we pressed on this machine for someone else
        self._sent_down = {}     # keys held down while driving a remote machine
        self._grabs = []
        self._centre = (0, 0)
        self.cursor = CursorHider()
        self._last_alive = 0.0
        self._esc_times = []
        self.paused = False
        self.icon = None
        self._settings_open = False
        self._outq = queue.Queue()        # input callbacks never touch the network themselves
        threading.Thread(target=self._send_loop, daemon=True).start()

    # ---- sending: callbacks only enqueue, so input hooks can never stall ----
    def _send(self, link, header):
        self._outq.put((link, header))

    def _send_loop(self):
        while True:
            link, header = self._outq.get()
            if link.alive:
                link.send(header)

    # ---- which neighbour is on which side ----
    def link_for(self, side):
        name = self.layout.get(side)
        if not name:
            return None
        for link in list(self.links.values()):
            if link.alive and self.peers.get(link.peer_id, {}).get("name", "").lower() == name.lower():
                return link
        return None

    # ---- local pointer watching: have we hit an edge? ----
    def watch_edges(self):
        bx, by, bw, bh = self.bounds

        def on_move(x, y):
            if self.remote or self.driven_by or self.paused or self._entering:
                return
            side = "right" if x >= bx + bw - 1 else "left" if x <= bx else None
            if side:
                link = self.link_for(side)
                if link:
                    # Hand the work to a thread: the hook itself must return straight away.
                    self._entering = True
                    threading.Thread(target=self._enter_safely, args=(link, side, (y - by) / max(bh, 1)),
                                     daemon=True).start()
        self._entering = False
        mouse.Listener(on_move=on_move).start()

    # ---- driving another machine ----
    def _enter_safely(self, link, side, y_ratio):
        try:
            self.enter_remote(link, side, y_ratio)
        except Exception as exc:
            log(f"couldn't cross over: {exc}")
            self.leave_remote()
        finally:
            self._entering = False

    def enter_remote(self, link, side, y_ratio):
        bx, by, bw, bh = self.bounds
        self.remote = (link, side)
        self._last_alive = time.time()
        link.send({"type": "kvm_enter", "from": OPPOSITE[side], "y": y_ratio})
        self._centre = (bx + bw // 2, by + bh // 2)
        self.mouse.position = self._centre
        self.cursor.hide()
        self._grabs = [
            mouse.Listener(on_move=self._g_move, on_click=self._g_click, on_scroll=self._g_scroll, suppress=True),
            keyboard.Listener(on_press=self._g_press, on_release=self._g_release, suppress=True),
        ]
        for g in self._grabs:
            g.start()
        self._entering = False
        threading.Thread(target=self._flush_loop, daemon=True).start()
        log(f"now controlling {self.layout.get(side)}")
        self.refresh_ui()

    def leave_remote(self, y_ratio=0.5):
        if not self.remote:
            return
        link, side = self.remote
        self.remote = None
        for g in self._grabs:
            g.stop()
        self._grabs = []
        time.sleep(0.05)          # let in-flight parking moves drain before placing the pointer
        for k in list(self._sent_down.values()):
            link.send({"type": "kvm_key", "key": k, "down": False})
        self._sent_down.clear()
        bx, by, bw, bh = self.bounds
        x = bx + bw - 1 - BACK_OFF if side == "right" else bx + BACK_OFF
        self.mouse.position = (x, by + int(y_ratio * bh))
        self.cursor.show()
        log("back home")
        self.refresh_ui()

    def _g_move(self, x, y):
        if not self.remote:
            return True           # control has ended: stop parking the pointer
        cx, cy = self._centre
        dx, dy = x - cx, y - cy
        if dx or dy:
            with self._acc_lock:
                self._acc[0] += dx
                self._acc[1] += dy
            if platform.system() != "Windows":
                self.mouse.position = self._centre     # X11: keep the real pointer parked
        return True

    def _flush_loop(self):
        while self.remote:
            if time.time() - self._last_alive > ALIVE_TIMEOUT:
                log("the other machine went quiet (asleep, locked or gone): taking control back")
                threading.Thread(target=self.leave_remote, daemon=True).start()
                return
            with self._acc_lock:
                dx, dy = self._acc
                self._acc = [0, 0]
            if dx or dy:
                self.remote[0].send({"type": "kvm_move", "dx": dx, "dy": dy})
            time.sleep(FLUSH_EVERY)

    def _g_click(self, x, y, button, pressed):
        if self.remote:
            self._send(self.remote[0], {"type": "kvm_btn", "button": button.name, "down": pressed})

    def _g_scroll(self, x, y, dx, dy):
        if self.remote:
            self._send(self.remote[0], {"type": "kvm_scroll", "dx": dx, "dy": dy})

    def _g_press(self, key):
        if key == RETURN_KEY:
            threading.Thread(target=self.leave_remote, daemon=True).start()
            return
        if key == keyboard.Key.esc:
            now = time.time()
            self._esc_times = [t for t in self._esc_times if now - t < ESCAPE_WINDOW] + [now]
            if len(self._esc_times) >= ESCAPE_PRESSES:
                self._esc_times = []
                threading.Thread(target=self.leave_remote, daemon=True).start()
                return
        wire = key_to_wire(key)
        if wire and self.remote:
            self._sent_down[json.dumps(wire, sort_keys=True)] = wire
            self._send(self.remote[0], {"type": "kvm_key", "key": wire, "down": True})

    def _g_release(self, key):
        wire = key_to_wire(key)
        if wire and self.remote:
            self._sent_down.pop(json.dumps(wire, sort_keys=True), None)
            self._send(self.remote[0], {"type": "kvm_key", "key": wire, "down": False})

    # ---- being driven by another machine, and replies to us ----
    def on_frame(self, link, header, payload):
        t = header.get("type")
        bx, by, bw, bh = self.bounds
        if t == "kvm_alive":
            if self.remote and self.remote[0] is link:
                self._last_alive = time.time()
            return
        if t == "kvm_enter":
            if input_blocked() or self.paused:
                link.send({"type": "kvm_leave", "y": float(header.get("y", 0.5))})
                log("refused control: this machine is " + ("paused" if self.paused else "locked"))
                return
            if ensure_pointer_visible():
                self.notify("Turned on Windows Mouse Keys so the pointer stays visible when another machine drives this one.")
            side = header.get("from", "left")
            x = bx + 2 if side == "left" else bx + bw - 3
            self.mouse.position = (x, by + int(float(header.get("y", 0.5)) * bh))
            self.driven_by = (link, side)
            threading.Thread(target=self._alive_loop, args=(link,), daemon=True).start()
            self.refresh_ui()
        elif t == "kvm_move" and self.driven_by:
            self.mouse.move(int(header.get("dx", 0)), int(header.get("dy", 0)))
            x, y = self.mouse.position
            side = self.driven_by[1]
            if (side == "left" and x <= bx) or (side == "right" and x >= bx + bw - 1):
                self._release_all()
                self.driven_by = None
                link.send({"type": "kvm_leave", "y": (y - by) / max(bh, 1)})
                self.refresh_ui()
        elif t == "kvm_btn" and self.driven_by:
            btn = getattr(mouse.Button, str(header.get("button")), None)
            if btn:
                (self.mouse.press if header.get("down") else self.mouse.release)(btn)
        elif t == "kvm_scroll" and self.driven_by:
            self.mouse.scroll(int(header.get("dx", 0)), int(header.get("dy", 0)))
        elif t == "kvm_key" and self.driven_by:
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
        elif t == "kvm_leave":
            if self.remote and self.remote[0] is link:
                self.leave_remote(float(header.get("y", 0.5)))

    def _alive_loop(self, link):
        """While we're being driven: heartbeat, and hand back if we get locked."""
        while self.driven_by and self.driven_by[0] is link and link.alive:
            if input_blocked():
                self._release_all()
                self.driven_by = None
                link.send({"type": "kvm_leave", "y": 0.5})
                log("locked: handed control back")
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
        if self.remote and self.remote[0] is link:
            self.leave_remote()                   # never leave the user stuck with a dead link
        if self.driven_by and self.driven_by[0] is link:
            self._release_all()
            self.driven_by = None
        Mesh.on_link_closed(self, link, why)


    # ---- tray and UI ----
    def notify(self, msg):
        log(msg)
        try:
            if platform.system() == "Linux" and __import__("shutil").which("notify-send"):
                __import__("subprocess").Popen(["notify-send", "AirShare", msg])
            elif self.icon:
                self.icon.notify(msg, "AirShare")
        except Exception:
            pass

    def status_line(self):
        if self.status:
            return self.status
        if self.paused:
            return "Paused"
        if self.remote:
            return f"Controlling {self.layout.get(self.remote[1])}"
        if self.driven_by:
            return f"Being controlled by {self.peers.get(self.driven_by[0].peer_id, {}).get('name', 'another machine')}"
        n = self.connected_count()
        if not n:
            return "Looking for your other machines..."
        return f"Ready: {n} machine{'s' if n != 1 else ''} connected"

    def refresh_ui(self):
        if not self.icon:
            return
        if self.paused or not self.connected_count():
            colour = COLOUR_OFF
        elif self.remote or self.driven_by:
            colour = COLOUR_ACTIVE
        else:
            colour = COLOUR_READY
        try:
            self.icon.icon = dot(colour)
            self.icon.title = f"AirShare - {self.status_line()}"
            self.icon.update_menu()
        except Exception:
            pass

    def build_menu(self):
        items = [pystray.MenuItem(lambda i: "Resume sharing" if self.paused else "Pause sharing", self.toggle_pause, default=True),
                 pystray.MenuItem(self.status_line(), None, enabled=False),
                 pystray.Menu.SEPARATOR]
        for side in ("left", "right"):
            name = self.layout.get(side)
            if name:
                up = self.link_for(side) is not None
                mark = "\u25cf" if up else "\u25cb"
                items.append(pystray.MenuItem(f"{mark} {name} ({side})", None, enabled=False))
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem("Layout and settings...", lambda: self.open_settings()),
                  pystray.MenuItem("Quit", self.quit)]
        return items

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.leave_remote()
        log("sharing paused" if self.paused else "sharing resumed")
        self.refresh_ui()

    def quit(self):
        self.leave_remote()
        self.stop()
        if self.icon:
            self.icon.stop()

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
            {"settings": self.open_settings, "toggle": self.toggle_pause, "quit": self.quit}.get(cmd, lambda: None)()

    def open_settings(self):
        if self._settings_open:
            return
        self._settings_open = True
        threading.Thread(target=self._settings_window, daemon=True).start()

    def _settings_window(self):
        try:
            SettingsWindow(self).run()
        except Exception as exc:
            log(f"settings window error: {exc}")
        finally:
            self._settings_open = False

    def save_layout(self, layout):
        self.layout = {k: v for k, v in layout.items() if v}
        (C.CONFIG_DIR / "layout.json").write_text(json.dumps(self.layout, indent=2))
        log(f"layout: {self.layout}")
        self.refresh_ui()


def dot(colour):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((8, 8, 56, 56), fill=colour, outline=(255, 255, 255, 200), width=3)
    return img


class SettingsWindow:
    NOBODY = "(nobody)"

    def __init__(self, app):
        self.app = app

    def run(self):
        import tkinter as tk
        from tkinter import messagebox, ttk
        self.tk, self.messagebox = tk, messagebox
        app = self.app
        root = tk.Tk()
        self.root = root
        root.title("AirShare")
        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Layout", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(outer, text="Which machine sits on each side of this one? The pointer crosses over at that edge "
                              "and arrives at the same height.", wraplength=420, foreground="#555").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))
        self.sides = {}
        names = self._names()
        for i, side in enumerate(("left", "right")):
            ttk.Label(outer, text=f"On the {side}:").grid(row=2 + i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=app.layout.get(side) or self.NOBODY)
            box = ttk.Combobox(outer, textvariable=var, values=names, state="readonly", width=28)
            box.grid(row=2 + i, column=1, sticky="w", padx=8)
            self.sides[side] = (var, box)
        ttk.Label(outer, text=f"This machine: {app.cfg.name}", foreground="#555").grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 14))

        ttk.Label(outer, text="Shared passphrase", font=("TkDefaultFont", 11, "bold")).grid(row=5, column=0, columnspan=3, sticky="w")
        ttk.Label(outer, text=f"The same on every machine, {MIN_PASSPHRASE}+ characters.", foreground="#555").grid(row=6, column=0, columnspan=3, sticky="w")
        self.pass_var = tk.StringVar(value=app.cfg.passphrase)
        ttk.Entry(outer, textvariable=self.pass_var, width=34).grid(row=7, column=0, columnspan=2, sticky="we", pady=(4, 0))
        ttk.Button(outer, text="Generate", command=lambda: self.pass_var.set("-".join(secrets.choice(WORDS) for _ in range(4)))).grid(row=7, column=2, padx=(8, 0))

        self.status_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.status_var, foreground="#555", wraplength=420).grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(outer, text="Save", command=self.save).grid(row=9, column=2, sticky="e", pady=(12, 0))

        self.tick()
        root.update_idletasks()
        root.lift()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
        root.after(100, root.focus_force)
        root.mainloop()

    def _names(self):
        return [self.NOBODY] + sorted({p["name"] for p in self.app.peers.values()} |
                                      {v for v in self.app.layout.values() if v})

    def tick(self):
        self.status_var.set(self.app.status_line())
        names = self._names()
        for var, box in self.sides.values():
            if list(box.cget("values")) != names:
                box.configure(values=names)
        self.root.after(1000, self.tick)

    def save(self):
        layout = {side: (var.get() if var.get() != self.NOBODY else None) for side, (var, _) in self.sides.items()}
        if layout["left"] and layout["left"] == layout["right"]:
            self.messagebox.showerror("AirShare", "A machine can't be on both sides.")
            return
        passphrase = self.pass_var.get().strip()
        if len(passphrase) < MIN_PASSPHRASE:
            self.messagebox.showerror("AirShare", f"The passphrase needs at least {MIN_PASSPHRASE} characters.")
            return
        self.app.save_layout(layout)
        if passphrase != self.app.cfg.passphrase:
            self.app.cfg.mode, self.app.cfg.passphrase = "local", passphrase
            self.app.cfg.save()
            self.app.reset_for_mode_change()
        self.status_var.set("Saved.")


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
    ap.add_argument("--left", help="name of the machine to the left of this one")
    ap.add_argument("--right", help="name of the machine to the right of this one")
    ap.add_argument("--name", help="this machine's name (default: its hostname)")
    ap.add_argument("--settings", action="store_true", help="open settings in the running copy")
    ap.add_argument("--quit", action="store_true", help="quit the running copy")
    a = ap.parse_args()

    # A second launch (e.g. clicking it in the menu again) talks to the running copy instead.
    if send_to_running("quit" if a.quit else "settings"):
        return
    if a.quit:
        return

    set_app("AirShare", VERSION)
    C.DEFAULT_PORT, C.BEACON_PORT = PORT, BEACON_PORT
    C.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    layout_file = C.CONFIG_DIR / "layout.json"
    layout = json.loads(layout_file.read_text()) if layout_file.exists() else {}
    for side in ("left", "right"):
        if getattr(a, side):
            layout[side] = getattr(a, side)
    layout_file.write_text(json.dumps(layout, indent=2))

    app = AirShare(layout)
    if a.name:
        app.cfg.name = a.name
    if a.passphrase:
        app.cfg.mode, app.cfg.passphrase = "local", a.passphrase
    app.cfg.mode = "local"
    app.cfg.port = PORT
    app.cfg.save()
    app._set_local_key()

    app.start()
    app.watch_edges()
    threading.Thread(target=app.control_loop, daemon=True).start()
    log(f"AirShare {VERSION} as '{app.cfg.name}', screen {app.bounds[2]}x{app.bounds[3]}, layout {layout}")
    if not app._local_key or not layout:
        threading.Timer(1.0, app.open_settings).start()
    app.icon = pystray.Icon("airshare", dot(COLOUR_OFF), "AirShare", menu=pystray.Menu(lambda: app.build_menu()))
    def keep_fresh():
        while app.running:
            time.sleep(2)
            app.refresh_ui()
    threading.Thread(target=keep_fresh, daemon=True).start()
    try:
        app.icon.run()
    except KeyboardInterrupt:
        app.quit()


if __name__ == "__main__":
    main()
