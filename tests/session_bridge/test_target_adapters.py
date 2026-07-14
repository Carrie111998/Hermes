from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

import session_bridge.characterize as characterize_module
from session_bridge.claude_adapter import (
    AmbiguousPlaceholderCreation,
    ClaudeSourceAdapter,
    ClaudeTargetAdapter,
    PlaceholderCreationError,
    PlaceholderResult,
    classify_claude_process_failure,
)
from session_bridge.codex_adapter import (
    CodexSourceAdapter,
    CodexTargetAdapter,
    classify_codex_empty_read_error,
)
from session_bridge.characterize import (
    LiveCharacterizationError,
    UnsafeCharacterizationCleanup,
    _claude_result_metrics,
    quarantine_claude_transcript,
    resolve_cli_executable,
    run_live_characterization,
    write_characterization_report,
)
from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    Provider,
    encode_bridge_marker,
)


SECRET = b"target-adapter-test-secret"
CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
CODEX_ID = "22222222-2222-4222-8222-222222222222"


class FakeClaudeSource:
    def __init__(
        self,
        *,
        found: Path | None = Path("C:/claude/project/transcript.jsonl"),
        projection_native_id: str = CLAUDE_ID,
        bridge_id: str = "bridge-1",
    ) -> None:
        self.found = found
        self.projection_native_id = projection_native_id
        self.bridge_id = bridge_id
        self.find_calls: list[str] = []

    def find_native_session(self, native_id: str) -> Path | None:
        self.find_calls.append(native_id)
        return self.found

    def parse(self, path: Path) -> Any:
        assert path == self.found
        projection = SimpleNamespace(
            native_id=self.projection_native_id,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=self.bridge_id,
        )
        return SimpleNamespace(projection=projection)


class FakeClaudeResumeSource:
    def __init__(
        self,
        projection: Any,
        *,
        found: Path | None = Path("C:/claude/project/transcript.jsonl"),
        parse_error: Exception | None = None,
    ) -> None:
        self.projection = projection
        self.found = found
        self.parse_error = parse_error
        self.find_calls: list[str] = []
        self.parse_calls: list[Path] = []

    def find_native_session(self, native_id: str) -> Path | None:
        self.find_calls.append(native_id)
        return self.found

    def parse(self, path: Path) -> Any:
        self.parse_calls.append(path)
        if self.parse_error is not None:
            raise self.parse_error
        return SimpleNamespace(projection=self.projection)


class SequencedClaudeResumeSource:
    def __init__(self, projection: Any, find_results: list[Path | None]) -> None:
        self.projection = projection
        self.find_results = list(find_results)
        self.find_calls: list[str] = []
        self.parse_calls: list[Path] = []

    def find_native_session(self, native_id: str) -> Path | None:
        self.find_calls.append(native_id)
        return self.find_results.pop(0)

    def parse(self, path: Path) -> Any:
        self.parse_calls.append(path)
        return SimpleNamespace(projection=self.projection)


def _resume_projection(
    *,
    origin_kind: OriginKind,
    bridge_id: str = "bridge-1",
    native_id: str = CLAUDE_ID,
    cursor: str = "cursor-after",
    native_hash: str = "hash-after",
    new_user_content: str | None = None,
) -> Any:
    messages = [
        SimpleNamespace(
            native_event_id="registration",
            ordinal=0,
            role="user",
            content="registration metadata",
        )
    ]
    if new_user_content is not None:
        messages.append(
            SimpleNamespace(
                native_event_id="resume-user",
                ordinal=0,
                role="user",
                content=new_user_content,
            )
        )
    return SimpleNamespace(
        native_id=native_id,
        origin_kind=origin_kind,
        origin_bridge_id=bridge_id,
        native_cursor=cursor,
        native_hash=native_hash,
        messages=messages,
    )


class FakeRequestClient:
    def __init__(self, responses: dict[str, list[dict[str, Any] | Exception]]) -> None:
        self.responses = {method: list(values) for method, values in responses.items()}
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        self.calls.append((method, deepcopy(params), timeout))
        response = self.responses[method].pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


class FakeCodexRpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def test_codex_empty_read_classifier_requires_rpc_code_exact_id_and_missing_rollout() -> None:
    recognized, code = classify_codex_empty_read_error(
        FakeCodexRpcError(
            -32603,
            f"failed to locate rollout for thread `{CODEX_ID}` (not persisted)",
        ),
        CODEX_ID,
    )
    assert recognized is True
    assert code == "codex_empty_read_missing_rollout"

    recognized, code = classify_codex_empty_read_error(
        FakeCodexRpcError(-32603, f"thread {CODEX_ID} not found or not persisted"),
        CODEX_ID,
    )
    assert recognized is True
    assert code == "codex_empty_read_missing_thread"

    recognized, code = classify_codex_empty_read_error(
        FakeCodexRpcError(
            -32603,
            f"failed to materialize rollout session for thread {CODEX_ID}",
        ),
        CODEX_ID,
    )
    assert recognized is True
    assert code == "codex_empty_read_missing_rollout"

    recognized, code = classify_codex_empty_read_error(
        FakeCodexRpcError(-32603, "failed to locate rollout for a different thread"),
        CODEX_ID,
    )
    assert recognized is False
    assert code == "codex_empty_read_identity_unconfirmed"

    secret = "sk-proj-THIS-MUST-NOT-LEAK-1234567890"
    recognized, code = classify_codex_empty_read_error(
        FakeCodexRpcError(-32000, f"provider failed {secret}"), CODEX_ID
    )
    assert recognized is False
    assert secret not in code


