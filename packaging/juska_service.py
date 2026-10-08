"""
Juska input service (Windows, optional).

Runs as SYSTEM so Juska can inject keyboard and mouse input onto Windows' *secure* desktops too:
the UAC prompt and the login screen, which a normal program is not allowed to touch. This matters for
headless machines (a server in a cupboard) with no keyboard or mouse of their own to answer a UAC
prompt with.

Deliberately small and dumb, to keep the attack surface tiny:
  * Listens ONLY on a local named pipe, never the network.
  * The pipe is locked to Administrators and SYSTEM, so only an elevated local process can open it.
  * Accepts ONLY input commands (move, button, scroll, key). There is no "run program" command.
  * Does nothing on its own: it injects only what the already-authenticated Juska app sends.

All of Juska's security (TLS between machines, the shared passphrase, the crossing and safety
logic) stays in the app. This service is just the last inch of injection on a privileged desktop.

How it is put together (Windows keeps services in a session of their own with no screen, so input sent from
the service itself would go nowhere):

  * the SERVICE (session 0, SYSTEM) does one thing: it keeps an AGENT running in whichever session is at the
    physical screen;
  * the AGENT (the same program started with "agent", still SYSTEM, but inside that session) owns the pipe and
    does the typing, hopping onto whichever desktop is showing: the user's, the lock screen or a UAC prompt.

The pipe admits the logged-in user, because Juska itself is an ordinary program. So the agent also checks
that whoever connects really is the installed juska.exe sitting beside it, and drops anyone else.

Everything it does and every reason it refuses is written to service.log in the ProgramData folder, under Juska.

  juska-service.exe install     register and set to start at boot
  juska-service.exe remove      stop and unregister
  juska-service.exe run         (used by Windows) the service
  juska-service.exe agent       (used by the service) the part that types
"""
import ctypes
import json
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes

import win32security
import ntsecuritycon

import win32service
import win32file
import win32pipe

PIPE_NAME = r"\\.\pipe\JuskaInput"
SVC_NAME = "JuskaInput"
SVC_DISPLAY = "Juska Input Service"
SVC_DESC = ("Lets Juska control this computer at the Windows login and security (UAC) screens. "
            "Optional; installed only if you chose that during Juska setup.")

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
            vk = int(cmd["vk"])
            # arrows, Home/End, Insert/Delete, PageUp/Down and the Windows/menu keys are "extended" keys;
            # without saying so, Shift+arrow and friends misbehave
            ext = 0x0001 if (0x21 <= vk <= 0x28 or vk in (0x2D, 0x2E, 0x5B, 0x5C, 0x5D)) else 0
            inp.u.ki = KEYBDINPUT(vk, 0, ext | (0x0002 if not cmd.get("down") else 0), 0, None)
        else:                                                        # a character, by Unicode scan code
            flags = 0x0004 | (0 if cmd.get("down") else 0x0002)      # UNICODE | (KEYUP)
            inp.u.ki = KEYBDINPUT(0, int(cmd.get("char", 0)), flags, 0, None)
        _send(inp)


user32.OpenInputDesktop.restype = ctypes.c_void_p
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.SetThreadDesktop.argtypes = [ctypes.c_void_p]
user32.CloseDesktop.argtypes = [ctypes.c_void_p]
_desktop = None


def attach_input_desktop():
    """SendInput goes to the calling thread's desktop; hop onto whichever desktop is showing now
    (the user's, or Winlogon's secure one) so input lands where the user can see it."""
    global _desktop
    try:
        hdesk = user32.OpenInputDesktop(0, False, 0x10000000)        # GENERIC_ALL
        if not hdesk:
            return
        if user32.SetThreadDesktop(hdesk):
            if _desktop:
                user32.CloseDesktop(_desktop)
            _desktop = hdesk
        else:
            user32.CloseDesktop(hdesk)
    except Exception:
        pass


