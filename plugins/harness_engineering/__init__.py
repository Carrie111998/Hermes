"""Harness / Agenting Engineering Hermes plugin.

The plugin delegates intake-form operations to the bundled skill helper when the
repo ships it, while keeping the user-local ``~/.hermes/bin/hermes-harness``
helper as a compatibility fallback for existing profiles.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

HELP_TEXT = """Harness / Agenting Engineering intake

CLI:
  hermes harness template
  hermes harness classify --text "Fix this WebUI bug and add tests"
  hermes harness new --title "My task" --workspace /path/to/repo --mode "Implement changes"
  hermes harness check /path/to/intake.md
  hermes harness prompt /path/to/intake.md

Helper resolution:
  bundled skill script first, then ~/.hermes/bin/hermes-harness

Purpose:
  Move non-trivial AI-assisted coding tasks from vibe coding to sustainable
  Harness / Agenting Engineering by requiring task scope, acceptance criteria,
  risk surface, and verification evidence before implementation.
""".strip()


class TaskClassification:
    """Advisory task routing result for Harness preflight and CLI use."""

    def __init__(
        self,
        *,
        task_type: str,
        harness_required: bool,
        risk_level: str,
        route: str,
        signals: list[str] | None = None,
        recommended_next_steps: list[str] | None = None,
    ) -> None:
        self.task_type = task_type
        self.harness_required = harness_required
        self.risk_level = risk_level
        self.route = route
        self.signals = signals or []
        self.recommended_next_steps = recommended_next_steps or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "harness_required": self.harness_required,
            "risk_level": self.risk_level,
            "route": self.route,
            "signals": self.signals,
            "recommended_next_steps": self.recommended_next_steps,
        }


def _bundled_helper_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "software-development"
        / "harness-agenting-engineering"
        / "scripts"
        / "harness_intake.py"
    )


def _user_helper_path() -> Path:
    return Path.home() / ".hermes" / "bin" / "hermes-harness"


def _helper_command() -> list[str] | None:
    bundled = _bundled_helper_path()
    if bundled.exists():
        return [os.environ.get("PYTHON", "python3"), str(bundled)]
    user_helper = _user_helper_path()
    if user_helper.exists():
        return [str(user_helper)]
    return None


def _run_helper(argv: Sequence[str]) -> int:
    command = _helper_command()
    if command is None:
        print("Missing Harness intake helper.")
        print(f"Expected bundled script: {_bundled_helper_path()}")
        print(f"Or user-local helper: {_user_helper_path()}")
        return 2
    proc = subprocess.run([*command, *argv], check=False)
    return int(proc.returncode)


def _setup_harness_cli(parser) -> None:
    sub = parser.add_subparsers(dest="harness_action")

    template_p = sub.add_parser("template", help="Print or copy the Harness task intake template")
    template_p.add_argument("--output", "-o", default="", help="Copy template to this path instead of stdout")

    classify_p = sub.add_parser("classify", help="Classify a task and print advisory Harness routing")
    classify_p.add_argument("--text", "-t", default="", help="Task text to classify")
    classify_p.add_argument("--format", choices=("markdown", "json"), default="markdown", help="Output format")

    new_p = sub.add_parser("new", help="Create a new Harness task intake form")
    new_p.add_argument("--title", default="", help="Task title")
    new_p.add_argument("--workspace", default="", help="Workspace / repo path")
    new_p.add_argument("--mode", default="", help="Desired mode / permission level")
    new_p.add_argument("--out", default="", help="Output path or directory")
    new_p.add_argument("--print-path", action="store_true", help="Print only the created file path")

    check_p = sub.add_parser("check", help="Validate a filled Harness intake form")
    check_p.add_argument("file", help="Intake markdown file")

    prompt_p = sub.add_parser("prompt", help="Render a compact prompt from a filled intake form")
    prompt_p.add_argument("file", help="Intake markdown file")
    prompt_p.add_argument("--allow-incomplete", action="store_true", help="Render even if required fields are missing")
    prompt_p.add_argument("--output", "-o", default="", help="Write prompt to this file instead of stdout")
    prompt_p.add_argument("--force", action="store_true", help="Overwrite output file if it exists")

    parser.set_defaults(func=_handle_harness_cli)


def _handle_harness_cli(args) -> None:
    action = getattr(args, "harness_action", None)
    if not action:
        print(HELP_TEXT)
        raise SystemExit(0)

    argv: list[str] = [action]
    if action == "classify":
        text = getattr(args, "text", "")
        if not text:
            print("Missing --text for harness classify.")
            raise SystemExit(2)
        classification = classify_task(text)
        if getattr(args, "format", "markdown") == "json":
            print(json.dumps(classification.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(_render_classification_markdown(classification))
        raise SystemExit(0)
    if action == "new":
        for flag in ("title", "workspace", "mode"):
            value = getattr(args, flag, "")
            if value:
                argv.extend([f"--{flag}", value])
        out_value = getattr(args, "out", "")
        if out_value:
            argv.extend(["--output", out_value])
        # The helper already prints the created path for `new`; keep
        # --print-path as a plugin-side compatibility flag without passing it
        # to the helper.
    elif action == "check":
        argv.append(getattr(args, "file"))
    elif action == "prompt":
        argv.append(getattr(args, "file"))
        if getattr(args, "allow_incomplete", False):
            argv.append("--allow-incomplete")
        output_value = getattr(args, "output", "")
        if output_value:
            argv.extend(["--output", output_value])
        if getattr(args, "force", False):
            argv.append("--force")
    elif action == "template":
        output_value = getattr(args, "output", "")
        if output_value:
            argv.extend(["--output", output_value])
    else:
        print(HELP_TEXT)
        raise SystemExit(2)
    raise SystemExit(_run_helper(argv))


ENGINEERING_KEYWORDS = re.compile(
    r"("
    r"修复|修 bug|bug|报错|异常|失败|不生效|调试|排查|"
    r"实现|开发|改代码|重构|优化|接入|集成|迁移|发布|部署|"
    r"测试|单测|pytest|CI|PR|代码审查|review|"
    r"feature|fix|debug|refactor|implement|integrat|migrat|deploy|release|test|coverage"
    r")",
    re.IGNORECASE,
)
RESEARCH_PATTERNS = re.compile(
    r"(调研|研究|查资料|搜索|对比|总结资料|文献|论文|market|research|search|compare|survey|literature)",
    re.IGNORECASE,
)
SMALL_CODE_PATTERNS = re.compile(
    r"(小修|简单修|typo|文案|样式|one[- ]?line|small fix|minor|quick fix|加测试|单测)",
    re.IGNORECASE,
)
LARGE_CODE_PATTERNS = re.compile(
    r"(重构|架构|迁移|端到端|全流程|跨模块|系统性|大规模|多文件|refactor|architecture|migration|end[- ]?to[- ]?end|cross[- ]?module|systematic)",
    re.IGNORECASE,
)
HIGH_RISK_PATTERNS = re.compile(
    r"(认证|授权|权限|密钥|凭证|token|secret|password|cookie|支付|license|删除|清空|覆盖|force[- ]?push|deploy|release|auth|oauth|credential|filesystem|webhook|database|migration)",
    re.IGNORECASE,
)
MULTI_AGENT_PATTERNS = re.compile(
    r"(多代理|子代理|并行|分解|派工|kanban|codex|claude|opencode|multi[- ]?agent|subagent|parallel|delegate|orchestrate)",
    re.IGNORECASE,
)
OPS_SCHEDULED_PATTERNS = re.compile(
    r"(cron|定时|周期|监控|告警|任务队列|scheduler|scheduled|monitor|daemon|gateway)",
    re.IGNORECASE,
)
LOW_RISK_PATTERNS = re.compile(
    r"^(怎么看|是什么|解释|总结|翻译|润色|写一段|生成文案|画|查一下|搜索|what is|explain|summarize|translate)\b",
    re.IGNORECASE,
)
HARNESS_ALREADY_PRESENT = re.compile(
    r"(Harness\s*/\s*Agenting|harness-agenting|hermes\s+harness|/intake|intake\s+form|验收标准|验证证据|risk surface)",
    re.IGNORECASE,
)

PREFLIGHT_NOTICE = """[Harness / Agenting Engineering preflight]\nThis appears to be a non-trivial engineering task. Before implementing, use the harness discipline:\n- define scope, acceptance criteria, risk surface, and rollback plan;\n- inspect the codebase before editing;\n- preserve tests / quality gates as evidence;\n- prefer reusable Skill/Rules/Plugin updates for repeated workflow.\nIf the task is underspecified, ask only for missing information that changes the implementation.\n\nOriginal user request:\n"""

STRICT_NOTICE = """[Harness / Agenting Engineering preflight: intake required]\nThis appears to be a high-risk engineering task. Ask the user to create or fill a Harness intake before implementation:\n  hermes harness new --title \"<task>\" --workspace \"<repo>\" --mode \"Implement changes\" --out /tmp/task-intake.md\n  hermes harness check /tmp/task-intake.md\nProceed only after scope, acceptance criteria, risk surface, and verification evidence are explicit.\n\nOriginal user request:\n"""


