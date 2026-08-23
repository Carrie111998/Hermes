---
sidebar_position: 2
title: "Installation"
description: "Install Orion Agent on Linux, macOS, WSL2, native Windows, or Android via Termux"
---

# Installation

Get Orion Agent up and running in under two minutes!

:::tip Platform Support
For the full platform support matrix (which OSes, distribution methods, and
platform-gated features are supported), see **[Platform Support](./platform-support.md)**.
:::

## Quick Install
### With the Orion Desktop installer on macOS or Windows (recommended)
To easily install the command-line and desktop applications, [download the Orion Desktop installer](https://your-orion-docs.example/) from our website and run it.

### Without Orion Desktop:
For a command-line only install without Orion Desktop, run:

#### Linux / macOS / WSL2 / Android (Termux)
```bash
curl -fsSL https://your-orion-docs.example/install.sh | bash
```

#### Windows (native)

Run in powershell:
```powershell
iex (irm https://your-orion-docs.example/install.ps1) 
```

If you want to install & run Orion Desktop after a command-line only install, simply run
```bash
orion desktop
```

### What the Installer Does

The installer handles everything automatically — all dependencies (Python, Node.js, ripgrep, ffmpeg), the repo clone, virtual environment, global `orion` command setup, and LLM provider configuration. By the end, you're ready to chat.

#### Install Layout

Where the installer puts things depends on whether you're installing as a normal user or as root:

| Installer                              | Code lives at                  | `orion` binary                         | Data directory                       |
| -------------------------------------- | ------------------------------ | --------------------------------------- | ------------------------------------ |
| Per-user (git installer)               | `~/.orion/orion-agent/`      | `~/.local/bin/orion` (symlink)         | `~/.orion/`                         |
| Root-mode (`sudo curl … \| sudo bash`) | `/usr/local/lib/orion-agent/` | `/usr/local/bin/orion`                 | `/root/.orion/` (or `$ORION_HOME`) |

The root-mode **FHS layout** (`/usr/local/lib/…`, `/usr/local/bin/orion`) matches where other system-wide developer tools land on Linux. It's useful for shared-machine deployments where one system install should serve every user. Per-user config (auth, skills, sessions) still lives under each user's `~/.orion/` or explicit `ORION_HOME`.

### After Installation

Reload your shell and start chatting:

```bash
source ~/.bashrc   # or: source ~/.zshrc
orion             # Start chatting!
```

To reconfigure individual settings later, use the dedicated commands:

```bash
orion model          # Choose your LLM provider and model
orion tools          # Configure which tools are enabled
orion gateway setup  # Set up messaging platforms
orion config set     # Set individual config values
orion config get     # Inspect individual config values
orion setup          # Or run the full setup wizard to configure everything at once
```

:::tip Fastest path: Nous Portal
One subscription covers 300+ models plus the [Tool Gateway](/user-guide/features/tool-gateway) (web search, image generation, TTS, cloud browser). Skip the per-tool key juggling:

```bash
orion setup --portal
```

That logs you in, sets Nous as your provider, and turns on the Tool Gateway in one command.
:::

:::tip Already running Orion on another machine?
You don't need to rebuild your setup from scratch. Restore a full backup with `orion import` (see [Exporting Orion to another machine](/reference/faq#exporting-orion-to-another-machine)), or bring over a single agent with `orion profile import` (see [Moving a single profile to another machine](/reference/faq#moving-a-single-profile-to-another-machine)). Note that a profile export excludes credentials by design, so an export alone is not a full backup — [`orion backup` vs `orion profile export`](/reference/faq#orion-backup-vs-orion-profile-export) explains which to use.
:::

---

## Prerequisites

**Installer:** On non-Windows platforms, the only prerequisite is **Git**. On Linux, also make sure `curl` and `xz-utils` are available (the installer downloads Node.js as a `.tar.xz` archive). The desktop app additionally requires `g++` (or `build-essential` on Debian/Ubuntu) to compile native modules. The installer automatically handles everything else:

- **uv** (fast Python package manager)
- **Python 3.11** (via uv, no sudo needed)
- **Node.js v22** (for browser automation and WhatsApp bridge)
- **ripgrep** (fast file search)
- **ffmpeg** (audio format conversion for TTS)

:::info
You do **not** need to install Python, Node.js, ripgrep, or ffmpeg manually. The installer detects what's missing and installs it for you. Just make sure `git` is available (`git --version`). On Linux, ensure `curl` and `xz-utils` are installed (`sudo apt install curl xz-utils` on Debian/Ubuntu). For the desktop app, also install `build-essential` (`sudo apt install build-essential`).
:::

:::tip Nix users
Nix is **no longer an explicitly supported install path** (best-effort only). If you already use Nix (on NixOS, macOS, or Linux), there's a dedicated setup path with a Nix flake, declarative NixOS module, and optional container mode. See the **[Nix & NixOS Setup](./nix-setup.md)** guide.
:::

---

## Manual / Developer Installation

If you want to clone the repo and install from source — for contributing, running from a specific branch, or having full control over the virtual environment — see the [Development Setup](../developer-guide/contributing.md#development-setup) section in the Contributing guide.

---

## Non-Sudo / System Service User Installs

Running Orion as a dedicated unprivileged user (e.g. a `orion` systemd service account, or any user without `sudo` access) is supported. The only thing on the install path that genuinely needs root is Playwright's `--with-deps` step, which `apt`-installs shared libraries (`libnss3`, `libxkbcommon`, etc.) used by Chromium. The installer detects whether sudo is available and gracefully degrades when it isn't — it will install the Chromium binary into the service user's own Playwright cache and print the exact command an administrator needs to run separately.

**Recommended split (Debian/Ubuntu):**

1. **One time, as an admin user with sudo**, install the system libraries Chromium needs:
   ```bash
   sudo npx playwright install-deps chromium
   ```
   (You can run this from anywhere — `npx` will fetch Playwright on the fly.)

2. **As the unprivileged service user**, run the regular installer. It will detect the missing sudo, skip `--with-deps`, and install Chromium into the user's local Playwright cache:
   ```bash
   curl -fsSL https://your-orion-docs.example/install.sh | bash
   ```

   If you want to skip the Playwright step entirely — for example because you're running headless and don't need browser automation — pass `--skip-browser`:
   ```bash
   curl -fsSL https://your-orion-docs.example/install.sh | bash -s -- --skip-browser
   ```

   The installer also pre-installs [`cua-driver`](../user-guide/features/computer-use.md) so the Computer Use toolset works the moment you enable it; pass `--skip-computer-use` to opt out (it will then install on demand when you enable the tool).

3. **Make `orion` available to the service user's shells.** The installer writes the launcher to `~/.local/bin/orion`. System service accounts often have a minimal PATH that doesn't include `~/.local/bin`. Either add it to the user's environment, or symlink the launcher into a system location:
   ```bash
   # Option A — add to the service user's profile
   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

   # Option B — symlink system-wide (run as an admin)
   sudo ln -s /home/orion/.orion/orion-agent/venv/bin/orion /usr/local/bin/orion
   ```

4. **Verify:** `orion doctor` should now run cleanly. If you get `ModuleNotFoundError: No module named 'dotenv'`, you're invoking the repo source `orion` file (`~/.orion/orion-agent/orion`) with system Python instead of the venv launcher (`~/.orion/orion-agent/venv/bin/orion`) — fix step 3.

5. **Running the messaging gateway from this account?** A user-level service stops at logout and does not start at boot until you enable lingering for the service user:

   ```bash
   sudo loginctl enable-linger <service-user>
   ```

   See [Messaging Gateway](/user-guide/messaging/) for the service setup itself.

The same pattern works on Arch (the installer uses pacman with the same sudo-detection logic), Fedora/RHEL, and openSUSE — those distros don't support `--with-deps` at all, so an administrator always installs the system libraries separately. The relevant `dnf`/`zypper` commands are printed by the installer.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `orion: command not found` | Reload your shell (`source ~/.bashrc`) or check PATH |
| `API key not set` | Run `orion model` to configure your provider, or `orion config set OPENROUTER_API_KEY your_key` |
| Missing config after update | Run `orion config check` then `orion config migrate` |

For more diagnostics, run `orion doctor` — it will tell you exactly what's missing and how to fix it.

## Install method auto-detection

Orion auto-detects whether it was installed via the git installer, Docker, or NixOS, and `orion update` prints the matching update command for that path. There's no env var to set — the detection is based on the install layout (`~/.orion/orion-agent/` checkout, Docker image stamp, or Nix store path). `orion doctor` also surfaces the detected method under its environment summary.
