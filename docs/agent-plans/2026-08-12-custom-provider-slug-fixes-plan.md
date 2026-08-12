# Fix the hermes client's custom-provider save/test paths (hermes-agent fork)

## Context

Two live failures in the Hermes desktop client against the deployed lab agent,
both traced to source:

1. **`agent init failed: Unknown provider 'custom:custom'`** — the desktop's
   provider rows use namespaced slugs (`"custom:" + name`,
   `hermes_cli/providers.py:658`). Saving provider settings sends that slug to
   `POST /api/model/set`. The **main** branch canonicalizes it only against
   declared `providers:`/`custom_providers:` entries
   (`_normalize_main_model_assignment` → `web_server.py:1230-1300`) — the
   deployed box declares none (Agent Command writes a *bare*
   `model.provider: custom` + base_url/api_key, hermes-deploy-manager.sh:4930)
   — so it falls through **verbatim**. The **auxiliary** branch
   (`_apply_model_assignment_sync`, `web_server.py:~6440+`) does **no
   normalization at all** and, with no `task` given, writes `provider:
   custom:custom` into *every* aux slot (it also pops `base_url` and clears
   slot credentials because `new_provider != "custom"`). At init the credential
   resolver (`hermes_cli/auth.py:1812-1827`) knows `custom`, aliases and
   registry names — not `custom:custom` — and raises; the gateway surfaces it
   as `agent init failed:` (`tui_gateway/server.py:1706`).
   Diagnostic output from the live box confirms: main model block healthy
   (`provider: custom`, env-placeholder key), no `custom_providers:` — the bad
   string lives in the aux slots / elsewhere in `~/.hermes`.

2. **"key was rejected by the provider" on Test** — `POST
   /api/providers/validate` probes by env-var name against a fixed table:
   `OPENAI_API_KEY → https://api.openai.com/v1/models`
   (`web_server.py:6932-6937`). The custom endpoint's key is stored under
   `OPENAI_API_KEY`, so Test sends the lab's vLLM key to **api.openai.com** →
   401 → false rejection. The base_url-aware branch exists only for the
   `OPENAI_BASE_URL` variable (`web_server.py:7261-7273`).

All fixes land in the **hermes-agent fork**. Per the fork's AGENTS.md: PR to
`ForgeGuard/hermes-agent:main` only (never upstream), and step 0 is saving
this plan to `docs/agent-plans/2026-08-12-custom-provider-slug-fixes-plan.md`
(checkbox-tracked).

## Fixes

**F1 — one shared slug canonicalizer** (new helper in
`hermes_cli/providers.py`, next to the slug builder at :658):
`canonicalize_provider_slug(provider, cfg) -> str`:
- resolve `custom:<name>` via the existing `resolve_user_provider` /
  `resolve_custom_provider` (as `web_server.py:1230-1300` does) → durable id;
- unresolvable `custom:<anything>` → bare `"custom"` (matches model_switch's
  existing bare-custom affordance, `_bare_custom_provider_def`,
  `model_switch.py:1022`) — never persist a string the resolver can't load;
- anything else returned unchanged.

**F2 — `/api/model/set` main branch**: `_normalize_main_model_assignment`
falls back through F1 instead of returning the unresolved slug verbatim.

**F3 — `/api/model/set` auxiliary branch**: run `provider` through F1 before
`slot_cfg["provider"] = provider`. With the slug canonicalized to `custom`,
the existing `new_provider != "custom"` guard also stops wrongly clearing the
slot's base_url/credentials.

**F4 — resolver defense in `hermes_cli/auth.py` (~1812)**: before raising
`Unknown provider`, attempt the F1 resolution for `custom:*` strings; if it
yields `custom` (or a resolvable id), proceed instead of failing init. A
config written by a UI must degrade, not brick startup. Keep the raise for
genuinely unknown providers.

**F5 — `/api/providers/validate` custom-aware key probe**: when the key var is
`OPENAI_API_KEY` **and** a custom base_url is in play (request carries one, or
the active `model.provider` is `custom`/a custom endpoint with a configured
`base_url`), probe `<base_url>/models` with the bearer — the same shape as the
existing `OPENAI_BASE_URL` branch — instead of api.openai.com. Response
contract unchanged (401/403 → rejected; success/429 → ok; network error →
reachable=False).

**F6 — tests** (pytest, beside the existing suites in `tests/hermes_cli/`):
canonicalizer table (namespaced→durable, unresolvable→custom, non-custom
untouched); model/set main + aux against a bare-custom config (asserting what
lands in `cfg`); auth resolver accepts `custom:custom` with a configured
base_url and still rejects `definitely-not-a-provider`; validate picks the
custom URL vs the table URL (monkeypatched httpx).

**F7 — version bump 0.19.3** following the 0.19.2 pattern (`6e893d973`):
`hermes_cli/__init__.py` (+release date), `pyproject.toml`,
`acp_registry/agent.json`, `apps/desktop/package.json`, `package-lock.json`,
`uv.lock` — the box images build `runtime-latest` from this fork, and the
maintenance pass moves on version-visible changes.

## Delivery

Branch `fix/custom-provider-slug-handling` off the fork's `main`; commit(s)
with the fork's conventional style; PR to `ForgeGuard/hermes-agent:main`
(gh is authenticated for this repo); merge on green per standing instruction.
NO upstream PR — hard fork policy.

## Verification

- `pytest tests/hermes_cli -q` (the resolver/credential suites the 0.19.2 fix
  pinned at 169+ tests) plus the new tests; `python -m compileall` sanity on
  touched files if the repo has no broader gate.
- **Live recovery + proof (operator-run, on the agent host):**
  1. Locate the damage:
     `docker exec -u hermes -e HOME=/opt/data template-agent sh -lc 'grep -rn "custom:custom" $HOME/.hermes --include="*.yaml" --include="*.json" | cut -c1-120'`
     — expect hits under `auxiliary:` in config.yaml.
  2. Recover now (no code needed): console → Operations → **Refresh config**
     (rewrites config.yaml wholesale from the profile, wiping the aux damage);
     reconnect the client — init error gone.
  3. After the fix ships (image rebuild + `make maintenance-images` moves
     `runtime-latest`): re-save the provider in the desktop settings → config
     keeps `provider: custom` in main AND aux; Test button → passes against
     the vLLM front; `hermes doctor` in-box reports no provider issues.
