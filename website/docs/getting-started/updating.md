---
sidebar_position: 3
title: "Updating & Uninstalling"
description: "How to update Orion Agent to the latest version or uninstall it"
---

# Updating & Uninstalling

## Updating

Update to the latest version with a single command:

```bash
orion update
```

This pulls the latest code from `main`, updates dependencies, and prompts you to configure any new options that were added since your last update.

:::tip
`orion update` automatically detects new configuration options and prompts you to add them. If you skipped that prompt, you can manually run `orion config check` to see missing options, then `orion config migrate` to interactively add them.
:::

### What happens during an update

When you run `orion update`, the following steps occur:

1. **Pre-update snapshot** — a lightweight state snapshot is saved by default (covers pairing data, cron jobs, `config.yaml`, `.env`, `auth.json`, and other state files that get modified at runtime; individual files over 1 GiB are skipped so a large sessions DB never slows the update down). Because the code swap and gateway restarts touch every profile, the same snapshot is taken for **every profile** on the install — each into its own `state-snapshots/` directory — and the post-update cron-jobs safety net checks each profile against its own snapshot. Controlled by `updates.pre_update_backup` (`quick` by default, `full` for a zip of all of `ORION_HOME`, `off` to disable). Recoverable via the snapshot restore flow described under [Snapshots and rollback](../user-guide/checkpoints-and-rollback.md). Quick snapshots are file-loss recovery, not code-rollback insurance — for a coherent point-in-time rollback use `--backup` (full mode).
2. **Git pull** — pulls the latest code from the `main` branch and updates submodules
3. **Post-pull syntax validation + auto-rollback** — after the pull, Orion compiles the nine critical files every `orion` invocation imports at startup. If any fails to parse (e.g. an orphan merge-conflict marker, an accidentally truncated file), Orion runs `git reset --hard <pre-pull-sha>` to roll the install back so your shell stays bootable. Re-run `orion update` once the upstream fix lands.
4. **Dependency install** — runs `uv pip install -e ".[all]"` to pick up new or changed dependencies
5. **Config migration** — detects new config options added since your version and prompts you to set them
6. **Gateway auto-restart** — running gateways are refreshed after the update completes so the new code takes effect immediately. Service-managed gateways (systemd on Linux, launchd on macOS) are restarted through the service manager. Manual gateways are relaunched automatically when Orion can map the running PID back to a profile.

### Updating against a non-default branch: `--branch`

By default `orion update` tracks `origin/main`. Pass `--branch <name>` to update against a different branch — useful for QA channels, feature branches, or release-candidate testing:

```bash
orion update --branch release-candidate
orion update --check --branch experimental   # preview behindness only
```

If your local checkout is on a different branch, Orion auto-stashes any uncommitted work, switches HEAD to the target branch, and then pulls. Branches that don't exist locally are auto-tracked from `origin/<name>` (`git checkout -B <name> origin/<name>`). Branches that don't exist anywhere fail cleanly — your stashed changes are restored before exit so you're never stranded in a weird state. The `main`-only fork-upstream sync logic is automatically skipped on non-`main` branches.

### Checkout parked on a feature branch

If the source checkout was left sitting on a feature branch (by tooling, a worktree experiment, or a manual checkout), `orion update` switches it back to the update target automatically whenever the working tree is clean:

- **Branch fully merged** (every commit already contained in `origin/main` — `git cherry` reports nothing unmerged): the update says so — `Checkout was parked on '<branch>' (fully merged) — switched back to main` — and stays on `main` afterwards.
- **Branch has unmerged commits** but the tree is clean: the update still switches to `main` so the update can proceed — this is what non-interactive callers (the desktop update button, gateway `/update`, cron) rely on, since they have no way to resolve a skip. Your commits are untouched: `git checkout` never discards committed work, and the update prints a loud notice naming the branch and commit count, plus the `git checkout <branch>` command to pick the work back up later.

