"""Optional system tray integration via pystray (fallback: window only)."""
from __future__ import annotations

import threading

try:
    import pystray
    from PIL import Image, ImageDraw

    HAS_TRAY = True
except ImportError:
    pystray = None
    HAS_TRAY = False


def make_icon_image():
    img = Image.new("RGB", (64, 64), (30, 144, 255))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill=(30, 144, 255))
    draw.text((24, 18), "C", fill="white")
    return img


class Tray:
    def __init__(self, on_show, on_quit):
        self.on_show = on_show
        self.on_quit = on_quit
        self._icon = None
        self._thread = None

    def start(self) -> bool:
        if not HAS_TRAY:
            return False
        menu = pystray.Menu(
            pystray.MenuItem("Show history", lambda: self.on_show()),
            pystray.MenuItem("Quit", lambda: self.on_quit()),
        )
        self._icon = pystray.Icon("clipshare", make_icon_image(), "Clipshare", menu)
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()
        return True

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None and HAS_TRAY:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is not None and HAS_TRAY:
            try:
                self._icon.stop()
            except Exception:
                pass
