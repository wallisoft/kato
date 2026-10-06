#!/usr/bin/env python3
"""
AirShare's Wayland capture helper.

Wayland won't let a program watch the keyboard and mouse, or learn when the pointer reaches a screen
edge. Recent GNOME and KDE offer an official service for exactly this (the InputCapture portal): a
program asks for a barrier on a screen edge, and when the pointer is pushed against it the desktop hands
that program the keyboard and mouse until it gives them back.

This small program does that conversation and nothing else. AirShare starts it with the SYSTEM Python
(it needs python3-gi and libei, which the desktop already has) and talks to it in lines of JSON:

  it prints   {"e":"ready","zones":[[w,h,x,y],...]}        screens the desktop reports
              {"e":"armed","sides":["left","right"]}       edges now carrying a barrier
              {"e":"activated","side":"right","x":..,"y":..,"zone":[w,h,x,y]}   pointer crossed: input is ours
              {"e":"m","dx":..,"dy":..}  {"e":"k","code":30,"down":true}
              {"e":"b","code":272,"down":true}  {"e":"s","dx":0,"dy":-1}
              {"e":"deactivated"}  {"e":"disabled"}  {"e":"error","msg":"..."}
  it reads    {"c":"barriers","sides":["left","right"]}   {"c":"release","x":..,"y":..}
              {"c":"alive"}   {"c":"quit"}

SAFETY. Holding someone's keyboard and mouse is serious, so this program does not rely on AirShare to let go:
  - it must hear {"c":"alive"} every few seconds while it holds them, or it hands them back;
  - Esc pressed three times quickly, or Scroll Lock, hands them back here, whatever AirShare thinks;
  - if input keeps arriving after it has handed them back, it switches capturing off altogether;
  - if the pointer bounces in and out several times in a few seconds, it switches off for a few seconds;
  - it hands them back if AirShare closes the pipe or this program ends for any reason.
"""
import ctypes
import ctypes.util
import json
import os
import random
import sys
import time

HEARTBEAT_LIMIT = 4.0                 # seconds without "alive" from AirShare before input is handed back
ESC_PRESSES, ESC_WINDOW = 3, 1.5      # the emergency way home, handled here
KEY_ESC, KEY_SCROLLLOCK = 1, 70
BOUNCE_COUNT, BOUNCE_WINDOW, BOUNCE_REST = 4, 3.0, 4     # this many captures this fast = something is looping


def out(**kw):
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def fail(msg, code=1):
    out(e="error", msg=msg)
    sys.exit(code)


try:
    import gi                                                   # noqa: F401
    from gi.repository import Gio, GLib
except Exception as exc:
    fail(f"the system Python has no GObject support (install python3-gi / python3-gobject): {exc}", 3)

BUS, PATH = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.InputCapture"
SIDES = ("left", "right", "top", "bottom")
EI_CAP_POINTER, EI_CAP_KEYBOARD, EI_CAP_SCROLL, EI_CAP_BUTTON = 1, 4, 16, 32

try:
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
except Exception as exc:
    fail(f"no desktop session bus: {exc}", 4)
sender = conn.get_unique_name()[1:].replace(".", "_")
loop = GLib.MainLoop()
S = {"session": None, "zones": [], "zone_set": 0, "sides": [], "barriers": {}, "enabled": False,
     "activation": None, "alive": 0.0, "inclusive": True, "last_discrete": 0.0, "scroll": [0.0, 0.0],
     "released_at": 0.0, "esc": [], "captures": [], "resting": False, "orphan": 0.0}


def call(method, params, reply=None):
    # A time limit: with none, a desktop that doesn't answer freezes this helper for 25 seconds, and while it is
    # frozen no input is read and neither the escape keys nor the heartbeat can hand the input back.
    return conn.call_sync(BUS, PATH, IFACE, method, params, reply, Gio.DBusCallFlags.NONE, 4000, None)


