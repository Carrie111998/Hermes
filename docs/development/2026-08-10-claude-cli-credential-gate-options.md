# Claude CLI credential gate options

Date: 2026-08-10
Status: **decision required; no implementation authorized**

## Decision summary

The defect is a contract mismatch, not a missing credential.

`claude-cli` is intentionally an `external_process` provider. Its resolver rejects
both `api_key` and `base_url`, while the common CLI gate requires both values after
resolution. The existing native `anthropic` provider does not hit this mismatch:
it resolves an Anthropic token and the HTTPS API base URL before entering the same
gate.

**Recommendation: Option B — add an explicit runtime credential contract and make
the existing gate validate that contract.** This preserves fail-closed validation,
does not fabricate credentials, keeps native `anthropic` unchanged, and gives
future non-HTTP runtimes a typed path without adding more provider-name exceptions.

No option is implemented by this document.

## Scope and evidence boundary

This decision uses only source and tests in this worktree. It does not read
`config.yaml`, credentials, tokens, keychains, or the installed Hermes tree. No
provider was activated and no CLI login was invoked.

CodeGraph was attempted first but this worktree has no initialized `.codegraph`
index, so the evidence below comes from direct source reads.

## Verified current behavior

### 1. Where the credential gate is and what it requires

The interactive gate is
`hermes_cli/cli_agent_setup_mixin.py::_ensure_runtime_credentials`.

1. It calls `resolve_runtime_provider(...)` (`cli_agent_setup_mixin.py:38-45`).
2. It reads `api_key`, `base_url`, `provider`, and `api_mode` from the returned
   runtime dictionary (`:86-91`).
3. Unless `api_key` is a callable, it rejects an empty key (`:98-121`). The only
   exception is a non-OpenRouter custom/local HTTP endpoint with a non-empty
   `base_url`; that path substitutes the literal placeholder
   `no-key-required` (`:100-112`).
4. It then rejects every empty `base_url` (`:122-125`).
5. Only after those checks does it copy the resolved provider/routing state into
   the CLI (`:127-141`).

The silent first-run probe
`CLIAgentSetupMixin._runtime_credentials_ready` repeats the same assumptions:
a callable or non-empty string key still requires a non-empty base URL, and a
keyless runtime is ready only when it has a non-empty non-OpenRouter base URL
(`:187-219`).

Therefore the post-resolver gate is effectively uniform. It does not consult
`ProviderProfile.auth_type` or a provider-specific credential policy. It reads
`source` for diagnostics and state propagation but does not branch on it. It
already has two non-uniform cases—callable bearer tokens
and keyless local HTTP endpoints—but no external-process case.

### 2. Why `claude-cli` cannot pass

The provider profile declares:

- `api_mode="claude_cli"`;
- `env_vars=()`;
- `base_url=""`;
- `auth_type="external_process"`;
- no HTTP health check.

Source: `plugins/model-providers/claude-cli/__init__.py:13-25`.

The runtime resolver identifies the exact provider/model pair and deliberately
rejects any supplied key or URL. On success it returns both values empty and
marks the source as `external-process`
(`hermes_cli/runtime_provider.py:1706-1734`). The provider regression test fixes
that exact dictionary as the intended contract
(`tests/providers/test_claude_cli_provider.py:18-45`).

The downstream agent constructor independently enforces the same design: for
`api_mode="claude_cli"` it requires provider `claude-cli`, model
`claude-opus-5`, and empty `api_key` and `base_url`; non-empty values raise
`ValueError` (`agent/agent_init.py:1034-1047`). Thus the empty values are not a
resolver accident—the common CLI gate is the inconsistent layer.

The transport module states that authentication, subscription billing, session
state, hooks, and managed policy remain owned by the official CLI, and that Hermes
never reads Claude credentials or calls an Anthropic HTTP endpoint
(`agent/transports/claude_cli.py:1-9`). Before a production process spawn it
validates the executable version and required help flags without reading
credentials (`:158-196`, `:354-362`).

This means resolution succeeds exactly as designed and the later common gate
rejects that successful result. The third independent review
reproduced the real method printing `No API key found for provider 'claude-cli'`
and returning `False`; both
selection routes are consequently unreachable.

### 3. How native `anthropic` passes

The native profile is a different transport contract:

- `api_mode="anthropic_messages"`;
- `auth_type="api_key"`;
- API-key/token environment names are declared;
- `base_url="https://api.anthropic.com"`.

Source: `plugins/model-providers/anthropic/__init__.py:44-52`.

Its runtime branch selects the Anthropic base URL, resolves an Anthropic token,
raises `AuthError` if no credential exists, and returns a non-empty `api_key`
and `base_url` (`hermes_cli/runtime_provider.py:2072-2139`). It therefore passes
the existing uniform checks without an exception.

No proposed option removes or changes this native provider. `anthropic` and
`claude-cli` remain separate, coexisting routes.

## What counts as a valid fix

A valid fix must keep validation enabled while validating the correct runtime
contract:

- HTTP/API-key providers still require their current credentials and endpoint.
- Native `anthropic` retains its current token resolution and errors.
- `claude-cli` must continue to reject supplied `api_key` and `base_url`.
- The official executable/version/help and process protocol checks remain the
  external-process readiness boundary.
