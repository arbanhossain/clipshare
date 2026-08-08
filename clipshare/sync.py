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


def encode_message(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def send_message(sock: socket.socket, obj: dict) -> None:
    data = encode_message(obj)
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(min(65536, remaining))
        if not chunk:
            raise ConnectionError("peer closed connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock: socket.socket) -> dict:
    header = recv_exact(sock, 4)
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
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
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
        t = threading.Thread(target=self._outbound_loop, args=(addr,), daemon=True)
        t.start()
        self._threads.append(t)

    def _spawn_discovery(self) -> None:
        for target in (self._discovery_loop, self._discovery_listener):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def _send_recent(self, sock: socket.socket) -> None:
        items = self.store.recent(limit=self.config.history_limit)
        for i in range(0, len(items), 50):
            chunk = []
            for it in items[i : i + 50]:
                msg = {
                    "type": "push",
                    "hash": it["hash"],
                    "kind": it["kind"],
                    "ts": it["created_at"],
                    "source": it["source"],
                }
                if it["kind"] == "text":
                    msg["text"] = it["text"]
                else:
                    full = self.store.get(it["id"])
                    if full and full.get("image"):
                        msg["image"] = base64.b64encode(full["image"]).decode("ascii")
                    else:
                        continue
                chunk.append(msg)
            if chunk:
                send_message(sock, {"type": "items", "items": chunk})

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

    def _outbound_loop(self, addr: str) -> None:
        while not self._stop.is_set():
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
                self._send_recent(sock)
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
            self._stop.wait(5)

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
            self._send_recent(sock)
            self._serve_conn(sock, label)
        except (OSError, ConnectionError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.debug("inbound connection ended: %s", exc)
            try:
                sock.close()
            except OSError:
                pass

    # -- shared connection loop ------------------------------------

    def _serve_conn(self, sock: socket.socket, label: str) -> None:
        with self._lock:
            self._conns.add(sock)
            self.connected.add(label)
        try:
            while not self._stop.is_set():
                msg = recv_message(sock)
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
            with self._lock:
                self._conns.discard(sock)
                self.connected.discard(label)
            try:
                sock.close()
            except OSError:
                pass

    # -- messages --------------------------------------------------

    def broadcast(self, item: dict) -> None:
        """Fan out a locally captured store row to all connected peers."""
        msg = item_to_message(item)
        with self._lock:
            conns = list(self._conns)
        for sock in conns:
            try:
                send_message(sock, msg)
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
        elif mtype == "ack":
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

    def _discovery_loop(self) -> None:
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
        while not self._stop.is_set():
            try:
                s.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
            except OSError:
                pass
            self._stop.wait(5)
        s.close()

    def _discovery_listener(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", DISCOVERY_PORT))
        except OSError:
            return
        s.settimeout(1.0)
        while not self._stop.is_set():
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
