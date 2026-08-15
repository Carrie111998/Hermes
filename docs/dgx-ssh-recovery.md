# DGX SSH recovery

Use the repository wrapper for all Codex-to-DGX SSH operations. It prefers the
already authenticated WSL SSH agent/default identity, then falls back to native
Windows OpenSSH when WSL is unavailable. Both routes use strict host-key
verification, bounded timeouts, and no stored credentials. For `exec`, a WSL
authentication failure is the only condition that triggers the native fallback;
remote command failures are returned directly, including a remote exit status
of `75` (the PowerShell bridge uses an internal sentinel to distinguish it from
the wrapper's `REAUTH_REQUIRED` status and never repeats the command). A successful
`probe` does not emit a reauth warning.

From Windows PowerShell:

```powershell
.\scripts\dgx_ssh.ps1 probe
.\scripts\dgx_ssh.ps1 exec hostname
```

From WSL:

```bash
bash scripts/dgx_ssh.sh probe
bash scripts/dgx_ssh.sh exec hostname
```

Before either wrapper attempts SSH, configure the target in the existing
user-owned Hermes config. The resolver reads the platform-native Hermes home
(`$HERMES_HOME` when set, otherwise the normal Hermes default):

```yaml
dgx_ssh:
  host: "<configured-hostname-or-ipv4>"
  user: "<configured-ssh-user>"
```

Missing, unreadable, malformed, partial, or unsafe target configuration fails
closed with `CONFIG_ERROR:<reason>` and exit code `78`; no SSH process is
started. A missing resolver Python runtime has the same result.

If a non-interactive probe cannot authenticate, it exits with code `75` and
prints `REAUTH_REQUIRED`. Run one of these only in a real interactive terminal:

```powershell
.\scripts\dgx_ssh.ps1 auth
.\scripts\dgx_ssh.ps1 bootstrap
```

`auth` performs one interactive login and lets OpenSSH reuse the control
connection; the native Windows fallback preserves the interactive console for
password/MFA prompts. The Windows entrypoint tries WSL auth first and native
Windows SSH second. `bootstrap` first reuses any working identity; only if none works does
it interactively create/authorize an Ed25519 public key through WSL. Passwords,
MFA codes, private keys, and tokens are never written to this repository.
Host-key verification remains strict; unknown or changed host keys are not
auto-accepted.

The wrapper intentionally does not start Claude, AGY, or any other headless
agent. Authentication and agent dispatch remain separate gates.
