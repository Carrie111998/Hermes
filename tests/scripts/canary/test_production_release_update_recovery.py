from __future__ import annotations

import inspect
import threading
import traceback
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator, Mapping
from unittest.mock import patch

import pytest

from scripts.canary import production_cutover_activation_lock as authority_lock
from scripts.canary import production_release_active_transaction as active
from scripts.canary import production_release_host_actions as host_actions
from scripts.canary import production_release_update_journal as journal_module
from scripts.canary import production_release_update_recovery as recovery
from scripts.canary import production_release_update_runtime as runtime
from tests.scripts.canary.test_production_release_update_runtime import (
    NOW,
    DurablePreauthorizationActions,
    FakeActions,
    MemoryJournal,
    _authority_record,
    _forward_prefix,
    _phases,
    _recover,
)


def _marker() -> Mapping[str, Any]:
    return active._build_marker(_authority_record())


def _install_outer_lock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    order: list[str] | None = None,
    held: dict[str, bool] | None = None,
) -> None:
    events = order if order is not None else []
    lock_state = held if held is not None else {"value": False}

    def fake_activation_lock(
        *,
        require_root: bool,
        lock_factory: Any | None = None,
    ) -> Any:
        if lock_factory is not None:
            return lock_factory()
        assert require_root is True

        @contextmanager
        def locked() -> Iterator[None]:
            assert lock_state["value"] is False
            lock_state["value"] = True
            events.append("outer_enter")
            try:
                yield
            finally:
                events.append("outer_exit")
                lock_state["value"] = False

        return locked()

    monkeypatch.setattr(
        authority_lock,
        "authority_activation_lock",
        fake_activation_lock,
    )


def _install_memory_recovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    journal: MemoryJournal,
    actions: FakeActions,
    now_unix: int = NOW,
    order: list[str] | None = None,
    held: dict[str, bool] | None = None,
) -> list[Mapping[str, Any]]:
    events = order if order is not None else []
    lock_state = held if held is not None else {"value": False}
    retired: list[Mapping[str, Any]] = []
    marker = _marker()
    _install_outer_lock(
        monkeypatch,
        order=events,
        held=lock_state,
    )

    def normalize() -> Mapping[str, Any]:
        assert lock_state["value"] is True
        events.append("normalize")
        return deepcopy(marker)

    def open_existing(
        _cls: type[journal_module.ReleaseUpdateJournal],
        *,
        authority_record: Mapping[str, Any],
    ) -> MemoryJournal:
        assert lock_state["value"] is True
        assert authority_record == _authority_record()
        events.append("journal_open")
        return journal

    def construct_actions() -> FakeActions:
        assert lock_state["value"] is True
        events.append("actions_construct")
        return actions

    def recover_update(
        *,
        authority_record: Mapping[str, Any],
        actions: FakeActions,
        journal: MemoryJournal,
    ) -> runtime.TransactionState:
        assert lock_state["value"] is True
        assert authority_record == _authority_record()
        events.append("runtime_enter")
        with patch.object(runtime.time, "time", return_value=now_unix):
            state = runtime._recover_update_for_test(
                authority_record=authority_record,
                actions=actions,
                journal=journal,
                lock_factory=nullcontext,
            )
        assert lock_state["value"] is True
        events.append("runtime_return")
        return state

    def retire(
        *,
        authority_record: Mapping[str, Any],
    ) -> None:
        assert lock_state["value"] is True
        events.append("retire")
        retired.append(deepcopy(dict(authority_record)))

    monkeypatch.setattr(
        active,
        "recover_existing_active_transaction",
        normalize,
    )
    monkeypatch.setattr(
        journal_module.ReleaseUpdateJournal,
        "open_existing",
        classmethod(open_existing),
    )
    monkeypatch.setattr(
        host_actions,
        "ProductionReleaseHostActions",
        construct_actions,
    )
    monkeypatch.setattr(runtime, "recover_update", recover_update)
    monkeypatch.setattr(active, "retire_active_transaction", retire)
    return retired


def test_public_recovery_boundary_is_fixed_and_minimal() -> None:
    assert list(inspect.signature(
        recovery.recover_active_release_transaction
    ).parameters) == []
    assert recovery.__all__ == [
        "ProductionReleaseUpdateRecoveryError",
        "recover_active_release_transaction",
    ]
    assert not hasattr(recovery, "main")
    assert not hasattr(recovery, "execute_active_release_transaction")


