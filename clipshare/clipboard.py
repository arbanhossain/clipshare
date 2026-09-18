"""Clipboard capture/set across X11 (xclip/tkinter) and Wayland (wl-clipboard)."""
from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import subprocess
import tkinter as tk
from dataclasses import dataclass

log = logging.getLogger("clipshare.clipboard")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Targets that mean "there is text here"; checked case-insensitively.
TEXT_TARGETS = (
    "text/plain;charset=utf-8",
    "text/plain",
    "utf8_string",
    "string",
    "text",
)
# Image targets in decode preference order; PNG first so the common case
# needs no re-encode.
IMAGE_TARGETS = (
    "image/png",
    "image/bmp",
    "image/tiff",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
)
# Modes Pillow can write straight to PNG; anything else gets converted.
PNG_MODES = {"1", "L", "LA", "I", "P", "RGB", "RGBA"}


@dataclass
class Clip:
    kind: str  # "text" | "image" | "none"
    payload: str | bytes
    digest: str = ""


def _digest(payload: str | bytes) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def pick_text_target(types: list[str]) -> str | None:
    lower = {t.lower(): t for t in types}
    for want in TEXT_TARGETS:
        if want in lower:
            return lower[want]
    return None


def pick_image_target(types: list[str]) -> str | None:
    lower = {t.lower(): t for t in types}
    for want in IMAGE_TARGETS:
        if want in lower:
            return lower[want]
    for t in types:
        if t.lower().startswith("image/"):
            return t
    return None


def to_png(data: bytes, target: str) -> bytes | None:
    """Normalise clipboard image bytes to PNG, or None if undecodable.

    Everything downstream (store, preview, wire) assumes PNG, so JPEG/BMP
    offers from apps that do not publish image/png get converted here.
    """
    if data.startswith(PNG_MAGIC):
        return data
    try:
        from PIL import Image
    except ImportError:
        log.warning("cannot convert %s to PNG without Pillow", target)
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.mode not in PNG_MODES:
                img = img.convert("RGBA")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
    except Exception as exc:  # Pillow raises many error types on bad data
        log.debug("cannot decode %s clipboard image: %s", target, exc)
        return None
    return buf.getvalue()


