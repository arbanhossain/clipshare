# Clipshare

Share clipboard history (text and images) between your laptops over LAN.
Copy something on laptop A and it shows up on laptop B's clipboard within a
second — and every copy is kept in a searchable local history on each machine.

## Features

- Watches the clipboard on each laptop and stores a history (text + images)
- Syncs new copies to every peer over your Wi-Fi in real time
- Auto-pushes remote copies onto the local clipboard (toggleable)
- Tray icon + searchable history window (Linux X11 via tkinter, Wayland via
  `wl-clipboard`)
- Content-hash dedup: the same copy on both machines is one history entry
- Optional UDP discovery on the LAN; token-protected sync
- Plaintext SQLite history (contains whatever you copy — treat it accordingly)

## Requirements

- Python 3.10+ with `tkinter` (Debian/Ubuntu: `sudo apt install python3-tk`)
- X11: nothing extra for text; **`xclip` for images** (`sudo apt install xclip`) —
  Tk cannot read or write binary clipboard targets, so without it images are
  neither captured nor pasted
- Wayland: `wl-clipboard` (`sudo apt install wl-clipboard`) for text and images
- `clipshare status` prints whether image support is active on this machine
- Tray icon: `pystray` + `pillow` (installed by `scripts/install.sh`)

## Quick start (on each laptop)

```bash
git clone <this repo> clipshare && cd clipshare
./scripts/install.sh   # installs the package + starts the systemd user service
clipshare status       # shows your token, listen port, and image support
```

`install.sh` links the `clipshare` command into `~/.local/bin`, so that
directory has to be on your `PATH`; otherwise call
`~/.local/share/clipshare/venv/bin/clipshare` directly.

Then pair the machines once (they share one token):

```bash
# on laptop B: adopt the token that laptop A printed in `clipshare status`
clipshare token set <TOKEN-FROM-A>

# on laptop A: point it at B — that's the only command you need
clipshare peers add <B-IP-OR-HOSTNAME>
```

That's it. Sync is **bidirectional by default**: one TCP connection carries
history and live copies in both directions, and when B accepts A's connection
it remembers A (auto-pairing), so both laptops reconnect on their own after
restarts. No need to configure the same thing on both machines.

Copy something on either laptop and it appears on the other. Discovery (UDP
broadcast on port 58322) can even skip the `peers add` step entirely when both
machines run with discovery on; TCP sync runs on port 58321.

## Graphical setup

Everything can be configured from the GUI — no terminal needed after install:

1. Launch the app: `clipshare app` (first launch shows a setup wizard)
2. Open **Settings** (toolbar button or tray menu)
3. On both laptops set the same token (Copy / Paste / Regenerate buttons), then
   add the other laptop's IP under **Peers** and hit **Save** — sync applies
   immediately, no restart required

The tray menu also offers **Settings** while the app runs in the background.

## Usage

- `clipshare app` — open the history window (search, copy back, pin, delete)
- `clipshare daemon` — background process with tray icon (what systemd runs)
- `clipshare history [-n 20]` — print recent history
- `clipshare search QUERY` — search text history
- `clipshare peers add|list|remove HOST[:PORT]` (add once — the other side auto-pairs)
- `clipshare token [set VALUE]` — show or set the shared token
- `clipshare status` — device, token, peers, database path

Settings live in `~/.config/clipshare/config.json`; set `"auto_push": false`
there to stop remote copies from overwriting your clipboard, or
`"auto_add_peers": false` to stop accepting automatic pairings. History is stored
in `~/.local/share/clipshare/history.db`.

## Notes

- Firewall: allow inbound TCP on the sync port and UDP on the discovery port
  (defaults 58321/58322) between the two machines.
- A push is only applied locally if it's new content (hash dedup), so
  copy-triggered loops are impossible.
- Unpinned history is pruned after 30 days / 500 entries (configurable).
