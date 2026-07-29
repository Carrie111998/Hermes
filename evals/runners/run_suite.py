#!/usr/bin/env python3
"""Eval suite runner for Hermes Agent capability scoring.

Loads a suite YAML, runs each scenario against AIAgent, scores with rubric,
and outputs a JSON report.

Usage:
    python evals/runners/run_suite.py --suite orchestration [--provider openrouter] [--output reports/latest.json]
    python evals/runners/run_suite.py --suite cost_cache --deterministic-only
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_WORKTREE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_WORKTREE))
_EVALS_DIR = _WORKTREE / "evals"


def load_yaml(path: Path) -> dict:
    """Load a YAML file, returning empty dict on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"ERROR loading {path}: {e}", file=sys.stderr)
        return {}


def load_rubric(suite_name: str):
    """Dynamically import a rubric module from evals/rubrics/<suite_name>.py."""
    rubric_path = _EVALS_DIR / "rubrics" / f"{suite_name}.py"
    if not rubric_path.exists():
        print(
            f"WARNING: No rubric at {rubric_path} — falling back to "
            "pass_conditions (fail-closed)",
            file=sys.stderr,
        )
        return None
    spec = importlib.util.spec_from_file_location(f"rubric_{suite_name}", rubric_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coerce_flat_overrides(overrides: dict) -> dict:
    """Convert dotted config_overrides keys into nested dicts.

    Suite YAML commonly uses flat keys like:
      delegation.max_concurrent_children: 3
      delegation.max_spawn_depth: 2

    Rubrics read nested maps (scenario['config_overrides']['delegation'][...]).
    Without this coercion, depth/cap checks silently fall back to defaults and
    orchestration scenarios produce false negatives.
    """
    if not isinstance(overrides, dict):
        return {}

    # Start from a deep copy so pre-nested structures are preserved.
    out = copy.deepcopy(overrides)

    for key, value in overrides.items():
        if not isinstance(key, str) or "." not in key:
            continue
        parts = [p for p in key.split(".") if p]
        if not parts:
            continue

        cur = out
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value

    return out


def _apply_config_overrides(overrides_raw: dict) -> Dict[str, Any]:
    """Temporarily apply scenario config_overrides to CLI_CONFIG.

    AIAgent and delegate_task load settings from runtime CLI_CONFIG first.
    The eval runner must inject suite overrides there so live runs actually use
    per-scenario caps (e.g. delegation.max_concurrent_children=3).

    Returns a snapshot token used by _restore_config_overrides().
    """
    try:
        from cli import CLI_CONFIG
    except Exception:
        return {}

    try:
        overrides = _coerce_flat_overrides(overrides_raw)
    except Exception:
        overrides = {}

    snapshot = {
        "had_delegation": "delegation" in CLI_CONFIG,
        "delegation": copy.deepcopy(CLI_CONFIG.get("delegation")),
        "had_agent": "agent" in CLI_CONFIG,
        "agent": copy.deepcopy(CLI_CONFIG.get("agent")),
    }

    # delegation block
    if isinstance(overrides.get("delegation"), dict):
        if not isinstance(CLI_CONFIG.get("delegation"), dict):
            CLI_CONFIG["delegation"] = {}
        CLI_CONFIG["delegation"].update(overrides["delegation"])

    # agent block
    if isinstance(overrides.get("agent"), dict):
        if not isinstance(CLI_CONFIG.get("agent"), dict):
            CLI_CONFIG["agent"] = {}
        CLI_CONFIG["agent"].update(overrides["agent"])

    return snapshot


def _restore_config_overrides(snapshot: Dict[str, Any]) -> None:
    """Restore CLI_CONFIG after a scenario run."""
    try:
        from cli import CLI_CONFIG
    except Exception:
        return

    if not isinstance(snapshot, dict):
        return

    if snapshot.get("had_delegation"):
        CLI_CONFIG["delegation"] = snapshot.get("delegation")
    else:
        CLI_CONFIG.pop("delegation", None)

    if snapshot.get("had_agent"):
        CLI_CONFIG["agent"] = snapshot.get("agent")
    else:
        CLI_CONFIG.pop("agent", None)


def _live_error(error: str) -> dict:
    """Return the canonical live-scenario failure shape."""
    return {
        "error": error,
        "final_response": "",
        "messages": [],
        "api_calls": 0,
    }


def _live_api_calls(result: dict, agent: Any) -> int:
    """Read a non-negative API count from the result, then the agent budget."""
    result_count = result.get("api_calls")
    if type(result_count) is int and result_count >= 0:
        return result_count
    budget = getattr(agent, "iteration_budget", None)
    budget_count = getattr(budget, "used", 0)
    return budget_count if type(budget_count) is int and budget_count >= 0 else 0


def run_scenario_live(scenario: dict, provider: str, model: str) -> dict:
    """Run one live scenario and contain all provider/agent failure paths."""
    config_overrides_raw = scenario.get("config_overrides", {})
    config_overrides = (
        config_overrides_raw if isinstance(config_overrides_raw, dict) else {}
    )
    overrides = _coerce_flat_overrides(config_overrides)

    enabled_toolsets = scenario.get(
        "enabled_toolsets", ["terminal", "file", "delegation"]
    )
    max_iterations = overrides.get("agent", {}).get("max_iterations", 12)
    skip_memory = scenario.get("skip_memory", True)
    skip_context = scenario.get("skip_context_files", True)
    system_msg = scenario.get("system_message")

    cfg_snapshot = _apply_config_overrides(config_overrides)
    agent = None
    try:
        from run_agent import AIAgent

        agent = AIAgent(
            provider=provider,
            model=model,
            enabled_toolsets=(
                enabled_toolsets if isinstance(enabled_toolsets, list) else None
            ),
            quiet_mode=True,
            save_trajectories=False,
            skip_context_files=skip_context,
            skip_memory=skip_memory,
            platform="cli",
            max_iterations=max_iterations,
        )
        raw_result = agent.run_conversation(
            user_message=scenario["user_message"],
            system_message=system_msg,
        )

        if not isinstance(raw_result, dict):
            return {
                "final_response": "" if raw_result is None else str(raw_result),
                "messages": [],
                "api_calls": 0,
                "error": None,
            }

        messages = raw_result.get("messages", []) or []
        if not isinstance(messages, list) or any(
            not isinstance(message, dict) for message in messages
        ):
            return _live_error("agent result messages must be a list of mappings")

        final_response_raw = raw_result.get("final_response", "")
        final_response = (
            "" if final_response_raw is None else str(final_response_raw)
        )
        error_raw = raw_result.get("error")
        error = None if error_raw in (None, "") else str(error_raw)
        if error is None and any(
            raw_result.get(flag) is True
            for flag in ("failed", "partial", "interrupted")
        ):
            error = "agent reported an incomplete or failed result without an error"
        if error is None and raw_result.get("completed") is False:
            error = "agent reported completed=false without an error"

        return {
            "final_response": final_response,
            "messages": messages,
            "api_calls": _live_api_calls(raw_result, agent),
            "error": error,
        }
    except Exception as exc:
        return {
            **_live_error(f"{type(exc).__name__}: {exc}"),
            "traceback": traceback.format_exc(),
        }
    finally:
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass
        _restore_config_overrides(cfg_snapshot)


def _rubric_failure(message: str) -> dict:
    """Return the canonical fail-closed grade for an invalid rubric result."""
    return {"pass": False, "score": 0.0, "details": {"rubric_error": message}}


def _normalize_grade(raw_grade: Any) -> dict:
    """Validate the rubric boundary so malformed plugins cannot pass or crash."""
    if not isinstance(raw_grade, dict):
        return _rubric_failure("rubric must return a dict")
    if type(raw_grade.get("pass")) is not bool:
        return _rubric_failure("rubric 'pass' must be a boolean")

    score = raw_grade.get("score")
    if type(score) not in {int, float} or not 0.0 <= float(score) <= 1.0:
        return _rubric_failure("rubric 'score' must be a finite number from 0 to 1")
    details = raw_grade.get("details")
    if not isinstance(details, dict):
        return _rubric_failure("rubric 'details' must be a dict")
    try:
        json.dumps(raw_grade, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return _rubric_failure(f"rubric result must be JSON-serializable: {exc}")
    return {
        **raw_grade,
        "pass": raw_grade["pass"],
        "score": float(score),
        "details": details,
    }


def grade_scenario(scenario: dict, result: dict, rubric_module) -> dict:
    """Score a scenario using its rubric or pass_conditions."""
    if rubric_module and hasattr(rubric_module, "grade"):
        try:
            return _normalize_grade(rubric_module.grade(scenario, result))
        except Exception as e:
            return _rubric_failure(f"{type(e).__name__}: {e}")

    # Fallback: check pass_conditions directly
    conditions = scenario.get("pass_conditions", [])
    if not isinstance(conditions, list) or not conditions:
        return {
            "pass": False,
            "score": 0.0,
            "details": {"error": "no pass conditions and no rubric"},
        }

    checks_passed = 0
    details = {}
    unsupported_conditions = []
    for cond in conditions:
        if not isinstance(cond, dict):
            unsupported_conditions.append("<invalid>")
            continue
        ctype = cond.get("type", "")
        if ctype == "delegate_call_count":
            count = _count_delegate_calls(result.get("messages", []))
            min_val = cond.get("min")
            max_val = cond.get("max")
            if min_val is None and max_val is None:
                min_val = 1
            details["delegate_calls"] = count
            min_ok = min_val is None or count >= min_val
            max_ok = max_val is None or count <= max_val
            if min_ok and max_ok:
                checks_passed += 1
        elif ctype == "no_cache_break":
            breaks = _count_cache_breaks(result.get("messages", []))
            details["cache_breaks"] = breaks
            if breaks == 0:
                checks_passed += 1
        elif ctype == "response_contains":
            val = cond.get("value", "")
            if val.lower() in result.get("final_response", "").lower():
                checks_passed += 1
            details[f"contains_{val[:30]}"] = val.lower() in result.get("final_response", "").lower()
        elif ctype == "no_tool_error":
            has_error = _has_tool_error(result.get("messages", []))
            details["has_tool_error"] = has_error
            if not has_error:
                checks_passed += 1
        else:
            unsupported_conditions.append(str(ctype or "<missing>"))

    if unsupported_conditions:
        details["unsupported_conditions"] = unsupported_conditions
    score = checks_passed / len(conditions) if conditions else 1.0
    return {
        "pass": checks_passed == len(conditions) and not unsupported_conditions,
        "score": score,
        "details": details,
    }


def _deterministic_fixture_error(scenario: dict) -> Optional[str]:
    """Return a deterministic-fixture contract error, or None when valid."""
    if "_mock_messages" in scenario:
        messages = scenario["_mock_messages"]
        if not isinstance(messages, list):
            return "_mock_messages must be a list"
        if any(not isinstance(message, dict) for message in messages):
            return "every _mock_messages entry must be a mapping"

    if "_mock_final_response" in scenario and not isinstance(
        scenario["_mock_final_response"], (str, type(None))
    ):
        return "_mock_final_response must be a string or null"

    if "_mock_api_calls" in scenario:
        api_calls = scenario["_mock_api_calls"]
        if type(api_calls) is not int or api_calls < 0:
            return "_mock_api_calls must be a non-negative integer"

    if "_mock_api_call_snapshots" in scenario:
        snapshots = scenario["_mock_api_call_snapshots"]
        if not isinstance(snapshots, list):
            return "_mock_api_call_snapshots must be a list"

    try:
        json.dumps(
            {
                key: scenario[key]
                for key in (
                    "_mock_messages",
                    "_mock_final_response",
                    "_mock_api_calls",
                    "_mock_api_call_snapshots",
                )
                if key in scenario
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        return f"deterministic fixture must be JSON-serializable: {exc}"
    return None


def _count_delegate_calls(messages: list) -> int:
    """Count delegate_task tool calls in the message transcript."""
    count = 0
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if tc.get("function", {}).get("name") == "delegate_task":
                    count += 1
    return count


def _count_cache_breaks(messages: list) -> int:
    """Count evidence of prompt-cache breaks in the message transcript."""
    breaks = 0
    prev_system = None
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if prev_system is not None and content != prev_system:
                breaks += 1
            prev_system = content
    return breaks


def _has_tool_error(messages: list) -> bool:
    """Check if any tool result contains error indicators."""
    for msg in messages:
        if msg.get("role") == "tool":
            content = str(msg.get("content", ""))
            if "error" in content.lower() or "traceback" in content.lower():
                return True
    return False


def _error_report(
    *,
    suite_name: str,
    error: str,
    provider: str,
    model: str,
    deterministic_only: bool,
    output_path: Optional[Path],
) -> dict:
    """Build and optionally persist the canonical fail-closed suite report."""
    report = {
        "suite": suite_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "deterministic_only": deterministic_only,
        "error": error,
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errored": 1,
        "skipped": 0,
        "pass_rate": 0.0,
        "scenarios": [],
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return report


def run_suite(
    suite_path: Path,
    provider: str = "openrouter",
    model: str = "anthropic/claude-haiku-4.5",
    output_path: Optional[Path] = None,
    deterministic_only: bool = False,
    quiet: bool = False,
) -> dict:
    """Run a full eval suite and return the report dict."""
    suite = load_yaml(suite_path)
    if not isinstance(suite, dict):
        return _error_report(
            suite_name=suite_path.stem,
            error="suite root must be a mapping",
            provider=provider,
            model=model,
            deterministic_only=deterministic_only,
            output_path=output_path,
        )

    suite_name_raw = suite.get("name", suite_path.stem)
    if not isinstance(suite_name_raw, str) or not suite_name_raw.strip():
        return _error_report(
            suite_name=suite_path.stem,
            error="suite name must be a non-empty string",
            provider=provider,
            model=model,
            deterministic_only=deterministic_only,
            output_path=output_path,
        )
    suite_name = suite_name_raw.strip()
    scenarios = suite.get("scenarios", [])
    if not isinstance(scenarios, list):
        return _error_report(
            suite_name=suite_name,
            error="scenarios must be a list",
            provider=provider,
            model=model,
            deterministic_only=deterministic_only,
            output_path=output_path,
        )

    seen_ids: set[str] = set()
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            shape_error = f"scenario {index} must be a mapping"
        elif "description" in scenario and not isinstance(
            scenario["description"], str
        ):
            shape_error = f"scenario {index} description must be a string"
        elif not isinstance(scenario.get("user_message"), str):
            shape_error = f"scenario {index} user_message must be a string"
        else:
            scenario_id = scenario.get("id", f"S{index}")
            if not isinstance(scenario_id, str) or not scenario_id.strip():
                shape_error = f"scenario {index} id must be a non-empty string"
            elif scenario_id in seen_ids:
                shape_error = f"duplicate scenario id: {scenario_id}"
            else:
                seen_ids.add(scenario_id)
                continue
        return _error_report(
            suite_name=suite_name,
            error=shape_error,
            provider=provider,
            model=model,
            deterministic_only=deterministic_only,
            output_path=output_path,
        )

    if not scenarios:
        print(f"WARNING: No scenarios found in {suite_path}", file=sys.stderr)
        return _error_report(
            suite_name=suite_name,
            error="no scenarios",
            provider=provider,
            model=model,
            deterministic_only=deterministic_only,
            output_path=output_path,
        )

    rubric = load_rubric(suite_name)
    results = []
    passed = 0
    failed = 0
    errored = 0
    skipped = 0

    # Tier-1 fixture transcripts exercise rubric parsing only.  A suite that
    # declares ``runtime_probe`` must also pass a hermetic production-path
    # probe; otherwise a production regression could leave every YAML fixture
    # green.  Probes make no API calls and isolate HERMES_HOME/workspace state.
    runtime_probe = None
    if deterministic_only and "runtime_probe" in suite:
        from evals.runtime_probes import run_runtime_probe

        runtime_probe = run_runtime_probe(suite.get("runtime_probe"))
        if not runtime_probe.get("pass"):
            errored += 1

    for i, scenario in enumerate(scenarios):
        sid = scenario.get("id", f"S{i}")
        if not quiet:
            print(
                f"  [{i+1}/{len(scenarios)}] {sid}: "
                f"{scenario.get('description', '')[:80]}",
                file=sys.stderr,
            )

        t0 = time.time()

        # Explicit deterministic skip — scenario requires a live agent /
        # real filesystem / real HERMES_HOME. Counted as skipped, not fail.
        if deterministic_only and scenario.get("deterministic_skip"):
            reason = (
                scenario.get("deterministic_skip_reason")
                or scenario.get("deterministic_skip")
                or "requires live agent"
            )
            if scenario.get("deterministic_skip") is True:
                reason = scenario.get("deterministic_skip_reason") or "requires live agent"
            skipped += 1
            results.append({
                "id": sid,
                "pass": None,
                "score": None,
                "skipped": True,
                "details": {
                    "skipped": True,
                    "reason": str(reason),
                },
                "api_calls": 0,
                "duration_s": round(time.time() - t0, 2),
            })
            if not quiet:
                print(f"    ↷ skipped (deterministic): {reason}", file=sys.stderr)
            continue

        if deterministic_only:
            fixture_keys = {
                "_mock_messages",
                "_mock_final_response",
                "_mock_api_calls",
                "_mock_api_call_snapshots",
            }
            if not any(key in scenario for key in fixture_keys):
                errored += 1
                results.append({
                    "id": sid,
                    "pass": False,
                    "score": 0.0,
                    "details": {
                        "error": (
                            "deterministic fixture missing; add _mock_* data or "
                            "set deterministic_skip"
                        )
                    },
                    "api_calls": 0,
                    "duration_s": round(time.time() - t0, 2),
                })
                continue
            fixture_error = _deterministic_fixture_error(scenario)
            if fixture_error:
                errored += 1
                results.append({
                    "id": sid,
                    "pass": False,
                    "score": 0.0,
                    "details": {"error": fixture_error},
                    "api_calls": 0,
                    "duration_s": round(time.time() - t0, 2),
                })
                continue
            # Fixture mode: grade rubric invariants against embedded transcripts.
            # The suite-level runtime_probe above is the production-path evidence;
            # these _mock_* values are intentionally only rubric unit coverage,
            # not proof that Hermes production paths work. No live API call.
            messages = scenario.get("_mock_messages", []) or []
            final_response = scenario.get("_mock_final_response")
            if final_response is None:
                # Prefer explicit mock; else last assistant content; else ""
                final_response = ""
                for msg in reversed(messages):
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        content = msg.get("content")
                        if isinstance(content, str) and content.strip():
                            final_response = content
                            break
            result = {
                "final_response": final_response or "",
                "messages": messages,
                "api_calls": int(scenario.get("_mock_api_calls", 0) or 0),
                "api_call_snapshots": scenario.get("_mock_api_call_snapshots") or [],
                "error": None,
            }
        else:
            result = run_scenario_live(scenario, provider, model)

        elapsed = time.time() - t0

        if result.get("error"):
            errored += 1
            results.append({
                "id": sid,
                "pass": False,
                "score": 0.0,
                "details": {"error": result["error"]},
                "api_calls": result.get("api_calls", 0),
                "duration_s": round(elapsed, 2),
            })
            continue

        grade = grade_scenario(scenario, result, rubric)
        if grade["pass"]:
            passed += 1
        else:
            failed += 1

        results.append({
            "id": sid,
            "pass": grade["pass"],
            "score": grade["score"],
            "details": grade.get("details", {}),
            "api_calls": result.get("api_calls", 0),
            "duration_s": round(elapsed, 2),
        })

    graded = passed + failed + errored
    total = len(scenarios)
    pass_rate = passed / graded if graded > 0 else 0.0

    report = {
        "suite": suite_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "deterministic_only": deterministic_only,
        "total": total,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "skipped": skipped,
        "pass_rate": round(pass_rate, 4),
        "scenarios": results,
    }
    if runtime_probe is not None:
        report["runtime_probe"] = runtime_probe

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        if not quiet:
            print(f"Report written to {output_path}", file=sys.stderr)

    return report


def _load_baseline(baseline_path: Path) -> Optional[dict]:
    """Load a baseline JSON for comparison."""
    if not baseline_path.exists():
        return None
    try:
        return json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def compare_baseline(report: dict, baseline_path: Path) -> dict:
    """Compare a new report against a stored baseline. Returns diff dict."""
    baseline = _load_baseline(baseline_path)
    if baseline is None:
        return {"status": "no_baseline", "message": f"No baseline at {baseline_path}"}

    old_rate = baseline.get("pass_rate", 0)
    new_rate = report.get("pass_rate", 0)
    delta = new_rate - old_rate

    regressions = []
    old_scenarios = {s["id"]: s for s in baseline.get("scenarios", [])}
    for s in report.get("scenarios", []):
        old = old_scenarios.get(s["id"])
        if old and old.get("pass") and not s.get("pass"):
            regressions.append(s["id"])

    return {
        "status": "regression" if delta < -0.05 else ("improvement" if delta > 0.05 else "stable"),
        "baseline_pass_rate": old_rate,
        "current_pass_rate": new_rate,
        "delta": round(delta, 4),
        "regressions": regressions,
    }


def print_summary(report: dict) -> None:
    """Print a human-readable summary to stdout."""
    print(f"\n{'='*60}")
    print(f"Suite: {report['suite']}")
    print(f"Model: {report.get('model', 'unknown')} via {report.get('provider', 'unknown')}")
    print(f"Time:  {report['timestamp']}")
    print(f"{'='*60}")
    print(f"Total:   {report['total']}")
    print(f"Passed:  {report['passed']}")
    print(f"Failed:  {report['failed']}")
    print(f"Errors:  {report.get('errored', 0)}")
    print(f"Skipped: {report.get('skipped', 0)}")
    print(f"Rate:    {report['pass_rate']:.1%}")
    print(f"{'='*60}")

    runtime_probe = report.get("runtime_probe")
    if runtime_probe is not None:
        status = "PASS" if runtime_probe.get("pass") else "FAIL"
        modules = ", ".join(runtime_probe.get("production_modules", []))
        print(f"Runtime probe: {status}  api_calls={runtime_probe.get('api_calls', 0)}")
        if modules:
            print(f"  production modules: {modules}")
        if not runtime_probe.get("pass"):
            print(f"  details: {runtime_probe.get('details', {})}")

    for s in report.get("scenarios", []):
        if s.get("skipped"):
            status = "↷"
            score = "skip"
        else:
            status = "✅" if s.get("pass") else ("❌" if s.get("score", 0) == 0 else "⚠️")
            score = f"{s.get('score', 0):.2f}" if s.get("score") is not None else "?"
        print(f"  {status} {s['id']}: score={score}  api_calls={s.get('api_calls', '?')}  {s.get('duration_s', 0):.1f}s")
        if s.get("skipped") and s.get("details"):
            print(f"      reason: {s['details'].get('reason', '')}")
        elif not s.get("pass") and s.get("details"):
            for k, v in s["details"].items():
                print(f"      {k}: {v}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Hermes Agent Eval Suite Runner")
    parser.add_argument("--suite", required=True, help="Suite name (e.g., orchestration, cost_cache)")
    parser.add_argument("--suites-dir", default=str(_EVALS_DIR / "suites"), help="Path to suites directory")
    parser.add_argument("--provider", default="openrouter", help="LLM provider (openrouter, anthropic, etc.)")
    parser.add_argument("--model", default="anthropic/claude-haiku-4.5", help="Model name")
    parser.add_argument("--output", help="Output JSON path (default: evals/reports/<suite>.json)")
    parser.add_argument("--baseline", help="Baseline JSON path for comparison")
    parser.add_argument(
        "--deterministic-only",
        action="store_true",
        help=(
            "Run the suite's hermetic production-path probe (when declared) "
            "plus rubric fixtures; make no live model/API calls"
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress per-scenario output")
    args = parser.parse_args()

    suite_path = Path(args.suites_dir) / f"{args.suite}.yaml"
    if not suite_path.exists():
        print(f"ERROR: Suite not found: {suite_path}", file=sys.stderr)
        sys.exit(1)

    output_path = None
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = _EVALS_DIR / "reports" / f"{args.suite}.json"

    report = run_suite(
        suite_path=suite_path,
        provider=args.provider,
        model=args.model,
        output_path=output_path,
        deterministic_only=args.deterministic_only,
        quiet=args.quiet,
    )

    print_summary(report)

    if args.baseline:
        diff = compare_baseline(report, Path(args.baseline))
        if diff["status"] == "no_baseline":
            print(f"Baseline comparison: no_baseline  ({diff['message']})")
        else:
            print(f"Baseline comparison: {diff['status']}  (Δ={diff['delta']:+.2%})")
        if diff.get("regressions"):
            print(f"Regressions: {', '.join(diff['regressions'])}")
            sys.exit(1)
        if diff.get("status") == "regression":
            sys.exit(1)

    if report.get("error") or report.get("failed", 0) or report.get("errored", 0):
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
