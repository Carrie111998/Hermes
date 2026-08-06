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
| commit           | `6122d85ec`, scoped to `deepseek-v4-flash` in a follow-up (2026-08-03)                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| file:line        | `agent/agent_init.py:829-855`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| what             | `model.temperature` / `model.top_p` from `config.yaml` land in `agent.request_overrides` via `setdefault` (an explicit per-turn override still wins), **only when the bare model name is `deepseek-v4-flash`** — the model those values were tuned for. Every other model keeps the provider default. The overrides channel already exists upstream and lands in the request body (`agent/transports/chat_completions.py`: `api_kwargs.update(overrides)`). |
| why it exists    | Hermes had no user-facing sampling knob: temperature only came from the hardcoded `_fixed_temperature_for_model()` table, and `top_p` was never emitted at all. Captured on the wire before the change: `{model, reasoning_effort, stream, tools, messages}` — no temperature, no top_p; the gateway decided.                                                                                                                                                                                                 |
| why it is scoped | The first version emitted the knobs for every model. The Codex Responses adapter validates against a strict allowlist holding `temperature` but not `top_p` (`agent/codex_responses_adapter.py:1029`), so every `gpt-5.6-luna` request raised `ValueError: Codex Responses request has unsupported field(s): top_p.` — killing the fallback provider at the exact moment the primary (`opencode-go`) was down. Scoping by model also removes the need for the `OMIT_TEMPERATURE` special case: Kimi is simply out of scope. |
| test             | `tests/local/test_local_patches.py::TestSamplingOverrides` (config → request_overrides; turn override wins; `<provider>/<model>` shape still matches; Codex and Kimi models get neither key — the last two FAIL with the gate reverted) + `TestSamplingWire` (transport passes overrides into api_kwargs; the Codex allowlist still rejects `top_p`, which is the reason the gate exists).                                                                                                                    |
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

## 3b. `KANBAN_GUIDANCE` — code workers complete, they do not block for review (ACTIVE — tested)

|                  |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| file:line        | `agent/prompt_builder.py:246-251` (inside `KANBAN_GUIDANCE`, step 5)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| what             | Upstream's step 5 carried an exception: a code worker should drop its metadata into a comment and finish with `kanban_block(reason="review-required: …")` "so a reviewer can approve+unblock". Replaced with the opposite instruction — a code change completes, and review is the downstream card.                                                                                                                                                                                                                                                                                                                                                                                    |
| why it exists    | The premise is false in this setup, twice over. **No task worker can unblock**: `_check_kanban_orchestrator_mode()` (`tools/kanban_tools.py:109-121`) returns `False` whenever `HERMES_KANBAN_TASK` is set, so `kanban_unblock` is not in the schema. And a review block is **sticky**: `recompute_ready()` (`hermes_cli/kanban_db.py:4177`) explicitly refuses to auto-recover it, so the card waits for a human — who, on unblocking, re-runs the worker rather than approving it. Observed 2026-08-06: the `implementer` did correct TDD work (RED, GREEN, mutation check), then blocked, and the three-card smoke graph stalled at node one. The six profiles' `.hermes.md` said to complete; the injected guidance won, being more specific and coming from the harness itself. |
| test             | `tests/local/test_local_patches.py::TestKanbanGuidanceCompletes` — the banned strings are absent, the positive instruction is present, and `_check_kanban_orchestrator_mode()` still hides board routing from workers (the premise the patch rests on). Restoring upstream's sentence turns the first two red.                                                                                                                                                                                                                                                                                                                                                                          |
| why not upstream | Upstream's wording is right for a deployment with a woken orchestrator or a human at the board. Patrick's graph runs unattended overnight, where a sticky block is a stall nobody sees until morning. Candidate for an upstream issue, not a PR — the fix depends on the deployment.                                                                                                                                                                                                                                                                                                                                                                                                    |

## 3c. Vault routers injected as their own context slot (ACTIVE — tested)

