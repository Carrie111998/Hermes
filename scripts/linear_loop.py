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

#: Ou vivent les worktrees de mission. Ce n'est pas un detail de rangement : les
#: profils Code n'autorisent l'ecriture que sous leur ``HERMES_WRITE_SAFE_ROOT``,
#: et le defaut du kanban (``<repo>/.worktrees/<id>``) tombe a l'interieur du
#: runtime deploye — hors racine sure, et de toute facon le dernier endroit ou un
#: codeur devrait pouvoir ecrire, puisque c'est le code qui le gouverne.
DEFAULT_WORKTREES_ROOT = "/Users/jeanyoder/Documents/GitHub/_worktrees"

#: Signature des cartes creees par la boucle : tout ce que la boucle archive ou
#: rapporte doit porter cette marque, pour ne jamais toucher une carte humaine.
LOOP_AUTHOR = "linear-loop"

LABEL_READY = "agent-ready"
LABEL_BUILDING = "agent-building"
LABEL_BLOCKED = "agent-blocked"
LABEL_REVIEW = "agent-review"

STATE_BUILDING = "In Progress"
STATE_REVIEW = "In Review"
STATE_DONE = "Done"

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

#: Seule forme de SHA qui lie un verdict : l'identifiant complet de l'objet
#: commit. Un prefixe court est ambigu (plusieurs objets peuvent le partager)
#: et ne prouve pas que la revue portait sur le candidat actuel.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Verdict implicite d'une mission jamais revue : le closeout peut avoir lieu
#: (Jean relit lui-meme), mais tout autre verdict doit citer son SHA exact.
VERDICT_PENDING = "PENDING_REVIEW"

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


def team_prefix(team: str) -> str:
    """Prefixe stable des alertes Telegram, borne aux equipes Linear."""
    return f"[{team.strip().upper() or DEFAULT_TEAM}]"


def closeout_marker(key: str, candidate_sha: str, verdict: str) -> str:
    return f"linear-loop:closeout:{key}:{candidate_sha or 'unknown'}:{verdict}"


def marker_comment_pattern(marker: str) -> re.Pattern[str]:
    """Seule preuve d'un closeout : le marqueur sous sa forme canonique.

    ``closeout_comment`` ecrit le marqueur comme commentaire HTML seul sur sa
    ligne. Une citation du meme texte au fil d'une phrase (resume, discussion)
    ne compte pas — sinon n'importe quelle prose pourrait etouffer un vrai post.
    """
    return re.compile(rf"(?m)^<!-- {re.escape(marker)} -->[ \t\r]*$")


def closeout_comment(
    *, key: str, assignee: str | None, branch: str, pull_request: str,
    candidate_sha: str, verdict: str, summary: str, marker: str,
) -> str:
    """Le fait canonique de closeout, re-jouable par son marqueur HTML."""
    return (
        f"Mission autonome terminée par `{assignee or 'un codeur'}`.\n\n"
        f"Triplet de closeout : {{branche/PR: `{branch}` / `{pull_request or 'non créée'}`, "
        f"SHA candidat: `{candidate_sha or '?'}`, verdict: `{verdict}`}}\n\n"
        f"{summary or '(pas de résumé fourni)'}\n\n"
        "Rien n'a été poussé ni fusionné : en attente du GO de Jean.\n\n"
        f"<!-- {marker} -->"
    )


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

    COMMENTS_QUERY = """
    query($id: String!, $after: String) {
      issue(id: $id) {
        comments(first: 100, after: $after) {
          nodes { body }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """

    def issue_comment_bodies(self, issue_id: str) -> list[str]:
        """Tous les corps de commentaires de l'issue, toutes pages confondues.

        Toute reponse anormale (erreur GraphQL via ``query``, issue absente,
        curseur qui ne progresse pas) leve : le lecteur echoue ferme plutot que
        de laisser croire qu'un commentaire n'existe pas.
        """
        bodies: list[str] = []
        after: Optional[str] = None
        while True:
            data = self.query(self.COMMENTS_QUERY, {"id": issue_id, "after": after})
            issue = data.get("issue")
            if not issue:
                raise LoopError(
                    f"issue {issue_id!r} introuvable en relisant ses commentaires"
                )
            page = issue.get("comments") or {}
            bodies.extend(str(node.get("body") or "") for node in page.get("nodes") or [])
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return bodies
            cursor = info.get("endCursor")
            if not cursor or cursor == after:
                raise LoopError("pagination des commentaires Linear ne progresse pas")
            after = cursor

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


