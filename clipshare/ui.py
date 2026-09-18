"""Tkinter history window, clipboard watcher, and tray integration."""
from __future__ import annotations

import base64
import datetime
import queue
import tkinter as tk
from tkinter import ttk

from . import __version__
from .clipboard import Clipboard
from .config import Config
from .settings import SettingsDialog
from .store import Store, digest
from .sync import SyncManager


def format_time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


class ClipboardWatcher:
    def __init__(self, root: tk.Tk, clipboard: Clipboard, on_change, interval_ms: int):
        self.root = root
        self.clipboard = clipboard
        self.on_change = on_change
        self.interval_ms = interval_ms
        self.last_digest: str | None = None
        root.after(interval_ms, self._poll)

    def _poll(self) -> None:
        try:
            clip = self.clipboard.get()
        except Exception:
            clip = None
        if clip is not None and clip.kind != "none":
            if clip.kind == "text":
                clip.payload = clip.payload.rstrip("\r\n")
                clip.digest = digest(clip.payload)
            if clip.digest != self.last_digest:
                self.last_digest = clip.digest
                try:
                    self.on_change(clip)
                except Exception:
                    pass
        self.root.after(self.interval_ms, self._poll)


class ClipshareUI:
    def __init__(self, config: Config, store: Store, clipboard: Clipboard | None = None):
        self.config = config
        self.store = store
        self.manager: SyncManager | None = None
        self.events: queue.Queue = queue.Queue()
        self._items: list[dict] = []
        self._photo: tk.PhotoImage | None = None
        self._tray = None
        self._window_visible = True
        self._action_msg = ""

        self.root = tk.Tk()
        self.root.title(f"Clipshare {__version__}")
        self.root.geometry("680x520")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.clipboard = clipboard or Clipboard(self.root, config.max_image_bytes)
        self._action_msg = self.clipboard.image_help()
        self._build()
        self.refresh()
        self.root.after(250, self._poll_events)
        self.root.after(2000, self._poll_status)

    def attach_manager(self, manager: SyncManager) -> None:
        self.manager = manager

    def open_settings(self) -> None:
        SettingsDialog(self.root, self.config, self.store, self.clipboard, self.manager)

    def _tray_show(self) -> None:
        self.root.after(0, self.show_window)

    def _tray_settings(self) -> None:
        self.root.after(0, self.open_settings)

    def _tray_quit(self) -> None:
        self.root.after(0, self.quit)

    def handle_remote_item(self, item: dict) -> None:
        self.events.put(("remote", item))

    # -- UI construction -------------------------------------------

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        self.search_var = tk.StringVar()
        search = ttk.Entry(top, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True)
        search.bind("<KeyRelease>", lambda e: self.refresh())
        ttk.Button(top, text="Copy", command=self.copy_selected).pack(side="left", padx=4)
        ttk.Button(top, text="Pin", command=self.toggle_pin).pack(side="left", padx=4)
        ttk.Button(top, text="Delete", command=self.delete_selected).pack(side="left", padx=4)
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="right")

        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, padx=6, pady=6)
        self.listbox = tk.Listbox(mid, font=("monospace", 10), activestyle="dotbox")
        scroll = ttk.Scrollbar(mid, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Double-Button-1>", lambda e: self.copy_selected())

        self.preview = ttk.Label(self.root, text="", anchor="center", justify="center")
        self.preview.pack(fill="x", padx=6, pady=4)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=6, pady=(0, 4)
        )

    # -- data ------------------------------------------------------

    def refresh(self) -> None:
        query = self.search_var.get().strip()
        items = self.store.search(query) if query else self.store.recent(
            limit=self.config.history_limit
        )
        self._items = items
        self.listbox.delete(0, "end")
        for it in items:
            tag = "[TXT]" if it["kind"] == "text" else "[IMG]"
            preview = (it["text"] or "").replace("\n", " ")[:60]
            pin = "PIN " if it["pinned"] else "    "
            label = f"{format_time(it['created_at'])} {tag} {it['source']:<12} {pin}{preview}"
            self.listbox.insert("end", label)

    def _on_select(self, _event=None) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        item = self._items[sel[0]]
        self.preview.configure(text="", image="")
        if item["kind"] == "text":
            text = (item["text"] or "").strip()
            self.preview.configure(text=text[:500], wraplength=640)
        else:
            full = self.store.get(item["id"])
            if full and full.get("image"):
                self._photo = tk.PhotoImage(
                    data=base64.b64encode(full["image"]).decode("ascii")
                )
                w, h = self._photo.width(), self._photo.height()
                biggest = max(w, h)
                scale = 1
                while biggest // scale > 480 and scale * 2 <= biggest:
                    scale *= 2
                if scale > 1:
                    self._photo = self._photo.subsample(scale, scale)
                self.preview.configure(image=self._photo)

    # -- actions ----------------------------------------------------

    def copy_selected(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        item = self._items[sel[0]]
        try:
            if item["kind"] == "text":
                self.clipboard.set_text(item["text"])
            else:
                full = self.store.get(item["id"])
                self.clipboard.set_image(full["image"])
            self._action_msg = f"Copied {item['kind']} to clipboard"
        except Exception as exc:
            self._action_msg = f"Copy failed: {exc}"

    def toggle_pin(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        item = self._items[sel[0]]
        self.store.set_pinned(item["id"], not item["pinned"])
        self.refresh()

    def delete_selected(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        item = self._items[sel[0]]
        self.store.delete(item["id"])
        self.refresh()

    # -- events / polling -------------------------------------------

    def _poll_events(self) -> None:
        try:
            while True:
                kind, item = self.events.get_nowait()
                if kind == "local":
                    self.refresh()
                    self._notify("Clipboard", f"Saved {item['kind']}")
                elif kind == "remote":
                    self.refresh()
                    self._apply_remote(item)
        except queue.Empty:
            pass
        self.root.after(250, self._poll_events)

    def _apply_remote(self, item: dict) -> None:
        self._notify("Clipshare", f"New {item['kind']} from {item['source']}")
        if not self.config.auto_push:
            return
        try:
            if item["kind"] == "text":
                self.clipboard.set_text(item["payload"])
            else:
                self.clipboard.set_image(item["payload"])
            self._action_msg = f"Pushed {item['kind']} from {item['source']} to clipboard"
        except Exception as exc:
            self._action_msg = f"Push failed: {exc}"

    def _poll_status(self) -> None:
        peers = self.manager.status() if self.manager else []
        if peers:
            parts = [f"{name}@{addr}:{state}" for name, addr, state in peers]
            peer_info = "Peers: " + "; ".join(parts)
        else:
            peer_info = "No peers connected — run: clipshare peers add HOST"
        self.status_var.set(f"{self._action_msg} | {peer_info}")
        self.root.after(2000, self._poll_status)

    def _notify(self, title: str, message: str) -> None:
        if self._tray is not None and not self._window_visible:
            self._tray.notify(title, message)

    # -- window / tray lifecycle ------------------------------------

    def set_tray(self, tray) -> None:
        self._tray = tray

    def show_window(self) -> None:
        self._window_visible = True
        self.root.deiconify()
        self.root.lift()

    def _on_close(self) -> None:
        if self._tray is not None:
            self._window_visible = False
            self.root.withdraw()
            self._tray.notify("Clipshare", "Still running in the tray")
        else:
            self.quit()

    def quit(self) -> None:
        if self._tray is not None:
            self._tray.stop()
        self.root.after(0, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()
