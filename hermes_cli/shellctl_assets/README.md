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

This prints the exact setup steps. It does not print the raw token inside a
command. The generated commands copy the `0600` token file over SSH and pass it
to the daemon with `--token-file`, so the value does not enter shell history or
the process argument list.

1. **Save the client and token on your machine:**
   ```sh
   ssh <host> 'hermes install shellctl --print-client' > ~/.hermes-shellctl
   (umask 077; ssh <host> 'cat <profile-home>/shellctl/bridge-token' \
       > ~/.hermes-shellctl-token)
   chmod 700 ~/.hermes-shellctl
   chmod 600 ~/.hermes-shellctl-token
   ```
2. **Add one block to `~/.ssh/config` on your machine:**
   ```
   Host <host>
       RemoteForward 127.0.0.1:8765 127.0.0.1:8765
       ExitOnForwardFailure yes
   ```
3. **Run the daemon on your machine:**
   ```sh
   python3 ~/.hermes-shellctl daemon --port 8765 \
       --token-file ~/.hermes-shellctl-token
   ```
4. **SSH in normally.** Load the generated `bridge.env`, then run the host
   bridge's `ping` command. `/ping` requires the same token as every other
   endpoint.

To restrict reads from the client machine, set the following on the Hermes
host before running the installer:

```yaml
shellctl:
  allowed_root: ~/Documents/hermes-share
```

The path is interpreted on the SSH client machine. You can override it for one
installation with `hermes install shellctl --allowed-root <client-path>`. The
installer adds `--allowed-root` to the generated daemon command. Symlinks that
resolve outside the root are rejected.

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

## Security and threat boundary

The exact bearer credential is the random value in
`<profile-home>/shellctl/bridge-token` on the Hermes host and
`~/.hermes-shellctl-token` on the client. It is sent in the
`X-Shellctl-Token` HTTP header. Every endpoint, including `/ping`, requires it.
Keep both token files mode `0600` and rotate the token by deleting the host
copy, rerunning the installer, and recopying it.

Possession of that token plus access to the listener grants the ability to:

* read any file readable by the user running the client daemon through `/pull`,
  unless `shellctl.allowed_root` or `--allowed-root` restricts it;
* read clipboard file or image content through `/clipboard`;
* write files into the configured download directory and request that the OS
  open them through `/push`.

The listener binds to `127.0.0.1`, but the SSH `RemoteForward` deliberately
makes it reachable from the Hermes host. A process on a compromised Hermes host
can read `bridge.env`, obtain the token, and use the forwarded listener with
all permissions above. The token does not protect the client from a compromised
Hermes host. The optional allowed root limits file pulls, but it does not remove
clipboard or push access. Do not run the bridge for an untrusted host. Stop the
daemon and SSH session to remove access.

Each transfer is capped at 64 MB. Existing destination files are never
replaced; shellctl allocates a numbered filename instead.

## RemoteForward troubleshooting

With `ExitOnForwardFailure yes`, SSH exits rather than silently connecting
without the bridge. If SSH reports `remote port forwarding failed for listen
port 8765`, another active SSH connection usually owns that listen address.
Close the stale connection, or choose a new port with `--port` and update both
sides of the `RemoteForward`. Check for duplicate `RemoteForward` lines in
included SSH config files as well. `ssh -vv <host>` shows which forwarding rule
was applied.

## Files

- Client (your machine): `~/.hermes-shellctl` (single file, stdlib only)
- Host orchestrator: `<HERMES_HOME>/shellctl/hermes-shellbridge`
- Token + config: `<HERMES_HOME>/shellctl/bridge-token`, `bridge.env`
  (both `0600`)
- Canonical source (ships with hermes_cli): `hermes_cli/shellctl_assets/`

## Notes

- Pulled files land in the gateway images/downloads dir so the normal
  attach pipeline handles them (the TUI `/image` and `/get` commands).
