"""Tests for hermes_cli.update_receipt (#91277)."""

import json
from hermes_cli.update_receipt import UpdateReceipt, load_latest_update_receipt


def test_update_receipt_lifecycle(tmp_path):
    hermes_home = tmp_path / ".hermes"
    receipt = UpdateReceipt(
        target_branch="main",
        previous_commit="abc1234",
        updated_commit="def5678",
        deployment_kind="git+venv",
        profiles_discovered=["default", "coder"],
    )
    receipt.add_step("git_pull", "success", duration_sec=1.23, details="Pulled 3 commits")
    receipt.add_step("pip_install", "success", duration_sec=4.56)

    saved_path = receipt.write(hermes_home)
    assert saved_path is not None
    assert saved_path.is_file()

    latest_file = hermes_home / "updates" / "latest-receipt.json"
    assert latest_file.is_file()

    loaded = load_latest_update_receipt(hermes_home)
    assert loaded is not None
    assert loaded["status"] == "SUCCESS"
    assert loaded["target_branch"] == "main"
    assert loaded["previous_commit"] == "abc1234"
    assert loaded["updated_commit"] == "def5678"
    assert len(loaded["steps"]) == 2
    assert loaded["steps"][0]["name"] == "git_pull"


def test_update_receipt_partial_status_on_failure(tmp_path):
    hermes_home = tmp_path / ".hermes"
    receipt = UpdateReceipt()
    receipt.add_step("git_pull", "success")
    receipt.add_step("desktop_build", "failed", error="npm build error")

    assert receipt.status == "PARTIAL"
    receipt.write(hermes_home)

    loaded = load_latest_update_receipt(hermes_home)
    assert loaded["status"] == "PARTIAL"
    assert loaded["steps"][1]["error"] == "npm build error"
