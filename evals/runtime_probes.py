"""Hermetic Tier-1 probes for real Hermes production paths.

These probes make no model or network calls.  Each probe creates an isolated
``HERMES_HOME`` and workspace, invokes production modules directly, and then
removes the temporary state.  YAML ``_mock_*`` transcripts remain useful for
rubric parser coverage, but they are not treated as evidence that production
paths work.
"""

from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


ProbeResult = dict[str, Any]


@contextmanager
def _isolated_runtime() -> Iterator[tuple[Path, Path]]:
    """Yield isolated Hermes/workspace roots and clean both on exit."""
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

        # This probe mutates the process-global tool-definitions cache; clear it
        # before and after so it leaves no probe-time state behind for anything
        # else sharing this interpreter (other suites, the test session).
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

        return _pass(
            ["agent.system_prompt", "model_tools"],
            prompt_byte_stable=True,
            tool_schema_byte_stable=second_tools == third_tools,
            tool_count=len(third_tools),
        )


def _probe_subagent_verify() -> ProbeResult:
    """Exercise verifiable-summary spill and independent read-back support."""
    with _isolated_runtime():
        from tools.delegate_tool import _trim_summary_with_footer
        from tools.file_tools import clear_file_ops_cache, read_file_tool

        unique_evidence = "verified-tail-evidence-9f52"
        full_summary = "\n".join(
            [f"subagent claim line {i}" for i in range(1_500)] + [unique_evidence]
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

        try:
            # Read the tail of the spilled file directly (the production
            # read_file tool caps a single read at 2000 lines, so page to the
            # end via offset) and confirm the full text was preserved on disk.
            head = json.loads(
                read_file_tool(spill_path, offset=1, limit=1, task_id="eval-verify")
            )
            assert not head.get("error"), head
            total_lines = head["total_lines"]
            tail = json.loads(
                read_file_tool(
                    spill_path,
                    offset=max(1, total_lines - 5),
                    limit=10,
                    task_id="eval-verify",
                )
            )
            assert not tail.get("error"), tail
            assert unique_evidence in tail["content"], tail
        finally:
            clear_file_ops_cache("eval-verify")

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
        from tools.file_tools import clear_file_ops_cache, read_file_tool, write_file_tool

        nested = workspace.joinpath(*[f"subdir_{i:02d}_long_name" for i in range(12)])
        target = nested / "اختبار_🖥️.txt"
        content = "مرحبا بالعالم 🎉\ndeep file content"
        task_id = "eval-windows"
        try:
            written = json.loads(
                write_file_tool(str(target), content, task_id=task_id)
            )
            assert not written.get("error")
            read_back = json.loads(read_file_tool(str(target), task_id=task_id))
            assert not read_back.get("error")
            assert "مرحبا بالعالم 🎉" in read_back["content"]
            assert "deep file content" in read_back["content"]
        finally:
            clear_file_ops_cache(task_id)

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


def run_runtime_probe(name: str) -> ProbeResult:
    """Run a registered production-path probe, failing closed on any error."""
    probe = _PROBES.get(name)
    if probe is None:
        return {
            "pass": False,
            "api_calls": 0,
            "production_modules": [],
            "details": {"error": f"unknown runtime probe: {name}"},
        }
    try:
        return probe()
    except Exception as exc:
        return {
            "pass": False,
            "api_calls": 0,
            "production_modules": [],
            "details": {"error": f"{type(exc).__name__}: {exc}"},
        }


__all__ = ["run_runtime_probe"]