def pipe_security():
    """SYSTEM and Administrators in full; the interactively logged-in user may read and write (Juska runs as
    them). Who is really on the other end is checked separately, see genuine_client()."""
    sd = win32security.SECURITY_DESCRIPTOR()
    dacl = win32security.ACL()
    for sid_name in (win32security.WinLocalSystemSid, win32security.WinBuiltinAdministratorsSid):
        sid = win32security.CreateWellKnownSid(sid_name)
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION,
                             ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE,
                             win32security.CreateWellKnownSid(win32security.WinInteractiveSid))
    sd.SetSecurityDescriptorDacl(1, dacl, 0)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    return sa


# ----- a log, because nobody can see a service ------------------------------
LOG_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Juska")


def note(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "service.log")
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def long_path(p):
    """The full spelling of a path (C:\\PROGRA~2\\... and C:\\Program Files (x86)\\... are the same place)."""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetLongPathNameW(str(p), buf, 1024)
        return buf.value if 0 < n < 1024 else str(p)
    except Exception:
        return str(p)


def own_exe():
    """This program's real path, asked of Windows itself. (Inside a compiled build sys.executable names a
    python.exe that isn't there, which is what the service used to be registered as.)"""
    if "__compiled__" not in globals():
        return os.path.abspath(sys.executable)
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetModuleFileNameW(None, buf, 1024)
    return long_path(buf.value if 0 < n < 1024 else os.path.abspath(sys.argv[0]))


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
HANDLE, DWORD, BOOL, LPVOID, LPWSTR = ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p, wintypes.LPWSTR

# ----- the agent: in the session at the screen, owns the pipe, does the typing ----
kernel32.GetNamedPipeClientProcessId.argtypes = [HANDLE, ctypes.POINTER(wintypes.ULONG)]
kernel32.GetNamedPipeClientProcessId.restype = BOOL
kernel32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
kernel32.OpenProcess.restype = HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [HANDLE, DWORD, LPWSTR, ctypes.POINTER(DWORD)]
kernel32.QueryFullProcessImageNameW.restype = BOOL
kernel32.CloseHandle.argtypes = [HANDLE]


def genuine_client(pipe):
    """Is the program on the other end of the pipe the installed juska.exe beside this service?"""
    expected = os.path.normcase(os.path.join(os.path.dirname(os.path.dirname(own_exe())), "juska.exe"))
    pid = wintypes.ULONG(0)
    if not kernel32.GetNamedPipeClientProcessId(int(getattr(pipe, "handle", pipe)), ctypes.byref(pid)):
        return False, "couldn't tell who connected"
    h = kernel32.OpenProcess(0x1000, False, pid.value)               # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False, f"couldn't look at process {pid.value}"
    try:
        buf, size = ctypes.create_unicode_buffer(1024), DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return False, f"couldn't name process {pid.value}"
        actual = os.path.normcase(long_path(os.path.abspath(buf.value)))
    finally:
        kernel32.CloseHandle(h)
    return actual == expected, actual


def run_agent():
    note(f"agent started (process {os.getpid()})")
    sa = pipe_security()
    while True:
        pipe = win32pipe.CreateNamedPipe(
            PIPE_NAME, win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
            1, 65536, 65536, 0, sa)
        try:
            win32pipe.ConnectNamedPipe(pipe, None)
            ok, who = genuine_client(pipe)
            if not ok:
                note(f"turned away a connection that wasn't Juska: {who}")
                continue
            note("Juska connected")
            failed = 0
            while True:
                try:
                    _hr, data = win32file.ReadFile(pipe, 65536)
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
                    failed += 1
                    if failed <= 5:
                        note(f"couldn't inject: {exc}")
            note("Juska disconnected")
        except Exception:
            note("agent pipe error:\n" + traceback.format_exc())
            time.sleep(1)
        finally:
            try:
                win32pipe.DisconnectNamedPipe(pipe)
            except Exception:
                pass
            try:
                win32file.CloseHandle(pipe)
            except Exception:
                pass