def request(method, build):
    """A portal call that answers later through a Request object. Returns its results, or raises."""
    token = "airshare%d" % random.randrange(1 << 30)
    req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    answer, inner = {}, GLib.MainLoop()

    def on_response(_c, _s, _p, _i, _sig, params):
        answer["code"], answer["results"] = params.unpack()
        inner.quit()

    sub = conn.signal_subscribe(BUS, "org.freedesktop.portal.Request", "Response", req_path, None,
                                Gio.DBusSignalFlags.NONE, on_response)
    try:
        call(method, build(token))
        timer = GLib.timeout_add_seconds(120, inner.quit)
        inner.run()
        if answer:
            GLib.source_remove(timer)
    finally:
        conn.signal_unsubscribe(sub)
    if not answer:
        raise RuntimeError(f"{method}: the desktop did not answer")
    if answer["code"] != 0:
        raise RuntimeError(f"{method}: {'you declined the request' if answer['code'] == 1 else 'the desktop refused'}")
    return answer["results"]


def tok(token, **extra):
    d = {"handle_token": GLib.Variant("s", token)}
    d.update(extra)
    return d


def get_zones():
    res = request("GetZones", lambda t: GLib.Variant("(oa{sv})", (S["session"], tok(t))))
    S["zones"], S["zone_set"] = [list(z) for z in res["zones"]], res["zone_set"]
    out(e="ready", zones=S["zones"])


def barrier_list(sides, inclusive):
    items, names, n = [], {}, 0
    for zi, (w, h, x, y) in enumerate(S["zones"]):
        end_y, end_x = (y + h - 1, x + w - 1) if inclusive else (y + h, x + w)
        where = {"left": (x, y, x, end_y), "right": (x + w, y, x + w, end_y),
                 "top": (x, y, end_x, y), "bottom": (x, y + h, end_x, y + h)}
        for side in SIDES:
            n += 1
            if side in sides:
                names[n] = (side, zi)                         # which edge, of which screen
                items.append({"barrier_id": GLib.Variant("u", n), "position": GLib.Variant("(iiii)", where[side])})
    return items, names


def set_barriers(sides):
    """Put barriers on these edges (and only these), then switch capturing on. An empty list switches it off."""
    S["sides"] = [s for s in SIDES if s in sides]
    try:
        if S["enabled"]:
            call("Disable", GLib.Variant("(oa{sv})", (S["session"], {})))
            S["enabled"] = False
        if not S["sides"]:
            S["barriers"] = {}
            out(e="armed", sides=[])
            return
        good = {}
        for inclusive in (S["inclusive"], not S["inclusive"]):      # desktops differ on where an edge ends
            items, names = barrier_list(S["sides"], inclusive)
            res = request("SetPointerBarriers", lambda t: GLib.Variant("(oa{sv}aa{sv}u)", (S["session"], tok(t), items, S["zone_set"])))
            failed = set(res.get("failed_barriers", []))
            good = {i: s for i, s in names.items() if i not in failed}
            if good:
                S["inclusive"] = inclusive
                break
        S["barriers"] = good
        if good:
            call("Enable", GLib.Variant("(oa{sv})", (S["session"], {})))
            S["enabled"] = True
        out(e="armed", sides=sorted({side for side, _ in good.values()}))
    except Exception as exc:
        out(e="error", msg=f"couldn't set the screen-edge barriers: {exc}")


def switch_off(why, rest):
    """Hand the input back, say why, and ignore the screen edge for a rest.
    This used to switch capturing off and on again. After that the desktop still takes the keyboard and mouse
    at the edge but delivers none of it, which freezes this machine, so capturing now stays on throughout."""
    S["resting"] = True
    if S["activation"] is not None:
        release()
    else:
        try:
            call("Release", GLib.Variant("(oa{sv})", (S["session"], {})))
        except Exception:
            pass
    out(e="error", msg=why)
    out(e="deactivated")

    def back():
        S["resting"] = False
        return False
    GLib.timeout_add_seconds(rest, back)


def start_afresh(why):
    """Stop this helper. That always frees the keyboard and mouse, and AirShare starts a new one in a few seconds."""
    out(e="error", msg=why + ": starting the capture afresh")
    loop.quit()


