# Agent Screen

> **A fork of [DeskPad](https://github.com/Stengo/DeskPad)** by
> [Bastian Andelefski](https://github.com/Stengo) (MIT, 2022).
> The virtual display, the window chrome, click-to-warp, and the titlebar
> highlight are DeskPad. Agent Screen adds a loopback MJPEG preview, a
> drag-portal, and a Hermes desktop chip/pane. See [`NOTICE`](./NOTICE)
> and [`LICENSE.deskpad`](./LICENSE.deskpad).
> `CGVirtualDisplayPrivate.h` originates from Khaos Tian's VirtualDisplayExp
> (2021).

A virtual display for macOS — the missing Xvfb equivalent. macOS offers no
headless second display; Agent Screen fills the gap with a tiny Swift
companion app that owns a **real** `CGVirtualDisplay` (a true second display
with its own Space), renders it into a native window, and exposes it as a
live MJPEG stream on loopback `:8788`.

**Experimental.** `CGVirtualDisplay` is private SPI. Apple can break it on
any macOS update. The plugin ships **off** by default.

**What you get:**

- A real second display — drag any window onto it (native display shift),
  or drop a window onto the Agent Screen window to teleport it (drag portal)
- A statusbar chip: monitor icon, green while the app runs, gray when off;
  click toggles start/stop; hover shows a live preview (only while active)
- A snappable pane in the desktop layout with the live stream
- Click into the Agent Screen window and the cursor warps onto the virtual
  display

**Local macOS backend only.** Start/stop talk to the connected Hermes
backend; the preview always reads `http://127.0.0.1:8788` on this Mac.
A remote or Linux backend cannot drive the display on your machine.

## Layout

```
plugins/agent-screen/
├── NOTICE                # DeskPad + VirtualDisplayExp credit (read this)
├── LICENSE.deskpad       # MIT (c) 2022 Bastian Andelefski
├── README.md
├── dashboard/            # FastAPI router at /api/plugins/agent-screen/
│   ├── manifest.json
│   └── plugin_api.py     # /status, /start, /stop
└── native/
    ├── agent-screen-app.swift
    ├── CGVirtualDisplayPrivate.h
    ├── build-app.sh
    ├── agent-screen.sh
    └── icon/
```

The desktop plugin (`apps/desktop/src/plugins/agent-screen/plugin.tsx`)
contributes the statusbar chip + pane; it ships OFF
(`defaultEnabled: false`) because the native companion must be built first.

## Setup

1. **Create the codesigning certificate once** (never ad-hoc).

   Keychain Access → Certificate Assistant → Create a Certificate…
   Name: `Agent Screen Dev` · Identity Type: Self Signed Root ·
   Certificate Type: Code Signing.

   Screen Recording TCC is bound to the signing identity. Ad-hoc
   (`codesign -s -`) loses the grant on every rebuild.

2. **Build the native app** (Xcode command line tools, macOS 14+):

   ```bash
   cd plugins/agent-screen/native
   ./build-app.sh
   ```

   This compiles a universal binary when the toolchain allows (arm64 +
   x86_64, else host arch), writes `Info.plist` (`ai.hermes.agent-screen`),
   and codesigns `~/.hermes/agent-screen/app/Agent Screen.app`.

3. **Start it** (or use the statusbar chip):

   ```bash
   ./agent-screen.sh
   ```

   Stream: `http://127.0.0.1:8788/stream.mjpeg` · health: `/ping` → `ok`.

4. **Enable the plugin** in Settings ▸ Plugins ("Agent Screen").

5. Grant **Screen Recording** (and **Accessibility** if you want the drag
   portal) in System Settings ▸ Privacy & Security, then restart the app.

## Threat model

- `/start` and `/stop` sit behind the dashboard session-token middleware,
  same as every other `/api/plugins/…` route.
- The MJPEG server binds **loopback only** and has **no auth**. Any local
  process can watch the virtual display. That is the trade-off for a cheap
  preview the desktop pane can `<img src>` without a token. Do not treat
  the virtual screen as a secrets vault.
- Process control uses `pgrep -x` / `pkill -x agent-screen-app` (exact
  name). It will not match an editor or compiler whose argv merely
  contains that string.

## Troubleshooting

- **Stream is black** → Screen Recording permission missing for the
  signing identity. Re-grant, then restart the app.
- **App crashes right after restart** → the virtual display wasn't released
  yet. Wait ~3s after stop (the backend already does this).
- **Chip says "macOS only" / "local backend"** → the connected Hermes
  backend is not this Mac. Switch the desktop app to the local gateway.
- **Drag portal does nothing** → grant Accessibility to Agent Screen.
- **Stale Dock icon after an icon change** → `killall Dock`.

## Attribution

Agent Screen is a **fork of [DeskPad](https://github.com/Stengo/DeskPad)**
by Bastian Andelefski, MIT-licensed (c) 2022 — full text in
`LICENSE.deskpad`, provenance in `NOTICE`.
`CGVirtualDisplayPrivate.h` itself originates from Khaos Tian's
VirtualDisplayExp (2/17/21).
