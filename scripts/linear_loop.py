#!/usr/bin/env python3
"""Boucle Linear -> codeurs autonomes.

Linear est la seule interface humaine. Jean pose le label ``agent-ready`` sur une
issue ; la boucle la transforme en mission pour un codeur libre, laisse le
dispatcher kanban faire tourner le worker, puis rend la main a Jean pour le
merge. Le kanban reste de la plomberie : personne n'a besoin de l'ouvrir.

L'ordonnancement est deterministe : aucun modele n'intervient dans le choix de
l'issue, du codeur ou du moment. Le seul LLM du systeme est celui qui code, dans
le worker. C'est ce qui rend un tick gratuit, rejouable et testable hors ligne.

Un tick fait, dans l'ordre :

1. ``closeout`` — toute mission terminee est rapportee sur son issue Linear
   (commentaire + passage en revue) et signalee a Jean pour le GO de merge.
2. ``feed`` — chaque codeur libre recoit l'issue ``agent-ready`` la plus
   prioritaire qui n'est pas deja prise.

Sortie vide = rien a signaler ; le cron reste silencieux. Toute sortie non vide
part vers Telegram.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

DEFAULT_TEAM = "HER"
DEFAULT_CODERS = ("hermes-code-a", "hermes-code-b")
DEFAULT_REPO = "/Users/jeanyoder/.hermes/hermes-agent"

#: Signature des cartes creees par la boucle : tout ce que la boucle archive ou
#: rapporte doit porter cette marque, pour ne jamais toucher une carte humaine.
LOOP_AUTHOR = "linear-loop"

LABEL_READY = "agent-ready"
LABEL_BUILDING = "agent-building"
LABEL_BLOCKED = "agent-blocked"
LABEL_REVIEW = "agent-review"

STATE_BUILDING = "In Progress"
STATE_REVIEW = "In Review"

#: En dessous de ce seuil on ne demarre plus de mission : un worktree par
#: mission finit par remplir le disque, et un disque plein casse le runtime
#: bien plus surement qu'une issue traitee en retard.
MIN_FREE_DISK_BYTES = 3 * 1024**3

#: Priorite Linear : 0 signifie "aucune", pas "la plus urgente".
NO_PRIORITY_RANK = 99

#: Plafond de duree d'une mission. Un worker qui a fini son travail peut
#: enchainer sur du zele (auto-revue, exploration) et retenir son codeur pour
#: rien ; passe ce delai le dispatcher le termine et remet la carte en file.
MISSION_MAX_RUNTIME_SECONDS = 3600

_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-[1-9][0-9]*)\b")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Statuts qui retiennent une issue : tant qu'une carte est dans un de ces
#: etats, la boucle ne redistribue pas son issue. ``blocked`` en fait partie —
#: une mission bloquee attend une reponse de Jean, elle n'est pas perdue.
ENGAGED_TASK_STATUSES = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")

#: Statuts qui occupent reellement un codeur. ``blocked`` en est absent a
#: dessein : une carte bloquee peut le rester des jours, et compter un blocage
#: comme une occupation gelerait le codeur definitivement.
BUSY_TASK_STATUSES = ("todo", "scheduled", "ready", "running", "review")


class LoopError(RuntimeError):
    """Erreur bloquante du tick, rapportee telle quelle a Jean."""


# --------------------------------------------------------------------------
# Modele
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    key: str
    id: str
    title: str
    description: str
    priority: int
    labels: frozenset[str]
    state_name: str
    state_type: str
    url: str
    created_at: str

    @classmethod
    def from_node(cls, node: dict[str, Any]) -> "Issue":
        state = node.get("state") or {}
        labels = (node.get("labels") or {}).get("nodes") or []
        return cls(
            key=node["identifier"],
            id=node["id"],
            title=node.get("title") or "",
            description=node.get("description") or "",
            priority=int(node.get("priority") or 0),
            labels=frozenset(label["name"] for label in labels),
            state_name=state.get("name") or "",
            state_type=state.get("type") or "",
            url=node.get("url") or "",
            created_at=node.get("createdAt") or "",
        )


@dataclass
class TickReport:
    """Ce que le tick a fait. Seul ``messages`` remonte a Jean."""

    started: list[dict[str, str]] = field(default_factory=list)
    closed: list[dict[str, str]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        return "\n\n".join(self.messages).strip()


# --------------------------------------------------------------------------
# Client Linear
# --------------------------------------------------------------------------


def load_api_key(hermes_home: Path) -> str:
    """Recupere la cle Linear sans jamais la journaliser.

    L'environnement du cron est assaini avant d'atteindre le script, donc on
    retombe sur le ``.env`` du profil quand la variable a ete filtree.
    """
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if key:
        return key
    env_file = hermes_home / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("LINEAR_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise LoopError(
        "LINEAR_API_KEY introuvable (ni dans l'environnement, ni dans "
        f"{env_file}). La boucle ne peut pas lire Linear."
    )


class LinearClient:
    """Acces GraphQL minimal : lire les issues, commenter, changer d'etat."""

    API_URL = "https://api.linear.app/graphql"

    def __init__(
        self,
        api_key: str,
        *,
        transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout

    def query(self, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        if self._transport is not None:
            body = self._transport(payload)
        else:
            body = self._http(payload)
        if body.get("errors"):
            raise LoopError("Linear a refuse la requete: " + json.dumps(body["errors"])[:400])
        return body.get("data") or {}

    def _http(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": self._api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise LoopError(f"Linear HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise LoopError(f"Linear injoignable: {exc.reason}") from None

    # -- lectures ---------------------------------------------------------

    ISSUES_QUERY = """
    query($team: String!) {
      team(id: $team) {
        id
        labels(first: 250) { nodes { id name } }
        states(first: 50) { nodes { id name type } }
        issues(first: 100, filter: { state: { type: { nin: ["completed", "canceled"] } } }) {
          nodes {
            id identifier title description priority url createdAt
            state { name type }
            labels { nodes { name } }
          }
        }
      }
    }
    """

    def team_snapshot(self, team: str) -> tuple[list[Issue], dict[str, str], dict[str, str]]:
        data = self.query(self.ISSUES_QUERY, {"team": team})
        team_node = data.get("team")
        if not team_node:
            raise LoopError(f"team Linear {team!r} introuvable")
        issues = [Issue.from_node(node) for node in team_node["issues"]["nodes"]]
        labels = {n["name"]: n["id"] for n in team_node["labels"]["nodes"]}
        states = {n["name"]: n["id"] for n in team_node["states"]["nodes"]}
        return issues, labels, states

    def issue_label_ids(self, issue_id: str) -> list[str]:
        data = self.query(
            "query($id: String!) { issue(id: $id) { labels { nodes { id } } } }",
            {"id": issue_id},
        )
        return [n["id"] for n in ((data.get("issue") or {}).get("labels") or {}).get("nodes", [])]

    # -- ecritures --------------------------------------------------------

    def add_comment(self, issue_id: str, body: str) -> None:
        self.query(
            "mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }",
            {"input": {"issueId": issue_id, "body": body}},
        )

    def update_issue(
        self,
        issue_id: str,
        *,
        label_ids: Optional[Sequence[str]] = None,
        state_id: Optional[str] = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if label_ids is not None:
            payload["labelIds"] = list(label_ids)
        if state_id is not None:
            payload["stateId"] = state_id
        if not payload:
            return
        self.query(
            "mutation($id: String!, $input: IssueUpdateInput!) "
            "{ issueUpdate(id: $id, input: $input) { success } }",
            {"id": issue_id, "input": payload},
        )

    def create_label(self, team_id: str, name: str, color: str, description: str) -> str:
        data = self.query(
            "mutation($input: IssueLabelCreateInput!) "
            "{ issueLabelCreate(input: $input) { issueLabel { id } } }",
            {
                "input": {
                    "teamId": team_id,
                    "name": name,
                    "color": color,
                    "description": description,
                }
            },
        )
        return data["issueLabelCreate"]["issueLabel"]["id"]


# --------------------------------------------------------------------------
# Selection — fonctions pures, testables sans reseau ni base
# --------------------------------------------------------------------------


def selection_rank(issue: Issue) -> tuple[int, str]:
    """Ordre de traitement : priorite Linear d'abord, puis anciennete."""
    priority = issue.priority if issue.priority else NO_PRIORITY_RANK
    return priority, issue.created_at


def is_eligible(issue: Issue) -> bool:
    """Une issue est traitable si Jean l'a autorisee et que rien ne la retient.

    ``agent-ready`` est le seul geste humain requis : sans lui, une issue reste
    invisible pour la boucle. C'est ce qui empeche un codeur de tomber sur une
    tache business (signature de deal, relecture juridique, achat media).
    """
    if LABEL_READY not in issue.labels:
        return False
    if issue.labels & {LABEL_BUILDING, LABEL_BLOCKED, LABEL_REVIEW}:
        return False
    return issue.state_type in {"backlog", "unstarted", "started"}


def select_missions(
    issues: Iterable[Issue],
    *,
    capacity: int,
    busy_keys: Iterable[str] = (),
) -> list[Issue]:
    """Les ``capacity`` prochaines issues a confier, deja triees."""
    if capacity <= 0:
        return []
    taken = set(busy_keys)
    ranked = sorted((i for i in issues if is_eligible(i) and i.key not in taken), key=selection_rank)
    return ranked[:capacity]


def branch_name_for(issue: Issue) -> str:
    slug = _SLUG_RE.sub("-", issue.title.lower()).strip("-")
    return f"agent/{issue.key.lower()}-{slug[:40].strip('-')}" if slug else f"agent/{issue.key.lower()}"


def issue_key_of_title(title: str) -> Optional[str]:
    match = _KEY_RE.search(title or "")
    return match.group(1) if match else None


def repo_for_issue(issue: Issue, default_repo: str) -> str:
    """Depot cible, surchargeable par une ligne ``Repo: /chemin`` dans l'issue.

    Sans directive on vise le depot Hermes : c'est la seule base de code que les
    codeurs ont le droit de muter aujourd'hui.
    """
    for line in (issue.description or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("repo:"):
            candidate = stripped.split(":", 1)[1].strip().strip("`")
            if candidate:
                return candidate
    return default_repo


def build_brief(issue: Issue, *, branch: str, repo: str) -> str:
    """Brief de mission depose dans la carte, lu par le worker au demarrage."""
    description = (issue.description or "").strip() or "(aucune description dans Linear)"
    return f"""Mission autonome — {issue.key}

Issue Linear : {issue.url}
Depot : {repo}
Branche imposee : {branch}

## Enonce (copie de Linear)

{description}

## Contrat

- Un seul perimetre : cette issue. Aucune autre carte, aucun autre worktree.
- Si les criteres d'acceptation ne sont pas explicites ci-dessus, publie ta
  lecture (AC-1..N et non-objectifs) en commentaire de la carte avant de coder,
  puis avance sans attendre de reponse.
- Test d'abord : reproduis le defaut en rouge, corrige, prouve le vert avec
  `scripts/run_tests.sh`. Ne modifie jamais un test pour le faire passer.
- Commits locaux uniquement, sur `{branch}`. Aucun push, aucune PR, aucun merge :
  la fusion appartient a Jean.
- Des que les criteres sont satisfaits et le commit fait, termine par
  `kanban_complete` avec un resume court : ce qui a change, ce qui est prouve,
  ce qui reste. N'enchaine sur aucun travail supplementaire — pas d'auto-revue,
  pas de refactor opportuniste, pas d'exploration : la revue est faite ailleurs
  et le temps passe apres la fin est du temps perdu pour l'issue suivante.
- Si tu es bloque sur une decision produit ou un credential, `kanban_block`
  avec la question exacte.
"""


# --------------------------------------------------------------------------
# Kanban — plomberie
# --------------------------------------------------------------------------


def _import_kanban(runtime_root: str):
    if runtime_root not in sys.path:
        sys.path.insert(0, runtime_root)
    from hermes_cli import kanban_db  # noqa: PLC0415 — resolution differee volontaire

    return kanban_db


def active_issue_keys(tasks: Iterable[Any]) -> set[str]:
    """Cles Linear deja engagees, pour ne jamais dispatcher deux fois la meme."""
    keys = set()
    for task in tasks:
        key = issue_key_of_title(getattr(task, "title", ""))
        if key:
            keys.add(key)
    return keys


def free_coders(tasks: Iterable[Any], coders: Sequence[str]) -> list[str]:
    """Codeurs sans mission vivante, dans l'ordre de la configuration."""
    busy = {
        getattr(task, "assignee", None)
        for task in tasks
        if getattr(task, "status", None) in BUSY_TASK_STATUSES
    }
    return [coder for coder in coders if coder not in busy]


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def mission_result(workspace: Path, branch: str) -> dict[str, str]:
    """Ce que la mission a reellement produit, lu dans son worktree."""
    if not workspace.exists():
        return {"commits": "0", "head": "", "dirty": "", "log": ""}
    head = git_output(workspace, "rev-parse", "--short", "HEAD")
    base = git_output(workspace, "merge-base", "HEAD", "main") or "HEAD"
    log = git_output(workspace, "log", "--oneline", f"{base}..HEAD")
    dirty = git_output(workspace, "status", "--porcelain")
    return {
        "commits": str(len([line for line in log.splitlines() if line])),
        "head": head,
        "dirty": dirty,
        "log": log,
        "branch": git_output(workspace, "rev-parse", "--abbrev-ref", "HEAD") or branch,
    }


# --------------------------------------------------------------------------
# Tick
# --------------------------------------------------------------------------


@dataclass
class LoopConfig:
    team: str = DEFAULT_TEAM
    coders: Sequence[str] = DEFAULT_CODERS
    repo: str = DEFAULT_REPO
    hermes_home: Path = Path("/Users/jeanyoder/.hermes")
    runtime_root: str = DEFAULT_REPO
    apply: bool = False
    min_free_disk: int = MIN_FREE_DISK_BYTES


def run_tick(config: LoopConfig, client: LinearClient, kanban) -> TickReport:
    report = TickReport()
    issues, labels, states = client.team_snapshot(config.team)
    by_key = {issue.key: issue for issue in issues}

    with kanban.connect() as conn:
        _closeout_finished(config, client, kanban, conn, by_key, labels, states, report)
        _feed_free_coders(config, client, kanban, conn, issues, labels, states, report)
    return report


def _closeout_finished(config, client, kanban, conn, by_key, labels, states, report) -> None:
    """Rapporte sur Linear chaque mission terminee et demande le GO de merge.

    Seules les cartes creees par la boucle sont touchees : une carte done posee
    a la main par Jean ne doit ni etre archivee ni declencher un writeback.
    """
    done = [
        task
        for task in kanban.list_tasks(conn, status="done")
        if task.created_by == LOOP_AUTHOR and issue_key_of_title(task.title)
    ]
    for task in done:
        key = issue_key_of_title(task.title)
        issue = by_key.get(key)
        workspace = Path(task.workspace_path) if task.workspace_path else None
        result = mission_result(workspace, task.branch_name or "") if workspace else {}
        summary = _mission_summary(kanban, conn, task, result)

        lines = [
            f"✅ {key} — {issue.title if issue else task.title}",
            f"Codeur : {task.assignee} · branche `{result.get('branch') or task.branch_name}` · "
            f"{result.get('commits', '?')} commit(s) · HEAD `{result.get('head', '?')}`",
        ]
        if summary:
            lines.append(f"Résumé : {summary}")
        if result.get("dirty"):
            lines.append("⚠ worktree non propre — des changements ne sont pas commités")
        if workspace:
            lines.append(f"Worktree : {workspace}")
        if issue:
            lines.append(issue.url)
        lines.append("→ GO pour merger ? (rien n'est poussé, rien n'est fusionné)")
        report.messages.append("\n".join(lines))
        report.closed.append({"issue": key or "", "task": task.id})

        if not config.apply:
            continue

        if issue:
            comment = (
                f"Mission autonome terminée par `{task.assignee}`.\n\n"
                f"- Branche locale : `{result.get('branch') or task.branch_name}`\n"
                f"- HEAD : `{result.get('head', '?')}` ({result.get('commits', '?')} commit(s))\n"
                f"- Worktree : `{workspace}`\n\n"
                f"{summary or '(pas de résumé fourni)'}\n\n"
                "Rien n'a été poussé ni fusionné : en attente du GO de Jean."
            )
            client.add_comment(issue.id, comment)
            label_ids = _labels_after_build(client, issue, labels, LABEL_REVIEW)
            client.update_issue(
                issue.id, label_ids=label_ids, state_id=states.get(STATE_REVIEW)
            )
        kanban.archive_task(conn, task.id)


def _feed_free_coders(config, client, kanban, conn, issues, labels, states, report) -> None:
    """Donne a chaque codeur libre l'issue autorisee la plus prioritaire."""
    active = [
        task
        for status in ENGAGED_TASK_STATUSES
        for task in kanban.list_tasks(conn, status=status)
    ]
    available = free_coders(active, config.coders)
    if not available:
        report.skipped["capacity"] = "les deux codeurs sont occupés"
        return

    free_disk = shutil.disk_usage(str(config.hermes_home)).free
    if free_disk < config.min_free_disk:
        message = (
            f"⛔ Boucle en pause : {free_disk / 1024**3:.1f} Gio libres sur le disque "
            f"(seuil {config.min_free_disk / 1024**3:.0f} Gio). Chaque mission crée un "
            "worktree ; je ne démarre rien tant que la place n'est pas faite."
        )
        report.skipped["disk"] = message
        report.messages.append(message)
        return

    missions = select_missions(
        issues, capacity=len(available), busy_keys=active_issue_keys(active)
    )
    if not missions:
        report.skipped["backlog"] = f"aucune issue {LABEL_READY} disponible"
        return

    for coder, issue in zip(available, missions):
        repo = repo_for_issue(issue, config.repo)
        branch = branch_name_for(issue)
        report.started.append({"issue": issue.key, "coder": coder, "branch": branch})
        if not config.apply:
            continue

        attempt = _attempt_number(kanban, conn, issue.key)
        task_id = kanban.create_task(
            conn,
            title=f"{issue.key} — {issue.title}",
            body=build_brief(issue, branch=branch, repo=repo),
            assignee=coder,
            created_by=LOOP_AUTHOR,
            workspace_kind="worktree",
            workspace_path=repo,
            branch_name=branch,
            priority=_kanban_priority(issue),
            max_runtime_seconds=MISSION_MAX_RUNTIME_SECONDS,
            idempotency_key=f"linear:{issue.key}:{attempt}",
        )
        label_ids = _labels_after_build(client, issue, labels, LABEL_BUILDING)
        client.add_comment(
            issue.id,
            f"Prise en charge par `{coder}` (branche locale `{branch}`).\n\n"
            f"Suivi : carte kanban `{task_id}`. Aucun push ni merge ne sera fait "
            "sans le GO de Jean.",
        )
        client.update_issue(issue.id, label_ids=label_ids, state_id=states.get(STATE_BUILDING))


def _mission_summary(kanban, conn, task, result: dict[str, str]) -> str:
    """Ce que le worker dit avoir fait.

    ``kanban_complete`` ecrit son handoff dans ``task_runs.summary`` et laisse
    ``tasks.result`` vide : lire uniquement ``result`` fait passer une mission
    reussie pour un no-op. A defaut de handoff, les titres de commits disent
    toujours quelque chose de vrai.
    """
    summary = (kanban.latest_summary(conn, task.id) or "").strip()
    if not summary:
        summary = (task.result or "").strip()
    if not summary:
        summary = (result.get("log") or "").strip()
    return summary


def _attempt_number(kanban, conn, key: str) -> int:
    """Numero de passage sur cette issue — une reprise doit pouvoir recreer une carte."""
    previous = [
        task
        for task in kanban.list_tasks(conn, include_archived=True)
        if task.created_by == LOOP_AUTHOR and issue_key_of_title(task.title) == key
    ]
    return len(previous) + 1


def _kanban_priority(issue: Issue) -> int:
    """Priorite Linear (1 urgent .. 4 bas) -> priorite kanban (haut = urgent)."""
    return 0 if not issue.priority else max(0, 5 - issue.priority)


def _labels_after_build(client, issue: Issue, labels: dict[str, str], add: str) -> list[str]:
    """Etat des labels apres transition, en preservant ceux qu'on ne gere pas."""
    managed = {LABEL_BUILDING, LABEL_BLOCKED, LABEL_REVIEW}
    keep = {name for name in issue.labels if name not in managed}
    keep.add(add)
    return [labels[name] for name in keep if name in labels]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

WANTED_LABELS = (
    (
        LABEL_READY,
        "#5E6AD2",
        "Jean autorise un codeur autonome a traiter cette issue. "
        "Seule porte d'entree de la boucle.",
    ),
    (LABEL_BUILDING, "#F2C94C", "Mission en cours (pose et retire par la boucle)."),
    (
        LABEL_BLOCKED,
        "#EB5757",
        "La boucle a besoin de Jean : specification ambigue, credential, decision produit.",
    ),
    (LABEL_REVIEW, "#27AE60", "Travail termine, branche locale prete : en attente du GO de merge."),
)


def cmd_ensure_labels(config: LoopConfig, client: LinearClient) -> str:
    data = client.query(
        "query($team: String!) { team(id: $team) { id labels(first: 250) { nodes { name } } } }",
        {"team": config.team},
    )
    team = data.get("team")
    if not team:
        raise LoopError(f"team Linear {config.team!r} introuvable")
    existing = {node["name"] for node in team["labels"]["nodes"]}
    lines = []
    for name, color, description in WANTED_LABELS:
        if name in existing:
            lines.append(f"= {name}")
        elif not config.apply:
            lines.append(f"+ {name} (dry-run)")
        else:
            client.create_label(team["id"], name, color, description)
            lines.append(f"+ {name} créé")
    return "\n".join(lines)


def cmd_status(config: LoopConfig, client: LinearClient, kanban) -> str:
    issues, _, _ = client.team_snapshot(config.team)
    with kanban.connect() as conn:
        active = [
            task
            for status in ENGAGED_TASK_STATUSES
            for task in kanban.list_tasks(conn, status=status)
        ]
    ready = sorted((i for i in issues if is_eligible(i)), key=selection_rank)
    lines = [f"Team {config.team} — {len(issues)} issues ouvertes, {len(ready)} prêtes pour un codeur"]
    for issue in ready[:10]:
        lines.append(f"  P{issue.priority} {issue.key} {issue.title[:60]}")
    lines.append(f"Missions en cours : {len(active)}")
    for task in active:
        lines.append(f"  {task.assignee} · {task.status} · {task.title[:60]}")
    lines.append(f"Codeurs libres : {', '.join(free_coders(active, config.coders)) or 'aucun'}")
    return "\n".join(lines)


def build_config(args: argparse.Namespace) -> LoopConfig:
    hermes_home = Path(os.environ.get("HERMES_HOME", "/Users/jeanyoder/.hermes"))
    return LoopConfig(
        team=args.team,
        coders=tuple(args.coder) if args.coder else DEFAULT_CODERS,
        repo=args.repo,
        hermes_home=hermes_home,
        runtime_root=args.runtime,
        apply=args.apply,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["tick", "status", "ensure-labels"])
    parser.add_argument("--team", default=DEFAULT_TEAM)
    parser.add_argument("--coder", action="append", default=None)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--runtime", default=DEFAULT_REPO)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="ecrire reellement (Linear + kanban). Sans ce drapeau, rien n'est mute.",
    )
    args = parser.parse_args(argv)
    config = build_config(args)

    try:
        client = LinearClient(load_api_key(config.hermes_home))
        if args.command == "ensure-labels":
            print(cmd_ensure_labels(config, client))
            return 0
        kanban = _import_kanban(config.runtime_root)
        if args.command == "status":
            print(cmd_status(config, client, kanban))
            return 0
        report = run_tick(config, client, kanban)
        rendered = report.render()
        if rendered:
            print(rendered)
        return 0
    except LoopError as exc:
        print(f"⛔ Boucle Linear : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
