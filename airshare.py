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
import threading
import time

from pynput import keyboard, mouse

from clickboard_core import Config, Mesh, log, set_app
from clickboard_core import common as C

VERSION = "0.1.0"
PORT, BEACON_PORT = 47810, 47812      # separate from Clickboard, so both can run
FLUSH_EVERY = 0.008                   # send gathered mouse movement ~120 times a second
BACK_OFF = 24                         # pixels inside the edge when the pointer comes home
RETURN_KEY = keyboard.Key.scroll_lock
OPPOSITE = {"left": "right", "right": "left"}


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
            if self.remote or self.driven_by:
                return
            side = "right" if x >= bx + bw - 1 else "left" if x <= bx else None
            if side:
                link = self.link_for(side)
                if link:
                    self.enter_remote(link, side, (y - by) / max(bh, 1))
        mouse.Listener(on_move=on_move).start()

    # ---- driving another machine ----
    def enter_remote(self, link, side, y_ratio):
        bx, by, bw, bh = self.bounds
        self.remote = (link, side)
        link.send({"type": "kvm_enter", "from": OPPOSITE[side], "y": y_ratio})
        self._centre = (bx + bw // 2, by + bh // 2)
        self.mouse.position = self._centre
        self._grabs = [
            mouse.Listener(on_move=self._g_move, on_click=self._g_click, on_scroll=self._g_scroll, suppress=True),
            keyboard.Listener(on_press=self._g_press, on_release=self._g_release, suppress=True),
        ]
        for g in self._grabs:
            g.start()
        threading.Thread(target=self._flush_loop, daemon=True).start()
        log(f"now controlling {self.layout.get(side)}")

    def leave_remote(self, y_ratio=0.5):
        if not self.remote:
            return
        link, side = self.remote
        self.remote = None
        for g in self._grabs:
            g.stop()
        self._grabs = []
        for k in list(self._sent_down.values()):
            link.send({"type": "kvm_key", "key": k, "down": False})
        self._sent_down.clear()
        bx, by, bw, bh = self.bounds
        x = bx + bw - 1 - BACK_OFF if side == "right" else bx + BACK_OFF
        self.mouse.position = (x, by + int(y_ratio * bh))
        log("back home")

    def _g_move(self, x, y):
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
            with self._acc_lock:
                dx, dy = self._acc
                self._acc = [0, 0]
            if dx or dy:
                self.remote[0].send({"type": "kvm_move", "dx": dx, "dy": dy})
            time.sleep(FLUSH_EVERY)

    def _g_click(self, x, y, button, pressed):
        if self.remote:
            self.remote[0].send({"type": "kvm_btn", "button": button.name, "down": pressed})

    def _g_scroll(self, x, y, dx, dy):
        if self.remote:
            self.remote[0].send({"type": "kvm_scroll", "dx": dx, "dy": dy})

    def _g_press(self, key):
        if key == RETURN_KEY:
            threading.Thread(target=self.leave_remote, daemon=True).start()
            return
        wire = key_to_wire(key)
        if wire and self.remote:
            self._sent_down[json.dumps(wire, sort_keys=True)] = wire
            self.remote[0].send({"type": "kvm_key", "key": wire, "down": True})

    def _g_release(self, key):
        wire = key_to_wire(key)
        if wire and self.remote:
            self._sent_down.pop(json.dumps(wire, sort_keys=True), None)
            self.remote[0].send({"type": "kvm_key", "key": wire, "down": False})

    # ---- being driven by another machine, and replies to us ----
    def on_frame(self, link, header, payload):
        t = header.get("type")
        bx, by, bw, bh = self.bounds
        if t == "kvm_enter":
            side = header.get("from", "left")
            x = bx + 2 if side == "left" else bx + bw - 3
            self.mouse.position = (x, by + int(float(header.get("y", 0.5)) * bh))
            self.driven_by = (link, side)
        elif t == "kvm_move" and self.driven_by:
            self.mouse.move(int(header.get("dx", 0)), int(header.get("dy", 0)))
            x, y = self.mouse.position
            side = self.driven_by[1]
            if (side == "left" and x <= bx) or (side == "right" and x >= bx + bw - 1):
                self._release_all()
                self.driven_by = None
                link.send({"type": "kvm_leave", "y": (y - by) / max(bh, 1)})
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


def main():
    ap = argparse.ArgumentParser(description="AirShare: one keyboard and mouse across your machines")
    ap.add_argument("--passphrase", help="shared passphrase (same on every machine, 8+ characters)")
    ap.add_argument("--left", help="name of the machine to the left of this one")
    ap.add_argument("--right", help="name of the machine to the right of this one")
    ap.add_argument("--name", help="this machine's name (default: its hostname)")
    a = ap.parse_args()

    set_app("AirShare", VERSION)
    C.DEFAULT_PORT, C.BEACON_PORT = PORT, BEACON_PORT

    layout_file = C.CONFIG_DIR / "layout.json"
    layout = json.loads(layout_file.read_text()) if layout_file.exists() else {}
    for side in ("left", "right"):
        if getattr(a, side):
            layout[side] = getattr(a, side)
    C.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    layout_file.write_text(json.dumps(layout, indent=2))

    app = AirShare(layout)
    if a.name:
        app.cfg.name = a.name
    if a.passphrase:
        app.cfg.mode, app.cfg.passphrase = "local", a.passphrase
    app.cfg.port = PORT
    app.cfg.save()
    app._set_local_key()
    if not app._local_key:
        raise SystemExit("Run once with --passphrase (8+ characters), the same on every machine.")

    app.start()
    app.watch_edges()
    log(f"AirShare {VERSION} as '{app.cfg.name}', screen {app.bounds[2]}x{app.bounds[3]}, layout {layout}")
    log("Scroll Lock brings the pointer home. Ctrl+C here to quit.")
    last = None
    try:
        while True:
            now = sorted(p["name"] for pid, p in app.peers.items() if app.links.get(pid) and app.links[pid].alive)
            if now != last:
                log(f"connected to: {', '.join(now) or 'nobody yet'}")
                last = now
            time.sleep(2)
    except KeyboardInterrupt:
        app.leave_remote()
        app.stop()


if __name__ == "__main__":
    main()
