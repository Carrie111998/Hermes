"""Reference-style routing and evidence brief helpers for Fusion.

These helpers deliberately collect raw, bounded repository evidence in the host
process before equal-peer participants start reasoning. They do not execute
project code or modify the repository.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .context import FusionContext
from .models import FusionParticipantResult, FusionRequest

BUG_HINTS = (
    "bug", "broken", "breaks", "failing", "fails", "failure", "error", "exception",
    "traceback", "regression", "flaky", "wrong", "incorrect", "slow", "timeout",
    "root cause", "why is", "doesn't work", "not working", "не работает", "слом",
    "ошиб", "падает", "баг", "регресс", "флейк", "медлен", "почему",
)

DESIGN_HINTS = (
    "plan", "design", "architect", "implement", "add", "build", "review", "recommend",
    "план", "спроект", "реализ", "добав", "сдел", "архитект", "рекоменд",
)

SENSITIVE_PATH_HINTS = (
    ".env", "secret", "secrets", "credential", "credentials", "token", "tokens",
    "private_key", "id_rsa", "oauth", "cookie", "cookies",
)

DOC_CANDIDATES = (
    "AGENTS.md",
    "README.md",
    "CONTEXT.md",
    "CONCEPTS.md",
    "STRATEGY.md",
    "ARCHITECTURE.md",
    "docs/README.md",
    "docs/architecture.md",
    "docs/design.md",
)


def _safe_run_git(repo_root: str | None, args: list[str], *, timeout: int = 10) -> tuple[int, str, str]:
    if not repo_root:
        return 1, "", "repo root unavailable"
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive around platform git failures
        return 1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _looks_sensitive(path: str) -> bool:
    lower = path.lower()
    return any(hint in lower for hint in SENSITIVE_PATH_HINTS)


def classify_fusion_task(task: str) -> dict[str, Any]:
    """Classify a request into the reference routing buckets.

    The reference repo defaults unknown malfunction-shaped tasks to LOCATE before
    planning. This heuristic is intentionally conservative for bug language while
    keeping ordinary feature/design requests on the wide-solution path.
    """

    text = (task or "").strip().lower()
    bug_hits = [hint for hint in BUG_HINTS if hint in text]
    design_hits = [hint for hint in DESIGN_HINTS if hint in text]
    if bug_hits:
        kind = "bug_unknown_root"
        locate_required = True
        rationale = [f"bug-shaped hint: {hit}" for hit in bug_hits[:5]]
    elif design_hits:
        kind = "design_wide_solution"
        locate_required = False
        rationale = [f"design-shaped hint: {hit}" for hit in design_hits[:5]]
    else:
        kind = "design_wide_solution"
        locate_required = False
        rationale = ["no malfunction/root-cause hint detected; treating as design/wide-solution"]
    return {
        "task_kind": kind,
        "locate_required": locate_required,
        "rationale": rationale,
    }


def _repo_tree(repo_root: str | None, *, limit: int = 240) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    code, out, err = _safe_run_git(repo_root, ["ls-files"], timeout=15)
    if code != 0:
        notes.append(f"git ls-files unavailable: {err or out or 'unknown error'}")
        return [], notes
    paths = [line for line in out.splitlines() if line and not _looks_sensitive(line)]
    omitted = max(0, len(paths) - limit)
    if omitted:
        notes.append(f"repo tree truncated: {omitted} additional tracked paths omitted")
    return paths[:limit], notes


def _read_repo_doc(repo_root: str | None, rel_path: str, *, max_chars: int = 5000) -> dict[str, str] | None:
    if not repo_root or _looks_sensitive(rel_path):
        return None
    path = Path(repo_root) / rel_path
    try:
        resolved = path.resolve()
        root = Path(repo_root).resolve()
        if not (resolved == root or resolved.is_relative_to(root)):
            return None
        if not resolved.is_file():
            return None
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip() + "\n...[truncated]"
    return {"path": rel_path, "content": text, "truncated": str(truncated).lower()}


def _layer_manifest(task: str, tree: list[str], routing: dict[str, Any]) -> dict[str, Any]:
    lower_paths = [p.lower() for p in tree]
    layer_patterns = {
        "ui_client": ("ui/", "frontend", "client", "web/", "src/components", "react"),
        "cli_gateway": ("cli.py", "gateway/", "hermes_cli/", "commands.py", "slash"),
        "orchestration": ("agent/", "orchestrator", "workflow", "runner"),
        "tooling": ("tools/", "toolsets", "registry"),
        "config": ("config", "settings", ".yaml", ".toml"),
        "tests": ("tests/", "test_", "spec"),
        "docs": ("docs/", "readme", "architecture", "design"),
        "protocol_schema_substrate": ("schema", "protocol", "codec", "migration", "db/", "models/"),
    }
    covered: list[str] = []
    for layer, hints in layer_patterns.items():
        if any(any(hint in path for hint in hints) for path in lower_paths):
            covered.append(layer)
    required = ["docs", "tests", "orchestration"]
    if routing.get("locate_required"):
        required.extend(["protocol_schema_substrate", "config"])
    not_covered = sorted(layer for layer in required if layer not in covered)
    return {
        "covered": sorted(set(covered)),
        "not_covered": not_covered,
        "coverage_basis": "tracked path names; participants must verify specific claims with read/search tools",
    }


def _summarize_phase(results: list[FusionParticipantResult] | None, *, limit: int = 2400) -> str:
    if not results:
        return ""
    chunks: list[str] = []
    for result in results:
        text = (result.output or result.error or "").strip()
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n...[truncated]"
        chunks.append(f"### {result.spec.slug} ({result.phase})\n{text or '<no output>'}")
    return "\n\n".join(chunks)


def build_reference_brief(
    request: FusionRequest,
    context: FusionContext,
    *,
    routing: dict[str, Any] | None = None,
    locate_results: list[FusionParticipantResult] | None = None,
) -> dict[str, Any]:
    """Build the raw evidence packet every reasoning participant sees."""

    routing = dict(routing or classify_fusion_task(request.task))
    repo_root = context.repo_root
    notes = list(context.notes)
    code, head, err = _safe_run_git(repo_root, ["rev-parse", "--short", "HEAD"])
    if code != 0:
        notes.append(f"git head unavailable: {err or head or 'unknown error'}")
        head = "unknown"
    code, log, err = _safe_run_git(repo_root, ["log", "--oneline", "-5"])
    if code != 0:
        notes.append(f"recent git log unavailable: {err or log or 'unknown error'}")
        log = ""
    tree, tree_notes = _repo_tree(repo_root)
    notes.extend(tree_notes)
    docs = [doc for rel in DOC_CANDIDATES if (doc := _read_repo_doc(repo_root, rel))]
    layer_manifest = _layer_manifest(request.task, tree, routing)
    locate_summary = _summarize_phase(locate_results)
    brief = {
        "schema": "fusion-reference-brief/v1",
        "task": request.task,
        "mode": request.mode,
        "repo_root": repo_root,
        "cwd": context.cwd,
        "git_head": head,
        "recent_log": log.splitlines() if log else [],
        "routing": routing,
        "repo_tree": tree,
        "docs": docs,
        "layers": layer_manifest,
        "locate_summary": locate_summary,
        "notes": notes,
    }
    brief["markdown"] = brief_to_markdown(brief)
    return brief


def brief_to_markdown(brief: dict[str, Any] | None) -> str:
    if not brief:
        return "# Fusion Evidence Brief\n\nNo brief was built.\n"
    lines: list[str] = [
        "# Fusion Evidence Brief",
        "",
        "This is raw, bounded context for equal-peer Fusion participants. It is not a final plan.",
        "",
        "## Task",
        str(brief.get("task") or ""),
        "",
        "## Routing",
        f"- task_kind: `{brief.get('routing', {}).get('task_kind', 'unknown')}`",
        f"- locate_required: `{brief.get('routing', {}).get('locate_required', False)}`",
    ]
    rationale = brief.get("routing", {}).get("rationale") or []
    for item in rationale:
        lines.append(f"- rationale: {item}")
    lines.extend([
        "",
        "## Repository Stamp",
        f"- repo_root: `{brief.get('repo_root') or '<unavailable>'}`",
        f"- cwd: `{brief.get('cwd') or '<unknown>'}`",
        f"- git_head: `{brief.get('git_head') or 'unknown'}`",
        "",
        "## Recent Git Log",
    ])
    recent = brief.get("recent_log") or []
    lines.extend(f"- `{line}`" for line in recent[:8])
    if not recent:
        lines.append("- <unavailable>")
    lines.extend(["", "## Layers covered / Layers NOT covered"])
    layers = brief.get("layers") or {}
    lines.append("- covered: " + (", ".join(layers.get("covered") or []) or "none detected"))
    lines.append("- not_covered: " + (", ".join(layers.get("not_covered") or []) or "none flagged"))
    lines.append(f"- basis: {layers.get('coverage_basis') or 'unknown'}")
    lines.extend(["", "## Repository Tree (bounded)"])
    tree = brief.get("repo_tree") or []
    lines.extend(f"- `{path}`" for path in tree[:240])
    if not tree:
        lines.append("- <unavailable>")
    docs = brief.get("docs") or []
    if docs:
        lines.extend(["", "## Repo Guidance Docs (bounded excerpts)"])
        for doc in docs:
            lines.extend([f"### {doc.get('path')}", "```markdown", str(doc.get("content") or ""), "```", ""])
    locate_summary = brief.get("locate_summary") or ""
    if locate_summary:
        lines.extend(["", "## LOCATE Evidence Summary", locate_summary])
    notes = brief.get("notes") or []
    if notes:
        lines.extend(["", "## Notes"])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines).rstrip() + "\n"
