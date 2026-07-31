"""Boucle Linear -> codeurs : selection, dispatch et closeout, sans reseau.

Le transport Linear est remplace par une fonction qui repond depuis un etat en
memoire et enregistre chaque mutation, si bien que ces tests prouvent aussi ce
que la boucle ecrit dans Linear — pas seulement ce qu'elle en lit.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "linear_loop", REPO_ROOT / "scripts" / "linear_loop.py"
)
linear_loop = importlib.util.module_from_spec(_spec)
# @dataclass resout ses annotations via sys.modules : sans cet enregistrement,
# le chargement par chemin echoue des la premiere dataclass du module.
sys.modules["linear_loop"] = linear_loop
_spec.loader.exec_module(linear_loop)


TEAM = "HER"


def make_issue(key, *, priority=2, labels=(linear_loop.LABEL_READY,), state="Todo",
               state_type="unstarted", created_at="2026-01-01T00:00:00Z", title=None,
               description=""):
    return linear_loop.Issue(
        key=key,
        id=f"uuid-{key}",
        title=title or f"titre {key}",
        description=description,
        priority=priority,
        labels=frozenset(labels),
        state_name=state,
        state_type=state_type,
        url=f"https://linear.app/x/issue/{key}",
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_only_agent_ready_issues_are_eligible():
    assert linear_loop.is_eligible(make_issue("HER-1"))
    assert not linear_loop.is_eligible(make_issue("HER-2", labels=()))


@pytest.mark.parametrize("blocking", [
    linear_loop.LABEL_BUILDING,
    linear_loop.LABEL_BLOCKED,
    linear_loop.LABEL_REVIEW,
])
def test_issue_already_engaged_is_not_reselected(blocking):
    issue = make_issue("HER-3", labels=(linear_loop.LABEL_READY, blocking))
    assert not linear_loop.is_eligible(issue)


def test_completed_issue_is_not_eligible():
    done = make_issue("HER-4", state="Done", state_type="completed")
    assert not linear_loop.is_eligible(done)


def test_urgent_wins_then_oldest_first():
    issues = [
        make_issue("HER-10", priority=2, created_at="2026-01-01T00:00:00Z"),
        make_issue("HER-11", priority=1, created_at="2026-05-01T00:00:00Z"),
        make_issue("HER-12", priority=1, created_at="2026-02-01T00:00:00Z"),
    ]
    selected = linear_loop.select_missions(issues, capacity=3)
    assert [i.key for i in selected] == ["HER-12", "HER-11", "HER-10"]


def test_priority_none_ranks_last_not_first():
    issues = [make_issue("HER-20", priority=0), make_issue("HER-21", priority=4)]
    assert [i.key for i in linear_loop.select_missions(issues, capacity=2)] == [
        "HER-21",
        "HER-20",
    ]


def test_capacity_bounds_the_selection():
    issues = [make_issue(f"HER-{n}", priority=1) for n in range(30, 35)]
    assert len(linear_loop.select_missions(issues, capacity=2)) == 2
    assert linear_loop.select_missions(issues, capacity=0) == []


def test_issue_already_taken_by_a_card_is_skipped():
    issues = [make_issue("HER-40", priority=1), make_issue("HER-41", priority=2)]
    selected = linear_loop.select_missions(issues, capacity=2, busy_keys={"HER-40"})
    assert [i.key for i in selected] == ["HER-41"]


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------


def test_branch_name_is_derived_from_key_and_title():
    issue = make_issue("HER-50", title="Réparer le cron sémantique !")
    branch = linear_loop.branch_name_for(issue)
    assert branch.startswith("agent/her-50-")
    assert " " not in branch and "!" not in branch


def test_repo_directive_overrides_the_default():
    issue = make_issue("HER-51", description="Contexte\nRepo: /tmp/autre-depot\nSuite")
    assert linear_loop.repo_for_issue(issue, "/defaut") == "/tmp/autre-depot"
    assert linear_loop.repo_for_issue(make_issue("HER-52"), "/defaut") == "/defaut"


def test_brief_carries_the_no_merge_rule_and_the_issue_text():
    issue = make_issue("HER-53", description="AC-1 : le tick est silencieux")
    brief = linear_loop.build_brief(issue, branch="agent/her-53-x", repo="/depot")
    assert "AC-1 : le tick est silencieux" in brief
    assert "Aucun push, aucune PR, aucun merge" in brief
    assert issue.url in brief


def test_linear_priority_maps_to_kanban_priority_order():
    urgent = linear_loop._kanban_priority(make_issue("HER-60", priority=1))
    low = linear_loop._kanban_priority(make_issue("HER-61", priority=4))
    none = linear_loop._kanban_priority(make_issue("HER-62", priority=0))
    assert urgent > low > none


def test_messages_are_written_for_jean_not_for_a_machine():
    """Les notifications arrivent sur son telephone : pas de nom de profil brut,
    pas de titre a rallonge, pas de pave technique."""
    assert linear_loop.coder_label("hermes-code-a") == "Code A"
    assert linear_loop.coder_label("hermes-code-b") == "Code B"
    assert linear_loop.coder_label(None) == "un codeur"

    long_title = "x" * 200
    assert len(linear_loop.short_title(long_title)) <= 62

    verbeux = "ligne\n\nautre   ligne " + "z" * 400
    resume = linear_loop.quote(verbeux)
    assert len(resume) <= 200 and "\n" not in resume and resume.endswith("…")


def test_issue_key_is_read_back_from_a_card_title():
    assert linear_loop.issue_key_of_title("HER-118 — bootstrap owner") == "HER-118"
    assert linear_loop.issue_key_of_title("carte sans issue") is None


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeLinear:
    """Linear en memoire : repond aux requetes, enregistre les mutations."""

    def __init__(self, issues):
        self.issues = {issue.key: issue for issue in issues}
        self.comments: list[tuple[str, str]] = []
        #: Commentaires deja presents sur Linear avant le tick (par issue id),
        #: pour simuler un historique que la boucle n'a pas ecrit elle-meme.
        self.preexisting: dict[str, list[str]] = {}
        self.updates: list[dict] = []
        self.labels = {
            name: f"label-{name}"
            for name in (
                linear_loop.LABEL_READY,
                linear_loop.LABEL_BUILDING,
                linear_loop.LABEL_BLOCKED,
                linear_loop.LABEL_REVIEW,
            )
        }
        self.states = {
            linear_loop.STATE_BUILDING: "state-building",
            linear_loop.STATE_REVIEW: "state-review",
            linear_loop.STATE_DONE: "state-done",
        }

    def issue_comment_bodies(self, issue_id):
        return self.preexisting.get(issue_id, []) + [
            body for posted_id, body in self.comments if posted_id == issue_id
        ]

    def __call__(self, payload):
        query = payload["query"]
        variables = payload.get("variables") or {}
        if "commentCreate" in query:
            self.comments.append((variables["input"]["issueId"], variables["input"]["body"]))
            return {"data": {"commentCreate": {"success": True}}}
        if "comments(" in query:
            bodies = self.issue_comment_bodies(variables["id"])
            return {"data": {"issue": {"comments": {
                "nodes": [{"body": body} for body in bodies],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}
        if "issueUpdate" in query:
            self.updates.append({"id": variables["id"], **variables["input"]})
            return {"data": {"issueUpdate": {"success": True}}}
        if "issues(" in query:
            return {"data": {"team": {
                "id": "team-her",
                "labels": {"nodes": [{"id": v, "name": k} for k, v in self.labels.items()]},
                "states": {"nodes": [{"id": v, "name": k, "type": "started"}
                                     for k, v in self.states.items()]},
                "issues": {"nodes": [self._node(i) for i in self.issues.values()]},
            }}}
        raise AssertionError(f"requete non prevue: {query[:80]}")

    @staticmethod
    def _node(issue):
        return {
            "id": issue.id,
            "identifier": issue.key,
            "title": issue.title,
            "description": issue.description,
            "priority": issue.priority,
            "url": issue.url,
            "createdAt": issue.created_at,
            "state": {"name": issue.state_name, "type": issue.state_type},
            "labels": {"nodes": [{"name": n} for n in sorted(issue.labels)]},
        }


class FakeTask:
    def __init__(self, task_id, title, assignee, status, **kw):
        self.id = task_id
        self.title = title
        self.assignee = assignee
        self.status = status
        self.created_by = kw.get("created_by", linear_loop.LOOP_AUTHOR)
        self.workspace_path = kw.get("workspace_path")
        self.branch_name = kw.get("branch_name")
        self.result = kw.get("result")


class FakeKanban:
    """Juste ce que la boucle utilise du kanban, avec un journal des appels."""

    def __init__(self, tasks=(), summaries=None):
        self.tasks = list(tasks)
        self.created: list[dict] = []
        self.archived: list[str] = []
        self.summaries = summaries or {}

    def latest_summary(self, conn, task_id):
        return self.summaries.get(task_id)

    def connect(self):
        kanban = self

        class _Ctx:
            def __enter__(self):
                return kanban

            def __exit__(self, *exc):
                return False

        return _Ctx()

    def list_tasks(self, conn, *, status=None, include_archived=False, **kw):
        rows = self.tasks if include_archived else [
            t for t in self.tasks if t.status != "archived"
        ]
        return [t for t in rows if status is None or t.status == status]

    def create_task(self, conn, **kw):
        task_id = f"t_{len(self.created)}"
        self.created.append({"id": task_id, **kw})
        self.tasks.append(
            FakeTask(task_id, kw["title"], kw["assignee"], "ready",
                     created_by=kw.get("created_by"),
                     branch_name=kw.get("branch_name"))
        )
        return task_id

    def archive_task(self, conn, task_id):
        self.archived.append(task_id)
        for task in self.tasks:
            if task.id == task_id:
                task.status = "archived"
        return True


def make_base_repo(path):
    """Un depot git minimal sur `main`, pour que les worktrees soient reels."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return path