# ----- the service: keeps an agent running in the session at the screen -----
class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("lpReserved", LPWSTR), ("lpDesktop", LPWSTR), ("lpTitle", LPWSTR),
                ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD), ("dwYSize", DWORD),
                ("dwXCountChars", DWORD), ("dwYCountChars", DWORD), ("dwFillAttribute", DWORD),
                ("dwFlags", DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
                ("hStdError", HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


kernel32.WTSGetActiveConsoleSessionId.restype = DWORD
kernel32.GetCurrentProcess.restype = HANDLE
kernel32.TerminateProcess.argtypes = [HANDLE, wintypes.UINT]
kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
kernel32.WaitForSingleObject.restype = DWORD
advapi32.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
advapi32.OpenProcessToken.restype = BOOL
advapi32.DuplicateTokenEx.argtypes = [HANDLE, DWORD, LPVOID, ctypes.c_int, ctypes.c_int, ctypes.POINTER(HANDLE)]
advapi32.DuplicateTokenEx.restype = BOOL
advapi32.SetTokenInformation.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD]
advapi32.SetTokenInformation.restype = BOOL
advapi32.CreateProcessAsUserW.argtypes = [HANDLE, wintypes.LPCWSTR, LPWSTR, LPVOID, LPVOID, BOOL, DWORD, LPVOID,
                                          wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                          ctypes.POINTER(PROCESS_INFORMATION)]
advapi32.CreateProcessAsUserW.restype = BOOL
NO_SESSION = 0xFFFFFFFF


def start_agent(session):
    """Start this program again with "agent", as SYSTEM, inside the given session. Returns its process handle."""
    token, dup = HANDLE(), HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0xF01FF, ctypes.byref(token)):   # TOKEN_ALL_ACCESS
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # MAXIMUM_ALLOWED, SecurityImpersonation, TokenPrimary
        if not advapi32.DuplicateTokenEx(token, 0x02000000, None, 2, 1, ctypes.byref(dup)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(token)
    try:
        sid = DWORD(session)
        if not advapi32.SetTokenInformation(dup, 12, ctypes.byref(sid), ctypes.sizeof(sid)):          # TokenSessionId
            raise ctypes.WinError(ctypes.get_last_error())
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = "winsta0\\default"
        pi = PROCESS_INFORMATION()
        exe = own_exe()
        cmd = ctypes.create_unicode_buffer(f'"{exe}" agent')
        if not advapi32.CreateProcessAsUserW(dup, exe, cmd, None, None, False, 0x08000000,           # CREATE_NO_WINDOW
                                             None, os.path.dirname(exe), ctypes.byref(si), ctypes.byref(pi)):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(pi.hThread)
        return pi.hProcess
    finally:
        kernel32.CloseHandle(dup)


SERVICE_MAIN = ctypes.WINFUNCTYPE(None, DWORD, ctypes.POINTER(LPWSTR))
HANDLER_EX = ctypes.WINFUNCTYPE(DWORD, DWORD, DWORD, LPVOID, LPVOID)


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", DWORD), ("dwCurrentState", DWORD), ("dwControlsAccepted", DWORD),
                ("dwWin32ExitCode", DWORD), ("dwServiceSpecificExitCode", DWORD), ("dwCheckPoint", DWORD),
                ("dwWaitHint", DWORD)]


class SERVICE_TABLE_ENTRYW(ctypes.Structure):
    _fields_ = [("lpServiceName", LPWSTR), ("lpServiceProc", SERVICE_MAIN)]


advapi32.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER_EX, LPVOID]
advapi32.RegisterServiceCtrlHandlerExW.restype = HANDLE
advapi32.SetServiceStatus.argtypes = [HANDLE, ctypes.POINTER(SERVICE_STATUS)]
advapi32.SetServiceStatus.restype = BOOL
advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]
advapi32.StartServiceCtrlDispatcherW.restype = BOOL
STOPPED, RUNNING, STOP_PENDING = 1, 4, 3
_stop = threading.Event()
_status = None


