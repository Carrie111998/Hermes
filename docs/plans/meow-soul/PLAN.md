# MEOW-SOUL Consolidation & Hermes Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate the four duplicated `meow-soul/` source files into one canonical, conflict-free persona bundle and make 橘宝 the default Hermes personality on this machine — plain `hermes` (no `-p` flag) acts like the MEOW AI assistant, with all discipline + work mode self-contained in a single loadable SOUL.md.

**Architecture:** Replace the 4 ad-hoc files with a `canonical/` bundle. Deploy the loadable parts (SOUL/USER/MEMORY) to the default home `~/.hermes/` so plain `hermes` *is* 橘宝 machine-wide — no profile flag, no isolation. SOUL.md is fully self-contained: all disciplinary tables and the 5-sub-agent work mode are inlined, so the model sees everything in one file with zero external dependencies. DISCIPLINE.md / AGENTS.md stay in the repo bundle as human-reference mirrors only. The repo's root `AGENTS.md` and `data/SOUL.md` are never touched.

**Revised direction (user follow-up, 2026-07-29):**
- **Deploy target: default home `~/.hermes/`** (Option A) — plain `hermes` is 橘宝, no `-p meow`. The earlier profile-isolated deploy was removed.
- **Sub-agents: fold into SOUL.md** as a `## 工作模式` section — travels with the persona, not shadowed by project AGENTS.md.
- **DISCIPLINE: inline all rules into SOUL.md** — self-contained, zero external-file dependency.
- **Voice: first-person ("我是橘宝…")** throughout.
- **Keep 语音系统/软件交互控制** + AI语音 sub-agent 5.

**Tech Stack:** Markdown (Hermes context files), Hermes profile system (`HERMES_HOME` scoping), `hermes profile create` / `hermes memory` CLI. No Python changes.

## Global Constraints

- **Never overwrite** the repo's root `AGENTS.md` or `data/SOUL.md`. The meow persona deploys under a profile directory only.
- **First-person voice** throughout SOUL.md ("我是橘宝…" / "我是一只进化的橘子猫…") per user decision — keep 我/你 consistent, no mixing.
- **Keep the 语音系统/软件交互控制 capability** and AI语音 sub-agent 5 — only present in the `172321` snapshot; do not regress.
- **Preserve contributor intent:** the `172321` composite is the base; merge, don't rewrite from scratch.
- **`[待补充]` placeholders:** leave genuinely-unknown user-profile fields (姓名/称呼偏好) as `[待补充]` — those are filled at runtime by the bootstrap; resolve the structural ones (长期记忆存储位置, 冲突解决) with concrete Hermes-accurate values.
- **Reference docs are not auto-loaded:** IDENTITY.md, TOOLS.md, BOOTSTRAP.md, README.md, HEARTBEAT.md, DISCIPLINE.md are for humans/onboarding; only SOUL.md, USER.md, MEMORY.md, AGENTS.md are loaded by Hermes.

---

## File Structure

### Create under `docs/plans/meow-soul/canonical/`
| File | Responsibility | Hermes-loaded? |
|---|---|---|
| `SOUL.md` | Persona core, second-person, references `DISCIPLINE.md` | ✅ via profile HERMES_HOME |
| `USER.md` | User profile template (runtime-filled) | ✅ memory store |
| `MEMORY.md` | Long-term memory seed + write rules, Hermes-accurate storage | ✅ memory store |
| `AGENTS.md` | Main + 5 sub-agents (code/文案/PPT/图片/语音), dispatch flow | ✅ via profile cwd OR profile AGENTS.md |
| `DISCIPLINE.md` | Consolidated 禁止事项 + 需要确认 + 安全边界 + 权限矩阵 | reference (SOUL cites it) |
| `IDENTITY.md` | Identity card (reference; folded into SOUL for loading) | reference |
| `TOOLS.md` | Tool architecture reference | reference |
| `BOOTSTRAP.md` | First-run onboarding script | reference |
| `README.md` | Bundle overview + deploy steps | reference |
| `HEARTBEAT.md` | Daily/weekly/monthly checklist (cron-friendly) | reference |

### Delete (after canonical is approved)
- `SOUL (1).md`, `SOUL (2).md`, `education-analyst-all-pages-20260707-162901.md`
- Keep `education-analyst-all-pages-20260707-172321.md` as `archive/snapshot-20260707-172321.md` (provenance)

