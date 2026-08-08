"""Clipboard capture/set across X11 (tkinter) and Wayland (wl-clipboard)."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Clip:
    kind: str  # "text" | "image" | "none"
    payload: str | bytes
    digest: str = ""


def _digest(payload: str | bytes) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()


class Clipboard:
    """Reads and writes the system clipboard.

    Prefers wl-paste/wl-copy on Wayland; falls back to Tk on X11.
    """

    def __init__(self, root: tk.Tk, max_image_bytes: int = 10 * 1024 * 1024):
        self.root = root
        self.max_image_bytes = max_image_bytes
        self.wayland = (
            bool(os.environ.get("WAYLAND_DISPLAY"))
            and shutil.which("wl-paste") is not None
            and shutil.which("wl-copy") is not None
        )
        self.xclip = shutil.which("xclip")

    def get(self) -> Clip:
        clip = self._get_wayland() if self.wayland else self._get_tk()
        if clip.kind == "none":
            return clip
        return Clip(clip.kind, clip.payload, _digest(clip.payload))

    def set_text(self, text: str) -> None:
        if self.wayland:
            subprocess.run(
                ["wl-copy", "--type", "text/plain"],
                input=text.encode("utf-8", "surrogatepass"),
                timeout=3,
                check=False,
            )
        else:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()

    def set_image(self, png_bytes: bytes) -> None:
        if self.wayland:
            subprocess.run(
                ["wl-copy", "--type", "image/png"],
                input=png_bytes,
                timeout=3,
                check=False,
            )
            return
        if self.xclip:
            subprocess.run(
                [self.xclip, "-selection", "clipboard", "-t", "image/png", "-i"],
                input=png_bytes,
                timeout=3,
                check=False,
            )
            return
        raise RuntimeError("Install xclip or wl-clipboard to set an image clipboard")

    def _get_tk(self) -> Clip:
        try:
            text = self.root.clipboard_get()
            if text:
                return Clip("text", text)
        except tk.TclError:
            pass
        try:
            img = self.root.clipboard_get(type="image")
        except tk.TclError:
            return Clip("none", "")
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp_path = f.name
            img.write(tmp_path, format="png")
            data = Path(tmp_path).read_bytes()
            os.unlink(tmp_path)
        except Exception:
            return Clip("none", "")
        if not data or len(data) > self.max_image_bytes:
            return Clip("none", "")
        return Clip("image", data)

    def _get_wayland(self) -> Clip:
        try:
            proc = subprocess.run(
                ["wl-paste", "--no-newline"], capture_output=True, timeout=3
            )
            text = proc.stdout.decode("utf-8", "surrogatepass")
            if proc.returncode == 0 and text:
                return Clip("text", text)
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            proc = subprocess.run(
                ["wl-paste", "--type", "image/png"], capture_output=True, timeout=3
            )
            if (
                proc.returncode == 0
                and proc.stdout
                and len(proc.stdout) <= self.max_image_bytes
            ):
                return Clip("image", proc.stdout)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return Clip("none", "")