def test_missing_marker_is_idle_without_opening_or_creating_any_downstream_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_outer_lock(monkeypatch)
    monkeypatch.setattr(
        active,
        "recover_existing_active_transaction",
        lambda: None,
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("idle recovery touched downstream state")

    monkeypatch.setattr(
        journal_module.ReleaseUpdateJournal,
        "open_existing",
        classmethod(forbidden),
    )
    monkeypatch.setattr(
        host_actions,
        "ProductionReleaseHostActions",
        forbidden,
    )
    monkeypatch.setattr(runtime, "recover_update", forbidden)
    monkeypatch.setattr(active, "retire_active_transaction", forbidden)

    assert recovery.recover_active_release_transaction() is None


@pytest.mark.parametrize(
    ("case", "terminal_phase"),
    (
        ("fresh", "completed"),
        ("expired", "aborted"),
        ("mutated", "rolled_back"),
        ("post_health", "rolled_back"),
        ("committed", "completed"),
    ),
)
def test_recovery_direction_table_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    terminal_phase: str,
) -> None:
    if case == "fresh":
        journal = MemoryJournal()
        actions = FakeActions()
        now_unix = NOW
    elif case == "expired":
        journal = MemoryJournal()
        actions = FakeActions()
        now_unix = int(_authority_record()["intent"]["approval_expires_at_unix"])
    elif case == "mutated":
        journal = _forward_prefix("host_payloads_applied")
        actions = FakeActions()
        now_unix = NOW
    elif case == "post_health":
        journal = _forward_prefix("target_health_validated")
        actions = DurablePreauthorizationActions(preauthorized=True)
        now_unix = NOW
    else:
        journal = _forward_prefix(runtime.COMMIT_PHASE)
        actions = FakeActions()
        now_unix = int(
            _authority_record()["intent"]["approval_expires_at_unix"]
        )

    retired = _install_memory_recovery(
        monkeypatch,
        journal=journal,
        actions=actions,
        now_unix=now_unix,
    )

    state = recovery.recover_active_release_transaction()

    assert state is not None
    assert state.terminal_phase == terminal_phase
    assert retired == [_authority_record()]
    assert actions.calls[-1] == f"{terminal_phase}_revalidated"
    if case == "expired":
        assert runtime.FIRST_APPLICATION_MUTATION_PHASE not in _phases(journal)
    if case in {"mutated", "post_health"}:
        assert runtime.COMMIT_PHASE not in _phases(journal)
        assert "rollback_intent" in _phases(journal)
    if case == "post_health":
        assert actions.calls.index(
            runtime.UNIT_INPUT_PREAUTHORIZATION_CANCEL_PHASE
        ) < actions.calls.index("target_stopped")
    if case == "committed":
        assert "rollback_intent" not in _phases(journal)
        assert "approval_expired_abort_intent" not in _phases(journal)


def test_marker_authority_is_the_only_authority_for_every_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = MemoryJournal()
    actions = FakeActions()
    observed: dict[str, Mapping[str, Any]] = {}
    marker = _marker()
    _install_outer_lock(monkeypatch)
    monkeypatch.setattr(
        active,
        "recover_existing_active_transaction",
        lambda: deepcopy(marker),
    )

    def open_existing(
        _cls: type[journal_module.ReleaseUpdateJournal],
        *,
        authority_record: Mapping[str, Any],
    ) -> MemoryJournal:
        observed["journal"] = authority_record
        return journal

    def recover_update(
        *,
        authority_record: Mapping[str, Any],
        actions: FakeActions,
        journal: MemoryJournal,
    ) -> runtime.TransactionState:
        observed["runtime"] = authority_record
        return _recover(actions=actions, journal=journal)

    def retire(
        *,
        authority_record: Mapping[str, Any],
    ) -> None:
        observed["retirement"] = authority_record

    monkeypatch.setattr(
        journal_module.ReleaseUpdateJournal,
        "open_existing",
        classmethod(open_existing),
    )
    monkeypatch.setattr(
        host_actions,
        "ProductionReleaseHostActions",
        lambda: actions,
    )
    monkeypatch.setattr(runtime, "recover_update", recover_update)
    monkeypatch.setattr(active, "retire_active_transaction", retire)

    state = recovery.recover_active_release_transaction()

    assert state is not None
    assert observed == {
        "journal": _authority_record(),
        "runtime": _authority_record(),
        "retirement": _authority_record(),
    }


