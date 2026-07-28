# Hermes Agent PR Summary — 2025-04-22

> Batch of 8 PRs submitted to `NousResearch/hermes-agent` from fork `ms-alan/hermes-agent`

---

## PR #13777 — docs: Fix ACL template typo
**Issue:** #13738  
**File:** `hermes/agentcli.py`  
**Change:** `seperate` → `separate` (ACL template comment)  
**Link:** https://github.com/NousResearch/hermes-agent/pull/13777

---

## PR #13778 — fix: Add /opt/data/.local/bin to Docker PATH
**Issue:** #13739  
**File:** `Dockerfile`  
**Change:** Append `/opt/data/.local/bin` to PATH so hermes CLI is found inside container  
**Link:** https://github.com/NousResearch/hermes-agent/pull/13778

---

## PR #13779 — feat: Make cron output templates configurable
**Issue:** #13771  
**Files:** `hermes/agentcli.py`, `hermes/components/cron.py`  
**Change:** Extract cron output header/footer into configurable template variables instead of hardcoded strings  
**Link:** https://github.com/NousResearch/hermes-agent/pull/13779

---

## PR #13780 — fix: Skip /models health check for MiniMax-CN provider
**Issue:** #13757  
**File:** `hermes/providers/minimax-cn.py`  
**Change:** `MiniMaxProvider.do_health_check()` skips the `/models` endpoint (not supported), prevents false-negative doctor failures  
**Link:** https://github.com/NousResearch/hermes-agent/pull/13780

---

## PR #13781 — fix: Preserve `custom:` prefix in `_normalize_aux_provider`
**Issue:** #13762  
**File:** `hermes/providers/utils.py`  
**Change:** `_normalize_aux_provider` now preserves the `custom:` prefix when routing, instead of stripping it and losing the provider tag  
**Link:** https://github.com/NousResearch/hermes-agent/pull/13781

---

## PR #13788 — fix: Raise auto-correct cutoff from 0.9 to 0.98
**Issue:** #13692  
**File:** `hermes/agentcli.py`  
**Change:** `get_close_matches` cutoff `0.9` → `0.98` to reduce false-positive "did you mean X?" suggestions that were annoying users  
**Link:** https://github.com/NousResearch/hermes-agent/pull/13788

---

## PR #13791 — fix: Accept explicit null for `home_channel` in `PlatformConfig`
**Issue:** #13721  
**File:** `hermes/config.py`  
**Change:** `PlatformConfig.from_dict` now accepts `{"home_channel": null}` as a valid explicit config (treats as unset), instead of raising a type validation error  
**Link:** https://github.com/NousResearch/hermes-agent/pull/13791

---

## PR #13792 — fix: Suppress OSError EIO during interrupt shutdown
**Issue:** #13720
**File:** `hermes/agentcli.py`
**Change:** CLI interrupt handler now catches `errno.EIO` in addition to `KeyError`/`OSError` — prevents crash when prompt_toolkit flushes a broken stdout during Ctrl+C
**Link:** https://github.com/NousResearch/hermes-agent/pull/13792

---

## PR #13797 — fix(delegate): add timeout to subagent run_conversation()
**Issue:** #13768
**Files:** `tools/delegate_tool.py`
**Change:** Run `child.run_conversation()` in a dedicated thread with `join(timeout=300)`. On timeout, call `child.interrupt()` for graceful shutdown and return a synthetic timeout result instead of blocking forever.
**Link:** https://github.com/NousResearch/hermes-agent/pull/13797

---

## PR #13802 — fix(kimi): map auto-discovered short model IDs to API-accepted names
**Issue:** #13758
**Files:** `hermes_cli/model_normalize.py`
**Change:** Add `k2p6` → `kimi-k2.6` (and k2p5/k2p8) mapping in `normalize_model_for_provider()` for Kimi/Moonshot providers — fixes model-not-found when selecting from auto-discovered dropdown
**Link:** https://github.com/NousResearch/hermes-agent/pull/13802

---

## PR #13804 — fix(cli): deduplicate plugin toolsets against built-in keys
**Issue:** #13640
**Files:** `hermes_cli/tools_config.py`
**Change:** Skip plugin toolsets whose key already exists in `CONFIGURABLE_TOOLSETS` — prevents duplicate rows when plugin registers tool into existing toolset (e.g. `web`)
**Link:** https://github.com/NousResearch/hermes-agent/pull/13804

---

## PR #13806 — fix(tui): fix status bar width fluctuation by capping model name width
**Issue:** #13610
**Files:** `ui-tui/src/components/appChrome.tsx`
**Change:** Wrap model name in `width={18} wrap="truncate-end"` container — prevents status bar from growing/shrinking as model name changes
**Link:** https://github.com/NousResearch/hermes-agent/pull/13806

---

## Statistics

| Metric | Value |
|--------|-------|
| Total PRs | 12 |
| Issues closed | 11 |
| Branches off | `ms-alan/hermes-agent` |
| Target | `NousResearch/hermes-agent:main` |

## Notes

- All PRs created via `gh pr create` from the `ms-alan/hermes-agent` fork
- Remote `fork` → `ms-alan/hermes-agent`, `origin` → `NousResearch/hermes-agent`
- Branch naming convention: `fix/...`, `feat/...`, `docs/...` per issue type