def _report(state):
    s = SERVICE_STATUS()
    s.dwServiceType = 0x10                                           # SERVICE_WIN32_OWN_PROCESS
    s.dwCurrentState = state
    s.dwControlsAccepted = 0x1 | 0x4 if state == RUNNING else 0      # STOP | SHUTDOWN
    s.dwWaitHint = 4000 if state == STOP_PENDING else 0
    if _status:
        advapi32.SetServiceStatus(_status, ctypes.byref(s))


def _on_control(control, _event, _data, _context):
    if control in (1, 5):                                            # SERVICE_CONTROL_STOP, _SHUTDOWN
        _report(STOP_PENDING)
        _stop.set()
    return 0


def _service_main(_argc, _argv):
    global _status
    try:
        _status = advapi32.RegisterServiceCtrlHandlerExW(SVC_NAME, _handler, None)
        _report(RUNNING)
        note("service started")
        agent, agent_session, complained = None, None, 0.0
        while not _stop.is_set():
            session = kernel32.WTSGetActiveConsoleSessionId()
            alive = agent is not None and kernel32.WaitForSingleObject(agent, 0) == 0x102   # WAIT_TIMEOUT
            if agent is not None and (not alive or session != agent_session):
                if alive:
                    kernel32.TerminateProcess(agent, 0)
                kernel32.CloseHandle(agent)
                agent = None
            if agent is None and session != NO_SESSION:
                try:
                    agent, agent_session = start_agent(session), session
                    note(f"agent started in session {session}")
                except Exception as exc:
                    if time.time() - complained > 60:
                        complained = time.time()
                        note(f"couldn't start the agent in session {session}: {exc}")
            _stop.wait(2.0)
        if agent is not None:
            kernel32.TerminateProcess(agent, 0)
            kernel32.CloseHandle(agent)
        note("service stopped")
    except Exception:
        note("service failed:\n" + traceback.format_exc())
    _report(STOPPED)


_main = SERVICE_MAIN(_service_main)                                   # kept alive for as long as Windows may call them
_handler = HANDLER_EX(_on_control)


def run_as_service():
    table = (SERVICE_TABLE_ENTRYW * 2)()
    table[0].lpServiceName, table[0].lpServiceProc = SVC_NAME, _main
    if not advapi32.StartServiceCtrlDispatcherW(table):
        err = ctypes.get_last_error()
        note(f"not started by Windows' service manager (error {err}); this program is run by the service, not by hand")
        print(f"This is the Juska Input Service. Windows starts it; it can't be run by hand (error {err}).")


# ----- install / remove, via Windows' own service manager -------------------
def _scm():
    return win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)


def _stop_and_delete(scm):
    try:
        old = win32service.OpenService(scm, SVC_NAME, win32service.SERVICE_ALL_ACCESS)
    except Exception:
        return
    try:
        win32service.ControlService(old, win32service.SERVICE_CONTROL_STOP)
        for _ in range(20):
            if win32service.QueryServiceStatus(old)[1] == win32service.SERVICE_STOPPED:
                break
            time.sleep(0.25)
    except Exception:
        pass
    try:
        win32service.DeleteService(old)
    finally:
        win32service.CloseServiceHandle(old)


def install():
    exe = f'"{own_exe()}" run' if "__compiled__" in globals() else f'"{sys.executable}" "{os.path.abspath(__file__)}" run'
    scm = _scm()
    try:
        _stop_and_delete(scm)
        time.sleep(0.5)
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
        note(f"installed as {exe} and started")
        print("JuskaInput installed and started")
    except Exception:
        note("install failed:\n" + traceback.format_exc())
        raise
    finally:
        win32service.CloseServiceHandle(scm)


def remove():
    scm = _scm()
    try:
        _stop_and_delete(scm)
        note("removed")
        print("JuskaInput removed")
    except Exception as exc:
        note(f"not removed: {exc}")
        print(f"JuskaInput not removed: {exc}")
    finally:
        win32service.CloseServiceHandle(scm)


if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "run"
    try:
        if cmd == "install":
            install()
        elif cmd == "remove":
            remove()
        elif cmd == "agent":
            run_agent()
        else:
            run_as_service()
    except Exception:
        note(f"'{cmd}' failed:\n" + traceback.format_exc())
        raise