- No placeholder secret, fake HTTP URL, direct Anthropic API call, provider
  activation, permission bypass, or credential-store inspection is allowed.
- Both `_ensure_runtime_credentials` and `_runtime_credentials_ready` must use
  the same decision rule; fixing only one leaves either chat reachability or
  first-run routing broken.

## Options

| Option | What changes | Impact scope | Main risks | Rollback |
|---|---|---|---|---|
| **A. Exact `claude_cli` branch in both gate methods** | In `_ensure_runtime_credentials` and `_runtime_credentials_ready`, recognize the exact tuple `provider=claude-cli`, `api_mode=claude_cli`, `source=external-process`; require both key and URL to remain empty, then allow normal routing. All other branches keep current checks. | Two gate methods plus focused CLI tests. Resolver and transport stay unchanged. | Lowest code volume, but duplicates identity checks in two methods; future external-process providers need more exceptions; the two branches can drift. A loose check on only provider name or only API mode could accept a malformed runtime. | Remove the two exact branches and their tests. Existing behavior returns immediately. |
| **B. Typed runtime credential contract — recommended** | Add an explicit resolver field such as `credential_contract="external_process"` (or an equivalent typed value) to the `claude-cli` runtime result. Route both gate methods through one shared validator. For `external_process`, require the exact dedicated provider/API mode, empty key, empty URL, and the expected source; for the default HTTP contract, preserve the current callable/key/local-endpoint/base-URL checks. The transport's existing executable/version/help probe remains the later process-readiness check. | Resolver result schema, one shared validation helper, two gate callers, and focused regression tests. Existing runtime dictionaries without the field default to the present HTTP behavior. | Wider than A and requires careful backward-compatible defaulting. If the contract field is trusted without checking the provider/API-mode tuple, a malformed plugin could claim `external_process`; fail closed by validating the tuple and unknown contract values. | Delete the new field/helper branch and restore both callers to their current inline checks. Because untagged runtimes retain current behavior, rollback is localized. |
| **C. Provider-profile validation hook** | Add a declarative hook or method to `ProviderProfile`, such as `validate_runtime_credentials(runtime)`, and have the CLI gate resolve the active profile and call it. The Claude CLI profile would require empty key/URL and its dedicated mode; the base profile would implement today's HTTP checks. | Shared provider abstraction, profile lookup, gate methods, Claude CLI profile, and broader provider-contract tests. | Most extensible but largest abstraction change. `ProviderProfile` is documented as declarative and as not owning client construction, credential rotation, or streaming; a validation hook broadens that boundary. Alias/profile lookup failures could change unrelated providers unless fallback behavior is strictly fail-closed. Existing `copilot-acp` also declares `external_process` but has a different ACP/base-URL contract, so branching on `auth_type` alone is unsafe. | Remove the hook override and restore the gate's local validator. Revert profile tests. |
| **D. Synthetic key and URL sentinels — reject** | Return values such as `api_key="external-process"` and `base_url="claude-cli://local"` solely to satisfy the current gate. | Resolver, `agent_init.py`, the transport, and their tests, because both downstream layers reject non-empty values. | Semantically false and guaranteed to raise `ValueError` during Claude CLI agent construction and transport kwargs validation. It contradicts the explicit empty-value contract and hides rather than resolves the type mismatch. | Restore empty values and remove any compensating downstream exceptions. |

## Recommendation

Choose **Option B**.

The current code already treats the runtime dictionary as the handoff between
provider resolution and CLI construction, and `claude-cli` already has a unique
resolver branch. Adding a typed contract at that handoff makes the difference
explicit where it originates while keeping the gate responsible for validation.
It is not a bypass: the gate still rejects malformed external-process results,
unknown contract values, unexpected credentials, wrong provider/model routing,
and all existing invalid HTTP credentials.

Option A is an acceptable emergency minimum but hard-codes the same special case
in two places. Option C is better suited to a separate provider-contract
refactor, not this blocked Stage-0 slice. Option D should not be adopted.

## Required verification if Option B is approved

These are acceptance criteria for a later implementation; they are not executed
or implemented by this document.

1. Resolver tests prove that `claude-cli` returns the typed external-process
   contract only for the exact `claude-opus-5` pair and still rejects any key or
   URL.
2. `_ensure_runtime_credentials` accepts that exact result and updates provider,
   API mode, source, key, and URL without fabricating values.
3. `_runtime_credentials_ready` returns true for the same exact result and false
   for malformed provider/mode/source combinations.
4. Empty credentials for OpenRouter/API-key providers still fail; keyless local
   HTTP endpoints and callable bearer-token providers retain existing behavior.
5. Native `anthropic` still fails without a token and passes with its resolved
   token and HTTPS base URL.
6. Unknown credential-contract values fail closed.
7. Existing `copilot-acp` behavior is unchanged; `auth_type="external_process"`
   alone does not select the Claude CLI contract.
8. The dedicated Claude CLI spawn path still runs the version/help capability
   check and still rejects forbidden flags, tools, non-empty init tools, and
   invalid process generations.
9. OCR preview/rule, fresh independent review, and repository quality gates pass
   on the final implementation diff before any commit or provider activation.

## User decision

Select A, B, or C before implementation. No implementation should begin from
this document alone.
