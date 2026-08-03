"""Content-free telemetry contract for Modal lifecycle instrumentation."""

from __future__ import annotations


def test_modal_lifecycle_event_hashes_correlation_ids_and_drops_content(monkeypatch):
    from agent.monitoring import modal_lifecycle

    emitted: list[dict] = []
    monkeypatch.setattr(modal_lifecycle, "emit", emitted.append)

    modal_lifecycle.record(
        "sandbox.create",
        task_id="slack-thread-private",
        lease_id="lease-secret",
        sandbox_id="ta-01KZ3ZQV9WT2CHK6V01ZHEC2FS",
        image="public.ecr.aws/example/cua-driver@sha256:abc123",
        duration_ms=12,
        error=RuntimeError("Bearer super-secret /home/cua/private-command"),
    )

    assert emitted == [{
        "event": "modal_lifecycle",
        "provider": "modal",
        "operation": "sandbox.create",
        "result": "error",
        "duration_ms": 12,
        "error_class": "RuntimeError",
        "task_id_hash": modal_lifecycle.fingerprint("slack-thread-private"),
        "lease_id_hash": modal_lifecycle.fingerprint("lease-secret"),
        "sandbox_id_hash": modal_lifecycle.fingerprint("ta-01KZ3ZQV9WT2CHK6V01ZHEC2FS"),
        "image_ref": "public.ecr.aws/example/cua-driver@sha256:abc123",
    }]
    assert "super-secret" not in str(emitted)
    assert "private-command" not in str(emitted)


def test_modal_lifecycle_event_is_fail_open(monkeypatch):
    from agent.monitoring import modal_lifecycle

    def fail(_event):
        raise RuntimeError("monitoring unavailable")

    monkeypatch.setattr(modal_lifecycle, "emit", fail)
    modal_lifecycle.record("sandbox.cleanup", task_id="task")


def test_modal_lifecycle_span_attrs_are_allowlisted():
    from agent.monitoring.otlp_exporter import _span_attrs

    attrs = _span_attrs({
        "event": "modal_lifecycle",
        "provider": "modal",
        "operation": "mcp.tunnel",
        "result": "error",
        "duration_ms": 99,
        "error_class": "RuntimeError",
        "task_id_hash": "sha256:task",
        "lease_id_hash": "sha256:lease",
        "sandbox_id_hash": "sha256:sandbox",
        "image_ref": "public.ecr.aws/example/cua-driver@sha256:abc123",
        "command": "cat /etc/shadow",
        "error_message": "Bearer secret",
    })

    assert attrs == {
        "hermes.event": "modal_lifecycle",
        "hermes.provider": "modal",
        "hermes.operation": "mcp.tunnel",
        "hermes.result": "error",
        "hermes.duration_ms": 99,
        "hermes.error_class": "RuntimeError",
        "hermes.task_id_hash": "sha256:task",
        "hermes.lease_id_hash": "sha256:lease",
        "hermes.sandbox_id_hash": "sha256:sandbox",
        "hermes.image_ref": "public.ecr.aws/example/cua-driver@sha256:abc123",
    }


def test_modal_lifecycle_log_attrs_are_allowlisted():
    from agent.monitoring.gateway_health_export import _diagnostic_log_attributes

    attrs = _diagnostic_log_attributes({
        "event": "modal_lifecycle",
        "provider": "modal",
        "operation": "sandbox.create",
        "result": "error",
        "error_class": "RuntimeError",
        "task_id_hash": "sha256:task",
        "command": "cat /etc/shadow",
        "error_message": "Bearer secret",
    })

    assert attrs == {
        "hermes.provider": "modal",
        "hermes.operation": "sandbox.create",
        "hermes.result": "error",
        "hermes.error_class": "RuntimeError",
        "hermes.task_id_hash": "sha256:task",
    }
