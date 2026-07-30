# Hermes Subscription Routing and Desktop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure the installed Hermes agent to use Claude Max as its primary provider, ChatGPT/Codex as its fallback, and expose the existing Electron app through a Windows desktop shortcut.

**Architecture:** Reuse Hermes' existing `anthropic` and `openai-codex` OAuth providers, its top-level `fallback_providers` configuration, and its packaged Electron application. No provider or desktop source changes are planned; the deliverables are Hermes-owned credentials, a minimal user configuration update, a verified packaged executable, and one shortcut.

**Tech Stack:** Hermes Agent 0.18.2, Python 3.11, Anthropic OAuth, OpenAI Codex OAuth, YAML configuration, Electron 40, React 19, PowerShell, Windows Shell shortcuts.

## Global Constraints

- Claude Max is the primary inference provider.
- ChatGPT through OpenAI Codex is the first and only configured fallback.
- Keep the existing Copilot credential pool intact and outside the selected route.
- Do not copy credentials into source, configuration prose, command output, the desktop package, or the shortcut.
- Do not modify Hermes provider or desktop source unless a live existing path fails and a revised design is approved.
- Preserve `D:\AI-Foundry\Infrastructure\hermes\.install_method`.
- Treat `D:\AI-Foundry\Infrastructure\hermes\.hermes\config.yaml` as the active Hermes configuration proved by `hermes config path`.
- A configuration value or successful build is not runtime proof; require real provider and GUI checks.

---

## File and state map

- Modify: `D:\AI-Foundry\Infrastructure\hermes\.hermes\config.yaml`
  - Owns primary model/provider and ordered fallback entries.
- Modify through Hermes only: `D:\AI-Foundry\Infrastructure\hermes\.hermes\` credential state
  - Hermes owns OAuth token storage; never edit these files manually.
- Generate through Hermes only: `D:\AI-Foundry\Infrastructure\hermes\apps\desktop\release\`
  - Contains the packaged Windows executable and support files.
- Create: `%USERPROFILE%\Desktop\Hermes.lnk`
  - Points to the exact packaged `Hermes.exe`; contains no provider arguments.
- Do not modify: Hermes Python, TypeScript, package manifests, or `.install_method`.

### Task 1: Authenticate both subscription providers

**Files:**

- Modify through CLI: Hermes-owned credential state under `D:\AI-Foundry\Infrastructure\hermes\.hermes\`
- Do not modify manually: credential JSON, OAuth cache, or `.env`

**Interfaces:**

- Consumes: the existing Claude Code login (`claude auth status`) and ChatGPT login (`codex login status`) as operator context only.
- Produces: Hermes credential-pool entries for `anthropic` and `openai-codex`.

- [ ] **Step 1: Capture the pre-change state without exposing tokens**

Run:

```powershell
hermes config path
hermes config get model
hermes config get fallback_providers
hermes auth list
hermes auth status anthropic
hermes auth status openai-codex
```

Expected: the config path is `D:\AI-Foundry\Infrastructure\hermes\.hermes\config.yaml`; the primary is the current custom Ollama route; fallback is `[]`; Copilot remains present; both target providers initially report logged out.

- [ ] **Step 2: Add the Claude Max OAuth credential**

Run:

```powershell
hermes auth add anthropic --type oauth --label "Claude Max"
```

Expected: Hermes opens Anthropic authorization, the operator approves it, and the command reports a new `anthropic` credential. If the command waits for a callback or code, keep that single process active until the provider flow completes.

- [ ] **Step 3: Verify the Claude credential without printing it**

Run:

```powershell
hermes auth status anthropic
hermes auth list
```

Expected: Anthropic reports logged in/configured and `Claude Max` appears in the pool. No raw token appears.

- [ ] **Step 4: Add the ChatGPT/Codex OAuth credential**

Run:

```powershell
hermes auth add openai-codex --type oauth --label "ChatGPT"
```

Expected: Hermes starts the OpenAI Codex authorization flow, the operator approves it, and the command reports a new `openai-codex` credential.

- [ ] **Step 5: Verify the Codex credential and ensure Copilot was preserved**

Run:

```powershell
hermes auth status openai-codex
hermes auth list
```

Expected: OpenAI Codex reports logged in, `ChatGPT` appears, and the pre-existing Copilot entry still appears unchanged.

### Task 2: Configure primary and fallback routing

**Files:**

- Modify: `D:\AI-Foundry\Infrastructure\hermes\.hermes\config.yaml`

**Interfaces:**

- Consumes: authenticated `anthropic` and `openai-codex` provider entries from Task 1.
- Produces: `model.provider = anthropic`, `model.default = claude-opus-5`, no stale custom base URL, and one fallback `{provider: openai-codex, model: gpt-5.6-sol}`.

- [ ] **Step 1: Switch the primary provider and model through Hermes**

Run:

```powershell
hermes config set model.provider anthropic
hermes config set model.default claude-opus-5
hermes config unset model.base_url
```

Expected: all three commands succeed. The unset removes the old Ollama endpoint so it cannot contaminate the Anthropic route.

- [ ] **Step 2: Write the ordered fallback as YAML**

Apply this exact configuration shape to `D:\AI-Foundry\Infrastructure\hermes\.hermes\config.yaml`, preserving every unrelated key:

```yaml
fallback_providers:
  - provider: openai-codex
    model: gpt-5.6-sol
