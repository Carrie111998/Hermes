# macOS Keychain storage for LLM provider API keys

Date: 2026-08-15
Status: proposed

## Problem

`~/.hermes/.env` stores every credential Hermes knows about — including LLM
provider API keys — as plaintext on disk. On macOS, the OS provides a much
better place for this class of secret: the Keychain. The desktop app already
uses `safeStorage` (Keychain-backed) for gateway auth/OAuth tokens
(`apps/desktop/electron/native-token-store.ts`), but provider API keys typed
into Settings still land in plaintext `.env` via `PUT /api/env`.

Goal: on macOS, LLM provider API keys are stored in and read from the
Keychain instead of `.env`, for both the desktop app and the CLI. Other
platforms are unaffected.

## Why this isn't a `secret_sources` plugin

`agent/secret_sources/` (Bitwarden, 1Password, `command`) already integrates
external secret managers, and its own module docstring flags "OS keystores
(Keychain/DPAPI/libsecret)... under discussion" as a candidate future source.
That framework is deliberately **read-only** (`agent/secret_sources/base.py`:
"Sources resolve refs → values. There is no write-back… do not bolt it on"),
built for pulling from a vault the user manages externally via an explicit
`env-var → reference` mapping.

That's not this problem. The user doesn't maintain a Keychain item out of
band and point Hermes at it — they type a key into Settings (or run a CLI
command) and Hermes stores and manages it. That's a **storage backend swap**
for Hermes's own credential store, not an external reference resolver. It
belongs next to the existing store, in `hermes_cli/config.py`.

## Scope

Eligible keys = every env var in the desktop's "API keys" tab, i.e.
`provider_catalog()` entries with `tab == "keys"` (`auth_type in
{"api_key", "aws_sdk"}` — the paste-a-key model providers: Anthropic,
OpenRouter, Groq, Fireworks, DeepInfra, GLM, Kimi, MiniMax, Novita, Ollama,
OpenCode Go/Zen, Upstage, xAI-compatible custom, etc.), **plus** custom
endpoint keys (`HERMES_CUSTOM_<slug>_API_KEY`, from
`custom_endpoint_key_env()`) since they're the same paste-a-key shape, just
user-defined.

Out of scope: platform/tool credentials (Slack, Telegram, GitHub, Exa,
Firecrawl, Browserbase, etc.) — unaffected, stay in `.env`. Non-macOS
keystores (DPAPI, libsecret) — future work, not part of this change.

## Storage backend: `hermes_cli/keychain_store.py`

Thin wrapper around the macOS `security` CLI (present on every Mac — no new
dependency, consistent with how this codebase already shells out to `op`/
`bws` rather than vendoring SDKs):

- `is_available() -> bool` — `sys.platform == "darwin"` and the `security`
  binary resolves on `PATH`.
- `set_secret(account: str, value: str) -> None` —
  `security add-generic-password -U -a <account> -s <service> -w <value>`.
  Raises on non-zero exit.
- `get_secret(account: str) -> Optional[str]` —
  `security find-generic-password -a <account> -s <service> -w`. Exit 44
  ("item not found") maps to `None`; any other non-zero exit raises.
- `delete_secret(account: str) -> bool` —
  `security delete-generic-password -a <account> -s <service>`. Exit 44 maps
  to `False` (nothing to delete); other non-zero exits raise.
- `service` = `f"Hermes:{hermes_home_key()}"` (existing helper from
  `hermes_constants.py`), so multiple `HERMES_HOME` profiles on one machine
  get isolated Keychain items instead of clobbering each other.
- `account` = the env var name (e.g. `ANTHROPIC_API_KEY`).

Subprocess calls follow the same posture as `agent/secret_sources/base.py`'s
`run_secret_cli()`: argv list (never `shell=True`), minimal inherited env,
short timeout, `stdin=DEVNULL`.

## Integration points

All in `hermes_cli/config.py`, gated by `is_available() and key in
_KEYCHAIN_ELIGIBLE_KEYS` (macOS + eligible key; no-op passthrough to today's
behavior otherwise):

- **`save_env_value(key, value)`** — for an eligible key on macOS, write to
  Keychain via `set_secret()` instead of `.env`, then delete any existing
  plaintext line for that key from `.env` (never lets the same key live in
  both places — mirrors the duplicate-line bug class already fixed for the
  `export KEY=` form). **If the Keychain write fails (denied, locked, binary
  missing), the save fails outright** — it does not fall back to writing the
  key in plaintext. The caller (CLI command / `PUT /api/env` handler) surfaces
  that failure the same way it surfaces any other save error today.
- **`get_env_value` / `get_env_value_prefer_dotenv`** — for an eligible key
  on macOS, check Keychain first, then fall back to `.env` (covers a key a
  user hand-added to `.env`, or a state left over from before this change).
  Non-macOS: unchanged.
- **`remove_env_value(key)`** — also deletes the Keychain entry for eligible
  keys on macOS.
- **`hermes_cli/env_loader.py::load_hermes_dotenv()`** — after loading
  `.env`, resolve eligible Keychain entries into `os.environ` (macOS only).
  Needed because a lot of call sites read `os.environ` directly rather than
  through `get_env_value()`, and this function is what runs at the start of
  every process (CLI invocation, gateway, the desktop app's spawned backend).

## Desktop app impact

None required. The desktop Settings UI already calls `PUT/GET/DELETE
/api/env` for every key operation, which already routes through
`save_provider_env_credential()` → `save_env_value()` /
`get_env_value()` / `remove_env_value()`. Once those functions are
Keychain-aware, the desktop app inherits the behavior automatically — same
UI, same requests, different storage underneath. `apps/desktop/electron/
hardening.ts`'s existing block on `.env` filesystem reveal remains correct
and unaffected.

## Failure / edge-case behavior

- **Keychain write denied/locked**: save fails outright (see above); no
  plaintext fallback for eligible keys once this ships.
- **Keychain read fails** (binary missing after being available at write
  time, transient `security` error): treated as "key not found" for that
  read — surfaces as the existing "missing API key" behavior, not a crash.
- **Non-macOS**: `is_available()` is `False`; every function takes its
  current `.env`-only path, zero behavior change.
- **No migration needed**: there are currently no real provider keys stored
  in `~/.hermes/.env`, so there's nothing to migrate. Keys added after this
  ships go straight to Keychain; the `.env` fallback in the read path is
  permanent resilience (e.g. a briefly-locked Keychain), not a one-time
  migration shim.

## Testing

- Unit tests for `keychain_store.py` mocking `subprocess.run` (exit 0 / 44 /
  other failure) — no real Keychain access in CI.
- Unit tests for the `config.py` integration points mocking
  `keychain_store` to verify: eligible key routes to Keychain and never
  appears in `.env`; non-eligible key is unaffected; non-macOS
  (`is_available() == False`) path is unchanged; write failure raises rather
  than silently falling back.
- Extend existing `tests/hermes_cli/test_credential_lifecycle.py` and
  `tests/hermes_cli/test_env_custom_keys.py` coverage for the new backend
  where they already exercise `save_env_value`/`get_env_value`.

## Out of scope

- Non-macOS OS keystores (Windows DPAPI, Linux libsecret) — natural follow-up
  once this pattern is proven, not part of this change.
- Platform/tool credentials (Slack, Telegram, GitHub, etc.).
- Any UI affordance beyond what's inherited for free (e.g. a "stored in
  Keychain" badge) — can be a fast follow if wanted, not required for the
  core behavior change.
