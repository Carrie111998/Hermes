#!/usr/bin/env python3
"""Teren-gated controller for both-key Christopher processing activation.

Deployment never calls this. The trust-ladder activation orchestrator calls it
after arming the post-flip detector and retiring the disabled-state check.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


PRE_CHECK = "pa.tgg.christopher.runtime_invariants"
POST_CHECK = "pa.tgg.christopher.post_flip_outbound_invariants"
EXPECTED_HOST = "tgg-app-1"
EXPECTED_RUNNER = "tgg-christopher-post-flip-outbound-invariants"
BRIDGE_DETECTOR = "/opt/tgg-capture/whatsapp-bridge/scripts/post-flip-detector.mjs"
REMOTE_PYTHON = "/home/pclaw/apps/hermes-pcl/.venv/bin/python"
REMOTE_TRANSACTION = "/home/pclaw/apps/hermes-pcl/deploy/tgg/christopher/scripts/processing_activation_transaction.py"
GRANT_SCHEMA = "tgg-activation-controller-grant/v1"
EXPECTED_CONFIG_PATH = "/home/pclaw/.hermes-christopher-tgg/config.yaml"
EXPECTED_GATE_PATH = "/home/pclaw/.hermes-christopher-tgg/runtime/processing-gate.json"


def _assert_origin() -> str:
    repo = Path(__file__).resolve().parents[4]
    subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", "main"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    origin = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "origin/main"], text=True).strip()
    if head != origin:
        raise RuntimeError("Hermes activation controller is not running from origin/main")
    if subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip():
        raise RuntimeError("Hermes activation controller refuses a dirty canonical checkout")
    for relative in (
        "deploy/tgg/christopher/scripts/activate_processing.py",
        "deploy/tgg/christopher/scripts/processing_activation_transaction.py",
    ):
        local = (repo / relative).read_bytes()
        versioned = subprocess.check_output(["git", "-C", str(repo), "show", f"origin/main:{relative}"])
        if local != versioned:
            raise RuntimeError(f"Hermes activation file differs from origin/main: {relative}")
    return origin


def _assert_spec_contract(repo: Path) -> dict[str, Any]:
    spec_path = repo / "deploy/tgg/christopher/client-agent-deployment.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    processing = spec.get("spec", {}).get("processing") or {}
    expected = {
        "configPath": EXPECTED_CONFIG_PATH,
        "configKey": "pa.enabled",
        "gatePath": EXPECTED_GATE_PATH,
        "effectiveEnableRule": "pa.enabled AND processing-gate.enabled",
    }
    actual = {
        "configPath": processing.get("configPath", EXPECTED_CONFIG_PATH),
        "configKey": processing.get("configKey"),
        "gatePath": processing.get("gatePath"),
        "effectiveEnableRule": processing.get("effectiveEnableRule"),
    }
    if actual != expected:
        raise RuntimeError(f"processing activation literals diverge from deployment spec: {actual!r}")
    return actual


def _sign_controller_grant(
    *,
    private_key_path: str,
    transition_id: str,
    packet_hash: str,
    hermes_commit: str,
    authority_sha: str,
    pre_run_id: str,
    armed_run_id: str,
    remote_transaction_sha256: str,
) -> dict[str, str]:
    key_path = Path(private_key_path).expanduser().resolve()
    stat = key_path.lstat()
    if not key_path.is_file() or key_path.is_symlink() or stat.st_mode & 0o077:
        raise RuntimeError("controller private key must be a regular 0600-or-stricter file")
    private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise RuntimeError("controller private key is not Ed25519")
    now = datetime.now(timezone.utc)
    payload = {
        "schema": GRANT_SCHEMA,
        "purpose": "processing-activate",
        "transitionId": transition_id,
        "packetHash": packet_hash,
        "sourceEventId": f"{transition_id}:processing-activate",
        "authoritySha256": authority_sha,
        "hermesCommit": hermes_commit,
        "canonicalRunIds": {
            "preLive": pre_run_id,
            "armedDetector": armed_run_id,
        },
        "remoteTransactionSha256": remote_transaction_sha256,
        "issuedAt": now.isoformat().replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "nonce": str(uuid.uuid4()),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    return {
        "grantB64": base64.b64encode(payload_bytes).decode(),
        "grantSignatureB64": base64.b64encode(private.sign(payload_bytes)).decode(),
    }


def _run_json(argv: list[str]) -> dict[str, Any]:
    completed = subprocess.run(argv, check=True, text=True, capture_output=True)
    return json.loads(completed.stdout)


def _payload(envelope: dict[str, Any]) -> dict[str, Any]:
    if envelope.get("ok") is not True or not isinstance(envelope.get("data"), dict):
        raise RuntimeError(f"canonical command failed: {envelope}")
    return envelope["data"]


def _check_canary_owner() -> dict[str, Any]:
    mode = _payload(_run_json(["pcl", "config", "get", "--key", "pcl_check.mode"]))
    owners = _payload(
        _run_json(["pcl", "config", "get", "--key", "pcl_check.canary_owner_ids"])
    )
    owner_set = {value.strip() for value in str(owners.get("value") or "").split(",")}
    if mode.get("value") not in {"owner-canary", "fleet"} or (
        mode.get("value") == "owner-canary" and "edna" not in owner_set
    ):
        raise RuntimeError("Edna-owned canonical checks cannot self-fire under current pcl_check rollout")
    return {"mode": mode.get("value"), "canaryOwnerIds": sorted(owner_set)}


def _canonical_checks(pre_run_id: str) -> dict[str, Any]:
    board = _payload(
        _run_json(["pcl", "check", "board", "--format", "json", "--owner", "edna"])
    )
    rows = {row.get("logical_key"): row for row in board.get("checks", [])}
    pre = rows.get(PRE_CHECK)
    post = rows.get(POST_CHECK)
    if not pre or pre.get("enforcement_mode") != "alert":
        raise RuntimeError("pre-live invariant is absent or no longer alert-only")
    if pre.get("active") is not False or pre.get("last_run_id") != pre_run_id:
        raise RuntimeError("pre-live invariant is not canonically retired after the named pass")
    if not pre.get("last_pass_at"):
        raise RuntimeError("pre-live invariant has no canonical pass")
    if not post or post.get("active") is not True:
        raise RuntimeError("post-flip detector is not canonically active")
    if post.get("runner_ref") != EXPECTED_RUNNER or post.get("enforcement_mode") != "alert":
        raise RuntimeError("post-flip canonical definition does not match the activation contract")
    return {
        "pre": {
            "checkId": pre.get("check_id"),
            "runId": pre.get("last_run_id"),
            "lastPassAt": pre.get("last_pass_at"),
            "enforcementMode": pre.get("enforcement_mode"),
            "active": pre.get("active"),
        },
        "post": {
            "checkId": post.get("check_id"),
            "runnerRef": post.get("runner_ref"),
            "enforcementMode": post.get("enforcement_mode"),
            "active": post.get("active"),
        },
    }


def _teren_verdict(verdict_id: str, reviewed_ref: str) -> dict[str, Any]:
    data = _payload(
        _run_json(
            [
                "pcl",
                "review-verdict",
                "list",
                "--surface",
                "tgg-trust-ladder",
                "--reviewed-ref",
                reviewed_ref,
                "--reviewer",
                "teren",
                "--verdict",
                "go",
                "--limit",
                "20",
            ]
        )
    )
    verdict = next((row for row in data.get("verdicts", []) if row.get("id") == verdict_id), None)
    proof = (verdict or {}).get("metadata", {}).get("proof", {})
    if not verdict or verdict.get("voided_at") or verdict.get("superseded_by"):
        raise RuntimeError("named Teren verdict is absent, voided, or superseded")
    if proof.get("verified") is not True or proof.get("reviewer") != "teren":
        raise RuntimeError("named Teren verdict lacks canonical principal proof")
    if verdict.get("reviewed_ref") != reviewed_ref or proof.get("reviewed_ref") != reviewed_ref:
        raise RuntimeError("Teren verdict is not bound to this activation")
    return {
        "id": verdict.get("id"),
        "reviewedRef": reviewed_ref,
        "proofSourceKind": proof.get("source_kind"),
        "proofSourceId": proof.get("source_id"),
    }


def _resolve_target() -> str:
    data = _payload(
        _run_json(["pcl", "service", "locate", "--system", "christopher", "--domain", "pa"])
    )
    host = data.get("system", {}).get("liveFacts", {}).get("host", {})
    target = str(host.get("sshTargetAlias") or "")
    if host.get("lifecycleState") != "active" or target != EXPECTED_HOST:
        raise RuntimeError(f"Bedrock target mismatch: {target!r}")
    remote = subprocess.run(["ssh", target, "hostname"], check=True, text=True, capture_output=True)
    if remote.stdout.strip() != EXPECTED_HOST:
        raise RuntimeError("remote host identity mismatch")
    return target


def _bridge_preflight(target: str, manifest_sha: str, authority_sha: str) -> dict[str, Any]:
    for label, value in (("manifest", manifest_sha), ("authority", authority_sha)):
        if not re.fullmatch(r"[a-f0-9]{64}", value or ""):
            raise RuntimeError(f"expected bridge {label} sha is invalid")
    completed = subprocess.run(
        [
            "ssh",
            target,
            "node",
            BRIDGE_DETECTOR,
            "--window-seconds",
            "3600",
            "--expected-manifest-sha",
            manifest_sha,
            "--expected-authority-sha",
            authority_sha,
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=120,
    )
    result = json.loads(completed.stdout)
    policy = result.get("activePolicy", {})
    authority = result.get("rungAuthority", {})
    if (
        result.get("status") != "pass"
        or policy.get("released") is not False
        or authority.get("releaseState") != "armed"
    ):
        raise RuntimeError("bridge is not detector-green and fail-closed at an armed rung")
    return {
        "manifestSha256": result.get("sourceIntegrity", {}).get("manifestSha256"),
        "authoritySha256": authority.get("sha256"),
        "rung": authority.get("rung"),
        "releaseState": authority.get("releaseState"),
        "processIdentity": result.get("runtime", {}).get("processIdentity"),
    }


def _remote_transition(target: str, request: dict[str, Any]) -> dict[str, Any]:
    encoded = base64.b64encode(json.dumps(request, sort_keys=True).encode()).decode()
    completed = subprocess.run(
        [
            "ssh",
            target,
            REMOTE_PYTHON,
            REMOTE_TRANSACTION,
            "--request-b64",
            encoded,
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("activate", "deactivate", "status"))
    parser.add_argument("--expected-host", required=True, choices=(EXPECTED_HOST,))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt")
    parser.add_argument("--pre-check-run-id")
    parser.add_argument("--teren-verdict-id")
    parser.add_argument("--reviewed-ref")
    parser.add_argument("--expected-bridge-manifest-sha")
    parser.add_argument("--expected-authority-sha")
    parser.add_argument("--transition-id")
    parser.add_argument("--transition-packet-hash")
    parser.add_argument("--armed-detector-run-id")
    parser.add_argument("--controller-private-key")
    parser.add_argument("--reason")
    args = parser.parse_args()

    source_commit = _assert_origin()
    repo = Path(__file__).resolve().parents[4]
    spec_contract = _assert_spec_contract(repo)
    remote_transaction_sha256 = hashlib.sha256(
        (repo / "deploy/tgg/christopher/scripts/processing_activation_transaction.py").read_bytes()
    ).hexdigest()
    target = _resolve_target()
    common: dict[str, Any] = {
        "target": target,
        "mode": args.mode,
        "apply": args.apply,
        "sourceCommit": source_commit,
        "remoteTransactionSha256": remote_transaction_sha256,
        "specContract": spec_contract,
    }
    if args.mode == "status":
        status = _remote_transition(target, {"mode": "status"})
        print(json.dumps({"ok": True, **status}, sort_keys=True))
        return 0
    if args.mode == "activate":
        required = {
            "--pre-check-run-id": args.pre_check_run_id,
            "--teren-verdict-id": args.teren_verdict_id,
            "--reviewed-ref": args.reviewed_ref,
            "--expected-bridge-manifest-sha": args.expected_bridge_manifest_sha,
            "--expected-authority-sha": args.expected_authority_sha,
            "--transition-id": args.transition_id,
            "--transition-packet-hash": args.transition_packet_hash,
            "--armed-detector-run-id": args.armed_detector_run_id,
            "--controller-private-key": args.controller_private_key,
        }
        missing = [flag for flag, value in required.items() if not value]
        if missing:
            parser.error("activation requires " + ", ".join(missing))
        common.update(
            {
                "canary": _check_canary_owner(),
                "checks": _canonical_checks(args.pre_check_run_id),
                "verdict": _teren_verdict(args.teren_verdict_id, args.reviewed_ref),
                "bridge": _bridge_preflight(
                    target,
                    args.expected_bridge_manifest_sha,
                    args.expected_authority_sha,
                ),
            }
        )
    else:
        if not args.reason or len(args.reason.strip()) < 10:
            parser.error("deactivation requires --reason with at least 10 characters")
        common["reason"] = args.reason.strip()

    if not args.apply:
        print(json.dumps({"ok": True, "preview": common}, sort_keys=True))
        return 0
    if not args.receipt:
        parser.error("--apply requires --receipt")

    request = {
        "mode": args.mode,
        "terenVerdictId": args.teren_verdict_id,
        "preCheckRunId": args.pre_check_run_id,
    }
    if args.mode == "activate":
        signed = _sign_controller_grant(
            private_key_path=args.controller_private_key,
            transition_id=args.transition_id,
            packet_hash=args.transition_packet_hash,
            hermes_commit=source_commit,
            authority_sha=args.expected_authority_sha,
            pre_run_id=args.pre_check_run_id,
            armed_run_id=args.armed_detector_run_id,
            remote_transaction_sha256=remote_transaction_sha256,
        )
        request.update(
            {
                "transitionId": args.transition_id,
                "transitionPacketHash": args.transition_packet_hash,
                "armedDetectorRunId": args.armed_detector_run_id,
                "authoritySha256": args.expected_authority_sha,
                "hermesCommit": source_commit,
                "remoteTransactionSha256": remote_transaction_sha256,
                **signed,
            }
        )
    remote = _remote_transition(target, request)
    receipt_path = Path(args.receipt).expanduser().resolve()
    receipt = {"schema": "tgg-processing-activation-control/v1", **common, "remote": remote}
    _write_receipt(receipt_path, receipt)
    print(json.dumps({"ok": True, "receipt": str(receipt_path), "runId": remote.get("runId")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
