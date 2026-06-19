---
name: linux-security-guardian
description: |-
  Linux server security health check — 5-dimension assessment covering SSH hardening,
  firewall & network, system hardening, user privileges, and operational security.
  Outputs a scored report with prioritized fix commands. Like a health checkup for your server.
  The agent runs all checks automatically, then offers to apply high-priority fixes interactively.
platforms: [debian, ubuntu]
category: security
triggers:
  - "security check"
  - "security health check"
  - "server security audit"
  - "hardening check"
  - "Linux security scan"
  - "检查服务器安全"
  - "安全体检"
  - "服务器安全加固"
toolsets:
  - terminal
  - file
---

# 🛡️ Linux Security Guardian

> **One-liner**: A lightweight, agent-driven security health check for Debian/Ubuntu servers.
> The agent automatically audits SSH, firewall, system hardening, user privileges, and
> operational security, produces a scored report, and interactively applies high-priority fixes.

**Target distribution**: Debian / Ubuntu (uses `apt` and `ufw`)
**Target users**: Developers, small-team ops, self-hosters
**Time**: Automated audit ~30s; with interactive fixes ~2-3 min

---

## ⚠️ Safety Guardrails

These are **hard rules**. Violating them invalidates the check and may lock you out of the server.

1. **Never modify SSH config without user confirmation.** Before any `sed` or file edit
   on `/etc/ssh/sshd_config`, show the exact change to the user and ask: *"Apply this change? (y/N)"*
2. **Test SSH access before closing the session.** After any SSH config change, ask the user to
   open a SECOND terminal and verify login BEFORE the current session is closed.
3. **Root-only checks must detect privilege first.** Run `sudo -n true 2>/dev/null` before any
   `sudo` command. If it fails, skip the check and note it in the report.
4. **Never blindly run `apt upgrade`.** Show the pending package list and ask for confirmation.
5. **Fix, then verify.** After applying any fix, re-run the relevant check(s) and confirm the
   score improved. Report the before/after delta.

---

## Phase 1: Discover Environment

Before running any checks, collect the environment baseline:

```bash
# Kernel & distro
uname -a
cat /etc/os-release

# Uptime & load — spikes may indicate compromise
uptime

# Who is logged in
who

# sudo capability
sudo -n true 2>/dev/null && echo "has_sudo" || echo "no_sudo"
```

From the output:
- Note the distro/version (check against Debian/Ubuntu assumption)
- Record `has_sudo` or `no_sudo` — this gates which Phase 2 checks are possible

Report the environment to the user before proceeding.

---

## Phase 2: Run Audit (5 Dimensions)

Execute each check group in order. For every check:

1. Run the **audit command**.
2. Compare output against the **pass condition**.
3. Record **pass** or **fail** for the check.
4. If **fail**: record the **fix command** (do NOT execute yet).
5. Accumulate the raw score.

Format the result as a flat JSON structure for scoring in Phase 3.

### 2.1 SSH Security (25%)

Audit all 5 checks:

```bash
# C1: Non-default port (5 pts)
# If output is empty (line commented or absent), Port 22 is the default → fail
# If output shows Port ≠ 22 → pass
grep -E '^Port\s+' /etc/ssh/sshd_config

# C2: Password auth disabled (5 pts)
grep -E '^PasswordAuthentication\s+' /etc/ssh/sshd_config

# C3: Root login disabled (5 pts)
grep -E '^PermitRootLogin\s+' /etc/ssh/sshd_config

# C4: Key auth configured (5 pts)
ls ~/.ssh/authorized_keys 2>/dev/null && wc -l < ~/.ssh/authorized_keys

# C5: Protocol version (5 pts) — modern SSH defaults to v2; Protocol 1 is the fail
grep -E '^Protocol\s+1' /etc/ssh/sshd_config
```

Pass conditions:
| Check | Pass if |
|-------|---------|
| C1 Port | Output contains a `Port` line with a number ≠ 22 |
| C2 PasswordAuth | Output is `PasswordAuthentication no` (or line absent AND no `yes` override) |
| C3 RootLogin | Output is `PermitRootLogin no` |
| C4 Keys | File exists and has ≥ 1 line |
| C5 Protocol | No line matching `Protocol 1` |

### 2.2 Network & Firewall (25%)

```bash
# C6: Firewall active (9 pts)
ufw status 2>/dev/null || echo "not-installed"
# Also check iptables if ufw absent:
sudo -n iptables -L -n 2>/dev/null | head -5 || echo "no-iptables-access"

# C7: Minimal open ports (9 pts)
# List all public listeners (non-127.0.0.1)
ss -tlnp | awk 'NR>1{print $4}' | grep -v 127.0.0.1 | sed 's/.*://'

# C8: Services bound locally (7 pts) — check common DB/cache ports
for port in 6379 5432 3306 27017 11211; do
  ss -tlnp | grep -q "0.0.0.0:$port" && echo "WARN: port $port on 0.0.0.0"
done
```