def make_config(tmp_path, **kw):
    repo = kw.pop("repo", None) or str(make_base_repo(tmp_path / "depot"))
    options = {
        "team": TEAM,
        "coders": ("hermes-code-a", "hermes-code-b"),
        "repo": repo,
        "hermes_home": tmp_path,
        "runtime_root": repo,
        "worktrees_root": str(tmp_path / "worktrees"),
        "apply": True,
        "min_free_disk": 0,
    }
    options.update(kw)
    return linear_loop.LoopConfig(**options)


# ---------------------------------------------------------------------------
# Tick — alimentation
# ---------------------------------------------------------------------------


def test_tick_gives_one_mission_to_each_free_coder(tmp_path):
    linear = FakeLinear([
        make_issue("HER-100", priority=1, created_at="2026-01-01T00:00:00Z"),
        make_issue("HER-101", priority=1, created_at="2026-02-01T00:00:00Z"),
        make_issue("HER-102", priority=3),
    ])
    kanban = FakeKanban()
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert [m["issue"] for m in report.started] == ["HER-100", "HER-101"]
    assert [c["assignee"] for c in kanban.created] == ["hermes-code-a", "hermes-code-b"]
    assert kanban.created[0]["title"].startswith("HER-100 —")


def test_mission_worktree_lands_where_the_coder_may_write(tmp_path):
    """Le worktree doit vivre sous la racine autorisee, pas dans le runtime.

    HER-112 s'est bloquee la-dessus : le defaut du kanban place le worktree dans
    le depot lui-meme, hors du HERMES_WRITE_SAFE_ROOT des profils Code.
    """
    linear = FakeLinear([make_issue("HER-104", priority=1)])
    kanban = FakeKanban()
    config = make_config(tmp_path)
    linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    created = kanban.created[0]
    workspace = Path(created["workspace_path"])
    assert created["workspace_kind"] == "dir"
    assert workspace.parent == Path(config.worktrees_root)
    assert workspace.is_dir() and (workspace / "README.md").exists()
    assert not Path(config.repo, ".worktrees").exists()


