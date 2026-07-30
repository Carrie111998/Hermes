# Hermes Claude CLI Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit `claude-cli` provider that obtains Claude Max inference through the authenticated official `claude` executable while Hermes retains ownership of sessions, tools, approvals, fallback, persistence, and delivery.

**Architecture:** A subprocess-backed client presents Hermes's existing `chat.completions.create()` contract and returns completed OpenAI-shaped responses. A strict protocol layer turns Hermes messages and tool schemas into bounded Claude decision prompts, validates Claude's structured result, and translates it back into Hermes text/tool-call objects. A profile-local SQLite attachment table maps each Hermes session to its provider-native Claude session and fingerprints; the existing conversation loop remains authoritative.

**Tech Stack:** Python 3.11+, Claude Code CLI, SQLite (`hermes_state.SessionDB`), OpenAI-compatible `SimpleNamespace` response objects, JSON Schema, `subprocess`, `threading`, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Implement in an isolated Git worktree created from the approved design commit `a6f7987`; do not modify the operator's live `.hermes` profile until rollout.
- Follow red-green-refactor for every behavior. Run tests only through `scripts/run_tests.sh`, never direct `pytest`.
- Keep `anthropic` as the direct Anthropic API provider. Never alias `anthropic` and `claude-cli`.
- Never read, copy, persist, log, or transmit Claude Code OAuth credentials.
- Invoke the executable with a discrete argv list and `shell=False`; Claude built-in tools remain disabled with `--tools ""`.
- Do not import EchoGrid code or replace Hermes's conversation/tool loop.
- Do not commit the existing untracked `.install_method`.
- Commits below are intentionally small. Before each commit, inspect `git diff --check`, `git status --short`, and the staged diff.

---

## Task 1: Add the strict Claude decision protocol

**Files:**

- Create: `agent/claude_cli_protocol.py`
- Create: `tests/agent/test_claude_cli_protocol.py`

- [ ] **Step 1: Write failing protocol tests**

Create table-driven tests whose expected values are literals:

```python
import json

import pytest

from agent.claude_cli_protocol import (
    ClaudeCLIProtocolError,
    build_bootstrap_prompt,
    build_resume_prompt,
    decision_schema_json,
    parse_decision,
    to_chat_completion,
)


TOOLS = [{
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Return text",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}]


def test_accepts_final_and_converts_to_completed_chat_response():
    decision = parse_decision(
        {"kind": "final", "text": "done"},
        tools=TOOLS,
    )
    response = to_chat_completion(decision, model="opus")
    assert response.choices[0].message.content == "done"
    assert response.choices[0].message.tool_calls is None
    assert response.choices[0].finish_reason == "stop"


def test_accepts_parallel_tool_calls_and_serializes_literal_arguments():
    decision = parse_decision(
        {
            "kind": "tool_calls",
            "calls": [
                {"id": "call-a", "name": "echo", "arguments": {"text": "A"}},
                {"id": "call-b", "name": "echo", "arguments": {"text": "B"}},
            ],
        },
        tools=TOOLS,
    )
    response = to_chat_completion(decision, model="opus")
    calls = response.choices[0].message.tool_calls
    assert [(c.id, c.function.name, c.function.arguments) for c in calls] == [
        ("call-a", "echo", '{"text":"A"}'),
        ("call-b", "echo", '{"text":"B"}'),
    ]
    assert response.choices[0].finish_reason == "tool_calls"


@pytest.mark.parametrize("payload", [
    {},
    {"kind": "final", "text": ""},
    {"kind": "final", "text": "x", "calls": []},
    {"kind": "tool_calls", "calls": []},
    {"kind": "tool_calls", "calls": [
        {"id": "same", "name": "echo", "arguments": {"text": "A"}},
        {"id": "same", "name": "echo", "arguments": {"text": "B"}},
    ]},
    {"kind": "tool_calls", "calls": [
        {"id": "x", "name": "missing", "arguments": {}},
    ]},
    {"kind": "tool_calls", "calls": [
        {"id": "x", "name": "echo", "arguments": {}},
    ]},
])
def test_rejects_invalid_decisions(payload):
    with pytest.raises(ClaudeCLIProtocolError):
        parse_decision(payload, tools=TOOLS)


def test_schema_forbids_unknown_fields():
    schema = json.loads(decision_schema_json())
    assert schema["additionalProperties"] is False


def test_bootstrap_and_resume_prompts_have_distinct_deterministic_frames():
    messages = [
        {"role": "system", "content": "Hermes system"},
        {"role": "user", "content": "hello"},
    ]
    first = build_bootstrap_prompt(messages=messages, tools=TOOLS)
    resumed = build_resume_prompt(messages=[
        {"role": "tool", "tool_call_id": "call-a", "content": "A"},
    ])
    assert '"frame":"bootstrap"' in first
    assert '"frame":"delta"' in resumed
    assert "Hermes system" in first
    assert "Hermes system" not in resumed
    assert "call-a" in resumed
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
scripts/run_tests.sh tests/agent/test_claude_cli_protocol.py -q
```

