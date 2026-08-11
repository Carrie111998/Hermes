# Lazy plugin-platform enumeration on the gateway config path

**Date:** 2026-08-11
**Status:** implemented
**Scope:** `gateway/config.py`, `gateway/platform_registry.py`, `hermes_cli/plugins.py`
(+ one unrelated defect fixed in `tests/gateway/test_feishu.py`)

## Problem

`platform_registry.plugin_entries()` and `all_entries()` call `_resolve_all()`, which
runs every pending deferred loader — importing all ~20 bundled platform adapters.
`gateway/config.py` called those accessors three times, so merely loading gateway
config materialized every platform the process would never use.

This is a production defect, not only a test one: `GET /api/status` awaits
`load_gateway_config()` synchronously.

The deferred-loading machinery already existed and worked per-name (`_resolve`).
Only the iterate-all accessors defeated it.

### The three call sites

| Site | Call | What it actually used |
|---|---|---|
| `config.py:1403` | `plugin_entries()` | `entry.name` only |
| `config.py:1532` | `all_entries()` | `apply_yaml_config_fn`, only for platforms with a YAML block |
| `config.py:2392` | `plugin_entries()` | `is_connected` / `check_fn` / `env_enablement_fn` |

## Design

### 1. Import-free registry introspection

`PlatformRegistry` gains `deferred_names()`, `deferred_env_hints(name)`, and
`loaded_entries()`. None of them resolve anything. `plugin_entries()` and
`all_entries()` keep their eager contract, so `cron/scheduler.py`,
`hermes_cli/status.py`, `web_server.py` and friends are untouched.

### 2. Env hints derived from `plugin.yaml`

`register_deferred()` accepts `env_hints`. `PluginManager._platform_env_hints()`
derives them from the manifest's `requires_env` — exact names plus their leading
prefix (`FEISHU_APP_ID` → `FEISHU_`), plus a prefix from the platform name itself
(`sms` → `SMS_`, since sms declares `TWILIO_*`). Prefixes subsume `optional_env`
for free.

Deliberately a **superset** of what could enable a platform. A false positive
costs one import; a false negative silently stops a platform auto-enabling.

A manifest that declares no `requires_env` (only `raft`) yields `()`, which callers
must treat as "resolve it".

### 3. Per-site gates — narrowest that stays exact

- **Site 1** needs only names → `_platform_registry_names()`. Zero imports, no heuristic.
- **Site 2** already `continue`d when a platform had no YAML block → test that first,
  resolve only a configured platform. Exact, no heuristic.
- **Site 3** the env gate: resolve if already in `config.platforms`, or a hint matches
  a visible env key, or hints are empty, or `HERMES_EAGER_PLATFORM_PLUGINS` is truthy.

### 4. Fail-open everywhere

Hint derivation or gate evaluation raising anything falls open to *resolve*. A broken
gate degrades to the old behaviour, never to a silently-unenabled platform.

## The assumption that was wrong

The gate initially read `os.environ` only. Measurement caught the regression
immediately: enabled platforms went `['telegram', 'whatsapp']` → `[]`.

Plugin `is_connected` hooks resolve credentials through
`hermes_cli.config.get_env_value`, which **falls back to the profile's `.env` file**.
On a normal Hermes install the Telegram and WhatsApp tokens live only there and never
appear in `os.environ`.

`_visible_env_keys()` therefore unions `os.environ` with `load_env()` (memoised on the
.env mtime; `hermes_cli.config` is already imported by `gateway/config.py`, so this
costs no extra import). Parity restored and pinned by
`test_gate_reads_the_profile_dotenv_not_just_os_environ`.

**Lesson:** "configured via env" is not the same as "in `os.environ`". Verify a gate
against real enablement output before trusting it.

## Measurements

Machine timings swing widely with disk cache (the same eager run measured 24.4s cold
and 8.0s warm), so **module count is the stable metric**.

`_apply_env_overrides` + `load_gateway_config`, `HERMES_DISABLE_LAZY_INSTALLS=1`,
Feishu unconfigured:

| | eager | lazy |
|---|---|---|
| `sys.modules` after `_apply_env_overrides` | 1621 | 1032 |
| platforms resolved | 20 (+`wecom_callback`) | 2 (telegram, whatsapp) |
| enabled platforms | `['telegram','whatsapp']` | `['telegram','whatsapp']` (parity) |

18 of 20 platform adapters are no longer imported. `lark_oapi` was already absent from
this path when Feishu is unconfigured — see below.

