# Hermes Signal Note-to-Self Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Link the operator's existing Signal account to Hermes for private Note-to-Self messaging with automatic Windows-login startup.

**Architecture:** Follow the upstream `signal-cli-rest-api` Docker and QR-link instructions using one persistent Docker volume. After linking, replace the wrapper container with the same image's bundled signal-cli running the native `/api/v1/*` HTTP daemon required by this Hermes checkout; configure Hermes through its existing config helper and install its supported Windows gateway task.

**Tech Stack:** Docker Desktop, `bbernhard/signal-cli-rest-api:0.99`, signal-cli, PowerShell 7, Hermes Gateway, Windows Task Scheduler

## Global Constraints

- The phone remains the primary Signal device; `HermesAgent` is secondary.
- Only the operator's own account is allowlisted.
- Signal groups and allow-all access remain disabled.
- Port 8080 binds only to `127.0.0.1`.
- The phone number must not be printed in chat or command output.
- No Hermes source files are modified.
- Preserve `anthropic/claude-opus-5` primary and `openai-codex/gpt-5.6-sol` fallback.
- Do not touch the pre-existing `.install_method`.

---

### Task 1: Start the Upstream QR-Link Container

**Files:**

- Runtime create: Docker volume `hermes-signal-data`
- Runtime create: temporary container `hermes-signal-link`

**Interfaces:**

- Consumes: upstream image `bbernhard/signal-cli-rest-api:0.99`.
- Produces: local QR page at `http://127.0.0.1:8080/v1/qrcodelink?device_name=HermesAgent`.

- [ ] **Step 1: Verify Docker and port availability**

Run:

```powershell
docker version
docker manifest inspect bbernhard/signal-cli-rest-api:0.99 *> $null
if (Get-NetTCPConnection -LocalPort 8080 -ErrorAction SilentlyContinue) {
    throw 'Port 8080 is already in use'
}
```

Expected: Docker is available, the pinned image exists, and port 8080 is free.

- [ ] **Step 2: Create persistent storage and start the documented wrapper**

Run:

```powershell
docker volume create hermes-signal-data
docker run -d `
  --name hermes-signal-link `
  -p 127.0.0.1:8080:8080 `
  -v hermes-signal-data:/home/.local/share/signal-cli `
  -e MODE=native `
  bbernhard/signal-cli-rest-api:0.99
```

Expected: Docker returns a container ID.

- [ ] **Step 3: Verify the wrapper and open its QR page**

Run:

```powershell
$deadline = [DateTime]::UtcNow.AddSeconds(120)
do {
    try {
        $about = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8080/v1/about' -TimeoutSec 5
    } catch {
        $about = $null
    }
    if ($about.StatusCode -eq 200) { break }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)
if ($about.StatusCode -ne 200) { throw 'Signal wrapper did not become ready' }
Start-Process 'http://127.0.0.1:8080/v1/qrcodelink?device_name=HermesAgent'
```

Expected: the browser displays a QR code.

- [ ] **Step 4: Complete the one operator action**

On the phone, open:

```text
Signal > Settings > Linked Devices > Link New Device
```

Scan the QR code and wait until Signal confirms `HermesAgent`.

---

### Task 2: Discover the Account and Switch to Hermes-Compatible Daemon Mode

**Files:**

- Runtime modify: Docker volume `hermes-signal-data`
- Runtime create: ignored Hermes environment file resolved by `hermes config env-path`
- Runtime create: production container `hermes-signal`

**Interfaces:**

- Consumes: linked account from `GET /v1/accounts`.
- Produces: signal-cli native HTTP daemon at `http://127.0.0.1:8080/api/v1/check` and private Hermes Signal settings.

- [ ] **Step 1: Discover and validate exactly one linked account**

Run the following as one PowerShell block so the full number remains only in
process memory:

```powershell
$accounts = @(Invoke-RestMethod -Uri 'http://127.0.0.1:8080/v1/accounts' -TimeoutSec 10)
if ($accounts.Count -ne 1) { throw 'Expected exactly one linked Signal account' }
$account = [string]$accounts[0]
if ($account -notmatch '^\+[1-9][0-9]{6,14}$') {
    throw 'Linked account is not a valid E.164 number'
}
```

Expected: validation succeeds without printing `$account`.

- [ ] **Step 2: Save the private Hermes policy through Hermes' config helper**

Continue in the same PowerShell block:

```powershell
$env:HERMES_SIGNAL_ACCOUNT = $account
@'
import os
from hermes_cli.config import save_env_value

account = os.environ["HERMES_SIGNAL_ACCOUNT"]
save_env_value("SIGNAL_HTTP_URL", "http://127.0.0.1:8080")
save_env_value("SIGNAL_ACCOUNT", account)
save_env_value("SIGNAL_ALLOWED_USERS", account)
save_env_value("SIGNAL_ALLOW_ALL_USERS", "false")
save_env_value("SIGNAL_GROUP_ALLOWED_USERS", "")
save_env_value("SIGNAL_HOME_CHANNEL", account)
'@ | python -
if ($LASTEXITCODE -ne 0) { throw 'Hermes Signal configuration failed' }
Remove-Item Env:HERMES_SIGNAL_ACCOUNT
```