Expected: FAIL during collection because `agent.claude_cli_protocol` does not exist.

- [ ] **Step 3: Implement the protocol minimally**

In `agent/claude_cli_protocol.py`:

- define `DECISION_SCHEMA` as a strict `oneOf` schema for `final` and `tool_calls`;
- serialize it with compact, sorted JSON in `decision_schema_json()`;
- normalize tool definitions by function name;
- validate the envelope, duplicate IDs, allowed names, and arguments with `jsonschema`;
- build deterministic compact JSON bootstrap/delta frames;
- translate into the completed response shape consumed by `conversation_loop.py`:

```python
SimpleNamespace(
    choices=[SimpleNamespace(
        message=SimpleNamespace(
            role="assistant",
            content=text_or_none,
            tool_calls=tool_calls_or_none,
        ),
        finish_reason="tool_calls" if tool_calls else "stop",
    )],
    usage=None,
    model=model,
)
```

Use `json.dumps(arguments, separators=(",", ":"), sort_keys=True)` so persisted tool arguments are deterministic.

- [ ] **Step 4: Run the test and verify GREEN**

Run:

```bash
scripts/run_tests.sh tests/agent/test_claude_cli_protocol.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/claude_cli_protocol.py tests/agent/test_claude_cli_protocol.py
git commit -m "feat: add strict Claude CLI decision protocol"
```

## Task 2: Add the bounded Claude process runner

**Files:**

- Create: `agent/claude_cli_process.py`
- Create: `tests/fixtures/fake_claude_cli.py`
- Create: `tests/agent/test_claude_cli_process.py`

- [ ] **Step 1: Write a deterministic fixture executable**

The fixture reads `FAKE_CLAUDE_MODE` and implements `--version`, `auth status`, successful structured output, nonzero auth/quota failures, delayed output, and a child process for termination testing. It prints Claude-like JSON:

```json
{
  "type": "result",
  "subtype": "success",
  "session_id": "11111111-1111-4111-8111-111111111111",
  "result": "{\"kind\":\"final\",\"text\":\"ok\"}",
  "structured_output": {"kind": "final", "text": "ok"},
  "model": "claude-opus-5"
}
```

- [ ] **Step 2: Write failing process-boundary tests**

Cover these observable contracts in `tests/agent/test_claude_cli_process.py`:

```python
def test_builds_discrete_argv_and_disables_claude_tools(tmp_path):
    runner = fixture_runner(tmp_path, mode="success")
    result = runner.complete(
        prompt='literal "$HOME" and ; never become shell syntax',
        schema_json='{"type":"object"}',
        model="opus",
        new_session_id="11111111-1111-4111-8111-111111111111",
    )
    assert result.decision == {"kind": "final", "text": "ok"}
    assert result.session_id == "11111111-1111-4111-8111-111111111111"
    assert result.model_reported == "claude-opus-5"
    assert "--tools" in result.argv
    assert result.argv[result.argv.index("--tools") + 1] == ""
    assert result.shell is False


def test_child_environment_removes_provider_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-child")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "never-child")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "never-child")
    monkeypatch.setenv("OPENAI_API_KEY", "remove-hermes-override")
    runner = fixture_runner(tmp_path, mode="dump-env")
    result = runner.complete(
        prompt="check environment",
        schema_json='{"type":"object"}',
        model="opus",
        new_session_id="22222222-2222-4222-8222-222222222222",
    )
    assert result.decision["text"] == "secrets-absent"
```