@pytest.mark.parametrize(
    ("stage", "code"),
    (
        ("registry", "release_update_recovery_registry_failed"),
        ("marker", "release_update_recovery_marker_invalid"),
        ("journal", "release_update_recovery_journal_failed"),
        ("actions", "release_update_recovery_host_actions_failed"),
        ("runtime", "release_update_recovery_runtime_failed"),
        ("terminal", "release_update_recovery_terminal_state_invalid"),
        ("retirement", "release_update_recovery_retirement_failed"),
    ),
)
def test_layer_failure_is_stable_secret_free_and_retains_marker(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    code: str,
) -> None:
    secret = "do-not-expose-release-secret"
    marker = _marker()
    journal = MemoryJournal()
    actions = FakeActions()
    terminal = _recover(actions=FakeActions(), journal=MemoryJournal())
    retired: list[Mapping[str, Any]] = []
    _install_outer_lock(monkeypatch)

    def normalize() -> Mapping[str, Any]:
        if stage == "registry":
            raise RuntimeError(secret)
        if stage == "marker":
            return {"authority_record": {"secret": secret}}
        return deepcopy(marker)

    def open_existing(
        _cls: type[journal_module.ReleaseUpdateJournal],
        *,
        authority_record: Mapping[str, Any],
    ) -> MemoryJournal:
        if stage == "journal":
            raise RuntimeError(secret)
        return journal

    def construct_actions() -> FakeActions:
        if stage == "actions":
            raise RuntimeError(secret)
        return actions

    def recover_update(**_kwargs: Any) -> Any:
        if stage == "runtime":
            raise RuntimeError(secret)
        if stage == "terminal":
            return {"terminal_phase": "completed", "secret": secret}
        return terminal

    def retire(
        *,
        authority_record: Mapping[str, Any],
    ) -> None:
        if stage == "retirement":
            raise RuntimeError(secret)
        retired.append(authority_record)

    monkeypatch.setattr(
        active,
        "recover_existing_active_transaction",
        normalize,
    )
    monkeypatch.setattr(
        journal_module.ReleaseUpdateJournal,
        "open_existing",
        classmethod(open_existing),
    )
    monkeypatch.setattr(
        host_actions,
        "ProductionReleaseHostActions",
        construct_actions,
    )
    monkeypatch.setattr(runtime, "recover_update", recover_update)
    monkeypatch.setattr(active, "retire_active_transaction", retire)

    with pytest.raises(
        recovery.ProductionReleaseUpdateRecoveryError,
        match=rf"^{code}$",
    ) as raised:
        recovery.recover_active_release_transaction()

    assert secret not in str(raised.value)
    assert secret not in "".join(
        traceback.format_exception(raised.value)
    )
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert retired == []