def stats():
    """Say how much input the desktop actually delivered during the capture that is ending."""
    if S.get("act_at"):
        out(e="stats", secs=round(time.monotonic() - S["act_at"], 1), motion=S.get("n_m", 0), keys=S.get("n_k", 0))
        if S.get("n_m", 0) + S.get("n_k", 0):
            S["strikes"] = 0
    S["act_at"] = 0.0


def release(x=None, y=None):
    if S["activation"] is None:
        return
    stats()
    aid, S["activation"] = S["activation"], None
    S["released_at"] = time.monotonic()
    opts = {"activation_id": GLib.Variant("u", aid)}
    if x is not None and y is not None:
        opts["cursor_position"] = GLib.Variant("(dd)", (float(x), float(y)))
    try:
        call("Release", GLib.Variant("(oa{sv})", (S["session"], opts)))
    except Exception as exc:
        out(e="error", msg=f"Release failed: {exc}")


# ---------------------------------------------------------------- the conversation with the desktop
try:
    props = conn.call_sync(BUS, PATH, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (IFACE, "version")),
                           None, Gio.DBusCallFlags.NONE, 5000, None)
    VERSION = props.unpack()[0]
except Exception:
    fail("this desktop doesn't offer input capture (it needs GNOME 45 or newer, or KDE Plasma 6.1 or newer)", 5)

libpath = ctypes.util.find_library("ei")
if not libpath:
    fail("libei is not installed (Ubuntu/Debian: libei1, Fedora/Arch: libei)", 6)
lib = ctypes.CDLL(libpath)
P = ctypes.c_void_p
for name, restype, argtypes in (
        ("ei_new_receiver", P, [P]), ("ei_configure_name", None, [P, ctypes.c_char_p]),
        ("ei_setup_backend_fd", ctypes.c_int, [P, ctypes.c_int]), ("ei_get_fd", ctypes.c_int, [P]),
        ("ei_dispatch", None, [P]), ("ei_get_event", P, [P]), ("ei_event_unref", P, [P]),
        ("ei_event_get_type", ctypes.c_int, [P]), ("ei_event_get_seat", P, [P]),
        ("ei_event_pointer_get_dx", ctypes.c_double, [P]), ("ei_event_pointer_get_dy", ctypes.c_double, [P]),
        ("ei_event_keyboard_get_key", ctypes.c_uint32, [P]), ("ei_event_keyboard_get_key_is_press", ctypes.c_bool, [P]),
        ("ei_event_button_get_button", ctypes.c_uint32, [P]), ("ei_event_button_get_is_press", ctypes.c_bool, [P]),
        ("ei_event_scroll_get_dx", ctypes.c_double, [P]), ("ei_event_scroll_get_dy", ctypes.c_double, [P]),
        ("ei_event_scroll_get_discrete_dx", ctypes.c_int32, [P]), ("ei_event_scroll_get_discrete_dy", ctypes.c_int32, [P])):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = restype, argtypes

