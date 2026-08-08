import socket
import tempfile
import time
import unittest
from pathlib import Path

from clipshare.config import Config
from clipshare.store import Store
from clipshare.sync import SyncManager, recv_message, send_message


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
