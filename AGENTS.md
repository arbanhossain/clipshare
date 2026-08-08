# Repository Guidelines

Contributor guide for Clipshare, a Python app that syncs clipboard history
(text and images) between laptops over a LAN.

## Project Structure & Module Organization

- `clipshare/` — application source. One module per responsibility:
  - `clipboard.py` — X11 (tkinter) / Wayland (`wl-clipboard`) clipboard adapters
  - `store.py` — SQLite history with content-hash dedup
  - `sync.py` — TCP sync protocol, UDP discovery, peer pairing
  - `ui.py`, `tray.py` — tkinter history window and pystray tray icon
  - `config.py`, `cli.py`, `__main__.py` — settings and entry points
- `tests/` — `unittest` suites mirroring module names (`test_store.py`, `test_sync.py`)
- `scripts/` — `install.sh` and the systemd unit template (`clipshare.service`)
- `pyproject.toml` — package metadata; console script is `clipshare`

## Build, Test, and Development Commands

Run from the repository root:

- `python3 -m unittest discover -s tests -t . -v` — run the full test suite
- `python3 -m py_compile clipshare/*.py tests/*.py` — syntax-check all modules
- `python3 -m clipshare status` — show device, token, peers, and DB path
- `python3 -m clipshare app` — launch the history window locally
- `./scripts/install.sh` — create a venv and install the systemd user service

## Coding Style & Naming Conventions

- Python 3.10+, standard library first; `tkinter`, `pystray`, `Pillow` are the only runtime deps
- 4-space indentation, double-quoted strings, `from __future__ import annotations`
- Type hints on public signatures; snake_case functions/variables, PascalCase classes
- Thread-safe access goes through `Store`'s internal lock — never touch its connection directly
- No formatter/linter is configured; keep diffs small and match surrounding style

## Testing Guidelines

- Framework: stdlib `unittest` (no pytest dependency)
- Name tests `test_<behavior>` (e.g., `test_bidirectional_sync`); classes extend `unittest.TestCase`
- Every sync test uses loopback sockets (`127.0.0.1`), a shared test token, and hermetic temp dirs
- New features need a passing test before merging; run the full suite before pushing

## Commit & Pull Request Guidelines

- Conventional Commits style: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`
- One logical change per commit; imperative subject under 72 characters
- PRs: describe the change and why, link related issues, and confirm the full test suite passes
- Screenshots only when a change affects the UI or tray behavior

## Security & Configuration Tips

- All machines must share one `token` (`clipshare token set VALUE`); it gates every handshake
- `~/.config/clipshare/config.json` holds settings — `auto_push` controls whether
  remote copies overwrite the local clipboard; `auto_add_peers` controls pairing
- History is plaintext SQLite in `~/.local/share/clipshare/history.db`; treat it as sensitive
