"""
AirShare input service (Windows, optional).

A small helper that runs as SYSTEM so AirShare can inject keyboard and mouse input onto Windows'
*secure* desktops too: the UAC prompt and the login screen, which a normal program is not allowed
to touch. This matters for headless machines (a server in a cupboard) with no keyboard or mouse of
their own to answer a UAC prompt with.

Deliberately small and dumb, to keep the attack surface tiny:
  * It listens ONLY on a local named pipe, never the network.
  * The pipe is locked to Administrators and SYSTEM, so only an elevated local process can open it.
  * It accepts ONLY input commands (move, button, scroll, key). There is no "run program" command.
  * It does nothing on its own: it injects only what the already-authenticated AirShare app sends,
    and only while that app has decided a crossing is active.

All of AirShare's security (TLS between machines, the shared passphrase, the crossing and safety
logic) stays in the app. This service is just the last inch of injection on a privileged desktop.

Installed and removed by the AirShare installer when the optional component is chosen. Runs via
Python's pywin32 service framework.
"""
import json
import struct
import sys

import servicemanager
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security
import win32service
import win32serviceutil
import win32ts
import winerror
import ntsecuritycon

PIPE_NAME = r"\\.\pipe\AirShareInput"
SECURE = (win32con.SE_GROUP_LOGON_ID,)

# INPUT structures for SendInput, built once.
import ctypes
from ctypes import wintypes

ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _I(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _I)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
user32 = ctypes.windll.user32


def _send(inp):
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def inject(cmd):
    """Perform one input command on whatever desktop is current. cmd is a small dict."""
    kind = cmd.get("t")
    if kind == "move":
        vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        inp = INPUT(type=INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(int((cmd["x"] - vx) * 65535 / max(vw - 1, 1)),
                              int((cmd["y"] - vy) * 65535 / max(vh - 1, 1)), 0,
                              0x0001 | 0x8000 | 0x4000, 0, None)      # MOVE | ABSOLUTE | VIRTUALDESK
        _send(inp)
    elif kind == "btn":
        flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
        down, up = flags.get(cmd.get("button", "left"), flags["left"])
        inp = INPUT(type=INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(0, 0, 0, down if cmd.get("down") else up, 0, None)
        _send(inp)
    elif kind == "scroll":
        for data, flag in ((int(cmd.get("dy", 0)) * 120, 0x0800),      # WHEEL
                           (int(cmd.get("dx", 0)) * 120, 0x1000)):     # HWHEEL
            if data:
                inp = INPUT(type=INPUT_MOUSE)
                inp.u.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flag, 0, None)
                _send(inp)
    elif kind == "key":
        inp = INPUT(type=INPUT_KEYBOARD)
        flags = 0x0008                                                # UNICODE
        if not cmd.get("down"):
            flags |= 0x0002                                           # KEYUP
        scan = int(cmd.get("vk", 0)) if cmd.get("vk") else int(cmd.get("char", 0))
        if cmd.get("vk"):                                             # a real virtual key (Enter, arrows...)
            inp.u.ki = KEYBDINPUT(int(cmd["vk"]), 0, (0x0002 if not cmd.get("down") else 0), 0, None)
        else:                                                        # a character, by Unicode scan code
            inp.u.ki = KEYBDINPUT(0, scan, flags, 0, None)
        _send(inp)


def pipe_security():
    """Allow only SYSTEM and Administrators to open the pipe."""
    sd = win32security.SECURITY_DESCRIPTOR()
    dacl = win32security.ACL()
    for sid_name in (win32security.WinLocalSystemSid, win32security.WinBuiltinAdministratorsSid):
        sid = win32security.CreateWellKnownSid(sid_name)
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    sd.SetSecurityDescriptorDacl(1, dacl, 0)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    return sa


class AirShareInputService(win32serviceutil.ServiceFramework):
    _svc_name_ = "AirShareInput"
    _svc_display_name_ = "AirShare Input Service"
    _svc_description_ = ("Lets AirShare control this computer at the Windows login and security (UAC) screens. "
                         "Optional; installed only if you chose that during AirShare setup.")

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.running = True

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.running = False
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
        sa = pipe_security()
        while self.running:
            pipe = win32pipe.CreateNamedPipe(
                PIPE_NAME,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                1, 65536, 65536, 0, sa)
            try:
                win32pipe.ConnectNamedPipe(pipe, None)
                while self.running:
                    try:
                        hr, data = win32file.ReadFile(pipe, 65536)
                    except Exception:
                        break
                    if not data:
                        break
                    try:
                        # the current session's desktop can change (user -> Winlogon); follow it
                        self._attach_input_desktop()
                        for line in data.decode("utf-8").splitlines():
                            if line.strip():
                                inject(json.loads(line))
                    except Exception as exc:
                        servicemanager.LogErrorMsg(f"AirShareInput inject error: {exc}")
            finally:
                try:
                    win32pipe.DisconnectNamedPipe(pipe)
                except Exception:
                    pass
                win32file.CloseHandle(pipe)
            if win32event.WaitForSingleObject(self.stop_event, 10) == win32event.WAIT_OBJECT_0:
                break

    def _attach_input_desktop(self):
        """SendInput goes to the calling thread's desktop; hop onto whichever desktop is now showing
        (the normal desktop, or Winlogon's secure one) so keystrokes land where the user can see them."""
        try:
            hdesk = win32service.OpenInputDesktop(0, True, win32con.MAXIMUM_ALLOWED) \
                if hasattr(win32service, "OpenInputDesktop") else None
        except Exception:
            hdesk = None
        if hdesk is None:
            try:
                import win32con as c
                hdesk = ctypes.windll.user32.OpenInputDesktop(0, True, 0x10000000)   # GENERIC_ALL
            except Exception:
                return
        try:
            ctypes.windll.user32.SetThreadDesktop(hdesk if isinstance(hdesk, int) else hdesk.handle)
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(AirShareInputService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(AirShareInputService)
