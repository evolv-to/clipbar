# ClipBar

**⌘V pastes what you copied last. ClipBar gives you back the four hundred and ninety-nine things before that.**

A free, native macOS clipboard manager with searchable history, image previews, and sync across your devices over LAN, Tailscale, and iCloud Drive. Includes a tiny Termux companion so your Android phone joins the same clipboard. No Electron. No subscriptions. See the privacy details below before enabling sync.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Platform: macOS 13+](https://img.shields.io/badge/macOS-13%2B-lightgrey.svg)

## The move

You copy something. Five other things later, you need it back. With ClipBar:

- Hit **⌘V** like normal — your most recent copy pastes instantly, *and* the panel opens so you can pick something older if that's not what you wanted. Pick a different item and ClipBar quietly undoes the first paste and swaps in the right one. Don't want it? Hit Esc, the original ⌘V stands.
- Or hit **⌘⇧V** to just open the picker without pasting first.
- Type to search across everything you've ever copied — text *and* images.
- Copy something on your laptop, paste it on your desktop. Same Wi-Fi: zero config. Different networks: same thing, over Tailscale.
- Copy on your phone, paste on your Mac (and back). The [`clipa`](clipa/) Termux agent puts your Android in the loop too.

That's the whole product. It stays out of your way until you need it.

## Why ClipBar

|  | ClipBar | [Maccy](https://maccy.app) | [Paste](https://pasteapp.io) | macOS built-in |
|---|---|---|---|---|
| Free + open source | ✅ | ✅ | ❌ ($30/yr) | ✅ |
| ⌘V works like normal *and* opens picker | ✅ | ❌ | ❌ | n/a |
| Image history with previews | ✅ | partial | ✅ | ❌ |
| LAN sync between Macs | ✅ free | ❌ | ✅ iCloud only | ❌ |
| Cross-internet sync over Tailscale | ✅ free | ❌ | ❌ | ❌ |
| Android clipboard sync | ✅ via Termux agent | ❌ | ❌ | ❌ |
| Native AppKit, no Electron | ✅ | ✅ | ✅ | ✅ |
| Telemetry / phone-home | none | none | analytics | n/a |
| Pinned snippets | ❌ (planned) | ✅ | ✅ | ❌ |

ClipBar exists because I wanted Paste's UX without paying $30/yr for iCloud-only sync, and Maccy's price without giving up image previews and cross-Mac continuity.

## Install

Requirements: macOS 13+, Xcode Command Line Tools (`xcode-select --install`), [`uv`](https://github.com/astral-sh/uv) for the Python daemon.

```bash
git clone https://github.com/evolv-to/clipbar.git ~/clipbar
cd ~/clipbar
./install.sh
```

The installer:

1. Builds `ClipBar.app` from source and copies it to `/Applications/`
2. Sets up the Python venv via `uv` inside `clipd/.venv`
3. Writes two `launchd` agents to `~/Library/LaunchAgents/` (one for `clipd`, one for `ClipBar`)
4. Loads them — both start now and re-launch on boot

Keep the cloned repo where it is — the `launchd` plists reference its absolute path.

## First-run permissions

ClipBar uses Carbon's `RegisterEventHotKey` for ⌘⇧V, which **needs no permissions**. The only TCC permission it asks for is **Accessibility**, and only the first time you select an item to paste — that's required for synthesizing ⌘V into the focused app, and to wire the ⌘V intercept feature.

After granting Accessibility in System Settings, click the menu bar icon → **Restart ClipBar** so the new permission takes effect.

If you'd rather have ⌘V behave like the system default and only summon ClipBar with ⌘⇧V, click the menu bar icon and turn off **Open on ⌘V**.

## Usage

| Action | Keys |
|---|---|
| Paste latest + open picker (auto-paste mode) | ⌘V |
| Just open the picker | ⌘⇧V |
| Navigate the list | ↑ ↓ |
| Jump | PgUp / PgDn / Home / End |
| Filter | Just type — selection stays live |
| Paste selected item (swaps if auto-paste already fired) | ↩ |
| Cancel (keeps the auto-paste if it fired) | ⎋ |
| Status icon menu | Right-click — Auto-paste, Open on ⌘V, Restart, Quit |

## Sync

ClipBar has three sync channels:

- **LAN** (Bonjour). Zero config — both Macs on the same Wi-Fi find each other via mDNS (`_clipd._tcp.local.`) and exchange clipboard items over plain TCP on port 2029.
- **Tailscale**. When the [`tailscale`](https://tailscale.com) CLI is present, ClipBar enumerates your tailnet via `tailscale status --json` and keeps a long-lived encrypted connection (over WireGuard, port 2029) to every other ClipBar peer. Works across the public internet, on cellular, behind NATs. **Default ON if Tailscale is detected**; toggle from the menu bar icon → "Sync over Tailscale".
- **iCloud Drive**. Writes each new item as a JSON snapshot in `~/Library/Mobile Documents/com~apple~CloudDocs/clipd/` whenever that directory is available and iCloud sync is enabled. This runs alongside LAN and Tailscale; it is not limited to fallback situations. Images are included in the snapshot.

Path selection: when both LAN and Tailscale are available between two Macs, **LAN wins** (lower latency, no relay risk). The Tailscale connection stays open in the background, so failover when you walk out of Wi-Fi range is instant — no reconnect lag on the next copy. Each item carries a `source_mac` so you can see where it came from. Sync ingests are not re-broadcast (no echo loops).

### Privacy

- Clipboard text and images are stored locally in `~/.config/clipd/` without application-level encryption. Credentials and other sensitive material you copy can become part of that history and sync to peers.
- LAN sync uses plaintext TCP and has no application-level authentication or peer pairing. Use it only on a network whose participants you trust. Turning on Tailscale does not disable the LAN listener or change the preference for LAN traffic.
- Tailscale traffic uses WireGuard encryption and your tailnet ACLs. ClipBar does not add a separate peer authorization step.
- iCloud snapshots leave your tailnet through iCloud Drive when that service syncs the directory. To opt out, create `~/.config/clipd/.icloud_off`; removing it re-enables snapshots. Existing snapshots must be removed separately if you no longer want them in iCloud Drive.
- Current source logs clipboard events without including text previews. Logs still contain device names, addresses, paths, and errors, so review them before sharing. Older releases and existing logs may contain clipboard text.

ClipBar does not provide a guarantee that copied secrets remain on one device. The plaintext LAN transport and lack of explicit peer pairing remain limitations of the current protocol.

### Setup for Tailscale sync

1. Install Tailscale on each Mac and join the same tailnet.
2. Make sure your Tailscale ACL allows TCP/2029 between the devices that should sync — at minimum, you need *one direction* per pair (the side that can dial holds the connection for both directions, so an asymmetric ACL still works for bidirectional sync). ClipBar does not modify your ACL.
3. Restart ClipBar (menu bar icon → Restart). The "Sync over Tailscale" toggle is on by default.

### Android (clipa)

The [`clipa/`](clipa/) directory is a tiny Go agent that runs under [Termux](https://termux.dev) and bridges Android's system clipboard. Same wire protocol, same tailnet, no central server. See [`clipa/README.md`](clipa/README.md) for the install steps and trade-offs.

The wire protocol is documented in [`PROTOCOL.md`](PROTOCOL.md).

## Architecture

Three pieces, all small:

- **`clipd/`** — Python daemon that watches the system pasteboard, writes to SQLite, and gossips with peers on the LAN and over Tailscale. Runs as a `launchd` agent.
- **`ClipBar/`** — Swift menu bar app. Reads the same SQLite DB, owns the global hotkey + UI + paste action + the Sync settings.
- **`clipa/`** — Go binary for Android (Termux). Speaks the same wire protocol as `clipd` so the phone shows up alongside the Macs. See [`clipa/README.md`](clipa/README.md).

Macs share `~/.config/clipd/history.db` between `clipd` and `ClipBar`. Either can run without the other (you'll lose monitoring or you'll lose UI, respectively).

The daemon writes an owner-only `~/.config/clipd/peer-status.json` snapshot for the
native menu. Snapshots older than ten seconds are ignored. There is no local HTTP
clipboard API; the former port 2028 and web clipboard routes have been removed.

Wire protocol shared by all three clients: [`PROTOCOL.md`](PROTOCOL.md).

## Uninstall

```bash
./uninstall.sh
```

Removes `/Applications/ClipBar.app`, the `launchd` agents, and stops the running processes. Your history DB at `~/.config/clipd/` is preserved unless you `rm -rf ~/.config/clipd`.

## Distribution

Install from source using the steps above. Prebuilt downloads are not currently available.

## Development

```bash
cd ClipBar && ./build.sh           # rebuild the Swift app
cd clipd  && uv run python clipd.py    # run the daemon in foreground for debugging
```

Logs:

- `/tmp/clipbar.log`, `/tmp/clipbar.err` — Swift app stdout/stderr
- `/tmp/clipd.log`, `/tmp/clipd.err` — Python daemon stdout/stderr

Single Swift file (`ClipBar/Sources/ClipBar/AppDelegate.swift`). Single Python file (`clipd/clipd.py`). Easy to read, easy to fork.

### Before committing

Install [Gitleaks](https://github.com/gitleaks/gitleaks#installing) 8.30.1 or newer, review the public identities in `.privacy-policy.json`, then run `./scripts/install-hooks.sh`. The installed hooks check exact staged content, commit identities and messages, and outgoing history before a push. They reject ignored runtime files and unreviewed opaque assets, and fail closed if the scanner is unavailable. Proposed edits cannot silently change the installed check.

Keep machine-specific settings and private research outside tracked files. Use the ignored `.privacy-denylist` for private literals and a reviewed public GitHub noreply identity for commits. Review the exact staged files; scanners cannot recognize every kind of private content. Check actual release packages separately before upload. See [the publication guide](docs/publication.md) for setup, policy updates, artifact checks and limitations.

After a history privacy rewrite, replace old clones with a fresh clone before pushing again. Merging or pushing from an old clone can restore the removed history.

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, sell a derivative if you want, just don't blame me when it deletes your clipboard.