def _codex_inventory(*, title: str = "Mirror title", native_id: str = CODEX_ID):
    return {
        "data": [
            {
                "id": native_id,
                "title": title,
                "cwd": "C:/valid",
                "createdAt": 100.0,
                "updatedAt": 101.0,
                "revision": "revision-1",
            }
        ]
    }


def _codex_read(*, native_id: str = CODEX_ID, turns: list | None = None):
    return {"thread": {"id": native_id, "turns": turns or []}}


def _codex_signed_read(
    *,
    native_id: str = CODEX_ID,
    bridge_id: str = "bridge-1",
    source_session_id: str = "claude:source-1",
    policy_generation: int = 1,
    turn_id: str = "turn-registration",
) -> dict[str, Any]:
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=policy_generation,
        ),
        SECRET,
    )
    return _codex_read(
        native_id=native_id,
        turns=[
            {
                "id": turn_id,
                "items": [
                    {
                        "type": "userMessage",
                        "id": f"item-{turn_id}",
                        "content": [{"type": "text", "text": marker}],
                    }
                ],
            }
        ],
    )


def _codex_adapter(
    responses: dict[str, list[dict[str, Any] | Exception]],
    **kwargs: Any,
) -> tuple[CodexTargetAdapter, FakeRequestClient]:
    client = FakeRequestClient(responses)
    source = CodexSourceAdapter(client, marker_secret=SECRET)
    return (
        CodexTargetAdapter(
            client,
            source_adapter=source,
            marker_secret=SECRET,
            clock=lambda: 1234.5,
            sleep=kwargs.pop("sleep", lambda _: None),
            verification_timeout=kwargs.pop("verification_timeout", 0.0),
            **kwargs,
        ),
        client,
    )


def test_placeholder_result_is_the_frozen_common_contract() -> None:
    result = PlaceholderResult(
        native_id=CLAUDE_ID,
        canonical_session_id=f"claude:{CLAUDE_ID}",
        used_registration_turn=False,
        verified_at=1234.5,
    )

    with pytest.raises(FrozenInstanceError):
        result.native_id = "different"  # type: ignore[misc]


def test_claude_uses_exact_no_shell_argv_and_verifies_the_signed_target() -> None:
    source = FakeClaudeSource()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), dict(kwargs)))
        return subprocess.CompletedProcess(args, 0, stdout='{"result":"ready"}', stderr="")

    result = ClaudeTargetAdapter(
        source,
        marker_secret=SECRET,
        claude_executable="C:/bin/claude.exe",
        runner=runner,
        clock=lambda: 1234.5,
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    ).create_placeholder(
        native_id=CLAUDE_ID,
        title="Mirror title",
        source_session_id="codex:source-1",
        bridge_id="bridge-1",
        policy_generation=7,
    )

    args, kwargs = calls[0]
    assert args[:-1] == [
        "C:/bin/claude.exe",
        "--print",
        "--session-id",
        CLAUDE_ID,
        "--name",
        "Mirror title",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--max-budget-usd",
        "0.50",
        "--output-format",
        "json",
    ]
    prompt = args[-1]
    assert "HERMES_SESSION_BRIDGE_V1:" in prompt
    assert "codex:source-1" in prompt
    assert "This registration message is metadata, not a substantive user message." in prompt
    assert "Do not call any tool now." in prompt
    assert "Reply exactly REGISTERED and nothing else." in prompt
    assert "On the first subsequent substantive user message" in prompt
    assert "session_continue" in prompt and "bridge-1" in prompt
    assert prompt.count("session_continue") == 1
    assert "\r" not in prompt and "\n" not in prompt
    assert "--model" not in args
    assert args.count("--max-budget-usd") == 1
    assert kwargs["shell"] is False
    assert result == PlaceholderResult(
        native_id=CLAUDE_ID,
        canonical_session_id=f"claude:{CLAUDE_ID}",
        used_registration_turn=False,
        verified_at=1234.5,
    )
    assert source.find_calls == [CLAUDE_ID]