def test_mission_carries_a_runtime_cap(tmp_path):
    """Un worker zele peut retenir son codeur apres avoir fini : on borne."""
    linear = FakeLinear([make_issue("HER-105", priority=1)])
    kanban = FakeKanban()
    linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert kanban.created[0]["max_runtime_seconds"] == linear_loop.MISSION_MAX_RUNTIME_SECONDS


def test_brief_forbids_self_review_after_completion(tmp_path):
    issue = make_issue("HER-106")
    brief = linear_loop.build_brief(issue, branch="agent/x", repo="/depot")
    assert "pas d'auto-revue" in brief


def test_brief_keeps_the_worker_inside_its_own_copy(tmp_path):
    """HER-112 a lu le code du runtime, s'est fait refuser, et a cru a une panne.

    Le depot est complet dans le worktree : lire ailleurs est inutile et refuse.
    """
    brief = linear_loop.build_brief(
        make_issue("HER-107"), branch="agent/x", repo="/worktrees/agent-her-107"
    )
    assert "Travaille exclusivement dans `/worktrees/agent-her-107`" in brief
    assert "n'est pas une panne" in brief
    assert "ne bloque pas pour cette raison" in brief


def test_tick_marks_the_issue_building_in_linear(tmp_path):
    linear = FakeLinear([make_issue("HER-110", priority=1)])
    linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), FakeKanban()
    )

    assert len(linear.comments) == 1
    assert "Prise en charge par `hermes-code-a`" in linear.comments[0][1]
    update = linear.updates[0]
    assert update["stateId"] == "state-building"
    assert linear.labels[linear_loop.LABEL_BUILDING] in update["labelIds"]
    assert linear.labels[linear_loop.LABEL_READY] in update["labelIds"]


def test_busy_coder_gets_no_second_mission(tmp_path):
    running = FakeTask("t_old", "HER-90 — en cours", "hermes-code-a", "running")
    linear = FakeLinear([make_issue("HER-120", priority=1), make_issue("HER-121", priority=1)])
    kanban = FakeKanban([running])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert [c["assignee"] for c in kanban.created] == ["hermes-code-b"]
    assert len(report.started) == 1


def test_a_blocked_card_does_not_freeze_its_coder(tmp_path):
    """Un blocage attend Jean ; il ne doit pas retirer un codeur du service.

    Les deux codeurs traînent des cartes bloquées héritées : les compter comme
    occupation les mettrait hors service définitivement.
    """
    stuck = FakeTask("t_stuck", "HER-80 — bloquée depuis des jours",
                     "hermes-code-a", "blocked")
    linear = FakeLinear([make_issue("HER-160", priority=1)])
    kanban = FakeKanban([stuck])
    linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert [c["assignee"] for c in kanban.created] == ["hermes-code-a"]


def test_a_blocked_card_still_holds_its_own_issue(tmp_path):
    """En revanche l'issue bloquée reste engagée : on ne la redistribue pas."""
    stuck = FakeTask("t_stuck", "HER-161 — bloquée", "hermes-code-a", "blocked")
    linear = FakeLinear([make_issue("HER-161", priority=1)])
    kanban = FakeKanban([stuck])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert kanban.created == []
    assert report.started == []


