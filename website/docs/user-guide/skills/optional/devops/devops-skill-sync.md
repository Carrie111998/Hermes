---
title: "Skill Sync — Sync skills between machines over SSH and Tailscale"
sidebar_label: "Skill Sync"
description: "Sync skills between machines over SSH and Tailscale"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Skill Sync

Sync skills between machines over SSH and Tailscale.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/devops/skill-sync` |
| Path | `optional-skills/devops/skill-sync` |
| Version | `1.0.0` |
| Author | alt-glitch (alt-glitch), Hermes Agent |
| License | MIT |
| Platforms | linux, macos |
| Tags | `Skills`, `Sync`, `SSH`, `Tailscale`, `Rsync`, `Multi-Machine`, `Cron` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Skill Sync

Sync `~/.hermes/skills/` between machines over SSH. Compares modification
times, pulls newer skills, discovers new ones, and can push the local tree to
a fresh box. Conflict rule is last-writer-wins by mtime; sync is additive
(nothing is deleted on either side). Works over any SSH route — Tailscale is
the recommended transport because it gives every machine a stable name that
works across networks.

## When to Use

- User has Hermes on two or more machines and wants skills kept in step
- User got a new machine and wants their skill library on it ("push my skills to the new box")
- User improved a skill on one box and wants it everywhere
- User asks for automatic/scheduled skill syncing between machines
- A skill exists on another machine but not here

## Prerequisites — walk the user through these, don't assume them

Each remote needs: a network route, a running sshd, key-based auth, and
rsync. **Run the doctor first** — it checks all four and prints the exact fix
for anything missing:

```
terminal(command="~/.hermes/skills/devops/skill-sync/scripts/doctor.sh <user@host>")
```

If the doctor reports failures, guide the user through them in this order:

1. **Network route (Tailscale, recommended).** If the machines aren't on the
   same LAN, have the user install Tailscale on both ends
   (https://tailscale.com/download) and run `tailscale up` on each, logged
   into the same tailnet. Verify with `tailscale status` — every machine gets
   a stable node name and a `100.x.y.z` IP that work from anywhere. Any other
   SSH route (LAN hostname, VPN, public IP) also works.
2. **sshd on the target.** Fresh macOS refuses SSH by default — the user must
   enable System Settings → General → Sharing → **Remote Login**, or run
   `sudo systemsetup -setremotelogin on`. Linux: `sudo apt install
   openssh-server && sudo systemctl enable --now ssh`. Tailscale users can
   skip sshd entirely with `sudo tailscale set --ssh` on the target.
3. **Key auth.** No password prompts — the scripts run with `BatchMode=yes`.
   If the user has no keypair: `ssh-keygen -t ed25519`. Then authorize it:
   `ssh-copy-id <user@host>` (one password entry, ever).
4. **The right login name.** With Tailscale the HOST is the node name from
   `tailscale status`, but the USER is that box's unix login — they usually
   differ, and the tailnet account name is often NOT an authorized login.
   Probe candidates before syncing:
   ```
   ssh -o BatchMode=yes -o ConnectTimeout=8 <candidate>@<host> 'echo OK'
   ```
   `Permission denied (publickey)` = wrong user or key not copied.
   `Connection refused` = sshd not running (step 2).

Re-run the doctor after each fix until it prints "all checks passed".

## How to Run

Invoke through the `terminal` tool. Scripts live in
`~/.hermes/skills/devops/skill-sync/scripts/` once installed. Remotes are
always explicit — positional args or `SKILL_SYNC_REMOTES` (comma-separated).

```bash
# Pull: bring newer/missing skills from a remote to this machine
~/.hermes/skills/devops/skill-sync/scripts/sync.sh user@host

# Push: send newer/missing local skills to a remote (new-machine bootstrap)
~/.hermes/skills/devops/skill-sync/scripts/sync.sh --push user@host

