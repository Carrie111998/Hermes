# Collective Wisdom (HSP M3) — Agent-side reader's guide

This directory carries the **Collective Wisdom** design docs. The full proposal is framed for the
**sync team** (it lives as a PR on `gateway-gateway`); this README orients **hermes-agent reviewers**
to the parts that land in *this* repo.

- **`IMPLEMENTATION_PLAN.md`** — the cross-repo plan (hermes-agent + nous-account-service + gateway-gateway).
- **`M3_COLLECTIVE_WISDOM_PROPOSAL.md`** — the sync-team proposal (context; the contract/plane changes).

Companion sync-team PR: **NousResearch/gateway-gateway#211**.

## Why the agent team is on this

Collective Wisdom is effectively **HSP Milestone 3**, layered on the existing M2 org-shared-skills spine.
The plane/registry parts are mostly *reuse*. **The largest net-new surface is agent-side**, and it's the one
milestone (**M3-A**) that has **no plane or wire-contract dependency** — it's fully buildable and testable in
this repo today.

## What lands in hermes-agent (please review these)

All new work sits behind a new `hermes wisdom` command group and an `agent/wisdom/` package, built on top of
primitives that already exist here:

| CW need | Extends / reuses (this repo) | Net-new |
|---|---|---|
| Windowed usage signals (30-day use, 7 consecutive days, distinct days) | `tools/skill_usage.py` (`.usage.json`; today only lifetime `use_count`/`patch_count`) | bounded **invocation event log** + per-skill **content hash** |
| "Meaningful refinement" count (not formatting/metadata edits) | `bump_patch()` in `tools/skill_usage.py`; aux client in `agent/curator.py` | structural-diff classifier + aux-LLM tiebreak |
| Candidate detection (refinement + high-usage paths, dedup/reproposal) | event hooks off `skill_manage`; one-shot **stability check** via `cron/` | `agent/wisdom/candidate_engine.py` |
| Proposal explanation via user's default LLM (respect `OrgModelPolicy`, record model, no silent substitution) | `agent/curator.py` aux pattern; `hermes_cli/nous_account.py` | proposal generation |
| System Specification metadata (§8.8) + sensitive-content scan (hard-blocks, not just advisory) | superset of the plane's `extractCapabilities()` | sys-spec extractor + scanner |
| In-agent owner review — **full raw contents verbatim**, approval **bound to content hash** | `hermes_cli/subcommands/*` (`add_parser` convention) | `hermes_cli/subcommands/wisdom.py` |
| Consumer install / update / compat preflight | `tools/skills_hub.py` `SkillSource` ABC (install/lockfile/quarantine) | `CollectiveWisdomSource` + `hermes wisdom install\|check\|update\|versions\|uninstall` |

See **§5 of the proposal** (agent-side work) and **the Stage A / M3-A slices** in the implementation plan
for the detailed breakdown, and **§7** for the open questions.

## Specific things worth an agent-team opinion

1. **Telemetry shape** — is a bounded invocation ring in `.usage.json` the right home for windowed counts, or
   should this move to a dedicated store? (`tools/skill_usage.py` is the current single source.)
2. **Meaningful-refinement classifier cost** — structural-diff-only vs. always paying an aux-LLM call per edit.
3. **Candidate-engine trigger placement** — hooking `skill_manage` patch/edit + usage-threshold crossing,
   event-driven (the spec is explicit it is **not** a recurring full scan).
4. **Owner-review surface parity** — the in-agent flow must be an equivalent consent surface to the portal page
   (raw contents verbatim, hash-bound approval). Does the existing review/approval plumbing cover this?

**Docs-only. No code, no runtime change.**
