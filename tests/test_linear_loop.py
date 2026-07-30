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

    def __call__(self, payload):
        query = payload["query"]
        variables = payload.get("variables") or {}
        if "commentCreate" in query:
            self.comments.append((variables["input"]["issueId"], variables["input"]["body"]))
            return {"data": {"commentCreate": {"success": True}}}
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