Also test:

- `auth_status()` accepts only `loggedIn: true` and `apiProvider: firstParty`;
- `version()` captures the executable version;
- missing executable raises `ClaudeCLIUnavailableError`;
- auth stderr raises `ClaudeCLIAuthenticationError`;
- subscription-limit stderr raises `ClaudeCLIQuotaError`;
- timeout raises `ClaudeCLITimeoutError`;
- malformed result JSON raises `ClaudeCLIExecutionError`;
- `close()` terminates the runner-owned process and its child process.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
scripts/run_tests.sh tests/agent/test_claude_cli_process.py -q
```

Expected: FAIL because the process runner does not exist.

- [ ] **Step 4: Implement the process runner**

Implement:

- immutable `ClaudeCLIRunResult`;
- typed error hierarchy with `reason` and redacted diagnostics;
- `ClaudeCLIProcessRunner(executable="claude", timeout_seconds=600)`;
- `_sanitized_env()` removing Anthropic credentials plus Hermes provider overrides (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `HERMES_*_API_KEY`, `HERMES_MODEL_PROVIDER`) while retaining `PATH`, user profile, and Claude's normal credential-store access;
- `auth_status()` using `[executable, "auth", "status", "--json"]`;
- `version()` using `[executable, "--version"]`;
- `complete()` using:

```python
[
    executable,
    "--print",
    "--output-format", "json",
    "--json-schema", schema_json,
    "--tools", "",
    "--model", model,
    "--session-id", new_session_id,  # or "--resume", existing_session_id
]
```

Pass the prompt through stdin, set `shell=False`, use `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` on Windows, track the exact active `Popen`, and kill only its owned process tree on timeout/cancel/`close()`. Parse `structured_output` first and fall back to JSON-decoding `result`; never log stdout prompts or the child environment.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
scripts/run_tests.sh tests/agent/test_claude_cli_process.py -q
```

Expected: PASS on Windows and Linux; Windows-only process-tree assertions are marked with a platform condition.

- [ ] **Step 6: Commit**

```bash
git add agent/claude_cli_process.py tests/fixtures/fake_claude_cli.py tests/agent/test_claude_cli_process.py
git commit -m "feat: add bounded Claude CLI process runner"
```

## Task 3: Persist provider-native session attachments in SQLite

**Files:**

- Modify: `hermes_state.py`
- Create: `tests/test_claude_cli_session_attachments.py`

- [ ] **Step 1: Write failing persistence and lifecycle tests**

Use a real temporary `SessionDB`, not a database mock:

```python
def test_provider_attachment_round_trips_without_credentials(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("h-1", "cli")
    db.upsert_provider_attachment(
        hermes_session_id="h-1",
        provider="claude-cli",
        provider_session_id="c-1",
        model_requested="opus",
        model_reported="claude-opus-5",
        tool_catalog_fingerprint="sha256:tools",
        system_prompt_fingerprint="sha256:system",
        last_success_at=123.0,
    )
    assert db.get_provider_attachment("h-1", "claude-cli") == {
        "hermes_session_id": "h-1",
        "provider": "claude-cli",
        "provider_session_id": "c-1",
        "model_requested": "opus",
        "model_reported": "claude-opus-5",
        "tool_catalog_fingerprint": "sha256:tools",
        "system_prompt_fingerprint": "sha256:system",
        "last_success_at": 123.0,
    }
    assert "token" not in str(db.get_provider_attachment("h-1", "claude-cli")).lower()
```

Also prove:

- upsert preserves at most one `claude-cli` attachment per Hermes session;
- a second profile DB cannot see the first profile's attachment;
- `delete_provider_attachment()` is idempotent;
- `delete_session()` cascades attachment deletion;
- schema initialization upgrades an existing v22 database without damaging session/message rows.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
scripts/run_tests.sh tests/test_claude_cli_session_attachments.py -q
```

Expected: FAIL because the table and CRUD methods do not exist.

- [ ] **Step 3: Add the declarative table and CRUD methods**

Bump `SCHEMA_VERSION` from 22 to 23 and extend `SCHEMA_SQL`:

```sql
CREATE TABLE IF NOT EXISTS provider_session_attachments (
    hermes_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_session_id TEXT NOT NULL,
    model_requested TEXT NOT NULL DEFAULT '',
    model_reported TEXT NOT NULL DEFAULT '',
    tool_catalog_fingerprint TEXT NOT NULL,
    system_prompt_fingerprint TEXT NOT NULL,
    last_success_at REAL NOT NULL,
    PRIMARY KEY (hermes_session_id, provider)
);
```

Add parameterized `upsert_provider_attachment`, `get_provider_attachment`, and `delete_provider_attachment` methods using the existing write retry/lock helpers. Keep the schema generic even though `claude-cli` is the first consumer. Enable foreign keys on each writable connection if not already enabled, and explicitly delete attachments inside `delete_session()` if existing connection behavior cannot guarantee the cascade.

- [ ] **Step 4: Run focused and existing state tests**

Run:

```bash
scripts/run_tests.sh tests/test_claude_cli_session_attachments.py tests/gateway/test_async_session_db.py tests/acp/test_session_db_private_access.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_state.py tests/test_claude_cli_session_attachments.py
git commit -m "feat: persist provider session attachments"
```

## Task 4: Build the OpenAI-compatible Claude CLI client facade

**Files:**

- Create: `agent/claude_cli_client.py`
- Create: `tests/agent/test_claude_cli_client.py`

- [ ] **Step 1: Write failing facade tests**

Inject the real fixture executable and a real temporary `SessionDB`. Cover:

- first completion generates a UUID, sends a bootstrap frame, validates the result, and writes an attachment;
- compatible second completion uses `--resume` and a delta frame;
- changed model/tool/system fingerprints create a fresh Claude session and bootstrap from Hermes canonical messages;
- a stale-session error deletes the attachment and retries exactly once with a new session;
- a second stale failure propagates;
- `close()` cancels the active runner;
- `stream=True` is accepted but returns a completed response object;
- raw prompts and credentials never enter attachment rows.

The core two-turn assertion:

```python
first = client.chat.completions.create(
    model="opus", messages=first_messages, tools=TOOLS, stream=True
)
second = client.chat.completions.create(
    model="opus", messages=first_messages + [
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "two"},
    ], tools=TOOLS, stream=True
)
assert first.choices[0].message.content == "ok"
assert second.choices[0].message.content == "ok"
assert fixture_log[0]["mode"] == "session-id"
assert fixture_log[1] == {
    "mode": "resume",
    "session_id": fixture_log[0]["session_id"],
    "frame": "delta",
}
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
scripts/run_tests.sh tests/agent/test_claude_cli_client.py -q
```

Expected: FAIL because the facade does not exist.

- [ ] **Step 3: Implement the facade**

Implement `ClaudeCLIClient` with:

- `.chat.completions.create(**request_kwargs)`;
- `.close()` and `.is_closed()`;
- `api_key="claude-cli-process"` and `base_url="claude-cli://local"` compatibility attributes;
- stable SHA-256 fingerprints over compact sorted tool JSON and the effective system message;
- Hermes session resolution from `gateway.session_context`, then `HERMES_SESSION_ID`, with an explicit constructor override for tests;
- attachment compatibility checks;
- bootstrap versus semantic-delta selection;
- exactly one fresh-session retry for classified stale resume errors;
- attachment writes only after successful protocol validation.

The facade must call `parse_decision()` itself before `to_chat_completion()` so invalid Claude output never reaches the Hermes loop.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
scripts/run_tests.sh tests/agent/test_claude_cli_client.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/claude_cli_client.py tests/agent/test_claude_cli_client.py
git commit -m "feat: adapt Claude CLI to Hermes completions"
```

## Task 5: Register `claude-cli` as a distinct subscription provider

**Files:**

- Modify: `hermes_cli/auth.py`
- Modify: `hermes_cli/models.py`
- Modify: `hermes_cli/model_normalize.py`
- Modify: `tests/hermes_cli/test_models.py`
- Create: `tests/hermes_cli/test_auth_claude_cli_provider.py`

- [ ] **Step 1: Write failing registry, picker, and status tests**

Assert:

```python
def test_claude_cli_is_distinct_from_direct_anthropic():
    assert normalize_provider_name("claude-cli") == "claude-cli"
    assert normalize_provider_name("anthropic") == "anthropic"
    assert get_provider_label("claude-cli") == "Claude Code (subscription)"


def test_claude_group_offers_subscription_and_direct_api_routes():
    assert PROVIDER_GROUPS["anthropic"][2] == ["claude-cli", "anthropic"]


def test_claude_cli_status_uses_executable_auth_probe(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_cli_process.ClaudeCLIProcessRunner.auth_status",
        lambda self: {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
        },
    )
    status = get_auth_status("claude-cli")
    assert status["logged_in"] is True
    assert status["subscription_type"] == "max"
```

Also assert the Anthropic picker description says direct API/extra usage, not “Claude Code”, and the curated `claude-cli` model list is `["opus", "sonnet", "haiku"]`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_auth_claude_cli_provider.py tests/hermes_cli/test_models.py -q
```

Expected: FAIL because `claude-cli` is unknown.

- [ ] **Step 3: Add provider metadata and health resolution**

Add:

- `ProviderConfig(id="claude-cli", name="Claude Code (subscription)", auth_type="external_process", inference_base_url="claude-cli://local")`;
- a Claude-specific external-process status path that resolves `HERMES_CLAUDE_CLI_COMMAND`, then `CLAUDE_CLI_PATH`, then `claude`, and calls `auth status --json`;
- `claude-cli: ["opus", "sonnet", "haiku"]`;
- canonical picker entry and an Anthropic display group containing subscription then direct API;
- aliases `claude-code` and `claude-subscription` to `claude-cli`;
- corrected direct `anthropic` description.

Do not reuse Copilot-specific command, args, base URL, or status fields.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_auth_claude_cli_provider.py tests/hermes_cli/test_models.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/auth.py hermes_cli/models.py hermes_cli/model_normalize.py tests/hermes_cli/test_models.py tests/hermes_cli/test_auth_claude_cli_provider.py
git commit -m "feat: register Claude subscription provider"
```

## Task 6: Route main and auxiliary inference through the facade

**Files:**

- Modify: `agent/agent_init.py`
- Modify: `agent/chat_completion_helpers.py`
- Modify: `agent/auxiliary_client.py`
- Create: `tests/agent/test_claude_cli_provider_integration.py`
- Modify: `tests/agent/test_auxiliary_client.py`

- [ ] **Step 1: Write failing main-provider integration tests**

Construct an `AIAgent` with the fixture executable and temporary Hermes home. Prove:

- `provider="claude-cli"` selects `api_mode="chat_completions"` and a `ClaudeCLIClient`;
- the streaming helper recognizes the completed response and does not construct an OpenAI HTTP client;
- a text turn completes;
- a tool decision reaches Hermes's existing tool execution path and its tool result is sent back through the same Claude session;
- a classified Claude CLI failure reaches the existing fallback chain once;
- `provider="anthropic"` still builds the native direct Anthropic client.

- [ ] **Step 2: Write failing auxiliary routing tests**

Add:

```python
def test_resolve_provider_client_claude_cli_never_builds_anthropic_client():
    with patch("agent.anthropic_adapter.build_anthropic_client") as direct:
        client, model = resolve_provider_client("claude-cli", model="opus")
        assert isinstance(client, ClaudeCLIClient)
        assert model == "opus"
        direct.assert_not_called()
```