def test_claude_single_line_registration_survives_windows_cmd_and_parses_marker(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / f"{CLAUDE_ID}.jsonl"
    source = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    prompts: list[str] = []

    def windows_cmd_runner(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        prompt = args[-1]
        prompts.append(prompt)
        persisted_prompt = prompt.splitlines()[0]
        transcript.write_text(
            json.dumps({
                "type": "user",
                "sessionId": CLAUDE_ID,
                "timestamp": "2026-07-14T00:00:00Z",
                "message": {"content": persisted_prompt},
            })
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    result = ClaudeTargetAdapter(
        source,
        marker_secret=SECRET,
        runner=windows_cmd_runner,
        clock=lambda: 1234.5,
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    ).create_placeholder(
        native_id=CLAUDE_ID,
        title="Mirror title",
        source_session_id="codex:source-1",
        bridge_id="bridge-1",
        policy_generation=7,
    )

    assert result.native_id == CLAUDE_ID
    assert len(prompts) == 1
    assert "\r" not in prompts[0] and "\n" not in prompts[0]
    projection = source.parse(transcript).projection
    assert projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
    assert projection.origin_bridge_id == "bridge-1"


def test_claude_timeout_reconciles_exact_uuid_before_returning_ambiguous() -> None:
    source = FakeClaudeSource(found=None)
    runner_calls = 0

    def runner(*args: Any, **kwargs: Any) -> Any:
        nonlocal runner_calls
        runner_calls += 1
        raise subprocess.TimeoutExpired(cmd="claude", timeout=10, output="token=secret")

    adapter = ClaudeTargetAdapter(
        source,
        marker_secret=SECRET,
        runner=runner,
        monotonic=iter([0.0, 0.0, 2.0]).__next__,
        sleep=lambda _: None,
        discovery_timeout=1.0,
    )

    with pytest.raises(AmbiguousPlaceholderCreation, match="claude_creation_ambiguous"):
        adapter.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert runner_calls == 1
    assert source.find_calls and set(source.find_calls) == {CLAUDE_ID}


def test_claude_nonzero_reconciles_an_exact_authenticated_durable_target() -> None:
    source = FakeClaudeSource()
    runner_calls = 0

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal runner_calls
        runner_calls += 1
        return subprocess.CompletedProcess(
            args,
            1,
            stdout=json.dumps({
                "subtype": "error_max_budget_usd",
                "total_cost_usd": 0.5,
                "duration_ms": 847,
                "num_turns": 1,
                "result": "sk-proj-THIS-MUST-NOT-LEAK-1234567890",
            }),
            stderr="private",
        )

    result = ClaudeTargetAdapter(
        source,
        marker_secret=SECRET,
        runner=runner,
        clock=lambda: 1234.5,
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    ).create_placeholder(
        native_id=CLAUDE_ID,
        title="Mirror title",
        source_session_id="codex:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    assert result == PlaceholderResult(
        native_id=CLAUDE_ID,
        canonical_session_id=f"claude:{CLAUDE_ID}",
        used_registration_turn=False,
        verified_at=1234.5,
    )
    assert runner_calls == 1
    assert source.find_calls == [CLAUDE_ID]


@pytest.mark.parametrize(
    ("projection_native_id", "bridge_id", "expected_code"),
    [
        ("different", "bridge-1", "claude_target_mismatch"),
        (CLAUDE_ID, "different", "claude_target_marker_mismatch"),
    ],
)
def test_claude_nonzero_durable_target_mismatch_fails_closed(
    projection_native_id: str,
    bridge_id: str,
    expected_code: str,
) -> None:
    source = FakeClaudeSource(
        projection_native_id=projection_native_id,
        bridge_id=bridge_id,
    )
    adapter = ClaudeTargetAdapter(
        source,
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            1,
            stdout='{"subtype":"error_max_budget_usd"}',
            stderr="private",
        ),
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )

    with pytest.raises(PlaceholderCreationError, match=expected_code):
        adapter.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )


def test_claude_rejects_wrong_verified_identity_and_sanitizes_process_errors() -> None:
    wrong_source = FakeClaudeSource(projection_native_id="different")
    adapter = ClaudeTargetAdapter(
        wrong_source,
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "{}", ""),
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )
    with pytest.raises(PlaceholderCreationError, match="claude_target_mismatch"):
        adapter.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    secret = "sk-proj-THIS-MUST-NOT-LEAK-1234567890"
    failed = ClaudeTargetAdapter(
        FakeClaudeSource(found=None),
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 9, "", f"provider rejected {secret}"
        ),
        monotonic=lambda: 0.0,
        discovery_timeout=0.0,
    )
    with pytest.raises(PlaceholderCreationError) as raised:
        failed.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )
    assert secret not in str(raised.value)
    assert "HERMES_SESSION_BRIDGE_V1" not in str(raised.value)


def test_claude_process_failure_exposes_only_a_bounded_json_subtype() -> None:
    assert classify_claude_process_failure(
        subprocess.CompletedProcess(
            ["claude"],
            1,
            stdout=(
                '{"type":"result","subtype":"error_max_budget_usd",'
                '"result":"sk-proj-THIS-MUST-NOT-LEAK-1234567890"}'
            ),
            stderr="provider token=secret",
        )
    ) == "claude_process_error_max_budget_usd"
    assert classify_claude_process_failure(
        subprocess.CompletedProcess(
            ["claude"], 9, stdout="not json sk-proj-secret", stderr="private"
        )
    ) == "claude_process_exit_9"