def test_no_mission_starts_when_the_disk_is_nearly_full(tmp_path):
    linear = FakeLinear([make_issue("HER-130", priority=1)])
    kanban = FakeKanban()
    config = make_config(tmp_path, min_free_disk=10**15)
    report = linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert kanban.created == []
    assert "disk" in report.skipped
    assert "il ne reste que" in report.render()


def test_dry_run_writes_nothing(tmp_path):
    linear = FakeLinear([make_issue("HER-140", priority=1)])
    kanban = FakeKanban()
    report = linear_loop.run_tick(
        make_config(tmp_path, apply=False),
        linear_loop.LinearClient("k", transport=linear),
        kanban,
    )

    assert report.started and kanban.created == [] and linear.comments == []


def test_empty_backlog_stays_silent(tmp_path):
    linear = FakeLinear([make_issue("HER-150", labels=())])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), FakeKanban()
    )

    assert report.render() == ""


# ---------------------------------------------------------------------------
# Tick — closeout
# ---------------------------------------------------------------------------


def test_closeout_comment_contains_the_exact_branch_pr_sha_and_verdict():
    comment = linear_loop.closeout_comment(
        key="HER-96",
        assignee="hermes-code-a",
        branch="agent/her-96-truth",
        pull_request="https://github.com/nousresearch/hermes-agent/pull/96",
        candidate_sha="abcdef123456",
        verdict="APPROVE",
        summary="Tests verts.",
        marker="linear-loop:closeout:HER-96:abcdef123456:APPROVE",
    )

    assert "Triplet de closeout : {branche/PR: `agent/her-96-truth` / `https://github.com/nousresearch/hermes-agent/pull/96`, SHA candidat: `abcdef123456`, verdict: `APPROVE`}" in comment
    assert "<!-- linear-loop:closeout:HER-96:abcdef123456:APPROVE -->" in comment


def test_new_candidate_sha_refuses_stale_verdict_and_does_not_ask_for_merge(tmp_path):
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_stale", "HER-206 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = FakeLinear([make_issue("HER-206", labels=(linear_loop.LABEL_BUILDING,))])
    config = make_config(tmp_path)
    state_path = linear_loop.mission_state_path(config, "HER-206")
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "branch": "agent/her-200-x",
        "verdict": "APPROVE",
        # SHA complet et bien formé, mais qui n'est pas le candidat actuel.
        "for_sha": "a" * 40,
    }))

    report = linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), FakeKanban([done])
    )

    assert report.skipped["HER-206"] == "verdict stale"
    assert "À toi de jouer" not in report.render()
    assert linear.comments == []


def test_replaying_a_closeout_tick_does_not_duplicate_its_linear_comment(tmp_path):
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_once", "HER-207 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = FakeLinear([make_issue("HER-207", labels=(linear_loop.LABEL_BUILDING,))])
    config = make_config(tmp_path)
    kanban = FakeKanban([done])

    linear_loop.run_tick(config, linear_loop.LinearClient("k", transport=linear), kanban)
    done.status = "done"  # simule un retry apres echec d'archivage.
    linear_loop.run_tick(config, linear_loop.LinearClient("k", transport=linear), kanban)

    assert len(linear.comments) == 1
    assert "<!-- linear-loop:closeout:HER-207:" in linear.comments[0][1]


def repo_head(repo):
    """Le SHA complet (40 hex) du HEAD d'un worktree de test."""
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_full_sha_verdict_bound_to_current_head_authorizes_closeout(tmp_path):
    """Un verdict lié au SHA complet du candidat courant doit passer le closeout."""
    repo = build_finished_repo(tmp_path)
    head = repo_head(repo)
    assert len(head) == 40
    done = FakeTask("t_ok", "HER-220 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = FakeLinear([make_issue("HER-220", labels=(linear_loop.LABEL_BUILDING,))])
    config = make_config(tmp_path)
    state_path = linear_loop.mission_state_path(config, "HER-220")
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"verdict": "APPROVE", "for_sha": head}))
    kanban = FakeKanban([done])

    report = linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert "HER-220" not in report.skipped
    assert len(linear.comments) == 1
    assert f"SHA candidat: `{head}`" in linear.comments[0][1]
    assert f"<!-- linear-loop:closeout:HER-220:{head}:APPROVE -->" in linear.comments[0][1]
    assert kanban.archived == ["t_ok"]


def test_short_prefix_of_current_head_cannot_authorize_closeout(tmp_path):
    """Un préfixe court (même exact) est ambigu : il ne lie aucun verdict."""
    repo = build_finished_repo(tmp_path)
    head = repo_head(repo)
    done = FakeTask("t_short", "HER-221 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = FakeLinear([make_issue("HER-221", labels=(linear_loop.LABEL_BUILDING,))])
    config = make_config(tmp_path)
    state_path = linear_loop.mission_state_path(config, "HER-221")
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"verdict": "APPROVE", "for_sha": head[:7]}))
    kanban = FakeKanban([done])

    report = linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert report.skipped["HER-221"] == "verdict unbound"
    assert linear.comments == []
    assert kanban.archived == []


