"""Bounded, deduplicated Kanban operations alerts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from gateway.kanban_alerts import (
    KanbanAlertIntake,
    KanbanAlertIncident,
    KanbanAlertNotifier,
    KanbanAlertSettings,
    collect_routed_blocker_incidents,
    intake_incident,
    reconcile_stale_task_incidents,
    record_dispatch_alerts,
    project_review_terminal_events,
)
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, content: str, metadata=None) -> object:
        self.sent.append((chat_id, content))


class FailingAdapter(RecordingAdapter):
    async def send(self, chat_id: str, content: str, metadata=None) -> object:
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=False)


class ListingAdapter(RecordingAdapter):
    async def list_channels(self):
        return [
            {"id": "alerts-id", "name": "alerts", "type": "channel"},
            {"id": "blockers-id", "name": "blockers", "type": "channel"},
        ]


def test_review_terminal_projection_is_typed_event_keyed_and_order_independent(
    tmp_path,
):
    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)
    genuine = kb.Event(
        id=41,
        task_id="t_decision",
        run_id=7,
        kind="blocked",
        payload={
            "alert_required": True,
            "reason": "maintainer must choose the public contract",
            "terminal_type": "GENUINE_DECISION",
        },
        created_at=1,
    )
    routine = kb.Event(
        id=42,
        task_id="t_routine",
        run_id=8,
        kind="changes_requested",
        payload={"reason": "add a boundary test"},
        created_at=2,
    )
    exhausted = kb.Event(
        id=43,
        task_id="t_exhausted",
        run_id=9,
        kind="review_budget_exhausted",
        payload={
            "alert_required": True,
            "reason": "third independent finding",
            "terminal_type": "RECOVERY_EXHAUSTED",
        },
        created_at=3,
    )

    project_review_terminal_events(
        notifier,
        board="default",
        events=[exhausted, routine, genuine, exhausted, genuine],
        active_decision_task_ids={"t_decision"},
    )
    asyncio.run(notifier.flush())
    project_review_terminal_events(
        notifier,
        board="default",
        events=[genuine, exhausted],
        active_decision_task_ids={"t_decision"},
    )
    asyncio.run(notifier.flush())

    assert len(adapter.sent) == 2
    assert adapter.sent[0][0] == "blockers-id"
    assert "t_decision" in adapter.sent[0][1]
    assert "Action required" in adapter.sent[0][1]
    assert adapter.sent[1][0] == "alerts-id"
    assert "t_exhausted" in adapter.sent[1][1]
    assert "final receipt" in adapter.sent[1][1]
    state = json.loads((tmp_path / "kanban-alerts.json").read_text(encoding="utf-8"))
    assert set(state["active"]) == {"kanban-event:default:41"}
    assert set(state["recent"]) == {
        "kanban-event:default:41",
        "kanban-event:default:43",
    }

    project_review_terminal_events(
        notifier,
        board="default",
        events=[genuine, exhausted],
        active_decision_task_ids=set(),
    )
    asyncio.run(notifier.flush())
    project_review_terminal_events(
        notifier,
        board="default",
        events=[exhausted, genuine],
        active_decision_task_ids=set(),
    )
    asyncio.run(notifier.flush())

    assert len(adapter.sent) == 3
    assert adapter.sent[2] == (
        "blockers-id",
        "✅ Kanban genuine decision resolved: default/t_decision.",
    )


def test_authoritative_intake_filters_raw_github_and_dedupes_root_cause():
    assert intake_incident(
        KanbanAlertIntake(
            source="github",
            root_cause="ci-42",
            state="open",
            route="automation",
            message="raw workflow_run payload",
        )
    ) is None

    first = intake_incident(
        KanbanAlertIntake(
            source="dispatcher",
            root_cause=" worker pool exhausted ",
            state="open",
            route="automation",
            message="worker pool exhausted",
        )
    )
    reordered = intake_incident(
        KanbanAlertIntake(
            source="DISPATCHER",
            root_cause="worker pool exhausted",
            state="open",
            route="automation",
            message="same material state from reordered event",
        )
    )
    assert first is not None and reordered is not None
    assert first.key == reordered.key


def _notifier(tmp_path, adapter, *, now=lambda: 1000.0):
    settings = KanbanAlertSettings(
        enabled=True,
        platform="buzz",
        automation_channel="#alerts",
        blockers_channel="#blockers",
        cooldown_seconds=300,
    )
    channels = {"#alerts": "alerts-id", "#blockers": "blockers-id"}
    return KanbanAlertNotifier(
        settings,
        state_path=tmp_path / "kanban-alerts.json",
        adapter_lookup=lambda platform, profile: adapter,
        resolve_channel=lambda platform, name: channels.get(name),
        lookup_channel_type=lambda platform, chat_id: "channel",
        list_known_channels=lambda platform: [
            {"id": "alerts-id", "name": "alerts", "type": "channel"},
            {"id": "blockers-id", "name": "blockers", "type": "channel"},
        ],
        now=now,
    )


def test_persistent_incident_is_deduplicated_and_recovers_once(tmp_path):
    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)
    incident = KanbanAlertIncident(
        key="ready-unspawned",
        route="automation",
        message="Dispatcher has spawnable ready work but no workers launched.",
        recovery_message="Dispatcher recovered and launched work again.",
    )

    notifier.sync_scope("dispatcher", [incident])
    asyncio.run(notifier.flush())
    notifier.sync_scope("dispatcher", [incident])
    asyncio.run(notifier.flush())
    notifier.sync_scope("dispatcher", [])
    asyncio.run(notifier.flush())
    notifier.sync_scope("dispatcher", [])
    asyncio.run(notifier.flush())

    assert adapter.sent == [
        ("alerts-id", incident.message),
        ("alerts-id", incident.recovery_message),
    ]


def test_automation_refuses_dm_but_actionable_blocker_may_use_dm(tmp_path):
    adapter = RecordingAdapter()
    settings = KanbanAlertSettings(
        enabled=True,
        platform="buzz",
        automation_channel="#alerts",
        blockers_channel="#blockers",
    )
    channels = {"#alerts": "alerts-dm", "#blockers": "blockers-dm"}
    notifier = KanbanAlertNotifier(
        settings,
        state_path=tmp_path / "kanban-alerts.json",
        adapter_lookup=lambda platform, profile: adapter,
        resolve_channel=lambda platform, name: channels[name],
        lookup_channel_type=lambda platform, chat_id: "dm",
        list_known_channels=lambda platform: [
            {"id": "alerts-dm", "name": "alerts", "type": "dm"},
            {"id": "blockers-dm", "name": "blockers", "type": "dm"},
        ],
    )

    notifier.sync_scope(
        "dispatcher",
        [
            KanbanAlertIncident(
                key="ready-unspawned",
                route="automation",
                message="automation stalled",
            )
        ],
    )
    notifier.sync_scope(
        "blockers",
        [
            KanbanAlertIncident(
                key="blocker:default:t_123",
                route="blockers",
                message="Human decision needed for t_123",
                recovery_message="Human decision resolved for t_123",
                allow_dm=True,
            ),
            KanbanAlertIncident(
                key="blocker:default:t_456",
                route="blockers",
                message="Task t_456 is waiting for a dependency",
            ),
        ],
    )
    asyncio.run(notifier.flush())

    assert adapter.sent == [("blockers-dm", "Human decision needed for t_123")]

    notifier.sync_scope("blockers", [])
    asyncio.run(notifier.flush())

    assert adapter.sent == [("blockers-dm", "Human decision needed for t_123")]
    assert notifier.active_incidents("blockers") == []


def test_route_can_ground_profile_channel_directly_from_adapter(tmp_path):
    adapter = ListingAdapter()
    settings = KanbanAlertSettings(
        enabled=True,
        platform="buzz",
        profile="ops",
        automation_channel="#alerts",
        blockers_channel="#blockers",
    )
    notifier = KanbanAlertNotifier(
        settings,
        state_path=tmp_path / "kanban-alerts.json",
        adapter_lookup=lambda platform, profile: adapter,
        resolve_channel=lambda platform, name: None,
        lookup_channel_type=lambda platform, chat_id: None,
    )
    notifier.sync_scope(
        "dispatcher",
        [
            KanbanAlertIncident(
                key="ready-unspawned",
                route="automation",
                message="dispatcher stalled",
            )
        ],
    )

    asyncio.run(notifier.flush())

    assert adapter.sent == [("alerts-id", "dispatcher stalled")]


def test_profile_route_revalidates_process_global_directory_hit(tmp_path):
    adapter = ListingAdapter()
    settings = KanbanAlertSettings(
        enabled=True,
        platform="buzz",
        profile="ops",
        automation_channel="#alerts",
    )
    notifier = KanbanAlertNotifier(
        settings,
        state_path=tmp_path / "kanban-alerts.json",
        adapter_lookup=lambda platform, profile: adapter,
        resolve_channel=lambda platform, name: "other-profile-id",
        lookup_channel_type=lambda platform, chat_id: "channel",
    )
    notifier.sync_scope(
        "dispatcher",
        [
            KanbanAlertIncident(
                key="ready-unspawned",
                route="automation",
                message="dispatcher stalled",
            )
        ],
    )

    asyncio.run(notifier.flush())

    assert adapter.sent == [("alerts-id", "dispatcher stalled")]


def test_profile_route_rejects_ambiguous_adapter_channel_names(tmp_path):
    class AmbiguousAdapter(RecordingAdapter):
        async def list_channels(self):
            return [
                {"id": "id-a", "name": "alerts", "type": "channel"},
                {"id": "id-b", "name": "alerts", "type": "channel"},
            ]

    adapter = AmbiguousAdapter()
    notifier = KanbanAlertNotifier(
        KanbanAlertSettings(
            enabled=True,
            platform="buzz",
            profile="ops",
            automation_channel="#alerts",
        ),
        state_path=tmp_path / "kanban-alerts.json",
        adapter_lookup=lambda platform, profile: adapter,
        resolve_channel=lambda platform, name: "id-a",
        lookup_channel_type=lambda platform, chat_id: "channel",
    )
    notifier.sync_scope(
        "dispatcher",
        [KanbanAlertIncident("stalled", "automation", "dispatcher stalled")],
    )

    asyncio.run(notifier.flush())

    assert adapter.sent == []


def test_process_directory_route_rejects_ambiguous_channel_names(tmp_path):
    adapter = RecordingAdapter()
    notifier = KanbanAlertNotifier(
        KanbanAlertSettings(
            enabled=True,
            platform="discord",
            automation_channel="#alerts",
        ),
        state_path=tmp_path / "kanban-alerts.json",
        adapter_lookup=lambda platform, profile: adapter,
        resolve_channel=lambda platform, name: "id-a",
        lookup_channel_type=lambda platform, chat_id: "channel",
        list_known_channels=lambda platform: [
            {"id": "id-a", "name": "alerts", "type": "channel"},
            {"id": "id-b", "name": "alerts", "type": "channel"},
        ],
    )
    notifier.sync_scope(
        "dispatcher",
        [KanbanAlertIncident("stalled", "automation", "dispatcher stalled")],
    )

    asyncio.run(notifier.flush())

    assert adapter.sent == []


def test_destination_change_reopens_active_incident_on_new_channel(tmp_path):
    adapter = RecordingAdapter()
    state_path = tmp_path / "kanban-alerts.json"
    incident = KanbanAlertIncident("stalled", "automation", "dispatcher stalled")

    def make_notifier(channel: str, channel_id: str) -> KanbanAlertNotifier:
        return KanbanAlertNotifier(
            KanbanAlertSettings(
                enabled=True,
                platform="buzz",
                automation_channel=channel,
            ),
            state_path=state_path,
            adapter_lookup=lambda platform, profile: adapter,
            resolve_channel=lambda platform, name: channel_id,
            lookup_channel_type=lambda platform, chat_id: "channel",
            list_known_channels=lambda platform: [
                {
                    "id": channel_id,
                    "name": channel.lstrip("#"),
                    "type": "channel",
                }
            ],
        )

    first = make_notifier("#alerts", "alerts-id")
    first.sync_scope("dispatcher", [incident])
    asyncio.run(first.flush())
    second = make_notifier("#new-alerts", "new-alerts-id")
    second.sync_scope("dispatcher", [incident])
    asyncio.run(second.flush())

    assert adapter.sent == [
        ("alerts-id", "dispatcher stalled"),
        ("new-alerts-id", "dispatcher stalled"),
    ]


def test_removed_board_scope_is_retired_without_false_recovery(tmp_path):
    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)
    notifier.sync_scope(
        "blockers:retired",
        [
            KanbanAlertIncident(
                "blocker:retired:t_old",
                "blockers",
                "old blocker",
                "old blocker cleared",
                board="retired",
            )
        ],
    )
    asyncio.run(notifier.flush())

    notifier.retire_missing_scopes("blockers:", {"blockers:default"})
    asyncio.run(notifier.flush())

    assert notifier.active_incidents("blockers:retired") == []
    assert adapter.sent == [("blockers-id", "old blocker")]


def test_human_and_dependency_block_kinds_route_to_blockers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_ids = {}
        for kind in ("needs_input", "capability", "transient", "dependency"):
            task_id = kb.create_task(
                conn,
                title=f"{kind} task",
                assignee="worker",
                initial_status="running",
            )
            task_ids[kind] = task_id
            assert kb.block_task(
                conn,
                task_id,
                reason=f"reason for {kind}",
                kind=kind,
            )
    finally:
        conn.close()

    incidents = collect_routed_blocker_incidents(kb, boards=["default"])

    assert {incident.key for incident in incidents} == {
        f"blocker:default:{task_ids['needs_input']}",
        f"blocker:default:{task_ids['capability']}",
        f"blocker:default:{task_ids['dependency']}",
    }
    rendered = "\n".join(incident.message for incident in incidents)
    assert "reason for needs_input" in rendered
    assert "reason for capability" in rendered
    assert "reason for dependency" in rendered
    assert task_ids["transient"] not in rendered
    by_key = {incident.key: incident for incident in incidents}
    assert by_key[f"blocker:default:{task_ids['needs_input']}"].allow_dm is True
    assert by_key[f"blocker:default:{task_ids['capability']}"].allow_dm is True
    assert by_key[f"blocker:default:{task_ids['dependency']}"].allow_dm is False


def test_actionable_blocker_recovery_follows_real_unblock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Choose production region",
            assignee="worker",
            initial_status="running",
        )
        assert kb.block_task(
            conn,
            task_id,
            reason="Need a human region decision",
            kind="needs_input",
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)
    notifier.sync_scope(
        "blockers",
        collect_routed_blocker_incidents(kb, boards=["default"]),
    )
    asyncio.run(notifier.flush())

    conn = kb.connect()
    try:
        assert kb.unblock_task(conn, task_id)
    finally:
        conn.close()
    notifier.sync_scope(
        "blockers",
        collect_routed_blocker_incidents(kb, boards=["default"]),
    )
    asyncio.run(notifier.flush())

    assert len(adapter.sent) == 2
    assert adapter.sent[0][0] == "blockers-id"
    assert "Need a human region decision" in adapter.sent[0][1]
    assert "blocker cleared" in adapter.sent[1][1]


def test_corrupt_board_does_not_hide_healthy_board_blockers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Healthy board blocker",
            assignee="worker",
            initial_status="running",
        )
        assert kb.block_task(conn, task_id, reason="Need input", kind="needs_input")
    finally:
        conn.close()

    original_connect = kb.connect

    def connect(*, board=None, db_path=None):
        if board == "corrupt":
            raise OSError("corrupt board")
        return original_connect(board=board, db_path=db_path)

    monkeypatch.setattr(kb, "connect", connect)

    failed_boards = set()
    incidents = collect_routed_blocker_incidents(
        kb,
        boards=["corrupt", "default"],
        failed_boards=failed_boards,
    )

    assert [incident.key for incident in incidents] == [f"blocker:default:{task_id}"]
    assert failed_boards == {"corrupt"}

    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)
    notifier.sync_scope(
        "blockers:corrupt",
        [
            KanbanAlertIncident(
                "blocker:corrupt:t_old",
                "blockers",
                "corrupt-board blocker",
                "corrupt-board blocker cleared",
                board="corrupt",
            )
        ],
    )
    asyncio.run(notifier.flush())
    for board in ("corrupt", "default"):
        if board not in failed_boards:
            notifier.sync_scope(
                f"blockers:{board}",
                [incident for incident in incidents if incident.board == board],
            )
    asyncio.run(notifier.flush())

    assert all(
        "corrupt-board blocker cleared" not in message for _, message in adapter.sent
    )


def test_transient_alerts_are_cooldown_deduplicated(tmp_path):
    adapter = RecordingAdapter()
    clock = [1000.0]
    notifier = _notifier(tmp_path, adapter, now=lambda: clock[0])
    incident = KanbanAlertIncident(
        key="stale-auto-recovered:default:t_123",
        route="automation",
        message="Reclaimed a stale worker and respawned t_123 automatically.",
    )

    notifier.queue_transient(incident)
    notifier.queue_transient(incident)
    asyncio.run(notifier.flush())
    clock[0] += 299
    notifier.queue_transient(incident)
    asyncio.run(notifier.flush())
    clock[0] += 2
    notifier.queue_transient(incident)
    asyncio.run(notifier.flush())

    assert adapter.sent == [
        ("alerts-id", incident.message),
        ("alerts-id", incident.message),
    ]


def test_many_new_blockers_are_batched_and_truncated(tmp_path):
    adapter = RecordingAdapter()
    settings = KanbanAlertSettings(
        enabled=True,
        platform="buzz",
        automation_channel="#alerts",
        blockers_channel="#blockers",
        max_items_per_message=2,
    )
    notifier = KanbanAlertNotifier(
        settings,
        state_path=tmp_path / "kanban-alerts.json",
        adapter_lookup=lambda platform, profile: adapter,
        resolve_channel=lambda platform, name: f"{name[1:]}-id",
        lookup_channel_type=lambda platform, chat_id: "channel",
        list_known_channels=lambda platform: [
            {"id": "alerts-id", "name": "alerts", "type": "channel"},
            {"id": "blockers-id", "name": "blockers", "type": "channel"},
        ],
    )
    notifier.sync_scope(
        "blockers",
        [
            KanbanAlertIncident(
                key=f"blocker:default:t_{index}",
                route="blockers",
                message=f"blocker {index}",
                recovery_message=f"recovered {index}",
            )
            for index in range(3)
        ],
    )

    asyncio.run(notifier.flush())

    assert len(adapter.sent) == 1
    assert adapter.sent[0][0] == "blockers-id"
    assert "blocker 0" in adapter.sent[0][1]
    assert "blocker 1" in adapter.sent[0][1]
    assert "blocker 2" not in adapter.sent[0][1]
    assert "+1 more" in adapter.sent[0][1]


def test_dispatch_alerts_cover_stale_workers_stalls_and_recovery(tmp_path):
    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)
    stale = SimpleNamespace(stale=["t_stale"], spawned=[])

    record_dispatch_alerts(
        notifier,
        [("default", stale)],
        ready_stalled=True,
        ready_healthy=False,
        health_window=6,
    )
    asyncio.run(notifier.flush())
    record_dispatch_alerts(
        notifier,
        [("default", stale)],
        ready_stalled=True,
        ready_healthy=False,
        health_window=6,
    )
    asyncio.run(notifier.flush())
    record_dispatch_alerts(
        notifier,
        [("default", stale)],
        ready_stalled=False,
        ready_healthy=False,
        health_window=6,
    )
    asyncio.run(notifier.flush())
    assert len(adapter.sent) == 1
    record_dispatch_alerts(
        notifier,
        [
            (
                "default",
                SimpleNamespace(stale=[], spawned=[("t_stale", "worker", "/tmp/w")]),
            )
        ],
        ready_stalled=False,
        ready_healthy=True,
        health_window=6,
    )
    asyncio.run(notifier.flush())

    rendered = "\n".join(message for _chat_id, message in adapter.sent)
    assert len(adapter.sent) == 2
    assert "stale worker" in rendered
    assert "ready work" in rendered
    assert "t_stale" in rendered
    assert "recovered" in rendered


def test_dispatch_result_marks_normal_global_capacity_backpressure(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda profile: True)
    conn = kb.connect(board="default")
    try:
        running_id = kb.create_task(
            conn,
            title="already running",
            assignee="worker",
            initial_status="running",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (running_id,))
        assert kb.claim_task(conn, running_id, claimer="test-worker") is not None
        ready_id = kb.create_task(
            conn,
            title="waiting for capacity",
            assignee="worker",
            initial_status="running",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (ready_id,))
        result = kb.dispatch_once(
            conn,
            max_in_progress=1,
            dry_run=True,
            reconcile_orphans=False,
        )
    finally:
        conn.close()

    assert running_id
    assert result.spawned == []
    assert result.skipped_global_capped is True


def test_same_tick_stale_respawn_is_one_combined_notice(tmp_path):
    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)

    record_dispatch_alerts(
        notifier,
        [
            (
                "default",
                SimpleNamespace(
                    stale=["t_stale"],
                    spawned=[("t_stale", "worker", "/tmp/workspace")],
                ),
            )
        ],
        ready_stalled=False,
        ready_healthy=True,
        health_window=6,
    )
    asyncio.run(notifier.flush())

    assert len(adapter.sent) == 1
    assert "auto-recovered" in adapter.sent[0][1]
    assert "respawned" in adapter.sent[0][1]


def test_stale_incident_closes_when_task_becomes_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Stale task",
            assignee="worker",
            initial_status="running",
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)
    notifier.open_incident(
        "dispatcher-stale",
        KanbanAlertIncident(
            key=f"dispatcher:stale:default:{task_id}",
            route="automation",
            message="stale task is waiting to respawn",
            recovery_message="stale task respawned",
            board="default",
            task_id=task_id,
        ),
    )
    asyncio.run(notifier.flush())

    conn = kb.connect()
    try:
        assert kb.archive_task(conn, task_id)
    finally:
        conn.close()
    reconcile_stale_task_incidents(notifier, kb)
    asyncio.run(notifier.flush())

    assert len(adapter.sent) == 2
    assert "archived" in adapter.sent[1][1]


def test_alert_settings_load_from_kanban_config():
    settings = KanbanAlertSettings.from_config({
        "kanban": {
            "alerts": {
                "enabled": True,
                "platform": "buzz",
                "profile": "ops",
                "automation_channel": "#alerts",
                "blockers_channel": "#blockers",
                "cooldown_seconds": 120,
                "retry_seconds": 30,
                "max_items_per_message": 7,
                "health_window_ticks": 8,
            }
        }
    })

    assert settings == KanbanAlertSettings(
        enabled=True,
        platform="buzz",
        profile="ops",
        automation_channel="#alerts",
        blockers_channel="#blockers",
        cooldown_seconds=120,
        retry_seconds=30,
        max_items_per_message=7,
        health_window_ticks=8,
    )


def test_failed_delivery_retries_only_after_retry_interval(tmp_path):
    adapter = FailingAdapter()
    clock = [1000.0]
    notifier = _notifier(tmp_path, adapter, now=lambda: clock[0])
    notifier.sync_scope(
        "dispatcher",
        [
            KanbanAlertIncident(
                key="ready-unspawned",
                route="automation",
                message="dispatcher stalled",
            )
        ],
    )

    asyncio.run(notifier.flush())
    asyncio.run(notifier.flush())
    clock[0] += 61
    asyncio.run(notifier.flush())

    assert len(adapter.sent) == 2


def test_state_write_failure_does_not_block_delivery(tmp_path, monkeypatch):
    import utils

    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)

    def fail_write(path, data):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(utils, "atomic_json_write", fail_write)
    notifier.sync_scope(
        "dispatcher",
        [
            KanbanAlertIncident(
                key="ready-unspawned",
                route="automation",
                message="dispatcher stalled",
            )
        ],
    )
    asyncio.run(notifier.flush())

    assert adapter.sent == [("alerts-id", "dispatcher stalled")]


def test_restart_does_not_reannounce_active_incident(tmp_path):
    adapter = RecordingAdapter()
    incident = KanbanAlertIncident(
        key="ready-unspawned",
        route="automation",
        message="dispatcher stalled",
        recovery_message="dispatcher recovered",
    )
    first = _notifier(tmp_path, adapter)
    first.sync_scope("dispatcher", [incident])
    asyncio.run(first.flush())

    restarted = _notifier(tmp_path, adapter)
    restarted.sync_scope("dispatcher", [incident])
    asyncio.run(restarted.flush())
    restarted.sync_scope("dispatcher", [])
    asyncio.run(restarted.flush())

    assert adapter.sent == [
        ("alerts-id", "dispatcher stalled"),
        ("alerts-id", "dispatcher recovered"),
    ]


def test_persisted_alert_state_is_bounded(tmp_path):
    adapter = RecordingAdapter()
    notifier = _notifier(tmp_path, adapter)
    for index in range(600):
        notifier.queue_transient(
            KanbanAlertIncident(
                key=f"transient:{index}",
                route="automation",
                message=f"event {index}",
            )
        )

    state = json.loads((tmp_path / "kanban-alerts.json").read_text(encoding="utf-8"))

    assert len(state["pending_transients"]) <= 512


def test_gateway_dispatcher_tick_routes_results_and_blocker_snapshot(
    tmp_path, monkeypatch
):
    import hermes_cli.config as config_module
    import hermes_cli.kanban_db as kb_module
    import gateway.kanban_alerts as alerts_module
    import gateway.kanban_watchers as watchers_module
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._authorization_adapter = lambda platform, profile=None: None

    class FakeConnection:
        def close(self):
            return None

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            self.scopes = []
            self.opened = []
            self.flushed = 0

        def sync_scope(self, scope, incidents):
            self.scopes.append((scope, list(incidents)))

        def retire_missing_scopes(self, prefix, keep):
            return None

        def open_incident(self, scope, incident):
            self.opened.append((scope, incident))

        def active_incidents(self, scope):
            return []

        def resolve_incident(self, key):
            return None

        def queue_transient(self, incident):
            return None

        async def flush(self):
            self.flushed += 1

    created = []

    def make_notifier(*args, **kwargs):
        notifier = FakeNotifier(*args, **kwargs)
        created.append(notifier)
        return notifier

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "alerts": {
                    "enabled": True,
                    "platform": "buzz",
                    "automation_channel": "#alerts",
                    "blockers_channel": "#blockers",
                },
            }
        },
    )
    monkeypatch.setattr(alerts_module, "KanbanAlertNotifier", make_notifier)
    monkeypatch.setattr(
        watchers_module, "_acquire_singleton_lock", lambda path: (None, "unavailable")
    )
    monkeypatch.setattr(kb_module, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(
        kb_module,
        "list_boards",
        lambda include_archived=False: [{"slug": "default"}],
    )
    monkeypatch.setattr(kb_module, "connect", lambda **kwargs: FakeConnection())
    monkeypatch.setattr(kb_module, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb_module, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kb_module, "has_spawnable_review", lambda conn: False)
    monkeypatch.setattr(kb_module, "list_tasks", lambda conn, **kwargs: [])

    def dispatch_once(conn, **kwargs):
        runner._running = False
        return SimpleNamespace(stale=["t_stale"], spawned=[])

    monkeypatch.setattr(kb_module, "dispatch_once", dispatch_once)

    async def immediate_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert len(created) == 1
    notifier = created[0]
    assert notifier.flushed == 1
    assert {scope for scope, _incidents in notifier.scopes} == {
        "blockers:default",
        "dispatcher-ready",
    }
    assert [incident.key for _scope, incident in notifier.opened] == [
        "dispatcher:stale:default:t_stale"
    ]

    runner._running = True
    monkeypatch.setattr(watchers_module, "_kanban_dispatch_allowed", lambda: False)
    sleep_calls = 0

    async def stop_after_paused_tick(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", stop_after_paused_tick)
    asyncio.run(runner._kanban_dispatcher_watcher())

    paused_notifier = created[1]
    assert paused_notifier.flushed == 1
    assert [scope for scope, _incidents in paused_notifier.scopes] == [
        "blockers:default"
    ]
    assert paused_notifier.opened == []

    runner._running = True
    monkeypatch.setattr(watchers_module, "_kanban_dispatch_allowed", lambda: True)

    def unreadable_board(**kwargs):
        raise OSError("board unavailable")

    monkeypatch.setattr(kb_module, "connect", unreadable_board)
    sleep_calls = 0

    async def stop_after_unreadable_tick(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", stop_after_unreadable_tick)
    asyncio.run(runner._kanban_dispatcher_watcher())

    unreadable_notifier = created[2]
    assert unreadable_notifier.flushed == 1
    assert unreadable_notifier.scopes == []
    assert unreadable_notifier.opened == []
