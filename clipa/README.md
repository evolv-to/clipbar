# clipa — ClipBar's Termux companion for Android

Bridges Android's system clipboard to a tailnet of ClipBar peers using the
same wire protocol the macOS daemon speaks. Single Go binary, runs under
Termux, no root.

## What you get

- Copy something on Android → it shows up in ClipBar's history on every
  Mac peer within ~2 seconds.
- Copy on a Mac → it lands on Android's clipboard, ready to long-press
  paste in any app.
- Survives phone reboots (via Termux:Boot).
- Survives network changes — Tailscale handles the tunnel; clipa
  reconnects on its own.

Text only in v1. Image clipboard support is a stretch goal.

## Prerequisites

- Tailscale Android app, signed in to your tailnet, currently connected.
- [Termux](https://f-droid.org/en/packages/com.termux/) (F-Droid build —
  the Play Store one is unmaintained).
- [Termux:API](https://f-droid.org/en/packages/com.termux.api/) APK
  installed, plus the `termux-api` package (`pkg install termux-api`).
- [Termux:Boot](https://f-droid.org/en/packages/com.termux.boot/) APK
  for autostart on phone reboot. Optional but recommended.

## Install

In Termux:

```bash
git clone https://github.com/evolv-to/clipbar.git
bash clipbar/clipa/install.sh
```

The installer:
1. Installs `golang` and `termux-api` if missing.
2. Builds the `clipa` binary into `~/.local/bin/clipa`.
3. Drops a starter `~/.config/clipa/peers.json` you must edit.
4. Writes a `~/.termux/boot/clipa` autostart script.

Then edit `~/.config/clipa/peers.json` to list your Mac peers — one
object per peer, with `hostname` (the tailnet name; whatever shows in
`tailscale status` on the Mac) and `address` (the tailnet IPv4 — also
from `tailscale status`):

```json
[
  { "hostname": "<your-first-mac>",  "address": "<tailnet-ipv4-of-first-mac>" },
  { "hostname": "<your-second-mac>", "address": "<tailnet-ipv4-of-second-mac>" }
]
```

(Tailscale assigns each device an IPv4 in its CGNAT range. Run
`tailscale status` on each Mac and copy the values straight out.)

If you skip the file or leave it with non-existent hosts, clipa falls back
to `tailscale status --json` if the Tailscale CLI happens to be installed
inside Termux. Most users won't have that — static config is the
recommended path.

## Run

Without rebooting:
```bash
nohup ~/.local/bin/clipa --log ~/.cache/clipa.log >/dev/null 2>&1 &
```

After rebooting (with Termux:Boot installed): nothing. The boot script
starts it automatically.

Tail the log:
```bash
tail -f ~/.cache/clipa.log
```

Stop it:
```bash
pkill -f '~/.local/bin/clipa'
```

## Verify it's working

On any Mac running ClipBar:

```bash
cat ~/.config/clipd/peer-status.json | jq
```

You should see your phone's tailnet hostname under `tailscale[*]`. Then:

1. Copy text on the phone (any app, normal long-press → Copy).
2. Watch `tail -f ~/.cache/clipa.log` — you'll see a `broadcast` line.
3. Open ClipBar on the Mac — the new item is at the top of the history.

Reverse direction:

1. Copy text on the Mac.
2. Long-press in any Android text field → Paste. The Mac's text is there.

## Why static peer config?

The official Tailscale Android app handles tunneling system-wide: any
process (including a Termux Go binary) can dial `100.x.x.x` IPs and the
kernel routes through the WireGuard tunnel transparently. So
**connectivity** is automatic.

What's not automatic is **discovery** — the Termux process can't query
the Android Tailscale app's peer list. So clipa needs to be told who's
out there. Static `peers.json` is the simplest answer and the one we
ship by default. Two-line file you edit once.

If you happen to have `tailscale` CLI in Termux (rare), clipa will use
`tailscale status --json` automatically when `peers.json` is missing.

## Battery + polling

clipa polls `termux-clipboard-get` every 1.5 seconds. That's the
trade-off: Android doesn't fire a callback for clipboard changes from
background processes (security-tightened in Android 10+), so polling is
the only no-root option.

If battery drain becomes a problem, you can run clipa under a foreground
service (Termux's `termux-wake-lock` is a partial mitigation already
applied by the boot script). A real foreground-service notification
(via Termux:API `termux-notification`) is a possible future addition.

## Uninstall

```bash
pkill -f '~/.local/bin/clipa' 2>/dev/null
rm -rf ~/.local/bin/clipa ~/.config/clipa ~/.termux/boot/clipa
```

## Wire protocol

See [`../PROTOCOL.md`](../PROTOCOL.md) — clipa and clipd implement the
same spec.
