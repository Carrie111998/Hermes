"""Kwilo-only governance admission and semantic dependency tests."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kwilo_governance as kwilo


@pytest.fixture
def kwilo_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    governance = tmp_path / "governance" / "kwilo"
    governance.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KWILO_GOVERNANCE_DIR", str(governance))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    manifest = {
        "profiles": {
            "kwilo-forge": {
                "role": "software-engineer",
                "lane": "app-general",
                "repositories": ["kwilo-app"],
                "workspace_classes": ["isolated-worktree", "declared-dirty-continuation"],
                "configured_cli_toolsets": ["file", "kanban", "terminal"],
                "configured_parent_skills": ["test-driven-development"],
                "configured_connectors": ["context7"],
                "configured_modes": ["implementation"],
                "connector_read_or_mutate_scope": {"context7": "read"},
                "intended_side_effects": ["bounded-code-edit", "local-test", "candidate-handoff"],
                "prohibited_side_effects": ["merge", "deploy"],
                "requires_dispatch_readback": False,
            },
            "kwilo-patch": {
                "role": "agent-tooling-maintainer",
                "lane": "internal-tooling",
                "repositories": ["kwilo-app"],
                "workspace_classes": ["isolated-read-only-worktree"],
                "configured_cli_toolsets": ["file", "kanban", "terminal"],
                "configured_parent_skills": ["test-driven-development"],
                "configured_connectors": ["context7"],
                "configured_modes": ["verification"],
                "connector_read_or_mutate_scope": {"context7": "read"},
                "intended_side_effects": ["deterministic-test", "evidence-capture"],
                "prohibited_side_effects": ["candidate-edit", "merge", "deploy"],
            },
            "kwilo-sentinel": {
                "role": "independent-code-security-review",
                "lane": "cross-repository-review",
                "repositories": ["kwilo-app", "kwilo-site"],
                "workspace_classes": ["isolated-read-only-worktree"],
                "configured_cli_toolsets": ["file", "kanban", "terminal"],
                "configured_parent_skills": ["github-code-review"],
                "configured_connectors": ["context7"],
                "configured_modes": ["read-only-review"],
                "connector_read_or_mutate_scope": {"context7": "read"},
                "intended_side_effects": ["read-only-review", "verdict-handoff"],
                "prohibited_side_effects": ["candidate-edit", "merge", "deploy"],
            },
            "kwilo-tess": {
                "role": "independent-qa",
                "lane": "cross-repository-qa",
                "repositories": ["kwilo-app", "kwilo-site"],
                "workspace_classes": ["isolated-read-only-worktree"],
                "configured_cli_toolsets": ["file", "kanban", "terminal"],
                "configured_parent_skills": ["test-driven-development"],
                "configured_connectors": ["context7"],
                "configured_modes": ["read-only-qa"],
                "connector_read_or_mutate_scope": {"context7": "read"},
                "intended_side_effects": ["read-only-review", "verdict-handoff"],
                "prohibited_side_effects": ["candidate-edit", "merge", "deploy"],
            },
        },
        "repositories": {
            "kwilo-app": {
                "canonical": "Hello-Kwilo/Kwilo",
                "local_root": str(tmp_path / "kwilo-app"),
            },
            "kwilo-site": {
                "canonical": "Hello-Kwilo/Kwilo-Site",
                "local_root": str(tmp_path / "kwilo-site"),
            },
        },
    }
    manifest_path = governance / "profile-capabilities-v1.0.0.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for profile_name, profile in manifest["profiles"].items():
        profile_dir = home / "profiles" / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_config = {
            "platform_toolsets": {"cli": profile["configured_cli_toolsets"]},
            "mcp_servers": {
                connector: {"enabled": True}
                for connector in profile["configured_connectors"]
            },
        }
        (profile_dir / "config.yaml").write_text(json.dumps(profile_config), encoding="utf-8")
        for skill_name in profile["configured_parent_skills"]:
            skill_dir = profile_dir / "skills" / "test-fixtures" / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                f"name: {skill_name}\n"
                "description: Test-only governed profile capability.\n"
                "platforms: [linux, macos, windows]\n"
                "---\n",
                encoding="utf-8",
            )
    semantic_policy = {
        "phases": [
            "implementation", "deterministic-verification", "sentinel-review",
            "tess-qa", "merge-readiness", "release-readiness",
        ],
        "verdicts": ["pass", "fail", "changes-requested", "blocked"],
        "verdict_required_for": [
            "deterministic-verification", "sentinel-review", "tess-qa",
            "merge-readiness", "release-readiness",
        ],
        "revision_bound_phases": [
            "implementation", "deterministic-verification", "sentinel-review",
            "tess-qa", "merge-readiness", "release-readiness",
        ],
        "readiness": {
            "legacy_required_gate_phases": [
                "deterministic-verification", "sentinel-review", "tess-qa",
            ],
            "required_verdict": "pass",
            "same_candidate_required": True,
            "host_attested_deterministic_verification": True,
            "risk_policy": {
                "gate_order": [
                    "deterministic-verification", "sentinel-review", "tess-qa",
                ],
                "tiers": {
                    "low": {
                        "required_gate_phases": [
                            "deterministic-verification", "sentinel-review",
                        ],
                    },
                    "standard": {
                        "required_gate_phases": [
                            "deterministic-verification", "sentinel-review",
                        ],
                    },
                    "high": {
                        "required_gate_phases": [
                            "deterministic-verification",
                            "sentinel-review",
                            "tess-qa",
                        ],
                    },
                },
                "categories": {
                    "documentation": {
                        "minimum_tier": "low",
                        "required_gate_phases": [],
                    },
                    "tests-only": {
                        "minimum_tier": "low",
                        "required_gate_phases": [],
                    },
                    "internal-tooling": {
                        "minimum_tier": "low",
                        "required_gate_phases": [],
                    },
                    "product-backend": {
                        "minimum_tier": "standard",
                        "required_gate_phases": [],
                    },
                    "user-facing": {
                        "minimum_tier": "standard",
                        "required_gate_phases": ["tess-qa"],
                    },
                },
            },
        },
    }
    policy_path = governance / "semantic-policy-v1.0.0.json"
    policy_path.write_text(json.dumps(semantic_policy), encoding="utf-8")
    (governance / "activation.json").write_text(
        json.dumps({
            "activated_at": "2026-07-22T16:44:52Z",
            "capability_manifest": {
                "path": str(manifest_path),
                "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            },
            "semantic_policy": {
                "path": str(policy_path),
                "sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            },
        }),
        encoding="utf-8",
    )
    kb.init_db(board="kwilo")
    return home, governance, manifest_path


def _contract(
    *,
    profile="kwilo-forge",
    phase="implementation",
    candidate="a" * 40,
    repository="kwilo-app",
):
    review = profile in {"kwilo-patch", "kwilo-sentinel", "kwilo-tess"}
    tess = profile == "kwilo-tess"
    deterministic = profile == "kwilo-patch"
    return {
        "requester": "gavin",
        "authoriser": "gavin",
        "acceptance_owner": "gavin",
        "role": "independent-qa" if tess else ("agent-tooling-maintainer" if deterministic else ("independent-code-security-review" if review else "software-engineer")),
        "lane": "cross-repository-qa" if tess else ("internal-tooling" if deterministic else ("cross-repository-review" if review else "app-general")),
        "repository": repository,
        "project_id": f"{repository}-project",
        "mode": "read-only-qa" if tess else ("verification" if deterministic else ("read-only-review" if review else "implementation")),
        "workspace_class": "isolated-read-only-worktree" if review else "isolated-worktree",
        "required_toolsets": ["file", "kanban", "terminal"],
        "required_parent_skills": ["test-driven-development"] if (tess or deterministic) else (["github-code-review"] if review else ["test-driven-development"]),
        "required_connectors": ["context7"],
        "connector_read_or_mutate_scope": {"context7": "read"},
        "allowed_side_effects": ["deterministic-test", "evidence-capture"] if deterministic else (["read-only-review", "verdict-handoff"] if review else ["bounded-code-edit", "local-test"]),
        "prohibited_actions": ["candidate-edit", "merge", "deploy"] if review else ["merge", "deploy"],
        "canonical_source": "https://github.com/Hello-Kwilo/Kwilo/issues/1",
        "evidence_destination": "https://github.com/Hello-Kwilo/Kwilo/pull/1",
        "phase": phase,
        "base_revision": "b" * 40,
        "candidate_identity": {"kind": "commit-sha", "value": candidate, "path_set_digest": None},
        "workflow_version": "3.0.0",
        "risk_tier": "standard",
        "change_categories": ["product-backend"],
        "required_gate_phases": [
            "deterministic-verification",
            "sentinel-review",
        ],
    }


def _parse_kanban_cli(argv: list[str]) -> argparse.Namespace:
    from hermes_cli import kanban as kb_cli

    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="command")
    kb_cli.build_parser(subparsers)
    return root.parse_args(["kanban", *argv])


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "kwilo@example.com"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Kwilo Test"],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True, text=True,
    )


def _rewrite_manifest(governance: Path, manifest_path: Path, manifest: dict) -> None:
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    activation_path = governance / "activation.json"
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    activation["capability_manifest"]["sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    activation_path.write_text(json.dumps(activation), encoding="utf-8")


def _evidence(*, phase="sentinel-review", verdict="pass", candidate="a" * 40):
    return {
        "phase": phase,
        "verdict": verdict,
        "candidate_identity": {"kind": "commit-sha", "value": candidate, "path_set_digest": None},
        "checks": {"executed": ["git diff --check"], "passed_count": 1, "failed_count": 0, "skipped_count": 0, "host_attested": True, "canonical_check_links": []},
        "blockers": [],
        "unresolved_acceptance_rows": [],
        "canonical_links": ["https://github.com/Hello-Kwilo/Kwilo/pull/1"],
    }


def test_kwilo_creation_requires_and_persists_governance_contract(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        with pytest.raises(ValueError, match="governance_contract is required"):
            kb.create_task(conn, title="ungoverned", assignee="kwilo-forge", board="kwilo")

        task_id = kb.create_task(
            conn,
            title="governed",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )
        semantics = kb.get_task_semantics(conn, task_id)

    assert semantics["phase"] == "implementation"
    assert semantics["repository_id"] == "Hello-Kwilo/Kwilo"
    assert semantics["candidate_value"] == "a" * 40
    assert semantics["contract"]["acceptance_owner"] == "gavin"


def test_kwilo_cli_create_accepts_and_persists_governance_contract(kwilo_home, monkeypatch, capsys):
    from hermes_cli import kanban as kb_cli

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "kwilo")
    contract = _contract(profile="kwilo-tess", phase="tess-qa")
    args = _parse_kanban_cli([
        "create", "governed CLI task",
        "--assignee", "kwilo-tess",
        "--workspace", "worktree",
        "--initial-status", "blocked",
        "--governance-contract", json.dumps(contract),
        "--json",
    ])

    assert kb_cli.kanban_command(args) == 0
    created = json.loads(capsys.readouterr().out)
    with kb.connect(board="kwilo") as conn:
        semantics = kb.get_task_semantics(conn, created["id"])

    assert semantics["phase"] == "tess-qa"
    assert semantics["candidate_value"] == "a" * 40
    assert {
        key: semantics["contract"][key]
        for key in contract
    } == contract
    assert semantics["contract"]["repository_id"] == "Hello-Kwilo/Kwilo"


def test_kwilo_cli_governance_contract_supports_at_file(kwilo_home, monkeypatch, tmp_path, capsys):
    from hermes_cli import kanban as kb_cli

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "kwilo")
    contract = _contract(profile="kwilo-tess", phase="tess-qa")
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8-sig")
    args = _parse_kanban_cli([
        "create", "file governed CLI task",
        "--assignee", "kwilo-tess",
        "--workspace", "worktree",
        "--initial-status", "blocked",
        "--max-retries", "1",
        "--governance-contract", f"@{contract_path}",
        "--json",
    ])

    assert kb_cli.kanban_command(args) == 0
    created = json.loads(capsys.readouterr().out)
    with kb.connect(board="kwilo") as conn:
        semantics = kb.get_task_semantics(conn, created["id"])

    assert {
        key: semantics["contract"][key]
        for key in contract
    } == contract
    assert semantics["contract"]["repository_id"] == "Hello-Kwilo/Kwilo"


def test_kwilo_cli_governance_contract_rejects_invalid_input_without_creating_task(
    kwilo_home, monkeypatch, tmp_path, capsys
):
    from hermes_cli import kanban as kb_cli

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "kwilo")
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    cases = [
        ("", "must not be empty"),
        ("@", "@path must name a JSON file"),
        ("{", "invalid JSON"),
        ("[]", "must decode to a JSON object"),
        (f"@{tmp_path / 'missing.json'}", "cannot read"),
        (f"@{invalid_utf8}", "cannot read"),
    ]

    for value, expected_error in cases:
        args = _parse_kanban_cli([
            "create", "invalid governed task",
            "--assignee", "kwilo-tess",
            "--workspace", "worktree",
            "--governance-contract", value,
        ])
        assert kb_cli.kanban_command(args) == 2
        assert expected_error in capsys.readouterr().err
        with kb.connect(board="kwilo") as conn:
            assert kb.list_tasks(conn) == []


def test_kwilo_cli_dispatch_readback_requires_separate_admission(kwilo_home, monkeypatch, capsys):
    from hermes_cli import kanban as kb_cli

    _, governance, manifest_path = kwilo_home
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"]["kwilo-tess"]["requires_dispatch_readback"] = True
    _rewrite_manifest(governance, manifest_path, manifest)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "kwilo")
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="readback governed CLI task",
            assignee="kwilo-tess",
            board="kwilo",
            workspace_kind="worktree",
            initial_status="blocked",
            governance_contract=_contract(profile="kwilo-tess", phase="tess-qa"),
        )

    readback_args = _parse_kanban_cli(["readback", task_id, "--json"])
    assert kb_cli.kanban_command(readback_args) == 0
    readback = json.loads(capsys.readouterr().out)
    assert readback["required"] is True
    assert readback["admitted"] is False
    assert readback["snapshot"]["task"]["id"] == task_id
    assert readback["snapshot"]["governance_contract"]["phase"] == "tess-qa"

    wrong_digest = _parse_kanban_cli([
        "admit", task_id, "0" * 64, "--actor", "test-operator", "--json",
    ])
    assert kb_cli.kanban_command(wrong_digest) == 1
    assert "digest" in capsys.readouterr().err

    with kb.connect(board="kwilo") as conn:
        assert kb.dispatch_readback(conn, task_id)["admitted"] is False
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("changed", task_id))
        conn.commit()

    stale_digest = _parse_kanban_cli([
        "admit", task_id, readback["digest"], "--actor", "test-operator", "--json",
    ])
    assert kb_cli.kanban_command(stale_digest) == 1
    assert "digest" in capsys.readouterr().err

    current_args = _parse_kanban_cli(["readback", task_id, "--json"])
    assert kb_cli.kanban_command(current_args) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["digest"] != readback["digest"]

    admit_args = _parse_kanban_cli([
        "admit", task_id, current["digest"],
        "--actor", "test-operator", "--json",
    ])
    assert kb_cli.kanban_command(admit_args) == 0
    admitted = json.loads(capsys.readouterr().out)
    assert admitted["admitted"] is True
    assert admitted["admitted_by"] == "test-operator"

    with kb.connect(board="kwilo") as conn:
        assert kb.dispatch_readback(conn, task_id)["admitted"] is True


def test_governed_worktree_digest_is_stable_across_first_materialization(
    kwilo_home, tmp_path
):
    _, governance, manifest_path = kwilo_home
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"]["kwilo-tess"]["requires_dispatch_readback"] = True
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    manifest["repositories"]["kwilo-app"]["local_root"] = str(repo)
    _rewrite_manifest(governance, manifest_path, manifest)
    kb.write_board_metadata("kwilo", default_workdir=str(repo))

    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="stable governed worktree",
            assignee="kwilo-tess",
            board="kwilo",
            workspace_kind="worktree",
            initial_status="blocked",
            governance_contract=_contract(profile="kwilo-tess", phase="tess-qa"),
        )
        expected = repo / ".worktrees" / task_id
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert Path(task.workspace_path) == expected
        assert task.branch_name == f"wt/{task_id}"
        before = kb.dispatch_readback(conn, task_id)
        kb.admit_dispatch_readback(
            conn, task_id, expected_digest=before["digest"], actor="test-operator"
        )
        assert kb.unblock_task(conn, task_id)
        dispatched = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace, board=None: None,
            board="kwilo",
        )
        assert dispatched.spawned == [
            (task_id, "kwilo-tess", str(expected))
        ]
        assert kb.complete_task(
            conn,
            task_id,
            semantic_evidence=_evidence(phase="tess-qa"),
        )
        after = kb.dispatch_readback(conn, task_id)

    assert after["digest"] == before["digest"]
    assert after["admitted"] is True


def test_governed_repository_routes_to_manifest_root_and_rejects_mismatches(
    kwilo_home, tmp_path
):
    _, governance, manifest_path = kwilo_home
    app_repo = tmp_path / "app-repo"
    site_repo = tmp_path / "site-repo"
    _init_git_repo(app_repo)
    _init_git_repo(site_repo)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["repositories"]["kwilo-app"]["local_root"] = str(app_repo)
    manifest["repositories"]["kwilo-site"]["local_root"] = str(site_repo)
    _rewrite_manifest(governance, manifest_path, manifest)
    kb.write_board_metadata("kwilo", default_workdir=str(app_repo))

    site_contract = _contract(
        profile="kwilo-tess",
        phase="tess-qa",
        repository="kwilo-site",
    )
    with kb.connect(board="kwilo") as conn:
        for unsafe_target in [
            site_repo / ".worktrees",
            site_repo / ".worktrees" / "nested" / "target",
        ]:
            assert not unsafe_target.exists()
            with pytest.raises(ValueError, match="repository workspace"):
                kb.create_task(
                    conn,
                    title="reject unsafe worktree shape",
                    assignee="kwilo-tess",
                    board="kwilo",
                    workspace_kind="worktree",
                    workspace_path=str(unsafe_target),
                    governance_contract=site_contract,
                )

        task_id = kb.create_task(
            conn,
            title="site review",
            assignee="kwilo-tess",
            board="kwilo",
            workspace_kind="worktree",
            governance_contract=site_contract,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert Path(task.workspace_path) == site_repo / ".worktrees" / task_id

        with pytest.raises(ValueError, match="workspace_class.*requires.*worktree"):
            kb.create_task(
                conn,
                title="wrong workspace kind",
                assignee="kwilo-tess",
                board="kwilo",
                workspace_kind="dir",
                governance_contract=site_contract,
            )

        with pytest.raises(ValueError, match="repository.*workspace"):
            kb.create_task(
                conn,
                title="wrong repository root",
                assignee="kwilo-tess",
                board="kwilo",
                workspace_kind="worktree",
                workspace_path=str(app_repo),
                governance_contract=site_contract,
            )


def test_governed_existing_worktree_without_branch_keeps_admitted_digest(
    kwilo_home, tmp_path
):
    _, governance, manifest_path = kwilo_home
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    existing = repo / ".worktrees" / "existing"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "existing", str(existing)],
        check=True,
        capture_output=True,
        text=True,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"]["kwilo-tess"]["requires_dispatch_readback"] = True
    manifest["repositories"]["kwilo-app"]["local_root"] = str(repo)
    _rewrite_manifest(governance, manifest_path, manifest)

    with kb.connect(board="kwilo") as conn:
        with pytest.raises(ValueError, match="not requested branch"):
            kb.create_task(
                conn,
                title="reject stale branch contract",
                assignee="kwilo-tess",
                board="kwilo",
                workspace_kind="worktree",
                workspace_path=str(existing),
                branch_name="different",
                initial_status="blocked",
                governance_contract=_contract(
                    profile="kwilo-tess", phase="tess-qa"
                ),
            )
        task_id = kb.create_task(
            conn,
            title="reuse admitted worktree",
            assignee="kwilo-tess",
            board="kwilo",
            workspace_kind="worktree",
            workspace_path=str(existing / ".." / "existing"),
            initial_status="blocked",
            governance_contract=_contract(profile="kwilo-tess", phase="tess-qa"),
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert Path(task.workspace_path) == existing
        assert task.branch_name == "existing"
        before = kb.dispatch_readback(conn, task_id)
        kb.admit_dispatch_readback(
            conn, task_id, expected_digest=before["digest"], actor="test-operator"
        )
        assert kb.unblock_task(conn, task_id)
        dispatched = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace, board=None: None,
            board="kwilo",
        )
        assert dispatched.spawned == [
            (task_id, "kwilo-tess", str(existing))
        ]
        assert kb.complete_task(
            conn,
            task_id,
            semantic_evidence=_evidence(phase="tess-qa"),
        )
        after = kb.dispatch_readback(conn, task_id)

    assert after["digest"] == before["digest"]
    assert after["admitted"] is True


def test_non_kwilo_board_remains_legacy_compatible(kwilo_home, monkeypatch, capsys):
    from hermes_cli import kanban as kb_cli

    kb.init_db(board="ordinary")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "ordinary")
    rejected = _parse_kanban_cli([
        "create", "misrouted governed card", "--assignee", "alice",
        "--governance-contract", json.dumps(_contract()), "--json",
    ])
    assert kb_cli.kanban_command(rejected) == 1
    assert "does not enforce governance" in capsys.readouterr().err
    with kb.connect(board="ordinary") as conn:
        assert kb.list_tasks(conn) == []

    args = _parse_kanban_cli([
        "create", "legacy", "--assignee", "alice", "--json",
    ])
    assert kb_cli.kanban_command(args) == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    with kb.connect(board="ordinary") as conn:
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.get_task_semantics(conn, task_id) is None

    readback_args = _parse_kanban_cli(["readback", task_id, "--json"])
    assert kb_cli.kanban_command(readback_args) == 0
    readback = json.loads(capsys.readouterr().out)
    assert readback["required"] is False
    assert readback["admitted"] is True
    assert readback["snapshot"] is None

    admit_args = _parse_kanban_cli(["admit", task_id, "0" * 64])
    assert kb_cli.kanban_command(admit_args) == 1
    assert "does not require dispatch readback" in capsys.readouterr().err


def test_kwilo_cli_help_exposes_governance_readback_commands(capsys):
    for argv, expected in [
        (["create", "--help"], "--governance-contract"),
        (["readback", "--help"], "task_id"),
        (["admit", "--help"], "digest"),
    ]:
        with pytest.raises(SystemExit) as exc:
            _parse_kanban_cli(argv)
        assert exc.value.code == 0
        assert expected in capsys.readouterr().out


def test_top_level_cli_propagates_kanban_command_exit_codes(kwilo_home):
    env = os.environ.copy()
    env["HERMES_KANBAN_BOARD"] = "kwilo"

    def run_kanban(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    success = run_kanban("list", "--json")
    missing_task = run_kanban("admit", "t_missing", "0" * 64)
    handler_usage_error = run_kanban("daemon")

    assert success.returncode == 0
    assert json.loads(success.stdout) == []
    assert missing_task.returncode == 1
    assert "task t_missing not found" in missing_task.stderr
    assert handler_usage_error.returncode == 2
    assert "DEPRECATED" in handler_usage_error.stderr


def test_file_backed_kwilo_connection_outranks_mismatched_ambient_board(kwilo_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "ordinary")
    with kb.connect(board="kwilo") as conn:
        assert kb._kwilo.is_kwilo_board(conn) is True
        with pytest.raises(ValueError, match="governance_contract is required"):
            kb.create_task(conn, title="must remain governed", assignee="kwilo-forge")


def test_file_backed_ordinary_connection_outranks_mismatched_ambient_board(kwilo_home, monkeypatch):
    kb.init_db(board="ordinary")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "kwilo")
    with kb.connect(board="ordinary") as conn:
        assert kb._kwilo.is_kwilo_board(conn) is False
        task_id = kb.create_task(conn, title="must remain legacy", assignee="alice")
        assert kb.get_task_semantics(conn, task_id) is None


def test_mismatched_ambient_board_cannot_bypass_claim_or_completion(kwilo_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "ordinary")
    with kb.connect(board="kwilo") as conn:
        claim_id = kb.create_task(
            conn, title="claim admission", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )
        complete_id = kb.create_task(
            conn, title="completion admission", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(candidate="c" * 40),
        )
        conn.execute("UPDATE tasks SET assignee = 'kwilo-tess' WHERE id IN (?, ?)", (claim_id, complete_id))
        conn.commit()

        assert kb.claim_task(conn, claim_id) is None
        assert kb.get_task(conn, claim_id).status == "blocked"
        with pytest.raises(ValueError, match="governed completion rejected"):
            kb.complete_task(conn, complete_id)
        assert kb.get_task(conn, complete_id).status == "ready"


def test_public_create_keeps_explicit_kwilo_board_under_ambient_mismatch(kwilo_home, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "ordinary")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    result = json.loads(kt._handle_create({
        "title": "public task must remain governed",
        "assignee": "kwilo-forge",
        "board": "kwilo",
    }))
    assert result.get("error")
    assert "governance_contract is required" in result["error"]


def test_capability_mismatch_is_rejected_before_task_creation(kwilo_home):
    contract = _contract()
    contract["required_toolsets"].append("browser")
    with kb.connect(board="kwilo") as conn, pytest.raises(ValueError, match="required_toolsets.*browser"):
        kb.create_task(
            conn,
            title="wrong capability",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=contract,
        )


def test_undeclared_effective_profile_toolset_is_rejected_at_creation(kwilo_home):
    home, _, _ = kwilo_home
    config_path = home / "profiles" / "kwilo-forge" / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["platform_toolsets"]["cli"].append("computer_use")
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with kb.connect(board="kwilo") as conn, pytest.raises(
        ValueError, match="effective CLI toolsets do not match"
    ):
        kb.create_task(
            conn, title="undeclared effective tool", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )


def test_missing_effective_profile_skill_blocks_creation_and_claim(kwilo_home):
    home, _, _ = kwilo_home
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn, title="claim after skill drift", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )

        skill_path = (
            home / "profiles" / "kwilo-forge" / "skills" / "test-fixtures"
            / "test-driven-development" / "SKILL.md"
        )
        skill_path.unlink()

        with pytest.raises(ValueError, match="effective parent skills unavailable"):
            kb.create_task(
                conn, title="missing effective skill", assignee="kwilo-forge", board="kwilo",
                governance_contract=_contract(candidate="d" * 40),
            )
        assert kb.claim_task(conn, task_id) is None
        assert kb.get_task(conn, task_id).status == "blocked"


def test_governed_creation_force_loads_contract_parent_skills(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="contract skill activation",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )

        assert kb.get_task(conn, task_id).skills == ["test-driven-development"]


def test_governed_contract_skills_reach_spawned_worker_argv(
    kwilo_home, monkeypatch
):
    home, _, _ = kwilo_home
    captured = {}

    class FakeProc:
        pid = 42

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="governed spawn activation",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )
        kb._default_spawn(
            kb.get_task(conn, task_id),
            str(home),
            board="kwilo",
        )

    cmd = captured["cmd"]
    assert [cmd[index + 1] for index, token in enumerate(cmd) if token == "--skills"] == [
        "test-driven-development"
    ]
    assert captured["env"]["HERMES_PLATFORM"] == "cli"


def test_governed_claim_fails_closed_if_contract_skill_activation_drifts(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="contract skill drift",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )
        conn.execute("UPDATE tasks SET skills = NULL WHERE id = ?", (task_id,))
        conn.commit()

        assert kb.claim_task(conn, task_id) is None
        task = kb.get_task(conn, task_id)
        event = kb.list_events(conn, task_id)[-1]
        assert task.status == "blocked"
        assert event.kind == "capability_admission_failed"
        assert (
            "contract-required worker skills are not activated"
            in event.payload["reason"]
        )


def test_legacy_semantic_contract_still_requires_worker_skill_activation(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="legacy contract activation drift",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )
        semantics = kwilo.get_task_semantics(conn, task_id)
        legacy_contract = dict(semantics["contract"])
        legacy_contract["workflow_version"] = "2.0.0"
        conn.execute(
            "UPDATE task_semantics SET contract_json = ? WHERE task_id = ?",
            (json.dumps(legacy_contract), task_id),
        )
        conn.execute("UPDATE tasks SET skills = NULL WHERE id = ?", (task_id,))
        conn.commit()

        assert (
            "contract-required worker skills are not activated"
            in kwilo.admission_error(conn, task_id)
        )


def test_governed_creation_unions_explicit_and_contract_parent_skills(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="contract and optional skills",
            assignee="kwilo-forge",
            board="kwilo",
            skills=["optional-review"],
            governance_contract=_contract(),
        )

        assert kb.get_task(conn, task_id).skills == [
            "optional-review",
            "test-driven-development",
        ]


def test_governed_creation_canonicalizes_contract_parent_skills(kwilo_home):
    contract = _contract()
    contract["required_parent_skills"] = [
        " test-driven-development ",
        "test-driven-development",
    ]

    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="canonical contract skills",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=contract,
        )

        task = kb.get_task(conn, task_id)
        semantics = kwilo.get_task_semantics(conn, task_id)
        assert task.skills == ["test-driven-development"]
        assert semantics["contract"]["required_parent_skills"] == [
            "test-driven-development"
        ]
        assert kb.claim_task(conn, task_id) is not None


def test_long_frontmatter_platform_gate_blocks_governed_creation(kwilo_home):
    home, _, _ = kwilo_home
    skill_path = (
        home / "profiles" / "kwilo-forge" / "skills" / "test-fixtures"
        / "test-driven-development" / "SKILL.md"
    )
    skill_path.write_text(
        "---\nname: test-driven-development\ndescription: |\n  "
        + ("x" * 4500)
        + "\nplatforms: [not-this-platform]\n---\n",
        encoding="utf-8",
    )

    with kb.connect(board="kwilo") as conn, pytest.raises(
        ValueError, match="effective parent skills unavailable"
    ):
        kb.create_task(
            conn,
            title="long frontmatter",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )


def test_blank_external_skill_dir_does_not_scan_entire_profile(kwilo_home):
    home, _, _ = kwilo_home
    profile_dir = home / "profiles" / "kwilo-forge"
    local_skill = (
        profile_dir / "skills" / "test-fixtures" / "test-driven-development"
        / "SKILL.md"
    )
    local_skill.unlink()
    stray_skill = profile_dir / "stray" / "test-driven-development" / "SKILL.md"
    stray_skill.parent.mkdir(parents=True)
    stray_skill.write_text(
        "---\nname: test-driven-development\n"
        "platforms: [linux, macos, windows]\n---\n",
        encoding="utf-8",
    )
    config_path = profile_dir / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["skills"] = {"external_dirs": [""]}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with kb.connect(board="kwilo") as conn, pytest.raises(
        ValueError, match="effective parent skills unavailable"
    ):
        kb.create_task(
            conn,
            title="blank external root",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )


def test_external_profile_skill_in_scripts_category_is_effective(kwilo_home):
    home, _, _ = kwilo_home
    profile_dir = home / "profiles" / "kwilo-forge"
    local_skill = (
        profile_dir / "skills" / "test-fixtures" / "test-driven-development"
        / "SKILL.md"
    )
    local_skill.unlink()
    external_skill = (
        profile_dir / "external-skills" / "scripts" / "test-driven-development"
        / "SKILL.md"
    )
    external_skill.parent.mkdir(parents=True)
    external_skill.write_text(
        "---\nname: test-driven-development\nplatforms: [linux, macos, windows]\n---\n",
        encoding="utf-8",
    )
    config_path = profile_dir / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["skills"] = {"external_dirs": ["external-skills"]}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn, title="external skill", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )
        assert kb.get_task(conn, task_id) is not None


def test_disabled_effective_profile_skill_is_unavailable(kwilo_home):
    home, _, _ = kwilo_home
    profile_dir = home / "profiles" / "kwilo-forge"
    skill_path = (
        profile_dir / "skills" / "test-fixtures" / "test-driven-development"
        / "SKILL.md"
    )
    skill_path.write_text(
        "---\nname: canonical-disabled-skill\n"
        "platforms: [linux, macos, windows]\n---\n",
        encoding="utf-8",
    )
    config_path = profile_dir / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["skills"] = {"disabled": ["canonical-disabled-skill"]}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with kb.connect(board="kwilo") as conn, pytest.raises(
        ValueError, match="effective parent skills unavailable"
    ):
        kb.create_task(
            conn, title="disabled skill", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )


def test_platform_incompatible_effective_profile_skill_is_unavailable(kwilo_home):
    home, _, _ = kwilo_home
    skill_path = (
        home / "profiles" / "kwilo-forge" / "skills" / "test-fixtures"
        / "test-driven-development" / "SKILL.md"
    )
    skill_path.write_text(
        "---\nname: test-driven-development\nplatforms: [not-this-platform]\n---\n",
        encoding="utf-8",
    )

    with kb.connect(board="kwilo") as conn, pytest.raises(
        ValueError, match="effective parent skills unavailable"
    ):
        kb.create_task(
            conn, title="wrong platform skill", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )


def test_ambiguous_effective_profile_skill_blocks_creation_and_claim(kwilo_home):
    home, _, _ = kwilo_home
    profile_dir = home / "profiles" / "kwilo-forge"
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn, title="claim after skill collision", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )

        external_skill = profile_dir / "external-skills" / "duplicate" / "SKILL.md"
        external_skill.parent.mkdir(parents=True)
        external_skill.write_text(
            "---\nname: test-driven-development\nplatforms: [linux, macos, windows]\n---\n",
            encoding="utf-8",
        )
        config_path = profile_dir / "config.yaml"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["skills"] = {"external_dirs": ["external-skills"]}
        config_path.write_text(json.dumps(config), encoding="utf-8")

        with pytest.raises(ValueError, match="effective parent skills unavailable"):
            kb.create_task(
                conn, title="ambiguous skill", assignee="kwilo-forge", board="kwilo",
                governance_contract=_contract(candidate="d" * 40),
            )
        assert kb.claim_task(conn, task_id) is None
        assert kb.get_task(conn, task_id).status == "blocked"


def test_effective_profile_connector_drift_blocks_claim_and_completion(kwilo_home):
    home, _, _ = kwilo_home
    with kb.connect(board="kwilo") as conn:
        claim_id = kb.create_task(
            conn, title="claim after drift", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )
        complete_id = kb.create_task(
            conn, title="complete after drift", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(candidate="d" * 40),
        )

        config_path = home / "profiles" / "kwilo-forge" / "config.yaml"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["mcp_servers"]["open-notebook"] = {"enabled": True}
        config_path.write_text(json.dumps(config), encoding="utf-8")

        assert kb.claim_task(conn, claim_id) is None
        assert kb.get_task(conn, claim_id).status == "blocked"
        with pytest.raises(ValueError, match="governed completion rejected"):
            kb.complete_task(conn, complete_id)
        assert kb.get_task(conn, complete_id).status == "ready"


def test_contract_mode_must_be_manifest_admitted(kwilo_home):
    contract = _contract()
    contract["mode"] = "deployment"
    with kb.connect(board="kwilo") as conn, pytest.raises(ValueError, match="mode.*not admitted"):
        kb.create_task(
            conn, title="wrong mode", assignee="kwilo-forge", board="kwilo",
            governance_contract=contract,
        )


def test_connector_scopes_must_exactly_match_manifest_capabilities(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        wrong_scope = _contract()
        wrong_scope["connector_read_or_mutate_scope"] = {"context7": "mutate"}
        with pytest.raises(ValueError, match="connector.*scope mismatch"):
            kb.create_task(
                conn, title="scope escalation", assignee="kwilo-forge", board="kwilo",
                governance_contract=wrong_scope,
            )

        extra_scope = _contract()
        extra_scope["connector_read_or_mutate_scope"]["github"] = "read"
        with pytest.raises(ValueError, match="exactly declare required connectors"):
            kb.create_task(
                conn, title="undeclared connector", assignee="kwilo-forge", board="kwilo",
                governance_contract=extra_scope,
            )


def test_capability_is_rechecked_immediately_before_claim(kwilo_home):
    _, governance, manifest_path = kwilo_home
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="capability changes",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["profiles"]["kwilo-forge"]["configured_cli_toolsets"].remove("terminal")
        _rewrite_manifest(governance, manifest_path, manifest)

        assert kb.claim_task(conn, task_id) is None
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert task.status == "blocked"
    assert events[-1].kind == "capability_admission_failed"
    assert "terminal" in events[-1].payload["reason"]


def test_strict_dispatch_requires_separate_readback_admission_before_claim(
    kwilo_home,
):
    _, governance, manifest_path = kwilo_home
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"]["kwilo-forge"]["requires_dispatch_readback"] = True
    _rewrite_manifest(governance, manifest_path, manifest)

    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="read back before dispatch",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )

        assert kb.get_task(conn, task_id).status == "todo"
        readback = kb.dispatch_readback(conn, task_id)
        assert readback["required"] is True
        assert readback["admitted"] is False
        assert len(readback["digest"]) == 64
        with pytest.raises(ValueError, match="digest does not match"):
            kb.admit_dispatch_readback(
                conn,
                task_id,
                expected_digest="0" * 64,
                actor="hermes",
            )

        admitted = kb.admit_dispatch_readback(
            conn,
            task_id,
            expected_digest=readback["digest"],
            actor="hermes",
        )
        assert admitted["admitted"] is True
        assert admitted["digest"] == readback["digest"]
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.claim_task(conn, task_id, claimer="single-writer") is not None


def test_dispatch_readback_treats_windows_path_separators_as_equivalent(
    kwilo_home, tmp_path,
):
    _, governance, manifest_path = kwilo_home
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"]["kwilo-forge"]["requires_dispatch_readback"] = True
    repository_root = tmp_path / "repository"
    manifest["repositories"]["kwilo-app"]["local_root"] = str(repository_root)
    _rewrite_manifest(governance, manifest_path, manifest)
    contract = _contract()
    contract["workspace_class"] = "declared-dirty-continuation"

    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="path canonicalisation",
            assignee="kwilo-forge",
            board="kwilo",
            workspace_kind="dir",
            workspace_path=repository_root.as_posix(),
            governance_contract=contract,
        )
        readback = kb.dispatch_readback(conn, task_id)
        kb.admit_dispatch_readback(
            conn,
            task_id,
            expected_digest=readback["digest"],
            actor="hermes",
        )

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                (str(repository_root), task_id),
            )

        after_resolution = kb.dispatch_readback(conn, task_id)
        assert after_resolution["digest"] == readback["digest"]
        assert after_resolution["admitted"] is True


def test_dispatch_readback_accepts_precanonical_windows_admission_digest(
    kwilo_home, tmp_path,
):
    _, governance, manifest_path = kwilo_home
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"]["kwilo-forge"]["requires_dispatch_readback"] = True
    repository_root = tmp_path / "repository"
    manifest["repositories"]["kwilo-app"]["local_root"] = str(repository_root)
    _rewrite_manifest(governance, manifest_path, manifest)
    contract = _contract()
    contract["workspace_class"] = "declared-dirty-continuation"

    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="legacy path admission",
            assignee="kwilo-forge",
            board="kwilo",
            workspace_kind="dir",
            workspace_path=str(repository_root),
            governance_contract=contract,
        )
        readback = kb.dispatch_readback(conn, task_id)
        assert readback["legacy_digest"]
        assert readback["legacy_digest"] != readback["digest"]

        # Simulate an admission written by the runtime before path
        # canonicalisation was introduced.
        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_semantics
                   SET dispatch_readback_digest = ?,
                       dispatch_readback_at = ?,
                       dispatch_readback_by = ?
                 WHERE task_id = ?
                """,
                (readback["legacy_digest"], 1, "legacy-hermes", task_id),
            )

        compatible = kb.dispatch_readback(conn, task_id)
        assert compatible["digest"] == readback["digest"]
        assert compatible["admitted_digest"] == readback["legacy_digest"]
        assert compatible["admitted"] is True


