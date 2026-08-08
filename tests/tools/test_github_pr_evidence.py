"""Security and completeness tests for route-scoped GitHub PR evidence."""

import asyncio
import base64
import copy
import io
import json
import os
import hashlib
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from model_tools import _clear_tool_defs_cache, get_tool_definitions
from tools.github_pr_evidence import (
    _Cursor,
    _is_canonical_gate_path,
    _new_blob_cursor,
    _new_cursor,
    EvidenceScope,
    execution_evidence_complete_for,
    evidence_complete_for,
    evidence_scope,
    github_pr_evidence_tool,
    record_gate_resolution,
    record_execution_attestation,
    review_evidence_complete_for,
)


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _comparison(merge_base_sha=BASE_SHA):
    return {
        "base_commit": {"sha": BASE_SHA},
        "merge_base_commit": {"sha": merge_base_sha},
    }


def _scope(pr_number=42):
    return EvidenceScope(
        contract_version="v2",
        repository="org/repo",
        pr_number=pr_number,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )


def _result(payload):
    return MagicMock(
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


def _bytes_result(payload):
    return MagicMock(returncode=0, stdout=payload, stderr=b"")


def _install_gate_resolution(scope, private_key, *, contracts=None):
    scope.execution_attestation_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    scope.execution_gate_policy_version = "newtonsapple-v1"
    scope.execution_gate_policy_sha256 = "f" * 64
    scope.baseline_execution_gates = ("quality", "integration", "e2e")
    contracts = contracts or {
        gate: {
            "kind": "command",
            "command": ["npm", "run", gate],
            "executor": "github_actions",
            "runner": {"kind": "github_actions", "name": "ubuntu-latest"},
            "status": "pass",
            "exit_codes": [0],
        }
        for gate in ("quality", "integration", "e2e", "voice-eval")
    }
    manifest = {
        **scope.tuple_dict,
        "policy_version": scope.execution_gate_policy_version,
        "policy_sha256": scope.execution_gate_policy_sha256,
        "baseline_gates": list(scope.baseline_execution_gates),
        "resolved_gates": list(contracts),
        "gate_contracts": contracts,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
    with evidence_scope(scope):
        assert record_gate_resolution(payload, signature) is True
    return hashlib.sha256(payload).hexdigest()


def _gate_record(scope, gate, *, status="pass", executor="github_actions"):
    evidence = (
        {
            "kind": "github_actions",
            "url": "https://github.com/org/repo/actions/runs/1/job/123",
            "job_id": 123,
            "log_sha256": "e" * 64,
        }
        if executor == "github_actions"
        else {
            "kind": "artifact",
            "path": f"artifacts/{gate}.log",
            "artifact_sha256": "e" * 64,
        }
    )
    if executor == "github_actions":
        scope.observed_action_jobs[123] = {
            "run_id": 1,
            "url": "https://github.com/org/repo/actions/runs/1/job/123",
            "conclusion": "success" if status == "pass" else "failure",
            "log_sha256": "e" * 64,
        }
    return {
        "id": gate,
        "executor": executor,
        "runner": {"kind": executor, "name": "ubuntu-latest", "job_id": 123},
        "status": status,
        "head_sha": scope.head_sha,
        "attempted": True,
        "command": ["npm", "run", gate],
        "exit_code": 0,
        "started_at": "2026-08-05T18:00:00Z",
        "completed_at": "2026-08-05T18:01:00Z",
        "duration_ms": 60_000,
        "tree_before": scope.head_tree_sha,
        "tree_after": scope.head_tree_sha,
        "evidence": evidence,
    }


def _signed_execution_attestation(
    scope, *, gates=None, preflight=None, worker_required=True
):
    private_key = Ed25519PrivateKey.generate()
    scope.base_tree_sha = "c" * 40
    scope.head_tree_sha = "d" * 40
    manifest_sha256 = _install_gate_resolution(scope, private_key)
    payload = json.dumps(
        {
            "contract_version": "v2",
            "repository": scope.repository,
            "pr_number": scope.pr_number,
            "base_sha": scope.base_sha,
            "head_sha": scope.head_sha,
            "base_tree_sha": scope.base_tree_sha,
            "head_tree_sha": scope.head_tree_sha,
            "gate_resolution": {
                "policy_version": scope.execution_gate_policy_version,
                "policy_sha256": scope.execution_gate_policy_sha256,
                "manifest_sha256": manifest_sha256,
                "resolved_gates": list(scope.required_execution_gates),
            },
            "worker": (
                {
                    "required": True,
                    "head_sha": scope.head_sha,
                    "base_present": True,
                    "tree_before": scope.head_tree_sha,
                    "tree_after": scope.head_tree_sha,
                    "preflight": preflight
                    or {
                        "disposable_home": True,
                        "credentials_absent": True,
                        "host_mounts_absent": True,
                        "host_docker_socket_absent": True,
                        "resources_bounded": True,
                        "egress_default_deny": True,
                    },
                }
                if worker_required
                else {"required": False}
            ),
            "gates": gates or [_gate_record(scope, gate) for gate in scope.required_execution_gates],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
    return payload, signature


@pytest.fixture(autouse=True)
def clear_tool_cache():
    _clear_tool_defs_cache()
    yield
    _clear_tool_defs_cache()


def _tool_names():
    return {
        item["function"]["name"]
        for item in get_tool_definitions(
            enabled_toolsets=["hermes-webhook"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
    }


def test_tool_is_hidden_without_a_route_scope_and_visible_only_inside_one():
    assert "github_pr_evidence" not in _tool_names()

    with evidence_scope(_scope()):
        assert "github_pr_evidence" in _tool_names()

    assert "github_pr_evidence" not in _tool_names()


def test_concurrent_scopes_do_not_share_tuple_or_cursors():
    barrier = __import__("threading").Barrier(2)

    def inspect(pr_number):
        with evidence_scope(_scope(pr_number)):
            barrier.wait()
            result = json.loads(github_pr_evidence_tool("manifest"))
            return result["tuple"]["pr_number"], result["cursors"]

    with patch("tools.github_pr_evidence.subprocess.run") as run:
        run.return_value = _result({})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(inspect, (41, 42)))

    assert first[0] == 41
    assert second[0] == 42
    assert set(first[1].values()).isdisjoint(second[1].values())


def test_manifest_exposes_only_opaque_cursors_bound_to_fixed_endpoints():
    with evidence_scope(_scope()):
        result = json.loads(github_pr_evidence_tool("manifest"))

    assert result["tuple"] == {
        "contract_version": "v2",
        "repository": "org/repo",
        "pr_number": 42,
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
    }
    assert set(result["cursors"]) == {
        "pull_request",
        "closing_issues",
        "tree_diff",
        "changed_files",
        "issue_comments",
        "reviews",
        "review_comments",
        "commits",
        "checks",
        "statuses",
        "workflow_runs",
    }
    assert all("/" not in token and len(token) >= 20 for token in result["cursors"].values())
    assert "repository" not in result["next_parameters"]
    assert "ref" not in result["next_parameters"]
    assert "path" not in result["next_parameters"]


def test_concise_manifest_exposes_only_tuple_diff_and_file_summary():
    scope = _scope()
    scope.concise_review = True
    with evidence_scope(scope):
        result = json.loads(github_pr_evidence_tool("manifest"))

    assert set(result["cursors"]) == {
        "pull_request",
        "tree_diff",
        "changed_files",
    }
    assert result["current_required_cursors"]["total"] == 3


def test_recalled_manifest_recovers_only_current_required_cursors():
    scope = _scope()
    with evidence_scope(scope):
        first = json.loads(github_pr_evidence_tool("manifest"))
        consumed = first["cursors"]["pull_request"]
        scope.cursors.pop(consumed)
        recalled = json.loads(github_pr_evidence_tool("manifest"))

    inventory = recalled["current_required_cursors"]
    assert inventory["total"] == 10
    assert inventory["truncated"] is False
    assert consumed not in {item["cursor"] for item in inventory["items"]}
    assert consumed not in recalled["cursors"].values()
    assert {item["cursor"] for item in inventory["items"]} == scope.required_cursors


def test_manifest_recovery_inventory_is_bounded_and_reports_truncation():
    scope = _scope()
    with evidence_scope(scope), patch(
        "tools.github_pr_evidence._MAX_RECOVERY_CURSOR_INVENTORY", 2
    ):
        manifest = json.loads(github_pr_evidence_tool("manifest"))

    inventory = manifest["current_required_cursors"]
    assert inventory["total"] == 11
    assert inventory["truncated"] is True
    assert len(inventory["items"]) == 2


def test_recalled_manifest_exposes_only_one_live_cursor_window():
    scope = _scope()
    with evidence_scope(scope):
        first = json.loads(github_pr_evidence_tool("manifest"))
        for token in first["cursors"].values():
            scope.cursors.pop(token)
        for index in range(100):
            _new_cursor(scope, _Cursor(kind="data", data={"index": index}))

        recalled = json.loads(github_pr_evidence_tool("manifest"))

    inventory = recalled["current_required_cursors"]
    assert recalled["cursors"] == {}
    assert inventory["total"] == 100
    assert inventory["truncated"] is True
    assert len(inventory["items"]) == 16
    assert {item["cursor"] for item in inventory["items"]} == scope.exposed_cursors


def test_cursor_exposure_is_a_bounded_rolling_window():
    scope = _scope()
    with evidence_scope(scope):
        children = [
            _new_cursor(scope, _Cursor(kind="data", data={"index": index}))
            for index in range(40)
        ]
        parent = _new_cursor(
            scope,
            _Cursor(kind="data", data={"child_cursors": children}),
        )
        first = json.loads(github_pr_evidence_tool("read", parent))

        visible = first["items"]["child_cursors"]
        assert len(visible) == 16
        assert first["cursor_exposure"] == {
            "shown": 16,
            "hidden": 24,
            "live_window": 16,
            "window_limit": 16,
        }
        assert first["next_required_cursors"] == []
        assert len(scope.exposed_cursors) == 16

        consumed = visible[0]
        second = json.loads(github_pr_evidence_tool("read", consumed))

    assert len(second["next_required_cursors"]) == 1
    replacement = second["next_required_cursors"][0]["cursor"]
    assert replacement not in visible
    assert consumed not in scope.exposed_cursors
    assert len(scope.exposed_cursors) == 16


def test_execution_control_plane_cursors_enforce_resolution_then_attestation_order():
    scope = _scope()
    scope.execution_attestation_loader = MagicMock()
    scope.gate_resolution_loader = MagicMock()

    with evidence_scope(scope):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        resolution_token = manifest["cursors"]["gate_resolution"]
        attestation_token = manifest["cursors"]["execution_attestation"]

        assert scope.cursors[resolution_token].required is True
        assert scope.cursors[attestation_token].required is False

        premature = json.loads(
            github_pr_evidence_tool("read", cursor=attestation_token)
        )
        assert premature == {
            "success": False,
            "error": "Execution attestation prerequisites are incomplete",
            "fatal": False,
        }
        assert attestation_token in scope.cursors
        scope.execution_attestation_loader.assert_not_called()


def test_gate_resolution_makes_execution_attestation_mandatory():
    scope = _scope()
    private_key = Ed25519PrivateKey.generate()
    scope.execution_attestation_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    scope.execution_gate_policy_version = "newtonsapple-v1"
    scope.execution_gate_policy_sha256 = "f" * 64
    scope.baseline_execution_gates = ("quality", "integration", "e2e")
    contracts = {
        gate: {
            "kind": "command",
            "command": ["npm", "run", gate],
            "executor": "github_actions",
            "runner": {"kind": "github_actions", "name": "ubuntu-latest"},
            "status": "pass",
            "exit_codes": [0],
        }
        for gate in scope.baseline_execution_gates
    }
    resolution = {
        **scope.tuple_dict,
        "policy_version": scope.execution_gate_policy_version,
        "policy_sha256": scope.execution_gate_policy_sha256,
        "baseline_gates": list(scope.baseline_execution_gates),
        "resolved_gates": list(scope.baseline_execution_gates),
        "gate_contracts": contracts,
    }
    resolution_payload = json.dumps(
        resolution, sort_keys=True, separators=(",", ":")
    ).encode()
    resolution_signature = base64.b64encode(
        private_key.sign(resolution_payload)
    ).decode("ascii")
    scope.gate_resolution_loader = lambda: (
        resolution_payload,
        resolution_signature,
    )
    scope.execution_attestation_loader = MagicMock()

    with evidence_scope(scope):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        resolution_result = json.loads(
            github_pr_evidence_tool(
                "read", cursor=manifest["cursors"]["gate_resolution"]
            )
        )

        assert resolution_result["success"] is True
        attestation_token = manifest["cursors"]["execution_attestation"]
        assert scope.cursors[attestation_token].required is True
        assert json.loads(github_pr_evidence_tool("manifest"))["coverage"][
            "execution_attestation"
        ]["gate_resolution_complete"] is True
        recovery = json.loads(github_pr_evidence_tool("manifest"))[
            "current_required_cursors"
        ]
        assert recovery["items"][-1] == {
            "cursor": attestation_token,
            "kind": "execution_attestation",
        }


def test_tree_diff_reconciles_github_inventory_and_requires_changed_and_canonical_blobs():
    scope = _scope()
    base_tree = {
        "sha": "c" * 40,
        "truncated": False,
        "tree": [
            {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": "1" * 40},
            {"path": "docs/DEV.md", "mode": "100644", "type": "blob", "sha": "8" * 40},
            {"path": "docs/TESTING.md", "mode": "100644", "type": "blob", "sha": "9" * 40},
            {"path": "package.json", "mode": "100644", "type": "blob", "sha": "2" * 40},
            {"path": "playwright.config.ts", "mode": "100644", "type": "blob", "sha": "a" * 40},
            {"path": ".github/workflows/ci.yml", "mode": "100644", "type": "blob", "sha": "3" * 40},
            {"path": "src/app.py", "mode": "100644", "type": "blob", "sha": "4" * 40},
        ],
    }
    head_tree = {
        "sha": "d" * 40,
        "truncated": False,
        "tree": [
            {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": "1" * 40},
            {"path": "docs/DEV.md", "mode": "100644", "type": "blob", "sha": "8" * 40},
            {"path": "docs/TESTING.md", "mode": "100644", "type": "blob", "sha": "9" * 40},
            {"path": "package.json", "mode": "100644", "type": "blob", "sha": "2" * 40},
            {"path": "playwright.config.ts", "mode": "100644", "type": "blob", "sha": "a" * 40},
            {"path": ".github/workflows/ci.yml", "mode": "100644", "type": "blob", "sha": "3" * 40},
            {"path": "src/app.py", "mode": "100644", "type": "blob", "sha": "5" * 40},
            {"path": "tests/test_app.py", "mode": "100644", "type": "blob", "sha": "6" * 40},
        ],
    }
    api_files = [
        {"filename": "src/app.py", "status": "modified", "sha": "5" * 40},
        {"filename": "tests/test_app.py", "status": "added", "sha": "6" * 40},
    ]

    with evidence_scope(scope):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        with patch("tools.github_pr_evidence._run_gh_json", return_value=[api_files]):
            json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["changed_files"])
            )
        with patch(
            "tools.github_pr_evidence._run_gh_json",
            side_effect=[_comparison(), base_tree, head_tree],
        ) as run:
            result = json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["tree_diff"])
            )

    assert result["success"] is True
    assert scope.tree_diff_reconciled is True
    assert scope.base_tree_sha == "c" * 40
    assert scope.head_tree_sha == "d" * 40
    assert [call.args[0] for call in run.call_args_list] == [
        [
            f"repos/org/repo/compare/{BASE_SHA}...{HEAD_SHA}",
            "--jq",
            "{base_commit:{sha:.base_commit.sha},merge_base_commit:{sha:.merge_base_commit.sha}}",
        ],
        [f"repos/org/repo/git/trees/{BASE_SHA}?recursive=1"],
        [f"repos/org/repo/git/trees/{HEAD_SHA}?recursive=1"],
    ]
    required_blobs = [cursor for cursor in scope.cursors.values() if cursor.kind == "blob"]
    assert {cursor.data["sha"] for cursor in required_blobs} == {
        "1" * 40,
        "2" * 40,
        "3" * 40,
        "4" * 40,
        "5" * 40,
        "6" * 40,
        "8" * 40,
        "9" * 40,
        "a" * 40,
    }
    assert all(cursor.required for cursor in required_blobs)


def test_concise_tree_diff_validates_exact_trees_without_blob_fanout():
    scope = _scope()
    scope.concise_review = True
    required_entries = {
        "AGENTS.md": "1" * 40,
        "docs/DEV.md": "2" * 40,
        "docs/TESTING.md": "3" * 40,
        "package.json": "4" * 40,
        "playwright.config.ts": "5" * 40,
        ".github/workflows/ci.yml": "6" * 40,
    }
    base_tree = {
        "sha": "c" * 40,
        "truncated": False,
        "tree": [
            {"path": path, "mode": "100644", "type": "blob", "sha": sha}
            for path, sha in required_entries.items()
        ],
    }
    head_tree = {**base_tree, "sha": "d" * 40}
    base_commit = {"sha": BASE_SHA, "tree": {"sha": "c" * 40}}
    head_commit = {"sha": HEAD_SHA, "tree": {"sha": "d" * 40}}
    pull = {
        "number": 42,
        "base": {"sha": BASE_SHA},
        "head": {"sha": HEAD_SHA},
        "changed_files": 0,
    }

    with evidence_scope(scope):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        with patch("tools.github_pr_evidence._run_gh_json", return_value=pull):
            json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["pull_request"])
            )
        with patch("tools.github_pr_evidence._run_gh_json", return_value=[[]]):
            json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["changed_files"])
            )
        with patch(
            "tools.github_pr_evidence._run_gh_json",
            side_effect=[
                _comparison(),
                base_tree,
                head_tree,
                base_commit,
                head_commit,
            ],
        ) as run:
            result = json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["tree_diff"])
            )

    assert result["success"] is True
    assert result["items"]["blob_cursors"] == {"changed": [], "canonical": []}
    assert not any(cursor.kind == "blob" for cursor in scope.cursors.values())
    assert result["items"]["base_tree_sha"] == "c" * 40
    assert result["items"]["head_tree_sha"] == "d" * 40
    assert [call.args[0] for call in run.call_args_list[-2:]] == [
        [f"repos/org/repo/git/commits/{BASE_SHA}"],
        [f"repos/org/repo/git/commits/{HEAD_SHA}"],
    ]
    assert result["coverage"]["complete"] is True


