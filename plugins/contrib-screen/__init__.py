"""contrib-screen — bundled, auto-loaded.

Pre-flight checks and a searchable org index for open-source contribution
work, folded in as a native Hermes plugin rather than a separate
standalone repo the founder would otherwise have to clone and install on
top of the harness — see this directory's README.md for why, and
internal-docs/harness/2026-08-20-system-design.md §0.1 (in the private
MershLab/internal-docs repo, not this one) for the superseded framing
this replaces.

Registers four tools into the ``contrib_screen`` toolset:

- ``contrib_screen`` — screen one issue: duplicate PR, assignee, CLA gate
- ``contrib_screen_index`` — pull an org's issues/PRs/comments into a
  local searchable index
- ``contrib_screen_search`` — full-text search across an already-indexed
  org (the org-repo-interconnectivity building block)
- ``contrib_screen_voice`` — real merged PR text from an indexed org, for
  maintainer-voice calibration, not an AI-authorship detector

Everything reads/writes under ``$HERMES_HOME/contrib-screen/`` — the same
per-user state directory convention every other bundled plugin uses
(``disk-cleanup`` writes to ``$HERMES_HOME/disk-cleanup/``), not a
separate dotfile directory a second tool would have invented on its own.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from hermes_constants import get_hermes_home
from tools.registry import tool_error, tool_result

from .audit import append_record
from .checks import ScreenResult, Verdict, screen_issue
from .github import GitHubClient
from .index_store import IndexStore, default_db_path
from .org_index import sync_org

VERDICT_LABELS = {
    Verdict.CLEAR: "CLEAR - no blocker found",
    Verdict.DUPLICATE: "DUPLICATE - a PR already references this issue",
    Verdict.ASSIGNED: "ASSIGNED - someone is already on this",
    Verdict.CLA_REQUIRED: "CLA REQUIRED - sign before proceeding",
    Verdict.NOT_FOUND: "NOT FOUND - check the owner/repo/issue number",
}


def _parse_target(raw: str) -> tuple[str, str, int]:
    owner, _, rest = raw.partition("/")
    repo, _, number = rest.partition("#")
    if not owner or not repo or not number.isdigit():
        raise ValueError(f"expected owner/repo#issue, got {raw!r}")
    return owner, repo, int(number)


# ---------------------------------------------------------------------------
# contrib_screen
# ---------------------------------------------------------------------------

CONTRIB_SCREEN_SCHEMA = {
    "name": "contrib_screen",
    "description": (
        "Pre-flight check on one GitHub issue before starting contribution "
        "work: is there already a PR referencing it, is it assigned, does "
        "the repo require a CLA. Run this before implementing anything. "
        "Every check is appended to a local audit log."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "owner/repo#issue, e.g. facebook/react#12345",
            },
            "signed_orgs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "orgs whose CLA is already signed, e.g. [\"microsoft\"]",
            },
        },
        "required": ["target"],
    },
}


def _handle_contrib_screen(args: dict, **kw) -> str:
    try:
        owner, repo, number = _parse_target(str(args.get("target") or ""))
    except ValueError as exc:
        return tool_error(str(exc))

    signed_orgs = frozenset(o.lower() for o in (args.get("signed_orgs") or []))
    client = GitHubClient()
    result = screen_issue(owner, repo, number, client, known_signed_orgs=signed_orgs)
    record = result.to_dict()
    log_path = append_record(record)

    return tool_result({
        **record,
        "label": VERDICT_LABELS[result.verdict],
        "logged_to": str(log_path),
    })


# ---------------------------------------------------------------------------
# contrib_screen_index
# ---------------------------------------------------------------------------

CONTRIB_SCREEN_INDEX_SCHEMA = {
    "name": "contrib_screen_index",
    "description": (
        "Pull issues, PRs, and comments for specific repos in an org into a "
        "local searchable index, for contrib_screen_search and "
        "contrib_screen_voice. Always scope with repos — indexing an entire "
        "large org is slow and burns real API quota; per "
        "internal-docs/harness/org-awareness-and-voice-design.md, do a live "
        "GitHub code/issue search first, index only the repos that search "
        "surfaces as real candidates."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "org": {"type": "string", "description": "GitHub org or user, e.g. microsoft"},
            "repos": {
                "type": "array",
                "items": {"type": "string"},
                "description": "repo names to index, e.g. [\"vscode\", \"TypeScript\"]. Omit only when you deliberately want the whole org.",
            },
        },
        "required": ["org"],
    },
}


def _handle_contrib_screen_index(args: dict, **kw) -> str:
    org = str(args.get("org") or "").strip()
    if not org:
        return tool_error("org is required")
    repos = args.get("repos") or None
    client = GitHubClient()
    with IndexStore(default_db_path(org)) as store:
        results = sync_org(store, client, org, repos)
    return tool_result({
        "org": org,
        "repos_indexed": len(results),
        "issues_and_prs": sum(r["issues"] for r in results),
        "comments": sum(r["comments"] for r in results),
        "db_path": str(default_db_path(org)),
    })


# ---------------------------------------------------------------------------
# contrib_screen_search
# ---------------------------------------------------------------------------

CONTRIB_SCREEN_SEARCH_SCHEMA = {
    "name": "contrib_screen_search",
    "description": (
        "Full-text search across an org already indexed with "
        "contrib_screen_index — issues, PRs, and comments. Use before "
        "implementing a fix to check whether this symptom already has "
        "activity elsewhere in the org, not just in the one repo being "
        "worked on."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "org": {"type": "string", "description": "the org previously passed to contrib_screen_index"},
            "query": {"type": "string", "description": "search text — key symptom terms, not a full sentence"},
            "limit": {"type": "integer", "description": "default 20"},
        },
        "required": ["org", "query"],
    },
}


def _handle_contrib_screen_search(args: dict, **kw) -> str:
    org = str(args.get("org") or "").strip()
    query = str(args.get("query") or "").strip()
    if not org or not query:
        return tool_error("org and query are both required")
    limit = int(args.get("limit") or 20)
    db_path = default_db_path(org)
    if not db_path.exists():
        return tool_error(f"{org} isn't indexed yet — run contrib_screen_index first")
    with IndexStore(db_path) as store:
        results = store.search(query, limit=limit)
    return tool_result({"org": org, "query": query, "matches": results})


# ---------------------------------------------------------------------------
# contrib_screen_voice
# ---------------------------------------------------------------------------

CONTRIB_SCREEN_VOICE_SCHEMA = {
    "name": "contrib_screen_voice",
    "description": (
        "Real, recent merged PR titles and bodies from an org already "
        "indexed with contrib_screen_index — use as few-shot grounding "
        "when drafting a PR description for that org, so it reads like "
        "this org's own contributors write, not generically AI-authored. "
        "This is calibration, not an AI-text detector — per "
        "internal-docs/harness/org-awareness-and-voice-design.md, "
        "detection degrades as models change; real examples don't."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "org": {"type": "string", "description": "the org previously passed to contrib_screen_index"},
            "limit": {"type": "integer", "description": "default 10"},
        },
        "required": ["org"],
    },
}


def _handle_contrib_screen_voice(args: dict, **kw) -> str:
    org = str(args.get("org") or "").strip()
    if not org:
        return tool_error("org is required")
    limit = int(args.get("limit") or 10)
    db_path = default_db_path(org)
    if not db_path.exists():
        return tool_error(f"{org} isn't indexed yet — run contrib_screen_index first")
    with IndexStore(db_path) as store:
        prs = store.merged_prs(limit=limit)
    if not prs:
        return tool_error(f"no merged PRs found in the indexed data for {org}")
    return tool_result({
        "org": org,
        "examples": [
            {"repo": p["repo"], "number": p["number"], "title": p["title"], "body": p["body"]}
            for p in prs
        ],
    })


# ---------------------------------------------------------------------------
# contrib_screen_claim
# ---------------------------------------------------------------------------

CONTRIB_SCREEN_CLAIM_SCHEMA = {
    "name": "contrib_screen_claim",
    "description": (
        "Atomically claim an issue before starting real work on it, so two "
        "overlapping runs (an unattended sweep re-firing before the previous "
        "run's PR exists yet, or a founder-triggered run overlapping a "
        "scheduled one) can't both pick the same CLEAR issue at once. "
        "contrib_screen only catches an *existing* PR, not a race between "
        "two runs starting at the same moment — this closes that gap. "
        "Call this AFTER contrib_screen returns CLEAR, before starting "
        "github-issue-to-pr. A False result means another run already "
        "claimed it (treat like ASSIGNED); a True result means proceed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "owner/repo#issue, e.g. facebook/react#12345",
            },
            "ttl_hours": {
                "type": "number",
                "description": "how old a claim must be before it's considered stale and reclaimable (default 2 — the other run likely failed or was killed)",
            },
        },
        "required": ["target"],
    },
}


def _handle_contrib_screen_claim(args: dict, **kw) -> str:
    try:
        owner, repo, number = _parse_target(str(args.get("target") or ""))
    except ValueError as exc:
        return tool_error(str(exc))
    raw_ttl = args.get("ttl_hours")
    ttl_hours = float(raw_ttl) if raw_ttl is not None else 2.0

    claims_dir = get_hermes_home() / "contrib-screen" / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    claim_path = claims_dir / f"{owner}-{repo}-{number}.json"
    now = datetime.now(timezone.utc)
    record = json.dumps({"claimed_at": now.isoformat()})

    # Exclusive create is the real race guard: two processes hitting this at
    # the same instant, only one O_EXCL open can win.
    try:
        fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(record)
        return tool_result({"claimed": True, "target": str(args.get("target")), "claimed_at": now.isoformat()})
    except FileExistsError:
        pass

    # Something already claimed it — only a genuinely stale claim is
    # reclaimable. The replace below is atomic (os.replace), so this can't
    # corrupt the file; it can only, in a narrow window, let two callers both
    # decide a claim is stale and both replace it — acceptable, not claimed
    # to be perfectly race-free, only far better than no guard at all.
    try:
        existing = json.loads(claim_path.read_text())
        claimed_at = datetime.fromisoformat(existing["claimed_at"])
    except Exception:
        claimed_at = None

    if claimed_at is not None and (now - claimed_at) < timedelta(hours=ttl_hours):
        return tool_result({
            "claimed": False,
            "target": str(args.get("target")),
            "reason": "already claimed",
            "claimed_at": claimed_at.isoformat(),
        })

    tmp_path = claim_path.with_suffix(".tmp")
    tmp_path.write_text(record)
    os.replace(tmp_path, claim_path)
    return tool_result({
        "claimed": True,
        "target": str(args.get("target")),
        "claimed_at": now.isoformat(),
        "note": "replaced a stale claim",
    })


_TOOLS = (
    ("contrib_screen", CONTRIB_SCREEN_SCHEMA, _handle_contrib_screen, "🔍"),
    ("contrib_screen_index", CONTRIB_SCREEN_INDEX_SCHEMA, _handle_contrib_screen_index, "📇"),
    ("contrib_screen_search", CONTRIB_SCREEN_SEARCH_SCHEMA, _handle_contrib_screen_search, "🔎"),
    ("contrib_screen_voice", CONTRIB_SCREEN_VOICE_SCHEMA, _handle_contrib_screen_voice, "🗣️"),
    ("contrib_screen_claim", CONTRIB_SCREEN_CLAIM_SCHEMA, _handle_contrib_screen_claim, "🚩"),
)


def register(ctx) -> None:
    """Register all contrib-screen tools. Called once by the plugin loader."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="contrib_screen",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )
