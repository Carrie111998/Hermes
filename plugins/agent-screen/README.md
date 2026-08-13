# Agent Screen

A virtual display for macOS — the missing Xvfb equivalent. macOS offers no
headless second display; Agent Screen fills the gap with a tiny Swift
companion app that owns a **real** `CGVirtualDisplay` (a true second display
with its own Space), renders it into a native window, and exposes it as a
live MJPEG stream on `:8788`.

**What you get:**

- A real second display — drag any window onto it (native display shift),
  or drag a window ONTO the agent-screen window and it teleports over
  (drag portal)
- A statusbar chip: monitor icon, **green** while the app runs, **gray**
  when off; click toggles start/stop; hover shows a live preview of the
  virtual screen (only while active)
- A snappable pane in the desktop layout with the live stream, dockable
  left/right/bottom via drag & drop
- Click into the agent-screen window and the cursor warps onto the virtual
  display — it behaves like a real screen

## Layout

```
plugins/agent-screen/
├── dashboard/            # FastAPI router mounted at /api/plugins/agent-screen/
│   ├── manifest.json
│   └── plugin_api.py     # /status, /start, /stop (starts/stops the app)
└── native/               # The Swift companion (sources, NOT the binary)
    ├── agent-screen-app.swift
    ├── CGVirtualDisplayPrivate.h
    ├── build-app.sh      # compiles + codesigns the .app bundle
    ├── agent-screen.sh   # launcher
    └── icon/             # app icon source (1024² PNG)
```

The desktop plugin (`apps/desktop/src/plugins/agent-screen/plugin.tsx`)
contributes the statusbar chip + pane; it ships OFF by default
(`defaultEnabled: false`) because the native companion must be built first —
enable it in **Settings ▸ Plugins** after building.

## Setup

1. **Build the native app** (Xcode command line tools required):

   ```bash
   cd plugins/agent-screen/native
   ./build-app.sh
   ```

   This compiles `agent-screen-app.swift` (macOS 14 target — required for
   `CGDisplayStream`), assembles `Agent Screen.app`, and codesigns it.

   > **Codesigning: never ad-hoc.** The app needs Screen Recording TCC
   > permission (macOS grants it per-signing-identity). If you codesign
   > ad-hoc, the TCC grant is lost on every rebuild and the stream stays
   > black until you re-grant it in System Settings ▸ Privacy &
   > Security ▸ Screen Recording. Create a self-signed certificate
   > ("Agent Screen Dev") in Keychain Access and codesign with it — the
   > TCC grant then survives rebuilds.

2. **Start it** (or use the statusbar chip — it does exactly this):

   ```bash
   cd plugins/agent-screen/native
   ./agent-screen.sh
   ```

   The stream is then at `http://127.0.0.1:8788/stream.mjpeg` (health:
   `/ping` → `ok`).

3. **Enable the plugin** in Settings ▸ Plugins ("Agent Screen").

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `AGENT_SCREEN_DIR` | `~/.hermes/agent-screen` | Where `build-app.sh` installs the app; `agent-screen.sh` and the backend resolve it the same way |

## Troubleshooting

- **Stream is black** → Screen Recording permission missing for the
  signing identity. Re-grant in System Settings ▸ Privacy & Security ▸
  Screen Recording, then restart the app.
- **App crashes right after restart** → the virtual display wasn't released
  yet. Wait ~3s after stop before starting again (the backend already does
  this; only relevant when restarting manually).
- **Dock shows the old icon** → flush the icon caches:
  `killall IconServicesAgent Dock`, then remove
  `/var/folders/**/iconcache*` and `iconserv*` and repeat.

## Attribution

The virtual-display plumbing (`CGVirtualDisplayPrivate.h`, display setup and
window rendering) is derived from
[DeskPad](https://github.com/Stengo/DeskPad) by Bastian Andelefski,
MIT-licensed (c) 2022 — see `LICENSE.deskpad` for the full license text.
`CGVirtualDisplayPrivate.h` itself originates from Khaos Tian's
VirtualDisplayExp (2/17/21).
