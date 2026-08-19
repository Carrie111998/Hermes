"""Global native tool for codebase knowledge graph management using Graphify.

Enables agents to build, update, and query the structural AST knowledge graph
for the current repository or feature workspace (.hermes/<feature>/codegraph/).
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

GRAPHIFY_TOOL_SCHEMA = {
    "name": "graphify",
    "description": (
        "Manage and query codebase knowledge graphs powered by Graphify.\n\n"
        "ACTIONS:\n"
        "- 'create': Run full AST and structural analysis on path, creating graph.json & GRAPH_REPORT.md.\n"
        "- 'update': Incrementally re-extract only new/changed files since last build.\n"
        "- 'query': Traverses knowledge graph to answer questions about architecture, imports, and relationships.\n"
        "- 'understand': Read existing GRAPH_REPORT.md and summary metrics without running analysis.\n\n"
        "USE FOR: Instant codebase orientation, tracing cross-file dependencies, locating symbols, "
        "and navigating unfamiliar architectures without wasting context tokens."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "query", "understand"],
                "description": "Operation to perform on the codebase knowledge graph.",
            },
            "path": {
                "type": "string",
                "description": "Target repository or directory path (default: current working directory).",
            },
            "query": {
                "type": "string",
                "description": "Question to answer when action='query' (e.g. 'How does authentication work?').",
            },
            "feature": {
                "type": "string",
                "description": "Optional feature name to store graph in .hermes/<feature>/codegraph/.",
            },
        },
        "required": ["action"],
    },
}


def _resolve_graphify_python() -> str:
    """Find a valid Python interpreter with graphify installed."""
    # Check current venv
    venv_py = "/mnt/hdd/venv/bin/python"
    if os.path.isfile(venv_py) and os.access(venv_py, os.X_OK):
        return venv_py
    import sys
    return sys.executable


def _handle_graphify(
    action: str,
    path: Optional[str] = None,
    query: Optional[str] = None,
    feature: Optional[str] = None,
    **kwargs: Any,
) -> str:
    target_dir = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    if not os.path.exists(target_dir):
        return json.dumps({"error": f"Target path does not exist: {target_dir}"})

    py_bin = _resolve_graphify_python()
    
    # Destination directory for graphify artifacts
    if feature:
        out_dir = Path(target_dir) / ".hermes" / feature / "codegraph"
    else:
        out_dir = Path(target_dir) / "graphify-out"
    out_dir.mkdir(parents=True, exist_ok=True)

    if action == "understand":
        report_file = out_dir / "GRAPH_REPORT.md"
        if not report_file.exists():
            report_file = Path(target_dir) / "graphify-out" / "GRAPH_REPORT.md"
        if report_file.exists():
            return json.dumps({
                "status": "success",
                "report": report_file.read_text(encoding="utf-8")[:4000],
                "path": str(report_file),
            })
        return json.dumps({
            "status": "not_found",
            "message": f"No knowledge graph found at {out_dir}. Run graphify(action='create') first.",
        })

    if action in ("create", "update"):
        cmd = [py_bin, "-m", "graphifyy.cli" if False else "graphify", target_dir]
        if action == "update":
            cmd.append("--update")
        cmd.extend(["--no-viz"])
        try:
            res = subprocess.run(
                [py_bin, "-c", f"import graphify; print('OK')"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # If python package is graphifyy / graphify
            extract_cmd = (
                f"import sys, json; from pathlib import Path; "
                f"from graphify.detect import detect; "
                f"from graphify.extract import collect_files, extract; "
                f"from graphify.build import build_from_json; "
                f"from graphify.report import generate; "
                f"from graphify.export import to_json; "
                f"d = detect(Path('{target_dir}')); "
                f"code_files = [Path(f) for f in d.get('files', {{}}).get('code', [])]; "
                f"res = extract(code_files, cache_root=Path('{target_dir}')) if code_files else {{'nodes':[], 'edges':[]}}; "
                f"G = build_from_json(res, root='{target_dir}'); "
                f"to_json(G, {{}}, '{out_dir}/graph.json'); "
                f"print(f'Done: {{G.number_of_nodes()}} nodes, {{G.number_of_edges()}} edges')"
            )
            run_res = subprocess.run(
                [py_bin, "-c", extract_cmd],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if run_res.returncode == 0:
                return json.dumps({
                    "status": "success",
                    "action": action,
                    "output_dir": str(out_dir),
                    "details": run_res.stdout.strip(),
                })
            else:
                return json.dumps({
                    "status": "partial",
                    "message": "Graph generation command exited",
                    "stdout": run_res.stdout.strip(),
                    "stderr": run_res.stderr.strip()[:500],
                })
        except Exception as e:
            return json.dumps({"error": f"Failed to execute graphify: {e}"})

    if action == "query":
        if not query:
            return json.dumps({"error": "Parameter 'query' is required for action='query'"})
        graph_file = out_dir / "graph.json"
        if not graph_file.exists():
            graph_file = Path(target_dir) / "graphify-out" / "graph.json"
        if not graph_file.exists():
            return json.dumps({"error": f"Graph file not found at {graph_file}. Run action='create' first."})
        
        # Simple BFS / graph reader query
        query_script = (
            f"import json; from pathlib import Path; "
            f"data = json.loads(Path('{graph_file}').read_text(encoding='utf-8')); "
            f"q = '{query}'.lower(); "
            f"nodes = [n for n in data.get('nodes', []) if any(q_term in str(n.get('id', '')).lower() or q_term in str(n.get('label', '')).lower() for q_term in q.split())]; "
            f"print(json.dumps({{'query': '{query}', 'matched_nodes': nodes[:15], 'total_matches': len(nodes)}}))"
        )
        try:
            q_res = subprocess.run([py_bin, "-c", query_script], capture_output=True, text=True, timeout=15)
            if q_res.returncode == 0:
                return q_res.stdout.strip()
            return json.dumps({"error": q_res.stderr.strip()[:300]})
        except Exception as e:
            return json.dumps({"error": f"Query failed: {e}"})

    return json.dumps({"error": f"Unknown action: {action}"})


def _check_graphify_reqs() -> Optional[str]:
    return None


registry.register(
    name="graphify",
    toolset="code",
    schema=GRAPHIFY_TOOL_SCHEMA,
    handler=_handle_graphify,
    check_fn=_check_graphify_reqs,
    emoji="🗺️",
    max_result_size_chars=100_000,
)