If you *deliberately* run a custom branch (local patches maintained on top of main), set `updates.parked_branch_strategy: update_in_place` in `config.yaml`. The update then merges `origin/main` **into** your branch instead of switching away from it — the checkout never moves, your commits survive, and the running code advances. Fast-forward when possible; on divergence a true merge behind a `pre-update-<stamp>` safety tag, stopping cleanly (nothing changed) on conflict. `orion update --switch-branch` overrides back to the switch path for one run — useful on a deep feature branch that must not accumulate update-driven merge commits.

When the parked branch has **uncommitted changes** (dirty tree), Orion does **not** touch it. The code update is marked **SKIPPED** with a loud warning naming the branch, how far behind `origin/main` it is, and the exact commands to resolve — instead of pretending the update succeeded. The completion line always shows the actual branch and HEAD (`✓ Update complete! [main @ 30fcf9580]`) so drift is visible at a glance. Set `updates.auto_switch_parked_branch: false` in `config.yaml` to disable the auto-switch entirely (the skip warning still fires).

### Local changes on non-interactive updates

When you run `orion update` in a terminal, Orion stashes any uncommitted source-tree changes, pulls, then **asks** whether to restore them — exactly as it always has. Nothing changes for interactive updates.

When the update runs **without a terminal** — from the desktop/chat app's "Update" button or a gateway-triggered update — there's no prompt to answer. The `updates.non_interactive_local_changes` setting decides what happens to your stashed changes:

```yaml
# ~/.orion/config.yaml
updates:
  non_interactive_local_changes: stash   # default: keep + auto-restore
  # non_interactive_local_changes: discard  # throw local source edits away
```

- `stash` (default) — auto-stash, pull, then auto-restore your changes on top of the updated code. Nothing is lost; if a restore hits conflicts they're preserved in a git stash for manual recovery.
- `discard` — auto-stash and drop the stash after the pull, so the update always lands on a clean tree. Use this only on machines where you never intend to keep local edits to the Orion source. It stash-drops (not `git reset --hard` + `git clean -fd`), so ignored paths like `node_modules`, `venv`, and build outputs are never touched.

In the desktop app this is **Settings → Advanced → In-App Update Local Changes**.

**Desktop updates never auto-restore.** The desktop updater invokes `orion update --keep-stash`: local source edits are still stashed so the update can proceed, but they are **not** re-applied afterward — they stay parked in `git stash` and the update log prints the exact `git stash apply <ref>` command to bring them back. This prevents local edits from silently riding along across desktop updates and breaking the freshly updated install. (`non_interactive_local_changes: discard` still wins if you've opted into discarding.) To restore parked changes manually:

```bash
cd ~/.orion/orion-agent   # or your install root
git stash list --format='%gd %H %s'   # find the orion-update-autostash entry
git stash apply stash@{0}
```

You can pass `--keep-stash` to a terminal `orion update` too if you want the same never-reapply behavior interactively.

### Preview-only: `orion update --check`

Want to know if an update is available before pulling? Run `orion update --check` — it fetches and compares commits against `origin/main`. No files are modified, no gateway is restarted. Useful in scripts and cron jobs that gate on "is there an update".

### Fleet preview: `orion update --plan`

Before updating a machine that runs several profiles or services, `orion update --plan` prints the full update plan without changing anything: the install kind (git checkout, Docker image, Nix/apt managed), every running Orion service across all profiles with its supervisor (systemd, launchd, manual) and the code version it is actually serving, and the restart mechanism each one will get. On image- or package-managed installs the plan reports that the install is not updatable in place and names the right update command instead. Read-only and safe on a live fleet.

The same inventory is embedded in every real update's receipt (`~/.orion/logs/update_receipts/`), so after an update you can compare what the updater saw against what it did.

### Update receipts and the fleet version check

Every `orion update` run writes a machine-readable receipt to `~/.orion/logs/update_receipts/` (last 20 kept, `latest.json` always points at the most recent): the pre-update fleet plan, each step taken, anything skipped and why, the gateway restart outcome, and the final fleet version matrix. After the restart phase the updater compares each live gateway's running code against the freshly updated checkout and prints a per-profile matrix — a gateway still serving pre-update code is reported loudly with the exact restart command, and the update exits non-zero so automation never treats a mixed-version fleet as healthy. Both `--plan` and the fleet check ask each running gateway directly over its local control socket (`gateway.sock` in the profile's data directory, a named pipe on Windows) when available, so version and supervisor information comes from the gateway itself; gateways from older versions are still discovered through their state files as before.