|                  |                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| file:line        | `agent/prompt_builder.py` (`build_vault_routes_prompt`), `agent/system_prompt.py` (call site after the context files), `run_agent.py` (re-export)                                                                                                                                                                                                                                                                                   |
| what             | `wiki/_active.md` + `wiki/_index.md` are injected as their own slot at the end of the context tier, gated on `vault.routes_dir` in `config.yaml`. ~1,670 tokens. The nine per-category `_context.md` files stay out (~8,800 tokens) — the layering exists so a category is opened only when a route points at it.                                                                                                                    |
| why it exists    | `.hermes.md` §8 claimed the vault index "arrives injected" while nothing injected it. The claim outlived the code that would have made it true, and `wiki/_active.md` says the same thing about itself. Either the docs were wrong or the feature was missing; this makes the docs right.                                                                                                                                            |
| why the ordering | The ingest cron rewrites both routers. Placed ahead of the context files, a regenerated index would invalidate the cached prefix of a `SOUL.md` / `.hermes.md` that did not change.                                                                                                                                                                                                                                                 |
| why opt-in       | Per-profile config, absent by default: the six worker profiles never follow vault routes and should not pay ~1,670 tokens a turn for them. Verified zero for all six.                                                                                                                                                                                                                                                              |
| test             | `tests/local/test_local_patches.py::TestVaultRoutesInjection` — absent config injects nothing; both routers load in reading order while a category `_context.md` stays out; an oversized router is truncated; `run_agent` still re-exports the builder; the call site stays after the context files.                                                                                                                                 |
| gotcha           | `system_prompt.py` reaches the builder through `_ra()` (the `run_agent` module), not `prompt_builder` directly. Adding the function without the re-export made every session die with `module 'run_agent' has no attribute 'build_vault_routes_prompt'` — while the unit tests, which called it by its real name, all passed. The smoke test caught it; `test_reachable_through_the_call_path_system_prompt_uses` now does.        |
| why not upstream | Patrick-specific: a vault layout with router files is his convention, not a Hermes feature.                                                                                                                                                                                                                                                                                                                                         |

## 4. Hindsight client pin + per-operation reasoning effort (ACTIVE)

|                  |                                                                                                                                                                                                                                                                                                                                        |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| commit           | `61e1c669d` + per-op effort follow-up                                                                                                                                                                                                                                                                                                  |
| file:line        | `plugins/memory/hindsight/__init__.py` (`_build_embedded_profile_env`), `pyproject.toml:174`, `tools/lazy_deps.py`, `uv.lock`                                                                                                                                                                                                          |
| what             | `hindsight-client` pinned to `0.8.6` (matches the running daemon API), and `_build_embedded_profile_env` propagates `llm_reasoning_effort` plus the per-operation keys (`retain_`, `reflect_`, `consolidation_`) to `HINDSIGHT_API_*_LLM_REASONING_EFFORT`, so `config.json` reaches the daemon and survives `.env` re-materialization. |
| why it exists    | Upstream maps only 6 fixed keys, so effort never reached the daemon. The pin exists because the client must match the daemon's API version.                                                                                                                                                                                            |
| test             | NONE. Gap.                                                                                                                                                                                                                                                                                                                             |
| why not upstream | The pin is local (upstream quarantines new releases for 14 days via `exclude-newer`). The effort passthrough is a genuine upstream gap.                                                                                                                                                                                                |

**`uv.lock` caveat:** the `pyproject.toml` pin alone does NOTHING — `uv sync --frozen`
obeys the lock, not the pyproject, and the upstream lock pinned `0.6.1`. The lock was
regenerated with `uv lock --exclude-newer-package hindsight-client=false`. A rebase that
takes the upstream `uv.lock` silently reverts the client to `0.6.1`; re-run that command.

## 5. `uv sync` UNINSTALLS the Hindsight daemon (operational trap — not a patch)

`hindsight-all` (which provides the `hindsight-api` daemon, plus `torch` and
`sentence-transformers` for the local embedding/reranker models) is NOT in
`pyproject.toml`. The plugin installs it at runtime into the live venv
(`install_specs` → `sys.executable`, `plugins/memory/hindsight/__init__.py:1005`).

`uv sync --frozen` removes everything absent from the lock — so **step 7 of the update
skill uninstalls the daemon every time**. It is not noticed immediately: a running
daemon keeps serving from memory, and only fails to come back on the next restart,
taking Hermes AND OpenCode memory down with it (both use the same bank).

After every `uv sync`, reinstall:

```bash
uv pip install --python venv/bin/python "hindsight-all==0.8.6" "hindsight-client==0.8.6" \
  --exclude-newer-package hindsight-all=false \
  --exclude-newer-package hindsight-api=false \
  --exclude-newer-package hindsight-api-slim=false \
  --exclude-newer-package hindsight-client=false
```

Verify with `./venv/bin/python -c "import hindsight_api"` — if it raises
`ModuleNotFoundError`, the daemon is a zombie and will not restart.

## 6. Global context FALLBACK (SUPERSEDED — do not resurrect)

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
