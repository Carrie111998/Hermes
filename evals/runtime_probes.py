"""Hermetic Tier-1 probes for real Hermes production paths.

These probes make no model or network calls.  Each probe creates an isolated
``HERMES_HOME`` and workspace, invokes production modules directly, and then
removes the temporary state.  YAML ``_mock_*`` transcripts remain useful for
rubric parser coverage, but they are not treated as evidence that production
paths work.
"""

from __future__ import annotations

import copy
import json
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


ProbeResult = dict[str, Any]


def _fail(error: str) -> ProbeResult:
    """Return the canonical fail-closed result shape."""
    return {
        "pass": False,
        "api_calls": 0,
        "production_modules": [],
        "details": {"error": error},
    }


def _validate_result(result: Any) -> ProbeResult:
    """Validate a probe result before the runner is allowed to trust it."""
    if not isinstance(result, dict):
        return _fail("runtime probe must return a dict")

    passed = result.get("pass")
    if type(passed) is not bool:
        return _fail("runtime probe 'pass' must be a boolean")

    api_calls = result.get("api_calls")
    if type(api_calls) is not int or api_calls != 0:
        return _fail("runtime probe must report integer zero API calls")

    modules = result.get("production_modules")
    if not isinstance(modules, list) or any(
        not isinstance(module, str) or not module.strip() for module in modules
    ):
        return _fail(
            "runtime probe 'production_modules' must be a list of non-empty strings"
        )
    if passed and not modules:
        return _fail("a passing runtime probe must name production_modules")

    details = result.get("details")
    if not isinstance(details, dict):
        return _fail("runtime probe 'details' must be a dict")

    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return _fail(f"runtime probe result must be JSON-serializable: {exc}")

    return dict(result)


def _unique_task_id(prefix: str) -> str:
    """Return a collision-resistant task id for process-global file-tool state."""
    return f"{prefix}-{uuid.uuid4().hex}"


def _cleanup_file_task(task_id: str, workspace: Path) -> None:
    """Remove only state created for one probe task, preserving other callers."""
    from tools import file_tools, terminal_tool
    from tools.file_state import get_registry

    # The probe registers an isolation-signalling env_type override, so this
    # task id cannot collapse to the shared "default" environment.
    terminal_tool.cleanup_vm(task_id)
    terminal_tool.clear_task_env_overrides(task_id)
    file_tools.clear_file_ops_cache(task_id)
    with file_tools._read_tracker_lock:
        file_tools._read_tracker.pop(task_id, None)
    with file_tools._patch_failure_lock:
        file_tools._patch_failure_tracker.pop(task_id, None)

    registry = get_registry()
    workspace_root = workspace.resolve()
    with registry._state_lock:
        registry._reads.pop(task_id, None)
        for path, (writer_task_id, _timestamp) in list(registry._last_writer.items()):
            if writer_task_id == task_id:
                registry._last_writer.pop(path, None)
    with registry._meta_lock:
        for path in list(registry._path_locks):
            try:
                Path(path).resolve().relative_to(workspace_root)
            except (OSError, ValueError):
                continue
            registry._path_locks.pop(path, None)


@contextmanager
def _isolated_file_task(workspace: Path, prefix: str) -> Iterator[str]:
    """Yield a unique local file-tool task without consulting external backends."""
    import time

    from tools import file_tools, terminal_tool
    from tools.environments.local import LocalEnvironment

    task_id = _unique_task_id(prefix)
    previous_max_read_chars = file_tools._max_read_chars_cached
    previous_config_resolved = file_tools._hermes_config_resolved
    previous_config_loaded = file_tools._hermes_config_resolved_loaded
    try:
        terminal_tool.register_task_env_overrides(
            task_id,
            {"env_type": "local", "cwd": str(workspace)},
        )
        # Pre-seed a real production LocalEnvironment. file_tools will find this
        # live environment and cannot inherit a process-level Docker/SSH/Modal
        # backend or invoke the backend factory, preserving the zero-external-call
        # Tier-1 contract even when the caller normally uses a remote terminal.
        environment = LocalEnvironment(cwd=str(workspace), timeout=60)
        with terminal_tool._env_lock:
            terminal_tool._active_environments[task_id] = environment
            terminal_tool._last_activity[task_id] = time.time()
        yield task_id
    finally:
        try:
            _cleanup_file_task(task_id, workspace)
        finally:
            file_tools._max_read_chars_cached = previous_max_read_chars
            file_tools._hermes_config_resolved = previous_config_resolved
            file_tools._hermes_config_resolved_loaded = previous_config_loaded