### Full pre-update backup: `--backup`

For high-value profiles (production gateways, shared team installs) you can opt into a full pre-pull backup of `ORION_HOME` (config, auth, sessions, skills, pairing):

```bash
orion update --backup
```

Or make it the default for every run:

```yaml
# ~/.orion/config.yaml
updates:
  pre_update_backup: full
```

`updates.pre_update_backup` is a single knob with three modes: `quick` (default — the lightweight state snapshot described above), `full` (the quick snapshot plus a complete `ORION_HOME` zip; can add minutes on large homes), and `off` (no pre-update backup at all — `--no-backup` does the same for a single run). Legacy boolean values still work: `true` means `full`, `false` means `off`.

:::tip Moving to a new machine instead?
Update backups protect an in-place update. If you're migrating your whole setup to different hardware, use `orion backup` + `orion import` instead — see [Exporting Orion to another machine](/reference/faq#exporting-orion-to-another-machine) and [`orion backup` vs `orion profile export`](/reference/faq#orion-backup-vs-orion-profile-export).
:::

### Windows: another `orion.exe` is running

On Windows, `orion update` will refuse to run if it detects another `orion.exe` process holding the venv's entry-point executable open — most commonly the Orion Desktop app's spawned backend, an open `orion` REPL in another terminal, or a running gateway:

```
$ orion update
✗ Another orion.exe is running:
    PID 12345  orion.exe

  Updating now would fail to overwrite ...\venv\Scripts\orion.exe because
  Windows blocks REPLACE on a running executable.

  Close Orion Desktop, exit any open `orion` REPLs, and
  stop the gateway (`orion gateway stop`) before retrying.
  Override with `orion update --force` if you've already
  confirmed those processes will not write to the venv.
```

Close the listed processes and re-run. If you're sure the concurrent process won't interfere (rare — usually only useful when an antivirus shim is mis-attributed), pass `--force` to skip the check. In that case the updater will still retry the `.exe` rename with exponential backoff and, on stubborn locks, schedule the replacement for next reboot via `MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT)` so the update can complete.