def test_claude_process_failure_preserves_only_allowlisted_numeric_diagnostics() -> None:
    secret = "sk-proj-THIS-MUST-NOT-LEAK-1234567890"
    marker = "HERMES_SESSION_BRIDGE_V1:payload.signature"
    failed = ClaudeTargetAdapter(
        FakeClaudeSource(found=None),
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            1,
            stdout=json.dumps({
                "type": "result",
                "subtype": "error_max_budget_usd",
                "total_cost_usd": 0.24987,
                "duration_ms": 42123,
                "num_turns": 2,
                "usage": {"input_tokens": 9876, "secret": secret},
                "result": secret,
                "prompt": marker,
                "path": "C:/private/session.jsonl",
            }),
            stderr=f"provider rejected {secret}",
        ),
        monotonic=lambda: 0.0,
        discovery_timeout=0.0,
    )

    with pytest.raises(PlaceholderCreationError) as raised:
        failed.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    error = raised.value
    assert error.code == "claude_process_error_max_budget_usd"
    assert error.observed_cost_usd == 0.24987
    assert error.duration_ms == 42123.0
    assert error.num_turns == 2
    assert set(vars(error)) == {
        "code",
        "native_id",
        "observed_cost_usd",
        "duration_ms",
        "num_turns",
    }
    serialized = json.dumps(vars(error), sort_keys=True)
    assert secret not in serialized
    assert marker not in serialized
    assert "usage" not in serialized
    assert "input_tokens" not in serialized
    assert "session.jsonl" not in serialized


@pytest.mark.parametrize(
    ("total_cost_usd", "duration_ms", "num_turns"),
    [
        (-0.01, -1, -1),
        ("0.24", "42123", 2.0),
        (True, False, True),
        (float("inf"), float("nan"), "2"),
    ],
)
def test_claude_process_failure_omits_malformed_numeric_diagnostics(
    total_cost_usd: Any,
    duration_ms: Any,
    num_turns: Any,
) -> None:
    failed = ClaudeTargetAdapter(
        FakeClaudeSource(found=None),
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            1,
            stdout=json.dumps({
                "subtype": "error_max_budget_usd",
                "total_cost_usd": total_cost_usd,
                "duration_ms": duration_ms,
                "num_turns": num_turns,
            }),
            stderr="private",
        ),
        monotonic=lambda: 0.0,
        discovery_timeout=0.0,
    )

    with pytest.raises(PlaceholderCreationError) as raised:
        failed.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert raised.value.observed_cost_usd is None
    assert raised.value.duration_ms is None
    assert raised.value.num_turns is None


def test_codex_uses_supported_method_order_instructions_and_exact_verification(
    tmp_path: Path,
) -> None:
    adapter, client = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [_codex_inventory()],
        "thread/read": [_codex_signed_read(policy_generation=3)],
    })

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=3,
        cwd=tmp_path,
    )

    assert [method for method, _, _ in client.calls] == [
        "thread/start",
        "thread/name/set",
        "thread/list",
        "thread/read",
    ]
    start = client.calls[0][1]
    assert start["cwd"] == str(tmp_path.resolve())
    assert "HERMES_SESSION_BRIDGE_V1:" in start["baseInstructions"]
    assert "HERMES_SESSION_BRIDGE_V1:" in start["developerInstructions"]
    assert "session_continue" in start["baseInstructions"]
    assert "session_continue" in start["developerInstructions"]
    assert client.calls[1][1] == {"threadId": CODEX_ID, "name": "Mirror title"}
    assert client.calls[2][1] == {
        "archived": False,
        "sourceKinds": ["vscode", "appServer"],
    }
    assert client.calls[3][1] == {"threadId": CODEX_ID, "includeTurns": True}
    assert result == PlaceholderResult(
        native_id=CODEX_ID,
        canonical_session_id=f"codex:{CODEX_ID}",
        used_registration_turn=False,
        verified_at=1234.5,
    )


def test_codex_visible_native_target_gets_one_authenticated_registration_turn() -> None:
    adapter, client = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [_codex_inventory(), _codex_inventory()],
        "thread/read": [_codex_read(), _codex_signed_read()],
        "turn/start": [{"turn": {"id": "turn-registration"}}],
    })

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    assert result.used_registration_turn is True
    assert [method for method, _, _ in client.calls] == [
        "thread/start",
        "thread/name/set",
        "thread/list",
        "thread/read",
        "turn/start",
        "thread/list",
        "thread/read",
    ]
    assert [
        params["threadId"]
        for method, params, _ in client.calls
        if method in {"thread/read", "turn/start"}
    ] == [CODEX_ID, CODEX_ID, CODEX_ID]
    assert [method for method, _, _ in client.calls].count("turn/start") == 1


@pytest.mark.parametrize(
    ("initial_read", "post_registration_read", "expected_turns"),
    [
        (_codex_signed_read(bridge_id="different"), None, 0),
        (_codex_read(), _codex_read(), 1),
    ],
)
def test_codex_wrong_or_missing_signed_provenance_fails_closed(
    initial_read: dict[str, Any],
    post_registration_read: dict[str, Any] | None,
    expected_turns: int,
) -> None:
    responses: dict[str, list[dict[str, Any] | Exception]] = {
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [_codex_inventory()],
        "thread/read": [initial_read],
        "turn/start": [],
    }
    if post_registration_read is not None:
        responses["thread/list"].append(_codex_inventory())
        responses["thread/read"].append(post_registration_read)
        responses["turn/start"].append({"turn": {"id": "turn-registration"}})
    adapter, client = _codex_adapter(responses)

    with pytest.raises(AmbiguousPlaceholderCreation, match="codex_target_marker_mismatch"):
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert [method for method, _, _ in client.calls].count("thread/start") == 1
    assert [method for method, _, _ in client.calls].count("turn/start") == expected_turns