def test_unbound_approve_verdict_fails_closed(tmp_path):
    """Un APPROVE sans for_sha ne se rattache à rien : aucun writeback."""
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_unbound", "HER-222 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = FakeLinear([make_issue("HER-222", labels=(linear_loop.LABEL_BUILDING,))])
    config = make_config(tmp_path)
    state_path = linear_loop.mission_state_path(config, "HER-222")
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"verdict": "APPROVE"}))
    kanban = FakeKanban([done])

    report = linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert report.skipped["HER-222"] == "verdict unbound"
    assert linear.comments == []
    assert kanban.archived == []


def test_record_verdict_is_the_producer_of_the_binding_end_to_end(tmp_path):
    """record_mission -> record_verdict -> tick : le contrat vit hors des tests."""
    config = make_config(tmp_path)
    repo = build_finished_repo(tmp_path)
    head = repo_head(repo)
    linear_loop.record_mission(
        config, make_issue("HER-223"), repo=str(repo),
        branch="agent/her-200-x", worktree=repo,
    )

    linear_loop.record_verdict(config, "HER-223", "APPROVE", head)

    state = linear_loop.read_mission(config, "HER-223")
    assert state["verdict"] == "APPROVE"
    assert state["for_sha"] == head

    done = FakeTask("t_e2e", "HER-223 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = FakeLinear([make_issue("HER-223", labels=(linear_loop.LABEL_BUILDING,))])
    kanban = FakeKanban([done])
    report = linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert "HER-223" not in report.skipped
    assert len(linear.comments) == 1
    assert kanban.archived == ["t_e2e"]


def test_record_verdict_refuses_malformed_sha_or_unknown_mission(tmp_path):
    config = make_config(tmp_path)
    repo = build_finished_repo(tmp_path)
    head = repo_head(repo)
    linear_loop.record_mission(
        config, make_issue("HER-224"), repo=str(repo),
        branch="agent/her-200-x", worktree=repo,
    )

    with pytest.raises(linear_loop.LoopError, match="40"):
        linear_loop.record_verdict(config, "HER-224", "APPROVE", head[:7])
    with pytest.raises(linear_loop.LoopError, match="40"):
        linear_loop.record_verdict(config, "HER-224", "APPROVE", "z" * 40)
    with pytest.raises(linear_loop.LoopError, match="mission"):
        linear_loop.record_verdict(config, "HER-999", "APPROVE", head)
    assert "verdict" not in (linear_loop.read_mission(config, "HER-224") or {})


def test_cli_verdict_command_records_the_binding(tmp_path, monkeypatch):
    """Le producteur est invocable en vrai : la CLI écrit le rattachement exact."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = make_config(tmp_path)
    repo = build_finished_repo(tmp_path)
    head = repo_head(repo)
    linear_loop.record_mission(
        config, make_issue("HER-225"), repo=str(repo),
        branch="agent/her-200-x", worktree=repo,
    )

    rc = linear_loop.main(
        ["verdict", "--issue", "HER-225", "--verdict", "APPROVE", "--for-sha", head]
    )

    assert rc == 0
    state = linear_loop.read_mission(config, "HER-225")
    assert state["verdict"] == "APPROVE"
    assert state["for_sha"] == head


class OutageLinear(FakeLinear):
    """commentCreate passe, issueUpdate tombe : la fenêtre de crash du closeout."""

    def __init__(self, issues):
        super().__init__(issues)
        self.fail_updates = True

    def __call__(self, payload):
        if self.fail_updates and "issueUpdate" in payload["query"]:
            return {"errors": [{"message": "gateway timeout"}]}
        return super().__call__(payload)


def test_comment_is_not_duplicated_when_issue_update_fails_midway(tmp_path):
    """Si issueUpdate échoue après le commentaire, le rejeu ne reposte pas."""
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_crash", "HER-226 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = OutageLinear([make_issue("HER-226", labels=(linear_loop.LABEL_BUILDING,))])
    config = make_config(tmp_path)
    kanban = FakeKanban([done])

    with pytest.raises(linear_loop.LoopError):
        linear_loop.run_tick(
            config, linear_loop.LinearClient("k", transport=linear), kanban
        )
    assert len(linear.comments) == 1
    assert kanban.archived == []

    linear.fail_updates = False
    linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert len(linear.comments) == 1
    assert kanban.archived == ["t_crash"]


def test_crash_after_comment_accepted_but_before_marker_write_does_not_duplicate(
    tmp_path, monkeypatch
):
    """La fenêtre résiduelle : Linear a le commentaire, le disque n'a pas le marqueur.

    Le processus meurt entre commentCreate et la persistance locale. Au rejeu,
    l'état local ne connaît pas le marqueur : la boucle doit relire les
    commentaires existants sur Linear et reconnaître le sien au lieu de reposter.
    """
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_window", "HER-227 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = FakeLinear([make_issue("HER-227", labels=(linear_loop.LABEL_BUILDING,))])
    config = make_config(tmp_path)
    kanban = FakeKanban([done])

    def dying_write(config, key, state):
        raise OSError("kill -9 avant la persistance du marqueur")

    monkeypatch.setattr(linear_loop, "write_mission_state", dying_write)
    with pytest.raises(OSError):
        linear_loop.run_tick(
            config, linear_loop.LinearClient("k", transport=linear), kanban
        )

    # Linear a accepté le commentaire ; l'état local ne le sait pas.
    assert len(linear.comments) == 1
    assert not (linear_loop.read_mission(config, "HER-227") or {}).get("closeout_markers")

    monkeypatch.undo()
    linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert len(linear.comments) == 1
    assert kanban.archived == ["t_window"]
    state = linear_loop.read_mission(config, "HER-227")
    assert any(marker.startswith("linear-loop:closeout:HER-227:")
               for marker in state["closeout_markers"])


class PagedLinear(FakeLinear):
    """Sert les commentaires un par page, pour exercer la pagination réelle."""

    def __call__(self, payload):
        query = payload["query"]
        variables = payload.get("variables") or {}
        if "comments(" in query:
            bodies = self.issue_comment_bodies(variables["id"])
            start = int(variables.get("after") or 0)
            has_next = start + 1 < len(bodies)
            return {"data": {"issue": {"comments": {
                "nodes": [{"body": body} for body in bodies[start:start + 1]],
                "pageInfo": {
                    "hasNextPage": has_next,
                    "endCursor": str(start + 1) if has_next else None,
                },
            }}}}
        return super().__call__(payload)


def test_marker_on_a_later_comment_page_still_prevents_reposting(tmp_path):
    """Le marqueur peut être enfoui derrière d'autres commentaires : il faut paginer."""
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_paged", "HER-228 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    issue = make_issue("HER-228", labels=(linear_loop.LABEL_BUILDING,))
    linear = PagedLinear([issue])
    config = make_config(tmp_path)
    kanban = FakeKanban([done])
    head = repo_head(repo)
    marker = linear_loop.closeout_marker("HER-228", head, "PENDING_REVIEW")
    linear.preexisting[issue.id] = [
        "Discussion humaine sans rapport.",
        "Encore un commentaire intermédiaire.",
        f"Mission autonome terminée.\n\n<!-- {marker} -->",
    ]

    linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert linear.comments == []
    assert kanban.archived == ["t_paged"]
    state = linear_loop.read_mission(config, "HER-228")
    assert marker in state["closeout_markers"]


def test_marker_quoted_in_prose_is_not_proof_of_closeout(tmp_path):
    """Un résumé qui cite le texte du marqueur ne vaut pas le marqueur canonique."""
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_decoy", "HER-229 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    issue = make_issue("HER-229", labels=(linear_loop.LABEL_BUILDING,))
    linear = FakeLinear([issue])
    config = make_config(tmp_path)
    kanban = FakeKanban([done])
    head = repo_head(repo)
    marker = linear_loop.closeout_marker("HER-229", head, "PENDING_REVIEW")
    linear.preexisting[issue.id] = [
        f"Le prochain closeout portera le marqueur {marker}, à surveiller.",
        f"citation en ligne : <!-- {marker} --> au milieu d'une phrase.",
    ]

    linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=linear), kanban
    )

    # Aucune des citations n'est la forme canonique : la boucle poste pour de vrai.
    assert len(linear.comments) == 1
    assert f"<!-- {marker} -->" in linear.comments[0][1]


