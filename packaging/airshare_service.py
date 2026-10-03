"""
AirShare input service (Windows, optional).

Runs as SYSTEM so AirShare can inject keyboard and mouse input onto Windows' *secure* desktops too:
the UAC prompt and the login screen, which a normal program is not allowed to touch. This matters for
headless machines (a server in a cupboard) with no keyboard or mouse of their own to answer a UAC
prompt with.

Deliberately small and dumb, to keep the attack surface tiny:
  * Listens ONLY on a local named pipe, never the network.
  * The pipe is locked to Administrators and SYSTEM, so only an elevated local process can open it.
  * Accepts ONLY input commands (move, button, scroll, key). There is no "run program" command.
  * Does nothing on its own: it injects only what the already-authenticated AirShare app sends.

All of AirShare's security (TLS between machines, the shared passphrase, the crossing and safety
logic) stays in the app. This service is just the last inch of injection on a privileged desktop.

Built standalone by Nuitka, so it registers itself with Windows' own service manager (sc) rather than
pywin32's pythonservice.exe, which doesn't exist inside a compiled bundle.

  airshare-service.exe install     register and set to start at boot
  airshare-service.exe remove      stop and unregister
  airshare-service.exe run         (used by Windows) the service body
"""
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes

import win32security
import ntsecuritycon

import servicemanager
import win32service
import win32file
import win32pipe

PIPE_NAME = r"\\.\pipe\AirShareInput"
SVC_NAME = "AirShareInput"
SVC_DISPLAY = "AirShare Input Service"
SVC_DESC = ("Lets AirShare control this computer at the Windows login and security (UAC) screens. "
            "Optional; installed only if you chose that during AirShare setup.")

# ----- SendInput plumbing ---------------------------------------------------
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
        for data, flag in ((int(cmd.get("dy", 0)) * 120, 0x0800), (int(cmd.get("dx", 0)) * 120, 0x1000)):
            if data:
                inp = INPUT(type=INPUT_MOUSE)
                inp.u.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flag, 0, None)
                _send(inp)
    elif kind == "key":
        inp = INPUT(type=INPUT_KEYBOARD)
        if cmd.get("vk"):                                             # a real virtual key (Enter, arrows...)
            inp.u.ki = KEYBDINPUT(int(cmd["vk"]), 0, (0x0002 if not cmd.get("down") else 0), 0, None)
        else:                                                        # a character, by Unicode scan code
            flags = 0x0004 | (0 if cmd.get("down") else 0x0002)      # UNICODE | (KEYUP)
            inp.u.ki = KEYBDINPUT(0, int(cmd.get("char", 0)), flags, 0, None)
        _send(inp)


def attach_input_desktop():
    """SendInput goes to the calling thread's desktop; hop onto whichever desktop is showing now
    (the user's, or Winlogon's secure one) so input lands where the user can see it."""
    try:
        hdesk = user32.OpenInputDesktop(0, True, 0x10000000)         # GENERIC_ALL
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass


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


# ----- the service body, driven by Windows' service control manager ---------
class Service:
    def __init__(self):
        self.running = True
        self.status_handle = None

    def ctrl(self, control, event_type, data, context):
        if control in (win32service.SERVICE_CONTROL_STOP, win32service.SERVICE_CONTROL_SHUTDOWN):
            self.running = False
            self._report(win32service.SERVICE_STOP_PENDING)
        return 0

    def _report(self, state, exit_code=0, wait=0):
        s = win32service.SERVICE_STATUS()
        s.dwServiceType = win32service.SERVICE_WIN32_OWN_PROCESS
        s.dwCurrentState = state
        s.dwControlsAccepted = (win32service.SERVICE_ACCEPT_STOP | win32service.SERVICE_ACCEPT_SHUTDOWN
                                if state == win32service.SERVICE_RUNNING else 0)
        s.dwWin32ExitCode = exit_code
        s.dwWaitHint = wait
        if self.status_handle:
            win32service.SetServiceStatus(self.status_handle, s)

    def main(self, argv):
        self.status_handle = win32service.RegisterServiceCtrlHandlerEx(SVC_NAME, self.ctrl, None)
        self._report(win32service.SERVICE_RUNNING)
        try:
            self._serve()
        except Exception as exc:
            servicemanager.LogErrorMsg(f"AirShareInput: {exc}")
        self._report(win32service.SERVICE_STOPPED)

    def _serve(self):
        sa = pipe_security()
        while self.running:
            pipe = win32pipe.CreateNamedPipe(
                PIPE_NAME, win32pipe.PIPE_ACCESS_DUPLEX,
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
                        attach_input_desktop()
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


SERVICE = Service()


def run_as_service():
    # the service control dispatcher lives in servicemanager, and takes the name + our body function
    servicemanager.Initialize(SVC_NAME, None)
    servicemanager.StartServiceCtrlDispatcher([(SVC_NAME, SERVICE.main)])


# ----- install / remove, via Windows' own service manager (sc) --------------
def _scm():
    return win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)


def install():
    exe = f'"{sys.executable}" run' if "__compiled__" in globals() else f'"{sys.executable}" "{__file__}" run'
    scm = _scm()
    try:
        try:
            old = win32service.OpenService(scm, SVC_NAME, win32service.SERVICE_ALL_ACCESS)
            win32service.DeleteService(old)
            win32service.CloseServiceHandle(old)
        except Exception:
            pass
        h = win32service.CreateService(
            scm, SVC_NAME, SVC_DISPLAY, win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS, win32service.SERVICE_AUTO_START,
            win32service.SERVICE_ERROR_NORMAL, exe, None, 0, None, None, None)
        try:
            win32service.ChangeServiceConfig2(h, win32service.SERVICE_CONFIG_DESCRIPTION, SVC_DESC)
        except Exception:
            pass
        win32service.StartService(h, None)
        win32service.CloseServiceHandle(h)
        print("AirShareInput installed and started")
    finally:
        win32service.CloseServiceHandle(scm)


def remove():
    scm = _scm()
    try:
        h = win32service.OpenService(scm, SVC_NAME, win32service.SERVICE_ALL_ACCESS)
        try:
            win32service.ControlService(h, win32service.SERVICE_CONTROL_STOP)
            time.sleep(1)
        except Exception:
            pass
        win32service.DeleteService(h)
        win32service.CloseServiceHandle(h)
        print("AirShareInput removed")
    except Exception as exc:
        print(f"AirShareInput not removed: {exc}")
    finally:
        win32service.CloseServiceHandle(scm)


if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "run"
    if cmd == "install":
        install()
    elif cmd == "remove":
        remove()
    else:
        run_as_service()
