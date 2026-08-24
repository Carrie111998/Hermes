# Hermes shellctl — SSH-layer file & image bridge

Move **images, PDFs, and any file** between the Hermes host (the box you
SSH into, running the TUI) and **your local machine** — over your
**existing SSH connection**, with no extra tunnel, no ControlMaster
requirement, and no dependency on iTerm2 / a specific terminal / a
specific OS.

Works on macOS, Linux, WSL, and any host that runs `ssh` (PuTTY
included). The client is a **single zero-dependency Python 3 file**
(stdlib only) — installs on a locked-down corporate Mac with no
admin/sudo and no package manager.

> This bridge is one slice of a general SSH media bridge. This branch
> ships the **file/image** transfer path. The **audio** path (TTS +
> mic) is a sibling feature that reuses the same daemon + install
> scaffolding.

## Why this exists

The Hermes TUI runs on the *remote* host, so its "clipboard" and
filesystem are the **remote host's**, not yours. shellctl bridges that
gap: a tiny HTTP listener on your machine, reachable by the remote host
through a reverse SSH forward, so the agent can pull/push bytes to/from
*your* machine.

```
Your machine (Mac/Linux/WSL)     existing SSH (any transport)   Hermes host
┌────────────────────────┐                                     ┌──────────────┐
│ hermes-shellctl daemon │  ◄── RemoteForward 127.0.0.1:8765 ─►│ TUI /get      │
│  • serves local files  │      (reverse: host reaches you)    │ hermes-shell- │
│  • clipboard image/file│                                     │   bridge      │
└────────────────────────┘                                     └──────────────┘
```

## Install (run on the HERMES host)

```sh
hermes install shellctl --ssh-host <the-host-alias-you-ssh-to>
```

This prints a token + the exact 4 steps. Summary:

1. **Save the client on your machine:**
   ```sh
   ssh <host> 'hermes install shellctl --print-client' > ~/.hermes-shellctl
   chmod +x ~/.hermes-shellctl
   ```
2. **Add ONE block to `~/.ssh/config` on your machine** (covers plain
   `ssh` and tmux-wrapping helpers like `sshp`; **no ControlMaster
   needed**):
   ```
   Host <host>
       RemoteForward 127.0.0.1:8765 127.0.0.1:8765
   ```
3. **Run the daemon on your machine** (leave it in a tab, or add to
   Login Items):
   ```sh
   HERMES_SHELLCTL_TOKEN=<token> python3 ~/.hermes-shellctl daemon --port 8765
   ```
4. **SSH in normally.** Verify from the host:
   ```sh
   <bridge> ping     # → {"ok": true, "caps": {...}}
   ```

## Use (in the TUI)

| Command | What it does |
|---|---|
| `/get <local-path>` | Pull a file FROM your machine; auto-attaches it to the turn (image/pdf/any) |
| `/paste` | Pull your machine's **clipboard image/file** and attach it |
| `/send <host-path>` | Push a Hermes-side file TO your machine and open it locally |

Image files land in the gateway images dir and carry an `/image` hint so
the TUI attaches them as visual content; everything else lands in the
downloads dir with a plain-path hint.

## Optional local helpers (better fidelity, not required)

- **Clipboard images:** macOS `pngpaste` (`brew install pngpaste`) or
  Linux `xclip`. Without them, macOS falls back to AppleScript.

`hermes-shellctl daemon` reports which capabilities are available at
startup and via `/ping`.

## Security

- The listener binds **127.0.0.1 only** and is reached solely through
  your SSH reverse-forward — nothing on the network can reach it.
- Every request is gated by a **shared token**
  (`HERMES_SHELLCTL_TOKEN`), generated per-install and stored `0600`.
- 64 MB per-transfer cap.

## Files

- Client (your machine): `~/.hermes-shellctl` (single file, stdlib only)
- Host orchestrator: `<HERMES_HOME>/shellctl/hermes-shellbridge`
- Token + config: `<HERMES_HOME>/shellctl/bridge-token`, `bridge.env`
  (both `0600`)
- Canonical source (ships with hermes_cli): `hermes_cli/shellctl_assets/`

## Notes

- Pulled files land in the gateway images/downloads dir so the normal
  attach pipeline handles them (the TUI `/image` and `/get` commands).
