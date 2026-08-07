#!/usr/bin/env python3
"""Live test harness for Hermes Agent's Tool Search feature.

Runs selectable fake-MCP and built-in direct-vs-deferred scenarios against a
real AIAgent and records exactly what the model did. Use ``--list`` or
``--dry-run`` to inspect the suite without making model calls.

For each scenario we record:
  - the full message transcript
  - the sequence of tool calls (name + args) the model emitted
  - which underlying tools actually got invoked (after bridge unwrap)
  - the final assistant response
  - timing and round-trip count

By default, MCP scenarios run enabled/disabled and built-in scenarios run
deferred/direct. Built-in rollback mode (tool search fully off) is selectable.

Output: ./out/<scenario_id>__<mode>.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Force-isolate the test environment BEFORE any hermes imports.
ORIGINAL_HOME = os.environ.get("HERMES_HOME")
ORIGINAL_AUTH = Path.home() / ".hermes" / "auth.json"

_THIS_DIR = Path(__file__).resolve().parent
_WORKTREE_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_WORKTREE_ROOT))

# ---------------------------------------------------------------------------
# Fake MCP tools — realistic shape, varied difficulty for retrieval
# ---------------------------------------------------------------------------

FAKE_MCP_TOOLS: List[Dict[str, Any]] = [
    # GitHub cluster
    {
        "name": "github_create_issue",
        "description": "Open a new issue in a GitHub repository. Use when the user wants to report a bug or request a feature in a repo.",
        "params": {"repo": ("string", "Repository in owner/name form"),
                   "title": ("string", "Issue title"),
                   "body": ("string", "Issue body in Markdown")},
        "returns": lambda args: {"ok": True, "issue_url": f"https://github.com/{args.get('repo','x/y')}/issues/42"},
    },
    {
        "name": "github_search_repos",
        "description": "Search GitHub repositories by free-text query. Returns a ranked list of repo names with star counts.",
        "params": {"query": ("string", "Search terms"),
                   "limit": ("integer", "Max results")},
        "returns": lambda args: {"results": [{"name": "fake/repo-1", "stars": 1200},
                                             {"name": "fake/repo-2", "stars": 540}]},
    },
    {
        "name": "github_close_pr",
        "description": "Close a pull request without merging it. Use when the PR should be abandoned.",
        "params": {"repo": ("string", ""), "pr_number": ("integer", "")},
        "returns": lambda args: {"ok": True, "state": "closed"},
    },
    {
        "name": "github_list_pulls",
        "description": "List open pull requests for a repository.",
        "params": {"repo": ("string", "")},
        "returns": lambda args: {"pulls": [{"number": 31163, "title": "feat(tools): tool search"}]},
    },

    # Slack cluster
    {
        "name": "slack_send_message",
        "description": "Post a message into a Slack channel as the connected workspace's app.",
        "params": {"channel": ("string", "Channel name with leading #"),
                   "text": ("string", "Message body")},
        "returns": lambda args: {"ok": True, "ts": "1716528000.000100"},
    },
    {
        "name": "slack_list_channels",
        "description": "Return all channels visible to the connected Slack workspace bot.",
        "params": {},
        "returns": lambda args: {"channels": ["#general", "#engineering", "#random"]},
    },
    {
        "name": "slack_set_status",
        "description": "Set the current user's Slack status (emoji + text).",
        "params": {"emoji": ("string", ""), "text": ("string", "")},
        "returns": lambda args: {"ok": True},
    },

    # Calendar cluster (intentionally vague names to stress retrieval)
    {
        "name": "evt_create",
        "description": "Add an event to the connected calendar. Used for scheduling meetings.",
        "params": {"title": ("string", ""),
                   "start": ("string", "ISO 8601 datetime"),
                   "duration_min": ("integer", "")},
        "returns": lambda args: {"ok": True, "event_id": "evt_abc"},
    },
    {
        "name": "evt_list",
        "description": "List upcoming calendar events.",
        "params": {"max_results": ("integer", "")},
        "returns": lambda args: {"events": [{"id": "evt_1", "title": "Standup", "start": "2026-05-25T09:00:00Z"}]},
    },

    # Knowledge / docs (paraphrased name to stress retrieval)
    {
        "name": "docsearch_query",
        "description": "Search the user's internal documentation index for matching pages.",
        "params": {"q": ("string", "Search query"), "limit": ("integer", "")},
        "returns": lambda args: {"hits": [{"title": "Onboarding", "url": "https://docs/x"}]},
    },
    {
        "name": "docsearch_fetch",
        "description": "Fetch the full markdown content of one document by ID.",
        "params": {"id": ("string", "")},
        "returns": lambda args: {"content": "# Onboarding\n..."},
    },

    # Database
    {
        "name": "db_query",
        "description": "Run a read-only SQL query against the analytics database.",
        "params": {"sql": ("string", "SELECT ... statement")},
        "returns": lambda args: {"rows": [{"id": 1, "name": "alice"}]},
    },
    {
        "name": "db_describe_table",
        "description": "Show the schema of a database table.",
        "params": {"table": ("string", "")},
        "returns": lambda args: {"columns": [{"name": "id", "type": "int"}, {"name": "name", "type": "text"}]},
    },

    # Linear
    {
        "name": "linear_create_ticket",
        "description": "Create a new Linear issue (ticket) in the connected workspace.",
        "params": {"title": ("string", ""), "body": ("string", ""), "priority": ("integer", "1-4")},
        "returns": lambda args: {"ok": True, "id": "ENG-101"},
    },
    {
        "name": "linear_assign",
        "description": "Reassign a Linear ticket to a different user.",
        "params": {"ticket_id": ("string", ""), "user": ("string", "")},
        "returns": lambda args: {"ok": True},
    },

    # Notion
    {
        "name": "notion_create_page",
        "description": "Create a new page in the connected Notion workspace.",
        "params": {"title": ("string", ""), "body": ("string", ""), "parent": ("string", "")},
        "returns": lambda args: {"ok": True, "page_id": "abc123"},
    },

    # Random others (filler / distractors)
    {
        "name": "weather_get",
        "description": "Look up the current weather for a city.",
        "params": {"city": ("string", "")},
        "returns": lambda args: {"city": args.get("city", ""), "temp_c": 19, "summary": "Cloudy"},
    },
    {
        "name": "translate_text",
        "description": "Translate a short text from one language to another.",
        "params": {"text": ("string", ""), "to": ("string", "Target language code")},
        "returns": lambda args: {"translated": args.get("text", "") + " [translated to " + args.get("to", "??") + "]"},
    },
    {
        "name": "pdf_extract",
        "description": "Extract text from a PDF file given its path.",
        "params": {"path": ("string", "")},
        "returns": lambda args: {"text": "[fake PDF text]"},
    },
    {
        "name": "yt_transcript",
        "description": "Fetch the transcript for a YouTube video by URL.",
        "params": {"url": ("string", "")},
        "returns": lambda args: {"transcript": "[fake transcript]"},
    },
]


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "A_obvious_single",
        "description": "Single tool, obvious name in the user request",
        "prompt": (
            "Open a GitHub issue in repo 'acme/widget' titled 'Crash on startup' "
            "with body 'App crashes immediately after launch when offline.' "
            "Then tell me you're done. Don't do anything else."
        ),
        "expected_underlying_tools": ["github_create_issue"],
    },
    {
        "id": "B_vague_paraphrased",
        "description": "Single tool, paraphrased intent (tests retrieval quality)",
        "prompt": (
            "Add a meeting to my schedule for tomorrow morning at 10am called "
            "'Design review', 30 minutes long. Then tell me you're done. Don't do anything else."
        ),
        "expected_underlying_tools": ["evt_create"],
    },
    {
        "id": "C_multi_tool_chain",
        "description": "Multi-step task requiring 2-3 deferred tools",
        "prompt": (
            "Find the open pull requests on repo 'acme/widget', then post a "
            "summary of how many there are to the #engineering Slack channel. "
            "Then tell me you're done."
        ),
        "expected_underlying_tools": ["github_list_pulls", "slack_send_message"],
    },
    {
        "id": "D_core_plus_deferred",
        "description": "Task uses BOTH a core tool (read_file) and a deferred tool",
        "prompt": (
            "Read the file at /tmp/livetest/notes.txt (it exists, just read it) "
            "and then post its contents to the #random Slack channel. Tell me you're done."
        ),
        "expected_underlying_tools": ["read_file", "slack_send_message"],
        "expected_core_tool_direct": True,  # must NOT use tool_call for read_file
    },
    {
        "id": "E_no_tool_needed",
        "description": "Question doesn't need any tool — model should just answer",
        "prompt": "What's 7 times 8? Answer with just the number.",
        "expected_underlying_tools": [],
    },
]


BUILTIN_DEFER_GROUPS = [
    "browser",
    "session_search",
    "delegation",
    "code_execution",
    "todo",
    "vision",
]

BUILTIN_SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "builtin_tool_free",
        "description": "Tool-free answer with the broad built-in catalog deferred",
        "prompt": "What's 7 times 8? Answer with just the number.",
        "expected_underlying_tools": [],
        "builtin_defer_groups": BUILTIN_DEFER_GROUPS,
    },
    {
        "id": "builtin_browser",
        "description": "Navigate with a deferred browser tool",
        "prompt": (
            "Open https://example.com in the browser and report the page title. "
            "Do not use web search."
        ),
        "expected_underlying_tools": ["browser_navigate"],
        "builtin_defer_groups": ["browser"],
    },
    {
        "id": "builtin_file_then_browser",
        "description": "Use eager read_file followed by a deferred browser tool",
        "prompt": (
            "Read /tmp/livetest/notes.txt, then open https://example.com in the "
            "browser. Report the file text and page title. Do not use web search."
        ),
        "expected_underlying_tools": ["read_file", "browser_navigate"],
        "expected_eager_tools": ["read_file"],
        "builtin_defer_groups": ["browser"],
    },
    {
        "id": "builtin_session_search",
        "description": "Search isolated session history through the deferred bridge",
        "prompt": (
            "Search prior sessions for the phrase 'livetest sentinel' and tell me "
            "whether any match exists."
        ),
        "expected_underlying_tools": ["session_search"],
        "builtin_defer_groups": ["session_search"],
    },
    {
        "id": "builtin_delegation",
        "description": "Delegate a tiny task through the deferred bridge",
        "prompt": (
            "Delegate this exact task to one leaf worker: calculate 19 + 23. "
            "Return only the worker's answer."
        ),
        "expected_underlying_tools": ["delegate_task"],
        "builtin_defer_groups": ["delegation"],
    },
    {
        "id": "builtin_code_execution",
        "description": "Execute a small calculation through the deferred bridge",
        "prompt": "Use execute_code to calculate sum(range(10)); report the result.",
        "expected_underlying_tools": ["execute_code"],
        "builtin_defer_groups": ["code_execution"],
    },
    {
        "id": "builtin_todo",
        "description": "Create planning state through the deferred bridge",
        "prompt": "Use the todo tool to add one item named 'livetest item', then stop.",
        "expected_underlying_tools": ["todo"],
        "builtin_defer_groups": ["todo"],
    },
    {
        "id": "builtin_vision",
        "description": "Analyze a local image through the deferred bridge",
        "prompt": (
            "Use vision_analyze on /tmp/livetest/pixel.png and briefly describe "
            "the image."
        ),
        "expected_underlying_tools": ["vision_analyze"],
        "builtin_defer_groups": ["vision"],
    },
]

SCENARIO_SUITES = {
    "mcp": SCENARIOS,
    "builtins": BUILTIN_SCENARIOS,
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def setup_isolated_home(enabled: bool, listing: str = "off",
                        listing_max_tokens: int = 4000,
                        model: str = "anthropic/claude-haiku-4.5",
                        builtins_enabled: bool = False,
                        builtin_defer_groups: List[str] | None = None,
                        builtin_min_schema_tokens: int = 0) -> Path:
    """Create a fresh ~/.hermes/ for one test, copying minimal credentials.

    Also reads OPENROUTER_API_KEY from the user's real ``~/.hermes/.env`` so
    the agent can authenticate against OpenRouter inside the isolated home.
    """
    home_dir = Path(tempfile.mkdtemp(prefix="hermes_ts_live_"))
    hermes_home = home_dir / ".hermes"
    hermes_home.mkdir(parents=True)

    if ORIGINAL_AUTH.exists():
        shutil.copy(ORIGINAL_AUTH, hermes_home / "auth.json")

    # Copy .env so OPENROUTER_API_KEY (or others) are visible to the agent
    # running inside the isolated home.
    real_env_file = Path.home() / ".hermes" / ".env"
    if real_env_file.exists():
        shutil.copy(real_env_file, hermes_home / ".env")
        # Also load the real user env into this process so the provider
        # resolver can authenticate. We go through the canonical loader
        # (python-dotenv under the hood) rather than parsing the file by
        # hand — it never materializes the secret in a local variable in
        # this module, which both avoids a hand-rolled parser bug and keeps
        # static analysis from tainting the transcript records with the key.
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv(hermes_home=str(Path.home() / ".hermes"))

    cfg = {
        "model": {
            "provider": "openrouter",
            "model": model,
        },
        "tools": {
            "tool_search": {
                "enabled": "on" if enabled else "off",
                "threshold_pct": 10,
                "search_default_limit": 5,
                "max_search_limit": 20,
                "listing": listing,
                "listing_max_tokens": listing_max_tokens,
                "builtins": {
                    "enabled": builtins_enabled,
                    "defer": (
                        BUILTIN_DEFER_GROUPS
                        if builtin_defer_groups is None
                        else builtin_defer_groups
                    ),
                    "min_schema_tokens": builtin_min_schema_tokens,
                },
            },
        },
        "logging": {"level": "WARNING"},
    }
    (hermes_home / "config.yaml").write_text(_yaml_dump(cfg), encoding="utf-8")
    return hermes_home


def _yaml_dump(obj: Any) -> str:
    try:
        import yaml
        return yaml.safe_dump(obj, sort_keys=False)
    except ImportError:
        return json.dumps(obj, indent=2)


def register_fake_tools() -> int:
    """Register the FAKE_MCP_TOOLS into the live tool registry."""
    from tools.registry import registry

    def make_handler(tool_def):
        def _handler(*args, **kwargs):
            try:
                return json.dumps(tool_def["returns"](kwargs), ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": f"fake tool handler error: {e}"})
        return _handler

    count = 0
    for tdef in FAKE_MCP_TOOLS:
        properties = {}
        required = []
        for p_name, (p_type, p_desc) in tdef["params"].items():
            properties[p_name] = {"type": p_type, "description": p_desc}
            required.append(p_name)

        registry.register(
            name=tdef["name"],
            toolset="mcp-fake",
            schema={
                "name": tdef["name"],
                "description": tdef["description"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
            handler=make_handler(tdef),
        )
        count += 1
    return count


def reset_module_state():
    """Drop cached modules so the new HERMES_HOME takes effect."""
    keys = [k for k in sys.modules.keys()
            if k.startswith(("tools.", "model_tools", "toolsets",
                             "hermes_cli", "agent.", "run_agent"))]
    for k in keys:
        del sys.modules[k]


def _prepare_fixtures() -> None:
    fixture_dir = Path("/tmp/livetest")
    fixture_dir.mkdir(exist_ok=True)
    (fixture_dir / "notes.txt").write_text(
        "Hello from the test fixture.\n", encoding="utf-8"
    )
    # One opaque white pixel. Keeping the fixture inline makes the harness
    # portable without adding a binary test artifact.
    (fixture_dir / "pixel.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMA"
        "ASsJTYQAAAAASUVORK5CYII="
    ))


def run_one_scenario(
    scenario: Dict[str, Any],
    out_dir: Path,
    *,
    suite: str = "mcp",
    mode: str,
) -> Dict[str, Any]:
    """Run one selected scenario/mode combination and record its transcript."""
    reset_module_state()
    builtin_disclosure = suite == "builtins" and mode == "deferred"
    tool_search_enabled = mode not in ("disabled", "off")
    home = setup_isolated_home(
        enabled=tool_search_enabled,
        builtins_enabled=builtin_disclosure,
        builtin_defer_groups=scenario.get("builtin_defer_groups"),
        builtin_min_schema_tokens=0,
    )
    os.environ["HERMES_HOME"] = str(home)

    _prepare_fixtures()

    n_registered = register_fake_tools() if suite == "mcp" else 0

    # Capture tool calls via a hook on the registry dispatch path. We use the
    # registry hook (rather than the run_agent.handle_function_call binding,
    # which is already cached by tool_executor) because the dispatch call is
    # the one place every underlying tool call lands. Bridge calls are
    # extracted from the message transcript after the run.
    tool_call_log: List[Dict[str, Any]] = []

    from tools.registry import registry
    original_dispatch = registry.dispatch

    def logging_dispatch(name, args, **kw):
        tool_call_log.append({"name": name, "args": _trim_args(args)})
        return original_dispatch(name, args, **kw)
    registry.dispatch = logging_dispatch

    # Build agent and run
    started = time.time()
    error = None
    final_response = ""
    messages_out = []
    try:
        from run_agent import AIAgent
        agent = AIAgent(
            provider="openrouter",
            model="anthropic/claude-haiku-4.5",
            enabled_toolsets=None,  # Default = all available toolsets, including the registered mcp-fake tools
            quiet_mode=True,
            save_trajectories=False,
            skip_context_files=True,
            skip_memory=True,
            platform="cli",
            max_iterations=15,
        )
        result = agent.run_conversation(
            user_message=scenario["prompt"],
            system_message=(
                "You are a test agent. Complete the user's task using available "
                "tools. Be concise; don't add commentary beyond what's needed."
            ),
        )
        if isinstance(result, dict):
            final_response = result.get("final_response") or ""
            messages_out = result.get("messages") or []
        else:
            final_response = str(result)
    except Exception as e:
        error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    finally:
        registry.dispatch = original_dispatch

    elapsed = time.time() - started

    # Extract bridge calls from the message transcript. Easier and more
    # accurate than monkey-patching: this is the actual wire shape the
    # model emitted.
    bridge_call_log = _extract_bridge_calls(messages_out)

    # Compose the trace.
    record = {
        "scenario_id": scenario["id"],
        "scenario_description": scenario["description"],
        "suite": suite,
        "mode": mode,
        "tool_search_enabled": tool_search_enabled,
        "builtin_disclosure_enabled": builtin_disclosure,
        "builtin_defer_groups": scenario.get("builtin_defer_groups", []),
        "model": "anthropic/claude-haiku-4.5 (via openrouter)",
        "prompt": scenario["prompt"],
        "expected_underlying_tools": scenario.get("expected_underlying_tools", []),
        "n_fake_tools_registered": n_registered,
        "elapsed_seconds": round(elapsed, 2),
        "bridge_calls": bridge_call_log,
        "underlying_tool_calls": tool_call_log,
        "final_response": _redact_secrets(final_response),
        "n_iterations": _count_assistant_turns(messages_out),
        "error": _redact_secrets(error) if error else error,
    }

    out_path = out_dir / f"{scenario['id']}__{mode}.json"
    out_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

    # Cleanup
    shutil.rmtree(home.parent, ignore_errors=True)
    return record


def _redact_secrets(text: str) -> str:
    """Strip anything secret-shaped from text before it is stored or printed.

    The harness runs against a real OpenRouter key, and ``error`` can carry a
    full traceback that — for an auth failure — may echo a request header or
    URL containing the key. We never want a credential landing in a checked-in
    transcript or the console, so we mask:
      * the live OPENROUTER_API_KEY value, if present in the environment, and
      * any ``sk-``/``sk-or-`` style bearer token by pattern.
    """
    if not text:
        return text
    out = text
    live_key = os.environ.get("OPENROUTER_API_KEY")
    if live_key and len(live_key) >= 8:
        out = out.replace(live_key, "[REDACTED]")
    out = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "[REDACTED]", out)
    out = re.sub(r"(?i)(authorization|bearer)\s*[:=]\s*\S+", r"\1: [REDACTED]", out)
    return out


def _trim_args(args: Any, max_chars: int = 300) -> Any:
    """Trim long string args so the log stays readable."""
    if not isinstance(args, dict):
        return args
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > max_chars:
            out[k] = v[:max_chars] + f"...[{len(v)-max_chars} chars trimmed]"
        else:
            out[k] = v
    return out


def _count_assistant_turns(messages: List[Dict[str, Any]]) -> int:
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")


def _extract_bridge_calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pull out every tool_search / tool_describe / tool_call from a transcript."""
    bridges = ("tool_search", "tool_describe", "tool_call")
    out: List[Dict[str, Any]] = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls") or []
        for c in tcs:
            if not isinstance(c, dict):
                continue
            fn = c.get("function") or {}
            name = fn.get("name")
            if name in bridges:
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {"_raw": raw_args}
                out.append({"name": name, "args": _trim_args(args)})
    return out


