import tempfile
import time
import unittest
from pathlib import Path

from clipshare.store import Store


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_and_dedup(self):
        is_new, item_id = self.store.add("text", "hello", "laptop-a")
        self.assertTrue(is_new)
        again, same_id = self.store.add("text", "hello", "laptop-b")
        self.assertFalse(again)
        self.assertEqual(item_id, same_id)

    def test_image_roundtrip(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        is_new, item_id = self.store.add("image", png, "laptop-a")
        self.assertTrue(is_new)
        item = self.store.get(item_id)
        self.assertEqual(item["image"], png)
        self.assertIsNone(item["text"])

    def test_search(self):
        self.store.add("text", "python lambda", "a")
        self.store.add("text", "rust traits", "b")
        found = self.store.search("lambda")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["text"], "python lambda")

    def test_pin_and_delete(self):
        _, item_id = self.store.add("text", "keep me", "a")
        self.store.set_pinned(item_id, True)
        self.assertEqual(self.store.get(item_id)["pinned"], 1)
        self.assertTrue(self.store.delete(item_id))
        self.assertIsNone(self.store.get(item_id))

    def test_prune_keeps_pinned(self):
        _, old = self.store.add("text", "old", "a", ts=time.time() - 999 * 86400)
        _, pinned = self.store.add("text", "pinned", "a")
        self.store.set_pinned(pinned, True)
        removed = self.store.prune(retention_days=30, history_limit=500)
        self.assertEqual(removed, 1)
        self.assertIsNone(self.store.get(old))
        self.assertIsNotNone(self.store.get(pinned))