def _has(pattern: re.Pattern[str], text: str) -> bool:
    return bool(pattern.search(text))


def classify_task(text: str) -> TaskClassification:
    """Classify task text into advisory Harness routing buckets."""
    compact = (text or "").strip()
    if not compact:
        return TaskClassification(
            task_type="simple_chat",
            harness_required=False,
            risk_level="low",
            route="answer_directly",
            signals=["empty_text"],
            recommended_next_steps=["Ask for the task text before routing."],
        )
    if compact.startswith("/"):
        return TaskClassification(
            task_type="simple_chat",
            harness_required=False,
            risk_level="low",
            route="slash_command",
            signals=["slash_command"],
            recommended_next_steps=["Handle as an in-session command."],
        )

    signals: list[str] = []
    if _has(HARNESS_ALREADY_PRESENT, compact):
        signals.append("harness_context_present")
    if _has(ENGINEERING_KEYWORDS, compact):
        signals.append("engineering_keywords")
    if _has(RESEARCH_PATTERNS, compact):
        signals.append("research_keywords")
    if _has(SMALL_CODE_PATTERNS, compact):
        signals.append("small_code_keywords")
    if _has(LARGE_CODE_PATTERNS, compact):
        signals.append("large_code_keywords")
    if _has(HIGH_RISK_PATTERNS, compact):
        signals.append("high_risk_keywords")
    if _has(MULTI_AGENT_PATTERNS, compact):
        signals.append("multi_agent_keywords")
    if _has(OPS_SCHEDULED_PATTERNS, compact):
        signals.append("ops_scheduled_keywords")

    if _has(LOW_RISK_PATTERNS, compact) and len(compact) < 220 and "engineering_keywords" not in signals:
        return TaskClassification(
            task_type="simple_chat",
            harness_required=False,
            risk_level="low",
            route="answer_directly",
            signals=[*signals, "low_risk_prompt_shape"],
            recommended_next_steps=["Answer directly; no Harness intake needed."],
        )

    if "multi_agent_keywords" in signals:
        task_type = "multi_agent_project"
    elif "high_risk_keywords" in signals:
        task_type = "high_risk_change"
    elif "ops_scheduled_keywords" in signals:
        task_type = "ops_scheduled"
    elif "large_code_keywords" in signals:
        task_type = "large_code_change"
    elif "engineering_keywords" in signals:
        task_type = "small_code_change" if "small_code_keywords" in signals else "large_code_change"
    elif "research_keywords" in signals:
        task_type = "research"
    else:
        task_type = "simple_chat"

    if task_type in {"high_risk_change", "multi_agent_project", "ops_scheduled"}:
        return TaskClassification(
            task_type=task_type,
            harness_required=True,
            risk_level="high",
            route="intake_required",
            signals=signals or ["no_special_signal"],
            recommended_next_steps=[
                "Create or fill a Harness intake before implementation.",
                "Name risk surface, rollback plan, and verification evidence.",
            ],
        )
    if task_type == "large_code_change":
        return TaskClassification(
            task_type=task_type,
            harness_required=True,
            risk_level="medium",
            route="harness_advisory",
            signals=signals or ["no_special_signal"],
            recommended_next_steps=[
                "Define scope, acceptance criteria, risk surface, and tests before editing.",
                "Inspect project rules and touched subsystem contracts first.",
            ],
        )
    if task_type == "small_code_change":
        return TaskClassification(
            task_type=task_type,
            harness_required=False,
            risk_level="medium",
            route="bounded_engineering",
            signals=signals or ["no_special_signal"],
            recommended_next_steps=["Keep the change scoped and run focused tests before reporting done."],
        )
    if task_type == "research":
        return TaskClassification(
            task_type=task_type,
            harness_required=False,
            risk_level="low",
            route="research_then_report",
            signals=signals or ["no_special_signal"],
            recommended_next_steps=["Gather source evidence and report assumptions or gaps explicitly."],
        )
    return TaskClassification(
        task_type="simple_chat",
        harness_required=False,
        risk_level="low",
        route="answer_directly",
        signals=signals or ["no_special_signal"],
        recommended_next_steps=["Answer directly; no Harness intake needed."],
    )


