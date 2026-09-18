"""LAN sync: TCP JSON protocol with token auth, plus UDP peer discovery."""
from __future__ import annotations

import base64
import json
import logging
import socket
import struct
import threading
import time

from .config import DISCOVERY_PORT, PROTOCOL_VERSION, format_peer, parse_peer
from .store import Store, digest

log = logging.getLogger("clipshare.sync")

MAX_MSG_BYTES = 32 * 1024 * 1024
# History replay batches are capped well under MAX_MSG_BYTES: base64 images
# are ~1.33x their blob size, so a fixed item count can overrun the limit.
MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_BATCH_ITEMS = 50
# History replay is capped: a peer that has accumulated a huge history must not
# spend every reconnect re-sending all of it.
MAX_REPLAY_BYTES = 8 * 1024 * 1024
PING_INTERVAL = 15


class IdleTimeout(Exception):
    """No message arrived, but the stream is still at a message boundary."""


def encode_message(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def send_message(sock: socket.socket, obj: dict) -> None:
    data = encode_message(obj)
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_exact(sock: socket.socket, n: int, idle_ok: bool = False) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        try:
            chunk = sock.recv(min(65536, remaining))
        except socket.timeout:
            # Only safe to shrug off before the first byte: a timeout mid-message
            # would leave the stream desynchronised.
            if idle_ok and not chunks:
                raise IdleTimeout from None
            raise
        if not chunk:
            raise ConnectionError("peer closed connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock: socket.socket) -> dict:
    header = recv_exact(sock, 4, idle_ok=True)
    (length,) = struct.unpack(">I", header)
    if length > MAX_MSG_BYTES:
        raise ConnectionError("message too large")
    payload = recv_exact(sock, length)
    return json.loads(payload.decode("utf-8"))


def item_to_message(item: dict) -> dict:
    """Convert a store row (with text/image) into a wire push message."""
    msg = {
        "type": "push",
        "hash": item["hash"],
        "kind": item["kind"],
        "ts": item["created_at"],
        "source": item["source"],
    }
    if item["kind"] == "text":
        msg["text"] = item["text"]
    else:
        msg["image"] = base64.b64encode(item["image"]).decode("ascii")
    return msg


def message_to_item(msg: dict) -> dict:
    """Convert a wire push message into {hash, kind, payload, created_at, source}."""
    if msg["kind"] == "text":
        payload: str | bytes = msg["text"]
    else:
        payload = base64.b64decode(msg["image"])
    return {
        "hash": msg["hash"],
        "kind": msg["kind"],
        "payload": payload,
        "created_at": msg.get("ts", time.time()),
        "source": msg.get("source", "remote"),
    }


class SyncManager:
    """Runs a listener + outbound connections per peer, fanning out pushes."""

    def __init__(self, config, store: Store, on_remote_item=None):
        self.config = config
        self.store = store
        self.on_remote_item = on_remote_item or (lambda item: None)
        self._lock = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._send_locks: dict[socket.socket, threading.Lock] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._outbound: dict[str, threading.Thread] = {}
        self._outbound_stop: dict[str, threading.Event] = {}
        self._discovery_stop: threading.Event | None = None
        self._discovery_threads: list[threading.Thread] = []
        self.peers: dict[str, tuple[str, str]] = {}  # device_id -> (name, label)
        self.connected: set[str] = set()

    # -- lifecycle -------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        t = threading.Thread(target=self._listener, daemon=True)
        t.start()
        self._threads.append(t)
        for addr in self.config.peers:
            self._spawn_outbound(addr)
        if self.config.discover:
            self._spawn_discovery()

    def stop(self) -> None:
        self._stop.set()
        self._stop_outbound()
        self._stop_discovery()
        with self._lock:
            for s in list(self._conns):
                try:
                    s.close()
                except OSError:
                    pass
        for t in self._threads:
            t.join(timeout=1)

    def status(self) -> list[tuple[str, str, str]]:
        with self._lock:
            out = []
            for device_id, (name, label) in self.peers.items():
                state = "connected" if label in self.connected else "connecting"
                out.append((name, label, state))
            return out

    # -- helpers ---------------------------------------------------

    def _spawn_outbound(self, addr: str) -> None:
        with self._lock:
            if addr in self._outbound:
                return
            stop_evt = threading.Event()
            t = threading.Thread(
                target=self._outbound_loop, args=(addr, stop_evt), daemon=True
            )
            self._outbound[addr] = t
            self._outbound_stop[addr] = stop_evt
        t.start()
        self._threads.append(t)

    def _spawn_discovery(self) -> None:
        with self._lock:
            if self._discovery_stop is not None:
                return
            stop_evt = threading.Event()
            self._discovery_stop = stop_evt
        for target in (self._discovery_loop, self._discovery_listener):
            t = threading.Thread(target=target, args=(stop_evt,), daemon=True)
            t.start()
            self._threads.append(t)
            with self._lock:
                self._discovery_threads.append(t)

    def _stop_outbound(self) -> None:
        with self._lock:
            stops = list(self._outbound_stop.values())
            threads = list(self._outbound.values())
            self._outbound.clear()
            self._outbound_stop.clear()
        for ev in stops:
            ev.set()
        for t in threads:
            t.join(timeout=1)

    def _stop_discovery(self) -> None:
        with self._lock:
            ev = self._discovery_stop
            threads = list(self._discovery_threads)
            self._discovery_stop = None
            self._discovery_threads = []
        if ev is not None:
            ev.set()
        for t in threads:
            t.join(timeout=1)

    def reload(self) -> None:
        """Apply peer/discovery config changes without restarting the app."""
        self._stop_outbound()
        self._stop_discovery()
        for addr in self.config.peers:
            self._spawn_outbound(addr)
        if self.config.discover:
            self._spawn_discovery()

    def _row_to_message(self, row: dict) -> dict | None:
        """Build a push message, loading the image blob when the row needs it."""
        if row["kind"] == "text":
            return item_to_message(row)
        full = self.store.get(row["id"])
        if not full or not full.get("image"):
            return None
        return item_to_message(full)

    def _send_recent(self, sock: socket.socket) -> None:
        items = self.store.recent(limit=self.config.history_limit)
        batch: list[dict] = []
        batch_bytes = 0
        sent_bytes = 0
        for it in items:
            msg = self._row_to_message(it)
            if msg is None:
                continue
            size = len(encode_message(msg))
            if size > MAX_BATCH_BYTES and size + 1024 > MAX_MSG_BYTES:
                log.warning("skipping oversized item %s (%d bytes)", it["hash"][:8], size)
                continue
            if sent_bytes + size > MAX_REPLAY_BYTES:
                log.info("replay capped at %.1f MB; older history not sent",
                         sent_bytes / 1048576)
                break
            if batch and (
                batch_bytes + size > MAX_BATCH_BYTES or len(batch) >= MAX_BATCH_ITEMS
            ):
                self._send(sock, {"type": "items", "items": batch})
                batch, batch_bytes = [], 0
            batch.append(msg)
            batch_bytes += size
            sent_bytes += size
        if batch:
            self._send(sock, {"type": "items", "items": batch})

    def _hello(self) -> dict:
        return {
            "type": "hello",
            "token": self.config.token,
            "name": self.config.device_name,
            "device_id": self.config.device_id,
            "port": self.config.port,
            "version": PROTOCOL_VERSION,
        }

    # -- outbound --------------------------------------------------

    def _outbound_loop(self, addr: str, stop_evt: threading.Event) -> None:
        while not self._stop.is_set() and not stop_evt.is_set():
            host, port = parse_peer(addr)
            label = f"out:{addr}"
            try:
                sock = socket.create_connection((host, port), timeout=5)
                sock.settimeout(30)
                send_message(sock, self._hello())
                msg = recv_message(sock)
                if msg.get("type") != "hello" or msg.get("token") != self.config.token:
                    raise ConnectionError("bad handshake")
                self.peers[msg["device_id"]] = (msg["name"], label)
                self._serve_conn(sock, label)
            except (OSError, ConnectionError, ValueError, json.JSONDecodeError) as exc:
                log.debug("peer %s unreachable: %s", addr, exc)
            finally:
                try:
                    sock.close()
                except (OSError, NameError, UnboundLocalError):
                    pass
                with self._lock:
                    self.connected.discard(label)
            stop_evt.wait(5)

    # -- inbound ---------------------------------------------------

    def _listener(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", self.config.port))
            srv.listen(8)
            srv.settimeout(1.0)
        except OSError as exc:
            log.error("cannot listen on port %s: %s", self.config.port, exc)
            return
        while not self._stop.is_set():
            try:
                sock, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            sock.settimeout(30)
            t = threading.Thread(target=self._inbound, args=(sock,), daemon=True)
            t.start()
            self._threads.append(t)
        srv.close()

    def _inbound(self, sock: socket.socket) -> None:
        try:
            msg = recv_message(sock)
            if msg.get("type") != "hello" or msg.get("token") != self.config.token:
                raise ConnectionError("bad handshake")
            send_message(sock, self._hello())
            self._auto_pair(
                sock.getpeername()[0],
                int(msg.get("port") or self.config.port),
                msg.get("name", "unknown"),
            )
            label = f"in:{sock.getpeername()[0]}:{sock.getpeername()[1]}"
            self.peers[msg["device_id"]] = (msg["name"], label)
            self._serve_conn(sock, label)
        except (OSError, ConnectionError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.debug("inbound connection ended: %s", exc)
            try:
                sock.close()
            except OSError:
                pass

    # -- shared connection loop ------------------------------------

    def _send(self, sock: socket.socket, msg: dict) -> None:
        """Serialise writes: the replay thread and broadcasts share one socket."""
        with self._lock:
            lock = self._send_locks.get(sock)
        if lock is None:
            send_message(sock, msg)
            return
        with lock:
            send_message(sock, msg)

    def _sender(self, sock: socket.socket, label: str, done: threading.Event) -> None:
        """Replay history, then keep the connection warm."""
        try:
            self._send_recent(sock)
            while not self._stop.is_set() and not done.is_set():
                if done.wait(PING_INTERVAL):
                    break
                self._send(sock, {"type": "ping"})
        except (OSError, ConnectionError, struct.error) as exc:
            log.debug("sender for %s ended: %s", label, exc)

    def _serve_conn(self, sock: socket.socket, label: str) -> None:
        # The replay runs in its own thread: both peers replay on connect, and
        # if each blocks in sendall before reading, a history larger than the
        # socket buffers deadlocks the pair until the timeout fires.
        done = threading.Event()
        with self._lock:
            self._conns.add(sock)
            self.connected.add(label)
            self._send_locks[sock] = threading.Lock()
        sender = threading.Thread(
            target=self._sender, args=(sock, label, done), daemon=True
        )
        sender.start()
        try:
            while not self._stop.is_set():
                try:
                    msg = recv_message(sock)
                except IdleTimeout:
                    continue
                self._handle(msg, sock)
        except (
            ConnectionError,
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            struct.error,
        ) as exc:
            log.debug("connection %s ended: %s", label, exc)
        finally:
            done.set()
            with self._lock:
                self._conns.discard(sock)
                self.connected.discard(label)
                self._send_locks.pop(sock, None)
            try:
                sock.close()
            except OSError:
                pass
            sender.join(timeout=1)

    # -- messages --------------------------------------------------

    def broadcast(self, item: dict) -> None:
        """Fan out a locally captured store row to all connected peers."""
        msg = item_to_message(item)
        with self._lock:
            conns = list(self._conns)
        for sock in conns:
            try:
                self._send(sock, msg)
            except (OSError, ConnectionError):
                try:
                    sock.close()
                except OSError:
                    pass

    def _handle(self, msg: dict, sock: socket.socket) -> None:
        mtype = msg.get("type")
        if mtype == "hello":
            if msg.get("token") != self.config.token:
                raise ConnectionError("invalid token")
            self.peers[msg["device_id"]] = (msg["name"], f"in:{sock.getpeername()[0]}")
        elif mtype in ("push", "items"):
            for it in msg.get("items", [msg]) if mtype == "items" else [msg]:
                self._apply_push(it)
        elif mtype in ("ack", "ping"):
            pass
        else:
            log.debug("unknown message type %r", mtype)

    def _apply_push(self, msg: dict) -> None:
        try:
            item = message_to_item(msg)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("ignoring bad push: %s", exc)
            return
        if item["hash"] != digest(item["payload"]):
            log.warning("ignoring push with mismatched hash")
            return
        is_new, _ = self.store.add(
            item["kind"], item["payload"], item["source"], item["created_at"]
        )
        if is_new:
            try:
                self.on_remote_item(item)
            except Exception:
                log.exception("on_remote_item callback failed")

    def _auto_pair(self, host: str, port: int, name: str) -> None:
        """Remember a peer that connected to us so we reconnect after restart.

        Sync is already bidirectional over one connection, so once either
        laptop has the other in its peer list, both stay paired.
        """
        if not self.config.auto_add_peers:
            return
        addr = format_peer(host, port)
        if addr in self.config.peers:
            return
        log.info("auto-paired with %s at %s", name, addr)
        self.config.add_peer(host, port)

    # -- discovery --------------------------------------------------

    def _discovery_loop(self, stop_evt: threading.Event) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = encode_message(
            {
                "type": "discover",
                "name": self.config.device_name,
                "port": self.config.port,
                "device_id": self.config.device_id,
            }
        )
        while not self._stop.is_set() and not stop_evt.is_set():
            try:
                s.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
            except OSError:
                pass
            stop_evt.wait(5)
        s.close()

    def _discovery_listener(self, stop_evt: threading.Event) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", DISCOVERY_PORT))
        except OSError:
            return
        s.settimeout(1.0)
        while not self._stop.is_set() and not stop_evt.is_set():
            try:
                data, addr = s.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if msg.get("type") == "discover" and msg.get("device_id") != self.config.device_id:
                reply = encode_message(
                    {
                        "type": "found",
                        "name": self.config.device_name,
                        "port": self.config.port,
                        "device_id": self.config.device_id,
                    }
                )
                s.sendto(reply, addr)
            elif msg.get("type") == "found" and msg.get("device_id") != self.config.device_id:
                self._note_peer(addr[0], int(msg.get("port") or self.config.port), msg.get("name"))
        s.close()

    def _note_peer(self, host: str, port: int, name: str) -> None:
        addr = format_peer(host, port)
        with self._lock:
            already = addr in self.config.peers
        if not already:
            log.info("discovered peer %s at %s", name, addr)
            self.config.add_peer(host, port)
            self._spawn_outbound(addr)
