"""
AirShare: large copies.

The clipboard connection is also the keyboard-and-mouse connection, so a big copy sent down it makes the pointer
wait behind megabytes of file. Anything over BIG_FILES_FROM therefore travels on a connection of its own, to the
same port and with the same pinned certificates, written to disk as it arrives. The two machines agree it first
over the ordinary connection (so a full disk or a paused machine says no before anything is sent), and the machine
receiving treats the next connection from that sender as the transfer.
"""
import json
import shutil
import socket
import struct
import threading
import time
import uuid

from clickboard_core import ClipboardSync, log
from clickboard_core import common as C
from clickboard_core.clipboard import human_size
from clickboard_core.identity import der_fingerprint
from clickboard_core.protocol import recv_frame

BIG_FILES_FROM = 32 * 1024 * 1024      # copies larger than this get a connection of their own
XFER_FROM = "0.9.4"                    # the first AirShare that can receive one
OLD_FILES_MAX = C.FILES_MAX_TOTAL      # what older AirShares accept (sized for the shared connection)
C.FILES_MAX_TOTAL = 200 * 1024 ** 3    # with its own connection, a copy can be far larger
SPARE_DISK = 512 * 1024 ** 2           # never fill the receiving disk to the brim


def _vt(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except ValueError:
        return (0,)


class TransferLink:
    """Stands in for a Link on a connection that carries one large copy and nothing else."""

    def __init__(self, sock, peer_id, on_bytes=None):
        self.sock, self.peer_id, self.alive, self.rx, self.error, self.on_bytes = sock, peer_id, True, {}, "", on_bytes

    def send(self, header, payload=b""):
        if not self.alive:
            return
        h = json.dumps(dict(header, size=len(payload))).encode()
        try:
            self.sock.sendall(struct.pack(">I", len(h)) + h + payload)
            if payload and self.on_bytes:
                self.on_bytes(len(payload))
        except Exception as exc:
            self.alive, self.error = False, str(exc)


class BigFiles:
    """Mixed into AirShare ahead of ClipboardSync and Mesh."""

    def _xfer_init(self):
        self._xfer, self._xfer_wait, self._xfer_expect = None, {}, {}

    def transfer_line(self):
        """What the tray should say while a large copy is on its way, or ''."""
        x = getattr(self, "_xfer", None)
        if not x:
            return ""
        pct = int(100 * x["done"] / max(x["total"], 1))
        return (f"{x['verb']} {human_size(x['done'])} of {human_size(x['total'])} ({pct}%) "
                f"{'to' if x['verb'] == 'Sending' else 'from'} {x['name']}")

    def _xfer_progress(self, n):
        x = self._xfer
        if x:
            x["done"] += n
            if time.time() - x["shown"] > 1.0:
                x["shown"] = time.time()
                self.refresh_ui()

    # ---- sending ----
    def _send_files(self, link, paths, total):
        name = self.peers.get(link.peer_id, {}).get("name", "another machine")
        if total <= BIG_FILES_FROM:
            return ClipboardSync._send_files(self, link, paths, total)
        if _vt(getattr(self, "versions", {}).get(name, "0")) < _vt(XFER_FROM):
            if total <= OLD_FILES_MAX:
                return ClipboardSync._send_files(self, link, paths, total)       # the old way, which it understands
            self.notify(f"{name} needs AirShare {XFER_FROM} or newer to receive {human_size(total)}.")
            log(f"not sending {human_size(total)} to {name}: it needs AirShare {XFER_FROM} or newer")
            return
        fid, answer = uuid.uuid4().hex, threading.Event()
        self._xfer_wait[fid] = [answer, None]
        link.send({"type": "xfer_offer", "id": fid, "total": total})
        answered = answer.wait(8)
        why = self._xfer_wait.pop(fid)[1]
        if not answered or why:
            why = why or "it didn't answer"
            self.notify(f"{name} can't take {human_size(total)} right now: {why}.")
            log(f"not sending {human_size(total)} to {name}: {why}")
            return
        started, tls = time.time(), None
        try:
            sock = socket.create_connection((link.sock.getpeername()[0], self.peers[link.peer_id]["port"]), timeout=5)
            tls = self.cli_ctx.wrap_socket(sock)
            tls.settimeout(60)
            self._xfer = {"verb": "Sending", "name": name, "done": 0, "total": total, "shown": 0.0}
            carrier = TransferLink(tls, link.peer_id, self._xfer_progress)
            log(f"sending {human_size(total)} to {name} on a connection of its own")
            ClipboardSync._send_files(self, carrier, paths, total)
            if not carrier.alive:
                raise ConnectionError(carrier.error or "the connection was lost")
            recv_frame(tls)                                       # the other machine says it has everything
            secs = max(time.time() - started, 0.1)
            log(f"{name} has all {human_size(total)} (took {secs:.0f}s, {human_size(int(total / secs))} a second)")
        except Exception as exc:
            log(f"sending {human_size(total)} to {name} stopped: {exc}")
            self.notify(f"Sending {human_size(total)} to {name} stopped: {exc}")
        finally:
            self._xfer = None
            try:
                if tls:
                    tls.close()
            except Exception:
                pass
            self.refresh_ui()

    # ---- the two machines agreeing it first, over the ordinary connection ----
    def on_transfer_frame(self, link, header):
        """True if the frame was one of ours."""
        t = header.get("type")
        if t == "xfer_offer":
            threading.Thread(target=self._xfer_offered, args=(link, header), daemon=True).start()
            return True
        if t == "xfer_answer":
            waiting = self._xfer_wait.get(str(header.get("id", "")))
            if waiting:
                waiting[1] = str(header.get("why", ""))
                waiting[0].set()
            return True
        return False

    def _xfer_offered(self, link, header):
        total, why = int(header.get("total", 0)), ""
        if getattr(self, "paused", False) or getattr(self.cfg, "paused", False):
            why = "sharing is paused there"
        else:
            try:
                self.received_dir.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(self.received_dir).free
                if free < total + SPARE_DISK:
                    why = f"it has only {human_size(free)} of disk space free"
            except OSError as exc:
                why = f"its folder for received files can't be used ({exc})"
        if not why:
            fp = self.peers.get(link.peer_id, {}).get("cert_fp")
            self._xfer_expect[fp] = (time.time() + 12, total)
        link.send({"type": "xfer_answer", "id": str(header.get("id", "")), "why": why})

    # ---- receiving: the next connection from a machine we said yes to is its transfer ----
    def _accept(self, conn, addr):
        try:
            conn.settimeout(10)
            tls = self.srv_ctx.wrap_socket(conn, server_side=True)
            fp = der_fingerprint(tls.getpeercert(binary_form=True))
            expected = self._xfer_expect.pop(fp, None)
            if expected and expected[0] > time.time():
                self._xfer_receive(tls, fp, expected[1])
                return
            self._adopt(tls, initiated_by_me=False)
        except Exception as exc:
            log(f"refused connection from {addr[0]}: {exc}")
            try:
                conn.close()
            except Exception:
                pass
            self.poke_register()

    def _xfer_receive(self, tls, fp, total):
        peer = next((p for p in self.peers.values() if p.get("cert_fp") == fp), {})
        name = peer.get("name", "another machine")
        carrier = TransferLink(tls, peer.get("device_id", ""))
        self._xfer = {"verb": "Receiving", "name": name, "done": 0, "total": total, "shown": 0.0}
        log(f"receiving {human_size(total)} from {name} on a connection of its own")
        try:
            tls.settimeout(60)
            while True:
                header, payload = recv_frame(tls)
                if payload:
                    self._xfer_progress(len(payload))
                kind = header.get("type")
                if kind in ("files_begin", "dir", "file_chunk", "files_end"):
                    self.on_remote_files(carrier, header, payload)
                if kind == "files_end":
                    carrier.send({"type": "xfer_done"})
                    break
        except Exception as exc:
            for rx in carrier.rx.values():                        # part of a copy is worse than none: keep nothing
                for fh in rx["open"].values():
                    try:
                        fh.close()
                    except Exception:
                        pass
                shutil.rmtree(rx["dest"], ignore_errors=True)
            log(f"receiving {human_size(total)} from {name} stopped: {exc}")
            self.notify(f"Receiving {human_size(total)} from {name} stopped part-way, so nothing was kept.")
        finally:
            self._xfer = None
            try:
                tls.close()
            except Exception:
                pass
            self.refresh_ui()