class Clipboard:
    """Reads and writes the system clipboard.

    Text comes from wl-paste on Wayland and Tk on X11. Images need a helper
    binary either way — Tk cannot read or write binary selection targets —
    so image support depends on wl-clipboard or xclip being installed.
    """

    def __init__(self, root: tk.Tk | None, max_image_bytes: int = 10 * 1024 * 1024):
        self.root = root
        self.max_image_bytes = max_image_bytes
        self.wayland = (
            bool(os.environ.get("WAYLAND_DISPLAY"))
            and shutil.which("wl-paste") is not None
            and shutil.which("wl-copy") is not None
        )
        self.xclip = shutil.which("xclip")

    # -- capability reporting ---------------------------------------

    @property
    def image_tool(self) -> str | None:
        if self.wayland:
            return "wl-clipboard"
        if self.xclip:
            return "xclip"
        return None

    def image_help(self) -> str:
        """Empty when images work, else what the user has to install."""
        if self.image_tool:
            return ""
        if os.environ.get("WAYLAND_DISPLAY"):
            return "Images need wl-clipboard: sudo apt install wl-clipboard"
        return "Images need xclip: sudo apt install xclip"

    # -- capture ----------------------------------------------------

    def get(self) -> Clip:
        if self.wayland:
            clip = self._get_wayland()
        elif self.xclip:
            clip = self._get_x11()
        else:
            clip = self._get_tk_text()
        if clip.kind == "none":
            return clip
        return Clip(clip.kind, clip.payload, _digest(clip.payload))

    def _get_wayland(self) -> Clip:
        types = self._list_types()
        if not types:
            return Clip("none", "")
        text_target = pick_text_target(types)
        if text_target:
            data = self._read(["wl-paste", "--no-newline", "--type", text_target])
            return self._text_clip(data)
        target = pick_image_target(types)
        if target is None:
            return Clip("none", "")
        return self._image_clip(self._read(["wl-paste", "--type", target]), target)

    def _get_x11(self) -> Clip:
        # Tk handles text without spawning a process, and owning apps that
        # offer text alongside an image (spreadsheets, browsers) should paste
        # as text — so only shell out to xclip when Tk finds no text.
        clip = self._get_tk_text()
        if clip.kind != "none":
            return clip
        target = pick_image_target(self._list_types())
        if target is None:
            return Clip("none", "")
        data = self._read([self.xclip, "-selection", "clipboard", "-t", target, "-o"])
        return self._image_clip(data, target)

    def _get_tk_text(self) -> Clip:
        if self.root is None:
            return Clip("none", "")
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return Clip("none", "")
        return Clip("text", text) if text else Clip("none", "")

    def _text_clip(self, data: bytes | None) -> Clip:
        if not data:
            return Clip("none", "")
        try:
            text = data.decode("utf-8", "surrogatepass")
        except UnicodeDecodeError:
            return Clip("none", "")
        return Clip("text", text) if text else Clip("none", "")

    def _image_clip(self, data: bytes | None, target: str) -> Clip:
        if not data:
            return Clip("none", "")
        if len(data) > self.max_image_bytes:
            log.info("skipping %s clipboard image: %d bytes", target, len(data))
            return Clip("none", "")
        png = to_png(data, target)
        if not png:
            return Clip("none", "")
        if len(png) > self.max_image_bytes:
            log.info("skipping converted clipboard image: %d bytes", len(png))
            return Clip("none", "")
        return Clip("image", png)

    def _list_types(self) -> list[str]:
        """MIME types / X11 targets the clipboard currently offers."""
        if self.wayland:
            out = self._read(["wl-paste", "--list-types"])
        elif self.xclip:
            out = self._read([self.xclip, "-selection", "clipboard", "-t", "TARGETS", "-o"])
        else:
            return []
        if not out:
            return []
        return [line.strip() for line in out.decode("utf-8", "replace").splitlines() if line.strip()]

    # -- writing ----------------------------------------------------

    def set_text(self, text: str) -> None:
        data = text.encode("utf-8", "surrogatepass")
        if self.wayland:
            if not self._write(["wl-copy", "--type", "text/plain"], data):
                raise RuntimeError("wl-copy failed to set the clipboard")
            return
        if self.root is not None:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            return
        if self.xclip and self._write(
            [self.xclip, "-selection", "clipboard", "-t", "text/plain", "-i"], data
        ):
            return
        raise RuntimeError("no way to set the clipboard")

    def set_image(self, png_bytes: bytes) -> None:
        if self.wayland:
            if not self._write(["wl-copy", "--type", "image/png"], png_bytes):
                raise RuntimeError("wl-copy failed to set the image clipboard")
            return
        if self.xclip:
            if not self._write(
                [self.xclip, "-selection", "clipboard", "-t", "image/png", "-i"], png_bytes
            ):
                raise RuntimeError("xclip failed to set the image clipboard")
            return
        raise RuntimeError(self.image_help())

    # -- subprocess helpers -----------------------------------------

    def _read(self, cmd: list[str]) -> bytes | None:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=3)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.debug("%s failed: %s", cmd[0], exc)
            return None
        return proc.stdout if proc.returncode == 0 else None

    def _write(self, cmd: list[str], data: bytes) -> bool:
        # xclip and wl-copy fork and keep serving the selection after they
        # return, so their stdout/stderr must go to DEVNULL: a captured pipe
        # stays open in the forked child and would hang the wait.
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.debug("%s failed to start: %s", cmd[0], exc)
            return False
        try:
            proc.communicate(data, timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            log.debug("%s timed out", cmd[0])
            return False
        return proc.returncode == 0
