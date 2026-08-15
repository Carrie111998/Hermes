# Decision Record: Slack Attachment Ingest Scopes & Blast Radius Mitigation

**Issue:** [DAN-2767](https://linear.app/issue/DAN-2767)  
**Status:** PROPOSED / REVIEW  
**Scope:** `dizhaky/hermes-agent` (Slack Gateway Adapter & App Manifest)  
**Author:** Antigravity / Hermes Engineering  
**Date:** 2026-08-15  
**Related Issues:** [DAN-2764](https://linear.app/issue/DAN-2764) (Channel allowlist cleanup), [DAN-2043](https://linear.app/issue/DAN-2043) (Privileged channel classification), [DAN-2045](https://linear.app/issue/DAN-2045) (Slack credential consolidation)

---

## 1. Executive Summary & Motivation

To support rich multimodal workflows — including processing voice clips, analyzing technical diagrams, inspecting PDF contracts, and responding with generated artifacts — Hermes requires the ability to ingest files uploaded to Slack channels and direct messages.

During the execution of **DAN-2764** (which pruned archived Slack channels from `slack.allowed_channels`), the requirement to add `files:read` and `files:write` bot scopes was surfaced. Because adding OAuth scopes is not a simple configuration edit but rather a **Slack App Manifest modification requiring an app reinstall and token rotation**, and because `files:read` grants workspace-wide file access across all joined channels, this decision document defines the threat model, blast radius, channel boundaries, and implementation plan before any scope expansion is applied.

---

## 2. Threat Model & Blast Radius Assessment

### 2.1 The Slack OAuth Permission Architecture
In Slack's permission model:
- `files:read` is **workspace-wide**, not scoped per-channel. Once granted to the bot user, the bot token can read metadata and download the binary contents of any file uploaded to *any channel the bot is a member of*, as well as files shared in DMs/MPIMs.
- There is no native Slack-side mechanism to grant `files:read` exclusively to specific channels (e.g., `#hermes-home`) while denying it in others (e.g., `#accounting`).

### 2.2 Channel Topology & Privileged Data Surfaces
The live Hermes workspace currently allowlists 16 active channels. Per **DAN-2043**, four of these channels are designated as **Privileged / Highly Confidential**:

| Channel Name | Channel ID | Data Classification & Sensitivity | Risk Vector Under Unbounded `files:read` |
| :--- | :--- | :--- | :--- |
| **`#accounting`** | `C0BCT6NA64F` | **Confidential Financial:** QBO ledgers, tax workpapers (1120-S), bank reconciliation statements, wire confirmations. | Unintentional ingestion of bank statements, tax IDs, or PII into LLM context windows, violating `~/.claude/CLAUDE.md` §Misc ("No financial data output"). |
| **`#ust-legal-hhs`** | `C0AECAHJR1N` | **Privileged Legal:** HHS CBCA 8583 litigation filings, settlement terms, protective order discovery materials. | Ingestion of sealed court filings, discovery exhibits, or attorney-client privileged memos into external inference providers. |
| **`#ust-general`** | `C0ACJT93QKB` | **Corporate Wind-down:** Asset liquidation, creditor correspondence, corporate dissolution workpapers. | Accidental parsing of third-party creditor agreements or sensitive personnel matters. |
| **`#security`** | `C0ACNQ8FEHX` | **Infrastructure Security:** Fusion-watchdog alerts, 1Password audit logs, CrowdStrike threat detections. | Exposure of raw stack traces containing sanitized credentials, token fragments, or network topologies. |

### 2.3 Key Threat Vectors
1. **Context Leakage to LLMs:** A user uploads a bank statement or confidential litigation PDF in `#accounting` or `#ust-legal-hhs`. An unbounded listener automatically downloads, extracts text, and injects the document into the conversation prompt sent to cloud inference providers (OpenAI, Anthropic, Google).
2. **Indirect Prompt Injection:** An untrusted document (e.g., vendor invoice, external PDF) contains adversarial system prompt override instructions.
3. **Context Window Flooding / DoS:** Large PDF dumps or high-resolution multi-page scans exhaust model context tokens and inflate inference latency/costs.
4. **Credential Rotation Alerting Outage:** Reinstalling the Slack app invalidates the current `SLACK_BOT_TOKEN`, temporarily disrupting health reporting on `#open-claude-health`.

---

## 3. The Four Core Decisions

### Decision 1: Channel Scoping & Hardened Isolation Boundary
* **Rule:** Enforce application-layer channel gating for file ingest inside `SlackAdapter`, independent of general message allowlists.
* **Mechanism:**
  1. Introduce `slack.file_ingest_allowed_channels` in `hermes/config.yaml`.
  2. By default, only non-privileged collaboration channels are permitted for file attachment processing:
     - `#hermes-home` (`C0BFXBH5Z8Q`)
     - `#daily-brief` (`C0BMT1GQ2FK`)
     - `#dev` (`C0AC2H0J85B`)
     - `#general` (`C0AC3U200F8`)
     - Direct Messages (DMs / 1:1 with Dan)
  3. **Hardcoded Denylist:** The gateway adapter will explicitly drop and reject attachment downloads originating from `#accounting`, `#ust-legal-hhs`, `#ust-general`, and `#security`, replacing them with a safe, non-ingested stub (`[attachment omitted: privileged channel]`).

### Decision 2: Decoupling Read and Write Scopes
* **Rule:** Adopt a phased scope rollout: **Phase 1 implements `files:read` only.**
* **Rationale:** Hermes currently returns responses via Slack mrkdwn, Block Kit cards, code fences, and links. File uploads (`files:write`) introduce complex multipart upload APIs (Slack `files.getUploadURLExternal` / `files.completeUploadExternal`) and are not required for core voice/image ingestion. `files:write` is deferred to Phase 2 after read stability is established.

### Decision 3: Ingestion Limits & Content Filtering
* **Rule:** Implement strict size and MIME allowlists mirroring the existing Discord adapter standard (`gateway/platforms/base.py` & `plugins/platforms/discord`).
* **Specifications:**
  - **Max File Size:** `20 MB` (20,971,520 bytes). Files exceeding this threshold are rejected with a user-facing size error.
  - **Allowed MIME Types:**
    - Images: `image/png`, `image/jpeg`, `image/webp`, `image/gif`
    - Audio (Voice Notes): `audio/mp4`, `audio/m4a`, `audio/mpeg`, `audio/ogg`, `audio/wav`
    - Documents: `application/pdf`, `text/plain`, `text/markdown`, `text/csv`
  - **Explicitly Forbidden:** Executable binaries, scripts, and macro-enabled files (`.exe`, `.sh`, `.bat`, `.py`, `.js`, `.xlsm`, `.docm`).

### Decision 4: Privacy & PII Redaction Pre-Processing
* **Rule:** Ingested document text must be routed through `privacy.redact_pii` filtering before insertion into the LLM prompt context whenever PII redaction is active or when files contain sensitive numerical patterns (SSN, credit card, bank routing markers).

---

## 4. Implementation Roadmap & Technical Architecture

### 4.1 Slack App Manifest Update
Update the manifest definition in `hermes_cli/slack_cli.py`:
```yaml
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - channels:history
      - channels:join
      - channels:read
      - chat:write
      - commands
      - files:read       # <-- Added in Phase 1
      # files:write      # <-- Deferred to Phase 2
      - groups:history
      - groups:read
      - im:history
      - im:read
      - im:write
      - mpim:history
      - mpim:read
      - reactions:read
      - users:read
```

### 4.2 Configuration Schema (`config.yaml`)
```yaml
slack:
  enabled: true
  allowed_channels:
    - C0BFXBH5Z8Q  # #hermes-home
    - C0AC2H0J85B  # #dev
    - C0BMT1GQ2FK  # #daily-brief
    - C0AC3U200F8  # #general
    # ... other allowed channels
  attachments:
    enabled: true
    max_attachment_bytes: 20971520  # 20MB
    allowed_channels:
      - C0BFXBH5Z8Q  # #hermes-home
      - C0AC2H0J85B  # #dev
      - C0BMT1GQ2FK  # #daily-brief
      - C0AC3U200F8  # #general
    denied_channels:
      - C0BCT6NA64F  # #accounting (STRICT DENY)
      - C0AECAHJR1N  # #ust-legal-hhs (STRICT DENY)
      - C0ACJT93QKB  # #ust-general (STRICT DENY)
      - C0ACNQ8FEHX  # #security (STRICT DENY)
```

### 4.3 Coordinated Token Rotation & Reinstall Sequence
When applying the manifest change in the Slack Developer Portal:
1. **Pre-Notification:** Post a 5-minute maintenance notification to `#open-claude-health`.
2. **Reinstall App:** Click **Reinstall to Workspace** on `api.slack.com/apps`.
3. **Capture New Token:** Copy the freshly generated `xoxb-...` Bot User OAuth Token.
4. **Update Secret Stores:**
   - Update 1Password item `Slack Bot Token (Hermes)`.
   - Update `~/.hermes/.env` on MacBook Pro.
   - Update `~/.hermes/.env` on `mfc1`.
5. **Restart Gateway Services:** Restart `hermes-gateway` on production nodes.
6. **Verify Health Alerting:** Send a verification ping to `#open-claude-health`.

---

## 5. Verification & Acceptance Criteria

- [x] **Blast Radius Evaluated:** Privileged channels `#accounting`, `#ust-legal-hhs`, `#ust-general`, `#security` identified and documented.
- [x] **Isolation Architecture Defined:** Dual-gate design (config allowlist + hardcoded denylist) specified.
- [x] **Scope Phasing Decided:** `files:read` prioritized; `files:write` deferred.
- [x] **Ingest Guardrails Specified:** 20MB size ceiling and strict MIME allowlist documented.
- [x] **Token Rotation Plan Established:** Downtime-mitigated deployment sequence established.

---
*(End of Decision Record)*