def test_dispatch_readback_is_invalidated_when_card_contract_surface_changes(
    kwilo_home,
):
    _, governance, manifest_path = kwilo_home
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"]["kwilo-forge"]["requires_dispatch_readback"] = True
    _rewrite_manifest(governance, manifest_path, manifest)

    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn,
            title="original bounded scope",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(),
        )
        readback = kb.dispatch_readback(conn, task_id)
        kb.admit_dispatch_readback(
            conn,
            task_id,
            expected_digest=readback["digest"],
            actor="hermes",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET title = ? WHERE id = ?",
                ("scope changed after readback", task_id),
            )

        assert kb.claim_task(conn, task_id, claimer="must-not-run") is None
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert "readback digest changed" in kb.list_events(conn, task_id)[-1].payload["reason"]


def test_review_claim_rechecks_capability_and_blocks_before_running(kwilo_home):
    _, governance, manifest_path = kwilo_home
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn, title="review-stage task", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["profiles"]["kwilo-sentinel"]["configured_cli_toolsets"].remove("terminal")
        _rewrite_manifest(governance, manifest_path, manifest)

        assert kb.claim_review_task(conn, task_id) is None
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.list_events(conn, task_id)[-1].kind == "capability_admission_failed"


def test_review_dispatch_rechecks_semantic_dependencies_before_spawn(kwilo_home, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    spawned = []
    with kb.connect(board="kwilo") as conn:
        parent = kb.create_task(
            conn, title="pending parent", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )
        review = kb.create_task(
            conn, title="review-stage dependent", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )
        kb.link_tasks(conn, parent, review)
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))

        result = kb.dispatch_once(
            conn, board="kwilo", spawn_fn=lambda task, workspace: spawned.append(task.id),
        )

        assert review not in spawned
        assert not any(item[0] == review for item in result.spawned)
        assert kb.get_task(conn, review).status == "todo"
        assert kb.list_events(conn, review)[-1].kind == "claim_rejected"