def test_tree_evidence_rejects_gitlink_submodule_entries():
    scope = _scope()
    with evidence_scope(scope):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        with patch(
            "tools.github_pr_evidence._run_gh_json",
            side_effect=[
                _comparison(),
                {
                    "sha": "c" * 40,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "vendor/untrusted",
                            "mode": "160000",
                            "type": "commit",
                            "sha": "d" * 40,
                        }
                    ],
                },
            ],
        ):
            result = json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["tree_diff"])
            )

    assert result["success"] is False
    assert result["fatal"] is True
    assert "submodule gitlink" in result["error"]


def test_tree_diff_normalizes_rename_with_changed_content_from_github_inventory():
    scope = _scope()
    scope.observed_changed_files = 1
    scope.api_changed_inventory = {("renamed", "src/old.py", "src/new.py")}
    base_tree = {
        "sha": "c" * 40,
        "truncated": False,
        "tree": [
            {"path": path, "mode": "100644", "type": "blob", "sha": sha}
            for path, sha in {
                "AGENTS.md": "1" * 40,
                "docs/DEV.md": "2" * 40,
                "docs/TESTING.md": "3" * 40,
                "package.json": "4" * 40,
                "package-lock.json": "5" * 40,
                "playwright.config.ts": "6" * 40,
                ".github/workflows/ci.yml": "7" * 40,
                "src/old.py": "8" * 40,
            }.items()
        ],
    }
    head_tree = {
        **base_tree,
        "sha": "d" * 40,
        "tree": [
            entry for entry in base_tree["tree"] if entry["path"] != "src/old.py"
        ]
        + [{"path": "src/new.py", "mode": "100644", "type": "blob", "sha": "9" * 40}],
    }

    with evidence_scope(scope), patch(
        "tools.github_pr_evidence._run_gh_json",
        side_effect=[_comparison(), base_tree, head_tree],
    ):
        token = _new_cursor(scope, _Cursor("tree_diff"))
        result = json.loads(github_pr_evidence_tool("read", token))

    assert result["success"] is True
    assert result["items"]["changes"] == [
        {"status": "renamed", "base_path": "src/old.py", "head_path": "src/new.py"}
    ]
    assert scope.tree_diff_reconciled is True
    changed_blobs = {
        cursor.data["sha"]
        for cursor in scope.cursors.values()
        if cursor.kind == "blob" and "changed" in cursor.data["purposes"]
    }
    assert changed_blobs == {"8" * 40, "9" * 40}


