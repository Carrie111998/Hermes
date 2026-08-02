#!/usr/bin/env python3
"""Run a real, isolated Agent A/B experiment for team memory.

This runner deliberately does not simulate agent behaviour. Each arm launches
an independent Hermes ``-z`` process with the same prompt, model config, and
working directory. The enhanced arm receives only the reviewed seed database
and the gated ``team_memory_search`` tool.

No production profile is touched unless the operator explicitly passes its
path as ``--source-home``. The copied temporary homes are deleted by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.team_memory.storage import (  # noqa: E402
    add_memory,
    get_query_metrics,
    init_database,
)
from experiments.team_memory_ab_test.shared_memory_seed.seed_data import (  # noqa: E402
    SEED_MEMORIES,
)


MINIMUM_PAIRS_FOR_DECISION = 30
DEFAULT_REPETITIONS = 2
MAX_RESPONSE_PREVIEW = 2_000
_SENSITIVE_OUTPUT_RE = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*[^\s,;]+"
)


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    description: str
    initial_prompt: str
    expected_keywords: list[str]


@dataclass
class TaskResult:
    task_id: str
    agent_type: str
    returncode: int
    time_seconds: float
    api_calls: Optional[int]
    total_tokens: Optional[int]
    memory_tool_calls: int
    success: bool
    keywords_found: list[str]
    keywords_missing: list[str]
    response_preview: str
    response_sha256: str
    failure: Optional[str] = None


@dataclass
class ComparisonResult:
    task_id: str
    repetition: int
    arm_order: list[str]
    baseline: TaskResult
    enhanced: TaskResult
    time_delta_seconds: float
    api_calls_delta: Optional[int]
    token_delta: Optional[int]
    success_delta: int


def _copy_runtime_home(source_home: Path, target_home: Path) -> None:
    """Copy only profile inputs needed for a comparable, isolated run."""
    target_home.mkdir(parents=True, exist_ok=True)
    for name in (
        "config.yaml",
        "models.json",
        "auth.json",
        ".env",
        "SOUL.md",
        "USER.md",
        "MEMORY.md",
    ):
        source = source_home / name
        if source.is_file():
            destination = target_home / name
            shutil.copy2(source, destination)
            try:
                destination.chmod(0o600)
            except OSError:
                pass


def _scrub_sensitive_files(home: Path) -> None:
    """Remove copied credentials after an arm has finished.

    The child process needs the copied auth material to call the configured
    provider. Keeping it in a retained ``--keep-temp`` directory would turn an
    experiment report into a credential backup, so audit homes retain only
    non-secret inputs and outputs.
    """
    for name in ("auth.json", ".env"):
        path = home / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # The experiment result is still useful even if a platform denies
            # cleanup; the caller gets a warning through the report path.
            pass


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _redact_output(value: Any, limit: int = 500) -> str:
    text = _as_text(value)
    text = _SENSITIVE_OUTPUT_RE.sub(r"\1=[REDACTED]", text)
    return text[-limit:]


def _redact_preview(value: str) -> str:
    return _SENSITIVE_OUTPUT_RE.sub(
        r"\1=[REDACTED]", _as_text(value)[:MAX_RESPONSE_PREVIEW]
    )


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _validate_run_inputs(
    *,
    source_home: Path,
    workdir: Path,
    hermes_command: list[str],
    repetitions: int,
    minimum_pairs: int,
) -> None:
    if not source_home.is_dir():
        raise ValueError(f"source Hermes home does not exist: {source_home}")
    if not (source_home / "config.yaml").is_file():
        raise ValueError(f"source Hermes home has no config.yaml: {source_home}")
    if not workdir.is_dir():
        raise ValueError(f"experiment workdir does not exist: {workdir}")
    if not hermes_command:
        raise ValueError("hermes command cannot be empty")
    executable = hermes_command[0]
    if not shutil.which(executable):
        raise ValueError(f"Hermes command is not executable: {executable}")
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if minimum_pairs < 1:
        raise ValueError("minimum_pairs must be at least 1")


def _load_config(path: Path) -> dict[str, Any]:
    try:
        import yaml

        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"config is not a mapping: {path}")
    return value


def _write_config(path: Path, config: dict[str, Any]) -> None:
    import yaml

    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _configure_arm(
    source_home: Path,
    target_home: Path,
    *,
    arm: str,
    db_path: Path,
    metrics_path: Path,
    workspace_id: str,
) -> None:
    _copy_runtime_home(source_home, target_home)
    config = _load_config(target_home / "config.yaml")
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}
        config["plugins"] = plugins
    enabled = plugins.get("enabled")
    enabled_list = [str(item) for item in enabled] if isinstance(enabled, list) else []
    enabled_list = [item for item in enabled_list if item != "team-memory"]

    team_cfg = config.setdefault("team_memory", {})
    if not isinstance(team_cfg, dict):
        team_cfg = {}
        config["team_memory"] = team_cfg
    team_cfg.update(
        {
            "enabled": arm == "enhanced",
            "workspace_id": workspace_id,
            "database_path": str(db_path),
            "metrics_path": str(metrics_path),
            "agent_variant": arm,
        }
    )
    if arm == "enhanced":
        enabled_list.append("team-memory")
    plugins["enabled"] = enabled_list
    _write_config(target_home / "config.yaml", config)


def _seed_database(db_path: Path, workspace_id: str) -> None:
    init_database(db_path, workspace_id=workspace_id)
    for memory in SEED_MEMORIES:
        add_memory(
            category=memory["category"],
            title=memory["title"],
            content=memory["content"],
            author=memory["author"],
            tags=memory["tags"],
            workspace_id=workspace_id,
            memory_key=f"seed-{hashlib.sha256(memory['title'].encode('utf-8')).hexdigest()[:20]}",
            db_path=db_path,
        )


def _keyword_match(response: str, keywords: Iterable[str]) -> tuple[list[str], list[str]]:
    found = [keyword for keyword in keywords if keyword.casefold() in response.casefold()]
    missing = [keyword for keyword in keywords if keyword not in found]
    return found, missing


def _usage(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _error_text(completed: subprocess.CompletedProcess[str], usage: dict[str, Any]) -> Optional[str]:
    if completed.returncode == 0 and not usage.get("failed"):
        return None
    failure = usage.get("failure")
    if failure:
        return _redact_output(failure)
    return _redact_output(completed.stderr or "process failed")


def _run_agent_process(
    task: Task,
    *,
    arm: str,
    source_home: Path,
    hermes_command: list[str],
    workdir: Path,
    workspace_id: str,
    timeout_seconds: int,
    root_temp: Path,
) -> TaskResult:
    arm_home = root_temp / f"{task.id}-{arm}-home"
    db_path = arm_home / "team-memory" / "shared.db"
    metrics_path = arm_home / "team-memory" / "metrics.db"
    _configure_arm(
        source_home,
        arm_home,
        arm=arm,
        db_path=db_path,
        metrics_path=metrics_path,
        workspace_id=workspace_id,
    )
    if arm == "enhanced":
        _seed_database(db_path, workspace_id)

    usage_path = arm_home / "usage.json"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(arm_home)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    command = [
        *hermes_command,
        "-z",
        task.initial_prompt,
        "--usage-file",
        str(usage_path),
        "--toolsets",
        "all",
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        elapsed = time.monotonic() - started
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        completed = subprocess.CompletedProcess(
            command,
            returncode=124,
            stdout=_as_text(exc.stdout),
            stderr=f"timeout after {timeout_seconds}s",
        )
    except OSError as exc:
        elapsed = time.monotonic() - started
        completed = subprocess.CompletedProcess(
            command,
            returncode=127,
            stdout="",
            stderr=f"unable to start Hermes: {type(exc).__name__}: {exc}",
        )
    finally:
        _scrub_sensitive_files(arm_home)
    usage = _usage(usage_path)
    response = _as_text(completed.stdout).strip()
    found, missing = _keyword_match(response, task.expected_keywords)
    # A result is successful only when Hermes completed and the explicit
    # task oracle found at least half of the required terms. This is a coarse
    # oracle; reviewers should inspect the saved previews before promotion.
    success = (
        completed.returncode == 0
        and not usage.get("failed")
        and len(found) >= math.ceil(len(task.expected_keywords) / 2)
    )
    try:
        metrics = (
            get_query_metrics(
                workspace_id=workspace_id,
                metrics_path=metrics_path,
            )
            if metrics_path.exists()
            else []
        )
    except Exception:
        # A metrics failure must not turn a completed Agent run into a false
        # task failure. The report still records zero observed memory calls.
        metrics = []
    return TaskResult(
        task_id=task.id,
        agent_type=arm,
        returncode=completed.returncode,
        time_seconds=round(elapsed, 4),
        api_calls=_int_or_none(usage.get("api_calls")),
        total_tokens=_int_or_none(usage.get("total_tokens")),
        memory_tool_calls=len(metrics),
        success=success,
        keywords_found=found,
        keywords_missing=missing,
        response_preview=_redact_preview(response),
        response_sha256=hashlib.sha256(response.encode("utf-8")).hexdigest(),
        failure=_error_text(completed, usage),
    )


def compare(
    task: Task,
    baseline: TaskResult,
    enhanced: TaskResult,
    *,
    repetition: int,
    arm_order: list[str],
) -> ComparisonResult:
    return ComparisonResult(
        task_id=task.id,
        repetition=repetition,
        arm_order=arm_order,
        baseline=baseline,
        enhanced=enhanced,
        time_delta_seconds=round(baseline.time_seconds - enhanced.time_seconds, 4),
        api_calls_delta=(
            baseline.api_calls - enhanced.api_calls
            if isinstance(baseline.api_calls, int) and isinstance(enhanced.api_calls, int)
            else None
        ),
        token_delta=(
            baseline.total_tokens - enhanced.total_tokens
            if isinstance(baseline.total_tokens, int) and isinstance(enhanced.total_tokens, int)
            else None
        ),
        success_delta=int(enhanced.success) - int(baseline.success),
    )


def analyze(results: list[ComparisonResult], *, minimum_pairs: int) -> dict[str, Any]:
    if not results:
        return {"decision": "no_data", "pairs": 0}
    time_deltas = [item.time_delta_seconds for item in results]
    success_deltas = [item.success_delta for item in results]
    enhanced_success = sum(item.enhanced.success for item in results)
    baseline_success = sum(item.baseline.success for item in results)
    paired_success_wins = sum(item.success_delta > 0 for item in results)
    paired_success_losses = sum(item.success_delta < 0 for item in results)
    paired_success_ties = sum(item.success_delta == 0 for item in results)
    baseline_median_seconds = statistics.median(
        item.baseline.time_seconds for item in results
    )
    enhanced_median_seconds = statistics.median(
        item.enhanced.time_seconds for item in results
    )
    median_time_saving_ratio = (
        statistics.median(time_deltas) / baseline_median_seconds
        if baseline_median_seconds > 0
        else None
    )
    decision = "insufficient_sample"
    reason = f"need at least {minimum_pairs} paired runs"
    if len(results) >= minimum_pairs:
        # This is a promotion gate, not a claim of statistical significance:
        # latency and model output are noisy, and no p-value is reported unless
        # a separately reviewed statistical analysis is run on raw samples.
        if enhanced_success >= baseline_success and statistics.median(time_deltas) > 0:
            decision = "candidate_go"
            reason = "enhanced arm is no worse on success and median runtime is lower"
        else:
            decision = "no_go"
            reason = "promotion criteria were not met"
    return {
        "decision": decision,
        "decision_reason": reason,
        "pairs": len(results),
        "minimum_pairs": minimum_pairs,
        "baseline_success_rate": baseline_success / len(results),
        "enhanced_success_rate": enhanced_success / len(results),
        "success_delta_mean": sum(success_deltas) / len(success_deltas),
        "paired_success_wins": paired_success_wins,
        "paired_success_losses": paired_success_losses,
        "paired_success_ties": paired_success_ties,
        "baseline_median_time_seconds": baseline_median_seconds,
        "enhanced_median_time_seconds": enhanced_median_seconds,
        "median_time_saving_seconds": statistics.median(time_deltas),
        "median_time_saving_ratio": median_time_saving_ratio,
        "mean_time_saving_seconds": sum(time_deltas) / len(time_deltas),
        "p_value": None,
        "note": "Do not promote from this report alone; inspect raw previews and run reviewed statistical analysis.",
    }


def run_experiment(
    tasks: list[Task],
    *,
    source_home: Path,
    workdir: Path,
    output_dir: Path,
    hermes_command: list[str],
    workspace_id: str,
    timeout_seconds: int,
    minimum_pairs: int,
    repetitions: int,
    keep_temp: bool,
) -> dict[str, Any]:
    _validate_run_inputs(
        source_home=source_home,
        workdir=workdir,
        hermes_command=hermes_command,
        repetitions=repetitions,
        minimum_pairs=minimum_pairs,
    )
    root_temp = Path(tempfile.mkdtemp(prefix="hermes-team-memory-ab-"))
    results: list[ComparisonResult] = []
    try:
        for task in tasks:
            for repetition in range(1, repetitions + 1):
                sample_id = f"{task.id}#r{repetition}"
                sample_task = Task(
                    id=sample_id,
                    category=task.category,
                    description=task.description,
                    initial_prompt=task.initial_prompt,
                    expected_keywords=task.expected_keywords,
                )
                # Alternate the first arm deterministically. This controls for
                # provider warm-up and transient service load while keeping the
                # experiment reproducible from the report alone.
                digest = hashlib.sha256(sample_id.encode("utf-8")).digest()
                arm_order = (
                    ["enhanced", "baseline"]
                    if digest[0] % 2
                    else ["baseline", "enhanced"]
                )
                print(
                    f"running {sample_id}: {task.description} "
                    f"(order={'/'.join(arm_order)})",
                    flush=True,
                )
                arm_results: dict[str, TaskResult] = {}
                for arm in arm_order:
                    arm_results[arm] = _run_agent_process(
                        sample_task,
                        arm=arm,
                        source_home=source_home,
                        hermes_command=hermes_command,
                        workdir=workdir,
                        workspace_id=workspace_id,
                        timeout_seconds=timeout_seconds,
                        root_temp=root_temp,
                    )
                baseline = arm_results["baseline"]
                enhanced = arm_results["enhanced"]
                results.append(
                    compare(
                        sample_task,
                        baseline,
                        enhanced,
                        repetition=repetition,
                        arm_order=arm_order,
                    )
                )
                print(
                    f"  baseline success={baseline.success} enhanced success={enhanced.success} "
                    f"memory_calls={enhanced.memory_tool_calls}",
                    flush=True,
                )
        report = {
            "experiment": "team_memory_agent_ab",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_home": str(source_home),
            "workdir": str(workdir),
            "hermes_command": hermes_command,
            "workspace_id": workspace_id,
            "task_count": len(tasks),
            "repetitions": repetitions,
            "paired_runs": len(results),
            "analysis": analyze(results, minimum_pairs=minimum_pairs),
            "results": [asdict(result) for result in results],
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"experiment_report_{int(time.time())}.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"report: {report_path}")
        print(f"decision: {report['analysis']['decision']}")
        return report
    finally:
        if not keep_temp:
            shutil.rmtree(root_temp, ignore_errors=True)
        else:
            print(f"temporary homes: {root_temp}")


def create_test_tasks() -> list[Task]:
    """Twenty deterministic prompts across frontend/backend/devops concerns."""
    return [
        Task("backend-01", "backend", "JWT decision", "请查找团队关于 JWT 与 Session 认证的既有决策，并说明原因。", ["JWT", "无状态", "微服务"]),
        Task("backend-02", "backend", "PostgreSQL decision", "请查找主数据库选型决策，并列出 JSONB、全文搜索和 CTE 相关理由。", ["PostgreSQL", "JSONB", "全文搜索", "CTE"]),
        Task("backend-03", "backend", "Users API", "请查找 User API v1 契约，给出 POST 路径和必填参数。", ["POST", "/api/users", "name", "email"]),
        Task("backend-04", "backend", "Auth API", "请查找登录和 refresh API 契约，说明 token 和错误码。", ["/api/auth/login", "token", "401"]),
        Task("backend-05", "backend", "Product API", "请查找 Product 列表 API，列出 page、limit、category、search 参数。", ["/api/products", "page", "limit", "pagination"]),
        Task("backend-06", "backend", "Error contract", "请查找统一错误处理规范，说明错误码和 request_id 要求。", ["错误码", "request_id", "USER_NOT_FOUND"]),
        Task("backend-07", "backend", "API versioning", "请查找 API 版本管理策略，说明 URL 格式和维护周期。", ["/api/v1", "breaking", "6个月"]),
        Task("backend-08", "backend", "Auth tradeoff", "请查找 JWT 决策中的反对意见和最终取舍。", ["Session", "撤销", "扩展性"]),
        Task("frontend-01", "frontend", "React state", "请查找 React 状态管理规范，区分 local、shared、server state。", ["useState", "Context", "React Query"]),
        Task("frontend-02", "frontend", "Prop drilling", "请查找前端避免 prop drilling 的团队建议。", ["Context", "composition", "children"]),
        Task("frontend-03", "frontend", "Frontend deployment", "请查找前后端部署分离决策，说明前端托管和 CDN 优势。", ["Frontend", "Nginx", "CDN"]),
        Task("frontend-04", "frontend", "CORS risk", "请查找前后端分离部署的主要风险和配置注意事项。", ["CORS", "Nginx", "反向代理"]),
        Task("frontend-05", "frontend", "React performance", "请查找 React 性能优化规范中的 useMemo 和 useCallback 建议。", ["useMemo", "useCallback", "render"]),
        Task("frontend-06", "frontend", "Frontend naming", "请查找 React 状态变量和 setter 的命名规范。", ["camelCase", "setUserName"]),
        Task("devops-01", "devops", "Deployment topology", "请查找部署架构，说明 Frontend、Backend、Nginx 的职责。", ["Vercel", "AWS ECS", "Nginx"]),
        Task("devops-02", "devops", "Independent release", "请查找前后端独立发布的收益和 trade-off。", ["独立发布", "独立扩展", "CORS"]),
        Task("devops-03", "devops", "Git workflow", "请查找团队 Git 分支和 commit message 规范，并给一个 fix 示例。", ["feature/", "fix/", "commit"]),
        Task("devops-04", "devops", "PR gate", "请查找 PR 合并前的 review、CI 和冲突要求。", ["review", "CI", "merge"]),
        Task("devops-05", "devops", "API release", "请查找 breaking changes 时的 API 升级策略。", ["v2", "breaking", "向后兼容"]),
        Task("devops-06", "devops", "Shared contract", "请查找前后端协作时应遵循的 API 契约和维护者信息。", ["契约", "维护者", "backend_agent"]),
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-home", required=True, help="Profile home to copy as the neutral baseline")
    parser.add_argument("--workdir", default=str(REPO_ROOT), help="Same working directory for both arms")
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "experiments/team_memory_ab_test/results"))
    parser.add_argument("--hermes-command", default="", help="Override Hermes command, e.g. 'hermes'")
    parser.add_argument("--workspace-id", default="xinxiang")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--minimum-pairs", type=int, default=MINIMUM_PAIRS_FOR_DECISION)
    parser.add_argument(
        "--repetitions",
        type=int,
        default=DEFAULT_REPETITIONS,
        help=(
            "Run each task this many times per arm; default 2 gives 40 "
            "paired runs from the 20-task catalog"
        ),
    )
    parser.add_argument("--keep-temp", action="store_true", help="Keep isolated homes for audit")
    parser.add_argument("--dry-run", action="store_true", help="Print task count and command without calling a model")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    tasks = create_test_tasks()
    hermes_command = shlex.split(args.hermes_command) if args.hermes_command else [sys.executable, "-m", "hermes_cli.main"]
    if args.dry_run:
        print(
            json.dumps(
                {
                    "tasks": len(tasks),
                    "repetitions": args.repetitions,
                    "paired_runs": len(tasks) * args.repetitions,
                    "minimum_pairs": args.minimum_pairs,
                    "hermes_command": hermes_command,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    try:
        run_experiment(
            tasks,
            source_home=Path(args.source_home).expanduser().resolve(),
            workdir=Path(args.workdir).expanduser().resolve(),
            output_dir=Path(args.output_dir).expanduser().resolve(),
            hermes_command=hermes_command,
            workspace_id=args.workspace_id,
            timeout_seconds=args.timeout_seconds,
            minimum_pairs=args.minimum_pairs,
            repetitions=args.repetitions,
            keep_temp=args.keep_temp,
        )
    except Exception as exc:
        print(f"experiment failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