### Deploy (runtime, NOT in repo tree)
- `~/.hermes/profiles/meow/SOUL.md`
- `~/.hermes/profiles/meow/memories/USER.md`
- `~/.hermes/profiles/meow/memories/MEMORY.md`
- `~/.hermes/profiles/meow/AGENTS.md` (if project-scoped dispatch is wanted)

---

## Task 1: Create canonical SOUL.md (first-person, consolidated)

**Files:**
- Create: `docs/plans/meow-soul/canonical/SOUL.md`
- Reference: `education-analyst-all-pages-20260707-172321.md` (base), `SOUL (1).md` (identity variant)

**Interfaces:**
- Produces: the loadable persona; references `DISCIPLINE.md` by relative path.

- [ ] **Step 1:** Write `SOUL.md` merging 172321's SOUL + IDENTITY常用表达, using **first-person throughout** ("我是橘宝…" / "我是一只进化的橘子猫…") per user decision. Resolve `响应长度偏好` → "短答优先，必要时展开". Add a `## 纪律边界` section that says "详见同目录 `DISCIPLINE.md`" and inlines the three highest-signal禁止事项 only.
- [ ] **Step 2:** Verify no `你是…` second-person persona framing remains in SOUL.md (grep).
- [ ] **Step 3:** Commit: `docs(meow-soul): add canonical SOUL.md (first-person, consolidated)`

## Task 2: Create DISCIPLINE.md (single disciplinary source of truth)

**Files:**
- Create: `docs/plans/meow-soul/canonical/DISCIPLINE.md`
- Consumes: 禁止事项/需要确认/安全边界 from SOUL, 权限矩阵 from TOOLS.md (172321)

**Interfaces:**
- Cited by `SOUL.md` `## 纪律边界` and by `AGENTS.md` dispatch rules.

- [ ] **Step 1:** Consolidate into three tables: (a) 禁止事项 (b) 需要确认的操作 (c) 权限矩阵 (工具/权限级别/需确认/使用场景). Add a short "决策原则" block at the end.
- [ ] **Step 2:** Commit: `docs(meow-soul): add DISCIPLINE.md as single disciplinary source`

## Task 3: Create canonical USER.md (resolve placeholders)

**Files:**
- Create: `docs/plans/meow-soul/canonical/USER.md`
- Reference: 172321 USER.md block

- [ ] **Step 1:** Copy 172321 USER.md. Leave 姓名/称呼偏好/建议支持/领域补充 as `[待补充]` (runtime-filled). Keep everything else verbatim.
- [ ] **Step 2:** Commit: `docs(meow-soul): add canonical USER.md`

## Task 4: Create canonical MEMORY.md (Hermes-accurate storage)

**Files:**
- Create: `docs/plans/meow-soul/canonical/MEMORY.md`
- Reference: 172321 MEMORY.md block, repo `cli-config.yaml.example:641` (MEMORY.md/USER.md stores), `hermes_cli/agent_import.py`

**Interfaces:**
- Produces: seed content for `~/.hermes/profiles/meow/memories/MEMORY.md`.

- [ ] **Step 1:** Copy 172321 MEMORY.md. Resolve `长期记忆存储位置` → "`~/.hermes/profiles/meow/memories/MEMORY.md`（Hermes 长期记忆库，跨会话持久）". Resolve `上下文记忆存储位置` → "会话上下文窗口（由 Hermes context_compressor 管理）". Keep write rules + 隐私保护 verbatim.
- [ ] **Step 2:** Commit: `docs(meow-soul): add canonical MEMORY.md with Hermes-accurate storage paths`

## Task 5: Create canonical AGENTS.md (5 sub-agents + dispatch + conflict rule)

**Files:**
- Create: `docs/plans/meow-soul/canonical/AGENTS.md`
- Reference: 172321 AGENTS.md block (includes AI语音 sub-agent 5)

**Interfaces:**
- Cites `DISCIPLINE.md` for conflict resolution.

- [ ] **Step 1:** Copy 172321 AGENTS.md including all 5 sub-agents. Resolve `冲突解决` → "主 Agent 按优先级 `本地安全 > 合规 > 人设 > 实用 > 趣味` 裁决；跨子 Agent 输出冲突时以 `DISCIPLINE.md` 权限矩阵为准，主 Agent 拥有最终整合权".
- [ ] **Step 2:** Add a header note: "本文件为 MEOW profile 专属，不替代仓库根 `AGENTS.md` 开发指南。"
- [ ] **Step 3:** Commit: `docs(meow-soul): add canonical AGENTS.md with 5 sub-agents + conflict rule`