def coder_label(assignee: Optional[str]) -> str:
    """`hermes-code-a` -> `Code A`. Les messages parlent a Jean, pas a la machine."""
    name = (assignee or "").strip()
    if name.startswith("hermes-code-"):
        return f"Code {name.rsplit('-', 1)[-1].upper()}"
    return name or "un codeur"


def short_title(title: str, limit: int = 62) -> str:
    text = (title or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def quote(text: str, limit: int = 200) -> str:
    """Reprend les mots du worker sans les laisser envahir le message."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


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
- **Travaille exclusivement dans `{repo}`.** Ce dossier contient le depot
  complet : le code que tu dois lire y est deja. N'utilise jamais un chemin
  absolu vers une autre copie (ni `~/.hermes/hermes-agent`, ni un autre
  worktree) — ce sont d'autres exemplaires du meme code, et y toucher est
  refuse par construction.
- **Un refus du garde-fou n'est pas une panne.** S'il refuse une commande,
  c'est que la cible est hors de ton worktree : corrige le chemin et continue.
  N'audite pas le systeme d'admission, ne cherche pas ton proprietaire dans le
  registre, ne bloque pas pour cette raison — l'admission a ete verifiee avant
  ton demarrage.
- Si les criteres d'acceptation ne sont pas explicites ci-dessus, publie ta
  lecture (AC-1..N et non-objectifs) en commentaire de la carte avant de coder,
  puis avance sans attendre de reponse.
- Test d'abord : reproduis le defaut en rouge, corrige, prouve le vert avec
  `scripts/run_tests.sh`. Ne modifie jamais un test pour le faire passer.
- Commits locaux uniquement, sur `{branch}`. Aucun push, aucune PR, aucun merge :
  la fusion appartient a Jean.
- Ecris les migrations, ne les execute jamais. Aucune commande qui touche une
  base de donnees, un service distant, un paiement ou une campagne publicitaire
  — meme en lecture, meme "pour verifier". Le garde-fou d'admission raisonne en
  proprietaire de repertoire : il ne peut pas rattraper un effet distant.
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


def git_run(repo: Path, *args: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=300
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def ensure_mission_worktree(repo: str, target: Path, branch: str) -> Path:
    """Cree le worktree de la mission a un endroit ou le codeur a le droit d'ecrire.

    Le kanban placerait le worktree sous ``<repo>/.worktrees/``, donc a
    l'interieur du runtime : hors de la racine sure des profils Code, et sur le
    code qui les gouverne. On le materialise nous-memes sous la racine autorisee
    et on passe la carte en ``dir`` pour que le kanban n'y touche plus.
    """
    repo_path = Path(repo)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    base = git_output(repo_path, "rev-parse", "--abbrev-ref", "HEAD") or "main"
    existing = git_output(repo_path, "rev-parse", "--verify", "--quiet", branch)
    args = (
        ["worktree", "add", str(target), branch]
        if existing
        else ["worktree", "add", "-b", branch, str(target), base]
    )
    ok, detail = git_run(repo_path, *args)
    if not ok:
        raise LoopError(f"worktree {target} impossible: {detail[:200]}")
    return target


def remove_mission_worktree(repo: str, target: Path) -> bool:
    """Rend l'espace d'une mission fusionnee. Refuse tout worktree encore sale."""
    if not target.exists():
        return False
    if git_output(target, "status", "--porcelain"):
        return False
    ok, _ = git_run(Path(repo), "worktree", "remove", str(target))
    return ok


def mission_result(workspace: Path, branch: str) -> dict[str, str]:
    """Ce que la mission a reellement produit, lu dans son worktree."""
    if not workspace.exists():
        return {"commits": "0", "head": "", "dirty": "", "log": ""}
    head = git_output(workspace, "rev-parse", "HEAD")
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
    worktrees_root: str = DEFAULT_WORKTREES_ROOT
    apply: bool = False
    min_free_disk: int = MIN_FREE_DISK_BYTES


def run_tick(config: LoopConfig, client: LinearClient, kanban) -> TickReport:
    report = TickReport()
    issues, labels, states = client.team_snapshot(config.team)
    by_key = {issue.key: issue for issue in issues}

    with kanban.connect() as conn:
        _close_merged(config, client, issues, labels, states, report)
        _closeout_finished(config, client, kanban, conn, by_key, labels, states, report)
        _report_blocked(config, client, kanban, conn, by_key, labels, report)
        _feed_free_coders(config, client, kanban, conn, issues, labels, states, report)
    prefix = team_prefix(config.team)
    report.messages = [message if message.startswith(prefix) else f"{prefix} {message}"
                       for message in report.messages]
    return report


def _closeout_already_posted(client, issue_id: str, marker: str) -> bool:
    """Vrai si l'issue porte deja le marqueur canonique de ce closeout."""
    pattern = marker_comment_pattern(marker)
    return any(pattern.search(body) for body in client.issue_comment_bodies(issue_id))


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
        task_key = key or task.id
        issue = by_key.get(key)
        workspace = Path(task.workspace_path) if task.workspace_path else None
        result = mission_result(workspace, task.branch_name or "") if workspace else {}
        summary = _mission_summary(kanban, conn, task, result)
        state = read_mission(config, task_key) or {}
        candidate_sha = result.get("head", "")
        verdict = str(state.get("verdict") or VERDICT_PENDING)
        for_sha = str(state.get("for_sha") or "")
        if verdict != VERDICT_PENDING and not _FULL_SHA_RE.fullmatch(for_sha):
            report.skipped[task_key] = "verdict unbound"
            report.messages.append(
                f"⛔ {key} — verdict `{verdict}` sans rattachement exact : il doit "
                f"citer le SHA complet du candidat (`{candidate_sha or '?'}`). "
                "Rien n'a été écrit sur Linear."
            )
            continue
        if for_sha and for_sha != candidate_sha:
            report.skipped[task_key] = "verdict stale"
            report.messages.append(
                f"⛔ {key} — verdict `{verdict}` lié à `{for_sha}` est périmé : "
                f"le candidat actuel est `{candidate_sha or '?'}`."
            )
            continue
        branch = result.get("branch") or task.branch_name or "?"
        pull_request = str(state.get("pull_request") or "")
        marker = closeout_marker(task_key, candidate_sha, verdict)
        markers = set(state.get("closeout_markers") or ())

        commits = result.get("commits", "?")
        pluriel = "s" if commits not in ("0", "1", "?") else ""
        lines = [
            f"✅ {key} — {short_title(issue.title if issue else task.title)}",
            f"{coder_label(task.assignee)} a terminé : {commits} commit{pluriel} "
            f"sur `{branch}`.",
        ]
        if summary:
            lines.append(f"\n📝 {quote(summary)}")
        if result.get("dirty"):
            lines.append("\n⚠️ Attention : des modifications n'ont pas été commitées.")
        lines.append("\n👉 À toi de jouer : relis, et fusionne si ça te va. "
                     "Rien n'a été poussé.")
        if issue:
            lines.append(f"🔗 {issue.url}")
        report.messages.append("\n".join(lines))
        report.closed.append({"issue": key or "", "task": task.id})

        if not config.apply:
            continue

        if issue:
            if marker not in markers:
                # L'etat local peut mentir par omission : un tick precedent a pu
                # mourir apres que Linear a accepte commentCreate mais avant la
                # persistance du marqueur. Avant de poster, on relit donc les
                # commentaires existants ; si la relecture echoue, on ne poste
                # pas (mieux vaut un closeout en retard qu'un doublon).
                if not _closeout_already_posted(client, issue.id, marker):
                    client.add_comment(issue.id, closeout_comment(
                        key=task_key,
                        assignee=task.assignee,
                        branch=branch,
                        pull_request=pull_request,
                        candidate_sha=candidate_sha,
                        verdict=verdict,
                        summary=summary,
                        marker=marker,
                    ))
                # Le marqueur est persiste des que le commentaire existe, AVANT
                # issueUpdate : si la transition d'etat echoue, le rejeu retente
                # la transition sans reposter. Un echec du post lui-meme leve
                # avant cette ecriture, donc on n'affirme jamais un commentaire
                # qui n'existe pas.
                markers.add(marker)
                state["closeout_markers"] = sorted(markers)
                write_mission_state(config, task_key, state)
            label_ids = _labels_after_build(client, issue, labels, LABEL_REVIEW)
            client.update_issue(
                issue.id, label_ids=label_ids, state_id=states.get(STATE_REVIEW)
            )
        if state or issue:
            state["closeout_markers"] = sorted(markers) if issue else state.get("closeout_markers", [])
            state["closed_out"] = True
            write_mission_state(config, task_key, state)
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
            f"💾 Boucle en pause — il ne reste que {free_disk / 1024**3:.1f} Go "
            "sur le disque.\nChaque mission a besoin de place pour travailler, "
            "donc je ne démarre rien tant qu'il n'y en a pas davantage.\n"
            "\n👉 Que faire : libère quelques Go, la boucle repartira toute seule."
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

        prior = _prior_missions(kanban, conn, config, issue.key)
        if prior:
            _hold_for_confirmation(config, client, issue, labels, prior, report)
            continue

        report.started.append({"issue": issue.key, "coder": coder, "branch": branch})
        if not config.apply:
            continue

        worktree = ensure_mission_worktree(
            repo, Path(config.worktrees_root) / f"agent-{issue.key.lower()}", branch
        )
        record_mission(config, issue, repo=repo, branch=branch, worktree=worktree)
        attempt = _attempt_number(kanban, conn, issue.key)
        task_id = kanban.create_task(
            conn,
            title=f"{issue.key} — {issue.title}",
            body=build_brief(issue, branch=branch, repo=str(worktree)),
            assignee=coder,
            created_by=LOOP_AUTHOR,
            workspace_kind="dir",
            workspace_path=str(worktree),
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


def mission_state_path(config, key: str) -> Path:
    return Path(config.hermes_home) / "linear-loop" / f"{key}.json"


def write_mission_state(config, key: str, state: dict[str, Any]) -> None:
    path = mission_state_path(config, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


def record_mission(config, issue: Issue, *, repo: str, branch: str, worktree: Path) -> None:
    """Note d'ou part la mission, pour pouvoir juger la suite sans deviner."""
    write_mission_state(config, issue.key, {
        "issue": issue.key,
        "repo": repo,
        "branch": branch,
        "worktree": str(worktree),
        "base": git_output(Path(repo), "rev-parse", "HEAD"),
    })


def record_verdict(config, key: str, verdict: str, for_sha: str) -> None:
    """Producteur canonique du couple (verdict, SHA exact) lu par le closeout.

    Un verdict de revue n'a de sens que rattache a l'identifiant complet du
    commit juge : c'est ce qui permet au tick de refuser tout verdict devenu
    perime ou ambigu. On refuse donc d'ecrire un rattachement invalide plutot
    que de laisser le closeout le decouvrir trop tard.
    """
    verdict_value = (verdict or "").strip()
    if not verdict_value:
        raise LoopError("verdict vide : rien a enregistrer")
    sha = (for_sha or "").strip().lower()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise LoopError(
            f"for_sha doit etre le SHA complet (40 hexa) du commit juge, recu {for_sha!r}"
        )
    state = read_mission(config, key)
    if state is None:
        raise LoopError(f"aucune mission enregistree pour {key} : verdict orphelin refuse")
    state["verdict"] = verdict_value
    state["for_sha"] = sha
    write_mission_state(config, key, state)


def read_mission(config, key: str) -> Optional[dict[str, Any]]:
    path = mission_state_path(config, key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return None


def branch_was_merged(repo: Path, branch: str, base: str) -> bool:
    """Vrai seulement si la branche a produit du travail ET qu'il est dans `main`.

    Sans la condition « a produit du travail », une branche creee puis abandonnee
    sans le moindre commit est mecaniquement un ancetre de `main` : l'issue
    serait fermee alors que rien n'a ete fait.
    """
    head = git_output(repo, "rev-parse", "--verify", "--quiet", branch)
    if not head or (base and head == base):
        return False
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", branch, "main"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    ).returncode == 0


def _prior_missions(kanban, conn, config, key: str) -> list[str]:
    """Preuves qu'un travail a deja abouti sur cette issue.

    HER-95 portait dix cartes terminees et decrivait un travail deja fait : rien
    ne l'aurait signale. Deux sources exactes, jamais une devinette : une mission
    de la boucle qui a ete cloturee, et toute carte `done` — y compris celles
    posees a la main bien avant la boucle. Une mission simplement bloquee puis
    rangee ne compte pas : rien n'a abouti.
    """
    evidence = []
    state = read_mission(config, key) or {}
    if state.get("closed_out"):
        evidence.append("mission autonome déjà clôturée")
    evidence.extend(
        f"carte {task.id}"
        for task in kanban.list_tasks(conn, status="done")
        if issue_key_of_title(task.title) == key
    )
    return evidence


def _hold_for_confirmation(config, client, issue, labels, prior, report) -> None:
    """Suspend une issue deja traitee au lieu de la relancer en silence."""
    fois = "une fois" if len(prior) == 1 else f"{len(prior)} fois"
    message = (
        f"⚠️ {issue.key} — {short_title(issue.title)}\n"
        f"Cette issue a déjà été traitée {fois}. Je ne la relance pas tout seul, "
        "pour éviter de refaire un travail existant.\n"
        f"\n👉 Que faire : soit tu la fermes parce que c'est fait, soit tu précises "
        f"ce qu'il reste et tu retires le label `{LABEL_BLOCKED}`.\n"
        f"🔗 {issue.url}"
    )
    report.skipped[issue.key] = f"déjà traitée {fois}"
    report.messages.append(message)
    if not config.apply:
        return
    client.add_comment(
        issue.id,
        f"Boucle en attente : {len(prior)} mission(s) autonome(s) ont déjà été menées "
        "sur cette issue. Reprise suspendue pour éviter de refaire un travail existant. "
        f"Retirer `{LABEL_BLOCKED}` pour relancer.",
    )
    client.update_issue(issue.id, label_ids=_labels_after_build(
        client, issue, labels, LABEL_BLOCKED
    ))


def _report_blocked(config, client, kanban, conn, by_key, labels, report) -> None:
    """Fait remonter les missions bloquees — sinon elles restent invisibles.

    Un worker qui bloque a fait ce qu'on lui demande : il attend une decision.
    Sans ce relais, l'issue reste eternellement « en cours » et personne ne le
    sait. Le label sert de marqueur d'idempotence : on ne signale qu'une fois.
    """
    for task in kanban.list_tasks(conn, status="blocked"):
        if task.created_by != LOOP_AUTHOR:
            continue
        key = issue_key_of_title(task.title)
        issue = by_key.get(key)
        if issue is None or LABEL_BLOCKED in issue.labels:
            continue
        reason = (kanban.latest_summary(conn, task.id) or task.result or "").strip()
        report.messages.append(
            f"⛔ {key} — {short_title(issue.title)}\n"
            f"{coder_label(task.assignee)} s'est arrêté et attend une décision de ta part.\n"
            f"\n💬 Ce qu'il dit : {quote(reason) or 'il n’a pas donné de raison.'}\n"
            f"\n👉 Que faire : précise ce qui manque dans l'issue, puis retire le "
            f"label `{LABEL_BLOCKED}` pour qu'il reprenne.\n"
            f"🔗 {issue.url}"
        )
        report.skipped[key] = "mission bloquée"
        if not config.apply:
            continue
        client.add_comment(
            issue.id,
            f"Mission suspendue par `{task.assignee}`.\n\n{reason or '(aucune raison fournie)'}"
            "\n\nAucun code n'a été poussé. La mission reprendra quand le point "
            "bloquant sera levé.",
        )
        client.update_issue(issue.id, label_ids=_labels_after_build(
            client, issue, labels, LABEL_BLOCKED
        ))


def _close_merged(config, client, issues, labels, states, report) -> None:
    """Ferme les issues dont la branche est effectivement entree dans `main`.

    Le merge est le seul signal qui vaille : tant que le commit n'est pas dans la
    branche principale, le travail n'existe pas pour le reste du monde. On lit
    donc le depot, on ne fait confiance ni au statut de la carte ni au notre.
    """
    for issue in issues:
        if LABEL_REVIEW not in issue.labels:
            continue
        mission = read_mission(config, issue.key) or {}
        repo = Path(mission.get("repo") or repo_for_issue(issue, config.repo))
        branch = mission.get("branch") or branch_name_for(issue)
        if not branch_was_merged(repo, branch, mission.get("base", "")):
            continue

        head = git_output(repo, "rev-parse", "--short", branch)
        worktree = Path(
            mission.get("worktree")
            or Path(config.worktrees_root) / f"agent-{issue.key.lower()}"
        )
        done_state = states.get(STATE_DONE)
        report.closed.append({"issue": issue.key, "merged_at": head})
        report.messages.append(
            f"🎉 {issue.key} — {short_title(issue.title)}\n"
            "Fusionnée : j'ai fermé l'issue et libéré l'espace de travail."
            if done_state else
            f"🎉 {issue.key} est fusionnée, mais je ne trouve pas l'état "
            f"« {STATE_DONE} » dans cette équipe : ferme-la à la main."
        )
        if not config.apply:
            continue
        freed = remove_mission_worktree(str(repo), worktree)
        client.add_comment(
            issue.id,
            f"Fusionné dans `main` au commit `{head}`. Issue fermée automatiquement."
            + ("" if freed else "\n\nWorktree conservé : il contient encore des "
                               "modifications non commitées."),
        )
        keep = [labels[n] for n in issue.labels if n in labels and n != LABEL_REVIEW]
        client.update_issue(issue.id, label_ids=keep, state_id=done_state)
        mission_state_path(config, issue.key).unlink(missing_ok=True)


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
    parser.add_argument("command", choices=["tick", "status", "ensure-labels", "verdict"])
    parser.add_argument("--team", default=DEFAULT_TEAM)
    parser.add_argument("--coder", action="append", default=None)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--runtime", default=DEFAULT_REPO)
    parser.add_argument("--issue", default=None, help="cle Linear visee par `verdict`.")
    parser.add_argument(
        "--verdict", dest="verdict_value", default=None,
        help="verdict de revue a lier (ex. APPROVE).",
    )
    parser.add_argument(
        "--for-sha", dest="for_sha", default=None,
        help="SHA complet (40 hexa) du commit candidat juge par la revue.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="ecrire reellement (Linear + kanban). Sans ce drapeau, rien n'est mute.",
    )
    args = parser.parse_args(argv)
    config = build_config(args)

    try:
        if args.command == "verdict":
            # Producteur local du rattachement verdict<->SHA : aucun acces Linear.
            if not (args.issue and args.verdict_value and args.for_sha):
                parser.error("verdict exige --issue, --verdict et --for-sha")
            record_verdict(config, args.issue, args.verdict_value, args.for_sha)
            print(f"verdict {args.verdict_value} lié à {args.issue}@{args.for_sha}")
            return 0
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
