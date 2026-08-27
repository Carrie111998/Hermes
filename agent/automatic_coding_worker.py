"""首轮自动编码 Worker 路由。"""

from __future__ import annotations

import json
from typing import Any, Optional


def maybe_run_automatic_coding_worker(agent: Any, user_message: Any) -> Optional[dict[str, Any]]:
    """对明确代码任务执行一次配置好的实现+审查闭环，否则返回 None。

    仅在交互式代码工作区、用户显式开启自动路由且任务包含明确代码动作时触发。
    """
    if getattr(agent, "_automatic_coding_worker_running", False):
        return None
    try:
        from hermes_cli.config import load_config_readonly
        from agent.coding_context import resolve_runtime_mode
        from agent.coding_router import is_explicit_coding_task

        cfg = load_config_readonly()
        worker_cfg = (cfg.get("agent") or {}).get("coding_worker") or {}
        if worker_cfg.get("enabled") is not True or worker_cfg.get("auto_route") is not True:
            return None
        if not is_explicit_coding_task(user_message):
            return None
        from tools.delegate_tool import _resolve_workspace_hint

        workspace = _resolve_workspace_hint(agent)
        for candidate in (
            getattr(agent, "terminal_cwd", None),
            getattr(agent, "cwd", None),
        ):
            if candidate:
                workspace = str(candidate)
                break
        mode = resolve_runtime_mode(
            platform=getattr(agent, "platform", None),
            cwd=workspace,
            config=cfg,
            model=getattr(agent, "model", None),
        )
        if not mode.is_coding:
            return None
        from tools.coding_worker_tool import coding_worker

        agent._automatic_coding_worker_running = True
        raw = coding_worker(
            task=user_message,
            workspace=workspace,
            worker=worker_cfg.get("worker"),
            review_worker=worker_cfg.get("review_worker"),
            run_review=True,
            timeout_seconds=worker_cfg.get("timeout_seconds"),
        )
        try:
            result = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"final_response": f"编码 Worker 未能启动：{raw}", "completed": False}
        implementation = result.get("implementation") or {}
        review = result.get("review") or {}
        implementation_status = implementation.get("status")
        review_status = review.get("status")
        final = (
            "编码 Worker 已执行。\n"
            f"- 实现：{result.get('worker')} / {implementation_status} / 退出码 {implementation.get('exit_code')}\n"
            f"- 审查：{result.get('review_worker')} / {review_status or '未执行'} / 退出码 {review.get('exit_code')}\n\n"
            f"实现输出：\n{implementation.get('output') or ''}\n\n"
            f"审查输出：\n{review.get('output') or ''}"
        )
        return {
            "final_response": final,
            "completed": implementation_status == "ok" and review_status == "ok",
            "worker_result": result,
        }
    except Exception:
        return None
    finally:
        agent._automatic_coding_worker_running = False