def test_tree_diff_uses_merge_base_when_the_base_branch_advanced():
    scope = _scope()
    scope.observed_changed_files = 1
    scope.api_changed_inventory = {("modified", "src/app.py", "src/app.py")}
    merge_base_sha = "e" * 40
    merge_base_tree = {
        "sha": "c" * 40,
        "truncated": False,
        "tree": [
            {"path": path, "mode": "100644", "type": "blob", "sha": sha}
            for path, sha in {
                "AGENTS.md": "1" * 40,
                "docs/DEV.md": "2" * 40,
                "docs/TESTING.md": "3" * 40,
                "package.json": "4" * 40,
                "playwright.config.ts": "5" * 40,
                ".github/workflows/ci.yml": "6" * 40,
                "src/app.py": "7" * 40,
            }.items()
        ],
    }
    head_tree = {
        **merge_base_tree,
        "sha": "d" * 40,
        "tree": [
            entry if entry["path"] != "src/app.py" else {**entry, "sha": "8" * 40}
            for entry in merge_base_tree["tree"]
        ],
    }
    base_tip_tree = {
        **merge_base_tree,
        "sha": "f" * 40,
        "tree": merge_base_tree["tree"]
        + [
            {
                "path": ".github/workflows/security.yml",
                "mode": "100644",
                "type": "blob",
                "sha": "9" * 40,
            }
        ],
    }

    with evidence_scope(scope), patch(
        "tools.github_pr_evidence._run_gh_json",
        side_effect=[
            _comparison(merge_base_sha),
            base_tip_tree,
            merge_base_tree,
            head_tree,
        ],
    ):
        token = _new_cursor(scope, _Cursor("tree_diff"))
        result = json.loads(github_pr_evidence_tool("read", token))

    assert result["success"] is True
    assert result["items"]["base_tree_sha"] == "f" * 40
    assert result["items"]["merge_base_sha"] == merge_base_sha
    assert result["items"]["merge_base_tree_sha"] == "c" * 40
    assert ".github/workflows/security.yml" in result["items"]["canonical_paths"]
    assert result["items"]["changes"] == [
        {"status": "modified", "base_path": "src/app.py", "head_path": "src/app.py"}
    ]
    assert scope.tree_diff_reconciled is True