Test both sync and async auxiliary calls. Async mode may wrap the facade in the existing thread-backed async adapter, but it must preserve `.close()` cancellation.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
scripts/run_tests.sh tests/agent/test_claude_cli_provider_integration.py tests/agent/test_auxiliary_client.py -q
```

Expected: new tests FAIL because the router does not recognize `claude-cli`.

- [ ] **Step 4: Integrate the facade**

In `agent_init.py`, add a `claude-cli` client-construction branch before generic OpenAI credential resolution. Give the facade the profile's `SessionDB`, selected executable, timeout, and model. Set:

```python
agent.api_mode = "chat_completions"
agent.api_key = "claude-cli-process"
agent.base_url = "claude-cli://local"
agent.client = ClaudeCLIClient(
    model=agent.model,
    session_db=session_db,
    executable=resolved_command,
    timeout_seconds=_provider_timeout or 600,
)
agent._client_kwargs = {}
```

In `chat_completion_helpers.py`:

- dispatch `claude-cli` through `agent.client.chat.completions.create()` like `moa`;
- in the streaming entry point, use the interruptible nonstreaming worker for `claude-cli`, because the CLI returns one bounded final object;
- ensure the worker's abort/close path calls the facade's `close()` so `/stop` terminates the active process.

In `auxiliary_client.py`, add an explicit `claude-cli` branch before direct Anthropic and generic external-process routing. Use isolated auxiliary session IDs derived from the main Hermes session plus task name so compression/search calls cannot mutate the main provider-native dialogue.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
scripts/run_tests.sh tests/agent/test_claude_cli_provider_integration.py tests/agent/test_auxiliary_client.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/agent_init.py agent/chat_completion_helpers.py agent/auxiliary_client.py tests/agent/test_claude_cli_provider_integration.py tests/agent/test_auxiliary_client.py
git commit -m "feat: route Hermes inference through Claude CLI"
```

## Task 7: Surface provider receipts and add guarded live verification

**Files:**

- Modify: `hermes_state.py`
- Modify: `hermes_cli/doctor.py`
- Modify: `hermes_cli/web_server.py`
- Create: `scripts/verify_claude_cli_provider.py`
- Create: `tests/hermes_cli/test_doctor_claude_cli.py`
- Modify: `tests/hermes_cli/test_web_server.py`
- Create: `tests/integration/test_claude_cli_live.py`

- [ ] **Step 1: Write failing receipt and diagnostic tests**

Prove:

- usage/billing rows store `billing_provider="claude-cli"` and `billing_mode="subscription_process"`;
- session/API payloads report requested alias plus provider-reported model when available;
- doctor reports executable path, version, logged-in state, `claude.ai`/first-party provider, and subscription type, without credential fields;
- fallback banners say `claude-cli`, not `anthropic`;
- live tests skip unless `HERMES_LIVE_CLAUDE_CLI=1`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_doctor_claude_cli.py tests/hermes_cli/test_web_server.py tests/integration/test_claude_cli_live.py -q
```

Expected: new assertions FAIL; live test SKIPS.

- [ ] **Step 3: Implement receipts and guarded verifier**

Record provider-reported model after successful completion without changing the configured alias. The standalone verifier must:

1. call `claude auth status --json`;
2. create a fresh Hermes session against `claude-cli / opus`;
3. require exact text `HERMES_CLAUDE_CLI_OK`;
4. require one harmless Hermes-owned tool call;
5. send a second turn and verify the same provider attachment resumed;
6. query `state.db` and require `claude-cli`/`subscription_process`;
7. fail if fallback activated.

The verifier must never inspect network packets or credential files. For the “no direct Anthropic call” acceptance, instrument Hermes's direct Anthropic client constructor and assert it is never invoked during the guarded process.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_doctor_claude_cli.py tests/hermes_cli/test_web_server.py tests/integration/test_claude_cli_live.py -q
```

Expected: unit tests PASS; live test SKIPS without its flag.

- [ ] **Step 5: Commit**

```bash
git add hermes_state.py hermes_cli/doctor.py hermes_cli/web_server.py scripts/verify_claude_cli_provider.py tests/hermes_cli/test_doctor_claude_cli.py tests/hermes_cli/test_web_server.py tests/integration/test_claude_cli_live.py
git commit -m "feat: report and verify Claude subscription usage"
```

## Task 8: Run regression gates and independent review

**Files:**

- Modify only files required by concrete gate or review failures.

- [ ] **Step 1: Run mutation-focused suites**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_claude_cli_protocol.py \
  tests/agent/test_claude_cli_process.py \
  tests/test_claude_cli_session_attachments.py \
  tests/agent/test_claude_cli_client.py \
  tests/hermes_cli/test_auth_claude_cli_provider.py \
  tests/agent/test_claude_cli_provider_integration.py \
  tests/hermes_cli/test_doctor_claude_cli.py -q