def test_admission_block_is_sticky_until_explicit_revalidation(kwilo_home):
    _, governance, manifest_path = kwilo_home
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn, title="sticky admission", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["profiles"]["kwilo-forge"]["configured_cli_toolsets"].remove("terminal")
        _rewrite_manifest(governance, manifest_path, manifest)
        assert kb.claim_task(conn, task_id) is None
        event_count = len(kb.list_events(conn, task_id))

        assert kb.recompute_ready(conn) == 0
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, task_id).status == "blocked"
        assert len(kb.list_events(conn, task_id)) == event_count

        manifest["profiles"]["kwilo-forge"]["configured_cli_toolsets"].append("terminal")
        _rewrite_manifest(governance, manifest_path, manifest)
        assert kb.unblock_task(conn, task_id) is True
        assert kb.claim_task(conn, task_id) is not None


def test_pre_activation_task_without_contract_keeps_legacy_claim_behaviour(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
            "VALUES ('legacy-pre-cutoff', 'existing', 'ready', 0, 1, 'scratch')"
        )
        claimed = kb.claim_task(conn, "legacy-pre-cutoff")
    assert claimed is not None
    assert claimed.status == "running"


def test_governed_readiness_creation_requires_policy_exact_pass_gates(kwilo_home):
    candidate = "9" * 40
    with kb.connect(board="kwilo") as conn:
        ordinary = kb.create_task(
            conn, title="ordinary", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(candidate=candidate),
        )
        deterministic = kb.create_task(
            conn, title="deterministic", assignee="kwilo-patch", board="kwilo",
            governance_contract=_contract(profile="kwilo-patch", phase="deterministic-verification", candidate=candidate),
        )
        sentinel = kb.create_task(
            conn, title="sentinel", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate),
        )
        tess = kb.create_task(
            conn, title="tess", assignee="kwilo-tess", board="kwilo",
            governance_contract=_contract(profile="kwilo-tess", phase="tess-qa", candidate=candidate),
        )

        with pytest.raises(
            ValueError,
            match="deterministic-verification.*sentinel-review",
        ):
            kb.create_task(
                conn, title="bypass", assignee="kwilo-forge", board="kwilo",
                parents=[ordinary], governance_contract=_contract(phase="merge-readiness", candidate=candidate),
            )

        readiness = kb.create_task(
            conn, title="readiness", assignee="kwilo-forge", board="kwilo",
            parents=[ordinary, deterministic, sentinel, tess],
            governance_contract=_contract(phase="merge-readiness", candidate=candidate),
        )
        links = conn.execute(
            "SELECT parent_id, link_kind, required_phase, required_verdict, require_candidate_match "
            "FROM task_links WHERE child_id = ? ORDER BY parent_id", (readiness,),
        ).fetchall()

    by_parent = {row["parent_id"]: dict(row) for row in links}
    assert by_parent[ordinary]["link_kind"] == "completion"
    assert by_parent[tess]["link_kind"] == "completion"
    for parent, phase in (
        (deterministic, "deterministic-verification"),
        (sentinel, "sentinel-review"),
    ):
        assert by_parent[parent]["link_kind"] == "evidence-gate"
        assert by_parent[parent]["required_phase"] == phase
        assert by_parent[parent]["required_verdict"] == "pass"
        assert by_parent[parent]["require_candidate_match"] == 1


