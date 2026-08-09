from datetime import datetime, timedelta, timezone
from plugins.agentops.incident import IncidentOpsService, IncidentSignal
from plugins.agentops.incident.dashboard import ReadOnlyDashboard
from plugins.agentops.incident.state_machine import transition
from plugins.agentops.incident.models import ReviewResult
from plugins.agentops.incident.fingerprint import incident_fingerprint
from plugins.agentops.control.redaction import contains_secret
import hashlib
import pytest

def sig(t, target="hermes:profile:default:gateway", severity="warning", payload=None):
    return IncidentSignal(f"{target}:{int(t.timestamp())}:{severity}", target, "processes", "process.snapshot", t, payload or {"command_fingerprint":"sha256:"+"a"*64,"state":"failed"}, severity)

def test_stable_cross_target_correlation_and_window_split():
    now=datetime.now(timezone.utc); service=IncidentOpsService(window_seconds=60)
    first=service.ingest(sig(now)); second=service.ingest(sig(now+timedelta(seconds=30), "hermes:profile:default:gateway2")); assert first is second; assert first.signal_count==2
    third=service.ingest(sig(now+timedelta(seconds=91))); assert third is not first

def test_state_merge_split_and_notification_throttle():
    now=datetime.now(timezone.utc); service=IncidentOpsService(); incident=service.ingest(sig(now,"t")); assert service.notify(incident,now); assert not service.notify(incident,now+timedelta(seconds=1)); transition(incident,"acknowledged"); transition(incident,"resolved")

def test_review_degrades_without_model_and_digest_dashboard_are_bounded():
    now=datetime.now(timezone.utc); service=IncidentOpsService(); incident=service.ingest(sig(now,"t", "critical")); review=service.review(incident); assert review.degraded and not review.model_used and review.decision=="escalate"
    report=service.digest("daily",now); assert report["incident_count"]==1; dash=ReadOnlyDashboard([incident], token_hash=hashlib.sha256(b"short").hexdigest(), issued_at=now-timedelta(seconds=1), expiry=now+timedelta(hours=1)); assert dash.serve(auth_token="short", request="manifest")["chat"] is False and dash.serve(auth_token="short", request="incidents")["incidents"]


def test_signal_id_is_atomic_idempotency_and_payload_is_redacted_and_frozen():
    now = datetime.now(timezone.utc)
    raw = {"command_fingerprint": "sha256:" + "b" * 64, "nested": {"Authorization": "Bearer hunter2"}, "Cookie": "sid=secret"}
    first = sig(now, "t", payload=raw)
    raw["nested"]["state"] = "mutated"
    service = IncidentOpsService()
    incident = service.ingest(first)
    retry = IncidentSignal(first.signal_id, first.target_id, first.collector, first.signal_type, now + timedelta(hours=2), {"state": "different"})
    assert service.ingest(retry) is incident
    assert incident.signal_count == 1
    assert not contains_secret(first.payload)
    assert "hunter2" not in repr(first.payload) and "secret" not in repr(first.payload)


def test_fingerprint_drops_volatile_ids_and_normalizes_uuid():
    base = {"message_id": "one", "session_id": "a", "task_id": "x", "run_id": "r", "request_id": "q", "error": "down", "trace": "550e8400-e29b-41d4-a716-446655440000"}
    changed = {**base, "message_id": "two", "session_id": "b", "task_id": "y", "run_id": "s", "request_id": "z", "trace": "123e4567-e89b-12d3-a456-426614174000"}
    assert incident_fingerprint("x", base, collector="logs") == incident_fingerprint("x", changed, collector="logs")


def test_split_merge_suppress_keep_public_history_and_rebind_seen():
    now = datetime.now(timezone.utc)
    service = IncidentOpsService(max_history=20)
    first = service.ingest(sig(now, "a"))
    second = service.ingest(sig(now + timedelta(seconds=1), "b"))
    child = service.correlator.split(first, {first.evidence[0].signal_id})
    assert child in service.correlator.history() and child in service.correlator.all_incidents()
    assert first in service.correlator.incidents()
    service.correlator.merge(second, child)
    assert child not in service.correlator.incidents()
    assert service.ingest(child.evidence[0]) is second
    suppressed = service.correlator.suppress(second.fingerprint)
    assert suppressed.state == "suppressed" and any("suppressed" in item for item in suppressed.history)
    assert len(service.digest("daily", now)["incidents"]) >= 2


def test_dashboard_auth_expiry_and_direct_reads_fail_closed():
    now = datetime.now(timezone.utc)
    incident = IncidentOpsService().ingest(sig(now))
    dash = ReadOnlyDashboard([incident], token_hash=hashlib.sha256(b"short").hexdigest(), issued_at=now - timedelta(seconds=2), expiry=now - timedelta(seconds=1))
    with pytest.raises(PermissionError):
        dash.serve(auth_token="short", request="incidents")
    with pytest.raises(PermissionError):
        dash.serve(auth_token="wrong", request="manifest")
    with pytest.raises(PermissionError):
        dash.manifest()
    with pytest.raises(PermissionError):
        dash.incidents()


def test_review_schema_rejects_unsafe_types_and_degraded_actions():
    kwargs = dict(incident_fingerprint="sha256:" + "a" * 64, decision="observe", rationale="x", risk="low", confidence=0.2)
    with pytest.raises(ValueError): ReviewResult(**{**kwargs, "confidence": 1})
    with pytest.raises(ValueError): ReviewResult(**{**kwargs, "confidence": True})
    with pytest.raises(ValueError): ReviewResult(**{**kwargs, "risk": "bogus"})
    with pytest.raises(ValueError): ReviewResult(**{**kwargs, "degraded": True, "actions": ("restart",)})


def test_notification_period_is_monotonic_under_date_replay():
    from plugins.agentops.incident.notifier import NotificationGate
    incident = IncidentOpsService().ingest(sig(datetime(2026, 8, 2, 1, tzinfo=timezone.utc)))
    gate = NotificationGate(min_interval_seconds=0, max_per_period=1)
    assert gate.allow(incident, datetime(2026, 8, 2, 1, tzinfo=timezone.utc))
    assert not gate.allow(incident, datetime(2026, 8, 1, 1, tzinfo=timezone.utc))
    assert not gate.allow(incident, datetime(2026, 8, 2, 2, tzinfo=timezone.utc))