## Second, separate defect: `tests/gateway/test_feishu.py`

The brief's "10,055 lark_oapi modules" did **not** come from the enumeration path.
`tests/gateway/test_feishu.py` had a module-level `import lark_oapi` inside a
`try/except` purely to set `_HAS_LARK_OAPI`, which is only ever read by
`skipUnless` decorators. That import ran at collection, before any test.

Replaced with `importlib.util.find_spec("lark_oapi") is not None` — equivalent for the
skip gates, and with the SDK installed the tests still **run** rather than skip.

| import `tests.gateway.test_feishu` | before | after |
|---|---|---|
| wall | 23.76s | 2.94s |
| lark modules | 10,055 | 0 |
| total modules | 10,698 | 464 |

Full file: 213 passed, 0 skipped.

## Cross-check against prior work

Agent memory `gateway-config-sweep-two-import-costs` (written earlier the same day)
splits this into **two independent costs**, and warns against parts of this design.
Reconciled:

- **Cost A** — `_resolve_all()` importing all 20 adapters, ~74s cold / 10–15s warm,
  concentrated in dingtalk/telegram/slack. **This is what the present change fixes**,
  and per that memory it is also what actually killed `TestConfigEnvOverrides`.
- **Cost B** — `check_fn()` → `check_feishu_requirements()` → `import lark_oapi`,
  413.9s cold. **Already fixed by an unmerged commit**, `243b230da` on branch
  `claude/ecstatic-poincare-518471`, which adds `PlatformEntry.deps_available_fn`
  (a probe that must not import) and prefers it in the sweep. Not on `main`. That is
  the right fix for the residual below and should be landed separately.
- The memory says *"do not pre-gate plugin resolution on manifest `requires_env`"*,
  citing google_chat. That warning holds for a gate on **exact declared names**; the
  prefix widening used here covers the cited case, and a static audit of all 20
  platforms' enablement functions found zero uncovered env vars. The residual
  scenario it raises — google_chat via Application Default Credentials with
  `GOOGLE_CHAT_SERVICE_ACCOUNT_JSON` unset — cannot auto-enable regardless, because
  `_env_enablement` returns `None` unless `GOOGLE_CHAT_HTTP_EVENTS_URL` or
  (project **and** subscription) is set, all of which match `GOOGLE_`. A YAML-configured
  google_chat is covered by the `config.platforms` branch of the gate.
- Its `importlib.util.find_spec` warning was **directly applicable** and had been
  violated by the first version of the test change — corrected to `PathFinder`.

## Residual risk (not addressed)

`check_feishu_requirements()` answers "is the SDK available?" by fully importing and
binding it. When Feishu *is* configured — as `test_feishu_config_loaded_from_env` does
via `patch.dict` — the enable pass legitimately triggers that import, now measured at
17.38s inside a 30s per-test cap. That passes, but with less headroom than is
comfortable on a cold-disk day.

The fix is **not** to make `check_feishu_requirements()` cheap —
`tests/gateway/test_feishu_lazy_sdk_import.py` asserts it *binds* the SDK. It belongs
at the call site, which is exactly what the unmerged `243b230da`
(`PlatformEntry.deps_available_fn`) does. Landing that branch closes this gap.

## Testing

`tests/gateway/test_platform_registry_lazy.py` (25 tests):

- hints round-trip; `deferred_names()`/`deferred_env_hints()`/`loaded_entries()` provably
  resolve nothing (tripwire loader)
- hint cleanup on `register`/`unregister`
- name enumeration spans loaded + deferred, respects `plugin_only`
- gate: skip on no match / resolve on exact name / resolve on prefix-only / resolve when
  in `config.platforms` / resolve on `.env`-only credential / fail open on empty hints /
  fail open on error / escape hatch
- candidate selection end to end: only the configured platform's loader runs
- manifest hint derivation, including the no-`requires_env` fail-open case
- the manifest-derived deferred key matches the adapter's registered name for every
  bundled platform (checked statically — doing it by import would load every SDK)
- **regression guard:** `load_gateway_config()` with Feishu unconfigured leaves
  `lark_oapi` out of `sys.modules`

## Rollback

`HERMES_EAGER_PLATFORM_PLUGINS=1` restores resolve-everything at site 3 without a revert.
Sites 1 and 2 are unconditionally lazy — both are exact, not heuristic, so there is
nothing to roll back there.
