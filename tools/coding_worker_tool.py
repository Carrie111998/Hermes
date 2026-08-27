"""外部编码 Worker：用 Codex 或 Claude Code 完成并审查代码任务。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from tools.registry import registry, tool_error


_MAX_OUTPUT_CHARS = 24_000
_ALLOWED_WORKERS = {"codex", "claude"}


def _load_worker_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
        worker = agent.get("coding_worker") if isinstance(agent.get("coding_worker"), dict) else {}
        return worker
    except Exception:
        return {}


def _workspace(path: Optional[str]) -> Path:
    candidate = Path(path or os.getenv("TERMINAL_CWD") or os.getcwd()).expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"工作目录不存在：{candidate}")
    if not (candidate / ".git").exists():
        raise ValueError(f"编码 Worker 只允许在 Git 仓库中运行：{candidate}")
    return candidate


def _command(worker: str, prompt: str) -> list[str]:
    executable = shutil.which(worker)
    if not executable:
        raise ValueError(f"未找到 {worker} CLI，请先安装或在 PATH 中配置。")
    if worker == "codex":
        return [executable, "exec", "--sandbox", "workspace-write", prompt]
    return [
        executable,
        "-p",
        prompt,
        "--allowedTools",
        "Read,Edit,Write,Bash,Glob,Grep",
    ]


def _run(worker: str, prompt: str, cwd: Path, timeout: int) -> dict[str, Any]:
    command = _command(worker, prompt)
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + "\n" + (exc.stderr or ""))[-_MAX_OUTPUT_CHARS:]
        return {"status": "timeout", "exit_code": None, "output": output}
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:12_000] + "\n…[输出已截断]…\n" + output[-12_000:]
    return {"status": "ok" if proc.returncode == 0 else "error", "exit_code": proc.returncode, "output": output}


def coding_worker(
    task: str,
    workspace: Optional[str] = None,
    worker: Optional[str] = None,
    review_worker: Optional[str] = None,
    run_review: bool = True,
    timeout_seconds: Optional[int] = None,
    task_id: Optional[str] = None,
) -> str:
    """让专用编码 CLI 修改仓库，并由独立 Worker 审查差异。"""
    if not isinstance(task, str) or not task.strip():
        return tool_error("task 必须是具体的编码任务。")
    cfg = _load_worker_config()
    if cfg.get("enabled") is not True:
        return tool_error("编码 Worker 未启用。请设置 agent.coding_worker.enabled=true。")
    selected = str(worker or cfg.get("worker") or "codex").strip().lower()
    reviewer = str(review_worker or cfg.get("review_worker") or "claude").strip().lower()
    if selected not in _ALLOWED_WORKERS or reviewer not in _ALLOWED_WORKERS:
        return tool_error("worker 和 review_worker 仅支持 codex 或 claude。")
    timeout = int(timeout_seconds or cfg.get("timeout_seconds") or 900)
    timeout = max(30, min(timeout, 3600))
    try:
        root = _workspace(workspace)
        implementation_prompt = (
            "你是 Hermes 编码实现 Worker。只在当前仓库完成以下任务：\n"
            f"{task.strip()}\n\n"
            "工作要求：先读仓库约束与现有实现；最小化修改；执行最相关的测试/静态检查；"
            "测试失败时先修复再重试；不要提交、推送、创建 PR、修改凭证或全局配置。"
            "最终输出改动、实际运行的验证命令与结果。"
        )
        implementation = _run(selected, implementation_prompt, root, timeout)
        review = None
        if run_review and implementation["status"] == "ok":
            review_prompt = (
                "你是独立代码审查 Worker。只读审查当前仓库尚未提交的差异。\n"
                f"原始任务：{task.strip()}\n\n"
                "检查：需求是否满足、回归风险、安全问题、测试是否真实覆盖。"
                "不得修改文件、不得提交。结尾必须给出 APPROVED 或 REQUEST_CHANGES。"
            )
            review = _run(reviewer, review_prompt, root, timeout)
        return json.dumps(
            {
                "workspace": str(root),
                "worker": selected,
                "implementation": implementation,
                "review_worker": reviewer if run_review else None,
                "review": review,
            },
            ensure_ascii=False,
        )
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"编码 Worker 运行失败：{exc}")


CODING_WORKER_SCHEMA = {
    "name": "coding_worker",
    "description": "在 Git 仓库中调用 Codex/Claude Code 完成编码任务，并由独立 Worker 审查。仅用户明确要求改代码时调用。",
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "具体编码目标和验收标准"},
            "workspace": {"type": "string", "description": "Git 仓库绝对路径；省略则使用当前工作目录"},
            "worker": {"type": "string", "enum": ["codex", "claude"]},
            "review_worker": {"type": "string", "enum": ["codex", "claude"]},
            "run_review": {"type": "boolean", "default": True},
            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
        },
        "required": ["task"],
    },
}

registry.register(
    name="coding_worker",
    toolset="coding_worker",
    schema=CODING_WORKER_SCHEMA,
    handler=lambda args, **kw: coding_worker(
        task=args.get("task", ""),
        workspace=args.get("workspace"),
        worker=args.get("worker"),
        review_worker=args.get("review_worker"),
        run_review=args.get("run_review", True),
        timeout_seconds=args.get("timeout_seconds"),
        task_id=kw.get("task_id"),
    ),
)
