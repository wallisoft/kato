"""
Juska: large copies.

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
XFER_FROM = "0.9.4"                    # the first Juska that can receive one
OLD_FILES_MAX = C.FILES_MAX_TOTAL      # what older versions accept (sized for the shared connection)
C.FILES_MAX_TOTAL = 200 * 1024 ** 3    # with its own connection, a copy can be far larger
SPARE_DISK = 512 * 1024 ** 2           # never fill the receiving disk to the brim
READERS = 8                            # files read at once (opening a file is the slow part on Windows)


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
    """Mixed into Juska ahead of ClipboardSync and Mesh."""

    def _xfer_init(self):
        self._xfer, self._xfer_wait, self._xfer_expect = None, {}, {}
        self._xfer_batches, self._xfer_lock = {}, threading.Lock()

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
    # The core starts one of these per connected machine, all at once, for the same copy. They are gathered into
    # one batch so the files are read ONCE and fed to every machine: on Windows, opening a file is the slow part
    # (the virus scanner looks at each one first), and a folder of small files was being opened twice over.
    def _send_files(self, link, paths, total):
        if total <= BIG_FILES_FROM:
            return ClipboardSync._send_files(self, link, paths, total)
        key = tuple(paths)
        with self._xfer_lock:
            batch = self._xfer_batches.get(key)
            if batch is not None:
                batch.append(link)
                return
            batch = self._xfer_batches[key] = [link]
        time.sleep(0.4)                                           # let the other machines' threads join in
        with self._xfer_lock:
            self._xfer_batches.pop(key, None)
        self._send_big(batch, list(paths), total)

    def _send_big(self, links, paths, total):
        name_of = lambda l: self.peers.get(l.peer_id, {}).get("name", "another machine")
        size = human_size(total)
        # 1. ask each machine (older versions get it the old way, or are told they need updating)
        asked = []
        for link in links:
            name = name_of(link)
            if _vt(getattr(self, "versions", {}).get(name, "0")) < _vt(XFER_FROM):
                if total <= OLD_FILES_MAX:
                    threading.Thread(target=ClipboardSync._send_files, args=(self, link, paths, total),
                                     daemon=True).start()
                else:
                    self.notify(f"{name} needs Juska {XFER_FROM} or newer to receive {size}.")
                    log(f"not sending {size} to {name}: it needs Juska {XFER_FROM} or newer")
                continue
            fid, answer = uuid.uuid4().hex, threading.Event()
            self._xfer_wait[fid] = [answer, None]
            link.send({"type": "xfer_offer", "id": fid, "total": total})
            asked.append((link, fid, answer))
        deadline, carriers = time.time() + 8, []
        for link, fid, answer in asked:
            name = name_of(link)
            answered = answer.wait(max(deadline - time.time(), 0.1))
            why = self._xfer_wait.pop(fid)[1]
            if not answered or why:
                why = why or "it didn't answer"
                self.notify(f"{name} can't take {size} right now: {why}.")
                log(f"not sending {size} to {name}: {why}")
                continue
            try:
                sock = socket.create_connection((link.sock.getpeername()[0], self.peers[link.peer_id]["port"]), timeout=5)
                tls = self.cli_ctx.wrap_socket(sock)
                tls.settimeout(60)
                carriers.append(TransferLink(tls, link.peer_id))
                carriers[-1].name = name
            except Exception as exc:
                log(f"couldn't open a connection to {name} for {size}: {exc}")
                self.notify(f"Sending {size} to {name} stopped: {exc}")
        if not carriers:
            return
        # 2. read once, send to all of them
        started = time.time()
        names = ", ".join(c.name for c in carriers)
        self._xfer = {"verb": "Sending", "name": names, "done": 0, "total": total, "shown": 0.0}
        log(f"sending {size} to {names}, each on a connection of its own")
        try:
            count = self._push(carriers, paths, total)
            for c in carriers:
                if c.alive:
                    try:
                        recv_frame(c.sock)                        # that machine says it has everything
                    except Exception as exc:
                        c.alive, c.error = False, str(exc)
            secs = max(time.time() - started, 0.1)
            for c in carriers:
                if c.alive:
                    log(f"{c.name} has all {size}: {count} file(s) in {secs:.0f}s, {human_size(int(total / secs))} a second")
                else:
                    log(f"sending {size} to {c.name} stopped: {c.error or 'the connection was lost'}")
                    self.notify(f"Sending {size} to {c.name} stopped: {c.error or 'the connection was lost'}")
        finally:
            self._xfer = None
            for c in carriers:
                try:
                    c.sock.close()
                except Exception:
                    pass
            self.refresh_ui()

    def _push(self, carriers, paths, total):
        """Send the files to every carrier. Small files are read several at a time; one lock keeps each
        message whole on the wire. Returns how many files went."""
        import os
        import queue
        fid, wire = uuid.uuid4().hex, threading.Lock()

        def send(header, payload=b""):
            with wire:
                for c in carriers:
                    c.send(header, payload)
            if payload:
                self._xfer_progress(len(payload))

        send({"type": "files_begin", "id": fid, "total": total,
              "items": [os.path.basename(p.rstrip("/\\")) for p in paths]})
        files = []
        for p in paths:
            p = p.rstrip("/\\")
            base = os.path.dirname(p)
            if os.path.isdir(p):
                for root, _dirs, names in os.walk(p):
                    rel_root = os.path.relpath(root, base).replace(os.sep, "/")
                    send({"type": "dir", "id": fid, "path": rel_root})
                    files += [(os.path.join(root, f), rel_root + "/" + f) for f in names]
            elif os.path.isfile(p):
                files.append((p, os.path.basename(p)))
        todo = queue.Queue()
        for item in files:
            todo.put(item)

        def worker():
            while any(c.alive for c in carriers):
                try:
                    full, rel = todo.get_nowait()
                except queue.Empty:
                    return
                offset = 0
                try:
                    with open(full, "rb") as fh:
                        while any(c.alive for c in carriers):
                            chunk = fh.read(C.CHUNK_BYTES)
                            eof = len(chunk) < C.CHUNK_BYTES
                            send({"type": "file_chunk", "id": fid, "path": rel, "offset": offset, "eof": eof}, chunk)
                            offset += len(chunk)
                            if eof:
                                break
                except OSError as exc:
                    log(f"skipped {full}: {exc}")

        workers = [threading.Thread(target=worker, daemon=True) for _ in range(min(READERS, max(len(files), 1)))]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        send({"type": "files_end", "id": fid})
        return len(files)

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
