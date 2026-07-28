"""Focused invariants for Cron final-result provenance storage."""
from __future__ import annotations

import base64
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

import cron.output_provenance as provenance
from cron.output_provenance import ProvenanceError, ProvenanceStore, SCHEMA_VERSION


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def _issue(store: ProvenanceStore, *, target_id: str = "t1", route: str = "sha256:route") -> dict:
    return store.issue(
        profile_id="atlas",
        job_id="job-1",
        occurrence_id="2026-07-27T00:00:00Z",
        target_id=target_id,
        route_digest=route,
        raw_body=b"daily report",
        template_digest="sha256:template",
        producer_class="llm_final",
    )


def test_bootstrap_and_issue_bind_raw_body(tmp_path):
    store = ProvenanceStore(tmp_path)
    anchor = store.bootstrap()

    issued = _issue(store)

    assert anchor["schema_version"] == SCHEMA_VERSION
    assert issued["proof"]["raw_sha256"].startswith("sha256:")
    assert base64.b64decode(issued["raw_body_b64"]) == b"daily report"
    assert issued["proof"]["state"] if "state" in issued["proof"] else True


def test_bootstrap_uses_exclusive_store_without_mutating_existing_cron_state(tmp_path):
    cron = tmp_path / "cron"
    cron.mkdir()
    jobs = cron / "jobs.json"
    lock = cron / ".jobs.lock"
    jobs.write_text('{"jobs":[]}', encoding="utf-8")
    lock.write_text("existing lock\n", encoding="utf-8")
    os.chmod(cron, 0o755)

    store = ProvenanceStore(tmp_path)
    store.bootstrap()

    assert store.root == cron / "output-provenance"
    assert jobs.read_text(encoding="utf-8") == '{"jobs":[]}'
    assert lock.read_text(encoding="utf-8") == "existing lock\n"
    assert stat.S_IMODE(cron.stat().st_mode) == 0o755


def test_same_occurrence_target_cannot_issue_twice(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    _issue(store)

    with pytest.raises(ProvenanceError, match="already issued"):
        _issue(store)


def test_same_target_route_cannot_be_substituted(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    _issue(store)

    with pytest.raises(ProvenanceError, match="route changed"):
        _issue(store, route="sha256:other-route")


def test_issue_requires_secure_bootstrap(tmp_path):
    with pytest.raises(ProvenanceError, match="missing provenance directory"):
        _issue(ProvenanceStore(tmp_path))


def test_claim_send_and_complete_are_ordered(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)

    claim = store.verify_and_claim(
        proof=issued["proof"],
        raw_body_b64=issued["raw_body_b64"],
        decision="allow",
    )
    body = base64.b64decode(claim["body_b64"])
    store.begin_send(
        capability_id=claim["capability_id"],
        claim_id=claim["claim_id"],
        body=body,
        rendered_body=b"wrapped daily report",
        route_digest="sha256:route",
    )
    store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent")
    ledger = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    target = next(iter(next(iter(ledger["occurrences"].values()))["targets"].values()))
    assert target["rendered_sha256"].startswith("sha256:")

    with pytest.raises(ProvenanceError, match="not prepared"):
        store.verify_and_claim(
            proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow"
        )


def test_rewrite_requires_canonical_body_and_deny_is_terminal(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)

    with pytest.raises(ProvenanceError, match="rewrite requires"):
        store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="rewrite")

    denied = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="deny")
    assert denied["decision"] == "deny"