def test_mandatory_readiness_gates_cannot_be_removed_or_downgraded(kwilo_home):
    candidate = "8" * 40
    with kb.connect(board="kwilo") as conn:
        deterministic = kb.create_task(
            conn, title="deterministic", assignee="kwilo-patch", board="kwilo",
            governance_contract=_contract(profile="kwilo-patch", phase="deterministic-verification", candidate=candidate),
        )
        sentinel = kb.create_task(
            conn, title="sentinel", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate),
        )
        tess = kb.create_task(
            conn, title="tess", assignee="kwilo-tess", board="kwilo",
            governance_contract=_contract(profile="kwilo-tess", phase="tess-qa", candidate=candidate),
        )
        ordinary = kb.create_task(
            conn, title="ordinary", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(candidate=candidate),
        )
        readiness = kb.create_task(
            conn, title="readiness", assignee="kwilo-forge", board="kwilo",
            parents=[deterministic, sentinel, tess, ordinary],
            governance_contract=_contract(phase="merge-readiness", candidate=candidate),
        )

        for parent in (deterministic, sentinel):
            with pytest.raises(ValueError, match="mandatory readiness evidence gate"):
                kb.unlink_tasks(conn, parent, readiness)
            with pytest.raises(ValueError, match="mandatory readiness evidence gate"):
                kb.link_tasks(conn, parent, readiness)

        assert kb.unlink_tasks(conn, ordinary, readiness) is True
        kb.link_tasks(conn, ordinary, readiness)

        links = conn.execute(
            "SELECT parent_id, link_kind FROM task_links WHERE child_id = ?",
            (readiness,),
        ).fetchall()
    assert {row["parent_id"]: row["link_kind"] for row in links} == {
        deterministic: "evidence-gate", sentinel: "evidence-gate",
        tess: "completion", ordinary: "completion",
    }