A second, separate guard refuses to touch the venv while any process is running from its Python interpreter (the Desktop app's backend, a gateway, a Python REPL). Those processes keep native extension files (`.pyd`) locked, and a dependency sync that dies partway on an access-denied error strands the install between versions. This guard is **not** bypassed by `--force`; if you're certain the detected holders are false positives, use the explicit `orion update --force-venv`.

#### Windows venv recreation is transactional

When the Windows installer must recreate an existing `venv`, it first moves the old directory to a unique `venv.stale.*` name, then creates and verifies the replacement. The old tree is deleted only after the dependency install completes and the baseline imports pass in the new tree — until then it is the rollback source (recorded in `venv.pending-backup`).

If the move cannot be completed, the installer stops and leaves the live `venv` untouched. If `uv` fails or reports success without creating the interpreter, any partial replacement is moved to `venv.failed.*` and the previous venv is restored. This keeps the health and blocker checks usable after a failed install.

A `venv.stale.*` or `venv.failed.*` directory can remain when another process still owns a file handle. Close Orion Desktop, gateways, and Python processes using the install, then retry the install/update; parked directories are cleaned up best-effort after a successful recreation.

Expected output looks like:

```
$ orion update
Updating Orion Agent...
📥 Pulling latest code...
Already up to date.  (or: Updating abc1234..def5678)
📦 Updating dependencies...
✅ Dependencies updated
🔍 Checking for new config options...
✅ Config is up to date  (or: Found 2 new options — running migration...)
🔄 Restarting gateways...
✅ Gateway restarted
✅ Orion Agent updated successfully!
```

### Recommended Post-Update Validation

`orion update` handles the main update path, but a quick validation confirms everything landed cleanly:

1. `git status --short` — if the tree is unexpectedly dirty, inspect before continuing
2. `orion doctor` — checks config, dependencies, and service health
3. `orion --version` — confirm the version bumped as expected
4. If you use the gateway: `orion gateway status`
5. If `doctor` reports npm audit issues: run `npm audit fix` in the flagged directory

:::warning Dirty working tree after update
If `git status --short` shows unexpected changes after `orion update`, stop and inspect them before continuing. This usually means local modifications were reapplied on top of the updated code, or a dependency step refreshed lockfiles.
:::

### If your terminal disconnects mid-update

`orion update` protects itself against accidental terminal loss:

- The update ignores `SIGHUP`, so closing your SSH session or terminal window no longer kills it mid-install. `pip` and `git` child processes inherit this protection, so the Python environment cannot be left half-installed by a dropped connection.
- All output is mirrored to `~/.orion/logs/update.log` while the update runs. If your terminal disappears, reconnect and inspect the log to see whether the update finished and whether the gateway restart succeeded:

```bash
tail -f ~/.orion/logs/update.log
```

- `Ctrl-C` (SIGINT) and system shutdown (SIGTERM) are still honored — those are deliberate cancellations, not accidents.

You no longer need to wrap `orion update` in `screen` or `tmux` to survive a terminal drop.

### Checking your current version

```bash
orion --version
```

Compare against the latest release at the [GitHub releases page](https://github.com/zacharyjleach-stack/Aries/releases).

### Updating from Messaging Platforms

You can also update directly from Telegram, Discord, Slack, WhatsApp, or Teams by sending:

```
/update
```

This pulls the latest code, updates dependencies, and restarts running gateways. The bot will briefly go offline during the restart (typically 5–15 seconds) and then resume.

### Manual Update

If you installed manually (not via the quick installer):

```bash
cd /path/to/orion-agent
# Activate the venv you created during install (outside the source tree)
export VIRTUAL_ENV="$HOME/.orion/venvs/orion-dev"
export PATH="$VIRTUAL_ENV/bin:$PATH"

# Pull latest code
git pull origin main

# Reinstall (picks up new dependencies)
uv pip install -e ".[all]"

# Check for new config options
orion config check
orion config migrate   # Interactively add any missing options
```

### Rollback instructions

If an update introduces a problem, you can roll back to a previous version:

```bash
cd /path/to/orion-agent

# List recent versions
git log --oneline -10

# Roll back to a specific commit
git checkout <commit-hash>
uv pip install -e ".[all]"

# Restart the gateway if running
orion gateway restart
```

To roll back to a specific release tag (substitute your previous tag — e.g. a recent release like `v2026.5.16`, or any earlier tag from `git tag --sort=-version:refname`):

```bash
git checkout vX.Y.Z
uv pip install -e ".[all]"
```

:::warning
Rolling back may cause config incompatibilities if new options were added. Run `orion config check` after rolling back and remove any unrecognized options from `config.yaml` if you encounter errors.
:::

### Note for Nix users

Nix is no longer an explicitly supported install path (best-effort only) — see [Nix Setup](./nix-setup.md). If you installed via Nix flake, updates are managed through the Nix package manager:

```bash
# Update the flake input
nix flake update orion-agent

# Or rebuild with the latest
nix profile upgrade orion-agent
```

Nix installations are immutable — rollback is handled by Nix's generation system:

```bash
nix profile rollback
```

See [Nix Setup](./nix-setup.md) for more details.

---

## Uninstalling

```bash
orion uninstall
```

The uninstaller gives you the option to keep your configuration files (`~/.orion/`) for a future reinstall.

:::tip Moving to a new machine rather than leaving?
Take your setup with you before removing anything: `orion backup` captures the entire `~/.orion` directory including credentials, while `orion profile export` packs a single profile with credentials excluded by design (so an export alone is not a full backup). See [`orion backup` vs `orion profile export`](/reference/faq#orion-backup-vs-orion-profile-export).
:::

### Manual Uninstall

```bash
rm -f ~/.local/bin/orion
rm -rf /path/to/orion-agent
rm -rf ~/.orion            # Optional — keep if you plan to reinstall
```

:::info
If you installed the gateway as a system service, stop and disable it first:
```bash
orion gateway stop
# Linux: systemctl --user disable orion-gateway
# macOS: launchctl remove ai.orion.gateway
```
:::
