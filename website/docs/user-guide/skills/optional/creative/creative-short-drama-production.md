---
title: "Short Drama Production — AI short-drama pipeline: cast, outline, art, script (短剧)"
sidebar_label: "Short Drama Production"
description: "AI short-drama pipeline: cast, outline, art, script (短剧)"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Short Drama Production

AI short-drama pipeline: cast, outline, art, script (短剧).

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/creative/short-drama-production` |
| Path | `optional-skills/creative/short-drama-production` |
| Version | `1.0.0` |
| Author | eternityspring (adapted for Hermes Agent) |
| License | Apache-2.0 |
| Platforms | linux, macos, windows |
| Tags | `short-drama`, `screenwriting`, `creative`, `video`, `chinese`, `短剧` |
| Related skills | [`kanban-video-orchestrator`](/docs/user-guide/skills/optional/creative/creative-kanban-video-orchestrator), [`baoyu-comic`](/docs/user-guide/skills/optional/creative/creative-baoyu-comic) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# AI Short-Drama Production Pipeline (AI 短剧制作)

Adapted from [shuohao-skills](https://github.com/eternityspring/shuohao-skills)
(Apache-2.0, upstream commit `04aa3005`) for Hermes Agent. Use when asked to
produce an AI short drama (短剧) from a novel or story: character bibles
(拆角色), adaptation outlines (排大纲), scene/prop art bibles (场景与道具设定),
or structured screenplays with per-line dialogue (写剧本 / 台词 / 场次).

The pipeline is four stages, each with a zero-dependency Node (≥18) CLI that
does deterministic validation — quality gates are enforced by scripts, not by
model judgment. Each stage emits JSON consumed by the next:

| Stage | Reference dir | Output | What it does |
|---|---|---|---|
| ① Characters | `references/novel-characters/` | `cast.json` | Extract + merge a character roster from source text (up to ~960K chars, chunked), profile passes, merge review |
| ② Outline | `references/novel-outline/` | `outline.json` | Volume/episode adaptation outline with hooks, cliffhangers, and 爽点 (payoff beats) per episode |
| ③ Art bible | `references/novel-art/` | `art.json` | Scenes, lighting states, props — the visual ledger later stages reconcile against |
| ④ Script | `references/novel-script/` | `script.json` | Episodes → scenes → beat flow (action beats ⇄ dialogue lines with speaker + tone), deterministic runtime budgeting |

## How to run it

1. Pick the stage the user needs (they compose, but each also works alone).
2. Load `references/<stage>/<stage>.md` — that is the full upstream skill body
   (kept verbatim, zh-CN) with the workflow, pass structure, and gate list.
   The JSON contracts are in `references/<stage>/references/schema.md`.
3. The deterministic tool for each stage is
   `references/<stage>/scripts/<stage>.mjs`. Run it with plain `node` — zero
   npm deps, zero API keys. Every stage supports at minimum:
   - `seed` — pre-fill the next stage's skeleton from the upstream JSON
   - `validate <file.json> [--outline o.json] [--art a.json]` — full check,
     prints violations and exits 1
   - `checkup <file.json>` — quality-gate ✓/✗ summary
   - `render <file.json> [--md|--html]` — review report to stdout
4. Iterate: you write/edit the JSON; the script is the referee. Do not ship a
   stage while `checkup` fails.
5. Worked example: `references/novel-script/examples/渡口-script.json`
   (6 episodes / 9 scenes / 123 dialogue lines) validates clean — use it to
   sanity-check the toolchain and as a schema example.

## Hermes adaptations (read before following upstream text)

- Upstream targets Claude Code / Codex; tool names map directly:
  Read/Write → `read_file`/`write_file`, Bash → `terminal`, Glob →
  `search_files`. `{baseDir}` = the reference dir of the stage you loaded.
- Dense-CJK files can be misdetected as binary by `read_file` — read them via
  `execute_code` with Python `open()` or `cat` through `terminal`.
- Downstream of stage ④ (storyboarding, shot prompts, actual video
  generation) is deliberately out of upstream scope. In Hermes, hand the
  per-scene beats to video generation (FAL families like seedance-2.0 /
  kling-v3, per the `kanban-video-orchestrator` skill) and dialogue lines to
  `text_to_speech` — the script layer's structured dialogue is designed to
  feed TTS line-by-line.
- Outputs are zh-CN-first (durations calibrated to Mandarin speech rate
  ~4.5 chars/s — the validator default). For non-Chinese dramas, keep the
  structure but expect the runtime gates to need a `params.charsPerSecond`
  override in the JSON (see the stage's `references/schema.md`).

## Verification

- `node references/novel-script/scripts/selftest.mjs` → "✓ 125 项自测全部通过"
  (each stage ships a selftest: 200/307/131/125 checks — run the one you use).
- `validate` on your produced JSON exits 0 with a summary line
  (e.g. "✓ 6 集 / 9 场 / 123 句台词全部通过校验").
- Cross-stage: run stage-④ `validate --outline --art` so character refs,
  payoff claims, and scene/lighting/prop ledgers reconcile.

## Pitfalls

- Node ≥18 required (`node --version`); no npm install is ever needed.
- Filenames contain CJK (e.g. `渡口-script.json`) — quote paths in shell.
- `validate` without `--outline`/`--art` silently skips the reconciliation
  gates (it warns) — always pass the upstream files when they exist.
- Dialogue must stay structured (speaker + line + tone as separate entries);
  prose-style dialogue fails the gates by design.
- Upstream license/notice preserved at `references/UPSTREAM-LICENSE` and
  `references/UPSTREAM-NOTICE`.