@contextmanager
def _isolated_runtime() -> Iterator[tuple[Path, Path]]:
    """Yield isolated roots and restore profile-scoped config caches on exit."""
    from hermes_cli import config as config_module

    # Hold the official re-entrant config lock across snapshot → probe →
    # restore. Without this, restoring a snapshot could erase a legitimate
    # concurrent config read/write that landed while the probe was running.
    with config_module._CONFIG_LOCK:
        config_snapshots = {
            "_LAST_EXPANDED_CONFIG_BY_PATH": copy.deepcopy(
                config_module._LAST_EXPANDED_CONFIG_BY_PATH
            ),
            "_LOAD_CONFIG_CACHE": copy.deepcopy(config_module._LOAD_CONFIG_CACHE),
            "_RAW_CONFIG_CACHE": copy.deepcopy(config_module._RAW_CONFIG_CACHE),
        }
        env_cache_snapshot = copy.deepcopy(config_module._env_cache)

        with tempfile.TemporaryDirectory(prefix="hermes-eval-home-") as home_raw:
            home = Path(home_raw)
            for name in ("memories", "skills", "sessions", "cron"):
                (home / name).mkdir(parents=True, exist_ok=True)
            workspace = home / "workspace with spaces"
            workspace.mkdir()
            token = set_hermes_home_override(home)
            try:
                yield home, workspace
            finally:
                try:
                    for name, snapshot in config_snapshots.items():
                        cache = getattr(config_module, name)
                        cache.clear()
                        cache.update(snapshot)
                    config_module._env_cache = env_cache_snapshot
                finally:
                    reset_hermes_home_override(token)


def _pass(modules: list[str], **details: Any) -> ProbeResult:
    return {
        "pass": True,
        "api_calls": 0,
        "production_modules": modules,
        "details": details,
    }


def _probe_orchestration() -> ProbeResult:
    """Exercise profile-scoped delegation config and dynamic tool schema."""
    with _isolated_runtime() as (home, _workspace):
        (home / "config.yaml").write_text(
            "delegation:\n"
            "  max_concurrent_children: 7\n"
            "  max_spawn_depth: 2\n",
            encoding="utf-8",
        )
        from tools import delegate_tool

        children = delegate_tool._get_max_concurrent_children()
        depth = delegate_tool._get_max_spawn_depth()
        schema = delegate_tool._build_dynamic_schema_overrides()
        description = schema["description"]
        tasks_description = schema["parameters"]["properties"]["tasks"]["description"]
        role_description = schema["parameters"]["properties"]["role"]["description"]

        assert children == 7
        assert depth == 2
        assert "up to 7" in description
        assert "up to 7" in tasks_description
        assert "max_spawn_depth=2" in description
        assert "max_spawn_depth=2" in role_description
        return _pass(
            ["tools.delegate_tool", "hermes_cli.config"],
            max_concurrent_children=children,
            max_spawn_depth=depth,
            dynamic_schema=True,
        )


def _probe_cost_cache() -> ProbeResult:
    """Exercise byte-stable prompt assembly and immutable tool-schema caching."""
    with _isolated_runtime():
        import model_tools
        from agent.system_prompt import build_system_prompt_parts

        # Preserve exact caller state. A probe can run in the same interpreter
        # as a live agent or another test, so "clear on exit" would destroy warm
        # entries and the caller's last-resolved tool snapshot.
        from tools import registry as registry_module

        previous_cache = copy.deepcopy(model_tools._tool_defs_cache)
        previous_tool_names = list(model_tools._last_resolved_tool_names)
        with registry_module._check_fn_cache_lock:
            previous_check_cache = dict(registry_module._check_fn_cache)
            previous_last_good = dict(registry_module._check_fn_last_good)

        model_tools._clear_tool_defs_cache()
        try:
            first_tools = model_tools.get_tool_definitions(
                enabled_toolsets=["file"], quiet_mode=True
            )
            second_tools = model_tools.get_tool_definitions(
                enabled_toolsets=["file"], quiet_mode=True
            )
            assert first_tools == second_tools
            assert first_tools is not second_tools

            tool_names = [tool["function"]["name"] for tool in first_tools]
            agent = SimpleNamespace(
                load_soul_identity=False,
                skip_context_files=True,
                valid_tool_names=tool_names,
                _task_completion_guidance=False,
                _parallel_tool_call_guidance=False,
                _tool_use_enforcement=False,
                _kanban_worker_guidance="",
                _memory_store=None,
                _memory_manager=None,
                _memory_enabled=False,
                _user_profile_enabled=False,
                model="",
                provider="",
                platform="",
                pass_session_id=False,
                session_id="",
                context_compressor=None,
                _platform_hint_overrides={},
            )
            first_prompt = build_system_prompt_parts(agent)
            second_prompt = build_system_prompt_parts(agent)
            assert first_prompt == second_prompt

            # A caller may mutate its returned list; the cached schema must survive.
            first_tools.append({"type": "function", "function": {"name": "probe_only"}})
            third_tools = model_tools.get_tool_definitions(
                enabled_toolsets=["file"], quiet_mode=True
            )
            assert "probe_only" not in {
                tool["function"]["name"] for tool in third_tools
            }
        finally:
            model_tools._clear_tool_defs_cache()
            model_tools._tool_defs_cache.update(previous_cache)
            model_tools._last_resolved_tool_names = previous_tool_names
            with registry_module._check_fn_cache_lock:
                registry_module._check_fn_cache.clear()
                registry_module._check_fn_cache.update(previous_check_cache)
                registry_module._check_fn_last_good.clear()
                registry_module._check_fn_last_good.update(previous_last_good)

        return _pass(
            ["agent.system_prompt", "model_tools"],
            prompt_byte_stable=True,
            tool_schema_byte_stable=second_tools == third_tools,
            tool_count=len(third_tools),
        )