try:
    res = request("CreateSession", lambda t: GLib.Variant("(sa{sv})", ("", tok(
        t, session_handle_token=GLib.Variant("s", "airshare"), capabilities=GLib.Variant("u", 3)))))
    S["session"] = res["session_handle"]
    get_zones()
    reply, fds = conn.call_with_unix_fd_list_sync(BUS, PATH, IFACE, "ConnectToEIS", GLib.Variant("(oa{sv})", (S["session"], {})),
                                                  GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
    ei = lib.ei_new_receiver(None)
    lib.ei_configure_name(ei, b"AirShare")
    if lib.ei_setup_backend_fd(ei, fds.get(reply.unpack()[0])) != 0:
        raise RuntimeError("the input channel could not be opened")
except Exception as exc:
    fail(str(exc), 7)


def on_ei(*_):
    lib.ei_dispatch(ei)
    mx = my = 0.0
    got_input = False
    while True:
        ev = lib.ei_get_event(ei)
        if not ev:
            break
        t = lib.ei_event_get_type(ev)
        if t == 2:                                              # the desktop closed the input channel
            lib.ei_event_unref(ev)
            start_afresh("the desktop closed the input channel")
            return False
        if t not in (3, 100, 300, 500, 600, 603, 700):
            S.setdefault("others", {})[t] = S.setdefault("others", {}).get(t, 0) + 1
        if t == 3:                                              # a seat: say which kinds of input we want from it
            lib.ei_seat_bind_capabilities(P(lib.ei_event_get_seat(ev)), ctypes.c_int(EI_CAP_POINTER), ctypes.c_int(EI_CAP_KEYBOARD),
                                          ctypes.c_int(EI_CAP_SCROLL), ctypes.c_int(EI_CAP_BUTTON), P(None))
        elif t == 300:
            got_input = True
            S["n_m"] = S.get("n_m", 0) + 1
            mx += lib.ei_event_pointer_get_dx(ev)
            my += lib.ei_event_pointer_get_dy(ev)
        elif t == 700:
            got_input = True
            S["n_k"] = S.get("n_k", 0) + 1
            code, down = int(lib.ei_event_keyboard_get_key(ev)), bool(lib.ei_event_keyboard_get_key_is_press(ev))
            if down and S["activation"] is not None and code in (KEY_ESC, KEY_SCROLLLOCK):
                now = time.monotonic()
                S["esc"] = [t0 for t0 in S["esc"] if now - t0 < ESC_WINDOW] + [now] if code == KEY_ESC else []
                if code == KEY_SCROLLLOCK or len(S["esc"]) >= ESC_PRESSES:     # the emergency way home
                    S["esc"] = []
                    release()
                    out(e="deactivated", why="escape")
                    lib.ei_event_unref(ev)
                    continue
            out(e="k", code=code, down=down)
        elif t == 500:
            out(e="b", code=int(lib.ei_event_button_get_button(ev)), down=bool(lib.ei_event_button_get_is_press(ev)))
        elif t == 603:                                          # a wheel click: 120 units each
            S["last_discrete"] = time.monotonic()
            dx, dy = lib.ei_event_scroll_get_discrete_dx(ev) / 120.0, lib.ei_event_scroll_get_discrete_dy(ev) / 120.0
            if dx or dy:
                out(e="s", dx=dx, dy=dy)
        elif t == 600 and time.monotonic() - S["last_discrete"] > 0.25:   # smooth scrolling (a touchpad), in pixels
            S["scroll"][0] += lib.ei_event_scroll_get_dx(ev)
            S["scroll"][1] += lib.ei_event_scroll_get_dy(ev)
            steps = [int(S["scroll"][0] / 15), int(S["scroll"][1] / 15)]
            if steps[0] or steps[1]:
                S["scroll"][0] -= steps[0] * 15
                S["scroll"][1] -= steps[1] * 15
                out(e="s", dx=steps[0], dy=steps[1])
        lib.ei_event_unref(ev)
    if S["activation"] is None:
        # Input with no capture that we know of. Usually that is just the start of a new capture: the input and
        # the desktop's "you have it now" message travel separately, and the input can arrive first. So wait.
        # Only if input keeps coming for a full second with still no such message has the desktop failed to let
        # go, and then capturing is switched off, which always frees the keyboard and mouse.
        if got_input and not S["resting"]:
            now = time.monotonic()
            if not S["orphan"]:
                S["orphan"] = now
            elif now - S["orphan"] > 1.5:
                S["orphan"] = 0.0
                S["orphan_hits"] = [t0 for t0 in S.get("orphan_hits", []) if now - t0 < 20] + [now]
                if len(S["orphan_hits"]) >= 2:
                    start_afresh("the desktop keeps sending input with no capture in progress")
                    return False
                switch_off("the desktop kept sending input with no capture in progress: handed it back", 2)
        return True
    S["orphan"] = 0.0
    if mx or my:
        out(e="m", dx=round(mx, 2), dy=round(my, 2))
    return True


def on_activated(_c, _s, _p, _i, _sig, params):
    _session, opts = params.unpack()
    now = time.monotonic()
    if S["resting"]:
        try:
            call("Release", GLib.Variant("(oa{sv})", (S["session"], {"activation_id": GLib.Variant("u", opts.get("activation_id", 0))})))
        except Exception:
            pass
        return
    S["activation"], S["alive"], S["released_at"], S["esc"], S["orphan"] = opts.get("activation_id", 0), now, 0.0, [], 0.0
    S["act_at"], S["n_m"], S["n_k"] = now, 0, 0
    S["captures"] = [t0 for t0 in S["captures"] if now - t0 < BOUNCE_WINDOW] + [now]
    if len(S["captures"]) >= BOUNCE_COUNT:
        S["captures"] = []
        switch_off("the pointer crossed over and straight back several times in a row: the screen edge is resting "
                   "for a few seconds", BOUNCE_REST)
        return
    pos = opts.get("cursor_position") or (0.0, 0.0)
    side, zi = S["barriers"].get(opts.get("barrier_id"), ("", -1))
    zone = S["zones"][zi] if 0 <= zi < len(S["zones"]) else None
    out(e="activated", side=side, x=float(pos[0]), y=float(pos[1]), zone=zone)


def on_deactivated(*_):
    stats()
    S["activation"], S["released_at"] = None, 0.0
    out(e="deactivated")


def on_disabled(*_):
    stats()
    S["activation"], S["enabled"] = None, False
    out(e="disabled")
    GLib.timeout_add_seconds(2, rearm)


def rearm():
    if not S["enabled"] and S["sides"]:
        set_barriers(S["sides"])
    return False


def on_zones_changed(*_):
    try:
        get_zones()
        set_barriers(S["sides"])
    except Exception as exc:
        out(e="error", msg=f"couldn't follow a screen change: {exc}")


def watchdog():
    if (S["activation"] is not None and S.get("act_at") and S.get("n_m", 0) + S.get("n_k", 0) == 0
            and time.monotonic() - S["act_at"] > 2.5):
        S["strikes"] = S.get("strikes", 0) + 1
        seen = ", ".join(f"{k}x{v}" for k, v in sorted(S.get("others", {}).items())) or "none"
        S["others"] = {}
        out(e="error", msg=f"the desktop took the keyboard and mouse but passed none of it on for 2.5 seconds "
                           f"(other messages from it: {seen}): handing them back")
        release()
        out(e="deactivated")
        if S["strikes"] >= 2:
            start_afresh("that happened twice running")
            return False
        return True
    if S["activation"] is not None and time.monotonic() - S["alive"] > HEARTBEAT_LIMIT:
        out(e="error", msg="AirShare went quiet while holding the keyboard and mouse: handing them back")
        release()
        out(e="deactivated")
    return True


buf = b""


def on_stdin(_fd, cond):
    global buf
    if cond & (GLib.IO_HUP | GLib.IO_ERR):
        loop.quit()
        return False
    data = os.read(0, 65536)
    if not data:                                                # AirShare closed the pipe: stop, which frees the input
        loop.quit()
        return False
    buf += data
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        try:
            cmd = json.loads(line.decode() or "{}")
        except Exception:
            continue
        c = cmd.get("c")
        if c == "alive":
            S["alive"] = time.monotonic()
        elif c == "barriers":
            set_barriers(cmd.get("sides") or [])
        elif c == "release":
            release(cmd.get("x"), cmd.get("y"))
        elif c == "quit":
            loop.quit()
            return False
    return True


GLib.io_add_watch(lib.ei_get_fd(ei), GLib.IO_IN, on_ei)
GLib.io_add_watch(0, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_stdin)
GLib.timeout_add(500, watchdog)
for sig, handler in (("Activated", on_activated), ("Deactivated", on_deactivated), ("Disabled", on_disabled),
                     ("ZonesChanged", on_zones_changed)):
    conn.signal_subscribe(BUS, IFACE, sig, PATH, None, Gio.DBusSignalFlags.NONE, handler)
try:
    loop.run()
except KeyboardInterrupt:
    pass
release()
