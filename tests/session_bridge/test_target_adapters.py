from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import traceback
from types import SimpleNamespace
from typing import Any

import pytest

if os.name == "nt" and "USERPROFILE" not in os.environ:
    os.environ["USERPROFILE"] = os.environ["HOME"]

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
    SidebarThreadVerifier,
    SidebarVerificationError,
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
    ProjectedMessage,
    Provider,
    SessionProjection,
    encode_bridge_marker,
)
from session_bridge.sidebar import VerifiedSidebarThread


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
        projection_title: str | None = "Mirror title",
        projection_cwd: str | None = None,
    ) -> None:
        self.found = found
        self.projection_native_id = projection_native_id
        self.bridge_id = bridge_id
        self.projection_title = projection_title
        self.projection_cwd = projection_cwd
        self.find_calls: list[str] = []

    def find_native_session(self, native_id: str) -> Path | None:
        self.find_calls.append(native_id)
        return self.found

    def parse(self, path: Path) -> Any:
        assert path == self.found
        projection = SimpleNamespace(
            native_id=self.projection_native_id,
            title=self.projection_title,
            cwd=self.projection_cwd,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=self.bridge_id,
        )
        return SimpleNamespace(projection=projection)

    def projection_has_exact_marker(self, projection: Any, marker: str) -> bool:
        return True

    def projection_has_marker_payload(
        self, projection: Any, payload: BridgeMarkerPayload
    ) -> bool:
        return True


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
        self.last_started_turn_id: str | None = None
        self.completion_notification_taken = False

    def request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        self.calls.append((method, deepcopy(params), timeout))
        response = self.responses[method].pop(0)
        if isinstance(response, Exception):
            raise response
        if method == "turn/start":
            turn = response.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                self.last_started_turn_id = turn["id"]
        return deepcopy(response)

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None:
        if self.last_started_turn_id is None:
            return None
        if self.completion_notification_taken:
            raise AssertionError("completion notification consumed more than once")
        self.completion_notification_taken = True
        return {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": self.last_started_turn_id,
                    "status": "completed",
                },
            },
        }


class ExperimentalSearchClient(FakeRequestClient):
    def __init__(self, responses: dict[str, list[dict[str, Any] | Exception]]) -> None:
        super().__init__(responses)
        self._initialized = False
        self.initialize_calls: list[dict[str, Any]] = []

    def initialize(self, **kwargs: Any) -> dict[str, Any]:
        self.initialize_calls.append(deepcopy(kwargs))
        self._initialized = True
        return {"userAgent": "synthetic"}


class CompletionAwareFakeRequestClient(FakeRequestClient):
    def __init__(
        self,
        responses: dict[str, list[dict[str, Any] | Exception]],
        notifications: list[dict[str, Any] | None],
    ) -> None:
        super().__init__(responses)
        self.notifications = list(notifications)
        self.events: list[str] = []

    def request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        self.events.append(f"request:{method}")
        return super().request(method, params, timeout)

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None:
        self.events.append("notification")
        return deepcopy(self.notifications.pop(0))


class FakeCodexRpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class StructuralSidebarInventory:
    def __init__(self) -> None:
        self.list_calls = 0

    def list_sidebar_inventory(
        self, *, deadline: float | None, page_cap: int
    ) -> list[Any]:
        self.list_calls += 1
        return []

    def find_sidebar_thread(
        self, thread_id: str, *, deadline: float | None, page_cap: int
    ) -> Any | None:
        return None

    def read_sidebar_thread(self, summary: Any, *, deadline: float | None) -> Any:
        raise AssertionError("empty structural inventory must not be read")


class MutableSidebarInventory:
    def __init__(self) -> None:
        self.visible = False
        self.list_deadlines: list[float | None] = []
        self.read_deadlines: list[float | None] = []
        self.summary = SimpleNamespace(native_id=CODEX_ID)

    def list_sidebar_inventory(
        self, *, deadline: float | None, page_cap: int
    ) -> list[Any]:
        self.list_deadlines.append(deadline)
        return [self.summary] if self.visible else []

    def find_sidebar_thread(
        self, thread_id: str, *, deadline: float | None, page_cap: int
    ) -> Any | None:
        return self.summary if self.visible and thread_id == CODEX_ID else None

    def read_sidebar_thread(
        self, summary: Any, *, deadline: float | None
    ) -> SessionProjection:
        self.read_deadlines.append(deadline)
        return SessionProjection(
            provider=Provider.CODEX,
            native_id=summary.native_id,
            title="Shared task",
            cwd=None,
            started_at=0.0,
            last_active=0.0,
            messages=(ProjectedMessage(
                native_event_id="marker",
                ordinal=0,
                role="user",
                content=encode_bridge_marker(_sidebar_expected(), SECRET),
                timestamp=0.0,
            ),),
        )


def _assert_sanitized_exception_has_no_chain(
    error: BaseException, *, secret: str
) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert secret not in formatted