def test_lock_failure_is_stable_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "do-not-expose-lock-secret"

    def fail_lock(**_kwargs: Any) -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(
        authority_lock,
        "authority_activation_lock",
        fail_lock,
    )

    with pytest.raises(
        recovery.ProductionReleaseUpdateRecoveryError,
        match=r"^release_update_recovery_lock_unavailable$",
    ) as raised:
        recovery.recover_active_release_transaction()

    assert secret not in str(raised.value)
    assert secret not in "".join(
        traceback.format_exception(raised.value)
    )
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_outer_lock_spans_normalization_terminal_revalidation_and_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    held = {"value": False}

    class OrderedJournal(MemoryJournal):
        def load(self) -> Any:
            assert held["value"] is True
            order.append("journal_load")
            return super().load()

        def append(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
            assert held["value"] is True
            order.append(f"append:{event['phase']}")
            return super().append(event)

    class OrderedActions(FakeActions):
        def perform(
            self,
            phase: str,
            *,
            intent: Mapping[str, Any],
            receipts: Mapping[str, Mapping[str, Any]],
        ) -> Mapping[str, Any]:
            assert held["value"] is True
            order.append(f"action:{phase}")
            return super().perform(
                phase,
                intent=intent,
                receipts=receipts,
            )

    journal = OrderedJournal()
    actions = OrderedActions()
    retired = _install_memory_recovery(
        monkeypatch,
        journal=journal,
        actions=actions,
        order=order,
        held=held,
    )

    state = recovery.recover_active_release_transaction()

    assert state is not None
    assert retired == [_authority_record()]
    assert order[:4] == [
        "outer_enter",
        "normalize",
        "journal_open",
        "journal_load",
    ]
    assert order.index("runtime_enter") < order.index(
        "action:completed_revalidated"
    )
    assert order.index("action:completed_revalidated") < order.index(
        "runtime_return"
    )
    assert order.index("runtime_return") < order.index("retire")
    assert order[-2:] == ["retire", "outer_exit"]


def test_concurrent_recovery_cannot_normalize_until_prior_retirement_unlocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_lock = threading.Lock()
    first_runtime_entered = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()
    order: list[str] = []
    counter_lock = threading.Lock()
    attempts = 0
    normalizations = 0
    runtime_calls = 0
    terminal = _recover(actions=FakeActions(), journal=MemoryJournal())

    def fake_activation_lock(
        *,
        require_root: bool,
        lock_factory: Any | None = None,
    ) -> Any:
        nonlocal attempts
        if lock_factory is not None:
            return lock_factory()
        assert require_root is True
        with counter_lock:
            attempts += 1
            attempt = attempts
            order.append(f"attempt:{attempt}")
            if attempt == 2:
                second_attempted.set()

        @contextmanager
        def locked() -> Iterator[None]:
            with shared_lock:
                order.append(f"enter:{attempt}")
                try:
                    yield
                finally:
                    order.append(f"exit:{attempt}")

        return locked()

    def normalize() -> Mapping[str, Any]:
        nonlocal normalizations
        with counter_lock:
            normalizations += 1
            current = normalizations
        order.append(f"normalize:{current}")
        return _marker()

    def open_existing(
        _cls: type[journal_module.ReleaseUpdateJournal],
        *,
        authority_record: Mapping[str, Any],
    ) -> MemoryJournal:
        return MemoryJournal(authority_record=authority_record)

    def recover_update(**_kwargs: Any) -> runtime.TransactionState:
        nonlocal runtime_calls
        with counter_lock:
            runtime_calls += 1
            current = runtime_calls
        order.append(f"runtime:{current}")
        if current == 1:
            first_runtime_entered.set()
            assert release_first.wait(10)
        return terminal

    def retire(**_kwargs: Any) -> None:
        order.append("retire")

    monkeypatch.setattr(
        authority_lock,
        "authority_activation_lock",
        fake_activation_lock,
    )
    monkeypatch.setattr(
        active,
        "recover_existing_active_transaction",
        normalize,
    )
    monkeypatch.setattr(
        journal_module.ReleaseUpdateJournal,
        "open_existing",
        classmethod(open_existing),
    )
    monkeypatch.setattr(
        host_actions,
        "ProductionReleaseHostActions",
        FakeActions,
    )
    monkeypatch.setattr(runtime, "recover_update", recover_update)
    monkeypatch.setattr(active, "retire_active_transaction", retire)

    errors: list[BaseException] = []

    def run() -> None:
        try:
            recovery.recover_active_release_transaction()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert first_runtime_entered.wait(10)
    second.start()
    assert second_attempted.wait(10)
    assert normalizations == 1
    release_first.set()
    first.join(10)
    second.join(10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert normalizations == 2
    assert order.index("retire") < order.index("exit:1")
    assert order.index("exit:1") < order.index("enter:2")
    assert order.index("enter:2") < order.index("normalize:2")


def _filesystem_paths(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.chmod(0o700)
    registry_root = (tmp_path / "registry").resolve()
    registry_root.mkdir(mode=active.DIRECTORY_MODE)
    registry_root.chmod(active.DIRECTORY_MODE)
    transactions = registry_root / "transactions"
    transactions.mkdir(mode=journal_module.DIRECTORY_MODE)
    transactions.chmod(journal_module.DIRECTORY_MODE)
    transaction = transactions / str(
        _authority_record()["intent"]["intent_sha256"]
    )
    return registry_root, transaction


def _install_filesystem_recovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry_root: Path,
    transaction: Path,
    actions: FakeActions,
) -> None:
    _install_outer_lock(monkeypatch)

    def normalize() -> Mapping[str, Any] | None:
        return active._recover_existing_for_test(registry_root)

    def open_existing(
        _cls: type[journal_module.ReleaseUpdateJournal],
        *,
        authority_record: Mapping[str, Any],
    ) -> journal_module.ReleaseUpdateJournal:
        return journal_module.ReleaseUpdateJournal._open_existing_for_test(
            transaction,
            authority_record=authority_record,
        )

    def recover_update(
        *,
        authority_record: Mapping[str, Any],
        actions: FakeActions,
        journal: journal_module.ReleaseUpdateJournal,
    ) -> runtime.TransactionState:
        with patch.object(runtime.time, "time", return_value=NOW):
            return runtime._recover_update_for_test(
                authority_record=authority_record,
                actions=actions,
                journal=journal,
                lock_factory=nullcontext,
            )

    def retire(
        *,
        authority_record: Mapping[str, Any],
    ) -> None:
        active._retire_for_test(
            registry_root,
            authority_record=authority_record,
        )

    monkeypatch.setattr(
        active,
        "recover_existing_active_transaction",
        normalize,
    )
    monkeypatch.setattr(
        journal_module.ReleaseUpdateJournal,
        "open_existing",
        classmethod(open_existing),
    )
    monkeypatch.setattr(
        host_actions,
        "ProductionReleaseHostActions",
        lambda: actions,
    )
    monkeypatch.setattr(runtime, "recover_update", recover_update)
    monkeypatch.setattr(active, "retire_active_transaction", retire)


def test_real_existing_journal_recovers_and_retires_only_active_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry_root, transaction = _filesystem_paths(tmp_path)
    writer = journal_module.ReleaseUpdateJournal._for_test(
        transaction,
        authority_record=_authority_record(),
    )
    assert writer.load() == []
    active._create_or_replay_for_test(
        registry_root,
        authority_record=_authority_record(),
    )
    actions = FakeActions()
    _install_filesystem_recovery(
        monkeypatch,
        registry_root=registry_root,
        transaction=transaction,
        actions=actions,
    )

    state = recovery.recover_active_release_transaction()

    assert state is not None
    assert state.terminal_phase == "completed"
    assert not (registry_root / active.ACTIVE_MARKER_NAME).exists()
    assert transaction.is_dir()
    assert (transaction / journal_module.AUTHORITY_FILE_NAME).is_file()
    existing = journal_module.ReleaseUpdateJournal._open_existing_for_test(
        transaction,
        authority_record=_authority_record(),
    )
    persisted = runtime.load_state(
        intent=_authority_record()["intent"],
        events=existing.load(),
    )
    assert persisted == state
    assert actions.calls[-1] == "completed_revalidated"


def test_real_terminal_revalidation_failure_retains_marker_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry_root, transaction = _filesystem_paths(tmp_path)
    writer = journal_module.ReleaseUpdateJournal._for_test(
        transaction,
        authority_record=_authority_record(),
    )
    with patch.object(runtime.time, "time", return_value=NOW):
        completed = runtime._execute_update_for_test(
            authority_record=_authority_record(),
            actions=FakeActions(),
            journal=writer,
            lock_factory=nullcontext,
        )
    assert completed.terminal_phase == "completed"
    expected = active._create_or_replay_for_test(
        registry_root,
        authority_record=_authority_record(),
    )
    _install_filesystem_recovery(
        monkeypatch,
        registry_root=registry_root,
        transaction=transaction,
        actions=FakeActions(fail_always="completed_revalidated"),
    )

    with pytest.raises(
        recovery.ProductionReleaseUpdateRecoveryError,
        match=r"^release_update_recovery_runtime_failed$",
    ):
        recovery.recover_active_release_transaction()

    assert active._read_for_test(registry_root) == expected
    assert (registry_root / active.ACTIVE_MARKER_NAME).is_file()

    retry_actions = FakeActions()
    _install_filesystem_recovery(
        monkeypatch,
        registry_root=registry_root,
        transaction=transaction,
        actions=retry_actions,
    )
    recovered = recovery.recover_active_release_transaction()

    assert recovered is not None
    assert recovered.terminal_phase == "completed"
    assert retry_actions.calls == ["completed_revalidated"]
    assert not (registry_root / active.ACTIVE_MARKER_NAME).exists()
    assert transaction.is_dir()


def test_missing_real_journal_retains_existing_active_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry_root, transaction = _filesystem_paths(tmp_path)
    expected = active._create_or_replay_for_test(
        registry_root,
        authority_record=_authority_record(),
    )
    _install_filesystem_recovery(
        monkeypatch,
        registry_root=registry_root,
        transaction=transaction,
        actions=FakeActions(),
    )

    with pytest.raises(
        recovery.ProductionReleaseUpdateRecoveryError,
        match=r"^release_update_recovery_journal_failed$",
    ):
        recovery.recover_active_release_transaction()

    assert active._read_for_test(registry_root) == expected
    assert (registry_root / active.ACTIVE_MARKER_NAME).is_file()
    assert not transaction.exists()