@pytest.mark.parametrize(
    ("profile", "phase", "expected"),
    (("kwilo-forge", "sentinel-review", "kwilo-sentinel"),
     ("kwilo-forge", "tess-qa", "kwilo-tess")),
)
def test_reserved_review_phases_require_authorized_profile(kwilo_home, profile, phase, expected):
    contract = _contract(profile=profile, phase=phase)
    with kb.connect(board="kwilo") as conn, pytest.raises(ValueError, match=expected):
        kb.create_task(
            conn, title="forged review identity", assignee=profile, board="kwilo",
            governance_contract=contract,
        )


def test_readiness_rechecks_exact_gate_producer_identity(kwilo_home):
    candidate = "7" * 40
    with kb.connect(board="kwilo") as conn:
        sentinel = kb.create_task(
            conn, title="sentinel", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate),
        )
        tess = kb.create_task(
            conn, title="tess", assignee="kwilo-tess", board="kwilo",
            governance_contract=_contract(profile="kwilo-tess", phase="tess-qa", candidate=candidate),
        )
        conn.execute("UPDATE tasks SET assignee = 'kwilo-forge' WHERE id = ?", (sentinel,))

        with pytest.raises(ValueError, match="kwilo-sentinel"):
            kb.create_task(
                conn, title="readiness", assignee="kwilo-forge", board="kwilo",
                parents=[sentinel, tess],
                governance_contract=_contract(phase="merge-readiness", candidate=candidate),
            )


def test_readiness_claim_rechecks_gate_producer_identity(kwilo_home):
    candidate = "5" * 40
    with kb.connect(board="kwilo") as conn:
        deterministic = kb.create_task(
            conn, title="deterministic", assignee="kwilo-patch", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-patch", phase="deterministic-verification", candidate=candidate,
            ),
        )
        sentinel = kb.create_task(
            conn, title="sentinel", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate),
        )
        tess = kb.create_task(
            conn, title="tess", assignee="kwilo-tess", board="kwilo",
            governance_contract=_contract(profile="kwilo-tess", phase="tess-qa", candidate=candidate),
        )
        readiness = kb.create_task(
            conn, title="readiness", assignee="kwilo-forge", board="kwilo",
            parents=[deterministic, sentinel, tess],
            governance_contract=_contract(phase="merge-readiness", candidate=candidate),
        )
        conn.execute("UPDATE tasks SET assignee = 'kwilo-forge' WHERE id = ?", (sentinel,))
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (readiness,))

        assert kb.claim_task(conn, readiness) is None
        assert kb.get_task(conn, readiness).status == "blocked"
        assert "kwilo-sentinel" in kb.list_events(conn, readiness)[-1].payload["reason"]


@pytest.mark.parametrize("phase", ["merge-readiness", "release-readiness"])
def test_governed_readiness_rejects_mismatched_gate_candidate(kwilo_home, phase):
    with kb.connect(board="kwilo") as conn:
        sentinel = kb.create_task(
            conn, title="sentinel", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review", candidate="1" * 40),
        )
        tess = kb.create_task(
            conn, title="tess", assignee="kwilo-tess", board="kwilo",
            governance_contract=_contract(profile="kwilo-tess", phase="tess-qa", candidate="2" * 40),
        )
        with pytest.raises(ValueError, match="exact task candidate"):
            kb.create_task(
                conn, title=phase, assignee="kwilo-forge", board="kwilo",
                parents=[sentinel, tess],
                governance_contract=_contract(phase=phase, candidate="3" * 40),
            )