class UnreadableCommentsLinear(FakeLinear):
    """La relecture des commentaires tombe en erreur GraphQL."""

    def __call__(self, payload):
        if "comments(" in payload["query"]:
            return {"errors": [{"message": "internal error"}]}
        return super().__call__(payload)


def test_comment_read_failure_fails_closed_without_posting(tmp_path):
    """Si on ne peut pas prouver l'absence du marqueur, on ne poste rien."""
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_blind", "HER-230 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = UnreadableCommentsLinear(
        [make_issue("HER-230", labels=(linear_loop.LABEL_BUILDING,))]
    )
    config = make_config(tmp_path)
    kanban = FakeKanban([done])

    with pytest.raises(linear_loop.LoopError):
        linear_loop.run_tick(
            config, linear_loop.LinearClient("k", transport=linear), kanban
        )

    assert linear.comments == []
    assert kanban.archived == []


def test_every_notification_is_prefixed_with_its_linear_team(tmp_path):
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_prefix", "HER-208 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=FakeLinear([make_issue("HER-208")])),
        FakeKanban([done]),
    )

    assert report.messages
    assert all(message.startswith("[HER]") for message in report.messages)


def build_finished_repo(tmp_path):
    """Un vrai worktree git avec un commit, pour lire un HEAD authentique."""
    import subprocess

    repo = tmp_path / "ws"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-qb", "agent/her-200-x"], cwd=repo, check=True)
    (repo / "f.txt").write_text("corrige\n")
    subprocess.run(["git", "commit", "-aqm", "fix: corriger le defaut"], cwd=repo, check=True)
    return repo