def test_codex_exact_discovery_can_include_sidebar_and_app_server_threads() -> None:
    client = FakeRequestClient({"thread/list": [_codex_inventory()]})
    source = CodexSourceAdapter(client, marker_secret=SECRET)

    found = source.find_native_thread(
        CODEX_ID, source_kinds=("vscode", "appServer")
    )

    assert found is not None and found.native_id == CODEX_ID
    assert client.calls == [
        (
            "thread/list",
            {
                "archived": False,
                "sourceKinds": ["vscode", "appServer"],
            },
            30.0,
        )
    ]


def test_codex_omits_invalid_cwd_and_rejects_non_exact_inventory_target(
    tmp_path: Path,
) -> None:
    adapter, client = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [_codex_inventory(native_id="different")],
        "thread/read": [],
    })

    with pytest.raises(PlaceholderCreationError, match="codex_target_not_found"):
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
            cwd=tmp_path / "missing",
        )

    assert "cwd" not in client.calls[0][1]
    assert [method for method, _, _ in client.calls].count("thread/start") == 1


def test_codex_ambiguous_name_timeout_reconciles_before_any_mutation_retry() -> None:
    adapter, client = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [TimeoutError("request payload token=secret")],
        "thread/list": [_codex_inventory()],
        "thread/read": [_codex_signed_read()],
    })

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    assert result.native_id == CODEX_ID
    assert [method for method, _, _ in client.calls].count("thread/name/set") == 1
    assert [method for method, _, _ in client.calls] == [
        "thread/start",
        "thread/name/set",
        "thread/list",
        "thread/read",
    ]


def test_codex_post_start_ambiguity_carries_exact_native_id_for_reconciliation() -> None:
    adapter, client = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [TimeoutError("unknown outcome")],
        "thread/list": [_codex_inventory(title="Different title")],
    })

    with pytest.raises(AmbiguousPlaceholderCreation) as raised:
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert raised.value.native_id == CODEX_ID
    assert [method for method, _, _ in client.calls].count("thread/start") == 1
    assert [method for method, _, _ in client.calls].count("thread/name/set") == 1


def test_codex_final_read_failure_has_a_sanitized_stage_code() -> None:
    adapter, _ = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [_codex_inventory()],
        "thread/read": [{"thread": {"id": CODEX_ID}}],
    })

    with pytest.raises(AmbiguousPlaceholderCreation) as raised:
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert raised.value.code == "codex_target_read_unreadable"
    assert raised.value.native_id == CODEX_ID


def test_codex_registration_turn_fallback_is_exactly_once_and_verified() -> None:
    adapter, client = _codex_adapter(
        {
            "thread/start": [{"thread": {"id": CODEX_ID}}],
            "thread/name/set": [{}],
            "turn/start": [{"turn": {"id": "turn-registration"}}],
            "thread/list": [_codex_inventory()],
            "thread/read": [_codex_signed_read()],
        },
        require_registration_turn=True,
    )

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    methods = [method for method, _, _ in client.calls]
    assert methods == [
        "thread/start",
        "thread/name/set",
        "turn/start",
        "thread/list",
        "thread/read",
    ]
    registration = client.calls[2][1]
    assert registration["threadId"] == CODEX_ID
    assert len(registration["input"]) == 1
    assert "HERMES_SESSION_BRIDGE_V1:" in registration["input"][0]["text"]
    assert result.used_registration_turn is True


def test_codex_characterization_probes_empty_visibility_before_fallback() -> None:
    adapter, client = _codex_adapter(
        {
            "thread/start": [{"thread": {"id": CODEX_ID}}],
            "thread/name/set": [{}],
            "thread/list": [{"data": []}, _codex_inventory()],
            "turn/start": [{"turn": {"id": "turn-registration"}}],
            "thread/read": [
                FakeCodexRpcError(
                    -32603,
                    f"failed to locate rollout for thread {CODEX_ID}",
                ),
                _codex_signed_read(),
            ],
        },
        require_registration_turn=None,
    )

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    assert [method for method, _, _ in client.calls] == [
        "thread/start",
        "thread/name/set",
        "thread/list",
        "thread/read",
        "turn/start",
        "thread/list",
        "thread/read",
    ]
    assert result.used_registration_turn is True


def test_codex_registration_fallback_polls_until_sidebar_inventory_is_visible() -> None:
    times = iter([0.0, 0.0, 0.5])
    sleeps: list[float] = []
    adapter, client = _codex_adapter(
        {
            "thread/start": [{"thread": {"id": CODEX_ID}}],
            "thread/name/set": [{}],
            "thread/list": [{"data": []}, {"data": []}, _codex_inventory()],
            "thread/read": [
                FakeCodexRpcError(
                    -32603,
                    f"failed to materialize rollout session for thread {CODEX_ID}",
                ),
                _codex_signed_read(),
            ],
            "turn/start": [{"turn": {"id": "turn-registration"}}],
        },
        require_registration_turn=None,
        verification_timeout=1.0,
        monotonic=times.__next__,
        sleep=sleeps.append,
    )

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    assert result.used_registration_turn is True
    assert [method for method, _, _ in client.calls].count("thread/list") == 3
    assert sleeps == [0.1]