def test_done_fail_does_not_satisfy_semantic_pass_gate(kwilo_home):
    candidate = "c" * 40
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn,
            title="Sentinel review",
            assignee="kwilo-sentinel",
            board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate),
        )
        child = kb.create_task(
            conn,
            title="Readiness",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(phase="implementation", candidate=candidate),
        )
        kb.link_tasks(
            conn,
            review,
            child,
            link_kind="evidence-gate",
            required_phase="sentinel-review",
            required_verdict="pass",
            require_candidate_match=True,
        )
        assert kb.complete_task(conn, review, semantic_evidence=_evidence(verdict="fail", candidate=candidate))
        assert kb.get_task(conn, review).status == "done"
        assert kb.get_task(conn, child).status == "todo"
        assert kb.dependency_satisfied(conn, review, child)[0] is False


def test_pass_on_wrong_candidate_does_not_satisfy_gate(kwilo_home):
    reviewed = "d" * 40
    current = "e" * 40
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn,
            title="Old candidate review",
            assignee="kwilo-sentinel",
            board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review", candidate=reviewed),
        )
        child = kb.create_task(
            conn,
            title="Current readiness",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(phase="implementation", candidate=current),
        )
        kb.link_tasks(conn, review, child, link_kind="evidence-gate", required_phase="sentinel-review", required_verdict="pass", require_candidate_match=True)
        kb.complete_task(conn, review, semantic_evidence=_evidence(candidate=reviewed))

        ok, reason = kb.dependency_satisfied(conn, review, child)

    assert ok is False
    assert reason == "candidate_mismatch"
    assert kb.get_task(conn, child).status == "todo"


def test_exact_candidate_pass_promotes_child(kwilo_home):
    candidate = "f" * 40
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn,
            title="Exact review",
            assignee="kwilo-sentinel",
            board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate),
        )
        child = kb.create_task(
            conn,
            title="Exact readiness",
            assignee="kwilo-forge",
            board="kwilo",
            governance_contract=_contract(phase="implementation", candidate=candidate),
        )
        kb.link_tasks(conn, review, child, link_kind="evidence-gate", required_phase="sentinel-review", required_verdict="pass", require_candidate_match=True)
        kb.complete_task(conn, review, semantic_evidence=_evidence(candidate=candidate))

        evidence = kb.list_task_evidence(conn, review)
        child_task = kb.get_task(conn, child)

    assert child_task.status == "ready"
    assert evidence[-1]["verdict"] == "pass"
    assert evidence[-1]["candidate_value"] == candidate


def test_semantic_completion_requires_valid_evidence_but_done_stays_lifecycle(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn,
            title="Review",
            assignee="kwilo-sentinel",
            board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )
        with pytest.raises(ValueError, match="semantic_evidence is required"):
            kb.complete_task(conn, review)
        assert kb.get_task(conn, review).status == "ready"

        malformed = _evidence()
        malformed["verdict"] = "approved"
        with pytest.raises(ValueError, match="verdict"):
            kb.complete_task(conn, review, semantic_evidence=malformed)
        assert kb.get_task(conn, review).status == "ready"

        assert kb.complete_task(conn, review, semantic_evidence=_evidence(verdict="changes-requested"))
        assert kb.get_task(conn, review).status == "done"
        assert kb.list_task_evidence(conn, review)[-1]["verdict"] == "changes-requested"


def test_legacy_completion_link_still_promotes_on_done(kwilo_home):
    kb.init_db(board="ordinary")
    with kb.connect(board="ordinary") as conn:
        parent = kb.create_task(conn, title="parent", board="ordinary")
        child = kb.create_task(conn, title="child", parents=[parent], board="ordinary")
        assert kb.get_task(conn, child).status == "todo"
        kb.complete_task(conn, parent)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.dependency_satisfied(conn, parent, child) == (True, "completion_satisfied")


def test_agent_tools_expose_and_forward_governance_contract_and_evidence(kwilo_home, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    created = json.loads(kt._handle_create({
        "title": "Tool-created review",
        "assignee": "kwilo-sentinel",
        "board": "kwilo",
        "governance_contract": _contract(profile="kwilo-sentinel", phase="sentinel-review"),
    }))
    assert created["ok"] is True

    completed = json.loads(kt._handle_complete({
        "task_id": created["task_id"],
        "board": "kwilo",
        "summary": "Independent review finished; lifecycle complete.",
        "semantic_evidence": _evidence(),
    }))
    assert completed["ok"] is True
    with kb.connect(board="kwilo") as conn:
        assert kb.list_task_evidence(conn, created["task_id"])[-1]["verdict"] == "pass"

    assert "governance_contract" in kt.KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert "semantic_evidence" in kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
    evidence_schema = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"][
        "semantic_evidence"
    ]
    assert evidence_schema["additionalProperties"] is False
    assert set(evidence_schema["required"]) == {
        "phase",
        "verdict",
        "checks",
        "blockers",
        "unresolved_acceptance_rows",
        "canonical_links",
    }
    assert set(evidence_schema["properties"]["checks"]["required"]) == {
        "executed",
        "passed_count",
        "failed_count",
        "skipped_count",
        "host_attested",
        "canonical_check_links",
    }
    assert evidence_schema["properties"]["verdict"]["enum"] == [
        "pass",
        "fail",
        "changes-requested",
        "blocked",
    ]
    for field in ("link_kind", "required_phase", "required_verdict", "require_candidate_match"):
        assert field in kt.KANBAN_LINK_SCHEMA["parameters"]["properties"]


@pytest.mark.parametrize("kind", ["uncommitted-tree-digest", "deployed-artifact-digest"])
@pytest.mark.parametrize("bad_digest", ["A" * 64, "a" * 63, "g" * 64])
def test_non_commit_candidate_digests_require_lowercase_64_hex(kwilo_home, kind, bad_digest):
    contract = _contract()
    contract["candidate_identity"] = {"kind": kind, "value": bad_digest, "path_set_digest": None}
    with kb.connect(board="kwilo") as conn, pytest.raises(ValueError, match="lowercase 64-character hex"):
        kb.create_task(
            conn, title="bad digest", assignee="kwilo-forge", board="kwilo",
            governance_contract=contract,
        )


def test_path_set_digest_requires_lowercase_64_hex(kwilo_home):
    contract = _contract()
    contract["candidate_identity"]["path_set_digest"] = "B" * 64
    with kb.connect(board="kwilo") as conn, pytest.raises(ValueError, match="path_set_digest.*lowercase 64-character hex"):
        kb.create_task(
            conn, title="bad path digest", assignee="kwilo-forge", board="kwilo",
            governance_contract=contract,
        )


def test_digest_bound_manifest_reference_is_loaded(kwilo_home):
    _, governance, manifest_path = kwilo_home
    activation = json.loads((governance / "activation.json").read_text(encoding="utf-8"))
    activation["capability_manifest"] = {
        "path": str(manifest_path),
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    (governance / "activation.json").write_text(json.dumps(activation), encoding="utf-8")
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(conn, title="digest-bound", assignee="kwilo-forge", board="kwilo", governance_contract=_contract())
    assert task_id


def test_activation_manifest_binding_requires_path_and_sha_object(kwilo_home):
    _, governance, manifest_path = kwilo_home
    activation_path = governance / "activation.json"
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    activation["capability_manifest"] = str(manifest_path)
    activation_path.write_text(json.dumps(activation), encoding="utf-8")

    with kb.connect(board="kwilo") as conn, pytest.raises(ValueError, match="path and sha256"):
        kb.create_task(
            conn, title="weak binding", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )


def test_missing_activation_blocks_queued_task_without_aborting_dispatch(kwilo_home, monkeypatch):
    _, governance, _ = kwilo_home
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    spawned = []
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn, title="queued before activation loss", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )
        (governance / "activation.json").unlink()

        result = kb.dispatch_once(
            conn, board="kwilo", spawn_fn=lambda task, workspace: spawned.append(task.id),
        )

        assert task_id not in spawned
        assert not any(item[0] == task_id for item in result.spawned)
        assert kb.get_task(conn, task_id).status == "blocked"
        event = kb.list_events(conn, task_id)[-1]
        assert event.kind == "capability_admission_failed"
        assert "activation is unavailable" in event.payload["reason"]


@pytest.mark.parametrize("status", ["ready", "review"])
def test_dry_run_dispatch_excludes_ineligible_task_without_mutation(
    kwilo_home, monkeypatch, status,
):
    _, governance, _ = kwilo_home
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn, title="preview admission", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        before_events = len(kb.list_events(conn, task_id))
        (governance / "activation.json").unlink()

        result = kb.dispatch_once(conn, board="kwilo", dry_run=True)

        assert not any(item[0] == task_id for item in result.spawned)
        assert kb.get_task(conn, task_id).status == status
        assert len(kb.list_events(conn, task_id)) == before_events


@pytest.mark.parametrize("status", ["ready", "review"])
def test_dry_run_dispatch_rechecks_dependencies_without_mutation(
    kwilo_home, monkeypatch, status,
):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect(board="kwilo") as conn:
        parent = kb.create_task(
            conn, title="pending parent", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(candidate="3" * 40),
        )
        child = kb.create_task(
            conn, title="dependent", assignee="kwilo-forge", board="kwilo",
            parents=[parent], governance_contract=_contract(candidate="3" * 40),
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, child))
        before_events = len(kb.list_events(conn, child))

        result = kb.dispatch_once(conn, board="kwilo", dry_run=True)

        assert not any(item[0] == child for item in result.spawned)
        assert kb.get_task(conn, child).status == status
        assert len(kb.list_events(conn, child)) == before_events