def test_gate_graph_includes_workspace_locks_containers_and_invoked_config_paths():
    scope = _scope()
    required_paths = {
        "AGENTS.md",
        "docs/DEV.md",
        "docs/TESTING.md",
        "package.json",
        "package-lock.json",
        "apps/web/package.json",
        "apps/web/package-lock.json",
        "playwright.config.ts",
        ".github/workflows/ci.yml",
        "Dockerfile",
        "docker/worker.Dockerfile",
        "compose.yml",
        "config/vitest.config.ts",
    }
    tree = {
        path: {"path": path, "mode": "100644", "type": "blob", "sha": f"{index:040x}"}
        for index, path in enumerate(sorted(required_paths), start=1)
    }
    scope.base_tree = dict(tree)
    scope.head_tree = dict(tree)

    with evidence_scope(scope):
        for source in ("base", "head"):
            for entry in tree.values():
                if entry["path"] != "config/vitest.config.ts":
                    _new_blob_cursor(scope, entry, source=source, purpose="canonical")
        package_token = next(
            token
            for token, cursor in scope.cursors.items()
            if cursor.kind == "blob"
            and any(path["path"] == "package.json" for path in cursor.data["paths"])
        )
        initial_paths = {
            path["path"]
            for cursor in scope.cursors.values()
            if cursor.kind == "blob" and "canonical" in cursor.data["purposes"]
            for path in cursor.data["paths"]
        }
        with patch(
            "tools.github_pr_evidence._run_gh_bytes",
            return_value=b'{"scripts":{"test":"vitest --config config/vitest.config.ts"}}',
        ):
            result = json.loads(github_pr_evidence_tool("read", package_token))

    assert result["success"] is True
    materialized_paths = {
        path["path"]
        for cursor in scope.cursors.values()
        if cursor.kind == "blob" and "canonical" in cursor.data["purposes"]
        for path in cursor.data["paths"]
    }
    assert required_paths - {"config/vitest.config.ts"} <= initial_paths
    assert "config/vitest.config.ts" in materialized_paths


def test_newtonsapple_5af3c38_exact_tree_has_the_complete_approved_gate_roots():
    fixture = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("newtonsapple_5af3c38_tree.json")
        .read_text()
    )
    assert fixture["commit"] == "5af3c3891e04ccda1aad028e1f677634669b3a45"
    selected = {path for path in fixture["paths"] if _is_canonical_gate_path(path)}
    assert selected == {
        ".github/workflows/ci.yml",
        ".github/workflows/staging-images.yml",
        ".github/workflows/staging-promotion.yml",
        ".dockerignore",
        ".prettierignore",
        "AGENTS.md",
        "CLAUDE.md",
        "apps/api/package.json",
        "apps/api/tsconfig.json",
        "apps/api/vitest.config.ts",
        "apps/web/eslint.config.mjs",
        "apps/web/next.config.test.ts",
        "apps/web/next.config.ts",
        "apps/web/package.json",
        "apps/web/postcss.config.mjs",
        "apps/web/tsconfig.e2e.json",
        "apps/web/tsconfig.json",
        "apps/web/vitest.config.mts",
        "compose.yaml",
        "compose.staging.yaml",
        "docs/DEV.md",
        "docs/TESTING.md",
        "infra/migrator/Dockerfile",
        "infra/staging/Caddyfile",
        "infra/staging/alloy/config.alloy",
        "infra/staging/api.Dockerfile",
        "infra/staging/web.Dockerfile",
        "package-lock.json",
        "package.json",
        "packages/auth/package.json",
        "packages/auth/tsconfig.json",
        "packages/auth/vitest.integration.config.ts",
        "packages/db/package.json",
        "packages/db/tsconfig.json",
        "packages/db/vitest.integration.config.ts",
        "packages/shared/package.json",
        "packages/shared/tsconfig.json",
        "playwright.config.ts",
        "turbo.json",
    }


def test_changed_files_are_paginated_completely_but_tree_diff_owns_blob_authority():
    files = [
        {
            "filename": "src/new.py",
            "previous_filename": "src/old.py",
            "status": "renamed",
            "sha": "c" * 40,
            "patch": "@@ -1 +1 @@",
        },
        {
            "filename": "assets/image.png",
            "status": "modified",
            "sha": "d" * 40,
            "patch": None,
        },
    ]

    with evidence_scope(_scope()):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        cursor = manifest["cursors"]["changed_files"]
        with patch(
            "tools.github_pr_evidence.subprocess.run",
            return_value=_result([files]),
        ) as run:
            result = json.loads(github_pr_evidence_tool("read", cursor))

        command = run.call_args.args[0]
        assert command == [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "repos/org/repo/pulls/42/files?per_page=100",
        ]
        assert result["kind"] == "changed_files"
        assert len(result["items"]) == 2
        assert all("blob_cursors" not in item for item in result["items"])
        assert result["coverage"]["required_outstanding"] == 10
        assert result["coverage"]["optional_available"] == 0


def test_concise_changed_files_bounds_large_patches_without_cursor_fanout():
    scope = _scope()
    scope.concise_review = True
    patch_text = "x" * 20_000
    files = [
        {
            "filename": "src/new.py",
            "status": "added",
            "sha": "c" * 40,
            "patch": patch_text,
        }
    ]

    with evidence_scope(scope):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        with patch(
            "tools.github_pr_evidence.subprocess.run", return_value=_result([files])
        ):
            result = json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["changed_files"])
            )

    item = result["items"][0]
    assert item["patch_truncated"] is True
    assert item["patch_length"] == len(patch_text)
    assert item["patch_sha256"] == hashlib.sha256(patch_text.encode()).hexdigest()
    assert len(item["patch"]) < 5_000
    assert result["coverage"]["required_outstanding"] == 2


def test_reading_gate_definitions_discovers_referenced_setup_scripts_as_required():
    scope = _scope()
    package = {"path": "package.json", "mode": "100644", "type": "blob", "sha": "2" * 40}
    setup = {
        "path": "scripts/setup-db.sh",
        "mode": "100755",
        "type": "blob",
        "sha": "7" * 40,
    }
    scope.base_tree = {"package.json": package, "scripts/setup-db.sh": setup}
    scope.head_tree = {"package.json": package, "scripts/setup-db.sh": setup}

    with evidence_scope(scope):
        token = _new_blob_cursor(scope, package, source="head", purpose="canonical")
        with patch(
            "tools.github_pr_evidence._run_gh_bytes",
            return_value=b'{"scripts":{"db:verify":"./scripts/setup-db.sh"}}',
        ):
            result = json.loads(github_pr_evidence_tool("read", token))

    assert result["success"] is True
    script_cursors = [
        cursor
        for cursor in scope.cursors.values()
        if cursor.kind == "blob" and cursor.data["sha"] == "7" * 40
    ]
    assert len(script_cursors) == 1
    assert script_cursors[0].required is True
    assert "canonical" in script_cursors[0].data["purposes"]


def test_cursor_from_another_scope_is_rejected_without_github_access():
    with evidence_scope(_scope(41)):
        cursor = json.loads(github_pr_evidence_tool("manifest"))["cursors"]["pull_request"]

    with evidence_scope(_scope(42)):
        with patch("tools.github_pr_evidence.subprocess.run") as run:
            result = json.loads(github_pr_evidence_tool("read", cursor))

    assert result["success"] is False
    assert result["error"] == "Unknown or already-consumed evidence cursor"
    run.assert_not_called()


def test_exact_head_ci_run_is_enforced_before_child_cursors_are_created():
    wrong_head_run = {"id": 7, "head_sha": "c" * 40, "status": "completed"}

    with evidence_scope(_scope()):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        cursor = manifest["cursors"]["workflow_runs"]
        with patch(
            "tools.github_pr_evidence.subprocess.run",
            return_value=_result([{"workflow_runs": [wrong_head_run]}]),
        ):
            result = json.loads(github_pr_evidence_tool("read", cursor))

        assert result["success"] is False
        assert result["fatal"] is True
        assert evidence_complete_for("v2", "org/repo", 42, BASE_SHA, HEAD_SHA) is False