```

Expected: PASS.

Mentally mutate each critical branch: remove environment sanitization, allow an unknown tool, reuse an incompatible attachment, skip stale-session retry bounds, route auxiliary work to Anthropic, and fail to close the child. Confirm a named test fails for each mutation.

- [ ] **Step 2: Run affected regression suites**

Run serially:

```bash
scripts/run_tests.sh tests/agent tests/hermes_cli/test_models.py tests/hermes_cli/test_auth_commands.py tests/run_agent/test_auth_provider_failover.py -q
scripts/run_tests.sh tests/gateway tests/acp -q
```

Expected: PASS. Do not run these suites concurrently.

- [ ] **Step 3: Run repository-required broad gates**

Inspect the repository's current CI/`AGENTS.md` gate commands and run the Python lint/type/test commands that apply to the touched paths. Preserve exact command, exit code, duration, and candidate SHA.

- [ ] **Step 4: Request independent code review**

Use `superpowers:requesting-code-review` against the exact candidate SHA. The reviewer must inspect:

- credential ownership and redaction;
- shell/process-tree safety;
- strict tool-envelope validation;
- session/profile isolation;
- interrupt and fallback bounds;
- no regression to direct Anthropic/API-key behavior.

Resolve every High/Medium finding with TDD and rerun the affected and broad gates. Commit any fixes as focused commits.

- [ ] **Step 5: Record the verified candidate**

Run:

```bash
git status --short
git rev-parse HEAD
git diff --check
```

Expected: clean worktree and no whitespace errors. Record the exact SHA and all gate outputs in the handoff; do not call this desktop, Signal, or live subscription acceptance yet.

## Task 9: Perform guarded live, desktop, and Signal acceptance

**Files:**

- Modify: operator profile configuration under `D:\AI-Foundry\Infrastructure\hermes\.hermes` only after the candidate passes Task 8.
- Do not commit profile secrets or runtime state.

- [ ] **Step 1: Back up and inspect the active profile**

Create a timestamped copy through Hermes's normal configuration backup mechanism. Record current primary/fallback provider values and verify the gateway/desktop process identities before stopping them.

- [ ] **Step 2: Run the guarded CLI subscription verification**

From the exact verified candidate:

```powershell
$env:HERMES_LIVE_CLAUDE_CLI='1'
python scripts/verify_claude_cli_provider.py --model opus
```

Expected:

- auth reports first-party `claude.ai` and the current Max subscription;
- exact response `HERMES_CLAUDE_CLI_OK`;
- Hermes executes the harmless tool;
- second turn resumes the same Claude session;
- receipts show `claude-cli`, no fallback, and no direct Anthropic client.

- [ ] **Step 3: Change only the provider order**

Set:

```yaml
model:
  provider: claude-cli
  model: opus
fallback_providers:
  - provider: openai-codex
    model: gpt-5.6-sol
```

Preserve unrelated Signal, desktop, profile, tool, and gateway settings.

- [ ] **Step 4: Restart and verify desktop**

Restart the Hermes backend/gateway from the candidate build, open the existing desktop shortcut, create a fresh session, and verify:

- provider label is **Claude Code (subscription)**;
- first and second messages succeed without a fallback banner;
- one approved Hermes tool call completes;
- the session receipt records `claude-cli` and the provider-reported model.

- [ ] **Step 5: Verify Signal Note-to-Self**

Send a fresh Note-to-Self message through the existing linked Signal account. Verify:

- Hermes replies once;
- no routine fallback banner appears;
- delivery remains on the existing private allowlist;
- state records the Signal Hermes session attached to a Claude session;
- a follow-up resumes it.

- [ ] **Step 6: Verify fallback deliberately**

Using only the test fixture or a temporary executable override, force one classified CLI failure. Verify Hermes emits one fallback receipt and completes through `openai-codex / gpt-5.6-sol`. Remove the override and verify the next fresh session uses `claude-cli` again.

- [ ] **Step 7: Final handoff and integration**

Use `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch`. Report:

- exact merged/candidate SHA;
- focused and broad gate commands/results;
- Claude CLI version/auth metadata without credentials;
- desktop and Signal acceptance evidence;
- final provider order;
- backup/rollback location;
- any degraded Continuity limitation.

Do not claim completion until the live CLI, desktop, and Signal checks all pass on the exact candidate state.