def _symlink_or_skip(
    link: Path, target: Path, *, target_is_directory: bool = False
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable in this Windows environment: {exc}")
    assert link.is_symlink()


def _write_claude_marker_transcript(
    path: Path, *, payload: BridgeMarkerPayload
) -> None:
    marker = encode_bridge_marker(payload, SECRET)
    path.write_text(
        json.dumps({
            "type": "user",
            "sessionId": CLAUDE_ID,
            "timestamp": "2026-07-14T00:00:00Z",
            "message": {"content": marker},
        })
        + "\n",
        encoding="utf-8",
    )


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


def _codex_inventory(
    *,
    title: str = "Mirror title",
    native_id: str = CODEX_ID,
    cwd: str | None = "C:/valid",
):
    return {
        "data": [
            {
                "id": native_id,
                "title": title,
                "cwd": cwd,
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
    target_provider: Provider = Provider.CODEX,
    policy_generation: int = 1,
    turn_id: str = "turn-registration",
    turn_status: str = "completed",
) -> dict[str, Any]:
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_session_id,
            target_provider=target_provider,
            policy_generation=policy_generation,
        ),
        SECRET,
    )
    return _codex_read(
        native_id=native_id,
        turns=[
            {
                "id": turn_id,
                "status": turn_status,
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
            require_registration_turn=kwargs.pop("require_registration_turn", False),
            **kwargs,
        ),
        client,
    )


def _sidebar_expected(
    *,
    source_session_id: str = "claude:source-1",
    bridge_id: str = "bridge-1",
) -> BridgeMarkerPayload:
    return BridgeMarkerPayload(
        bridge_id=bridge_id,
        source_session_id=source_session_id,
        target_provider=Provider.CODEX,
        policy_generation=1,
    )


def test_sidebar_thread_verifier_reads_only_exact_authenticated_thread() -> None:
    client = FakeRequestClient({
        "thread/list": [_codex_inventory()],
        "thread/read": [_codex_signed_read()],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    verified = verifier.verify_thread(
        thread_id=CODEX_ID,
        expected=_sidebar_expected(),
    )

    assert verified == VerifiedSidebarThread(
        thread_id=CODEX_ID,
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
    )
    assert [method for method, _, _ in client.calls] == [
        "thread/list",
        "thread/read",
    ]
    assert all(method not in {"thread/start", "thread/name/set"} for method, _, _ in client.calls)


def test_sidebar_thread_verifier_accepts_structural_read_only_inventory() -> None:
    inventory = StructuralSidebarInventory()
    verifier = SidebarThreadVerifier(
        inventory,
        marker_secret=SECRET,
        reconciliation_interval=30.0,
        monotonic=lambda: 0.0,
    )

    assert verifier.find_by_marker(_sidebar_expected()) is None
    assert inventory.list_calls == 1


def test_sidebar_marker_lookup_page_cap_is_retryable_not_false_zero() -> None:
    client = FakeRequestClient({
        "thread/list": [{**_codex_inventory(), "nextCursor": "more"}],
    })
    source = CodexSourceAdapter(client, marker_secret=SECRET, monotonic=lambda: 0.0)
    verifier = SidebarThreadVerifier(
        source,
        marker_secret=SECRET,
        reconciliation_interval=30.0,
        inventory_page_cap=1,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "bridge_temporarily_unavailable"
    assert [method for method, _, _ in client.calls] == ["thread/list"]


def test_sidebar_marker_lookup_thread_cap_stops_before_thread_reads() -> None:
    first = _codex_inventory()["data"][0]
    second = _codex_inventory(
        native_id="33333333-3333-4333-8333-333333333333"
    )["data"][0]
    client = FakeRequestClient({
        "thread/list": [{"data": [first, second]}, {"data": []}],
    })
    source = CodexSourceAdapter(client, marker_secret=SECRET, monotonic=lambda: 0.0)
    verifier = SidebarThreadVerifier(
        source,
        marker_secret=SECRET,
        reconciliation_interval=30.0,
        inventory_thread_cap=1,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "bridge_temporarily_unavailable"
    assert all(method != "thread/read" for method, _, _ in client.calls)


def test_sidebar_marker_lookup_deadline_before_list_makes_no_request() -> None:
    client = FakeRequestClient({})
    source = CodexSourceAdapter(client, marker_secret=SECRET, monotonic=lambda: 2.0)
    verifier = SidebarThreadVerifier(
        source,
        marker_secret=SECRET,
        reconciliation_interval=1.0,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "bridge_temporarily_unavailable"
    assert client.calls == []


def test_zero_interval_sidebar_lookup_still_has_finite_read_budget() -> None:
    client = FakeRequestClient({})
    source = CodexSourceAdapter(client, marker_secret=SECRET, monotonic=lambda: 31.0)
    verifier = SidebarThreadVerifier(
        source,
        marker_secret=SECRET,
        reconciliation_interval=0,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "bridge_temporarily_unavailable"
    assert client.calls == []


def test_sidebar_marker_lookup_deadline_after_list_is_retryable() -> None:
    ticks = iter((0.0, 2.0))
    client = FakeRequestClient({"thread/list": [_codex_inventory()]})
    source = CodexSourceAdapter(
        client,
        marker_secret=SECRET,
        monotonic=lambda: next(ticks),
    )
    verifier = SidebarThreadVerifier(
        source,
        marker_secret=SECRET,
        reconciliation_interval=1.0,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "bridge_temporarily_unavailable"
    assert [method for method, _, _ in client.calls] == ["thread/list"]


def test_sidebar_marker_lookup_deadline_between_list_and_read_never_false_zero() -> None:
    ticks = iter((0.0, 0.0, 0.0, 0.0, 2.0))
    client = FakeRequestClient({
        "thread/list": [_codex_inventory(), {"data": []}],
    })
    source = CodexSourceAdapter(
        client,
        marker_secret=SECRET,
        monotonic=lambda: next(ticks),
    )
    verifier = SidebarThreadVerifier(
        source,
        marker_secret=SECRET,
        reconciliation_interval=1.0,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "bridge_temporarily_unavailable"
    assert [method for method, _, _ in client.calls] == ["thread/list", "thread/list"]


def test_sidebar_marker_lookup_deadline_after_read_is_retryable_not_a_match() -> None:
    ticks = iter((0.0, 0.0, 0.0, 0.0, 0.0, 2.0))
    client = FakeRequestClient({
        "thread/list": [_codex_inventory(), {"data": []}],
        "thread/read": [_codex_signed_read()],
    })
    source = CodexSourceAdapter(
        client,
        marker_secret=SECRET,
        monotonic=lambda: next(ticks),
    )
    verifier = SidebarThreadVerifier(
        source,
        marker_secret=SECRET,
        reconciliation_interval=1.0,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "bridge_temporarily_unavailable"
    assert [method for method, _, _ in client.calls] == [
        "thread/list",
        "thread/list",
        "thread/read",
    ]


def test_two_sidebar_claim_markers_reuse_one_bounded_inventory_snapshot() -> None:
    other_id = "33333333-3333-4333-8333-333333333333"
    first = _codex_inventory()["data"][0]
    second = _codex_inventory(native_id=other_id)["data"][0]
    client = FakeRequestClient({
        "thread/list": [{"data": [first, second]}, {"data": []}],
        "thread/read": [
            _codex_signed_read(),
            _codex_signed_read(
                native_id=other_id,
                bridge_id="bridge-2",
                source_session_id="hermes-source-2",
            ),
        ],
    })
    now = [0.0]
    source = CodexSourceAdapter(client, marker_secret=SECRET, monotonic=lambda: now[0])
    verifier = SidebarThreadVerifier(
        source,
        marker_secret=SECRET,
        reconciliation_interval=0,
        monotonic=lambda: now[0],
    )

    assert verifier.find_by_marker(_sidebar_expected()) is not None
    now[0] = 0.5
    assert verifier.find_by_marker(BridgeMarkerPayload(
        "bridge-2", "hermes-source-2", Provider.CODEX, 1
    )) == VerifiedSidebarThread(other_id, "hermes-source-2", "bridge-2")
    assert [method for method, _, _ in client.calls] == [
        "thread/list",
        "thread/list",
        "thread/read",
        "thread/read",
    ]


def test_sidebar_snapshot_ttl_is_capped_below_lease_retry_window() -> None:
    now = [0.0]
    inventory = MutableSidebarInventory()
    verifier = SidebarThreadVerifier(
        inventory,
        marker_secret=SECRET,
        reconciliation_interval=600.0,
        monotonic=lambda: now[0],
    )

    assert verifier.find_by_marker(_sidebar_expected()) is None
    assert inventory.list_deadlines == [600.0]

    inventory.visible = True
    now[0] = 29.0
    assert verifier.find_by_marker(_sidebar_expected()) is None
    assert inventory.list_deadlines == [600.0]

    now[0] = 31.0
    assert verifier.find_by_marker(_sidebar_expected()) == VerifiedSidebarThread(
        CODEX_ID,
        "claude:source-1",
        "bridge-1",
    )
    assert inventory.list_deadlines == [600.0, 631.0]
    assert inventory.read_deadlines == [631.0]


@pytest.mark.parametrize(
    ("read", "code"),
    [
        (_codex_signed_read(source_session_id="claude:other"), "source_identity_mismatch"),
        (_codex_signed_read(bridge_id="bridge-other"), "source_identity_mismatch"),
        (_codex_signed_read(target_provider=Provider.CLAUDE), "provider_mismatch"),
        (_codex_signed_read(policy_generation=2), "provider_mismatch"),
    ],
)
def test_sidebar_thread_verifier_rejects_wrong_authenticated_lineage(
    read: dict[str, Any], code: str
) -> None:
    client = FakeRequestClient({
        "thread/list": [_codex_inventory()],
        "thread/read": [read],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.verify_thread(thread_id=CODEX_ID, expected=_sidebar_expected())

    assert raised.value.code == code


def test_sidebar_thread_verifier_rejects_invalid_signature_and_duplicate_marker() -> None:
    valid = encode_bridge_marker(_sidebar_expected(), SECRET)
    invalid = encode_bridge_marker(_sidebar_expected(), b"different-secret")
    for content in (invalid, f"{valid}\n{valid}"):
        client = FakeRequestClient({
            "thread/list": [_codex_inventory()],
            "thread/read": [_codex_read(turns=[{
                "id": "registration",
                "items": [{
                    "type": "userMessage",
                    "id": "item-1",
                    "content": [{"type": "text", "text": content}],
                }],
            }])],
        })
        verifier = SidebarThreadVerifier(
            CodexSourceAdapter(client, marker_secret=SECRET),
            marker_secret=SECRET,
            reconciliation_interval=0,
        )

        with pytest.raises(SidebarVerificationError) as raised:
            verifier.verify_thread(thread_id=CODEX_ID, expected=_sidebar_expected())

        assert raised.value.code == "marker_conflict"


def test_sidebar_marker_lookup_recovers_one_exact_thread_read_only() -> None:
    client = FakeRequestClient({
        "thread/list": [_codex_inventory(), {"data": []}],
        "thread/read": [_codex_signed_read()],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    assert verifier.find_by_marker(_sidebar_expected()) == VerifiedSidebarThread(
        CODEX_ID,
        "claude:source-1",
        "bridge-1",
    )
    assert [method for method, _, _ in client.calls] == [
        "thread/list",
        "thread/list",
        "thread/read",
    ]


def test_sidebar_marker_lookup_searches_exact_marker_before_thread_reads() -> None:
    expected = _sidebar_expected()
    marker = encode_bridge_marker(expected, SECRET)
    row = _codex_inventory()["data"][0]
    client = ExperimentalSearchClient({
        "thread/search": [
            {"data": [{"thread": row, "snippet": f"Signed marker: {marker}"}]},
            {"data": []},
        ],
        "thread/read": [_codex_signed_read()],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=30.0,
    )

    assert verifier.find_by_marker(expected) == VerifiedSidebarThread(
        CODEX_ID,
        "claude:source-1",
        "bridge-1",
    )
    assert [method for method, _, _ in client.calls] == [
        "thread/search",
        "thread/search",
        "thread/read",
    ]
    search_calls = [params for method, params, _ in client.calls if method == "thread/search"]
    assert [params["archived"] for params in search_calls] == [False, True]
    unsigned_marker = marker.rsplit(".", 1)[0]
    assert all(params["searchTerm"] == unsigned_marker for params in search_calls)
    assert all(method != "thread/list" for method, _, _ in client.calls)
    assert client.initialize_calls == [
        {"capabilities": {"experimentalApi": True}}
    ]


def test_sidebar_marker_search_fails_closed_on_matching_invalid_signature() -> None:
    expected = _sidebar_expected()
    valid = encode_bridge_marker(expected, SECRET)
    invalid = encode_bridge_marker(expected, b"different-secret")
    row = _codex_inventory()["data"][0]
    client = ExperimentalSearchClient({
        "thread/search": [
            {"data": [{"thread": row, "snippet": f"Signed marker: {invalid}"}]},
            {"data": []},
        ],
        "thread/read": [_codex_read(turns=[{
            "id": "registration",
            "items": [{
                "type": "userMessage",
                "id": "item-1",
                "content": [{"type": "text", "text": invalid}],
            }],
        }])],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=30.0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(expected)

    assert raised.value.code == "marker_conflict"
    search_calls = [params for method, params, _ in client.calls if method == "thread/search"]
    assert all(params["searchTerm"] == valid.rsplit(".", 1)[0] for params in search_calls)


def test_sidebar_marker_lookup_ignores_archived_duplicate() -> None:
    other_id = "33333333-3333-4333-8333-333333333333"
    client = FakeRequestClient({
        "thread/list": [
            _codex_inventory(),
            _codex_inventory(native_id=other_id),
        ],
        "thread/read": [
            _codex_signed_read(),
            _codex_signed_read(native_id=other_id),
        ],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    assert verifier.find_by_marker(_sidebar_expected()) == VerifiedSidebarThread(
        CODEX_ID,
        "claude:source-1",
        "bridge-1",
    )
    assert all(method not in {"thread/start", "thread/name/set"} for method, _, _ in client.calls)


def test_sidebar_marker_lookup_rejects_multiple_active_authenticated_threads() -> None:
    other_id = "33333333-3333-4333-8333-333333333333"
    first = _codex_inventory()["data"][0]
    second = _codex_inventory(native_id=other_id)["data"][0]
    client = FakeRequestClient({
        "thread/list": [
            {"data": [first, second]},
            {"data": []},
        ],
        "thread/read": [
            _codex_signed_read(),
            _codex_signed_read(native_id=other_id),
        ],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "marker_conflict"


@pytest.mark.parametrize(
    ("observed", "code"),
    [
        (
            BridgeMarkerPayload("bridge-other", "claude:source-1", Provider.CODEX, 1),
            "source_identity_mismatch",
        ),
        (
            BridgeMarkerPayload("bridge-1", "claude:other", Provider.CODEX, 1),
            "source_identity_mismatch",
        ),
        (
            BridgeMarkerPayload("bridge-1", "claude:source-1", Provider.CLAUDE, 1),
            "provider_mismatch",
        ),
        (
            BridgeMarkerPayload("bridge-1", "claude:source-1", Provider.CODEX, 2),
            "provider_mismatch",
        ),
    ],
)
def test_sidebar_marker_lookup_rejects_authenticated_related_near_match(
    observed: BridgeMarkerPayload,
    code: str,
) -> None:
    client = FakeRequestClient({
        "thread/list": [_codex_inventory(), {"data": []}],
        "thread/read": [_codex_signed_read(
            bridge_id=observed.bridge_id,
            source_session_id=observed.source_session_id,
            target_provider=observed.target_provider,
            policy_generation=observed.policy_generation,
        )],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == code


@pytest.mark.parametrize(
    "near",
    [
        BridgeMarkerPayload("bridge-other", "claude:source-1", Provider.CODEX, 1),
        BridgeMarkerPayload("bridge-1", "claude:other", Provider.CODEX, 1),
        BridgeMarkerPayload("bridge-1", "claude:source-1", Provider.CLAUDE, 1),
        BridgeMarkerPayload("bridge-1", "claude:source-1", Provider.CODEX, 2),
    ],
)
def test_sidebar_marker_lookup_rejects_exact_plus_related_near_match(
    near: BridgeMarkerPayload,
) -> None:
    exact = encode_bridge_marker(_sidebar_expected(), SECRET)
    related = encode_bridge_marker(near, SECRET)
    client = FakeRequestClient({
        "thread/list": [_codex_inventory(), {"data": []}],
        "thread/read": [_codex_read(turns=[{
            "id": "registration",
            "items": [{
                "type": "userMessage",
                "id": "item-1",
                "content": [{"type": "text", "text": f"{exact}\n{related}"}],
            }],
        }])],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "marker_conflict"


def test_sidebar_marker_lookup_ignores_fully_unrelated_authenticated_marker() -> None:
    client = FakeRequestClient({
        "thread/list": [_codex_inventory(), {"data": []}],
        "thread/read": [_codex_signed_read(
            bridge_id="bridge-unrelated",
            source_session_id="hermes-unrelated",
        )],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    assert verifier.find_by_marker(_sidebar_expected()) is None
    assert all(method not in {"thread/start", "thread/name/set"} for method, _, _ in client.calls)


def test_sidebar_marker_lookup_ignores_multiple_fully_unrelated_markers() -> None:
    unrelated = [
        encode_bridge_marker(
            BridgeMarkerPayload("bridge-unrelated-a", "hermes-a", Provider.CODEX, 1),
            SECRET,
        ),
        encode_bridge_marker(
            BridgeMarkerPayload("bridge-unrelated-b", "hermes-b", Provider.CODEX, 1),
            SECRET,
        ),
    ]
    client = FakeRequestClient({
        "thread/list": [_codex_inventory(), {"data": []}],
        "thread/read": [_codex_read(turns=[{
            "id": "registration",
            "items": [{
                "type": "userMessage",
                "id": "item-1",
                "content": [{"type": "text", "text": "\n".join(unrelated)}],
            }],
        }])],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    assert verifier.find_by_marker(_sidebar_expected()) is None


@pytest.mark.parametrize("include_valid", [False, True])
def test_sidebar_marker_lookup_never_turns_expected_invalid_signature_into_zero(
    include_valid: bool,
) -> None:
    valid = encode_bridge_marker(_sidebar_expected(), SECRET)
    invalid = encode_bridge_marker(_sidebar_expected(), b"different-secret")
    content = f"{valid}\n{invalid}" if include_valid else invalid
    client = FakeRequestClient({
        "thread/list": [_codex_inventory(), {"data": []}],
        "thread/read": [_codex_read(turns=[{
            "id": "registration",
            "items": [{
                "type": "userMessage",
                "id": "item-1",
                "content": [{"type": "text", "text": content}],
            }],
        }])],
    })
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(_sidebar_expected())

    assert raised.value.code == "marker_conflict"


@pytest.mark.parametrize(
    "expected",
    [
        BridgeMarkerPayload("bridge-1", "claude:source-1", Provider.CLAUDE, 1),
        BridgeMarkerPayload("bridge-1", "claude:source-1", Provider.CODEX, 2),
    ],
)
def test_sidebar_verifier_requires_codex_policy_generation_one(
    expected: BridgeMarkerPayload,
) -> None:
    client = FakeRequestClient({})
    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        reconciliation_interval=0,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.find_by_marker(expected)

    assert raised.value.code == "provider_mismatch"
    assert client.calls == []


def test_sidebar_thread_verifier_bounds_not_indexed_polling() -> None:
    client = FakeRequestClient({
        "thread/list": [
            {"data": []},
            {"data": []},
            {"data": []},
            {"data": []},
            {"data": []},
            {"data": []},
        ],
    })
    now = [0.0]
    sleeps: list[float] = []
    def advance(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    verifier = SidebarThreadVerifier(
        CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            monotonic=lambda: now[0],
        ),
        marker_secret=SECRET,
        reconciliation_interval=2.0,
        poll_interval=1.0,
        monotonic=lambda: now[0],
        sleep=advance,
    )

    with pytest.raises(SidebarVerificationError) as raised:
        verifier.verify_thread(thread_id=CODEX_ID, expected=_sidebar_expected())

    assert raised.value.code == "native_task_not_indexed"
    assert sleeps == [1.0, 1.0]
    assert [method for method, _, _ in client.calls] == ["thread/list"] * 4


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


def test_claude_direct_node_runtime_keeps_registration_as_one_literal_argv_entry(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / f"{CLAUDE_ID}.jsonl"
    source = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    node = tmp_path / "node.exe"
    cli = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    cli.parent.mkdir(parents=True)
    node.write_bytes(b"")
    cli.write_text("", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def direct_runtime_runner(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), dict(kwargs)))
        prompt = args[-1]
        transcript.write_text(
            json.dumps({
                "type": "custom-title",
                "sessionId": CLAUDE_ID,
                "customTitle": "Mirror title",
            })
            + "\n"
            + json.dumps({
                "type": "user",
                "sessionId": CLAUDE_ID,
                "timestamp": "2026-07-14T00:00:00Z",
                "message": {"content": prompt},
            })
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    result = ClaudeTargetAdapter(
        source,
        marker_secret=SECRET,
        claude_executable=(str(node.resolve()), str(cli.resolve())),
        runner=direct_runtime_runner,
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
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == [str(node.resolve()), str(cli.resolve())]
    assert args.count(str(node.resolve())) == 1
    assert args.count(str(cli.resolve())) == 1
    assert "\r" not in args[-1] and "\n" not in args[-1]
    assert kwargs["shell"] is False
    projection = source.parse(transcript).projection
    assert projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
    assert projection.origin_bridge_id == "bridge-1"


@pytest.mark.parametrize("suffix", [".cmd", ".ps1"])
def test_claude_target_resolves_recognized_npm_shim_to_literal_node_argv(
    tmp_path: Path, suffix: str
) -> None:
    npm_root = tmp_path / "npm & literal"
    cli = npm_root / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node = npm_root / "node.exe"
    shim = npm_root / f"claude{suffix}"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    node.write_bytes(b"")
    shim.write_text("recognized npm shim", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), dict(kwargs)))
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    ClaudeTargetAdapter(
        FakeClaudeSource(),
        marker_secret=SECRET,
        claude_executable=str(shim),
        runner=runner,
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    ).create_placeholder(
        native_id=CLAUDE_ID,
        title="Mirror title",
        source_session_id="codex:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    args, kwargs = calls[0]
    assert args[:2] == [str(node.resolve()), str(cli.resolve())]
    assert args[-1].count("HERMES_SESSION_BRIDGE_V1:") == 1
    assert kwargs["shell"] is False


@pytest.mark.parametrize("suffix", [".cmd", ".ps1", ".bat"])
def test_claude_target_rejects_unrecognized_explicit_shell_shim(
    tmp_path: Path, suffix: str
) -> None:
    shim = tmp_path / f"claude{suffix}"
    shim.write_text("invoke arbitrary shell content", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsupported_shell_shim"):
        ClaudeTargetAdapter(
            FakeClaudeSource(),
            marker_secret=SECRET,
            claude_executable=str(shim),
            runner=lambda *args, **kwargs: pytest.fail("runner must not execute"),
        )


def test_claude_target_default_command_cannot_bypass_unsafe_path_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "claude.cmd"
    shim.write_text("invoke arbitrary shell content", encoding="utf-8")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: str(shim) if name == "claude" else None,
    )

    with pytest.raises(RuntimeError, match="unsupported_shell_shim"):
        ClaudeTargetAdapter(
            FakeClaudeSource(),
            marker_secret=SECRET,
            runner=lambda *args, **kwargs: pytest.fail("runner must not execute"),
        )


def test_claude_direct_runtime_keeps_provider_metacharacters_literal() -> None:
    title = "Mirror & literal | title <unchanged> ^ %PATH%"
    source_session_id = "codex:source&literal|value"
    bridge_id = "bridge&literal|value"
    calls: list[tuple[list[str], dict[str, Any]]] = []

    adapter = ClaudeTargetAdapter(
        FakeClaudeSource(projection_title=title, bridge_id=bridge_id),
        marker_secret=SECRET,
        claude_executable=(
            "C:/runtime & literal/node.exe",
            "C:/runtime & literal/node_modules/@anthropic-ai/claude-code/cli.js",
        ),
        runner=lambda args, **kwargs: (
            calls.append((list(args), dict(kwargs)))
            or subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")
        ),
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )

    adapter.create_placeholder(
        native_id=CLAUDE_ID,
        title=title,
        source_session_id=source_session_id,
        bridge_id=bridge_id,
        policy_generation=1,
    )

    args, kwargs = calls[0]
    assert args[:2] == [
        "C:/runtime & literal/node.exe",
        "C:/runtime & literal/node_modules/@anthropic-ai/claude-code/cli.js",
    ]
    assert args[args.index("--name") + 1] == title
    assert source_session_id in args[-1]
    assert bridge_id in args[-1]
    assert "\r" not in args[-1] and "\n" not in args[-1]
    assert kwargs["shell"] is False


@pytest.mark.parametrize(
    ("source_session_id", "bridge_id"),
    [
        ("codex:source\rmalicious", "bridge-1"),
        ("codex:source-1", "bridge\nmalicious"),
    ],
)
def test_claude_rejects_crlf_in_prompt_identifiers(
    source_session_id: str, bridge_id: str
) -> None:
    runner_calls = 0

    def runner(*args: Any, **kwargs: Any) -> Any:
        nonlocal runner_calls
        runner_calls += 1
        raise AssertionError("invalid prompt identity reached provider")

    with pytest.raises(ValueError, match="single-line"):
        ClaudeTargetAdapter(
            FakeClaudeSource(), marker_secret=SECRET, runner=runner
        ).create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id=source_session_id,
            bridge_id=bridge_id,
            policy_generation=1,
        )

    assert runner_calls == 0


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


def test_claude_rejects_provider_ignored_requested_title() -> None:
    adapter = ClaudeTargetAdapter(
        FakeClaudeSource(projection_title="Provider ignored title"),
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "{}", ""),
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )

    with pytest.raises(PlaceholderCreationError, match="claude_target_title_mismatch"):
        adapter.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )


def test_claude_rejects_provider_misrouted_requested_cwd(tmp_path: Path) -> None:
    requested = tmp_path / "requested"
    observed = tmp_path / "observed"
    requested.mkdir()
    observed.mkdir()
    adapter = ClaudeTargetAdapter(
        FakeClaudeSource(projection_cwd=str(observed)),
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "{}", ""),
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )

    with pytest.raises(PlaceholderCreationError, match="claude_target_cwd_mismatch"):
        adapter.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
            cwd=requested,
        )


def test_claude_accepts_normalized_same_filesystem_cwd(tmp_path: Path) -> None:
    requested = tmp_path / "requested"
    requested.mkdir()
    alternate = str(requested.resolve()) + (r"\." if os.name == "nt" else "/.")
    result = ClaudeTargetAdapter(
        FakeClaudeSource(projection_cwd=alternate),
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "{}", ""),
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    ).create_placeholder(
        native_id=CLAUDE_ID,
        title="Mirror title",
        source_session_id="codex:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
        cwd=requested,
    )

    assert result.native_id == CLAUDE_ID


@pytest.mark.parametrize(
    ("source_session_id", "target_provider", "policy_generation"),
    [
        ("codex:different-source", Provider.CLAUDE, 1),
        ("codex:source-1", Provider.CODEX, 1),
        ("codex:source-1", Provider.CLAUDE, 2),
    ],
)
def test_claude_rejects_same_bridge_marker_with_wrong_signed_payload(
    tmp_path: Path,
    source_session_id: str,
    target_provider: Provider,
    policy_generation: int,
) -> None:
    transcript = tmp_path / f"{CLAUDE_ID}.jsonl"
    source = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    wrong_marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id="bridge-1",
            source_session_id=source_session_id,
            target_provider=target_provider,
            policy_generation=policy_generation,
        ),
        SECRET,
    )

    def runner(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        transcript.write_text(
            json.dumps({
                "type": "custom-title",
                "sessionId": CLAUDE_ID,
                "customTitle": "Mirror title",
            })
            + "\n"
            + json.dumps({
                "type": "user",
                "sessionId": CLAUDE_ID,
                "timestamp": "2026-07-14T00:00:00Z",
                "message": {"content": wrong_marker},
            })
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    adapter = ClaudeTargetAdapter(
        source,
        marker_secret=SECRET,
        runner=runner,
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )

    with pytest.raises(PlaceholderCreationError, match="claude_target_marker_mismatch"):
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


def test_claude_runner_boundary_drops_secret_exception_chain() -> None:
    secret = "sk-proj-CLAUDE-RUNNER-CHAIN-MUST-NOT-LEAK"

    def runner(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"provider request exposed {secret}")

    adapter = ClaudeTargetAdapter(
        FakeClaudeSource(found=None),
        marker_secret=SECRET,
        runner=runner,
        monotonic=lambda: 0.0,
        discovery_timeout=0.0,
    )

    with pytest.raises(PlaceholderCreationError) as raised:
        adapter.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    _assert_sanitized_exception_has_no_chain(raised.value, secret=secret)


def test_claude_parse_boundary_drops_secret_exception_chain() -> None:
    secret = "sk-proj-CLAUDE-PARSE-CHAIN-MUST-NOT-LEAK"

    class FailingSource(FakeClaudeSource):
        def parse(self, path: Path) -> Any:
            raise RuntimeError(f"provider transcript exposed {secret}")

    adapter = ClaudeTargetAdapter(
        FailingSource(),
        marker_secret=SECRET,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "{}", ""),
        monotonic=lambda: 0.0,
        discovery_timeout=0.0,
    )

    with pytest.raises(PlaceholderCreationError) as raised:
        adapter.create_placeholder(
            native_id=CLAUDE_ID,
            title="Mirror title",
            source_session_id="codex:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    _assert_sanitized_exception_has_no_chain(raised.value, secret=secret)


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
        "thread/list": [_codex_inventory(cwd=str(tmp_path.resolve()))],
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
    assert start["threadSource"] == "user"
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
        (
            _codex_read(),
            _codex_read(turns=[
                {
                    "id": "turn-registration",
                    "status": "completed",
                    "items": [],
                }
            ]),
            1,
        ),
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


@pytest.mark.parametrize(
    ("source_session_id", "target_provider", "policy_generation"),
    [
        ("claude:different-source", Provider.CODEX, 1),
        ("claude:source-1", Provider.CLAUDE, 1),
        ("claude:source-1", Provider.CODEX, 2),
    ],
)
def test_codex_rejects_same_bridge_marker_with_wrong_signed_payload(
    source_session_id: str,
    target_provider: Provider,
    policy_generation: int,
) -> None:
    adapter, client = _codex_adapter(
        {
            "thread/start": [{"thread": {"id": CODEX_ID}}],
            "thread/name/set": [{}],
            "thread/list": [_codex_inventory()],
            "thread/read": [
                _codex_signed_read(
                    source_session_id=source_session_id,
                    target_provider=target_provider,
                    policy_generation=policy_generation,
                )
            ],
        },
        require_registration_turn=False,
    )

    with pytest.raises(AmbiguousPlaceholderCreation, match="codex_target_marker_mismatch"):
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert [method for method, _, _ in client.calls].count("turn/start") == 0


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


def test_codex_source_kinds_enum_drift_retries_read_only_inventory_without_filter() -> None:
    adapter, client = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [
            FakeCodexRpcError(-32602, "invalid params: sourceKinds enum value"),
            _codex_inventory(),
        ],
        "thread/read": [_codex_signed_read()],
    })

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    assert result.native_id == CODEX_ID
    assert [method for method, _, _ in client.calls] == [
        "thread/start",
        "thread/name/set",
        "thread/list",
        "thread/list",
        "thread/read",
    ]
    assert client.calls[2][1] == {
        "archived": False,
        "sourceKinds": ["vscode", "appServer"],
    }
    assert client.calls[3][1] == {"archived": False}
    assert [method for method, _, _ in client.calls].count("thread/start") == 1
    assert [method for method, _, _ in client.calls].count("turn/start") == 0


def test_codex_source_kinds_fallback_still_requires_exact_signed_provenance() -> None:
    adapter, client = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [
            FakeCodexRpcError(-32602, "invalid params: sourceKinds enum value"),
            _codex_inventory(),
        ],
        "thread/read": [
            _codex_signed_read(source_session_id="claude:wrong-source")
        ],
    })

    with pytest.raises(AmbiguousPlaceholderCreation, match="codex_target_marker_mismatch"):
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert [method for method, _, _ in client.calls].count("thread/start") == 1
    assert [method for method, _, _ in client.calls].count("turn/start") == 0


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


def test_codex_rejects_provider_misrouted_requested_cwd(tmp_path: Path) -> None:
    requested = tmp_path / "requested"
    observed = tmp_path / "observed"
    requested.mkdir()
    observed.mkdir()
    adapter, client = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [_codex_inventory(cwd=str(observed))],
        "thread/read": [_codex_signed_read()],
    })

    with pytest.raises(AmbiguousPlaceholderCreation, match="codex_target_cwd_mismatch"):
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
            cwd=requested,
        )

    assert [method for method, _, _ in client.calls].count("thread/start") == 1
    assert [method for method, _, _ in client.calls].count("turn/start") == 0


def test_codex_accepts_normalized_same_filesystem_cwd(tmp_path: Path) -> None:
    requested = tmp_path / "requested"
    requested.mkdir()
    alternate = str(requested.resolve()) + (r"\." if os.name == "nt" else "/.")
    adapter, _ = _codex_adapter({
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [_codex_inventory(cwd=alternate)],
        "thread/read": [_codex_signed_read()],
    })

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
        cwd=requested,
    )

    assert result.native_id == CODEX_ID


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


def test_codex_production_default_waits_for_exact_registration_turn_completion() -> None:
    exact_turn_id = "turn-registration-exact"
    client = CompletionAwareFakeRequestClient(
        {
            "thread/start": [{"thread": {"id": CODEX_ID}}],
            "thread/name/set": [{}],
            "turn/start": [
                {"turn": {"id": exact_turn_id, "status": "inProgress"}}
            ],
            "thread/list": [_codex_inventory()],
            "thread/read": [_codex_signed_read(turn_id=exact_turn_id)],
        },
        notifications=[
            {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": "unrelated-turn", "status": "completed"},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": exact_turn_id, "status": "completed"},
                },
            },
        ],
    )
    adapter = CodexTargetAdapter(
        client,
        source_adapter=CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        clock=lambda: 1234.5,
        verification_timeout=1.0,
    )

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    assert result.used_registration_turn is True
    assert [method for method, _, _ in client.calls].count("turn/start") == 1
    assert client.events == [
        "request:thread/start",
        "request:thread/name/set",
        "request:turn/start",
        "notification",
        "notification",
        "request:thread/list",
        "request:thread/read",
    ]


def test_codex_registration_rejects_marker_on_a_different_returned_turn() -> None:
    returned_turn_id = "turn-registration-returned"
    client = CompletionAwareFakeRequestClient(
        {
            "thread/start": [{"thread": {"id": CODEX_ID}}],
            "thread/name/set": [{}],
            "turn/start": [
                {"turn": {"id": returned_turn_id, "status": "inProgress"}}
            ],
            "thread/list": [_codex_inventory()],
            "thread/read": [_codex_signed_read(turn_id="different-marker-turn")],
        },
        notifications=[
            {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": returned_turn_id, "status": "completed"}
                },
            }
        ],
    )
    adapter = CodexTargetAdapter(
        client,
        source_adapter=CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        verification_timeout=0.0,
    )

    with pytest.raises(
        AmbiguousPlaceholderCreation, match="codex_registration_turn_not_found"
    ):
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_codex_registration_uses_noncompleted_notification_as_wakeup(
    status: str,
) -> None:
    turn_id = f"turn-registration-{status}"
    client = CompletionAwareFakeRequestClient(
        {
            "thread/start": [{"thread": {"id": CODEX_ID}}],
            "thread/name/set": [{}],
            "turn/start": [{"turn": {"id": turn_id, "status": "inProgress"}}],
            "thread/list": [_codex_inventory()],
            "thread/read": [_codex_signed_read(turn_id=turn_id)],
        },
        notifications=[
            {
                "method": "turn/completed",
                "params": {"turn": {"id": turn_id, "status": status}},
            }
        ],
    )
    adapter = CodexTargetAdapter(
        client,
        source_adapter=CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        verification_timeout=0.0,
    )

    result = adapter.create_placeholder(
        title="Mirror title",
        source_session_id="claude:source-1",
        bridge_id="bridge-1",
        policy_generation=1,
    )

    assert result.used_registration_turn is True
    assert client.events.count("notification") == 1


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_codex_registration_rejects_noncompleted_exact_turn_in_thread_read(
    status: str,
) -> None:
    turn_id = f"turn-registration-read-{status}"
    client = CompletionAwareFakeRequestClient(
        {
            "thread/start": [{"thread": {"id": CODEX_ID}}],
            "thread/name/set": [{}],
            "thread/read": [
                _codex_signed_read(turn_id=turn_id, turn_status=status)
            ],
            "turn/start": [{"turn": {"id": turn_id, "status": "inProgress"}}],
            "thread/list": [_codex_inventory()],
        },
        notifications=[
            {
                "method": "turn/completed",
                "params": {"turn": {"id": turn_id, "status": "completed"}},
            }
        ],
    )
    adapter = CodexTargetAdapter(
        client,
        source_adapter=CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        verification_timeout=0.0,
    )

    with pytest.raises(
        AmbiguousPlaceholderCreation, match="codex_registration_turn_not_completed"
    ):
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_codex_registration_durable_terminal_status_fails_without_retry(
    status: str,
) -> None:
    turn_id = f"turn-registration-terminal-{status}"
    client = CompletionAwareFakeRequestClient(
        {
            "thread/start": [{"thread": {"id": CODEX_ID}}],
            "thread/name/set": [{}],
            "turn/start": [{"turn": {"id": turn_id, "status": "inProgress"}}],
            "thread/list": [_codex_inventory(), _codex_inventory()],
            "thread/read": [
                _codex_signed_read(turn_id=turn_id, turn_status=status),
                _codex_signed_read(turn_id=turn_id, turn_status="completed"),
            ],
        },
        notifications=[
            {
                "method": "turn/completed",
                "params": {"turn": {"id": turn_id, "status": "completed"}},
            }
        ],
    )
    times = iter([0.0, 0.0, 0.0])
    adapter = CodexTargetAdapter(
        client,
        source_adapter=CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
        verification_timeout=1.0,
        monotonic=times.__next__,
        sleep=lambda _: None,
    )

    with pytest.raises(
        AmbiguousPlaceholderCreation, match="codex_registration_turn_not_completed"
    ):
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert [method for method, _, _ in client.calls].count("thread/read") == 1


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


@pytest.mark.parametrize(
    "provider_stage",
    ["thread_start", "thread_name", "registration_turn", "thread_read"],
)
def test_codex_provider_boundaries_drop_secret_exception_chain(
    provider_stage: str,
) -> None:
    secret = f"sk-proj-CODEX-{provider_stage}-CHAIN-MUST-NOT-LEAK"
    responses: dict[str, list[dict[str, Any] | Exception]] = {
        "thread/start": [{"thread": {"id": CODEX_ID}}],
        "thread/name/set": [{}],
        "thread/list": [_codex_inventory()],
        "thread/read": [_codex_signed_read()],
        "turn/start": [{"turn": {"id": "turn-registration"}}],
    }
    require_registration_turn = False
    if provider_stage == "thread_start":
        responses["thread/start"] = [RuntimeError(f"provider exposed {secret}")]
    elif provider_stage == "thread_name":
        responses["thread/name/set"] = [RuntimeError(f"provider exposed {secret}")]
    elif provider_stage == "registration_turn":
        require_registration_turn = True
        responses["turn/start"] = [RuntimeError(f"provider exposed {secret}")]
    else:
        responses["thread/read"] = [RuntimeError(f"provider exposed {secret}")]

    adapter, _ = _codex_adapter(
        responses,
        require_registration_turn=require_registration_turn,
    )

    with pytest.raises(PlaceholderCreationError) as raised:
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    _assert_sanitized_exception_has_no_chain(raised.value, secret=secret)


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
        source_session_id="codex:source-1",
        policy_generation=1,
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
            source_session_id="codex:source-1",
            policy_generation=1,
            projects_root=tmp_path,
            quarantine_root=quarantine,
        )
    assert wrong.exists()


@pytest.mark.parametrize(
    ("source_session_id", "target_provider", "policy_generation"),
    [
        ("codex:different-source", Provider.CLAUDE, 1),
        ("codex:source-1", Provider.CODEX, 1),
        ("codex:source-1", Provider.CLAUDE, 2),
    ],
)
def test_characterization_quarantine_rejects_wrong_full_signed_marker_payload(
    tmp_path: Path,
    source_session_id: str,
    target_provider: Provider,
    policy_generation: int,
) -> None:
    transcript = tmp_path / f"{CLAUDE_ID}.jsonl"
    wrong_marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id="bridge-1",
            source_session_id=source_session_id,
            target_provider=target_provider,
            policy_generation=policy_generation,
        ),
        SECRET,
    )
    transcript.write_text(
        json.dumps({
            "type": "user",
            "sessionId": CLAUDE_ID,
            "timestamp": "2026-07-14T00:00:00Z",
            "message": {"content": wrong_marker},
        })
        + "\n",
        encoding="utf-8",
    )
    source = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)

    with pytest.raises(UnsafeCharacterizationCleanup, match="signed marker mismatch"):
        quarantine_claude_transcript(
            source,
            native_id=CLAUDE_ID,
            bridge_id="bridge-1",
            source_session_id="codex:source-1",
            policy_generation=1,
            projects_root=tmp_path,
            quarantine_root=tmp_path / "quarantine",
        )

    assert transcript.exists()


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


def test_characterization_report_rejects_symlinked_final_target(
    tmp_path: Path,
) -> None:
    report_root = tmp_path / "reports"
    outside = tmp_path / "outside"
    report_root.mkdir()
    outside.mkdir()
    characterization_id = "44444444-4444-4444-8444-444444444444"
    name = f"{characterization_id}.json"
    outside_target = outside / "final.json"
    outside_target.write_text("outside-safe", encoding="utf-8")
    _symlink_or_skip(report_root / name, outside_target)

    with pytest.raises(RuntimeError, match="unsafe_characterization_report"):
        write_characterization_report(
            {"schema_version": 1},
            report_root=report_root,
            characterization_id=characterization_id,
        )

    assert outside_target.read_text(encoding="utf-8") == "outside-safe"


def test_characterization_report_rejects_windows_reparse_attribute_without_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_root = tmp_path / "reports"
    report_root.mkdir()
    real_lstat = os.lstat
    root_key = os.path.normcase(os.path.abspath(report_root))

    def reparse_lstat(path: os.PathLike[str] | str, *args: Any, **kwargs: Any) -> Any:
        observed = real_lstat(path, *args, **kwargs)
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if key != root_key:
            return observed
        return SimpleNamespace(
            st_mode=observed.st_mode,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )

    monkeypatch.setattr(os, "lstat", reparse_lstat)

    with pytest.raises(RuntimeError, match="unsafe_characterization_report"):
        write_characterization_report(
            {"schema_version": 1},
            report_root=report_root,
            characterization_id="45454545-4545-4545-8545-454545454545",
        )


def test_characterization_report_rejects_tmp_local_windows_junction(
    tmp_path: Path,
) -> None:
    container = tmp_path / "container"
    redirected_target = tmp_path / "redirected-target"
    container.mkdir()
    redirected_target.mkdir()
    report_root = container / "reports"
    created = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(report_root),
            str(redirected_target),
        ],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(f"Windows junctions are unavailable: {created.stderr}")

    try:
        with pytest.raises(RuntimeError, match="unsafe_characterization_report"):
            write_characterization_report(
                {"schema_version": 1},
                report_root=report_root,
                characterization_id="46464646-4646-4646-8646-464646464646",
            )
    finally:
        if report_root.exists():
            os.rmdir(report_root)

    assert list(redirected_target.iterdir()) == []


def test_characterization_report_rejects_symlinked_root_parent(tmp_path: Path) -> None:
    container = tmp_path / "container"
    outside = tmp_path / "outside"
    container.mkdir()
    outside.mkdir()
    report_root = container / "reports"
    _symlink_or_skip(report_root, outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="unsafe_characterization_report"):
        write_characterization_report(
            {"schema_version": 1},
            report_root=report_root,
            characterization_id="55555555-5555-4555-8555-555555555555",
        )

    assert list(outside.iterdir()) == []


def test_characterization_report_uses_exclusive_same_root_temporary_file(
    tmp_path: Path,
) -> None:
    report_root = tmp_path / "reports"
    report_root.mkdir()
    characterization_id = "66666666-6666-4666-8666-666666666666"
    temporary = report_root / f".{characterization_id}.tmp"
    temporary.write_text("preexisting", encoding="utf-8")

    report = write_characterization_report(
        {"schema_version": 1},
        report_root=report_root,
        characterization_id=characterization_id,
    )

    assert temporary.read_text(encoding="utf-8") == "preexisting"
    assert report == report_root / f"{characterization_id}.json"
    assert report.is_file()
    assert sorted(path.name for path in report_root.iterdir()) == sorted(
        [temporary.name, report.name]
    )


def test_claude_quarantine_rejects_symlinked_destination_root(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    transcript = projects_root / f"{CLAUDE_ID}.jsonl"
    _write_claude_marker_transcript(
        transcript,
        payload=BridgeMarkerPayload(
            bridge_id="bridge-1",
            source_session_id="codex:source-1",
            target_provider=Provider.CLAUDE,
            policy_generation=1,
        ),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    quarantine_root = tmp_path / "quarantine"
    _symlink_or_skip(quarantine_root, outside, target_is_directory=True)

    with pytest.raises(UnsafeCharacterizationCleanup, match="quarantine.*symlink"):
        quarantine_claude_transcript(
            ClaudeSourceAdapter(projects_root, marker_secret=SECRET),
            native_id=CLAUDE_ID,
            bridge_id="bridge-1",
            source_session_id="codex:source-1",
            policy_generation=1,
            projects_root=projects_root,
            quarantine_root=quarantine_root,
        )

    assert transcript.exists()
    assert list(outside.iterdir()) == []


def test_claude_quarantine_rejects_broken_symlink_destination(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    quarantine_root = tmp_path / "quarantine"
    projects_root.mkdir()
    quarantine_root.mkdir()
    transcript = projects_root / f"{CLAUDE_ID}.jsonl"
    _write_claude_marker_transcript(
        transcript,
        payload=BridgeMarkerPayload(
            bridge_id="bridge-1",
            source_session_id="codex:source-1",
            target_provider=Provider.CLAUDE,
            policy_generation=1,
        ),
    )
    destination = quarantine_root / f"{CLAUDE_ID}.jsonl"
    _symlink_or_skip(destination, tmp_path / "outside" / "missing.jsonl")

    with pytest.raises(UnsafeCharacterizationCleanup, match="quarantine.*symlink"):
        quarantine_claude_transcript(
            ClaudeSourceAdapter(projects_root, marker_secret=SECRET),
            native_id=CLAUDE_ID,
            bridge_id="bridge-1",
            source_session_id="codex:source-1",
            policy_generation=1,
            projects_root=projects_root,
            quarantine_root=quarantine_root,
        )

    assert transcript.exists()


def test_codex_resume_polls_until_exact_started_turn_is_read() -> None:
    nonce = "c" * 32
    client = FakeRequestClient({
        "turn/start": [
            {"turn": {"id": "turn-resume-exact", "status": "inProgress"}}
        ],
        "thread/read": [
            _codex_read(
                turns=[
                    {"id": "turn-registration", "status": "completed", "items": []}
                ]
            ),
            _codex_read(turns=[
                {"id": "turn-registration", "status": "completed", "items": []},
                {
                    "id": "turn-resume-exact",
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": nonce}],
                        }
                    ],
                },
            ]),
        ],
    })
    waited: list[tuple[str, str, float]] = []
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
        completion_waiter=lambda client, thread_id, expected_turn_id, timeout: waited.append(
            (thread_id, expected_turn_id, timeout)
        ),
    )

    assert turn_id == "turn-resume-exact"
    assert [method for method, _, _ in client.calls] == [
        "thread/read",
        "turn/start",
        "thread/read",
    ]
    assert [method for method, _, _ in client.calls].count("turn/start") == 1
    turn_input = client.calls[1][1]["input"]
    assert turn_input[0]["text"].count(nonce) == 1
    assert waited == [(CODEX_ID, "turn-resume-exact", 180.0)]
    assert sleeps == []


def test_codex_resume_proves_baseline_thread_completion_and_exact_nonce() -> None:
    nonce = "d" * 32
    turn_id = "turn-resume-proof"
    baseline = _codex_read(
        turns=[
            {"id": "turn-registration", "status": "completed", "items": []}
        ]
    )
    resumed = _codex_read(
        turns=[
            {"id": "turn-registration", "status": "completed", "items": []},
            {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [
                            {
                                "type": "text",
                                "text": f"resume verification {nonce}",
                            }
                        ],
                    }
                ],
            },
        ]
    )
    client = FakeRequestClient({
        "thread/read": [baseline, resumed],
        "turn/start": [
            {"turn": {"id": turn_id, "status": "inProgress"}}
        ],
    })
    waited: list[tuple[str, str, float]] = []

    result = characterize_module._resume_codex_characterization(
        client,
        native_id=CODEX_ID,
        resume_nonce=nonce,
        request_timeout=45.0,
        verification_timeout=1.0,
        verification_poll_interval=0.1,
        completion_waiter=lambda client, thread_id, expected_turn_id, timeout: waited.append(
            (thread_id, expected_turn_id, timeout)
        ),
    )

    assert result == turn_id
    assert [method for method, _, _ in client.calls] == [
        "thread/read",
        "turn/start",
        "thread/read",
    ]
    assert waited == [(CODEX_ID, turn_id, 180.0)]
    prompt = client.calls[1][1]["input"][0]["text"]
    assert prompt.count(nonce) == 1


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_codex_resume_uses_noncompleted_notification_as_wakeup(status: str) -> None:
    nonce = "9" * 32
    turn_id = f"turn-resume-notification-{status}"
    client = CompletionAwareFakeRequestClient(
        {
            "thread/read": [
                _codex_read(turns=[]),
                _codex_read(turns=[
                    {
                        "id": turn_id,
                        "status": "completed",
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [{"type": "text", "text": nonce}],
                            }
                        ],
                    }
                ]),
            ],
            "turn/start": [
                {"turn": {"id": turn_id, "status": "inProgress"}}
            ],
        },
        notifications=[
            {
                "method": "turn/completed",
                "params": {"turn": {"id": turn_id, "status": status}},
            }
        ],
    )

    result = characterize_module._resume_codex_characterization(
        client,
        native_id=CODEX_ID,
        resume_nonce=nonce,
        request_timeout=45.0,
        verification_timeout=0.0,
        verification_poll_interval=0.1,
    )

    assert result == turn_id
    assert client.events.count("notification") == 1


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_codex_resume_rejects_noncompleted_exact_turn_in_thread_read(
    status: str,
) -> None:
    nonce = "a" * 32
    turn_id = f"turn-resume-read-{status}"
    client = FakeRequestClient({
        "thread/read": [
            _codex_read(turns=[]),
            _codex_read(turns=[
                {
                    "id": turn_id,
                    "status": status,
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": nonce}],
                        }
                    ],
                }
            ]),
        ],
        "turn/start": [
            {"turn": {"id": turn_id, "status": "inProgress"}}
        ],
    })

    with pytest.raises(RuntimeError, match="codex_resume_turn_not_completed"):
        characterize_module._resume_codex_characterization(
            client,
            native_id=CODEX_ID,
            resume_nonce=nonce,
            request_timeout=45.0,
            verification_timeout=0.0,
            verification_poll_interval=0.1,
            completion_waiter=lambda *args: None,
        )


def test_codex_resume_polls_durable_inprogress_turn_until_completed() -> None:
    nonce = "b" * 32
    turn_id = "turn-resume-durable-poll"

    def durable_turn(status: str) -> dict[str, Any]:
        return {
            "id": turn_id,
            "status": status,
            "items": [
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": nonce}],
                }
            ],
        }

    client = FakeRequestClient({
        "thread/read": [
            _codex_read(turns=[]),
            _codex_read(turns=[durable_turn("inProgress")]),
            _codex_read(turns=[durable_turn("completed")]),
        ],
        "turn/start": [
            {"turn": {"id": turn_id, "status": "inProgress"}}
        ],
    })
    times = iter([0.0, 0.0])
    sleeps: list[float] = []

    result = characterize_module._resume_codex_characterization(
        client,
        native_id=CODEX_ID,
        resume_nonce=nonce,
        request_timeout=45.0,
        verification_timeout=1.0,
        verification_poll_interval=0.1,
        monotonic=times.__next__,
        sleep=sleeps.append,
        completion_waiter=lambda *args: None,
    )

    assert result == turn_id
    assert sleeps == [0.1]


def test_codex_resume_rejects_post_read_for_wrong_thread() -> None:
    turn_id = "turn-resume-wrong-thread"
    client = FakeRequestClient({
        "thread/read": [
            _codex_read(turns=[]),
            _codex_read(
                native_id="different-thread",
                turns=[{"id": turn_id, "status": "completed", "items": []}],
            ),
        ],
        "turn/start": [
            {"turn": {"id": turn_id, "status": "inProgress"}}
        ],
    })

    with pytest.raises(RuntimeError, match="codex_resume_identity_mismatch"):
        characterize_module._resume_codex_characterization(
            client,
            native_id=CODEX_ID,
            resume_nonce="e" * 32,
            request_timeout=45.0,
            verification_timeout=1.0,
            verification_poll_interval=0.1,
            completion_waiter=lambda *args: None,
        )


def test_codex_resume_rejects_preexisting_returned_turn_id() -> None:
    nonce = "f" * 32
    turn_id = "turn-resume-stale"
    stale = _codex_read(
        turns=[
            {
                "id": turn_id,
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": nonce}],
                    }
                ],
            }
        ]
    )
    client = FakeRequestClient({
        "thread/read": [stale],
        "turn/start": [
            {"turn": {"id": turn_id, "status": "inProgress"}}
        ],
    })

    with pytest.raises(RuntimeError, match="codex_resume_turn_preexisting"):
        characterize_module._resume_codex_characterization(
            client,
            native_id=CODEX_ID,
            resume_nonce=nonce,
            request_timeout=45.0,
            verification_timeout=1.0,
            verification_poll_interval=0.1,
            completion_waiter=lambda *args: None,
        )