Expected: the helper exits `0` and does not echo the account.

- [ ] **Step 3: Replace the wrapper with native daemon mode**

Continue in the same PowerShell block:

```powershell
docker rm -f hermes-signal-link
docker run -d `
  --name hermes-signal `
  --restart unless-stopped `
  -p 127.0.0.1:8080:8080 `
  -v hermes-signal-data:/home/.local/share/signal-cli `
  -e SIGNAL_ACCOUNT=$account `
  --entrypoint /bin/sh `
  bbernhard/signal-cli-rest-api:0.99 `
  -c 'chown -R 1000:1000 /home/.local/share/signal-cli && exec setpriv --reuid=1000 --regid=1000 --init-groups signal-cli --config /home/.local/share/signal-cli --account "$SIGNAL_ACCOUNT" daemon --http 0.0.0.0:8080'
$account = $null
```

Expected: Docker returns a new container ID and the wrapper container is gone.

- [ ] **Step 4: Verify the exact endpoint Hermes uses**

Run:

```powershell
$deadline = [DateTime]::UtcNow.AddSeconds(120)
do {
    try {
        $health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8080/api/v1/check' -TimeoutSec 5
    } catch {
        $health = $null
    }
    if ($health.StatusCode -eq 200) { break }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)
if ($health.StatusCode -ne 200) {
    docker logs --tail 100 hermes-signal
    throw 'signal-cli native daemon did not become ready'
}
```

Expected: HTTP 200.

---

### Task 3: Install Automatic Startup and Verify Access Policy

**Files:**

- Runtime create/update: Hermes-managed Windows gateway Scheduled Task
- Runtime verify: Docker container restart policy and Docker Desktop login setting

**Interfaces:**

- Consumes: healthy native daemon and active Hermes home.
- Produces: automatically starting Signal bridge and Hermes messaging gateway.

- [ ] **Step 1: Validate Hermes configuration and preserve model routing**

Run:

```powershell
hermes config check
hermes config get model
hermes fallback list
```

Expected: config check succeeds; primary is `anthropic/claude-opus-5`; fallback
is `openai-codex/gpt-5.6-sol`.

- [ ] **Step 2: Install and start the supported Windows gateway task**

Run:

```powershell
hermes gateway install --force --start-now --start-on-login
hermes gateway status
```

Expected: the gateway reports running and login startup enabled.

- [ ] **Step 3: Verify the bridge is loopback-only and restartable**

Run:

```powershell
docker inspect hermes-signal --format '{{.HostConfig.RestartPolicy.Name}}'
docker port hermes-signal
Get-NetTCPConnection -State Listen -LocalPort 8080 |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

Expected: restart policy is `unless-stopped`; every port binding is
`127.0.0.1:8080`; no wildcard or LAN address appears.

- [ ] **Step 4: Ensure Docker Desktop starts at Windows login**

Inspect Docker Desktop's **Start Docker Desktop when you sign in** setting.
Enable it only if currently disabled.

Expected: Docker Desktop and its `unless-stopped` container recover after login.

---

### Task 4: Note-to-Self Acceptance and Restart Proof

**Files:**

- No source files.

**Interfaces:**

- Consumes: operator's Signal Note-to-Self conversation.
- Produces: real message, loop-prevention, and restart-recovery evidence.

- [ ] **Step 1: Confirm the Signal adapter connected**

Run:

```powershell
hermes gateway status
hermes logs gateway --level info
```

Expected: logs include `Signal: connected` and no duplicate-listener error.

- [ ] **Step 2: Run the first real Note-to-Self probe**

Send from Signal Note to Self:

```text
Reply with exactly: HERMES_SIGNAL_OK
```

Expected: exactly one reply containing `HERMES_SIGNAL_OK`.

- [ ] **Step 3: Confirm echo-loop protection**

Wait 30 seconds and confirm no second Hermes reply arrives. Inspect:

```powershell
hermes logs gateway --level warning
```

Expected: no repeated turn and no echo-loop warning.

- [ ] **Step 4: Prove restart recovery**

Run:

```powershell
docker restart hermes-signal
hermes gateway restart
Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8080/api/v1/check' -TimeoutSec 10
hermes gateway status
```

Then send:

```text
Reply with exactly: HERMES_SIGNAL_RESTART_OK
```

Expected: exactly one reply containing `HERMES_SIGNAL_RESTART_OK`.

- [ ] **Step 5: Final state report**

Report:

```text
Signal device: HermesAgent linked to existing phone
Access: Note to Self only
Groups: disabled
Bridge: bbernhard/signal-cli-rest-api:0.99 image running native signal-cli HTTP mode
Endpoint: http://127.0.0.1:8080
Container restart: unless-stopped
Hermes gateway: Windows login task running
Primary: anthropic/claude-opus-5
Fallback: openai-codex/gpt-5.6-sol
Acceptance: initial response, no echo, post-restart response
```