def test_codex_start_timeout_is_ambiguous_without_retry_and_errors_are_sanitized() -> None:
    secret = "sk-proj-THIS-MUST-NOT-LEAK-1234567890"
    adapter, client = _codex_adapter({
        "thread/start": [TimeoutError(f"request leaked {secret}")],
    })

    with pytest.raises(AmbiguousPlaceholderCreation) as raised:
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert [method for method, _, _ in client.calls] == ["thread/start"]
    assert secret not in str(raised.value)
    assert "HERMES_SESSION_BRIDGE_V1" not in str(raised.value)


def test_target_adapters_never_write_provider_jsonl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("provider transcript writes are forbidden")

    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)

    ClaudeTargetAdapter(
        FakeClaudeSource(),
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "{}", ""),
        clock=lambda: 1.0,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    ).create_placeholder(
        native_id=CLAUDE_ID,
        title="Mirror title",
        source_session_id="codex:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    codex, _ = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [_codex_inventory()],
        "thread/read": [_codex_signed_read()],
    })
    codex.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )


def test_characterization_quarantine_moves_only_exact_parsed_uuid_and_marker(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / f"{CLAUDE_ID}.jsonl"
    transcript.write_text("disposable", encoding="utf-8")
    source = FakeClaudeSource(found=transcript)
    quarantine = tmp_path / "quarantine"

    moved = quarantine_claude_transcript(
        source,
        native_id=CLAUDE_ID,
        bridge_id="bridge-1",
        projects_root=tmp_path,
        quarantine_root=quarantine,
    )

    assert moved == quarantine / f"{CLAUDE_ID}.jsonl"
    assert moved.read_text(encoding="utf-8") == "disposable"
    assert not transcript.exists()

    wrong = tmp_path / f"{CLAUDE_ID}.jsonl"
    wrong.write_text("ordinary", encoding="utf-8")
    wrong_source = FakeClaudeSource(found=wrong, bridge_id="ordinary-bridge")
    with pytest.raises(UnsafeCharacterizationCleanup):
        quarantine_claude_transcript(
            wrong_source,
            native_id=CLAUDE_ID,
            bridge_id="bridge-1",
            projects_root=tmp_path,
            quarantine_root=quarantine,
        )
    assert wrong.exists()


def test_characterization_report_drops_sensitive_diagnostics(tmp_path: Path) -> None:
    secret = "sk-proj-THIS-MUST-NOT-LEAK-1234567890"
    report_path = write_characterization_report(
        {
            "schema_version": 1,
            "providers": {"codex": {"create": False, "error_code": "timeout"}},
            "stderr": f"provider said {secret}",
            "marker": "HERMES_SESSION_BRIDGE_V1:payload.signature",
        },
        report_root=tmp_path,
        characterization_id="33333333-3333-4333-8333-333333333333",
    )

    serialized = report_path.read_text(encoding="utf-8")
    assert secret not in serialized
    assert "HERMES_SESSION_BRIDGE_V1" not in serialized
    assert "stderr" not in serialized
    assert '"error_code":"timeout"' in serialized


def test_codex_resume_polls_until_exact_started_turn_is_read() -> None:
    nonce = "c" * 32
    client = FakeRequestClient({
        "turn/start": [{"turn": {"id": "turn-resume-exact"}}],
        "thread/read": [
            _codex_read(turns=[{"id": "turn-registration", "items": []}]),
            _codex_read(turns=[
                {"id": "turn-registration", "items": []},
                {"id": "turn-resume-exact", "items": []},
            ]),
        ],
    })
    waited: list[tuple[str | None, float]] = []
    sleeps: list[float] = []
    times = iter([0.0, 0.0])

    turn_id = characterize_module._resume_codex_characterization(
        client,
        native_id=CODEX_ID,
        resume_nonce=nonce,
        request_timeout=45.0,
        verification_timeout=1.0,
        verification_poll_interval=0.1,
        monotonic=times.__next__,
        sleep=sleeps.append,
        completion_waiter=lambda client, expected_turn_id, timeout: waited.append(
            (expected_turn_id, timeout)
        ),
    )

    assert turn_id == "turn-resume-exact"
    assert [method for method, _, _ in client.calls] == [
        "turn/start",
        "thread/read",
        "thread/read",
    ]
    assert [method for method, _, _ in client.calls].count("turn/start") == 1
    turn_input = client.calls[0][1]["input"]
    assert turn_input[0]["text"].count(nonce) == 1
    assert waited == [("turn-resume-exact", 180.0)]
    assert sleeps == [0.1]


def test_codex_resume_rejects_stale_or_wrong_turn_identity() -> None:
    client = FakeRequestClient({
        "turn/start": [{"turn": {"id": "turn-resume-exact"}}],
        "thread/read": [
            _codex_read(turns=[{"id": "turn-registration", "items": []}])
        ],
    })

    with pytest.raises(RuntimeError, match="codex_resume_turn_not_found"):
        characterize_module._resume_codex_characterization(
            client,
            native_id=CODEX_ID,
            resume_nonce="c" * 32,
            request_timeout=45.0,
            verification_timeout=0.0,
            verification_poll_interval=0.1,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
            completion_waiter=lambda *args, **kwargs: None,
        )

    assert [method for method, _, _ in client.calls].count("turn/start") == 1
    assert [method for method, _, _ in client.calls].count("thread/read") == 1


@pytest.mark.parametrize("process_outcome", ["nonzero", "timeout"])
def test_claude_resume_reconciles_nonce_bound_durable_continuation_once(
    tmp_path: Path,
    process_outcome: str,
) -> None:
    nonce = "a" * 32
    baseline = _resume_projection(
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        cursor="cursor-before",
        native_hash="hash-before",
    )
    source = FakeClaudeResumeSource(
        _resume_projection(
            origin_kind=OriginKind.BRIDGE_CONTINUATION,
            new_user_content=f"resume verification {nonce}",
        )
    )
    runner_calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(args: list[str], **kwargs: Any) -> Any:
        runner_calls.append((list(args), dict(kwargs)))
        if process_outcome == "timeout":
            raise subprocess.TimeoutExpired(
                cmd="claude", timeout=180, output="private", stderr="private"
            )
        return subprocess.CompletedProcess(
            args,
            1,
            stdout=json.dumps({
                "subtype": "error_max_budget_usd",
                "total_cost_usd": 0.5,
                "duration_ms": 847,
                "num_turns": 1,
                "result": "sk-proj-THIS-MUST-NOT-LEAK-1234567890",
            }),
            stderr="private",
        )

    completed = characterize_module._resume_claude_characterization(
        source,
        baseline_projection=baseline,
        native_id=CLAUDE_ID,
        bridge_id="bridge-1",
        resume_nonce=nonce,
        executable="C:/bin/claude.cmd",
        cwd=tmp_path,
        runner=runner,
    )

    assert completed is None or completed.returncode == 1
    assert len(runner_calls) == 1
    args, kwargs = runner_calls[0]
    assert args.count("--resume") == 1
    assert args[-1].count(nonce) == 1
    assert "\r" not in args[-1] and "\n" not in args[-1]
    assert kwargs["shell"] is False
    assert source.find_calls == [CLAUDE_ID]
    assert source.parse_calls == [source.found]


@pytest.mark.parametrize("process_outcome", ["success", "timeout"])
def test_claude_resume_polls_bounded_delayed_persistence_with_one_runner_call(
    tmp_path: Path,
    process_outcome: str,
) -> None:
    nonce = "b" * 32
    transcript = Path("C:/claude/project/transcript.jsonl")
    baseline = _resume_projection(
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        cursor="cursor-before",
        native_hash="hash-before",
    )
    source = SequencedClaudeResumeSource(
        _resume_projection(
            origin_kind=OriginKind.BRIDGE_CONTINUATION,
            new_user_content=f"delayed resume {nonce}",
        ),
        [None, transcript],
    )
    runner_calls = 0
    sleeps: list[float] = []
    times = iter([0.0, 0.0])

    def runner(args: list[str], **kwargs: Any) -> Any:
        nonlocal runner_calls
        runner_calls += 1
        if process_outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd="claude", timeout=180)
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    characterize_module._resume_claude_characterization(
        source,
        baseline_projection=baseline,
        native_id=CLAUDE_ID,
        bridge_id="bridge-1",
        resume_nonce=nonce,
        executable="C:/bin/claude.cmd",
        cwd=tmp_path,
        runner=runner,
        verification_timeout=1.0,
        verification_poll_interval=0.1,
        monotonic=times.__next__,
        sleep=sleeps.append,
    )

    assert runner_calls == 1
    assert source.find_calls == [CLAUDE_ID, CLAUDE_ID]
    assert source.parse_calls == [transcript]
    assert sleeps == [0.1]