# Preview either direction without transferring
DRY_RUN=1 ~/.hermes/skills/devops/skill-sync/scripts/sync.sh user@host
```

## Quick Reference

| Command | What it does |
|---|---|
| `doctor.sh <user@host>` | Verify Tailscale/SSH/rsync; prints fixes |
| `sync.sh <user@host>` | Pull remote-newer + remote-only skills |
| `sync.sh --push <user@host>` | Push local-newer + local-only skills |
| `DRY_RUN=1 sync.sh ...` | Preview; read the `[^]`/`[+]` lines |
| `p2p_sync.py <user@host>` | Per-skill triage picker (no transfers) |

## Procedure

### First-time pairing

1. `doctor.sh <user@host>` — fix anything it flags (see Prerequisites).
2. `DRY_RUN=1 sync.sh <user@host>` — show the user the plan. The change set
   is the union of `[^]` (update) and `[+]` (new) lines.
3. `sync.sh <user@host>` (or `--push` for a new-machine bootstrap).
4. Verify (see Verification).

### Selective sync

When the user only wants some skills moved, run
`python3 ~/.hermes/skills/devops/skill-sync/scripts/p2p_sync.py <user@host>`.
It prints a numbered PULL/PUSH picker (new / updated / divergent per skill)
and transfers nothing. Present the picker, get the user's selection, then
rsync the chosen skill directories yourself:

```bash
rsync -azL -e "ssh -o BatchMode=yes" "user@host:.hermes/skills/<cat>/<skill>/" ~/.hermes/skills/<cat>/<skill>/
```

### Scheduled sync (cron)

The cron runner passes no args, so bake the remotes into a wrapper:

```bash
mkdir -p ~/.hermes/scripts
printf '#!/usr/bin/env bash\nSKILL_SYNC_REMOTES="user@host" exec ~/.hermes/skills/devops/skill-sync/scripts/sync.sh\n' > ~/.hermes/scripts/skill-sync-tick.sh
chmod +x ~/.hermes/scripts/skill-sync-tick.sh
hermes cron create "every 6h" --name skill-sync --script ~/.hermes/scripts/skill-sync-tick.sh --no-agent
```

## Conflict Resolution

The unit of sync is the whole skill directory (SKILL.md + references/ +
scripts/ + templates/), transferred atomically via rsync. The rule is
**last-writer-wins by SKILL.md mtime** — skills are single-author documents,
and this matches how they evolve: one machine gets the fix, the others pick
it up.

**Exception — the same skill forked on two machines.** If BOTH sides added
unique content, last-writer-wins is LOSSY: the newer mtime overwrites
wholesale and drops the loser's additions. `p2p_sync.py` flags these as
`divergent`. Do NOT sync a divergent skill — do a manual union merge; full
procedure in `references/merging-forked-skill-copies.md`.

## Pitfalls

- **mtime lies about content.** A copy can be newer AND missing files the
  older copy has (fork trap). When in doubt, compare file inventories:
  `ssh <remote> 'find ~/.hermes/skills/<cat>/<skill> -type f'` vs local.
- **`DRY_RUN=1` plan = the `[^]` + `[+]` lines**, not just the summary count.
- **Never trust "Push complete" — verify on the remote.** `sync.sh` prints a
  remote skill count after every push; if it doesn't move, the transfer
  failed. Spot-check specific paths too (see Verification).
- **Whole-tree fallback when per-skill sync misbehaves:** one additive pass,
  no `--delete`, surfaces errors:
  `rsync -azL -e "ssh -o BatchMode=yes" ~/.hermes/skills/ user@host:.hermes/skills/`
- **Only one SSH direction may work.** If push to a box is refused but that
  box can SSH back here, flip it: have the other machine's agent PULL. Write
  it a handoff note with the source `user@host` and the exact `sync.sh`
  command rather than fighting the dead direction.
- Sync is additive — it never deletes local skills missing on the remote.
  Within an updated skill, `--delete` prunes files the newer side removed.
- Skills under paths containing `.bak` or `.archive` are always excluded,
  both directions.
- macOS vs Linux `stat` flags differ; the scripts handle both.
- Byte-comparing across OSes: macOS `wc -c` left-pads output — `tr -d ' '`
  before comparing.

## Verification

```bash
# Counts converge and a spot-checked skill landed whole:
ssh -o BatchMode=yes user@host 'find ~/.hermes/skills -name SKILL.md | wc -l'
ssh -o BatchMode=yes user@host 'find ~/.hermes/skills/<cat>/<skill> -type f | sed "s|.*/skills/||"'
```

A second `DRY_RUN=1 sync.sh` run should report zero `[^]`/`[+]` lines — the
trees have converged.
