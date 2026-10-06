"""
AirShare on Wayland: being the main computer.

The watching itself is done by airshare_capture_helper.py, a separate small program run with the system
Python (see its header for why and how). This module starts it, keeps it fed with which screen edges have
a neighbour, and turns what it reports into the same actions the X11 code takes: cross to a neighbour,
steer its pointer, forward keys and clicks, come home.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import airshare_uinput as U

SIDE = {"left": "left", "right": "right", "top": "up", "bottom": "down"}        # the desktop's names -> ours
EDGE = {v: k for k, v in SIDE.items()}
OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}
BUTTONS = {0x110: "left", 0x111: "right", 0x112: "middle"}
KEY_ESC, KEY_SCROLLLOCK, KEY_CAPSLOCK = 1, 70, 58
SHIFTS, ALTGR = (42, 54), 100
# physical keys that travel by name (the names the other machines already understand)
NAMES = {42: "shift", 54: "shift_r", 29: "ctrl", 97: "ctrl_r", 56: "alt", 100: "alt_gr", 125: "cmd", 126: "cmd_r",
         28: "enter", 96: "enter", 1: "esc", 15: "tab", 57: "space", 14: "backspace", 111: "delete", 110: "insert",
         102: "home", 107: "end", 104: "page_up", 109: "page_down", 103: "up", 108: "down", 105: "left", 106: "right",
         58: "caps_lock", 87: "f11", 88: "f12", 99: "print_screen", 70: "scroll_lock", 119: "pause", 69: "num_lock",
         127: "menu", 164: "media_play_pause", 113: "media_volume_mute", 114: "media_volume_down",
         115: "media_volume_up", 165: "media_previous", 163: "media_next",
         **{58 + n: f"f{n}" for n in range(1, 11)}, **{170 + n: f"f{n}" for n in range(13, 21)}}
# the number pad travels as the characters it types
KEYPAD = {82: "0", 79: "1", 80: "2", 81: "3", 75: "4", 76: "5", 77: "6", 71: "7", 72: "8", 73: "9",
          83: ".", 78: "+", 74: "-", 55: "*", 98: "/"}
NO_REPEAT = {42, 54, 29, 97, 56, 100, 125, 126, 58, 69, 70}      # modifiers and locks never auto-repeat
REPEAT_DELAY, REPEAT_EVERY = 0.45, 0.033
HELPER_PYTHON = "/usr/bin/python3"


class WaylandCapture:
    def __init__(self, app, log, back_off=24, escape=(3, 1.2)):
        self.app, self.log = app, log
        self.back_off, self.escape = back_off, escape
        self.proc = None
        self.error = ""
        self.ready = False
        self.bounds = (0, 0, 1, 1)          # the whole desktop, as the desktop describes it
        self.home = None                    # the one screen the pointer last left by (x, y, w, h)
        self.sides = None                   # edges we last asked for (None = never asked)
        self.armed = []
        self.mods = set()
        self.caps = False
        self.down = {}                      # physical key -> the wire key we sent for it
        self.repeat = None                  # (physical key, wire key, when pressed): the key being held
        self.rest_until = 0.0               # after a refused crossing, leave the edges alone until this time
        self.was_ready = False              # the helper got as far as talking to the desktop at least once
        self.stopped_at = 0.0               # when the helper stopped (AirShare starts it again, see _revive_capture)
        self.idle_reason = ""
        self.running = False
        self.lock = threading.Lock()
        layout = U.detect_layout()
        try:
            self.levels = U.build_levels(*layout)
        except Exception as exc:
            log(f"keyboard layout '{layout[0]}' not usable ({exc}); using US")
            self.levels = U.build_levels("us", "")
        self.layout = layout

    # ---- lifecycle ----
    def start(self):
        helper = os.environ.get("AIRSHARE_CAPTURE_HELPER") or str(Path(__file__).with_name("airshare_capture_helper.py"))
        python = os.environ.get("AIRSHARE_CAPTURE_PYTHON") or HELPER_PYTHON
        if not os.path.exists(helper):
            self.error, self.stopped_at = "AirShare's Wayland helper is missing from this installation", time.time()
            return False
        if not os.path.exists(python):
            self.error, self.stopped_at = f"the system Python ({python}) was not found", time.time()
            return False
        env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH")}
        try:
            # In a session of its own: Ctrl+C in a terminal goes to everything started from it, and would
            # otherwise stop the helper while leaving AirShare running without one.
            self.proc = subprocess.Popen([python, "-I", helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1, env=env, start_new_session=True)
        except Exception as exc:
            self.error = f"couldn't start the Wayland helper: {exc}"
            self.stopped_at = time.time()
            return False
        self.running = True
        self.last_err = ""
        threading.Thread(target=self._read_errors, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._tend, daemon=True).start()
        threading.Thread(target=self._repeat, daemon=True).start()
        return True

    def _read_errors(self, proc):
        """Keep the last thing the helper said on its error output, so a helper that dies can say why."""
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    self.last_err = line[:300]
        except Exception:
            pass

    def stop(self):
        self.running = False
        self._say(c="quit")
        try:
            if self.proc:
                self.proc.stdin.close()
                self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def _say(self, **cmd):
        try:
            with self.lock:
                if self.proc and self.proc.poll() is None:
                    self.proc.stdin.write(json.dumps(cmd) + "\n")
                    self.proc.stdin.flush()
        except Exception:
            pass

    # ---- which edges should carry a barrier: the ones with a connected neighbour ----
    def wanted_sides(self):
        a = self.app
        if a.paused or a.driven_by or not (a.is_master() and a.kvm_on()) or time.time() < self.rest_until:
            return []
        return [EDGE[s] for s in ("left", "right", "up", "down")
                if (lambda n: n and a.link_to(n) and n in a.sizes)(a.neighbour(s))]

    def why_idle(self):
        """In plain words, why no screen edge is being watched right now."""
        a = self.app
        if a.paused:
            return "sharing is paused"
        if a.driven_by:
            return "another machine is driving this one"
        if time.time() < self.rest_until:
            return "resting for a moment after a crossing that didn't take"
        beside = [n for n in (a.neighbour(s) for s in ("left", "right", "up", "down")) if n]
        if not beside:
            return "no machine is placed next to this one in Layout"
        waiting = [n for n in beside if not a.link_to(n)]
        if waiting:
            return f"waiting for {', '.join(waiting)} to connect"
        return f"waiting for {', '.join(beside)} to report its screen"

    def _tend(self):
        """Once a second: keep the helper's heartbeat going while we hold the input, and keep the barriers
        matching the layout as machines come and go."""
        while self.running:
            if self.app.active:
                self._say(c="alive")
            if self.ready:
                want = self.wanted_sides()
                if want != self.sides:
                    self.sides = want
                    self.idle_reason = "" if want else self.why_idle()
                    self._say(c="barriers", sides=want)
            if self.proc and self.proc.poll() is not None and self.running:
                self.running = False
                self.stopped_at = time.time()
                if not self.error:
                    time.sleep(0.3)
                    why = getattr(self, "last_err", "")
                    self.error = "the Wayland helper stopped" + (f" ({why})" if why else "")
                self.log(f"Wayland: {self.error}")
                if self.app.active:
                    threading.Thread(target=self.app._home_safely, daemon=True).start()
                self.app.refresh_ui()
            time.sleep(1.0)

    def _repeat(self):
        """The desktop reports a key going down and coming up, nothing in between. A held key repeats because
        the keyboard driver says so, and that never reaches us, so do it here: after a pause, the other machine
        is sent the key again and again (as up then down, which every kind of machine treats as a fresh press)."""
        while self.running:
            r = self.repeat
            if not r or not self.app.active:
                time.sleep(0.05)
                continue
            code, wire, since = r
            if time.time() - since < REPEAT_DELAY:
                time.sleep(0.01)
                continue
            link = self.app.link_to(self.app.active)
            if link and self.repeat is r:
                self.app._send(link, {"type": "kvm_key", "key": wire, "down": False})
                self.app._send(link, {"type": "kvm_key", "key": wire, "down": True})
            time.sleep(REPEAT_EVERY)

    # ---- what the helper reports ----
    def _read(self):
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
                self._handle(ev)
            except Exception as exc:
                self.log(f"Wayland: problem handling {line.strip()[:80]}: {exc}")

    def _handle(self, ev):
        a, e = self.app, ev.get("e")
        if e == "m":
            if a.active:
                with a._acc_lock:
                    a._acc[0] += ev.get("dx", 0)
                    a._acc[1] += ev.get("dy", 0)
        elif e == "k":
            self._key(int(ev.get("code", 0)), bool(ev.get("down")))
        elif e == "b":
            name, link = BUTTONS.get(int(ev.get("code", 0))), a.link_to(a.active)
            if name and link:
                a._send(link, {"type": "kvm_btn", "button": name, "down": bool(ev.get("down"))})
        elif e == "s":
            link = a.link_to(a.active)
            if link:                                            # the desktop counts down as positive; AirShare counts up
                a._send(link, {"type": "kvm_scroll", "dx": ev.get("dx", 0), "dy": -ev.get("dy", 0)})
        elif e == "activated":
            self._activated(SIDE.get(ev.get("side"), ""), float(ev.get("x", 0)), float(ev.get("y", 0)), ev.get("zone"))
        elif e == "deactivated" or e == "disabled":
            self.forget_keys()
            if a.active:                                        # the desktop took the input back (a lock screen, say)
                threading.Thread(target=a._home_safely, daemon=True).start()
        elif e == "ready":
            zones = ev.get("zones") or []
            if zones:
                x0, y0 = min(z[2] for z in zones), min(z[3] for z in zones)
                x1, y1 = max(z[2] + z[0] for z in zones), max(z[3] + z[1] for z in zones)
                self.bounds = (x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))
            self.ready, self.was_ready, self.error, self.sides = True, True, "", None
            self.log(f"Wayland: main computer ready, desktop {self.bounds[2]}x{self.bounds[3]}, "
                     f"keyboard layout {'+'.join(x for x in self.layout if x)}")
            a.refresh_ui()
        elif e == "armed":
            self.armed = [SIDE.get(s, s) for s in ev.get("sides", [])]
            self.log(f"Wayland: watching the {', '.join(self.armed)} screen edge(s)" if self.armed
                     else f"Wayland: not watching any screen edge yet ({self.idle_reason or self.why_idle()})")
        elif e == "error":
            self.error = str(ev.get("msg", "unknown problem"))
            self.log(f"Wayland: {self.error}")
            a.refresh_ui()

    def forget_keys(self):
        """Nothing is held any more as far as we are concerned (we came home, or are setting off afresh).
        Without this a key still down when the pointer came home would carry on repeating on the next visit."""
        self.mods.clear()
        self.down.clear()
        self.repeat = None

    def refused(self, why):
        """A crossing didn't take. Leave the edges alone for a few seconds, or pushing against the edge would
        capture and release in a tight loop."""
        self.rest_until = time.time() + 3
        self.sides = []
        self._say(c="barriers", sides=[])
        self.log(f"Wayland: {why}; the screen edge rests for 3 seconds")

    def _activated(self, side, x, y, zone=None):
        a = self.app
        # With several screens of different sizes, "halfway down the edge" must mean halfway down the screen the
        # pointer actually left by, not halfway down the whole desktop.
        self.home = (zone[2], zone[3], max(zone[0], 1), max(zone[1], 1)) if zone and len(zone) == 4 else None
        bx, by, bw, bh = self.home or self.bounds
        ratio = (y - by) / bh if side in ("left", "right") else (x - bx) / bw
        ratio = min(max(ratio, 0.0), 1.0)
        target = a.neighbour(side) if side else None
        if a.driven_by:
            # Another machine is driving this one, so what we just captured is ITS input arriving through our
            # virtual devices. Passing it on would send it straight back to where it came from.
            self.release_home(side or "right", ratio)
            self.refused("this machine is being driven by another")
            return
        if a.paused or a._busy or not target or not a.link_to(target) or target not in a.sizes:
            self.release_home(side or "right", ratio)           # nowhere to go: give the input straight back
            return
        a._busy = True
        self.forget_keys()

        def cross():
            a._enter_grace = time.time() + 0.35                 # the pointer arrives one pixel in: let it settle
            a._go_safely(target, OPPOSITE[side], ratio, side)
            if not a.active:                                    # the crossing didn't happen
                self.release_home(side, ratio)
                self.refused(f"couldn't cross to {target}")
        threading.Thread(target=cross, daemon=True).start()

    def release_home(self, side, ratio):
        """Give the keyboard and mouse back, putting the pointer just inside the edge it left by."""
        self.forget_keys()
        bx, by, bw, bh = self.home or self.bounds
        r, off = min(max(float(ratio), 0.0), 1.0), self.back_off
        x, y = {"right": (bx + bw - 1 - off, by + r * bh), "left": (bx + off, by + r * bh),
                "up": (bx + r * bw, by + off), "down": (bx + r * bw, by + bh - 1 - off)}.get(side, (bx + bw / 2, by + bh / 2))
        self._say(c="release", x=x, y=y)

    # ---- keys: the desktop reports the physical key; the other machines expect what it types ----
    def wire_for(self, code):
        if code in NAMES:
            return {"name": NAMES[code]}
        if code in KEYPAD:
            return {"char": KEYPAD[code]}
        shift, altgr = bool(self.mods & set(SHIFTS)), ALTGR in self.mods
        level = (1 if shift else 0) + (2 if altgr else 0)
        ch = self.levels.get((code, level)) or self.levels.get((code, level & 1)) or self.levels.get((code, 0))
        if not ch:
            return None
        if self.caps and ch.isalpha():
            ch = ch.swapcase()
        return {"char": ch}

    def _key(self, code, down):
        a = self.app
        if code in SHIFTS or code == ALTGR:
            (self.mods.add if down else self.mods.discard)(code)
        if code == KEY_CAPSLOCK and down:
            self.caps = not self.caps
        if not a.active:
            return
        link = a.link_to(a.active)
        if down:
            if code == KEY_SCROLLLOCK:                          # the "bring me home" key
                threading.Thread(target=a._home_safely, daemon=True).start()
                return
            if code == KEY_ESC:
                presses, window = self.escape
                now = time.time()
                a._esc_times = [t for t in a._esc_times if now - t < window] + [now]
                if len(a._esc_times) >= presses:
                    a._esc_times = []
                    threading.Thread(target=a._home_safely, daemon=True).start()
                    return
            if code in self.down:                               # held: the other machine repeats it by itself
                return
            wire = self.wire_for(code)
            if wire and link:
                self.down[code] = wire
                a._sent_down[json.dumps(wire, sort_keys=True)] = wire
                a._send(link, {"type": "kvm_key", "key": wire, "down": True})
                self.repeat = None if code in NO_REPEAT else (code, wire, time.time())
        else:
            if self.repeat and self.repeat[0] == code:
                self.repeat = None
            wire = self.down.pop(code, None)
            if wire and link:
                a._sent_down.pop(json.dumps(wire, sort_keys=True), None)
                a._send(link, {"type": "kvm_key", "key": wire, "down": False})
