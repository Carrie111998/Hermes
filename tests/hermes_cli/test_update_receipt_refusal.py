from __future__ import annotations

import json

import hermes_cli.update_receipt as receipts


def test_refusal_receipt_public_api_preserves_correlation_and_drops_unknown_data(
    monkeypatch, tmp_path
):
    receipt_dir = tmp_path / "receipts"
    correlation_id = "a" * 32
    monkeypatch.setenv("HERMES_ACTION_ID", correlation_id)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: receipt_dir)
    receipts._current = None

    returned_id = receipts.begin_update_receipt(
        surface="dashboard_api",
        requested_target="main",
    )
    assert returned_id == correlation_id
    assert receipts.has_active_update_receipt() is True
    assert receipts.active_update_correlation_id() == correlation_id

    receipts.record_refusal(
        {
            "code": "image_managed_update_refused",
            "message": "pull the image and recreate",
            "update_command": "docker pull nousresearch/hermes-agent:latest",
            "surface": "dashboard_api",
            "requested_target": "main",
            "correlation_id": correlation_id,
            "credential": "must-not-be-persisted",
        }
    )
    path = receipts.finalize_update_receipt(
        "refused",
        stop_reason="image_managed_update_refused",
    )

    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["correlation_id"] == correlation_id
    assert payload["surface"] == "dashboard_api"
    assert payload["requested_target"] == "main"
    assert payload["refusal"]["code"] == "image_managed_update_refused"
    assert "credential" not in payload["refusal"]
    assert receipts.has_active_update_receipt() is False


def test_invalid_environment_action_id_is_not_copied(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ACTION_ID", "not-safe secret material")
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    correlation_id = receipts.begin_update_receipt(surface="cli")

    assert correlation_id is not None
    assert len(correlation_id) == 32
    assert all(character in "0123456789abcdef" for character in correlation_id)
    assert correlation_id != "not-safe secret material"
    receipts.finalize_update_receipt("success")


def test_authoritative_identity_bypasses_live_probe_at_both_receipt_edges(
    monkeypatch, tmp_path
):
    identity = {"sha": None, "version": "0.20.5"}

    def _forbidden(*args, **kwargs):
        raise AssertionError("receipt crossed authoritative identity boundary")

    monkeypatch.setattr("hermes_cli.build_info.get_code_identity", _forbidden)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    receipts.begin_update_receipt(surface="cli", code_identity=identity)
    path = receipts.finalize_update_receipt(
        "refused",
        code_identity=identity,
    )

    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pre_update"] == identity
    assert payload["post_update"] == identity


def test_same_second_same_process_receipts_never_overwrite(monkeypatch, tmp_path):
    receipt_dir = tmp_path / "receipts"
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: receipt_dir)
    monkeypatch.setattr(receipts.time, "strftime", lambda _fmt: "20260823_120000")
    receipts._current = None

    receipts.begin_update_receipt(
        surface="dashboard_api",
        correlation_id="a" * 32,
        code_identity={},
    )
    first = receipts.finalize_update_receipt("refused", code_identity={})
    receipts.begin_update_receipt(
        surface="dashboard_api",
        correlation_id="b" * 32,
        code_identity={},
    )
    second = receipts.finalize_update_receipt("refused", code_identity={})

    assert first is not None and second is not None
    assert first != second
    assert first.is_file() and second.is_file()
    assert len(list(receipt_dir.glob("update_*.json"))) == 2
    latest = json.loads((receipt_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["correlation_id"] == "b" * 32
