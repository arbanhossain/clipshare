import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipshare import sync
from clipshare.config import Config
from clipshare.store import Store
from clipshare.sync import (
    MAX_MSG_BYTES,
    IdleTimeout,
    SyncManager,
    encode_message,
    recv_message,
    send_message,
)


def fake_png(seed: int, size: int = 4096) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes([seed]) * size


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, name, port, peers=None):
        cfg = Config()
        cfg.device_name = name
        cfg.device_id = f"id-{name}"
        cfg.port = port
        cfg.token = "test-token"
        cfg.discover = False
        cfg.config_dir = Path(self.tmp.name) / f"cfg-{name}"
        cfg.data_dir = Path(self.tmp.name) / f"data-{name}"
        if peers:
            cfg.peers = peers
        return cfg

    def test_message_roundtrip(self):
        a, b = socket.socketpair()
        send_message(a, {"type": "ping", "data": "héllo"})
        msg = recv_message(b)
        self.assertEqual(msg["type"], "ping")
        self.assertEqual(msg["data"], "héllo")
        a.close()
        b.close()

    def test_bidirectional_sync(self):
        store_a = Store(Path(self.tmp.name) / "a.db")
        store_b = Store(Path(self.tmp.name) / "b.db")
        got_a, got_b = [], []
        cfg_a = self._cfg("a", free_port(), peers=[f"127.0.0.1:{free_port()}"])
        cfg_b = self._cfg("b", free_port())
        # point A's peer entry at B's real port
        cfg_a.peers = [f"127.0.0.1:{cfg_b.port}"]
        mgr_a = SyncManager(cfg_a, store_a, on_remote_item=lambda it: got_a.append(it))
        mgr_b = SyncManager(cfg_b, store_b, on_remote_item=lambda it: got_b.append(it))
        # A's history exists before it connects to B
        store_a.add("text", "history from A", "laptop-a")
        try:
            mgr_b.start()  # B's listener comes up first
            time.sleep(0.2)
            mgr_a.start()

            deadline = time.time() + 6
            while time.time() < deadline and not any(
                it.get("payload") == "history from A" for it in got_b
            ):
                time.sleep(0.05)
            self.assertTrue(
                any(it.get("payload") == "history from A" for it in got_b),
                "A's history never arrived on B",
            )

            # live push B -> A
            store_b.add("text", "live from B", "laptop-b")
            item = store_b.get(store_b.recent(1)[0]["id"])
            mgr_b.broadcast(item)
            deadline = time.time() + 6
            while time.time() < deadline and not any(
                it.get("payload") == "live from B" for it in got_a
            ):
                time.sleep(0.05)
            self.assertTrue(
                any(it.get("payload") == "live from B" for it in got_a),
                "live push from B never arrived on A",
            )
        finally:
            mgr_a.stop()
            mgr_b.stop()
            store_a.close()
            store_b.close()

    def test_image_sync(self):
        png = fake_png(7)
        store_a = Store(Path(self.tmp.name) / "ia.db")
        store_b = Store(Path(self.tmp.name) / "ib.db")
        got_b = []
        cfg_b = self._cfg("b", free_port())
        cfg_a = self._cfg("a", free_port(), peers=[f"127.0.0.1:{cfg_b.port}"])
        mgr_a = SyncManager(cfg_a, store_a)
        mgr_b = SyncManager(cfg_b, store_b, on_remote_item=lambda it: got_b.append(it))
        store_a.add("image", png, "laptop-a")
        try:
            mgr_b.start()
            time.sleep(0.2)
            mgr_a.start()
            deadline = time.time() + 6
            while time.time() < deadline and not got_b:
                time.sleep(0.05)
            self.assertTrue(got_b, "A's image never arrived on B")
            self.assertEqual(got_b[0]["kind"], "image")
            self.assertEqual(got_b[0]["payload"], png)
            stored = store_b.get(store_b.recent(1)[0]["id"])
            self.assertEqual(stored["image"], png)
        finally:
            mgr_a.stop()
            mgr_b.stop()
            store_a.close()
            store_b.close()

    def test_history_replay_chunks_by_size(self):
        """Batches must respect a byte budget: base64 images overrun a count cap."""
        budget = 8192
        store = Store(Path(self.tmp.name) / "chunk.db")
        for i in range(5):
            store.add("image", fake_png(i), "laptop-a")
        mgr = SyncManager(self._cfg("a", free_port()), store)
        sender, receiver = socket.socketpair()
        try:
            with mock.patch.object(sync, "MAX_BATCH_BYTES", budget):
                mgr._send_recent(sender)
            sender.close()
            receiver.settimeout(2)
            messages = []
            while True:
                try:
                    messages.append(recv_message(receiver))
                except (ConnectionError, OSError):
                    break
            total = sum(len(m["items"]) for m in messages)
            self.assertEqual(total, 5, "not every image was replayed")
            self.assertGreater(len(messages), 1, "history was sent as one huge message")
            for m in messages:
                size = len(encode_message(m))
                self.assertLess(size, MAX_MSG_BYTES, "message exceeds the receiver's cap")
                self.assertTrue(
                    len(m["items"]) == 1 or size <= budget,
                    f"batch of {len(m['items'])} items is {size} bytes, over budget",
                )
        finally:
            sender.close()
            receiver.close()
            store.close()

    def test_large_history_does_not_stall(self):
        """Both peers replay on connect; neither may block the other out."""
        blob = "x" * 40000  # ~5 MB each side, past any socket buffer
        store_a = Store(Path(self.tmp.name) / "la.db")
        store_b = Store(Path(self.tmp.name) / "lb.db")
        for i in range(125):
            store_a.add("text", f"A{i}-{blob}", "laptop-a")
            store_b.add("text", f"B{i}-{blob}", "laptop-b")
        got_a, got_b = [], []
        cfg_b = self._cfg("b", free_port())
        cfg_a = self._cfg("a", free_port(), peers=[f"127.0.0.1:{cfg_b.port}"])
        mgr_a = SyncManager(cfg_a, store_a, on_remote_item=lambda it: got_a.append(it))
        mgr_b = SyncManager(cfg_b, store_b, on_remote_item=lambda it: got_b.append(it))
        try:
            mgr_b.start()
            time.sleep(0.2)
            mgr_a.start()
            deadline = time.time() + 30
            while time.time() < deadline and (len(got_a) < 125 or len(got_b) < 125):
                time.sleep(0.1)
            self.assertEqual(
                (len(got_a), len(got_b)), (125, 125),
                "history replay deadlocked: each peer blocked in sendall "
                "before reading the other's replay",
            )
        finally:
            mgr_a.stop()
            mgr_b.stop()
            store_a.close()
            store_b.close()

    def test_idle_socket_is_not_a_framing_error(self):
        a, b = socket.socketpair()
        b.settimeout(0.2)
        with self.assertRaises(IdleTimeout):
            recv_message(b)
        # the stream is still usable afterwards
        send_message(a, {"type": "ping"})
        self.assertEqual(recv_message(b)["type"], "ping")
        a.close()
        b.close()

    def test_ping_is_accepted(self):
        cfg = self._cfg("a", free_port())
        store = Store(Path(self.tmp.name) / "p.db")
        mgr = SyncManager(cfg, store)
        a, b = socket.socketpair()
        try:
            mgr._handle({"type": "ping"}, a)  # must not raise
        finally:
            a.close()
            b.close()
            store.close()

    def test_auto_pair(self):
        port_a = free_port()
        port_b = free_port()
        cfg_a = self._cfg("a", port_a, peers=[f"127.0.0.1:{port_b}"])
        cfg_b = self._cfg("b", port_b)
        store_a = Store(Path(self.tmp.name) / "aa.db")
        store_b = Store(Path(self.tmp.name) / "bb.db")
        mgr_a = SyncManager(cfg_a, store_a)
        mgr_b = SyncManager(cfg_b, store_b)
        try:
            mgr_b.start()
            time.sleep(0.2)
            mgr_a.start()
            deadline = time.time() + 6
            while time.time() < deadline and f"127.0.0.1:{port_a}" not in cfg_b.peers:
                time.sleep(0.05)
            self.assertIn(
                f"127.0.0.1:{port_a}", cfg_b.peers,
                "B never remembered A, so pairing is not two-way by default",
            )
        finally:
            mgr_a.stop()
            mgr_b.stop()
            store_a.close()
            store_b.close()

    def test_reload_connects_new_peers(self):
        port_b = free_port()
        cfg_a = self._cfg("a", free_port())
        cfg_b = self._cfg("b", port_b)
        store_a = Store(Path(self.tmp.name) / "ra.db")
        store_b = Store(Path(self.tmp.name) / "rb.db")
        got = []
        mgr_a = SyncManager(cfg_a, store_a, on_remote_item=lambda it: got.append(it))
        mgr_b = SyncManager(cfg_b, store_b)
        try:
            mgr_b.start()
            time.sleep(0.2)
            mgr_a.start()
            time.sleep(0.3)
            self.assertFalse(mgr_a.status(), "A should have no peers initially")

            cfg_a.peers = [f"127.0.0.1:{port_b}"]
            mgr_a.reload()
            deadline = time.time() + 6
            while time.time() < deadline and not any(
                state == "connected" for _, _, state in mgr_a.status()
            ):
                time.sleep(0.05)
            self.assertTrue(
                any(state == "connected" for _, _, state in mgr_a.status()),
                "A never connected after reload",
            )

            store_b.add("text", "post-reload", "laptop-b")
            item = store_b.get(store_b.recent(1)[0]["id"])
            mgr_b.broadcast(item)
            deadline = time.time() + 6
            while time.time() < deadline and not any(
                it.get("payload") == "post-reload" for it in got
            ):
                time.sleep(0.05)
            self.assertTrue(
                any(it.get("payload") == "post-reload" for it in got),
                "push after reload never arrived on A",
            )
        finally:
            mgr_a.stop()
            mgr_b.stop()
            store_a.close()
            store_b.close()

    def test_bad_token_rejected(self):
        cfg = self._cfg("a", free_port())
        store = Store(Path(self.tmp.name) / "a.db")
        mgr = SyncManager(cfg, store)
        try:
            mgr.start()
            time.sleep(0.2)
            s = socket.create_connection(("127.0.0.1", cfg.port), timeout=2)
            send_message(s, {"type": "hello", "token": "wrong", "name": "x",
                             "device_id": "x", "version": 1})
            s.settimeout(2)
            with self.assertRaises((ConnectionError, OSError)):
                recv_message(s)
            s.close()
        finally:
            mgr.stop()
            store.close()