def test_codex_resume_rejects_exact_new_turn_without_nonce() -> None:
    turn_id = "turn-resume-without-nonce"
    client = FakeRequestClient({
        "thread/read": [
            _codex_read(turns=[]),
            _codex_read(
                turns=[
                    {
                        "id": turn_id,
                        "status": "completed",
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [
                                    {"type": "text", "text": "wrong nonce"}
                                ],
                            }
                        ],
                    }
                ]
            ),
        ],
        "turn/start": [
            {"turn": {"id": turn_id, "status": "inProgress"}}
        ],
    })

    with pytest.raises(RuntimeError, match="codex_resume_nonce_mismatch"):
        characterize_module._resume_codex_characterization(
            client,
            native_id=CODEX_ID,
            resume_nonce="1" * 32,
            request_timeout=45.0,
            verification_timeout=1.0,
            verification_poll_interval=0.1,
            completion_waiter=lambda *args: None,
        )


def test_codex_resume_rejects_stale_or_wrong_turn_identity() -> None:
    client = FakeRequestClient({
        "turn/start": [
            {"turn": {"id": "turn-resume-exact", "status": "inProgress"}}
        ],
        "thread/read": [
            _codex_read(turns=[]),
            _codex_read(
                turns=[
                    {"id": "turn-registration", "status": "completed", "items": []}
                ]
            )
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
    assert [method for method, _, _ in client.calls].count("thread/read") == 2


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


def test_claude_resume_runner_failure_drops_secret_exception_chain(
    tmp_path: Path,
) -> None:
    secret = "sk-proj-CLAUDE-RESUME-RUNNER-MUST-NOT-LEAK"
    baseline = _resume_projection(
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        cursor="cursor-before",
        native_hash="hash-before",
    )

    def runner(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"provider runner exposed {secret}")

    with pytest.raises(PlaceholderCreationError) as raised:
        characterize_module._resume_claude_characterization(
            FakeClaudeResumeSource(baseline),
            baseline_projection=baseline,
            native_id=CLAUDE_ID,
            bridge_id="bridge-1",
            resume_nonce="7" * 32,
            executable="C:/bin/claude.exe",
            cwd=tmp_path,
            runner=runner,
        )

    assert raised.value.code == "claude_resume_process_failed"
    _assert_sanitized_exception_has_no_chain(raised.value, secret=secret)


def test_claude_resume_parse_failure_drops_secret_exception_chain(
    tmp_path: Path,
) -> None:
    secret = "sk-proj-CLAUDE-RESUME-PARSE-MUST-NOT-LEAK"
    baseline = _resume_projection(
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        cursor="cursor-before",
        native_hash="hash-before",
    )
    source = FakeClaudeResumeSource(
        baseline,
        parse_error=RuntimeError(f"provider parser exposed {secret}"),
    )

    with pytest.raises(PlaceholderCreationError) as raised:
        characterize_module._resume_claude_characterization(
            source,
            baseline_projection=baseline,
            native_id=CLAUDE_ID,
            bridge_id="bridge-1",
            resume_nonce="8" * 32,
            executable="C:/bin/claude.exe",
            cwd=tmp_path,
            runner=lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, stdout="{}", stderr=""
            ),
            verification_timeout=0.0,
        )

    assert raised.value.code == "claude_resume_target_unreadable"
    _assert_sanitized_exception_has_no_chain(raised.value, secret=secret)


def test_codex_source_initialization_failure_drops_secret_exception_chain() -> None:
    secret = "sk-proj-CODEX-INITIALIZE-MUST-NOT-LEAK"

    class FailingInitializationClient(FakeRequestClient):
        _initialized = False

        def initialize(self) -> None:
            raise RuntimeError(f"provider initialize exposed {secret}")

    client = FailingInitializationClient({})
    adapter = CodexTargetAdapter(
        client,
        source_adapter=CodexSourceAdapter(client, marker_secret=SECRET),
        marker_secret=SECRET,
    )

    with pytest.raises(PlaceholderCreationError) as raised:
        adapter.create_placeholder(
            title="Mirror title",
            source_session_id="claude:source-1",
            bridge_id="bridge-1",
            policy_generation=1,
        )

    assert raised.value.code == "codex_initialization_failed"
    _assert_sanitized_exception_has_no_chain(raised.value, secret=secret)


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


def test_live_characterization_preserves_direct_runtime_argv_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_command = (
        "C:/runtime & literal/node.exe",
        "C:/runtime & literal/@anthropic-ai/claude-code/cli.js",
    )
    codex_command = ("C:/runtime & literal/codex.exe",)
    version_calls: list[list[str]] = []
    characterized: dict[str, Any] = {}

    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setattr(
        characterize_module,
        "resolve_cli_executable",
        lambda value: claude_command if value == "claude" else codex_command,
    )
    monkeypatch.setattr(
        characterize_module,
        "_cli_version",
        lambda args: version_calls.append(list(args)) or "test",
    )
    monkeypatch.setattr(
        characterize_module,
        "_characterize_claude",
        lambda status, **kwargs: characterized.update(claude=kwargs["executable"]),
    )
    monkeypatch.setattr(
        characterize_module,
        "_characterize_codex",
        lambda status, **kwargs: characterized.update(codex=kwargs["executable"]),
    )

    run_live_characterization(
        report_root=tmp_path / "reports",
        claude_projects_root=tmp_path / "projects",
        cwd=tmp_path,
    )

    assert version_calls == [
        [*claude_command, "--version"],
        [*codex_command, "--version"],
    ]
    assert characterized == {
        "claude": claude_command,
        "codex": codex_command,
    }


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


@pytest.mark.parametrize("suffix", [".cmd", ".ps1"])
def test_live_characterization_resolves_recognized_claude_npm_shim_to_node_argv(
    tmp_path: Path, suffix: str
) -> None:
    npm_root = tmp_path / "npm & literal"
    cli = npm_root / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node = npm_root / "node.exe"
    shim = npm_root / f"claude{suffix}"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    node.write_bytes(b"")
    shim.write_text("recognized npm shim", encoding="utf-8")

    resolved = resolve_cli_executable(
        "claude",
        which=lambda name: str(shim if name == "claude" else node),
    )

    assert resolved == (str(node.resolve()), str(cli.resolve()))


@pytest.mark.parametrize("suffix", [".cmd", ".ps1", ".bat"])
def test_live_characterization_rejects_unrecognized_shell_shims(
    tmp_path: Path, suffix: str
) -> None:
    shim = tmp_path / f"claude{suffix}"
    shim.write_text("invoke arbitrary shell content", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsupported_shell_shim"):
        resolve_cli_executable(
            "claude",
            which=lambda name: str(shim) if name == "claude" else None,
        )


def test_live_characterization_keeps_native_executable_as_single_argv_prefix() -> None:
    assert resolve_cli_executable(
        "C:/tools/claude.exe", which=lambda _: None
    ) == ("C:/tools/claude.exe",)


def test_codex_resolver_preserves_launchable_absolute_npm_cmd_over_windowsapps_exe(
    tmp_path: Path,
) -> None:
    npm_root = tmp_path / "npm & literal"
    npm_root.mkdir()
    shim = npm_root / "codex.CMD"
    shim.write_text("@echo off\r\necho codex-cli 0.125.0\r\n", encoding="utf-8")
    inaccessible_native = tmp_path / "WindowsApps" / "codex.exe"
    inaccessible_native.parent.mkdir()
    inaccessible_native.write_bytes(b"")

    resolved = resolve_cli_executable(
        "codex",
        which=lambda name: str(
            inaccessible_native if name == "codex.exe" else shim
        ),
    )

    assert resolved == (str(shim.resolve()),)


def test_live_characterization_aborts_before_sessions_when_cli_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_calls: list[list[str]] = []
    provider_calls: list[str] = []

    def version(args: list[str]) -> str | None:
        version_calls.append(list(args))
        return "1.2.3" if "claude.exe" in args[0] else None

    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setattr(
        characterize_module,
        "resolve_cli_executable",
        lambda value: (f"C:/launchable/{value}.exe",),
    )
    monkeypatch.setattr(characterize_module, "_cli_version", version)
    monkeypatch.setattr(
        characterize_module,
        "_characterize_claude",
        lambda *args, **kwargs: provider_calls.append("claude"),
    )
    monkeypatch.setattr(
        characterize_module,
        "_characterize_codex",
        lambda *args, **kwargs: provider_calls.append("codex"),
    )

    with pytest.raises(RuntimeError, match="codex.*preflight|preflight.*codex"):
        run_live_characterization(
            report_root=tmp_path / "reports",
            claude_projects_root=tmp_path / "projects",
            cwd=tmp_path,
        )

    assert version_calls == [
        ["C:/launchable/claude.exe", "--version"],
        ["C:/launchable/codex.exe", "--version"],
    ]
    assert provider_calls == []