def _render_classification_markdown(classification: TaskClassification) -> str:
    lines = [
        "## Harness Task Classification",
        "",
        f"Task type: `{classification.task_type}`",
        f"Risk level: `{classification.risk_level}`",
        f"Route: `{classification.route}`",
        f"Harness intake required: `{str(classification.harness_required).lower()}`",
        "",
        "Signals:",
    ]
    lines.extend(f"- `{signal}`" for signal in classification.signals)
    lines.extend(["", "Recommended next steps:"])
    lines.extend(f"- {step}" for step in classification.recommended_next_steps)
    return "\n".join(lines)


def _configured_preflight_mode() -> str:
    try:
        from hermes_cli.config import cfg_get, load_config

        value = cfg_get(load_config(), "harness_engineering", "preflight_mode", default="advisory")
    except Exception:
        value = "advisory"
    return str(value or "advisory").strip().lower()


def _preflight_mode() -> str:
    env_value = os.getenv("HERMES_HARNESS_PREFLIGHT")
    if env_value is not None:
        return env_value.strip().lower()
    return _configured_preflight_mode()


def _looks_like_engineering_task(text: str) -> bool:
    classification = classify_task(text)
    return classification.task_type in {
        "small_code_change",
        "large_code_change",
        "high_risk_change",
        "multi_agent_project",
        "ops_scheduled",
    }


