"""CLI presentation for the skill-lint subsystem."""

from __future__ import annotations

from pathlib import Path

from hermes_cli.skills.lint import lint_skill_body, split_skill_document
from hermes_cli.skills.lint_log import category_stats, list_findings


def lint_check(file_path: str | Path) -> None:
    path = Path(file_path).expanduser()
    content = path.read_text(encoding="utf-8")
    _frontmatter, body = split_skill_document(content)
    result = lint_skill_body(body)
    if not result.findings:
        print(f"{path}: clean")
        return
    print(f"{path}: {len(result.findings)} finding(s)")
    for finding in result.findings:
        print(
            f"line {finding.line_number}: {finding.category} "
            f"[{finding.pattern_label}] {finding.matched_text!r} -> "
            f"{finding.replacement}"
        )


def lint_log(
    *,
    skill_name: str | None = None,
    category: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> None:
    rows = list_findings(
        skill_name=skill_name,
        category=category,
        since=since,
        limit=limit,
    )
    if not rows:
        print("No skill-lint rows.")
        return
    print("ID  Timestamp             Skill               Category             Source      Line  Match")
    for row in rows:
        matched = str(row["matched_text"]).replace("\n", "\\n")
        write_source = str(row["write_source"] or "")
        line_number = "" if row["line_number"] is None else str(row["line_number"])
        print(
            f"{row['id']:>2}  {row['ts'][:20]:20}  "
            f"{row['skill_name'][:19]:19}  {row['category'][:19]:19}  "
            f"{write_source[:10]:10}  {line_number:>4}  {matched}"
        )


def lint_stats() -> None:
    rows = category_stats(days=7)
    print("Skill lint findings — last 7 days")
    if not rows:
        print("No skill-lint rows.")
        return
    for row in rows:
        print(f"{row['category']}: {row['count']}")


__all__ = ["lint_check", "lint_log", "lint_stats"]
