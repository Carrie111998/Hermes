from __future__ import annotations

import json
import runpy
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
    """Copy the exact producer bytes, including an uncommitted safe-local fix."""
    from tools.computer_use.tool import _CUA_ATTESTATION_MODULES

    review = tmp_path / "review"
    for relative in _CUA_ATTESTATION_MODULES:
        source = ROOT / relative
        destination = review / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return review


def _committed_review_root(tmp_path: Path) -> Path:
    from tools.computer_use.tool import _CUA_ATTESTATION_MODULES

    review = tmp_path / "review"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", "--shared", str(ROOT), str(review)],
        check=True,
    )
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=review,
        check=True,
    )
    subprocess.run(
        ["git", "sparse-checkout", "set", "--no-cone", *_CUA_ATTESTATION_MODULES],
        cwd=review,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", "--detach", "HEAD"],
        cwd=review,
        check=True,
    )
    return review


def _verify(
    receipt: Path, review: Path, deployed: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--receipt",
            str(receipt),
            "--review-root",
            str(review),
            "--deployed-root",
            str(deployed),
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
    assert "verified reviewed content identity" in result.stdout


def test_verifier_writes_complete_live_verification_receipt(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    review = _review_root(tmp_path)
    verification_receipt = tmp_path / "live-verifier-receipt.json"
    command = [
        sys.executable, str(SCRIPT), "--receipt", str(receipt),
        "--review-root", str(review), "--deployed-root", str(ROOT),
        "--expected-commit", "4f818e9cf4f2ced855c0b73dee92a54a25b3df68",
        "--verification-receipt", str(verification_receipt),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode != 0  # The producer is dirty, so a commit claim must fail closed.
    live = json.loads(verification_receipt.read_text(encoding="utf-8"))
    assert live["command"] == command[1:]
    assert live["receipt_path"] == str(receipt)
    assert live["review_root"] == str(review)
    assert live["deployed_root"] == str(ROOT)
    assert live["expected_commit"] == "4f818e9cf4f2ced855c0b73dee92a54a25b3df68"
    assert live["verifier_sha256"]
    assert live["exit_code"] == result.returncode
    assert live["stdout"] == result.stdout
    assert live["stderr"] == result.stderr


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


def test_verifier_rejects_deployed_only_source_mutation(tmp_path, monkeypatch):
    """The deployed root is an independent byte-identity boundary."""
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    deployed = _review_root(tmp_path / "deployed")
    for relative, row in data["modules"].items():
        row["source_path"] = str((deployed / relative).resolve())
    target = deployed / "tools/approval.py"
    target.write_bytes(target.read_bytes() + b"\n# deployed-only mutation\n")
    receipt.write_text(json.dumps(data), encoding="utf-8")

    result = _verify(receipt, _review_root(tmp_path / "review"), deployed)
    assert result.returncode != 0
    assert "deployed module hash mismatch" in result.stderr


def test_verifier_rejects_live_launcher_or_parent_identity_mismatch(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["launcher"] = "C:/forged/launcher.exe"
    receipt.write_text(json.dumps(data), encoding="utf-8")

    result = _verify(receipt, _review_root(tmp_path))
    assert result.returncode != 0
    assert "live process launcher mismatch" in result.stderr

    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["parent"]["pid"] = 0
    receipt.write_text(json.dumps(data), encoding="utf-8")
    result = _verify(receipt, _review_root(tmp_path / "parent"))
    assert result.returncode != 0
    assert "live parent process identity mismatch" in result.stderr


def test_verifier_rejects_forged_clean_claim_for_dirty_review_source(tmp_path, monkeypatch):
    import hashlib

    receipt = _receipt(tmp_path, monkeypatch)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    review = _committed_review_root(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=review, text=True).strip()
    for relative, row in data["modules"].items():
        raw = (review / relative).read_bytes()
        row.update(
            source_path=str((review / relative).resolve()),
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    verifier = runpy.run_path(str(SCRIPT))
    compiled = {}
    for relative in data["modules"]:
        found = {}
        verifier["_codes"](
            compile(
                (review / relative).read_bytes(), str(review / relative), "exec",
                dont_inherit=True, optimize=sys.flags.optimize,
            ),
            found,
        )
        compiled[relative] = found
    for identity, row in data["callables"].items():
        code = compiled[row["source_relative_path"]][row["qualname"]]
        row["first_line"] = code.co_firstlineno
        row["code_sha256"] = verifier["_fingerprint"](code)
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
            str(review),
            "--expected-commit",
            head,
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "does not match clean HEAD" in result.stderr