def _handle_pre_gateway_dispatch(event: Any = None, **_: Any) -> dict[str, str] | None:
    """Soft Level-4 preflight for gateway messages.

    Modes via HERMES_HARNESS_PREFLIGHT:
      off/0/false/no  -> disabled
      advisory/warn/rewrite (default) -> prepend Harness discipline reminder
      strict -> prepend an intake-required instruction

    The hook returns only `allow` or `rewrite`; it never skips messages.
    """
    mode = _preflight_mode()
    if mode in {"", "off", "0", "false", "no", "disabled"}:
        return {"action": "allow"}
    text = getattr(event, "text", "") if event is not None else ""
    if not isinstance(text, str):
        return {"action": "allow"}
    classification = classify_task(text)
    if classification.task_type not in {
        "small_code_change",
        "large_code_change",
        "high_risk_change",
        "multi_agent_project",
        "ops_scheduled",
    }:
        return {"action": "allow"}
    if mode in {"strict", "require", "required"}:
        return {"action": "rewrite", "text": STRICT_NOTICE + text}
    if classification.harness_required:
        return {"action": "rewrite", "text": STRICT_NOTICE + text}
    return {"action": "rewrite", "text": PREFLIGHT_NOTICE + text}


def _handle_intake_slash(raw_args: str = "") -> str:
    raw = (raw_args or "").strip()
    if raw:
        return (
            f"/intake currently provides entry instructions only. Received: {raw}\n\n"
            f"{HELP_TEXT}"
        )
    return HELP_TEXT


def register(ctx) -> None:
    ctx.register_cli_command(
        "harness",
        help="Harness / Agenting Engineering task intake helper",
        description=HELP_TEXT,
        setup_fn=_setup_harness_cli,
        handler_fn=_handle_harness_cli,
    )
    ctx.register_command(
        "intake",
        handler=_handle_intake_slash,
        description="Show Harness / Agenting Engineering task intake instructions.",
        args_hint="[optional note]",
    )
    ctx.register_hook("pre_gateway_dispatch", _handle_pre_gateway_dispatch)
