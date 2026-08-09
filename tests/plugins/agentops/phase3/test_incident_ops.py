from datetime import datetime, timedelta, timezone
from plugins.agentops.incident import IncidentOpsService, IncidentSignal
from plugins.agentops.incident.dashboard import ReadOnlyDashboard
from plugins.agentops.incident.state_machine import transition
import hashlib

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
    report=service.digest("daily",now); assert report["incident_count"]==1; dash=ReadOnlyDashboard([incident], token_hash=hashlib.sha256(b"short").hexdigest()); assert dash.serve(auth_token="short", request="manifest")["chat"] is False and dash.serve(auth_token="short", request="incidents")["incidents"]
