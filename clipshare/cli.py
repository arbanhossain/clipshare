"""Command-line interface for clipshare."""
from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import Config, parse_peer
from .store import Store
from .sync import SyncManager


def _print_items(items: list[dict]) -> None:
    if not items:
        print("(empty history)")
        return
    for it in items:
        kind = "TXT" if it["kind"] == "text" else "IMG"
        preview = (it["text"] or "").replace("\n", " ")[:60]
        mark = "PIN" if it["pinned"] else "   "
        print(f"#{it['id']:>4} {mark} {kind} {it['source']:<12} {preview}")


def run_app(show_window: bool) -> int:
    import tkinter as tk

    from .clipboard import Clipboard
    from .tray import Tray
    from .ui import ClipshareUI, ClipboardWatcher

    config = Config.load()
    store = Store(config.db_path)

    def on_local_clip(clip):
        is_new, item_id = store.add(clip.kind, clip.payload, config.device_name)
        if is_new and item_id is not None:
            item = store.get(item_id)
            manager.broadcast(item)
            ui.events.put(("local", item))

    root = tk.Tk()
    clipboard = Clipboard(root, config.max_image_bytes)
    ui = ClipshareUI(config, store, clipboard)
    manager = SyncManager(config, store, on_remote_item=ui.handle_remote_item)
    ui.attach_manager(manager)
    ClipboardWatcher(root, clipboard, on_local_clip, config.watch_interval_ms)

    tray = Tray(ui.show_window, ui.quit)
    has_tray = tray.start()
    if has_tray:
        ui.set_tray(tray)

    if not show_window:
        if not has_tray:
            print("Warning: system tray unavailable (pip install pystray pillow). "
                  "Showing window instead.", file=sys.stderr)
            show_window = True
        else:
            root.withdraw()
            ui._window_visible = False

    manager.start()
    try:
        ui.run()
    finally:
        manager.stop()
        store.close()
    return 0


def cmd_peers(args) -> int:
    config = Config.load()
    if args.action == "add":
        host, port = parse_peer(args.host)
        if config.add_peer(host, port):
            print(f"Added peer {host}:{port}")
        else:
            print(f"Peer {host}:{port} already present")
    elif args.action == "list":
        for p in config.peers or ["(none)"]:
            print(p)
    elif args.action == "remove":
        host, port = parse_peer(args.host)
        addr = f"{host}:{port}"
        if config.remove_peer(addr):
            print(f"Removed {addr}")
        else:
            print(f"Peer {addr} not found")
    return 0


def cmd_history(args) -> int:
    store = Store(Config.load().db_path)
    try:
        _print_items(store.recent(limit=args.n))
    finally:
        store.close()
    return 0


def cmd_search(args) -> int:
    store = Store(Config.load().db_path)
    try:
        _print_items(store.search(args.query))
    finally:
        store.close()
    return 0


def cmd_status(args) -> int:
    config = Config.load()
    print(f"Device: {config.device_name} ({config.device_id})")
    print(f"Listen port: {config.port}   Discovery: {'on' if config.discover else 'off'}")
    print(f"Token: {config.token}")
    print("Peers:")
    for p in config.peers or ["(none)"]:
        print(f"  {p}")
    print(f"Database: {config.db_path}")
    return 0


def cmd_token(args) -> int:
    config = Config.load()
    if args.set:
        config.token = args.set
        config.save()
        print("Token updated.")
    else:
        print(config.token)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipshare",
        description="Share clipboard history between laptops over LAN.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("app", help="run the app with its history window")
    sub.add_parser("daemon", help="run in the background (tray icon only)")

    p_peers = sub.add_parser("peers", help="manage sync peers")
    p_peers.add_argument("action", choices=["add", "list", "remove"])
    p_peers.add_argument("host", nargs="?", help="hostname or IP, optionally :port")

    p_history = sub.add_parser("history", help="print recent clipboard history")
    p_history.add_argument("-n", type=int, default=20)

    p_search = sub.add_parser("search", help="search clipboard history")
    p_search.add_argument("query")

    p_token = sub.add_parser("token", help="show or set the shared sync token")
    p_token.add_argument("set", nargs="?", metavar="VALUE")

    sub.add_parser("status", help="show device, token, peers, and database info")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.cmd in (None, "app"):
        return run_app(show_window=True)
    if args.cmd == "daemon":
        return run_app(show_window=False)
    if args.cmd == "peers":
        return cmd_peers(args)
    if args.cmd == "history":
        return cmd_history(args)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "token":
        return cmd_token(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