## Task 6: Create reference docs (IDENTITY/TOOLS/BOOTSTRAP/README/HEARTBEAT)

**Files:**
- Create: `docs/plans/meow-soul/canonical/IDENTITY.md`, `TOOLS.md`, `BOOTSTRAP.md`, `README.md`, `HEARTBEAT.md`
- Reference: 172321 blocks; `SOUL.md` for identity folding.

- [ ] **Step 1:** IDENTITY.md — keep as a human-readable identity card; note "personality loads via SOUL.md".
- [ ] **Step 2:** TOOLS.md — copy 172321 TOOLS.md verbatim (already filled in 172321).
- [ ] **Step 3:** BOOTSTRAP.md — copy 172321; fix step numbering (1→2→5 jumps to 5; renumber 2→3, 5→3/4 or document the gap).
- [ ] **Step 4:** README.md — write a real bundle overview: what each file is, which are Hermes-loaded, deploy steps pointing to Task 8.
- [ ] **Step 5:** HEARTBEAT.md — provide concrete checklists (morning: review memory deltas; weekly: prune stale file-index; monthly: re-seed skill bundle).
- [ ] **Step 6:** Commit: `docs(meow-soul): add reference docs (IDENTITY/TOOLS/BOOTSTRAP/README/HEARTBEAT)`

## Task 7: Archive duplicates + snapshot provenance

**Files:**
- Move: `education-analyst-all-pages-20260707-172321.md` → `docs/plans/meow-soul/archive/snapshot-20260707-172321.md`
- Delete: `SOUL (1).md`, `SOUL (2).md`, `education-analyst-all-pages-20260707-162901.md`
- Keep: `REVIEW.md` (already created)

- [ ] **Step 1:** `git mv` the 172321 snapshot to `archive/`.
- [ ] **Step 2:** `git rm` the three superseded files.
- [ ] **Step 3:** Commit: `docs(meow-soul): archive 172321 snapshot, drop superseded duplicates`

## Task 8: Deploy to default home `~/.hermes/` (runtime, APPROVED — Option A)

**Files:** (outside repo tree)
- `~/.hermes/SOUL.md` ← canonical/SOUL.md (self-contained)
- `~/.hermes/memories/USER.md` ← canonical/USER.md
- `~/.hermes/memories/MEMORY.md` ← canonical/MEMORY.md
- **NOT deployed:** AGENTS.md (work mode is inlined in SOUL.md; project AGENTS.md files would shadow it anyway)

- [x] **Step 1:** Removed the earlier mis-targeted `~/.hermes/profiles/meow/` deploy.
- [x] **Step 2:** Created `~/.hermes/memories/` and copied the three canonical files into the default home.
- [x] **Step 3:** Verified all three deployed files are non-empty.
- [ ] **Step 4 (user, once Hermes CLI installed):** `hermes` (no flag) → banner shows 橘宝 persona; `/soul` shows canonical SOUL.md; `/memory` shows USER+MEMORY seeds.
- [ ] **Step 5:** (Optional) run `hermes setup` / configure `config.yaml` + `.env` for provider keys if not already done. No repo commit (runtime state outside tree).

## Verification (after Task 7, before Task 8)

- [ ] `grep -r "我是橘宝" docs/plans/meow-soul/canonical/` → only inside `IDENTITY.md` reference (acceptable), not in `SOUL.md`.
- [ ] `grep -r "\[待补充\]" docs/plans/meow-soul/canonical/SOUL.md docs/plans/meow-soul/canonical/MEMORY.md docs/plans/meow-soul/canonical/AGENTS.md` → only USER-profile runtime fields remain in `USER.md`.
- [ ] `list_dir docs/plans/meow-soul/` shows: `REVIEW.md`, `PLAN.md`, `canonical/`, `archive/` — no `SOUL (N).md`, no `education-analyst-*` at top level.
- [ ] `DISCIPLINE.md` contains every rule that was previously scattered across SOUL 禁止事项 + 需要确认 + 安全边界 + TOOLS 权限矩阵.