Pass conditions:
| Check | Pass if |
|-------|---------|
| C6 Firewall | `ufw status` shows `active`, OR iptables has rules |
| C7 Open ports | Only expected service ports (SSH, HTTP/S, etc.) are public |
| C8 Local bind | No WARN lines (DB/cache bound to 0.0.0.0) |

For C7, prompt the user: *"These ports are exposed. Are any unexpected? (y/N)"*
If the user marks any as unexpected, record a fail.

### 2.3 System Hardening (20%)

```bash
# C9: Security updates (5 pts)
apt list --upgradable 2>/dev/null | grep -i security | wc -l

# C10: Shadow permissions (5 pts)
stat -c '%a' /etc/shadow 2>/dev/null

# C11: /tmp sticky bit (5 pts)
stat -c '%a' /tmp 2>/dev/null | grep -q '^1' && echo "sticky" || echo "no-sticky"

# C12: npm global packages (5 pts)
npm ls -g --depth=0 2>/dev/null | tail -n +2 | wc -l
```

Pass conditions:
| Check | Pass if |
|-------|---------|
| C9 Security updates | Output is `0` (no security upgrades pending) |
| C10 Shadow | Output is `600` or `640` |
| C11 Sticky bit | Output is `sticky` |
| C12 npm | ≤ 10 global packages (alert user if more) |

### 2.4 User Privileges (20%)

```bash
# C13: Empty passwords (7 pts)
sudo -n awk -F: '($2 == "" || $2 == "!") {print $1}' /etc/shadow 2>/dev/null

# C14: Sudo group (7 pts)
getent group sudo 2>/dev/null | cut -d: -f4

# C15: Failed logins (6 pts)
lastb 2>/dev/null | wc -l
```

Pass conditions:
| Check | Pass if |
|-------|---------|
| C13 Empty passwords | No output (or only system accounts) — **requires sudo** |
| C14 Sudo group | Output shows only expected system administrators |
| C15 Failed logins | < 10 recent entries (subjective; flag to user if high) |

### 2.5 Operational Security (10%)

```bash
# C16: fail2ban (4 pts)
systemctl is-active fail2ban 2>/dev/null

# C17: Auto-updates (3 pts)
dpkg -l unattended-upgrades 2>/dev/null | grep -c '^ii'

# C18: Logging (3 pts)
systemctl is-active rsyslog 2>/dev/null || journalctl -n 1 2>/dev/null | wc -l
```

Pass conditions:
| Check | Pass if |
|-------|---------|
| C16 fail2ban | Output is `active` |
| C17 Auto-updates | ≥ 1 (package installed) |
| C18 Logging | Service active OR journalctl has output |

---

## Phase 3: Score & Report

Calculate the composite score:

```python
DIMENSIONS = {
    "SSH Security":       {"weight": 25, "checks": [5, 5, 5, 5, 5]},       # 5 checks
    "Network & Firewall": {"weight": 25, "checks": [9, 9, 7]},            # 3 checks
    "System Hardening":   {"weight": 20, "checks": [5, 5, 5, 5]},         # 4 checks
    "User Privileges":    {"weight": 20, "checks": [7, 7, 6]},            # 3 checks
    "Operational Security":{"weight": 10, "checks": [4, 3, 3]},           # 3 checks
}

WHEN_UNABLE_TO_CHECK = "skip"  # skip skips the check (0 pts awarded); "exclude" removes it from the dimension

for dim_name, dim in DIMENSIONS.items():
    passes = [result for result in dim_results[dim_name] if result == "pass"]
    raw_score = sum(pts for pts, r in zip(dim["checks"], dim_results[dim_name]) if r == "pass")
    max_possible = sum(dim["checks"])
    dim_pct = round(raw_score / max_possible * 100)
    weighted = dim_pct * dim["weight"] / 100
    composite += weighted

composite = round(composite)

GRADES = [
    (90, "A", "🛡️ Excellent", "Baseline security in place"),
    (70, "B", "⚠️ Good", "Minor optimizations available"),
    (50, "C", "🔶 Fair", "Notable risks — recommend fixes"),
    (0,  "D", "🔴 Poor", "Critical gaps — fix immediately"),
]
```

**When a check cannot be run** (e.g. C13 needs sudo and user declined to provide it):
- Skip the check (mark as `unable`), award 0 pts for it.
- Note it in the report: `"C13 Empty passwords — skipped (requires sudo access)"`.
- The max_possible DOES NOT change — the report honestly reflects incomplete coverage.