def test_finished_mission_asks_jean_for_the_merge_go(tmp_path):
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_done", "HER-200 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = FakeLinear([make_issue("HER-200", labels=(linear_loop.LABEL_READY,
                                                       linear_loop.LABEL_BUILDING))])
    kanban = FakeKanban(
        [done], summaries={"t_done": "Défaut reproduit puis corrigé, suite verte."}
    )
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    message = report.render()
    assert "HER-200" in message
    assert "À toi de jouer" in message
    assert "1 commit " in message
    assert "Défaut reproduit" in message
    assert kanban.archived == ["t_done"]


def test_closeout_moves_the_issue_to_review_not_to_done(tmp_path):
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_done", "HER-201 — corriger", "hermes-code-b", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x", result="ok")
    linear = FakeLinear([make_issue("HER-201", labels=(linear_loop.LABEL_READY,
                                                       linear_loop.LABEL_BUILDING))])
    linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), FakeKanban([done])
    )

    update = linear.updates[0]
    assert update["stateId"] == "state-review"
    assert linear.labels[linear_loop.LABEL_REVIEW] in update["labelIds"]
    assert linear.labels[linear_loop.LABEL_BUILDING] not in update["labelIds"]


def test_summary_falls_back_to_commit_titles_when_no_handoff(tmp_path):
    """Sans handoff du worker, le rapport dit quand meme ce qui a ete commite."""
    repo = build_finished_repo(tmp_path)
    done = FakeTask("t_done", "HER-203 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x")
    linear = FakeLinear([make_issue("HER-203")])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear),
        FakeKanban([done]),
    )

    assert "fix: corriger le defaut" in report.render()


def test_uncommitted_work_is_flagged_in_the_report(tmp_path):
    repo = build_finished_repo(tmp_path)
    (repo / "oublie.txt").write_text("pas commité\n")
    done = FakeTask("t_done", "HER-202 — corriger", "hermes-code-a", "done",
                    workspace_path=str(repo), branch_name="agent/her-200-x", result="ok")
    linear = FakeLinear([make_issue("HER-202")])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), FakeKanban([done])
    )

    assert "n'ont pas été commitées" in report.render()


def test_a_card_jean_made_by_hand_is_never_archived_nor_reported(tmp_path):
    manual = FakeTask("t_manual", "HER-210 — carte manuelle", "hermes-code-a", "done",
                      created_by="jean", workspace_path=str(tmp_path))
    linear = FakeLinear([make_issue("HER-210")])
    kanban = FakeKanban([manual])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert kanban.archived == []
    assert report.closed == []


def test_a_card_still_running_holds_its_issue(tmp_path):
    """Tant qu'une carte travaille sur une issue, la boucle ne la redistribue pas.

    Vrai pour une carte de la boucle comme pour une carte posee a la main : c'est
    l'engagement en cours qui compte, pas l'auteur de la carte.
    """
    manual = FakeTask("t_manual", "HER-211 — deja en cours", "hermes-code-a", "running",
                      created_by="jean")
    linear = FakeLinear([make_issue("HER-211", priority=1)])
    kanban = FakeKanban([manual])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert kanban.created == []
    assert report.started == []


def test_closeout_survives_an_issue_deleted_from_linear(tmp_path):
    repo = build_finished_repo(tmp_path)
    orphan = FakeTask("t_orphan", "HER-999 — issue disparue", "hermes-code-a", "done",
                      workspace_path=str(repo), branch_name="agent/her-200-x", result="ok")
    linear = FakeLinear([])
    kanban = FakeKanban([orphan])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert "HER-999" in report.render()
    assert kanban.archived == ["t_orphan"]
    assert linear.comments == []


# ---------------------------------------------------------------------------
# Blocage, fusion, anteriorite
# ---------------------------------------------------------------------------


def test_a_blocked_mission_reaches_jean(tmp_path):
    """Sans relais, un worker bloque attend indefiniment sans que personne sache."""
    stuck = FakeTask("t_stuck", "HER-400 — bloquée", "hermes-code-a", "blocked")
    linear = FakeLinear([make_issue("HER-400", labels=(linear_loop.LABEL_READY,
                                                       linear_loop.LABEL_BUILDING))])
    kanban = FakeKanban([stuck], summaries={"t_stuck": "Racine d'écriture refusée."})
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    message = report.render()
    assert "HER-400" in message and "attend une décision" in message
    assert "Racine d'écriture refusée." in message
    assert linear.labels[linear_loop.LABEL_BLOCKED] in linear.updates[0]["labelIds"]


def test_a_blocked_mission_is_announced_once(tmp_path):
    """Le label sert de marqueur : pas de rappel a chaque tick."""
    stuck = FakeTask("t_stuck", "HER-401 — bloquée", "hermes-code-a", "blocked")
    linear = FakeLinear([make_issue("HER-401", labels=(linear_loop.LABEL_READY,
                                                       linear_loop.LABEL_BLOCKED))])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear),
        FakeKanban([stuck]),
    )

    assert "HER-401" not in report.render()


