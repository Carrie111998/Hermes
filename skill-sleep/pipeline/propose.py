"""
skill-sleep — Stage 2: PROPOSE

Read task cards + current SKILL.md + rejected buffer, assemble an optimizer
prompt, call `omp -p` (muse-spark-1.2) to generate a bounded candidate diff,
and output candidate.diff + proposal.json.

Uses subprocess with list-form args (no shell injection).
Checks NINEROUTER_KEY exists (no value logged).
Scans diff for secret patterns before writing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow `python3 pipeline/propose.py` direct execution and `python3 -m pipeline.propose`
try:
    from lib.proposal import Proposal  # type: ignore
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from lib.proposal import Proposal  # type: ignore

# ── Constants ───────────────────────────────────────────────────────────────

DEFAULT_MODEL = "gpt-4o-mini"
MAX_ADDED_LINES = 30

# Secret patterns — if diff contains any, refuse to write and WARN
SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"sk-proj-[a-zA-Z0-9_-]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{30,}"),
    re.compile(r"ghs_[a-zA-Z0-9]{30,}"),
    re.compile(r"github_pat_[a-zA-Z0-9_]{50,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[bprs]-[0-9a-zA-Z-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

TEMPLATE_REL = Path("templates/propose_prompt.md")


# ── Secret guard ────────────────────────────────────────────────────────────


def contains_secret(text: str) -> tuple[bool, str]:
    """Return (True, pattern) if text matches a secret pattern."""
    for pat in SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            # only show pattern, never the matched secret
            return True, pat.pattern[:40]
    return False, ""


# ── Tasks loading ───────────────────────────────────────────────────────────


def load_tasks(tasks_path: str) -> dict[str, Any]:
    p = Path(tasks_path)
    if not p.exists():
        print(f"ERROR: tasks file not found: {tasks_path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid tasks.json: {e}", file=sys.stderr)
        sys.exit(1)
    return data


def build_tasks_summary(tasks_data: dict[str, Any]) -> str:
    tasks: list[dict[str, Any]] = tasks_data.get("tasks", [])
    if not tasks:
        return "(no task cards — skill may not need changes; propose minimal guardrails)"

    lines: list[str] = []
    for i, t in enumerate(tasks, 1):
        skill = t.get("skill_name", "?")
        req = (t.get("user_request") or "")[:600]
        evidence = t.get("friction_evidence") or []
        tool_calls = t.get("tool_calls", "?")
        lines.append(f"### Task {i} — skill: {skill}")
        lines.append(f"- user_request: {req}")
        lines.append(f"- friction_evidence:")
        for ev in evidence[:5]:
            lines.append(f"  - {str(ev)[:300]}")
        lines.append(f"- tool_calls: {tool_calls}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Skill loading ───────────────────────────────────────────────────────────


def resolve_skill_path(skill_arg: str | None, tasks_data: dict[str, Any]) -> str:
    if skill_arg:
        return skill_arg
    # infer from first task card's skill_name
    tasks: list[dict[str, Any]] = tasks_data.get("tasks", [])
    skill_name = ""
    if tasks:
        skill_name = str(tasks[0].get("skill_name") or "").strip()
    if skill_name and skill_name != "default":
        # try flat path first, then recursive glob (skills are nested by category)
        candidate = Path.home() / ".hermes" / "skills" / skill_name / "SKILL.md"
        if candidate.exists():
            return str(candidate)
        # recursive search: ~/.hermes/skills/**/skill_name/SKILL.md
        for p in (Path.home() / ".hermes" / "skills").rglob(f"{skill_name}/SKILL.md"):
            return str(p)
        # also try case-insensitive glob for common names
        for p in (Path.home() / ".hermes" / "skills").rglob("SKILL.md"):
            if p.parent.name.lower() == skill_name.lower():
                return str(p)
    # fallback: glob for hermes-agent anywhere under skills
    for p in (Path.home() / ".hermes" / "skills").rglob("hermes-agent/SKILL.md"):
        return str(p)
    return str(Path.home() / ".hermes" / "skills" / "hermes-agent" / "SKILL.md")


def load_skill_content(skill_path: str) -> str:
    p = Path(skill_path)
    if not p.exists():
        print(f"ERROR: SKILL.md not found: {skill_path}", file=sys.stderr)
        print("       Hint: pass --skill <path> to specify the target skill", file=sys.stderr)
        sys.exit(1)
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        print(f"ERROR: cannot read SKILL.md: {e}", file=sys.stderr)
        sys.exit(1)


# ── Rejected buffer ─────────────────────────────────────────────────────────


def load_rejected_context(rejected_dir: str | None) -> str:
    if not rejected_dir:
        # auto-discover: <repo>/rejected or <cwd>/rejected
        candidates = [
            Path(__file__).resolve().parents[1] / "rejected",
            Path.cwd() / "rejected",
        ]
        for c in candidates:
            if c.is_dir():
                rejected_dir = str(c)
                break
        if not rejected_dir:
            return "(none — no prior rejected edits)"
    p = Path(rejected_dir)
    if not p.is_dir():
        return "(none — rejected dir not found)"

    parts: list[str] = []
    # .jsonl first (structured), then .diff
    jsonl_files = sorted(p.glob("*.jsonl"))
    diff_files = sorted(p.glob("*.diff"))[:5]

    for jf in jsonl_files[:3]:
        try:
            lines = jf.read_text(encoding="utf-8").splitlines()
            for line in lines[:5]:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    diff = str(obj.get("diff", "") or obj.get("candidate_diff", ""))[:1200]
                    reason = str(obj.get("reason", "") or obj.get("rejected_reason", ""))[:400]
                    if diff:
                        parts.append(f"- rejected diff ({jf.name}):\n```diff\n{diff[:800]}\n```\n  reason: {reason or 'validation gate failed'}")
                except json.JSONDecodeError:
                    parts.append(f"- {jf.name}: {line[:400]}")
        except OSError:
            continue

    for df in diff_files:
        try:
            content = df.read_text(encoding="utf-8")[:1200]
            if content.strip():
                parts.append(f"- rejected file {df.name}:\n```diff\n{content[:800]}\n```")
        except OSError:
            continue

    if not parts:
        return "(none — rejected dir empty)"
    # cap total length
    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)"
    return text


# ── Prompt rendering ────────────────────────────────────────────────────────


def render_prompt(
    template_path: str | Path,
    skill_content: str,
    tasks_summary: str,
    rejected_context: str,
) -> str:
    p = Path(template_path)
    if not p.exists():
        print(f"ERROR: prompt template not found: {template_path}", file=sys.stderr)
        sys.exit(1)
    template = p.read_text(encoding="utf-8")
    # Jinja2-style {placeholder} — simple str replace (no eval)
    result = template.replace("{skill_content}", skill_content[:12000])
    result = result.replace("{tasks_summary}", tasks_summary[:8000])
    result = result.replace("{rejected_context}", rejected_context[:5000])
    return result


def extract_diff_and_meta(omp_output: str) -> tuple[str, str, list[str]]:
    """Parse omp output: extract unified diff + summary + focused_on."""
    text = omp_output.strip()

    # summary: first "summary: ..." line
    summary = ""
    m = re.search(r"^summary\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    if m:
        summary = m.group(1).strip()[:2000]

    focused_on: list[str] = []
    for fm in re.finditer(r"^focused_on\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE):
        focused_on.append(fm.group(1).strip()[:400])

    # diff: from first "--- " or "diff --git" to end; prefer fenced block
    diff = ""

    # 1) fenced ```diff ... ```
    fence = re.search(r"```diff\s*\n(.*?)\n```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
        if "---" in candidate or "+++" in candidate or "@@" in candidate:
            diff = candidate

    # 2) fallback: raw diff header search
    if not diff:
        # find first diff header line
        header = re.search(r"(^---\s+[^\n]+\n\+\+\+.*)", text, re.MULTILINE | re.DOTALL)
        if header:
            diff = header.group(1).strip()
        else:
            # last resort: any @@ hunk
            hunk = re.search(r"(@@.*)", text, re.DOTALL)
            if hunk:
                diff = hunk.group(1).strip()

    if diff and not diff.endswith("\n"):
        diff += "\n"

    return diff, summary, focused_on


def count_added_lines(diff: str) -> int:
    count = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            count += 1
    return count


def is_valid_diff(diff: str) -> bool:
    if not diff.strip():
        return False
    has_header = ("---" in diff and "+++" in diff) or "@@" in diff
    return has_header


# ── omp call ─────────────────────────────────────────────────────────────────


def call_omp(
    prompt_path: Path,
    workdir: str,
    model: str,
    timeout: int,
) -> str:
    """Call `omp -p --cwd <workdir> --model <model> @prompt` and return stdout."""
    cmd = [
        "omp",
        "-p",
        "--cwd",
        workdir,
        "--model",
        model,
        f"@{prompt_path}",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HERMES_NO_COLOR": "1"},
        )
    except FileNotFoundError:
        print("ERROR: 'omp' not found in PATH", file=sys.stderr)
        print("       Install: npm i -g @mariozechner/pi-coding-agent  (provides `omp`)", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"ERROR: omp timed out after {timeout}s", file=sys.stderr)
        sys.exit(1)

    # omp prints to stdout; combine stdout+stderr for robustness
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        # omp may still have produced useful output; warn but continue
        print(f"WARN: omp exited {proc.returncode}: {(proc.stderr or '')[:500]}", file=sys.stderr)

    if not output.strip():
        print("ERROR: omp produced no output", file=sys.stderr)
        sys.exit(1)

    return output


# ── Output writing ───────────────────────────────────────────────────────────


def write_candidate_diff(diff: str, output_dir: str) -> str:
    hit, pat = contains_secret(diff)
    if hit:
        print(f"WARN: candidate diff contains secret-like pattern ({pat}) — refusing to write", file=sys.stderr)
        print("      Fix the skill or prompt to avoid emitting secrets.", file=sys.stderr)
        sys.exit(1)

    added = count_added_lines(diff)
    if added > MAX_ADDED_LINES:
        print(f"WARN: diff has {added} added lines, exceeds budget {MAX_ADDED_LINES} — still writing but gate may reject", file=sys.stderr)

    out = Path(output_dir) / "candidate.diff"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(diff, encoding="utf-8")
    return str(out)


def write_proposal_json(
    proposal: Proposal,
    output_dir: str,
) -> str:
    out = Path(output_dir) / "proposal.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(proposal.to_json(indent=2) + "\n", encoding="utf-8")
    return str(out)


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="skill-sleep PROPOSE: generate bounded candidate diff via omp optimizer")
    p.add_argument("--tasks", required=False, default=None, help="Path to tasks.json (from MINE stage)")
    p.add_argument("--skill", required=False, default=None, help="Path to target SKILL.md (default: inferred from task card)")
    p.add_argument("--output-dir", required=False, default=".", help="Output directory for candidate.diff + proposal.json")
    p.add_argument("--rejected-dir", required=False, default=None, help="Rejected buffer dir (default: ./rejected if exists)")
    p.add_argument("--model", required=False, default=DEFAULT_MODEL, help=f"omp model (default: {DEFAULT_MODEL})")
    p.add_argument("--template", required=False, default=None, help="Prompt template path (default: templates/propose_prompt.md)")
    p.add_argument("--timeout", type=int, default=300, help="Timeout for omp call in seconds (default: 300)")
    p.add_argument("--dry-run", action="store_true", help="Skip omp call; emit placeholder diff for testing")
    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    # --help already handled; check key only for real runs
    if not args.dry_run:
        if not os.environ.get("NINEROUTER_KEY"):
            print("ERROR: NINEROUTER_KEY not set", file=sys.stderr)
            print("      omp (muse-spark) requires NINEROUTER_KEY in env.", file=sys.stderr)
            print("      Export it or source ~/.zshenv:  export NINEROUTER_KEY=...", file=sys.stderr)
            sys.exit(1)

    # Resolve tasks path: default <output-dir>/tasks.json or ./tasks.json
    tasks_path = args.tasks
    if not tasks_path:
        # try output-dir/tasks.json then ./tasks.json
        for cand in [Path(args.output_dir) / "tasks.json", Path("tasks.json")]:
            if cand.exists():
                tasks_path = str(cand)
                break
        if not tasks_path:
            print("ERROR: --tasks is required (no tasks.json found)", file=sys.stderr)
            sys.exit(1)

    print(f"[propose] Loading tasks from {tasks_path} ...")
    tasks_data = load_tasks(tasks_path)
    total_cards = int(tasks_data.get("total_cards", len(tasks_data.get("tasks", []))))
    print(f"[propose] Got {total_cards} task card(s)")

    skill_path = resolve_skill_path(args.skill, tasks_data)
    print(f"[propose] Target skill: {skill_path}")

    skill_content = load_skill_content(skill_path)
    print(f"[propose] Skill content: {len(skill_content)} chars")

    rejected_context = load_rejected_context(args.rejected_dir)
    print(f"[propose] Rejected context: {len(rejected_context)} chars")

    tasks_summary = build_tasks_summary(tasks_data)

    template_path = args.template or str(Path(__file__).resolve().parents[1] / TEMPLATE_REL)
    prompt_text = render_prompt(template_path, skill_content, tasks_summary, rejected_context)
    print(f"[propose] Prompt assembled: {len(prompt_text)} chars")

    # Call optimizer
    omp_output = ""
    if args.dry_run:
        print("[propose] Dry-run: skipping omp call, using placeholder diff")
        omp_output = (
            "summary: 补充分支部署与远程路径检查的 pitfalls\n"
            "focused_on: pitfall: 远程部署不要直接写 /home/momo，先检查目标路径\n"
            "focused_on: rule: scp 前用 ssh 检查远端目录是否存在\n"
            "```diff\n"
            "--- a/SKILL.md\n"
            "+++ b/SKILL.md\n"
            "@@ -10,6 +10,14 @@\n"
            " ## Pitfalls\n"
            "+- 远程部署前先 ssh 检查目标路径是否存在，避免写到只读挂载点\n"
            "+- scp 后删除 .git，避免把本地 git 历史传到服务器\n"
            "+- Dockerfile 使用 node:20-alpine，EXPOSE 与端口映射保持一致\n"
            "```\n"
        )
    else:
        with tempfile.TemporaryDirectory(prefix="skill-sleep-propose-") as tmpdir:
            prompt_path = Path(tmpdir) / "prompt.md"
            prompt_path.write_text(prompt_text, encoding="utf-8")
            print(f"[propose] Calling omp --model {args.model} (timeout {args.timeout}s) ...")
            omp_output = call_omp(prompt_path, tmpdir, args.model, args.timeout)
            print(f"[propose] omp output: {len(omp_output)} chars")

    diff, summary, focused_on = extract_diff_and_meta(omp_output)

    if not diff or not is_valid_diff(diff):
        print("ERROR: could not extract a valid unified diff from omp output", file=sys.stderr)
        print("       Raw output (first 2000 chars):", file=sys.stderr)
        print(omp_output[:2000], file=sys.stderr)
        sys.exit(1)

    added = count_added_lines(diff)
    print(f"[propose] Extracted diff: {added} added lines")

    if not summary:
        summary = "优化器生成的有界编辑（补 pitfalls / 规则）"
    if not focused_on:
        focused_on = ["bounded edit: add pitfalls / guardrails"]

    diff_path = write_candidate_diff(diff, args.output_dir)
    print(f"[propose] Wrote {diff_path}")

    proposal = Proposal(
        generated_at=Proposal.now_iso(),
        skill_path=skill_path,
        source_task_cards=total_cards,
        diff_lines=added,
        summary=summary,
        focused_on=focused_on,
    )
    json_path = write_proposal_json(proposal, args.output_dir)
    print(f"[propose] Wrote {json_path}")
    print(f"[propose] Done — proposal: {proposal}")
