"""GUI dialogs for first-run setup and configuration."""
from __future__ import annotations

import secrets
import socket
import tkinter as tk
from tkinter import messagebox, ttk

from .config import Config, parse_peer


def local_ipv4s() -> list[str]:
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    addrs.discard("127.0.0.1")
    return sorted(addrs)


class SettingsDialog:
    """Modal settings window; applies changes live and reloads the sync manager."""

    def __init__(self, root, config: Config, store, clipboard, manager=None):
        self.config = config
        self.store = store
        self.clipboard = clipboard
        self.manager = manager

        self.win = tk.Toplevel(root)
        self.win.title("Clipshare settings")
        self.win.geometry("560x640")
        self.win.transient(root)
        self.win.resizable(False, False)
        self._build()
        self._load()

    # -- construction ----------------------------------------------

    def _build(self) -> None:
        body = ttk.Frame(self.win, padding=10)
        body.pack(fill="both", expand=True)

        dev = ttk.LabelFrame(body, text="Device", padding=8)
        dev.pack(fill="x", pady=(0, 8))
        ttk.Label(dev, text="Device name:").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar()
        ttk.Entry(dev, textvariable=self.name_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(dev, text="Listen port:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.port_var = tk.StringVar()
        ttk.Entry(dev, textvariable=self.port_var, width=10).grid(
            row=1, column=1, sticky="w", padx=4, pady=(4, 0)
        )
        dev.columnconfigure(1, weight=1)
        ips = ", ".join(local_ipv4s()) or "(none detected)"
        ttk.Label(dev, text=f"Other laptops connect to one of:\n{ips}", foreground="#555").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )

        tok = ttk.LabelFrame(body, text="Shared security token", padding=8)
        tok.pack(fill="x", pady=(0, 8))
        ttk.Label(
            tok,
            text="Both laptops must use the same token. Set it on each machine.",
            wraplength=500,
        ).pack(anchor="w")
        self.token_var = tk.StringVar()
        token_row = ttk.Frame(tok)
        token_row.pack(fill="x", pady=(4, 0))
        ttk.Entry(token_row, textvariable=self.token_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(token_row, text="Copy", command=self.copy_token).pack(side="left", padx=4)
        ttk.Button(token_row, text="Paste", command=self.paste_token).pack(side="left", padx=4)
        ttk.Button(token_row, text="Regenerate", command=self.regenerate_token).pack(
            side="left", padx=4
        )
        ttk.Label(
            tok,
            text='"Paste" reads the current clipboard; "Regenerate" creates a new random token.',
            foreground="#555",
        ).pack(anchor="w", pady=(4, 0))

        peers = ttk.LabelFrame(body, text="Peers", padding=8)
        peers.pack(fill="both", expand=True, pady=(0, 8))
        list_row = ttk.Frame(peers)
        list_row.pack(fill="both", expand=True)
        self.peer_list = tk.Listbox(list_row, height=4)
        scroll = ttk.Scrollbar(list_row, orient="vertical", command=self.peer_list.yview)
        self.peer_list.configure(yscrollcommand=scroll.set)
        self.peer_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.add_var = tk.StringVar()
        add_row = ttk.Frame(peers)
        add_row.pack(fill="x", pady=(4, 0))
        ttk.Label(add_row, text="Host (optionally :port):").pack(side="left")
        ttk.Entry(add_row, textvariable=self.add_var).pack(
            side="left", fill="x", expand=True, padx=4
        )
        ttk.Button(add_row, text="Add", command=self.add_peer).pack(side="left")
        ttk.Button(add_row, text="Remove", command=self.remove_peer).pack(side="left", padx=4)
        ttk.Label(
            peers,
            text="Add the other laptop once on either machine; auto-pairing remembers the rest.",
            foreground="#555",
        ).pack(anchor="w", pady=(4, 0))
        self.discover_var = tk.BooleanVar()
        self.auto_add_var = tk.BooleanVar()
        ttk.Checkbutton(
            peers, text="Discover peers automatically (UDP broadcast)", variable=self.discover_var
        ).pack(anchor="w")
        ttk.Checkbutton(
            peers,
            text="Accept new peers that connect with the right token",
            variable=self.auto_add_var,
        ).pack(anchor="w")

        opts = ttk.LabelFrame(body, text="Options", padding=8)
        opts.pack(fill="x")
        self.auto_push_var = tk.BooleanVar()
        ttk.Checkbutton(
            opts,
            text="Push remote copies onto the local clipboard automatically",
            variable=self.auto_push_var,
        ).pack(anchor="w")
        row = ttk.Frame(opts)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="History limit:").pack(side="left")
        self.limit_var = tk.StringVar()
        ttk.Spinbox(row, from_=10, to=10000, textvariable=self.limit_var, width=8).pack(
            side="left", padx=4
        )
        ttk.Label(row, text="Retention (days):").pack(side="left", padx=(12, 0))
        self.retention_var = tk.StringVar()
        ttk.Spinbox(row, from_=1, to=3650, textvariable=self.retention_var, width=8).pack(
            side="left", padx=4
        )

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.win.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right", padx=4)

    def _load(self) -> None:
        self.name_var.set(self.config.device_name)
        self.port_var.set(str(self.config.port))
        self.token_var.set(self.config.token)
        self.discover_var.set(self.config.discover)
        self.auto_add_var.set(self.config.auto_add_peers)
        self.auto_push_var.set(self.config.auto_push)
        self.limit_var.set(str(self.config.history_limit))
        self.retention_var.set(str(self.config.retention_days))
        self._refresh_peers()

    def _refresh_peers(self) -> None:
        self.peer_list.delete(0, "end")
        for addr in self.config.peers:
            self.peer_list.insert("end", addr)

    # -- token actions ----------------------------------------------

    def copy_token(self) -> None:
        self.clipboard.set_text(self.config.token)
        messagebox.showinfo("Clipshare", "Token copied to clipboard.", parent=self.win)

    def paste_token(self) -> None:
        clip = self.clipboard.get()
        if clip.kind == "text" and clip.payload.strip():
            self.config.token = clip.payload.strip()
            self.token_var.set(self.config.token)
        else:
            messagebox.showwarning(
                "Clipshare", "Clipboard does not contain text.", parent=self.win
            )

    def regenerate_token(self) -> None:
        if messagebox.askyesno(
            "Clipshare",
            "Replace the token? Other laptops must use the new value too.",
            parent=self.win,
        ):
            self.config.token = secrets.token_urlsafe(24)
            self.token_var.set(self.config.token)

    # -- peer actions -----------------------------------------------

    def add_peer(self) -> None:
        text = self.add_var.get().strip()
        if not text:
            messagebox.showwarning("Clipshare", "Enter a host or IP address.", parent=self.win)
            return
        try:
            host, port = parse_peer(text)
        except ValueError:
            messagebox.showerror("Clipshare", "Invalid host or port.", parent=self.win)
            return
        if not host:
            messagebox.showwarning("Clipshare", "Enter a host or IP address.", parent=self.win)
            return
        self.config.add_peer(host, port)
        self.add_var.set("")
        self._refresh_peers()

    def remove_peer(self) -> None:
        sel = self.peer_list.curselection()
        if not sel:
            return
        addr = self.peer_list.get(sel[0])
        self.config.remove_peer(addr)
        self._refresh_peers()

    # -- save -------------------------------------------------------

    def save(self) -> None:
        try:
            port = int(self.port_var.get())
            limit = int(self.limit_var.get())
            retention = int(self.retention_var.get())
        except ValueError:
            messagebox.showerror("Clipshare", "Port, history limit, and retention must be numbers.", parent=self.win)
            return
        if not (1 <= port <= 65535) or limit < 1 or retention < 1:
            messagebox.showerror("Clipshare", "Values are out of range.", parent=self.win)
            return
        name = self.name_var.get().strip() or self.config.device_name
        self.config.device_name = name
        self.config.port = port
        self.config.discover = self.discover_var.get()
        self.config.auto_add_peers = self.auto_add_var.get()
        self.config.auto_push = self.auto_push_var.get()
        self.config.history_limit = limit
        self.config.retention_days = retention
        self.config.save()
        if self.manager is not None:
            self.manager.reload()
        self.win.destroy()


class SetupDialog:
    """First-run wizard: explains pairing and opens the settings window."""

    def __init__(self, root, config: Config, ui):
        self.config = config
        self.ui = ui
        self.win = tk.Toplevel(root)
        self.win.title("Welcome to Clipshare")
        self.win.geometry("540x400")
        self.win.transient(root)
        self.win.resizable(False, False)
        self._build()

    def _build(self) -> None:
        body = ttk.Frame(self.win, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Welcome to Clipshare", font=("", 14, "bold")).pack(anchor="w")
        ttk.Label(
            body,
            text=(
                "Clipshare syncs your clipboard between laptops on the same "
                "network. Set it up in three steps:"
            ),
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        steps = ttk.Frame(body)
        steps.pack(anchor="w", pady=(10, 0))
        ttk.Label(
            steps,
            text=(
                "1. Both laptops must use the same token.\n"
                "2. Add the other laptop's IP address as a peer.\n"
                "3. That's it — copies sync in both directions automatically."
            ),
            justify="left",
            foreground="#333",
        ).pack(anchor="w")

        ttk.Label(body, text="Your token (set this on both laptops):", anchor="w").pack(
            anchor="w", pady=(12, 0)
        )
        token_row = ttk.Frame(body)
        token_row.pack(fill="x")
        ttk.Entry(token_row, textvariable=tk.StringVar(value=self.config.token), state="readonly").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(token_row, text="Copy", command=self.copy_token).pack(side="left", padx=4)

        ips = ", ".join(local_ipv4s()) or "(none detected)"
        ttk.Label(
            body, text=f"Other laptops can reach this one at: {ips}", foreground="#555"
        ).pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="Skip", command=self.finish).pack(side="right")
        ttk.Button(buttons, text="Open settings", command=self.open_settings).pack(
            side="right", padx=4
        )

    def copy_token(self) -> None:
        self.ui.clipboard.set_text(self.config.token)

    def open_settings(self) -> None:
        self.ui.open_settings()
        self.finish()

    def finish(self) -> None:
        self.config.onboarded = True
        self.config.save()
        self.win.destroy()