def test_delimiter_bearing_occurrence_identity_does_not_collide(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    first = store.issue(
        profile_id="a:b", job_id="c", occurrence_id="d", target_id="t", route_digest="sha256:r1",
        raw_body=b"one", template_digest="sha256:t", producer_class="llm_final", now=NOW,
    )
    second = store.issue(
        profile_id="a", job_id="b:c", occurrence_id="d", target_id="t", route_digest="sha256:r2",
        raw_body=b"two", template_digest="sha256:t", producer_class="llm_final", now=NOW,
    )
    assert first["proof"]["capability_id"] != second["proof"]["capability_id"]


def test_expired_claim_is_durably_blocked(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)

    with pytest.raises(ProvenanceError, match="expired"):
        monkeypatch.setattr(provenance, "_now", lambda: datetime(2026, 7, 28, tzinfo=timezone.utc))
        store.verify_and_claim(
            proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow",
        )
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    with pytest.raises(ProvenanceError, match="not prepared"):
        store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")


def test_expired_claim_is_blocked_again_at_send_fence(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    monkeypatch.setattr(provenance, "_now", lambda: datetime(2026, 7, 28, tzinfo=timezone.utc))

    with pytest.raises(ProvenanceError, match="expired before send"):
        store.begin_send(
            capability_id=claim["capability_id"], claim_id=claim["claim_id"],
            body=base64.b64decode(claim["body_b64"]), rendered_body=b"daily report", route_digest="sha256:route",
        )

    ledger = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    target = next(iter(next(iter(ledger["occurrences"].values()))["targets"].values()))
    assert target["state"] == "blocked"


def test_malformed_expiry_at_send_fence_is_durably_blocked(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    ledger = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    target = next(iter(next(iter(ledger["occurrences"].values()))["targets"].values()))
    target["proof"]["expires_at"] = "not-a-date"
    store.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    with pytest.raises(ProvenanceError, match="invalid provenance expiry"):
        store.begin_send(
            capability_id=claim["capability_id"], claim_id=claim["claim_id"],
            body=base64.b64decode(claim["body_b64"]), rendered_body=b"daily report", route_digest="sha256:route",
        )
    persisted = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    assert next(iter(next(iter(persisted["occurrences"].values()))["targets"].values()))["state"] == "blocked"


def test_completion_persists_post_send_error(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    body = base64.b64decode(claim["body_b64"])
    store.begin_send(capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=body, rendered_body=body, route_digest="sha256:route")
    repair_context = {"event": "outbound:after_send", "route": {"platform": "telegram"}, "content": "daily report"}
    store.complete_claim(
        capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent",
        post_send_error="frame write failed", post_send_repair_context=repair_context,
    )
    ledger = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    target = next(iter(next(iter(ledger["occurrences"].values()))["targets"].values()))
    assert target["state"] == "sent"
    assert target["post_send_error"] == "frame write failed"
    event_id = f"after-send:{claim['capability_id']}:{claim['claim_id']}:sent"
    expected_context = {**repair_context, "observer_event_id": event_id}
    assert target["post_send_repair"] == {
        "state": "pending", "error": "frame write failed", "context": expected_context,
        "event_id": event_id, "attempts": 0,
    }
    assert store.pending_post_send_repairs() == [{
        "capability_id": claim["capability_id"], "error": "frame write failed", "context": expected_context,
    }]
    repair = store.claim_post_send_repair(capability_id=claim["capability_id"])
    assert repair["context"] == expected_context
    assert repair["event_id"] == event_id
    store.complete_post_send_repair(capability_id=claim["capability_id"], repair_id=repair["repair_id"], success=True)
    assert store.pending_post_send_repairs() == []
    repaired = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    repaired_target = next(iter(next(iter(repaired["occurrences"].values()))["targets"].values()))
    assert repaired_target["state"] == "sent"
    assert repaired_target["post_send_repair"]["state"] == "repaired"
    with pytest.raises(ProvenanceError, match="repair is not pending"):
        store.claim_post_send_repair(capability_id=claim["capability_id"])


def test_post_send_repair_rejects_missing_context_and_wrong_repair_lease(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    body = base64.b64decode(claim["body_b64"])
    store.begin_send(capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=body, rendered_body=body, route_digest="sha256:route")
    with pytest.raises(ProvenanceError, match="context is required"):
        store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent", post_send_error="observer failed")
    store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent", post_send_error="observer failed", post_send_repair_context={"event": "after"})
    repair = store.claim_post_send_repair(capability_id=claim["capability_id"])
    with pytest.raises(ProvenanceError, match="repair is not claimed"):
        store.complete_post_send_repair(capability_id=claim["capability_id"], repair_id="wrong", success=True)
    store.complete_post_send_repair(capability_id=claim["capability_id"], repair_id=repair["repair_id"], success=False, error="still failed")
    assert store.pending_post_send_repairs()[0]["error"] == "still failed"


@pytest.mark.parametrize("terminal", ["blocked", "indeterminate"])
def test_post_send_repair_is_consumable_for_every_terminal_without_resend(tmp_path, monkeypatch, terminal):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    body = base64.b64decode(claim["body_b64"])
    store.begin_send(capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=body, rendered_body=body, route_digest="sha256:route")
    store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result=terminal, post_send_error="observer failed", post_send_repair_context={"terminal": terminal})

    repair = store.claim_post_send_repair(capability_id=claim["capability_id"])
    assert repair["context"]["terminal"] == terminal
    assert repair["context"]["observer_event_id"].endswith(f":{terminal}")
    store.complete_post_send_repair(capability_id=claim["capability_id"], repair_id=repair["repair_id"], success=True)
    assert store.pending_post_send_repairs() == []


def test_post_send_repair_lease_reclaims_only_after_expiry(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    body = base64.b64decode(claim["body_b64"])
    store.begin_send(capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=body, rendered_body=body, route_digest="sha256:route")
    store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent", post_send_error="observer failed", post_send_repair_context={"event": "after"})
    first = store.claim_post_send_repair(capability_id=claim["capability_id"])
    with pytest.raises(ProvenanceError, match="not pending"):
        store.claim_post_send_repair(capability_id=claim["capability_id"])
    monkeypatch.setattr(provenance, "_now", lambda: NOW + timedelta(seconds=provenance.REPAIR_LEASE_SECONDS + 1))
    second = store.claim_post_send_repair(capability_id=claim["capability_id"])

    assert second["repair_id"] != first["repair_id"]
    assert second["event_id"] == first["event_id"]
    assert second["context"]["observer_event_id"] == first["context"]["observer_event_id"]
    assert second["event_id"] == second["context"]["observer_event_id"]


def test_post_send_repair_scan_reclaims_an_expired_worker_lease(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    body = base64.b64decode(claim["body_b64"])
    store.begin_send(capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=body, rendered_body=body, route_digest="sha256:route")
    store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent", post_send_repair_context={"event": "after"})
    first = store.claim_post_send_repair(capability_id=claim["capability_id"])
    assert store.pending_post_send_repairs() == []
    monkeypatch.setattr(provenance, "_now", lambda: NOW + timedelta(seconds=provenance.REPAIR_LEASE_SECONDS + 1))
    assert store.pending_post_send_repairs()[0]["capability_id"] == claim["capability_id"]
    second = store.claim_post_send_repair(capability_id=claim["capability_id"])
    assert second["repair_id"] != first["repair_id"]
    assert second["event_id"] == first["event_id"]


def test_post_send_repair_scan_reclaims_a_malformed_worker_lease(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    body = base64.b64decode(claim["body_b64"])
    store.begin_send(capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=body, rendered_body=body, route_digest="sha256:route")
    store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent", post_send_repair_context={"event": "after"})
    ledger = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    target = next(iter(next(iter(ledger["occurrences"].values()))["targets"].values()))
    target["post_send_repair"].update({"state": "claimed", "claimed_at": "not-a-date"})
    store.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    assert store.pending_post_send_repairs()[0]["capability_id"] == claim["capability_id"]
    reclaimed = store.claim_post_send_repair(capability_id=claim["capability_id"])
    assert reclaimed["event_id"].endswith(":sent")


def test_stale_send_started_converges_to_indeterminate_observer_repair(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    body = base64.b64decode(claim["body_b64"])
    store.begin_send(
        capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=body,
        rendered_body=body, route_digest="sha256:route", post_send_repair_context={"content": "body"},
    )
    monkeypatch.setattr(provenance, "_now", lambda: NOW + timedelta(seconds=provenance.SEND_RECOVERY_SECONDS + 1))

    assert store.pending_post_send_repairs() == [{
        "capability_id": claim["capability_id"], "error": "send_started_recovery_pending", "context": {"content": "body"},
    }]
    repair = store.claim_post_send_repair(capability_id=claim["capability_id"])

    assert repair["event_id"].endswith(":indeterminate")
    assert repair["context"]["send_result"] == {"error": "send_started_recovered"}
    assert repair["context"]["content"] == "body"


def test_begin_send_rejects_route_substitution(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(
        proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow"
    )

    with pytest.raises(ProvenanceError, match="route changed"):
        store.begin_send(
            capability_id=claim["capability_id"],
            claim_id=claim["claim_id"],
            body=base64.b64decode(claim["body_b64"]),
            rendered_body=b"wrapped daily report",
            route_digest="sha256:substituted-route",
        )


def test_claim_can_be_blocked_before_transport(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    claim = store.verify_and_claim(
        proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow"
    )

    store.block_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"])

    with pytest.raises(ProvenanceError, match="not prepared"):
        store.verify_and_claim(
            proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow"
        )


def test_bootstrap_requires_an_empty_private_store(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.root.mkdir(mode=0o700, parents=True)
    (store.root / "leftover").write_text("x", encoding="utf-8")

    with pytest.raises(ProvenanceError, match="empty store"):
        store.bootstrap()


@pytest.mark.parametrize("partial_name", [provenance.LOCK_NAME, provenance.LEDGER_NAME, provenance.KEY_NAME])
def test_bootstrap_recovers_a_private_pre_anchor_interruption(tmp_path, partial_name):
    store = ProvenanceStore(tmp_path)
    store.root.mkdir(mode=0o700, parents=True)
    partial = store.root / partial_name
    partial.write_bytes(b"partial")
    os.chmod(partial, 0o600)

    anchor = store.bootstrap()

    assert anchor["schema_version"] == provenance.SCHEMA_VERSION
    assert store.anchor_path.is_file()
    assert store.key_path.stat().st_size == 32


def test_bootstrap_is_idempotent_after_another_initializer_commits_the_anchor(tmp_path):
    store = ProvenanceStore(tmp_path)
    first = store.bootstrap()

    assert store.bootstrap() == first


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda store: os.chmod(store.root, 0o755), "directory permissions"),
        (lambda store: store.anchor_path.unlink(), "missing provenance path"),
        (lambda store: store.anchor_path.write_text("not-json", encoding="utf-8"), "malformed provenance anchor"),
        (lambda store: os.chmod(store.key_path, 0o644), "file permissions"),
    ],
)
def test_store_rejects_tampered_private_state(tmp_path, mutate, message):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    mutate(store)

    with pytest.raises(ProvenanceError, match=message):
        _issue(store)


def test_store_rejects_symlinked_private_state(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    target = tmp_path / "outside"
    target.write_text("outside", encoding="utf-8")
    store.key_path.unlink()
    store.key_path.symlink_to(target)

    with pytest.raises(ProvenanceError, match="regular file"):
        _issue(store)


def test_issue_rejects_incomplete_or_oversized_input(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()

    with pytest.raises(ProvenanceError, match="identity"):
        store.issue(
            profile_id="", job_id="job", occurrence_id="occurrence", target_id="target",
            route_digest="route", raw_body=b"x", template_digest="template", producer_class="final",
        )
    with pytest.raises(ProvenanceError, match="exceeds"):
        store.issue(
            profile_id="profile", job_id="job", occurrence_id="occurrence", target_id="target",
            route_digest="route", raw_body=b"x" * (provenance.MAX_BODY_BYTES + 1),
            template_digest="template", producer_class="final",
        )


def test_claim_rejects_tampering_and_invalid_decisions(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)

    with pytest.raises(ProvenanceError, match="invalid provenance decision"):
        store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="maybe")
    with pytest.raises(ProvenanceError, match="raw proof body encoding"):
        store.verify_and_claim(proof=issued["proof"], raw_body_b64="!", decision="allow")
    with pytest.raises(ProvenanceError, match="raw proof body hash"):
        store.verify_and_claim(
            proof=issued["proof"], raw_body_b64=base64.b64encode(b"substitute").decode("ascii"), decision="allow"
        )
    tampered = dict(issued["proof"], mac="0" * 64)
    with pytest.raises(ProvenanceError, match="durable capability"):
        store.verify_and_claim(proof=tampered, raw_body_b64=issued["raw_body_b64"], decision="allow")


def test_claim_rewrite_and_send_state_validation(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)

    with pytest.raises(ProvenanceError, match="replacement body encoding"):
        store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="rewrite", replacement_body_b64="!")
    with pytest.raises(ProvenanceError, match="replacement body violates"):
        store.verify_and_claim(
            proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="rewrite",
            replacement_body_b64=base64.b64encode(b"").decode("ascii"),
        )
    claim = store.verify_and_claim(
        proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="rewrite",
        replacement_body_b64=base64.b64encode(b"safe").decode("ascii"),
    )
    with pytest.raises(ProvenanceError, match="body changed"):
        store.begin_send(
            capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=b"other",
            rendered_body=b"safe", route_digest="sha256:route",
        )
    with pytest.raises(ProvenanceError, match="rendered provenance"):
        store.begin_send(
            capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=b"safe",
            rendered_body=b"", route_digest="sha256:route",
        )
    with pytest.raises(ProvenanceError, match="invalid provenance completion"):
        store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="lost")


def test_require_private_regular_rejects_wrong_inode_and_mode(tmp_path):
    path = tmp_path / "private"
    path.write_text("x", encoding="utf-8")
    os.chmod(path, 0o600)
    info = path.stat()
    with pytest.raises(ProvenanceError, match="identity changed"):
        provenance._require_private_regular(path, expected_inode=(info.st_dev, info.st_ino + 1))
    os.chmod(path, stat.S_IMODE(info.st_mode) | 0o040)
    with pytest.raises(ProvenanceError, match="permissions"):
        provenance._require_private_regular(path)


def test_private_directory_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ProvenanceError, match="directory is invalid"):
        provenance._require_private_dir(link)


def test_atomic_write_cleans_a_partial_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    monkeypatch.setattr(provenance.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        provenance._atomic_write(path, b"contents")

    assert not path.exists()
    assert not list(tmp_path.glob(".ledger.*"))


def test_atomic_write_keeps_original_error_when_cleanup_also_fails(tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    real_close = provenance.os.close
    real_unlink = provenance.os.unlink
    leaked_fds: list[int] = []

    def refuse_close(fd):
        leaked_fds.append(fd)
        raise OSError("close failed")

    monkeypatch.setattr(provenance.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(provenance.os, "close", refuse_close)
    monkeypatch.setattr(provenance.os, "unlink", lambda _path: (_ for _ in ()).throw(OSError("unlink failed")))
    with pytest.raises(OSError, match="disk full"):
        provenance._atomic_write(path, b"contents")

    monkeypatch.setattr(provenance.os, "close", real_close)
    monkeypatch.setattr(provenance.os, "unlink", real_unlink)
    for fd in set(leaked_fds):
        try:
            real_close(fd)
        except OSError:
            pass
    for temporary in tmp_path.glob(".ledger.*"):
        real_unlink(temporary)


def test_locking_rejects_missing_nofollow_and_changed_inode(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    nofollow = provenance.os.O_NOFOLLOW
    monkeypatch.setattr(provenance.os, "O_NOFOLLOW", 0, raising=False)
    with pytest.raises(ProvenanceError, match="O_NOFOLLOW"):
        with store._locked():
            pass
    monkeypatch.setattr(provenance.os, "O_NOFOLLOW", nofollow, raising=False)

    store = ProvenanceStore(tmp_path / "second")
    store.bootstrap()
    actual = provenance.os.fstat
    monkeypatch.setattr(provenance.os, "fstat", lambda fd: os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0)))
    with pytest.raises(ProvenanceError, match="changed while opening"):
        with store._locked():
            pass
    monkeypatch.setattr(provenance.os, "fstat", actual)


@pytest.mark.parametrize(
    ("ledger", "message"),
    [
        ("not-json", "malformed provenance ledger"),
        (json.dumps({"schema_version": "wrong", "occurrences": {}}), "unexpected provenance ledger schema"),
    ],
)
def test_locking_rejects_malformed_ledger(tmp_path, ledger, message):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    store.ledger_path.write_text(ledger, encoding="utf-8")
    with pytest.raises(ProvenanceError, match=message):
        _issue(store)


def test_claim_rejects_unknown_and_oversized_encoded_body(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)

    unknown = dict(issued["proof"], capability_id="unknown")
    with pytest.raises(ProvenanceError, match="unknown provenance capability"):
        store.verify_and_claim(proof=unknown, raw_body_b64=issued["raw_body_b64"], decision="allow")
    with pytest.raises(ProvenanceError, match="raw proof body exceeds"):
        store.verify_and_claim(
            proof=issued["proof"],
            raw_body_b64=base64.b64encode(b"x" * (provenance.MAX_BODY_BYTES + 1)).decode("ascii"),
            decision="allow",
        )


def test_claim_rejects_durable_mac_and_expiry_tampering(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)
    ledger = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    target = next(iter(next(iter(ledger["occurrences"].values()))["targets"].values()))

    target["proof"]["mac"] = "0" * 64
    store.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(ProvenanceError, match="invalid proof MAC"):
        store.verify_and_claim(proof=target["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")

    store = ProvenanceStore(tmp_path / "expiry")
    store.bootstrap()
    issued = _issue(store)
    ledger = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    target = next(iter(next(iter(ledger["occurrences"].values()))["targets"].values()))
    target["proof"]["expires_at"] = "not-a-date"
    key = store.key_path.read_bytes()
    target["proof"]["mac"] = provenance.hmac.new(
        key, provenance._canonical(provenance.ProvenanceStore._proof_mac_body(target["proof"])), provenance.hashlib.sha256
    ).hexdigest()
    store.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(ProvenanceError, match="invalid proof expiry"):
        store.verify_and_claim(proof=target["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")


def test_allow_deny_and_terminal_state_checks(tmp_path, monkeypatch):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = _issue(store)
    monkeypatch.setattr(provenance, "_now", lambda: NOW)

    with pytest.raises(ProvenanceError, match="allow must"):
        store.verify_and_claim(
            proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow", replacement_body_b64="eA=="
        )
    with pytest.raises(ProvenanceError, match="deny must"):
        store.verify_and_claim(
            proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="deny", replacement_body_b64="eA=="
        )
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    with pytest.raises(ProvenanceError, match="not sendable"):
        store.begin_send(capability_id=claim["capability_id"], claim_id="wrong", body=b"daily report", rendered_body=b"report", route_digest="sha256:route")
    with pytest.raises(ProvenanceError, match="not blockable"):
        store.block_claim(capability_id=claim["capability_id"], claim_id="wrong")
    with pytest.raises(ProvenanceError, match="not in send_started"):
        store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent")


def test_health_check_rejects_invalid_key_and_ledger_without_rewriting(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    before = store.ledger_path.read_bytes()
    store.health_check()
    assert store.ledger_path.read_bytes() == before

    store.key_path.write_bytes(b"too-short")
    with pytest.raises(ProvenanceError, match="invalid provenance key"):
        store.health_check()

    store = ProvenanceStore(tmp_path / "ledger")
    store.bootstrap()
    store.ledger_path.write_text('{"schema_version":"wrong","occurrences":{}}', encoding="utf-8")
    with pytest.raises(ProvenanceError, match="unexpected provenance ledger schema"):
        store.health_check()
