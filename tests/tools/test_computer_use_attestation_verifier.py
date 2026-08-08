from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_cua_gateway_attestation.py"


def _receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tools.computer_use import tool as computer_use

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    receipt = computer_use.write_computer_use_runtime_attestation()
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def _review_root(tmp_path: Path) -> Path:
    from tools.computer_use.tool import _CUA_ATTESTATION_MODULES

    review = tmp_path / "review"
    for relative in _CUA_ATTESTATION_MODULES:
        source = ROOT / relative
        destination = review / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return review


def _verify(receipt: Path, review: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--receipt",
            str(receipt),
            "--review-root",
            str(review),
            "--deployed-root",
            str(ROOT),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_verifier_accepts_exact_reviewed_source(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "reviewed content identity" in result.stdout


def test_verifier_recomputes_callable_fingerprint_from_reviewed_source(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    review = _review_root(tmp_path)
    target = review / "tools/computer_use/cua_backend.py"
    target.write_text(target.read_text(encoding="utf-8").replace("def _run_input_action(", "def _run_input_action(\n        # reviewed-source mutation\n", 1), encoding="utf-8")
    result = _verify(receipt, review)
    assert result.returncode != 0
    assert "module hash mismatch" in result.stderr or "callable fingerprint mismatch" in result.stderr


def test_verifier_rejects_missing_fixed_cua_policy_entry(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["callables"].pop("tools.computer_use.cua_backend:CuaDriverBackend._run_input_action")
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "fixed callable policy" in result.stderr


def test_verifier_rejects_python_semantic_mismatch(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["runtime"]["python_version"] = [0, 0, 0]
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "runtime Python semantics mismatch" in result.stderr


def test_verifier_rejects_receipt_controlled_deployed_source_path(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["modules"]["tools/computer_use/cua_backend.py"]["source_path"] = (
        "C:/forged/deployment/tools/computer_use/cua_backend.py"
    )
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "receipt deployed source path mismatch" in result.stderr


def test_verifier_rejects_forged_clean_claim_for_dirty_review_source(tmp_path, monkeypatch):
    import hashlib

    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    review = _review_root(tmp_path)
    subprocess.run(["git", "init"], cwd=review, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=review, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=review, check=True)
    subprocess.run(["git", "add", "."], cwd=review, check=True)
    subprocess.run(["git", "commit", "-m", "review baseline"], cwd=review, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=review, text=True).strip()
    target_relative = "tools/approval.py"
    target = review / target_relative
    target.write_text(target.read_text(encoding="utf-8") + "\n# forged dirty review bytes\n", encoding="utf-8")
    raw = target.read_bytes()
    data["modules"][target_relative]["size"] = len(raw)
    data["modules"][target_relative]["sha256"] = hashlib.sha256(raw).hexdigest()
    data["source_identity"]["kind"] = "git-clean"
    data["source_identity"]["repository"] = {
        "vcs": "git",
        "root": str(review),
        "head_commit": head,
        "head_tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=review, text=True).strip(),
    }
    for relative, row in data["source_identity"]["attested_paths"].items():
        row["matches_head"] = True
        row["worktree_sha256"] = data["modules"][relative]["sha256"]
        row["head_blob"] = subprocess.check_output(
            ["git", "rev-parse", f"HEAD:{relative}"], cwd=review, text=True
        ).strip()
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--receipt",
            str(receipt),
            "--review-root",
            str(review),
            "--deployed-root",
            str(ROOT),
            "--expected-commit",
            head,
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "does not match clean HEAD" in result.stderr