def build_run_matrix(
    suite: str,
    scenario_ids: List[str] | None = None,
    modes: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """Resolve CLI selection into deterministic live cases without running them."""
    suite_names = list(SCENARIO_SUITES) if suite == "all" else [suite]
    available = {
        scenario["id"]
        for suite_name in suite_names
        for scenario in SCENARIO_SUITES[suite_name]
    }
    requested = set(scenario_ids or available)
    unknown = sorted(requested - available)
    if unknown:
        raise ValueError(f"unknown scenario(s) for suite {suite}: {', '.join(unknown)}")

    allowed_modes = {
        "mcp": ["enabled", "disabled"],
        "builtins": ["deferred", "direct", "off"],
    }
    default_modes = {
        "mcp": ["enabled", "disabled"],
        "builtins": ["deferred", "direct"],
    }
    cases: List[Dict[str, Any]] = []
    for suite_name in suite_names:
        selected_modes = modes or default_modes[suite_name]
        selected_modes = [mode for mode in selected_modes if mode in allowed_modes[suite_name]]
        for scenario in SCENARIO_SUITES[suite_name]:
            if scenario["id"] not in requested:
                continue
            for mode in selected_modes:
                cases.append({"suite": suite_name, "scenario": scenario, "mode": mode})
    if not cases:
        raise ValueError("selection produced no runnable cases")
    return cases


def _case_manifest(case: Dict[str, Any]) -> Dict[str, Any]:
    scenario = case["scenario"]
    return {
        "suite": case["suite"],
        "scenario_id": scenario["id"],
        "description": scenario["description"],
        "mode": case["mode"],
        "expected_underlying_tools": scenario.get("expected_underlying_tools", []),
        "expected_eager_tools": scenario.get("expected_eager_tools", []),
        "builtin_defer_groups": scenario.get("builtin_defer_groups", []),
    }


def _parse_args(argv: List[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("mcp", "builtins", "all"), default="mcp")
    parser.add_argument("--scenario", action="append", dest="scenario_ids")
    parser.add_argument(
        "--mode",
        action="append",
        choices=("enabled", "disabled", "deferred", "direct", "off"),
        dest="modes",
    )
    parser.add_argument("--list", action="store_true", help="List scenarios without running them")
    parser.add_argument("--dry-run", action="store_true", help="Print selected cases as JSON")
    parser.add_argument("--out-dir", type=Path, default=_THIS_DIR / "out")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cases = build_run_matrix(args.suite, args.scenario_ids, args.modes)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    manifest = [_case_manifest(case) for case in cases]
    if args.list:
        seen = set()
        for item in manifest:
            key = (item["suite"], item["scenario_id"])
            if key in seen:
                continue
            seen.add(key)
            print(f"{item['suite']:8} {item['scenario_id']:28} {item['description']}")
        return 0
    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return 0

    out_dir = args.out_dir
    out_dir.mkdir(exist_ok=True)
    print(f"Writing transcripts to: {out_dir}")

    summary = []
    try:
        for case in cases:
            scenario = case["scenario"]
            mode = case["mode"]
            print(f"\n{'='*72}\nScenario {scenario['id']} ({case['suite']}={mode})\n{'='*72}")
            record = run_one_scenario(
                scenario,
                out_dir,
                suite=case["suite"],
                mode=mode,
            )
            n_bridge = len(record["bridge_calls"])
            n_under = len(record["underlying_tool_calls"])
            err = record["error"]
            print(f"  bridge calls: {n_bridge}, underlying tool calls: {n_under}, "
                  f"elapsed: {record['elapsed_seconds']}s, error: {bool(err)}")
            if err:
                print(f"  ERROR: {err[:300]}")
            summary.append({
                "scenario": scenario["id"],
                "suite": case["suite"],
                "mode": mode,
                "enabled": record["tool_search_enabled"],
                "n_bridge": n_bridge,
                "n_underlying": n_under,
                "elapsed": record["elapsed_seconds"],
                "error": bool(err),
                "underlying_tools_called": [c["name"] for c in record["underlying_tool_calls"]],
                "expected": scenario.get("expected_underlying_tools", []),
            })

        summary_path = out_dir / "_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nSummary saved to: {summary_path}")
    finally:
        # Restore the caller's profile even when a live case fails.
        if ORIGINAL_HOME is not None:
            os.environ["HERMES_HOME"] = ORIGINAL_HOME
        else:
            os.environ.pop("HERMES_HOME", None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