def test_activation_timestamp_must_be_timezone_aware(kwilo_home):
    _, governance, _ = kwilo_home
    activation_path = governance / "activation.json"
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    activation["activated_at"] = "2026-07-22T16:44:52"
    activation_path.write_text(json.dumps(activation), encoding="utf-8")

    with pytest.raises(ValueError, match="timezone-aware"):
        kb._kwilo.activation_epoch()


def test_idempotency_cannot_resolve_governed_request_to_historical_task(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind, idempotency_key) "
            "VALUES ('historical', 'old', 'ready', 0, 1, 'scratch', 'same-key')"
        )
        with pytest.raises(ValueError, match="historical|governance"):
            kb.create_task(
                conn, title="new", assignee="kwilo-forge", board="kwilo",
                idempotency_key="same-key", governance_contract=_contract(),
            )


def test_idempotency_revalidates_contract_and_mandatory_parents(kwilo_home):
    candidate = "6" * 40
    with kb.connect(board="kwilo") as conn:
        existing = kb.create_task(
            conn, title="existing", assignee="kwilo-forge", board="kwilo",
            idempotency_key="governed-key", governance_contract=_contract(candidate=candidate),
        )
        changed = _contract(candidate=candidate)
        changed["canonical_source"] = "https://github.com/Hello-Kwilo/Kwilo/issues/2"
        with pytest.raises(ValueError, match="contract"):
            kb.create_task(
                conn, title="retry", assignee="kwilo-forge", board="kwilo",
                idempotency_key="governed-key", governance_contract=changed,
            )

        with pytest.raises(
            ValueError,
            match="deterministic-verification.*sentinel-review",
        ):
            kb.create_task(
                conn, title="retry readiness", assignee="kwilo-forge", board="kwilo",
                idempotency_key="governed-key", parents=[],
                governance_contract=_contract(phase="merge-readiness", candidate=candidate),
            )

        assert kb.create_task(
            conn, title="retry", assignee="kwilo-forge", board="kwilo",
            idempotency_key="governed-key", governance_contract=_contract(candidate=candidate),
        ) == existing


def test_idempotency_rejects_different_mandatory_gate_parents(kwilo_home):
    candidate = "4" * 40
    with kb.connect(board="kwilo") as conn:
        deterministics = [
            kb.create_task(
                conn, title=f"deterministic {index}", assignee="kwilo-patch", board="kwilo",
                governance_contract=_contract(
                    profile="kwilo-patch", phase="deterministic-verification", candidate=candidate,
                ),
            )
            for index in range(2)
        ]
        sentinels = [
            kb.create_task(
                conn, title=f"sentinel {index}", assignee="kwilo-sentinel", board="kwilo",
                governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate),
            )
            for index in range(2)
        ]
        tesses = [
            kb.create_task(
                conn, title=f"tess {index}", assignee="kwilo-tess", board="kwilo",
                governance_contract=_contract(profile="kwilo-tess", phase="tess-qa", candidate=candidate),
            )
            for index in range(2)
        ]
        readiness_contract = _contract(phase="merge-readiness", candidate=candidate)
        existing = kb.create_task(
            conn, title="readiness", assignee="kwilo-forge", board="kwilo",
            parents=[deterministics[0], sentinels[0], tesses[0]], idempotency_key="readiness-key",
            governance_contract=readiness_contract,
        )

        with pytest.raises(ValueError, match="mandatory parent"):
            kb.create_task(
                conn, title="retry readiness", assignee="kwilo-forge", board="kwilo",
                parents=[deterministics[1], sentinels[1], tesses[1]], idempotency_key="readiness-key",
                governance_contract=readiness_contract,
            )

        assert kb.create_task(
            conn, title="retry readiness", assignee="kwilo-forge", board="kwilo",
            parents=[deterministics[0], sentinels[0], tesses[0]], idempotency_key="readiness-key",
            governance_contract=readiness_contract,
        ) == existing


def test_repositoryless_role_accepts_explicit_none(kwilo_home):
    home, governance, manifest_path = kwilo_home
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"]["kwilo-clara"] = {
        "role": "customer-operations-support", "lane": "operations",
        "repositories": [], "workspace_classes": ["owned-scratch"],
        "configured_cli_toolsets": ["kanban"], "configured_parent_skills": [],
        "configured_connectors": [], "configured_modes": ["implementation"],
        "connector_read_or_mutate_scope": {},
        "intended_side_effects": ["operations-recommendation"],
        "prohibited_side_effects": ["external-contact-without-authority"],
    }
    clara_config = {
        "platform_toolsets": {"cli": ["kanban"]},
        "mcp_servers": {},
    }
    clara_config_path = home / "profiles" / "kwilo-clara" / "config.yaml"
    clara_config_path.parent.mkdir(parents=True, exist_ok=True)
    clara_config_path.write_text(json.dumps(clara_config), encoding="utf-8")
    _rewrite_manifest(governance, manifest_path, manifest)
    contract = _contract(phase="intake", candidate=None)
    contract.update({
        "role": "customer-operations-support", "lane": "operations", "repository": "none",
        "project_id": None, "workspace_class": "owned-scratch", "required_toolsets": ["kanban"],
        "required_parent_skills": [], "required_connectors": [],
        "connector_read_or_mutate_scope": {}, "allowed_side_effects": ["operations-recommendation"],
        "prohibited_actions": ["external-contact-without-authority"],
        "base_revision": "not-applicable", "candidate_identity": None,
    })
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(conn, title="operations intake", assignee="kwilo-clara", board="kwilo", governance_contract=contract)
    assert task_id


def test_public_api_rejects_semantically_impossible_pass_evidence(kwilo_home):
    """The audit probe must not turn a contradictory PASS into gate evidence."""
    candidate = "1" * 40
    malformed = _evidence(candidate=candidate)
    malformed["checks"].update({"failed_count": 1, "host_attested": False})
    malformed["blockers"] = ["critical vulnerability"]
    malformed["unresolved_acceptance_rows"] = ["AC-1"]

    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn, title="adversarial review", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate,
            ),
        )

        with pytest.raises(ValueError, match="semantic_evidence|PASS evidence"):
            kb.complete_task(conn, review, semantic_evidence=malformed)

        assert kb.get_task(conn, review).status == "ready"
        assert kb.list_task_evidence(conn, review) == []


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda item: item.update({"unexpected": True}), "unknown field"),
        (lambda item: item.update({"blockers": "none"}), "blockers must be a list"),
        (lambda item: item.update({"blockers": ["x"] * 257}), "exceeds 256 items"),
        (lambda item: item.update({"environment": "host=local"}), "environment must be an object"),
        (lambda item: item["checks"].update({"passed_count": 2}), "counts must match"),
        (lambda item: item["checks"].update({"host_attested": "yes"}), "host_attested"),
        (lambda item: item.update({"canonical_links": ["not-a-url"]}), "absolute HTTP"),
        (lambda item: item["checks"].update({"executed": [], "passed_count": 0}), "PASS evidence"),
        (lambda item: item.update({"blockers": ["critical vulnerability"]}), "PASS evidence"),
        (lambda item: item.update({"unresolved_acceptance_rows": ["AC-1"]}), "PASS evidence"),
        (lambda item: item.update({"canonical_links": []}), "PASS evidence"),
    ],
)
def test_evidence_schema_is_strict_bounded_and_pass_safe(kwilo_home, mutation, match):
    evidence = _evidence()
    mutation(evidence)
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn, title="strict evidence", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )
        with pytest.raises(ValueError, match=match):
            kb.complete_task(conn, review, semantic_evidence=evidence)
        assert kb.get_task(conn, review).status == "ready"


def test_non_pass_evidence_allows_failures_but_keeps_counts_consistent(kwilo_home):
    evidence = _evidence(verdict="fail")
    evidence["checks"].update({"passed_count": 0, "failed_count": 1})
    evidence["blockers"] = ["critical vulnerability"]
    evidence["unresolved_acceptance_rows"] = ["AC-1"]
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn, title="failed review", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )
        assert kb.complete_task(conn, review, semantic_evidence=evidence)
        assert kb.get_task(conn, review).status == "done"
        assert kb.list_task_evidence(conn, review)[-1]["verdict"] == "fail"


def test_pass_allows_only_explicit_policy_safe_nonrequired_skips(kwilo_home):
    evidence = _evidence()
    evidence["checks"].update({
        "executed": ["required check", "optional unavailable check"],
        "skipped_count": 1,
        "skipped_required_count": 0,
        "skipped_policy_safe": True,
    })
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn, title="policy-safe skip", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )
        assert kb.complete_task(conn, review, semantic_evidence=evidence)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("supersedes_evidence_ids", [999999], "nonexistent evidence"),
        ("invalidates_evidence_ids", [0], "positive integer"),
        ("supersedes_evidence_ids", [1, 1], "unique IDs"),
    ],
)
def test_public_api_rejects_malformed_or_nonexistent_evidence_references(
    kwilo_home, field, value, match,
):
    evidence = _evidence(verdict="fail")
    evidence[field] = value
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn, title="bad reference", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )
        with pytest.raises(ValueError, match=match):
            kb.complete_task(conn, review, semantic_evidence=evidence)


def test_public_api_rejects_cross_task_and_overlapping_evidence_references(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        first = kb.create_task(
            conn, title="first review", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )
        second = kb.create_task(
            conn, title="second review", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )
        assert kb.complete_task(conn, first, semantic_evidence=_evidence(verdict="fail"))
        evidence_id = kb.list_task_evidence(conn, first)[0]["id"]

        cross_task = _evidence(verdict="fail")
        cross_task["supersedes_evidence_ids"] = [evidence_id]
        with pytest.raises(ValueError, match="cross-task"):
            kb.complete_task(conn, second, semantic_evidence=cross_task)

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (first,))
        overlap = _evidence(verdict="fail")
        overlap["supersedes_evidence_ids"] = [evidence_id]
        overlap["invalidates_evidence_ids"] = [evidence_id]
        with pytest.raises(ValueError, match="must not overlap"):
            kb.complete_task(conn, first, semantic_evidence=overlap)


