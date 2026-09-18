import os
import tempfile
import unittest
from pathlib import Path

try:
    import tkinter as tk

    HAS_TK = True
except Exception:
    HAS_TK = False

HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@unittest.skipUnless(HAS_TK and HAS_DISPLAY, "requires a display and tkinter")
class TestSettingsSmoke(unittest.TestCase):
    """Build the GUI windows to catch construction errors (skipped headless)."""

    def test_dialogs_build(self):
        from clipshare.config import Config
        from clipshare.settings import SettingsDialog, SetupDialog, local_ipv4s
        from clipshare.store import Store
        from clipshare.ui import ClipshareUI

        tmp = tempfile.TemporaryDirectory()
        cfg = Config()
        cfg.config_dir = Path(tmp.name)
        cfg.data_dir = Path(tmp.name)
        store = Store(Path(tmp.name) / "h.db")
        try:
            ui = ClipshareUI(cfg, store)
            dlg = SettingsDialog(ui.root, cfg, store, ui.clipboard)
            setup = SetupDialog(ui.root, cfg, ui)
            ui.root.update_idletasks()
            self.assertIsInstance(local_ipv4s(), list)
            dlg.win.destroy()
            setup.win.destroy()
            ui.root.destroy()
        finally:
            store.close()
            tmp.cleanup()