```

Do not use `hermes config set fallback_providers ...`; this Hermes version treats unknown composite values as strings rather than YAML lists.

- [ ] **Step 3: Validate the resolved configuration**

Run:

```powershell
hermes config get model
hermes config get fallback_providers
hermes fallback list
hermes config check
```

Expected: Anthropic/`claude-opus-5` is primary, Codex/`gpt-5.6-sol` is fallback #1, the fallback value resolves as a list rather than a string, and configuration validation reports no routing error.

- [ ] **Step 4: Verify the primary with a real one-shot prompt**

Run:

```powershell
hermes -z "Reply with exactly: HERMES_CLAUDE_PRIMARY_OK"
```

Expected: exit code `0`, exact marker in the response, and no fallback activation message.

- [ ] **Step 5: Verify the fallback provider directly without changing the saved primary**

Run:

```powershell
hermes --provider openai-codex --model gpt-5.6-sol -z "Reply with exactly: HERMES_CHATGPT_FALLBACK_OK"
```

Expected: exit code `0` and the exact fallback marker. Afterward, `hermes config get model.provider` must still return `anthropic`.

### Task 3: Build and expose Hermes Desktop

**Files:**

- Generate: `D:\AI-Foundry\Infrastructure\hermes\apps\desktop\release\**`
- Create: `%USERPROFILE%\Desktop\Hermes.lnk`

**Interfaces:**

- Consumes: the existing Hermes checkout and the routing state from Tasks 1–2.
- Produces: one packaged Windows `Hermes.exe` and one shortcut targeting it.

- [ ] **Step 1: Build the packaged desktop app without launching it**

Run from `D:\AI-Foundry`:

```powershell
hermes desktop --build-only
```

Expected: exit code `0`; Hermes installs/reuses workspace dependencies, builds the renderer and Electron main process, packages the unpacked Windows application, and does not open the GUI.

- [ ] **Step 2: Resolve and validate the exact executable**

Run:

```powershell
$hermesExecutable = Get-ChildItem -LiteralPath 'D:\AI-Foundry\Infrastructure\hermes\apps\desktop\release' -Recurse -Filter 'Hermes.exe' |
  Where-Object { $_.FullName -match 'win-unpacked|windows' } |
  Select-Object -First 1 -ExpandProperty FullName
if (-not $hermesExecutable -or -not (Test-Path -LiteralPath $hermesExecutable -PathType Leaf)) {
  throw 'Packaged Hermes.exe was not found'
}
$hermesExecutable
```

Expected: one absolute path under the `release` directory. Stop before creating a shortcut if no executable resolves.

- [ ] **Step 3: Create the desktop shortcut**

Run with the exact `$hermesExecutable` resolved in Step 2:

```powershell
$desktopDirectory = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopDirectory 'Hermes.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $hermesExecutable
$shortcut.WorkingDirectory = 'D:\AI-Foundry'
$shortcut.IconLocation = "$hermesExecutable,0"
$shortcut.Description = 'Open Hermes Desktop'
$shortcut.Save()
```

Expected: `%USERPROFILE%\Desktop\Hermes.lnk` exists. Its target is the packaged executable, working directory is `D:\AI-Foundry`, and arguments are empty.

- [ ] **Step 4: Inspect the shortcut before launching**

Run:

```powershell
$shell = New-Object -ComObject WScript.Shell
$resolved = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Hermes.lnk'))
[pscustomobject]@{
  TargetPath = $resolved.TargetPath
  WorkingDirectory = $resolved.WorkingDirectory
  Arguments = $resolved.Arguments
  IconLocation = $resolved.IconLocation
}
```

Expected: the target and working directory match Steps 2–3, arguments are empty, and the icon comes from `Hermes.exe`.

### Task 4: End-to-end desktop verification and handoff

**Files:**

- Read only: `%USERPROFILE%\Desktop\Hermes.lnk`
- Read only: Hermes desktop and backend logs under the active Hermes home

**Interfaces:**

- Consumes: the configured providers, packaged application, and shortcut.
- Produces: evidence that the shortcut launches a usable GUI backed by the same Hermes configuration.

- [ ] **Step 1: Launch through the shortcut**

Run:

```powershell
Start-Process -FilePath (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Hermes.lnk')
```

Expected: one Hermes window opens without a visible terminal window.

- [ ] **Step 2: Verify backend readiness**

In the GUI, wait for the startup overlay to complete and confirm the chat composer becomes usable. If startup fails, collect:

```powershell
hermes logs desktop
hermes logs gateway
```

Expected: the desktop connects to its headless Hermes backend using the same active Hermes home.

- [ ] **Step 3: Send one real GUI prompt**

Send:

```text
Reply with exactly: HERMES_DESKTOP_CLAUDE_OK
```

Expected: the GUI returns the exact marker through the configured Claude primary.

- [ ] **Step 4: Reconcile final state**

Run:

```powershell
hermes auth status anthropic
hermes auth status openai-codex
hermes config get model
hermes fallback list
git -C 'D:\AI-Foundry\Infrastructure\hermes' status --short --branch
```

Expected: both providers are authenticated, Claude remains primary, ChatGPT/Codex remains fallback #1, `.install_method` is still the only pre-existing unrelated untracked file, and no Hermes source file changed during setup.

- [ ] **Step 5: Record the operational handoff**

Report:

```text
Hermes home: D:\AI-Foundry\Infrastructure\hermes\.hermes
Primary: anthropic / claude-opus-5 / OAuth subscription
Fallback: openai-codex / gpt-5.6-sol / ChatGPT subscription
Desktop executable: the absolute path emitted by Task 3 Step 2
Desktop shortcut: the absolute path returned by Join-Path in Task 3 Step 3
Primary probe: the observed pass result or its exact failure
Fallback probe: the observed pass result or its exact failure
Desktop probe: the observed pass result or its exact failure
```

Write the observed paths and probe results in the handoff. Do not include credentials.