### Report Format

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  🛡️ Linux Security Guardian
  Server: <hostname>
  Time: <ISO-8601 timestamp>
  Coverage: 14/18 checks (4 skipped — needs sudo)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 Score: <N>/100 (<Grade> — <Label>)

① SSH Security:      <N>/100 <emoji>
  ✅ <passing check>
  ❌ <failing check>     ← fix: <fix command>

② Network & Firewall: <N>/100 <emoji>
  ...

③ System Hardening:   ...

④ User Privileges:    ...

⑤ Ops Security:      ...

📋 Fix Priority:
  [HIGH]   <critical fix 1>
  [HIGH]   <critical fix 2>
  [MEDIUM] <medium fix>
  [LOW]    <nice-to-have fix>
```

---

## Phase 4: Interactive Fix

After presenting the report, DO NOT apply fixes automatically. Instead:

1. **Ask**: *"Would you like me to apply the HIGH-priority fixes? (y/N)"*
2. If yes, apply fixes **one at a time**, with confirmation before each:
   - Show the fix command and what it changes
   - Ask: *"Apply this fix? (y/N)"*
   - If yes, execute; if no, skip and explain the risk
3. After each fix, re-run the affected check(s) to confirm.
4. For **SSH config changes**: After editing `sshd_config`, run `sshd -t` to validate
   the config syntax BEFORE restarting sshd. Only proceed if `sshd -t` returns exit
   code 0. If it fails, display the error output to the user and abort the fix — do not
   restart sshd or continue applying this change.
5. For **SSH config changes**: remind the user to open a second SSH session and verify
   login BEFORE closing the current one.
6. **Never** modify `sshd_config` and then close the session in the same turn.

### Fix Commands Reference

| Check | Fix Command |
|-------|-------------|
| C1 Port | Suggest changing port in `/etc/ssh/sshd_config` |
| C2 PasswordAuth | `sed -i 's/^PasswordAuthentication yes$/PasswordAuthentication no/' /etc/ssh/sshd_config && sshd -t` |
| C3 RootLogin | Edit `PermitRootLogin no` in `/etc/ssh/sshd_config` |
| C4 Keys | `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" && cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys` |
| C6 Firewall | `ufw allow <ssh-port> && ufw enable` |
| C7 Exposed | `ufw deny <port>` |
| C8 Local bind | Show config file paths for each exposed service |
| C9 Updates | Show `apt list --upgradable \| grep security` output, ask before `apt upgrade` |
| C13 Empty pwd | `passwd -l <user>` |
| C14 Sudo | `gpasswd -d <user> sudo` |
| C16 fail2ban | `apt install -y fail2ban && systemctl enable --now fail2ban` |
| C17 Auto-updates | `apt install -y unattended-upgrades && dpkg-reconfigure -plow unattended-upgrades` |

---

## Verification

After applying any fix:

1. Re-run the affected check(s):
   ```bash
   <audit command from Phase 2>
   ```
2. Confirm the check now passes.
3. Recalculate the composite score and show the before/after delta.
4. Report: *"Score improved from <before> to <after>. <N> fixes applied."*

For SSH config changes specifically:
- Run `sshd -t` to validate syntax BEFORE restarting sshd.
- Tell the user: *"Please open a NEW terminal and SSH in to verify. Keep this session open as a fallback."*
- Do NOT restart sshd or close the session for the user — they must do this manually for safety.

## Testing the Skill

```bash
# Quick test — just check SSH checks are accessible
grep -E '^Port\s+|^PasswordAuthentication\s+|^PermitRootLogin\s+' /etc/ssh/sshd_config

# Full test — run all audit commands (as root or with sudo)
bash -c "$(grep -E '^\s*# C[0-9]+' SKILL.md | sed 's/.*# C[0-9]\+: //' | tr '\n' ';')"
```

## Pitfalls

- **SSH lockout risk**: The #1 danger. Never modify `sshd_config` without (a) syntax validation
  via `sshd -t`, (b) user confirmation, and (c) instructing the user to test in a separate session.
  If the new session fails, the old session is still alive for recovery.
- **`ufw enable` blocks cloud metadata**: Cloud providers serve metadata at 169.254.169.254.
  Run `ufw allow from 169.254.169.254` BEFORE `ufw enable`.
- **fail2ban whitelist**: If the user's SSH client has a dynamic IP (VPN, roaming), set
  `ignoreip` in `/etc/fail2ban/jail.local` before enabling.
- **`apt upgrade` without review**: Always show the pending package list. A kernel upgrade
  triggers a reboot requirement that the user may not be ready for.
- **npm false positives**: Some packages legitimately use postinstall scripts. Never
  auto-uninstall; flag for user review with the script content.
- **Permission gaps**: The report honestly reflects which checks could and could not run.
  Telling the user *"4 checks need root — run with sudo for full coverage"* is honest.
