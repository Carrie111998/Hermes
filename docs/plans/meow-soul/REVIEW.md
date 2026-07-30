# meow-soul Source Review

> Status: read-only audit, no files changed. Output of the verify/compare phase.

## Inventory

| File | Type | Scope | Verdict |
|---|---|---|---|
| `SOUL (1).md` | standalone | SOUL only | duplicate of `SOUL (2).md` with one-line identity variant |
| `SOUL (2).md` | standalone | SOUL only | duplicate; identity line matches the composite dumps |
| `education-analyst-all-pages-20260707-162901.md` | composite (6 files) | SOUL+IDENTITY+USER+AGENTS+MEMORY+TOOLS | older snapshot, missing 语音 capability + BOOTSTRAP/README/HEARTBEAT |
| `education-analyst-all-pages-20260707-172321.md` | composite (9 files) | +BOOTSTRAP+README+HEARTBEAT | **canonical base** — strict superset of 162901 |

## Conflicts

1. **Identity voice (`我是` vs `你是`).** Composite + `SOUL (2).md` say "我是橘宝…"; `SOUL (1).md` says "你是七彩虹COLORFIRE品牌形象橘宝…"; all 角色定位 blocks use "你是". Hermes' `default_soul.py` convention is second-person ("You are…").
   - **Resolution:** standardize on second-person throughout SOUL.md to match Hermes' SOUL convention and the existing 角色定位 wording.

2. **Capability drift.** 172321 adds 语音系统/软件交互控制 + AI语音 sub-agent 5; 162901 and both `SOUL (N).md` lack it. 172321 also fills `响应长度偏好` and `工具系统架构`.
   - **Resolution:** 172321 wins; keep the voice capability.

3. **Scattered disciplinary rules.** 禁止事项 (SOUL), 需要确认的操作 + 安全边界 (SOUL), and the TOOLS permission matrix each hold pieces. No single source of truth.
   - **Resolution:** add a consolidated `DISCIPLINE.md` and have SOUL.md reference it.

## Unresolved placeholders (`[待补充]`)

- IDENTITY: 补充说明, 同理心, 直接度
- USER: 姓名, 称呼偏好, 建议支持, 领域补充
- MEMORY: 长期记忆存储位置
- AGENTS: 冲突解决
- README.md body, HEARTBEAT.md body (entire)

## Canonical Hermes targets

| meow-soul file | Hermes canonical location | Loaded by |
|---|---|---|
| `SOUL.md` | `data/SOUL.md` (runtime HERMES_HOME) or `~/.hermes/SOUL.md` | `agent/agent_init.py` context-file injection; `default_soul.py` seeds default |
| `IDENTITY.md` | fold into `SOUL.md` (Hermes has no separate IDENTITY.md loader) | — |
| `USER.md` | `~/.hermes/memories/USER.md` | memory store, re-bound per session |
| `MEMORY.md` | `~/.hermes/memories/MEMORY.md` | memory store |
| `AGENTS.md` | cwd `AGENTS.md` (project-level) — the repo already has a root `AGENTS.md`, so this must NOT overwrite it; deploy as a profile-scoped `AGENTS.md` under a MEOW profile | context-file injection |
| `TOOLS.md` | reference doc only (Hermes has no TOOLS.md loader) | — |
| `BOOTSTRAP.md` | reference doc / onboarding script | — |
| `README.md` | reference doc | — |
| `HEARTBEAT.md` | reference doc / cron-friendly checklist | — |
| `DISCIPLINE.md` (new) | reference doc; SOUL.md cites it | — |

## Repo constraints (must respect)

- `data/SOUL.md` does **not exist** in this checkout (no `data/` dir), so there is no fork-maintenance-protected runtime SOUL to preserve. The repo root `AGENTS.md` is the dev guide and **must not be touched** by the persona.
- The machine's `~/.hermes/` had **no pre-existing default persona** (no SOUL.md, no memories/, no config.yaml) — only an empty `profiles/` dir created during the earlier (now-reverted) profile deploy. So default-home adoption (Option A) overwrites nothing.
- Hermes loads SOUL.md, MEMORY.md, USER.md, AGENTS.md, CLAUDE.md, .cursorrules from cwd / HERMES_HOME. IDENTITY.md, TOOLS.md, BOOTSTRAP.md, README.md, HEARTBEAT.md are **not** auto-loaded — they are reference/onboarding docs.

## Revised deployment direction (user follow-up, 2026-07-29)

- **Target: default home `~/.hermes/`** (Option A). Plain `hermes` (no flag) is 橘宝 machine-wide. The earlier profile-isolated deploy under `~/.hermes/profiles/meow/` was removed.
- **SOUL.md is self-contained:** all disciplinary tables + the 5-sub-agent work mode are inlined. `DISCIPLINE.md` and `AGENTS.md` remain in the repo `canonical/` bundle as human-reference mirrors only (not deployed, not auto-loaded).
- **Memory** (`~/.hermes/memories/MEMORY.md` + `USER.md`) is the global store; 橘宝's seed starts populated and grows with real usage.