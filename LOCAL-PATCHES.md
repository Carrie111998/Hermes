# LOCAL-PATCHES.md — fork-only patches inventory

Everything in this fork (`DoGMaTiiC/hermes-agent`, `origin` = NousResearch) that
does NOT exist upstream. If you are rebasing onto upstream and one of these
files has conflicts or a _clean_ merge, this is the list of behaviors that must
survive. The regression tests in `tests/local/test_local_patches.py` are the
only proof they did — run them after every update.

Local commits (relative to upstream `origin/main`): `git log origin/main..HEAD`.

---

## 1. Global context file ADDS to project context (ACTIVE — tested)

|                  |                                                                                                                                                                                                                                                                                                                                                               |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| commit           | `6fc5f190f`                                                                                                                                                                                                                                                                                                                                                   |
| file:line        | `agent/prompt_builder.py:2195-2228`                                                                                                                                                                                                                                                                                                                           |
| what             | `HERMES_HOME/.hermes.md` (or `AGENTS.md`) is ALWAYS injected into the system prompt, ADDED TO the project context — not a fallback for it. Global goes first so the project can override on conflict. Deduped by resolved path when cwd IS `HERMES_HOME` (`_find_hermes_md` climbs to the git root and would find the same file — see guard at `:2206-2214`). |
| why it exists    | Before: global rules only loaded when the cwd had NO context file of its own. 9 of Patrick's 21 projects have `AGENTS.md`, so the global operating rules silently vanished from the prompt there.                                                                                                                                                             |
| test             | `tests/local/test_local_patches.py::TestGlobalContextAdds` — `test_global_adds_to_project_agents_md` (global + project both present; FAILS with the patch reverted) and `test_global_not_duplicated_when_cwd_is_hermes_home` (count == 1; fails if the dedupe guard regresses while injection survives).                                                      |
| why not upstream | Upstream PR #23331 ("feat(prompt): load AGENTS.md from HERMES_HOME as global operational policy") implements the same idea with tests and a double-injection guard, but is STALLED with label `needs-decision`. If it merges, this divergence disappears (rebase should drop our block or align it with the PR's version).                                    |

## 2. Sampling knobs — model.temperature / model.top_p (ACTIVE — tested)

|                  |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| commit           | `6122d85ec`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| file:line        | `agent/agent_init.py:825-850`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| what             | `model.temperature` / `model.top_p` from `config.yaml` land in `agent.request_overrides` via `setdefault` (an explicit per-turn override still wins). Temperature is SKIPPED for models where `_fixed_temperature_for_model()` returns `OMIT_TEMPERATURE` (Kimi/Moonshot — the server owns temperature; re-adding the field would break that contract). The overrides channel already exists upstream and lands in the request body (`agent/transports/chat_completions.py`: `api_kwargs.update(overrides)`). |
| why it exists    | Hermes had no user-facing sampling knob: temperature only came from the hardcoded `_fixed_temperature_for_model()` table, and `top_p` was never emitted at all. Captured on the wire before the change: `{model, reasoning_effort, stream, tools, messages}` — no temperature, no top_p; the gateway decided.                                                                                                                                                                                                 |
| test             | `tests/local/test_local_patches.py::TestSamplingOverrides` (config → request_overrides; turn override wins; Kimi skips temperature, keeps top_p — all 3 FAIL with the patch reverted) + `TestSamplingWire` (transport passes overrides into api_kwargs; Kimi `OMIT_TEMPERATURE` contract holds end to end).                                                                                                                                                                                                   |
| why not upstream | Genuine upstream gap — no sampling knob exists there. Candidate for its own upstream PR (small, self-contained, no new deps). Do NOT submit from this fork without Patrick's go-ahead.                                                                                                                                                                                                                                                                                                                        |

## 3. `.cursor/rules/*.mdc` NOT auto-loaded (ACTIVE — no test, known gap)

|                  |                                                                                                                                                                |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| commit           | `2111a9425`                                                                                                                                                    |
| file:line        | `agent/prompt_builder.py:2081-2093` (`_load_cursor_rules_enabled`), gate at `:2108`                                                                            |
| what             | Cursor rules are not injected into Hermes' prompt unless `agent.context_cursor_rules: true` in `config.yaml`.                                                  |
| why it exists    | Patrick decision 2026-08-01: Cursor's rule files belong to Cursor; Hermes should not inherit them silently.                                                    |
| test             | NONE. Gap: this patch has no regression test — a clean upstream merge could eat it silently. Add one (same shape as `TestGlobalContextAdds`) when time allows. |
| why not upstream | Local product decision, not an upstream feature request.                                                                                                       |

## 4. Global context FALLBACK (SUPERSEDED — do not resurrect)

|           |                                                                                                                                                                                |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| commit    | `1c793522d`                                                                                                                                                                    |
| file:line | `agent/prompt_builder.py` (block removed by `6fc5f190f`)                                                                                                                       |
| status    | Replaced by patch 1. The fallback-only behavior (global loaded only when cwd has no context file) is DEAD. If a rebase resurrects this block, drop it — patch 1 supersedes it. |

---

## Why these can't be plugins (so nobody tries the plugin route)

- `pre_api_request` is an **observer**: `agent/conversation_loop.py:2190-2230` invokes it and consumes NO return value (telemetry/observability only). It cannot inject or modify the request.
- `pre_llm_call` injects context into the **user message**, not the system prompt: `agent/turn_context.py:321` — `plugin_user_context` is "appended to user message". Both patches change the system-prompt assembly and the init-time request overrides — no hook exists for either. Plugin-ifying them is not possible without upstream hook changes.

## Update procedure (tie-in)

`~/.hermes/skills/devops/hermes-update/SKILL.md` step 8: after every update,
run `./venv/bin/python -m pytest tests/local/test_local_patches.py -q -o 'addopts='`.
The update is only COMPLETE if they pass. A clean merge that eats a patch shows
up here, not as a conflict — that is the point of this directory.