def merge_branch_into_main(repo, branch):
    import subprocess

    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    subprocess.run(["git", "merge", "-q", "--no-ff", "-m", "merge", branch],
                   cwd=repo, check=True)


def test_merged_issue_is_closed_and_its_worktree_freed(tmp_path):
    linear = FakeLinear([make_issue("HER-500", priority=1)])
    config = make_config(tmp_path)
    kanban = FakeKanban()
    linear_loop.run_tick(config, linear_loop.LinearClient("k", transport=linear), kanban)

    worktree = Path(kanban.created[0]["workspace_path"])
    (worktree / "fix.txt").write_text("fait\n")
    import subprocess
    subprocess.run(["git", "add", "fix.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "fix"], cwd=worktree, check=True)
    branch = linear_loop.branch_name_for(make_issue("HER-500"))
    merge_branch_into_main(Path(config.repo), branch)

    # Jean a mergé : au tick suivant l'issue porte agent-review.
    merged_view = FakeLinear([make_issue("HER-500", labels=(linear_loop.LABEL_REVIEW,))])
    report = linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=merged_view), FakeKanban()
    )

    assert "Fusionnée" in report.render()
    assert merged_view.updates[0]["stateId"] == "state-done"
    assert not worktree.exists()


def test_an_unmerged_branch_keeps_its_issue_open(tmp_path):
    linear = FakeLinear([make_issue("HER-501", priority=1)])
    config = make_config(tmp_path)
    kanban = FakeKanban()
    linear_loop.run_tick(config, linear_loop.LinearClient("k", transport=linear), kanban)
    worktree = Path(kanban.created[0]["workspace_path"])

    review = FakeLinear([make_issue("HER-501", labels=(linear_loop.LABEL_REVIEW,))])
    report = linear_loop.run_tick(
        config, linear_loop.LinearClient("k", transport=review), FakeKanban()
    )

    assert "Fusionnée" not in report.render()
    assert review.updates == []
    assert worktree.exists()


def test_an_issue_already_worked_on_is_held_not_relaunched(tmp_path):
    """Le cas HER-95 : dix cartes terminees, une issue qui repartirait en boucle.

    Les cartes anterieures peuvent avoir ete posees a la main bien avant la
    boucle — l'anteriorite ne se limite donc pas a ses propres missions.
    """
    past = FakeTask("t_old", "HER-600 — déjà traitée", "hermes-code", "done",
                    created_by="jean")
    linear = FakeLinear([make_issue("HER-600", priority=1)])
    kanban = FakeKanban([past])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert kanban.created == []
    assert "déjà été traitée" in report.render()
    assert linear.labels[linear_loop.LABEL_BLOCKED] in linear.updates[-1]["labelIds"]


def test_a_mission_that_only_blocked_does_not_count_as_prior_work(tmp_path):
    """HER-112 s'est bloquee sur l'infra : rien n'a abouti, elle doit repartir."""
    stuck = FakeTask("t_stuck", "HER-602 — bloquée puis rangée", "hermes-code-a",
                     "archived")
    linear = FakeLinear([make_issue("HER-602", priority=1)])
    kanban = FakeKanban([stuck])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert len(kanban.created) == 1
    assert "déjà été traitée" not in report.render()


def test_a_fresh_issue_is_not_held(tmp_path):
    linear = FakeLinear([make_issue("HER-601", priority=1)])
    kanban = FakeKanban()
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("k", transport=linear), kanban
    )

    assert len(kanban.created) == 1
    assert "déjà été traitée" not in report.render()


# ---------------------------------------------------------------------------
# Garde-fous
# ---------------------------------------------------------------------------


def test_api_key_is_read_from_the_profile_env_when_absent_from_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    (tmp_path / ".env").write_text('OTHER=1\nLINEAR_API_KEY="lin_api_secret"\n')
    assert linear_loop.load_api_key(tmp_path) == "lin_api_secret"


def test_missing_api_key_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(linear_loop.LoopError, match="LINEAR_API_KEY"):
        linear_loop.load_api_key(tmp_path)


def test_graphql_errors_are_raised_as_loop_errors():
    def transport(payload):
        return {"errors": [{"message": "Access denied"}]}

    client = linear_loop.LinearClient("k", transport=transport)
    with pytest.raises(linear_loop.LoopError, match="Access denied"):
        client.team_snapshot(TEAM)


def test_unknown_team_is_reported_not_silently_empty():
    client = linear_loop.LinearClient("k", transport=lambda p: {"data": {"team": None}})
    with pytest.raises(linear_loop.LoopError, match="introuvable"):
        client.team_snapshot("NOPE")


def test_tick_output_never_contains_the_api_key(tmp_path):
    linear = FakeLinear([make_issue("HER-300", priority=1)])
    report = linear_loop.run_tick(
        make_config(tmp_path), linear_loop.LinearClient("lin_api_TOPSECRET", transport=linear),
        FakeKanban(),
    )
    assert "TOPSECRET" not in report.render()
    assert "TOPSECRET" not in json.dumps(report.started)