def test_workflow_attempts_generate_fixed_jobs_logs_and_artifact_cursors():
    run = {
        "id": 7,
        "head_sha": HEAD_SHA,
        "status": "completed",
        "run_attempt": 2,
    }
    with evidence_scope(_scope()):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        cursor = manifest["cursors"]["workflow_runs"]
        with patch(
            "tools.github_pr_evidence.subprocess.run",
            return_value=_result([{"workflow_runs": [run]}]),
        ):
            result = json.loads(github_pr_evidence_tool("read", cursor))

        child = result["items"][0]["evidence_cursors"]
        assert [attempt["attempt"] for attempt in child["attempts"]] == [1, 2]
        assert child["artifacts"]


def test_changed_file_count_mismatch_is_fatal():
    with evidence_scope(_scope()):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        with patch(
            "tools.github_pr_evidence.subprocess.run",
            return_value=_result(
                {
                    "state": "open",
                    "draft": False,
                    "number": 42,
                    "base": {"sha": BASE_SHA},
                    "head": {"sha": HEAD_SHA},
                    "changed_files": 2,
                }
            ),
        ):
            json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["pull_request"])
            )
        with patch(
            "tools.github_pr_evidence.subprocess.run",
            return_value=_result([[{"filename": "only.py", "status": "modified"}]]),
        ):
            result = json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["changed_files"])
            )

        assert result["success"] is False
        assert result["fatal"] is True