def _probe_subagent_verify() -> ProbeResult:
    """Exercise verifiable-summary spill and independent read-back support."""
    with _isolated_runtime() as (_home, workspace):
        from tools.delegate_tool import _trim_summary_with_footer
        from tools.file_tools import read_file_tool

        with _isolated_file_task(workspace, "eval-verify") as task_id:
            unique_evidence = "verified-tail-evidence-9f52"
            full_summary = "\n".join(
                [f"subagent claim line {i}" for i in range(1_500)]
                + [unique_evidence]
            )
            trimmed, spill_path = _trim_summary_with_footer(
                full_summary, cap=2_000, task_index=0
            )
            assert spill_path is not None
            assert "SUMMARY TRUNCATED" in trimmed
            assert "Full subagent output saved to:" in trimmed
            # The trimmed head+tail the parent sees must retain the closing
            # evidence line even though the middle is elided.
            assert unique_evidence in trimmed

            # Read the tail of the spilled file directly (the production
            # read_file tool caps a single read at 2000 lines, so page to the
            # end via offset) and confirm the full text was preserved on disk.
            head = json.loads(
                read_file_tool(spill_path, offset=1, limit=1, task_id=task_id)
            )
            assert not head.get("error"), head
            total_lines = head["total_lines"]
            tail = json.loads(
                read_file_tool(
                    spill_path,
                    offset=max(1, total_lines - 5),
                    limit=10,
                    task_id=task_id,
                )
            )
            assert not tail.get("error"), tail
            assert unique_evidence in tail["content"], tail

        return _pass(
            ["tools.delegate_tool", "tools.file_tools"],
            summary_truncated=True,
            full_summary_preserved=True,
            independent_read_back=True,
        )


def _probe_memory_recall() -> ProbeResult:
    """Exercise write, replace, reload, and frozen prompt snapshot behavior."""
    with _isolated_runtime():
        from tools.memory_tool import MemoryStore, memory_tool

        old_fact = "Project Alpha uses port 9090."
        new_fact = "Project Alpha uses port 7070."
        first = MemoryStore()
        first.load_from_disk()
        added = json.loads(
            memory_tool(
                action="add", target="memory", content=old_fact, store=first
            )
        )
        assert added["success"] is True
        replaced = json.loads(
            memory_tool(
                action="replace",
                target="memory",
                old_text="9090",
                content=new_fact,
                store=first,
            )
        )
        assert replaced["success"] is True

        reloaded = MemoryStore()
        reloaded.load_from_disk()
        snapshot = reloaded.format_for_system_prompt("memory") or ""
        assert new_fact in reloaded.memory_entries
        assert old_fact not in reloaded.memory_entries
        assert new_fact in snapshot
        assert old_fact not in snapshot
        return _pass(
            ["tools.memory_tool"],
            persisted=True,
            replacement_visible_after_reload=True,
            stale_fact_absent=True,
        )


def _probe_windows_reliability() -> ProbeResult:
    """Exercise production file tools with spaces, Unicode, and a deep path."""
    with _isolated_runtime() as (_home, workspace):
        from tools.file_tools import read_file_tool, write_file_tool

        nested = workspace.joinpath(*[f"subdir_{i:02d}_long_name" for i in range(12)])
        target = nested / "اختبار_🖥️.txt"
        content = "مرحبا بالعالم 🎉\ndeep file content"
        with _isolated_file_task(workspace, "eval-windows") as task_id:
            written = json.loads(
                write_file_tool(str(target), content, task_id=task_id)
            )
            assert not written.get("error")
            read_back = json.loads(read_file_tool(str(target), task_id=task_id))
            assert not read_back.get("error")
            assert "مرحبا بالعالم 🎉" in read_back["content"]
            assert "deep file content" in read_back["content"]

        return _pass(
            ["tools.file_tools"],
            unicode_round_trip=True,
            workspace_with_spaces=True,
            deep_path_chars=len(str(target)),
        )


_PROBES: dict[str, Callable[[], ProbeResult]] = {
    "orchestration": _probe_orchestration,
    "cost_cache": _probe_cost_cache,
    "subagent_verify": _probe_subagent_verify,
    "memory_recall": _probe_memory_recall,
    "windows_reliability": _probe_windows_reliability,
}


def run_runtime_probe(name: Any) -> ProbeResult:
    """Run a registered production-path probe, failing closed on any error."""
    if not isinstance(name, str) or not name.strip():
        return _fail("runtime probe name must be a non-empty string")
    name = name.strip()
    probe = _PROBES.get(name)
    if probe is None:
        return _fail(f"unknown runtime probe: {name}")
    try:
        return _validate_result(probe())
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")


__all__ = ["run_runtime_probe"]