@pytest.mark.parametrize(
    ("post_projection", "expected_code"),
    [
        (
            _resume_projection(
                origin_kind=OriginKind.BRIDGE_CONTINUATION,
                cursor="cursor-before",
                native_hash="hash-before",
            ),
            "claude_resume_not_advanced",
        ),
        (
            _resume_projection(
                origin_kind=OriginKind.BRIDGE_CONTINUATION,
                new_user_content="wrong nonce",
            ),
            "claude_resume_nonce_mismatch",
        ),
        (
            _resume_projection(
                origin_kind=OriginKind.BRIDGE_CONTINUATION,
                bridge_id="different",
                new_user_content="a" * 32,
            ),
            "claude_resume_marker_mismatch",
        ),
        (
            _resume_projection(
                origin_kind=OriginKind.BRIDGE_CONTINUATION,
                native_id="different",
                new_user_content="a" * 32,
            ),
            "claude_resume_identity_mismatch",
        ),
    ],
)
def test_claude_resume_durable_proof_fails_closed(
    tmp_path: Path,
    post_projection: Any,
    expected_code: str,
) -> None:
    baseline = _resume_projection(
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        cursor="cursor-before",
        native_hash="hash-before",
    )
    source = FakeClaudeResumeSource(post_projection)
    runner_calls = 0

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal runner_calls
        runner_calls += 1
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    with pytest.raises(PlaceholderCreationError, match=expected_code):
        characterize_module._resume_claude_characterization(
            source,
            baseline_projection=baseline,
            native_id=CLAUDE_ID,
            bridge_id="bridge-1",
            resume_nonce="a" * 32,
            executable="C:/bin/claude.cmd",
            cwd=tmp_path,
            runner=runner,
            verification_timeout=0.0,
        )

    assert runner_calls == 1