def test_dependency_revalidates_directly_injected_pass_and_fails_closed(kwilo_home):
    """A forged verdict column cannot bypass the semantic dependency gate."""
    candidate = "2" * 40
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn, title="forged parent", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate,
            ),
        )
        child = kb.create_task(
            conn, title="gated child", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(candidate=candidate),
        )
        kb.link_tasks(
            conn, review, child, link_kind="evidence-gate",
            required_phase="sentinel-review", required_verdict="pass",
            require_candidate_match=True,
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (review,))
        conn.execute(
            """INSERT INTO task_evidence (
                task_id, phase, verdict, candidate_kind, candidate_value,
                candidate_paths_digest, checks_json, blockers_json,
                unresolved_acceptance_json, canonical_links_json,
                supersedes_json, invalidates_json, created_at
            ) VALUES (?, 'sentinel-review', 'pass', 'commit-sha', ?, NULL,
                      ?, ?, ?, ?, '[]', '[]', 1)""",
            (
                review, candidate,
                json.dumps({
                    "executed": ["fake"], "passed_count": 0, "failed_count": 1,
                    "skipped_count": 0, "host_attested": False,
                    "canonical_check_links": [],
                }),
                json.dumps(["critical vulnerability"]), json.dumps(["AC-1"]),
                json.dumps(["https://github.com/Hello-Kwilo/Kwilo/pull/1"]),
            ),
        )

        assert kb.dependency_satisfied(conn, review, child) == (False, "malformed_evidence")
        assert kb.get_task(conn, child).status == "todo"


def test_valid_supersession_and_invalidation_remain_context_bound(kwilo_home):
    candidate = "4" * 40
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn, title="evidence revisions", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate,
            ),
        )
        child = kb.create_task(
            conn, title="evidence consumer", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(candidate=candidate),
        )
        kb.link_tasks(
            conn, review, child, link_kind="evidence-gate",
            required_phase="sentinel-review", required_verdict="pass",
            require_candidate_match=True,
        )
        assert kb.complete_task(conn, review, semantic_evidence=_evidence(candidate=candidate))
        first_id = kb.list_task_evidence(conn, review)[0]["id"]

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (review,))
        invalidation = _evidence(verdict="fail", candidate=candidate)
        invalidation["invalidates_evidence_ids"] = [first_id]
        assert kb.complete_task(conn, review, semantic_evidence=invalidation)
        second_id = kb.list_task_evidence(conn, review)[-1]["id"]
        assert kb.dependency_satisfied(conn, review, child) == (False, "verdict_mismatch")

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (review,))
        replacement = _evidence(candidate=candidate)
        replacement["supersedes_evidence_ids"] = [second_id]
        assert kb.complete_task(conn, review, semantic_evidence=replacement)
        assert kb.dependency_satisfied(conn, review, child) == (
            True, "evidence_gate_satisfied",
        )


def test_repeated_active_blocker_without_measurable_progress_fails_closed(kwilo_home):
    candidate = "5" * 40
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn, title="stalled correction", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate,
            ),
        )
        first = _evidence(verdict="fail", candidate=candidate)
        first["checks"].update({"passed_count": 0, "failed_count": 1})
        first["blockers"] = ["tenant boundary still bypassable"]
        first["unresolved_acceptance_rows"] = ["AC-SEC-1"]
        assert kb.complete_task(conn, review, semantic_evidence=first)

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (review,))
        repeated = _evidence(verdict="fail", candidate=candidate)
        repeated["checks"].update({"passed_count": 0, "failed_count": 1})
        repeated["blockers"] = ["tenant boundary still bypassable"]
        repeated["unresolved_acceptance_rows"] = ["AC-SEC-1"]

        with pytest.raises(ValueError, match="without measurable progress.*kanban_block"):
            kb.complete_task(conn, review, semantic_evidence=repeated)
        assert kb.get_task(conn, review).status == "ready"


def test_repeated_blocker_allows_deterministically_measurable_progress(kwilo_home):
    candidate = "6" * 40
    with kb.connect(board="kwilo") as conn:
        review = kb.create_task(
            conn, title="progressing correction", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate,
            ),
        )
        first = _evidence(verdict="fail", candidate=candidate)
        first["checks"].update({
            "executed": ["tenant isolation regression", "cross-tenant replay"],
            "passed_count": 0,
            "failed_count": 2,
        })
        first["blockers"] = ["tenant boundary still bypassable"]
        first["unresolved_acceptance_rows"] = ["AC-SEC-1"]
        assert kb.complete_task(conn, review, semantic_evidence=first)

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (review,))
        progress = _evidence(verdict="fail", candidate=candidate)
        progress["checks"].update({
            "executed": ["tenant isolation regression", "cross-tenant replay"],
            "passed_count": 1,
            "failed_count": 1,
        })
        progress["blockers"] = ["tenant boundary still bypassable"]
        progress["unresolved_acceptance_rows"] = ["AC-SEC-1"]

        assert kb.complete_task(conn, review, semantic_evidence=progress)


def test_readiness_gates_come_from_digest_bound_semantic_policy(kwilo_home):
    candidate = "3" * 40
    with kb.connect(board="kwilo") as conn:
        deterministic = kb.create_task(
            conn, title="deterministic", assignee="kwilo-patch", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-patch", phase="deterministic-verification", candidate=candidate,
            ),
        )
        sentinel = kb.create_task(
            conn, title="sentinel", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate,
            ),
        )
        tess = kb.create_task(
            conn, title="tess", assignee="kwilo-tess", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-tess", phase="tess-qa", candidate=candidate,
            ),
        )

        with pytest.raises(ValueError, match="deterministic-verification"):
            kb.create_task(
                conn, title="two-gate bypass", assignee="kwilo-forge", board="kwilo",
                parents=[sentinel, tess],
                governance_contract=_contract(phase="release-readiness", candidate=candidate),
            )

        readiness = kb.create_task(
            conn, title="three-gate readiness", assignee="kwilo-forge", board="kwilo",
            parents=[deterministic, sentinel, tess],
            governance_contract=_contract(phase="release-readiness", candidate=candidate),
        )
        phases = {
            row["required_phase"]
            for row in conn.execute(
                "SELECT required_phase FROM task_links "
                "WHERE child_id = ? AND required_phase IS NOT NULL",
                (readiness,),
            )
        }

    assert phases == {"deterministic-verification", "sentinel-review"}


def test_governed_reserved_phase_cannot_be_reassigned_to_unauthorised_producer(kwilo_home):
    with kb.connect(board="kwilo") as conn:
        sentinel = kb.create_task(
            conn, title="reserved producer", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(profile="kwilo-sentinel", phase="sentinel-review"),
        )

        with pytest.raises(ValueError, match="kwilo-sentinel"):
            kb.assign_task(conn, sentinel, "kwilo-forge")

        assert kb.get_task(conn, sentinel).assignee == "kwilo-sentinel"


def test_evidence_persists_and_defensively_validates_immutable_producer_context(kwilo_home):
    candidate = "7" * 40
    with kb.connect(board="kwilo") as conn:
        sentinel = kb.create_task(
            conn, title="producer-bound evidence", assignee="kwilo-sentinel", board="kwilo",
            governance_contract=_contract(
                profile="kwilo-sentinel", phase="sentinel-review", candidate=candidate,
            ),
        )
        child = kb.create_task(
            conn, title="consumer", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(candidate=candidate),
        )
        kb.link_tasks(
            conn, sentinel, child, link_kind="evidence-gate",
            required_phase="sentinel-review", required_verdict="pass",
            require_candidate_match=True,
        )
        assert kb.complete_task(
            conn, sentinel, semantic_evidence=_evidence(candidate=candidate),
        )
        stored = kb.list_task_evidence(conn, sentinel)[-1]
        assert stored["producer_profile"] == "kwilo-sentinel"
        assert stored["producer_task_id"] == sentinel
        assert kb.dependency_satisfied(conn, sentinel, child)[0] is True

        conn.execute(
            "UPDATE task_evidence SET producer_profile = 'kwilo-forge' WHERE id = ?",
            (stored["id"],),
        )
        assert kb.dependency_satisfied(conn, sentinel, child) == (
            False, "malformed_evidence",
        )


@pytest.mark.parametrize("forced_status", ["ready", "blocked"])
def test_manual_readiness_completion_cannot_self_pass_failed_mandatory_parents(
    kwilo_home, forced_status,
):
    candidate = "8" * 40
    with kb.connect(board="kwilo") as conn:
        gates = []
        for profile, phase in (
            ("kwilo-patch", "deterministic-verification"),
            ("kwilo-sentinel", "sentinel-review"),
            ("kwilo-tess", "tess-qa"),
        ):
            gate = kb.create_task(
                conn, title=phase, assignee=profile, board="kwilo",
                governance_contract=_contract(
                    profile=profile, phase=phase, candidate=candidate,
                ),
            )
            failed = _evidence(phase=phase, verdict="fail", candidate=candidate)
            failed["checks"].update({"passed_count": 0, "failed_count": 1})
            failed["blockers"] = [f"{phase} failed"]
            assert kb.complete_task(conn, gate, semantic_evidence=failed)
            gates.append(gate)

        readiness = kb.create_task(
            conn, title="release readiness", assignee="kwilo-forge", board="kwilo",
            parents=gates,
            governance_contract=_contract(phase="release-readiness", candidate=candidate),
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (forced_status, readiness))

        with pytest.raises(ValueError, match="semantic dependencies|verdict_mismatch"):
            kb.complete_task(
                conn, readiness,
                semantic_evidence=_evidence(
                    phase="release-readiness", candidate=candidate,
                ),
            )

        assert kb.get_task(conn, readiness).status == forced_status
        assert kb.list_task_evidence(conn, readiness) == []


def test_completion_revalidates_current_capability_contract(kwilo_home):
    _, governance, manifest_path = kwilo_home
    with kb.connect(board="kwilo") as conn:
        task_id = kb.create_task(
            conn, title="completion capability drift", assignee="kwilo-forge", board="kwilo",
            governance_contract=_contract(),
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["profiles"]["kwilo-forge"]["configured_cli_toolsets"].remove("terminal")
        _rewrite_manifest(governance, manifest_path, manifest)

        with pytest.raises(ValueError, match="terminal"):
            kb.complete_task(conn, task_id)

        assert kb.get_task(conn, task_id).status == "ready"