def test_pr_184_shape_completes_without_reading_157_artifact_entries():
    """Authoritative inventories/logs are required; coverage files are drill-down."""
    scope = _scope()
    pull = {
        "state": "open",
        "draft": False,
        "number": 42,
        "base": {"sha": BASE_SHA},
        "head": {"sha": HEAD_SHA},
        "changed_files": 2,
    }
    files = [
        {"filename": "src/app.py", "status": "modified"},
        {"filename": "tests/test_app.py", "status": "modified"},
    ]
    workflow_run = {
        "id": 30958841811,
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
    }
    closing_issues = {
        "data": {
            "repository": {
                "pullRequest": {
                    "closingIssuesReferences": {
                        "nodes": [
                            {"number": 183, "repository": {"nameWithOwner": "org/repo"}}
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
    }
    base_tree = {
        "sha": "c" * 40,
        "truncated": False,
        "tree": [
            {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": "1" * 40},
            {"path": "docs/DEV.md", "mode": "100644", "type": "blob", "sha": "8" * 40},
            {"path": "docs/TESTING.md", "mode": "100644", "type": "blob", "sha": "9" * 40},
            {"path": "package.json", "mode": "100644", "type": "blob", "sha": "2" * 40},
            {"path": "playwright.config.ts", "mode": "100644", "type": "blob", "sha": "a" * 40},
            {"path": ".github/workflows/ci.yml", "mode": "100644", "type": "blob", "sha": "3" * 40},
            {"path": "src/app.py", "mode": "100644", "type": "blob", "sha": "4" * 40},
            {"path": "tests/test_app.py", "mode": "100644", "type": "blob", "sha": "5" * 40},
        ],
    }
    head_tree = {
        "sha": "d" * 40,
        "truncated": False,
        "tree": [
            {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": "1" * 40},
            {"path": "docs/DEV.md", "mode": "100644", "type": "blob", "sha": "8" * 40},
            {"path": "docs/TESTING.md", "mode": "100644", "type": "blob", "sha": "9" * 40},
            {"path": "package.json", "mode": "100644", "type": "blob", "sha": "2" * 40},
            {"path": "playwright.config.ts", "mode": "100644", "type": "blob", "sha": "a" * 40},
            {"path": ".github/workflows/ci.yml", "mode": "100644", "type": "blob", "sha": "3" * 40},
            {"path": "src/app.py", "mode": "100644", "type": "blob", "sha": "6" * 40},
            {"path": "tests/test_app.py", "mode": "100644", "type": "blob", "sha": "7" * 40},
        ],
    }
    coverage_bytes = io.BytesIO()
    with zipfile.ZipFile(coverage_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(157):
            archive.writestr(f"coverage/{index}.json", '{"covered": true}')

    def json_response(args):
        if args[0] == f"repos/org/repo/compare/{BASE_SHA}...{HEAD_SHA}":
            return _comparison()
        endpoint = args[-1]
        if endpoint == "repos/org/repo/pulls/42":
            return pull
        if endpoint.endswith("/files?per_page=100"):
            return [files]
        if endpoint.endswith(f"git/trees/{BASE_SHA}?recursive=1"):
            return base_tree
        if endpoint.endswith(f"git/trees/{HEAD_SHA}?recursive=1"):
            return head_tree
        if endpoint.endswith("actions/runs?head_sha=" + HEAD_SHA + "&per_page=100"):
            return [{"workflow_runs": [workflow_run]}]
        if endpoint.endswith("/jobs?per_page=100"):
            return [{"jobs": [{"id": 77, "status": "completed", "conclusion": "success"}]}]
        if endpoint.endswith("/artifacts?per_page=100"):
            return [{"artifacts": [{"id": 184, "name": "coverage", "size_in_bytes": 739076}]}]
        if endpoint == "repos/org/repo/issues/183":
            return {"number": 183, "title": "Requirement", "body": "Acceptance criteria"}
        if args and args[0] == "graphql":
            return closing_issues
        return [[]]

    def bytes_response(endpoint, **_kwargs):
        if endpoint.endswith("/zip"):
            return coverage_bytes.getvalue()
        if "/logs" in endpoint:
            return b"all jobs passed\n"
        return b"immutable file contents\n"

    with evidence_scope(scope):
        json.loads(github_pr_evidence_tool("manifest"))
        with (
            patch("tools.github_pr_evidence._run_gh_json", side_effect=json_response),
            patch("tools.github_pr_evidence._run_gh_bytes", side_effect=bytes_response),
        ):
            calls = 0
            while scope.required_cursors:
                cursor = next(iter(scope.required_cursors))
                result = json.loads(github_pr_evidence_tool("read", cursor))
                assert result["success"] is True
                calls += 1
                assert calls <= 60

        assert review_evidence_complete_for(
            "v2", "org/repo", 42, BASE_SHA, HEAD_SHA
        ) is True
        assert evidence_complete_for("v2", "org/repo", 42, BASE_SHA, HEAD_SHA) is False
        payload, signature = _signed_execution_attestation(scope)
        assert record_execution_attestation(payload, signature) is True
        assert evidence_complete_for("v2", "org/repo", 42, BASE_SHA, HEAD_SHA) is True
        assert len(scope.cursors) == 157
        assert all(not cursor.required for cursor in scope.cursors.values())
        assert {cursor.kind for cursor in scope.cursors.values()} == {"archive_entry"}


def test_closing_issue_relationship_is_tuple_bound_and_not_parsed_from_body_text():
    scope = _scope()
    with evidence_scope(scope):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        graphql = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "closingIssuesReferences": {
                            "nodes": [
                                {"number": 183, "repository": {"nameWithOwner": "org/repo"}},
                                {"number": 9, "repository": {"nameWithOwner": "other/repo"}},
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        with patch("tools.github_pr_evidence._run_gh_json", return_value=graphql) as run:
            result = json.loads(
                github_pr_evidence_tool("read", manifest["cursors"]["closing_issues"])
            )

    assert run.call_args.args[0][0] == "graphql"
    assert len(result["items"]["issues"]) == 1
    assert result["items"]["issues"][0]["number"] == 183
    assert result["items"]["issues"][0]["repository"] == "org/repo"
    assert result["items"]["issues"][0]["cursor"]
    assert result["items"]["complete"] is True


def test_large_coverage_archive_inventory_is_optional_and_bounded():
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(157):
            archive.writestr(f"coverage/{index}.json", '{"covered": true}')

    scope = _scope()
    with evidence_scope(scope):
        token = _new_cursor(
            scope,
            _Cursor(
                kind="archive",
                endpoint="repos/org/repo/actions/artifacts/184/zip",
                data={"archive_kind": "artifact"},
                required=False,
            ),
        )
        with patch(
            "tools.github_pr_evidence._run_gh_bytes", return_value=archive_bytes.getvalue()
        ):
            result = json.loads(github_pr_evidence_tool("read", token))

    assert result["success"] is True
    assert len(result["items"]["entries"]) == 157
    assert result["coverage"]["required_outstanding"] == 0
    assert result["coverage"]["optional_available"] == 157


def test_required_evidence_can_expand_past_bounded_optional_archive_inventory():
    scope = _scope()
    with evidence_scope(scope):
        for index in range(200):
            _new_cursor(
                scope,
                _Cursor(
                    kind="archive_entry",
                    data={"path": f"coverage/{index}.json", "content": b"{}"},
                    required=False,
                ),
            )
        required = {
            _new_cursor(scope, _Cursor(kind="data", data={"index": index}))
            for index in range(100)
        }

    assert len(scope.cursors) == 300
    assert scope.required_cursors == required


def test_cursor_registry_still_fails_closed_at_configured_scope_limit():
    scope = _scope()
    with evidence_scope(scope), patch(
        "tools.github_pr_evidence._MAX_ACTIVE_CURSORS", 2
    ):
        _new_cursor(scope, _Cursor(kind="data"))
        _new_cursor(scope, _Cursor(kind="data"))
        with pytest.raises(RuntimeError, match="cursor limit exceeded"):
            _new_cursor(scope, _Cursor(kind="data"))


def test_large_archive_entry_creates_only_one_lazy_continuation_cursor():
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("coverage/large.bin", os.urandom(100_000))

    scope = _scope()
    with evidence_scope(scope):
        token = _new_cursor(
            scope,
            _Cursor(
                kind="archive",
                endpoint="repos/org/repo/actions/artifacts/184/zip",
                data={"archive_kind": "artifact"},
                required=False,
            ),
        )
        with patch(
            "tools.github_pr_evidence._run_gh_bytes", return_value=archive_bytes.getvalue()
        ):
            inventory = json.loads(github_pr_evidence_tool("read", token))
        entry_cursors = inventory["items"]["entries"][0]["cursors"]
        assert len(entry_cursors) == 1
        assert inventory["coverage"]["optional_available"] == 1

        first_chunk = json.loads(github_pr_evidence_tool("read", entry_cursors[0]))
        assert first_chunk["items"]["part"] == 1
        assert first_chunk["items"]["parts"] == 9
        assert first_chunk["items"]["next_cursor"]
        assert first_chunk["coverage"]["optional_available"] == 1


def test_binary_optional_entry_is_reported_as_base64_without_blocking_completion():
    scope = _scope()
    with evidence_scope(scope):
        token = _new_cursor(
            scope,
            _Cursor(
                kind="archive_entry",
                data={"path": "coverage/data.bin", "content": b"\xff\x00", "part": 1, "parts": 1},
                required=False,
            ),
        )
        result = json.loads(github_pr_evidence_tool("read", token))

    assert result["items"]["encoding"] == "base64"
    assert result["items"]["content"] == "/wA="


def test_oversized_archive_is_fatal_even_when_the_cursor_is_optional():
    scope = _scope()
    with evidence_scope(scope):
        token = _new_cursor(
            scope,
            _Cursor(
                kind="archive",
                endpoint="repos/org/repo/actions/artifacts/184/zip",
                data={"archive_kind": "artifact"},
                required=False,
            ),
        )
        with patch(
            "tools.github_pr_evidence._run_gh_bytes",
            side_effect=RuntimeError("GitHub evidence archive exceeded the size limit"),
        ):
            result = json.loads(github_pr_evidence_tool("read", token))

    assert result["success"] is False
    assert result["fatal"] is True


def test_concurrent_same_tuple_scopes_keep_required_and_optional_cursors_isolated():
    barrier = __import__("threading").Barrier(2)

    def inspect(_):
        scope = _scope()
        with evidence_scope(scope):
            manifest = json.loads(github_pr_evidence_tool("manifest"))
            barrier.wait()
            return set(scope.required_cursors), set(scope.cursors), set(manifest["cursors"].values())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(inspect, (1, 2)))

    assert first[0].isdisjoint(second[0])
    assert first[1].isdisjoint(second[1])
    assert first[2].isdisjoint(second[2])


def test_context_is_preserved_by_asyncio_to_thread_and_cleared_after_exit():
    async def run():
        with evidence_scope(_scope()):
            inside = await asyncio.to_thread(_tool_names)
        outside = await asyncio.to_thread(_tool_names)
        return inside, outside

    inside, outside = asyncio.run(run())
    assert "github_pr_evidence" in inside
    assert "github_pr_evidence" not in outside


def test_execution_control_plane_cursors_enforce_resolution_before_attestation():
    scope = _scope()
    private_key = Ed25519PrivateKey.generate()
    scope.execution_attestation_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    scope.execution_gate_policy_version = "newtonsapple-v1"
    scope.execution_gate_policy_sha256 = "f" * 64
    scope.baseline_execution_gates = ("quality", "integration", "e2e")
    contracts = {
        gate: {
            "kind": "command",
            "command": ["npm", "run", gate],
            "executor": "github_actions",
            "runner": {"kind": "github_actions", "name": "ubuntu-latest"},
            "status": "pass",
            "exit_codes": [0],
        }
        for gate in scope.baseline_execution_gates
    }
    resolution = {
        **scope.tuple_dict,
        "policy_version": scope.execution_gate_policy_version,
        "policy_sha256": scope.execution_gate_policy_sha256,
        "baseline_gates": list(scope.baseline_execution_gates),
        "resolved_gates": list(contracts),
        "gate_contracts": contracts,
    }
    resolution_payload = json.dumps(
        resolution, sort_keys=True, separators=(",", ":")
    ).encode()
    resolution_signature = base64.b64encode(
        private_key.sign(resolution_payload)
    ).decode("ascii")
    attestation_calls = []
    scope.gate_resolution_loader = lambda: (
        resolution_payload,
        resolution_signature,
    )
    scope.execution_attestation_loader = lambda: (
        attestation_calls.append(True) or b"unused",
        "unused",
    )

    with evidence_scope(scope):
        manifest = json.loads(github_pr_evidence_tool("manifest"))
        gate_cursor = manifest["cursors"]["gate_resolution"]
        attestation_cursor = manifest["cursors"]["execution_attestation"]

        assert gate_cursor in scope.required_cursors
        assert attestation_cursor not in scope.required_cursors

        premature = json.loads(
            github_pr_evidence_tool("read", attestation_cursor)
        )
        assert premature == {
            "success": False,
            "error": "Execution attestation prerequisites are incomplete",
            "fatal": False,
        }
        assert attestation_cursor in scope.cursors
        assert attestation_calls == []
        assert scope.fatal_error == ""

        resolved = json.loads(github_pr_evidence_tool("read", gate_cursor))
        assert resolved["success"] is True
        assert attestation_cursor in scope.required_cursors

        still_premature = json.loads(
            github_pr_evidence_tool("read", attestation_cursor)
        )
        assert still_premature["success"] is False
        assert still_premature["fatal"] is False
        assert attestation_cursor in scope.required_cursors
        assert attestation_calls == []
        assert scope.fatal_error == ""


def test_signed_execution_attestation_requires_worker_identity_preflight_and_all_gates():
    scope = _scope()
    with evidence_scope(scope):
        payload, signature = _signed_execution_attestation(scope)

        assert record_execution_attestation(payload, signature) is True
        assert execution_evidence_complete_for(
            "v2", "org/repo", 42, BASE_SHA, HEAD_SHA
        ) is True

        missing_gate = json.loads(payload)
        missing_gate["gates"] = missing_gate["gates"][:-1]
        rejected_payload, rejected_signature = _signed_execution_attestation(
            scope, gates=missing_gate["gates"]
        )
        assert record_execution_attestation(rejected_payload, rejected_signature) is False

        unsafe_preflight = dict(missing_gate["worker"]["preflight"])
        unsafe_preflight["credentials_absent"] = False
        rejected_payload, rejected_signature = _signed_execution_attestation(
            scope, preflight=unsafe_preflight
        )
        assert record_execution_attestation(rejected_payload, rejected_signature) is False


def test_github_actions_only_attestation_does_not_claim_a_disposable_worker():
    scope = _scope()
    with evidence_scope(scope):
        payload, signature = _signed_execution_attestation(
            scope, worker_required=False
        )

        assert record_execution_attestation(payload, signature) is True


def test_signed_local_worker_attestation_accepts_unavailable_gate_results():
    scope = _scope()
    private_key = Ed25519PrivateKey.generate()
    scope.base_tree_sha = "c" * 40
    scope.head_tree_sha = "d" * 40
    contracts = {
        gate: {
            "kind": "command",
            "command": ["npm", "run", gate],
            "executor": "review_worker",
            "runner": {"kind": "review_worker", "name": "docker-node22"},
            "statuses": ["pass", "pr-fail", "unavailable"],
            "exit_codes": list(range(0, 256)),
        }
        for gate in ("quality", "integration", "e2e")
    }
    manifest_sha256 = _install_gate_resolution(
        scope, private_key, contracts=contracts
    )
    report = {
        **scope.tuple_dict,
        "base_tree_sha": scope.base_tree_sha,
        "head_tree_sha": scope.head_tree_sha,
        "gate_resolution": {
            "policy_version": scope.execution_gate_policy_version,
            "policy_sha256": scope.execution_gate_policy_sha256,
            "manifest_sha256": manifest_sha256,
            "resolved_gates": list(scope.required_execution_gates),
        },
        "worker": {
            "required": True,
            "head_sha": scope.head_sha,
            "base_present": True,
            "tree_before": scope.head_tree_sha,
            "tree_after": scope.head_tree_sha,
            "mutations": [],
            "preflight": {
                "disposable_home": True,
                "credentials_absent": True,
                "host_mounts_absent": True,
                "host_docker_socket_absent": True,
                "resources_bounded": True,
                "egress_default_deny": True,
            },
        },
        "gates": [
            {
                **_gate_record(
                    scope, gate, status="unavailable", executor="review_worker"
                ),
                "runner": {"kind": "review_worker", "name": "docker-node22"},
                "attempted": False,
                "exit_code": 125,
                "evidence": {
                    "kind": "local_worker",
                    "log_sha256": "e" * 64,
                    "reason": "Docker daemon unavailable",
                },
            }
            for gate in scope.required_execution_gates
        ],
    }
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    signature = base64.b64encode(private_key.sign(payload)).decode("ascii")

    with evidence_scope(scope):
        assert record_execution_attestation(payload, signature) is True
        assert execution_evidence_complete_for(
            scope.contract_version,
            scope.repository,
            scope.pr_number,
            scope.base_sha,
            scope.head_sha,
        ) is True


def test_review_worker_gate_cannot_omit_disposable_worker_preflight():
    scope = _scope()
    contracts = {
        **{
            gate: {
                "kind": "command",
                "command": ["npm", "run", gate],
                "executor": "github_actions",
                "runner": {"kind": "github_actions", "name": "ubuntu-latest"},
                "status": "pass",
                "exit_codes": [0],
            }
            for gate in ("quality", "integration", "e2e")
        },
        "feature": {
            "kind": "command",
            "command": ["npm", "run", "feature"],
            "executor": "review_worker",
            "runner": {"kind": "review_worker", "name": "isolated"},
            "status": "pass",
            "exit_codes": [0],
        },
    }
    private_key = Ed25519PrivateKey.generate()
    scope.base_tree_sha = "c" * 40
    scope.head_tree_sha = "d" * 40
    _install_gate_resolution(scope, private_key, contracts=contracts)
    gates = [
        _gate_record(
            scope,
            gate,
            executor="review_worker" if gate == "feature" else "github_actions",
        )
        for gate in scope.required_execution_gates
    ]
    payload, signature = _signed_execution_attestation(
        scope, gates=gates, worker_required=False
    )

    with evidence_scope(scope):
        assert record_execution_attestation(payload, signature) is False


def test_gate_resolution_is_tuple_policy_and_baseline_bound():
    scope = _scope()
    private_key = Ed25519PrivateKey.generate()
    with evidence_scope(scope):
        manifest_sha256 = _install_gate_resolution(
            scope,
            private_key,
            contracts={
                gate: {
                    "kind": "command",
                    "command": ["npm", "run", gate],
                    "executor": "github_actions",
                    "runner": {"kind": "github_actions", "name": "ubuntu-latest"},
                    "status": "pass",
                    "exit_codes": [0],
                }
                for gate in ("quality", "integration", "e2e", "database-migrations")
            },
        )

    assert scope.required_execution_gates == (
        "quality",
        "integration",
        "e2e",
        "database-migrations",
    )
    assert scope.gate_resolution_manifest_sha256 == manifest_sha256
    assert scope.gate_resolution_valid is True

    invalid = {
        **scope.tuple_dict,
        "policy_version": scope.execution_gate_policy_version,
        "policy_sha256": scope.execution_gate_policy_sha256,
        "baseline_gates": list(scope.baseline_execution_gates),
        "resolved_gates": ["database-migrations"],
        "gate_contracts": {"database-migrations": {"kind": "command"}},
    }
    payload = json.dumps(invalid, sort_keys=True, separators=(",", ":")).encode()
    signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
    with evidence_scope(scope):
        assert record_gate_resolution(payload, signature) is False


def test_pr_184_execution_contract_preserves_feature_specific_failures_and_capability_limits():
    scope = _scope(pr_number=184)
    contracts = {
        **{
            gate: {
                "kind": "command",
                "command": ["npm", "run", gate],
                "executor": "github_actions",
                "runner": {"kind": "github_actions", "name": "ubuntu-latest"},
                "status": "pass",
                "exit_codes": [0],
            }
            for gate in ("quality", "integration", "e2e")
        },
        "voice-eval-dry-run": {
            "kind": "voice_eval_dry_run",
            "cases": 20,
            "max_estimated_cost_usd": 1.0,
            "thresholds": {"mathematical_notation_recall": 0.95},
        },
        "voice-eval-paid": {
            "kind": "voice_eval_paid",
            "max_budget_usd": 1.0,
            "allowed_endpoints": ["/v1/audio/speech", "/v1/audio/transcriptions"],
        },
        "voice-browser": {
            "kind": "browser_scenarios",
            "required_scenarios": [
                "click-to-start-stop",
                "get-user-media-delayed",
                "get-user-media-denied",
                "minimum-400ms",
                "silence-auto-stop",
                "transcript-edit-before-send",
                "safe-502-logging",
            ],
        },
        "issue-183-requirements": {
            "kind": "requirement_contradiction",
            "issue_number": 183,
            "criterion": "mathematical-notation",
            "minimum": 0.95,
        },
    }
    scope.base_tree_sha = "c" * 40
    scope.head_tree_sha = "d" * 40
    private_key = Ed25519PrivateKey.generate()
    manifest_sha256 = _install_gate_resolution(scope, private_key, contracts=contracts)
    artifact = "artifacts/voice-eval-fresh.json"
    report = {
        **scope.tuple_dict,
        "base_tree_sha": scope.base_tree_sha,
        "head_tree_sha": scope.head_tree_sha,
        "gate_resolution": {
            "policy_version": scope.execution_gate_policy_version,
            "policy_sha256": scope.execution_gate_policy_sha256,
            "manifest_sha256": manifest_sha256,
            "resolved_gates": list(scope.required_execution_gates),
        },
        "worker": {
            "required": True,
            "head_sha": scope.head_sha,
            "base_present": True,
            "tree_before": scope.head_tree_sha,
            "tree_after": scope.head_tree_sha,
            "mutations": [artifact],
            "preflight": {
                "disposable_home": True,
                "credentials_absent": True,
                "host_mounts_absent": True,
                "host_docker_socket_absent": True,
                "resources_bounded": True,
                "egress_default_deny": True,
            },
        },
        "gates": [
            _gate_record(scope, "quality"),
            _gate_record(scope, "integration"),
            _gate_record(scope, "e2e"),
            {
                **_gate_record(scope, "voice-eval-dry-run", executor="review_worker"),
                "id": "voice-eval-dry-run",
                "executor": "review_worker",
                "status": "pass",
                "head_sha": scope.head_sha,
                "attempted": True,

                "command": ["npm", "run", "ai:eval:voice", "--", "--dry-run"],
                "exit_code": 0,
                "plan": {
                    "cases": 20,
                    "estimated_cost_usd": 0.42,
                    "thresholds": {"mathematical_notation_recall": 0.95},
                },
            },
            {
                **_gate_record(
                    scope, "voice-eval-paid", status="pr-fail", executor="review_worker"
                ),
                "id": "voice-eval-paid",
                "executor": "review_worker",
                "status": "pr-fail",
                "head_sha": scope.head_sha,
                "attempted": True,

                "command": [
                    "npm", "run", "ai:eval:voice", "--", "--output", artifact,
                    "--confirm-cost",
                ],
                "exit_code": 0,
                "declared_artifact": {
                    "path": artifact,
                    "sha256": "e" * 64,
                },
                "capability": {
                    "provider": "openai",
                    "single_use": True,
                    "endpoint_scoped": True,
                    "allowed_endpoints": [
                        "/v1/audio/speech",
                        "/v1/audio/transcriptions",
                    ],
                    "budget_cap_usd": 1.0,
                    "production_credential": False,
                    "long_lived_credential": False,
                },
                "result": {
                    "provider": "openai",
                    "model": "gpt-4o-mini-transcribe",
                    "voice": "alloy",
                    "overall_pass": False,
                    "thresholds": {
                        "mathematical_notation_recall": {
                            "observed": 0.875,
                            "minimum": 0.95,
                            "passed": False,
                        }
                    },
                },
            },
            {
                **_gate_record(scope, "voice-browser", executor="review_worker"),
                "id": "voice-browser",
                "executor": "review_worker",
                "status": "pass",
                "head_sha": scope.head_sha,
                "attempted": True,

                "exit_code": 0,
                "scenarios": scope.execution_gate_contracts["voice-browser"][
                    "required_scenarios"
                ],
            },
            {
                **_gate_record(
                    scope, "issue-183-requirements", status="pr-fail", executor="review_worker"
                ),
                "id": "issue-183-requirements",
                "executor": "review_worker",
                "status": "pr-fail",
                "head_sha": scope.head_sha,
                "attempted": True,

                "issue_number": 183,
                "criterion": "mathematical-notation",
                "checked": True,
                "prose_open": True,
                "observed": 0.875,
                "minimum": 0.95,
                "contradiction": True,
            },
        ],
    }

    def sign(candidate):
        payload = json.dumps(
            candidate, sort_keys=True, separators=(",", ":")
        ).encode()
        return payload, base64.b64encode(private_key.sign(payload)).decode("ascii")

    with evidence_scope(scope):
        payload, signature = sign(report)
        assert record_execution_attestation(payload, signature) is True

        for field in (
            "runner",
            "command",
            "exit_code",
            "started_at",
            "completed_at",
            "duration_ms",
            "tree_before",
            "tree_after",
            "evidence",
        ):
            invalid_report = copy.deepcopy(report)
            invalid_report["gates"][0].pop(field)
            invalid_payload, invalid_signature = sign(invalid_report)
            assert record_execution_attestation(invalid_payload, invalid_signature) is False

        missing_provider = copy.deepcopy(report)
        missing_provider["gates"][4]["result"].pop("provider")
        invalid_payload, invalid_signature = sign(missing_provider)
        assert record_execution_attestation(invalid_payload, invalid_signature) is False

        missing_dry_run_command = copy.deepcopy(report)
        missing_dry_run_command["gates"][3].pop("command")
        invalid_payload, invalid_signature = sign(missing_dry_run_command)
        assert record_execution_attestation(invalid_payload, invalid_signature) is False

        paid_gate = report["gates"][4]
        original_status = paid_gate["status"]
        paid_gate["status"] = "pass"
        payload, signature = sign(report)
        assert record_execution_attestation(payload, signature) is False
        paid_gate["status"] = original_status

        report["worker"]["mutations"] = [artifact, "src/changed.ts"]
        payload, signature = sign(report)
        assert record_execution_attestation(payload, signature) is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda gate: gate.update(command=["npm", "run", "substituted"]),
        lambda gate: gate.update(executor="review_worker"),
        lambda gate: gate.update(status="pr-fail"),
        lambda gate: gate.update(exit_code=1),
        lambda gate: gate.update(started_at="not-a-timestamp"),
        lambda gate: gate.update(completed_at="2026-08-05T17:59:00Z"),
        lambda gate: gate.update(duration_ms=59_999),
        lambda gate: gate["evidence"].update(job_id=999),
        lambda gate: gate["evidence"].update(
            url="https://github.com/org/repo/actions/runs/2/job/123"
        ),
        lambda gate: gate["evidence"].update(log_sha256="d" * 64),
    ],
)
def test_command_gate_rejects_contract_time_and_exact_head_actions_substitutions(mutation):
    scope = _scope()
    with evidence_scope(scope):
        payload, _ = _signed_execution_attestation(scope)
        report = json.loads(payload)
        mutation(report["gates"][0])
        private_key = Ed25519PrivateKey.generate()
        scope.execution_attestation_public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        mutated = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        mutated_signature = base64.b64encode(private_key.sign(mutated)).decode("ascii")
        assert record_execution_attestation(mutated, mutated_signature) is False


def test_command_gate_rejects_actions_job_with_contradictory_conclusion():
    scope = _scope()
    with evidence_scope(scope):
        payload, signature = _signed_execution_attestation(scope)
        scope.observed_action_jobs[123]["conclusion"] = "failure"
        assert record_execution_attestation(payload, signature) is False


def test_review_and_execution_attestations_are_independent_publication_gates():
    scope = _scope()
    scope.manifest_created = True
    scope.pull_validated = True
    scope.expected_changed_files = 0
    scope.observed_changed_files = 0
    scope.workflow_runs_observed = 1
    scope.tree_diff_reconciled = True
    scope.canonical_files_materialized = True
    scope.required_logs_materialized = True
    scope.required_artifact_inventories_materialized = True

    with evidence_scope(scope):
        assert review_evidence_complete_for(
            "v2", "org/repo", 42, BASE_SHA, HEAD_SHA
        ) is True
        assert execution_evidence_complete_for(
            "v2", "org/repo", 42, BASE_SHA, HEAD_SHA
        ) is False
        assert evidence_complete_for("v2", "org/repo", 42, BASE_SHA, HEAD_SHA) is False

        payload, signature = _signed_execution_attestation(scope)
        assert record_execution_attestation(payload, signature) is True
        assert evidence_complete_for("v2", "org/repo", 42, BASE_SHA, HEAD_SHA) is True

        scope.tree_diff_reconciled = False
        assert execution_evidence_complete_for(
            "v2", "org/repo", 42, BASE_SHA, HEAD_SHA
        ) is True
        assert review_evidence_complete_for(
            "v2", "org/repo", 42, BASE_SHA, HEAD_SHA
        ) is False
        assert evidence_complete_for("v2", "org/repo", 42, BASE_SHA, HEAD_SHA) is False