def test_claude_resume_absent_target_preserves_sanitized_process_failure(
    tmp_path: Path,
) -> None:
    secret = "sk-proj-THIS-MUST-NOT-LEAK-1234567890"
    baseline = _resume_projection(
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        cursor="cursor-before",
        native_hash="hash-before",
    )
    source = FakeClaudeResumeSource(baseline, found=None)
    runner_calls = 0

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal runner_calls
        runner_calls += 1
        return subprocess.CompletedProcess(
            args,
            1,
            stdout=json.dumps({
                "subtype": "error_max_budget_usd",
                "total_cost_usd": 0.5,
                "duration_ms": 847,
                "num_turns": 1,
                "result": secret,
                "usage": {"input_tokens": 9999},
            }),
            stderr=secret,
        )

    with pytest.raises(PlaceholderCreationError) as raised:
        characterize_module._resume_claude_characterization(
            source,
            baseline_projection=baseline,
            native_id=CLAUDE_ID,
            bridge_id="bridge-1",
            resume_nonce="a" * 32,
            executable="C:/bin/claude.cmd",
            cwd=tmp_path,
            runner=runner,
            verification_timeout=0.0,
        )

    error = raised.value
    assert error.code == "claude_resume_error_max_budget_usd"
    assert error.observed_cost_usd == 0.5
    assert error.duration_ms == 847.0
    assert error.num_turns == 1
    assert secret not in str(error)
    assert "usage" not in json.dumps(vars(error), sort_keys=True)
    assert runner_calls == 1


def test_characterization_report_preserves_only_sanitized_claude_failure_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-proj-THIS-MUST-NOT-LEAK-1234567890"
    marker = "HERMES_SESSION_BRIDGE_V1:payload.signature"
    failure = PlaceholderCreationError(
        "claude_process_error_max_budget_usd",
        observed_cost_usd=0.24987,
        duration_ms=42123.0,
        num_turns=2,
    )
    failure.result = secret  # type: ignore[attr-defined]
    failure.prompt = marker  # type: ignore[attr-defined]
    failure.usage = {"input_tokens": 9876}  # type: ignore[attr-defined]
    failure.stderr = secret  # type: ignore[attr-defined]
    failure.path = "C:/private/session.jsonl"  # type: ignore[attr-defined]

    def fail_claude(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setattr(characterize_module, "resolve_cli_executable", lambda value: value)
    monkeypatch.setattr(characterize_module, "_cli_version", lambda args: "test")
    monkeypatch.setattr(characterize_module, "_characterize_claude", fail_claude)
    monkeypatch.setattr(characterize_module, "_characterize_codex", lambda *args, **kwargs: None)

    with pytest.raises(LiveCharacterizationError) as raised:
        run_live_characterization(
            report_root=tmp_path,
            claude_projects_root=tmp_path,
            claude_executable="fake-claude",
            codex_executable="fake-codex",
            cwd=tmp_path,
        )

    report = json.loads(raised.value.report_path.read_text(encoding="utf-8"))
    claude = report["providers"]["claude"]
    assert claude["error_code"] == "claude_process_error_max_budget_usd"
    assert claude["observed_cost_usd"] == 0.24987
    assert claude["duration_ms"] == 42123.0
    assert claude["num_turns"] == 2
    serialized = json.dumps(report, sort_keys=True)
    assert secret not in serialized
    assert marker not in serialized
    assert "input_tokens" not in serialized
    assert "session.jsonl" not in serialized
    assert "result" not in serialized
    assert "prompt" not in serialized
    assert "usage" not in serialized
    assert "stderr" not in serialized
    assert "path" not in serialized


def test_claude_live_metrics_accept_only_finite_numeric_result_fields() -> None:
    metrics = _claude_result_metrics(
        subprocess.CompletedProcess(
            ["claude"],
            0,
            stdout=(
                '{"total_cost_usd":0.18236025,"duration_ms":12601,'
                '"num_turns":1,'
                '"result":"private transcript"}'
            ),
            stderr="private",
        )
    )

    assert metrics == {
        "cost_usd": 0.18236025,
        "duration_ms": 12601.0,
        "num_turns": 1,
    }
    assert _claude_result_metrics(
        subprocess.CompletedProcess(
            ["claude"], 0, stdout='{"total_cost_usd":"secret"}', stderr=""
        )
    ) == {}


def test_live_characterization_resolves_windows_command_shims_without_shell() -> None:
    resolved = resolve_cli_executable(
        "claude",
        which=lambda name: (
            "C:/Users/test/AppData/Roaming/npm/claude.cmd"
            if name == "claude"
            else None
        ),
    )

    assert resolved.endswith("claude.cmd")
    assert resolve_cli_executable(
        "C:/tools/claude.exe", which=lambda _: None
    ) == "C:/tools/claude.exe"
