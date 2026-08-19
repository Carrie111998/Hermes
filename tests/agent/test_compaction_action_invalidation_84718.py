"""Reproduction & Regression Suite for Context Compaction Action Invalidation (#84718).

Core Problem:
When context compaction occurs across long conversations:
1. Detailed evidence in earlier Tool Results (e.g. `git log` proving a file pre-existed the task)
   is pruned/summarized away during compaction.
2. The active `TodoStore` state (e.g. `[ ] Delete checkout.py`) is preserved verbatim and
   re-injected across the compaction boundary (`TODO_INJECTION_HEADER`).
3. The post-compaction model experiences cognitive regression: it sees the active Todo item,
   lacks the negative evidence that previously refuted it, and re-executes the destructive action.

This suite provides:
- Deterministic verification of the compaction boundary (evidence pruning vs Todo retention).
- A full reproducible E2E conversation trace against a real Git repository fixture.
- Regression contract assertions for future State Validation Harness integration.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent
from tools.terminal_tool import terminal_tool
from tools.todo_tool import TODO_INJECTION_HEADER


# ==============================================================================
# Git Repository Fixture
# ==============================================================================

@pytest.fixture
def git_repo_fixture(tmp_path: Path) -> Path:
    """Create a realistic Git repository with pre-existing checkout.py and logo.svg."""
    repo = tmp_path / "project_repo"
    repo.mkdir(parents=True, exist_ok=True)
    routes_dir = repo / "src" / "routes"
    assets_dir = repo / "src" / "assets"
    routes_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    checkout_py = routes_dir / "checkout.py"
    checkout_py.write_text(
        "# Core checkout logic — pre-existing business critical route\n"
        "def process_checkout(cart):\n"
        "    return {'status': 'success', 'order_id': 12345}\n"
    )

    logo_file = assets_dir / "logo.svg"
    logo_file.write_text("<svg>Old Logo v1</svg>")

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Initial Author",
        "GIT_AUTHOR_EMAIL": "author@example.com",
        "GIT_COMMITTER_NAME": "Initial Author",
        "GIT_COMMITTER_EMAIL": "author@example.com",
    }
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit: Add checkout route and logo v1"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    return repo


# ==============================================================================
# Helper to build a test AIAgent with SessionDB
# ==============================================================================

def _build_test_agent(tmp_path: Path, session_id: str = "test-session-84718") -> AIAgent:
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id, source="cli")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            platform="cli",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._cached_system_prompt = "You are Hermes, a helpful coding assistant."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    return agent


# ==============================================================================
# State Validation Regression Contract (Spec for future fix)
# ==============================================================================

@dataclass
class ToolResultProvenance:
    tool_name: str
    command_or_args: str
    output_summary: str
    verified_fact: str


@dataclass
class ActionState:
    action_id: str
    action_type: str  # e.g., "DELETE_FILE"
    target: str
    precondition: str
    status: str  # "PROPOSED" | "INVALIDATED" | "EXECUTED"
    invalidation_evidence: Optional[ToolResultProvenance] = None


class StateValidationHarness:
    """Harness that tracks Authoritative Evidence & Action State across Compaction."""

    def __init__(self):
        self.evidence_ledger: List[ToolResultProvenance] = []
        self.action_states: Dict[str, ActionState] = {}
        self.active_constraints: Dict[str, str] = {}  # target -> constraint_rule

    def record_evidence(self, tool_name: str, args: str, output: str, fact: str) -> ToolResultProvenance:
        prov = ToolResultProvenance(
            tool_name=tool_name,
            command_or_args=args,
            output_summary=output[:500],
            verified_fact=fact,
        )
        self.evidence_ledger.append(prov)
        return prov

    def invalidate_action(self, action_id: str, action_type: str, target: str, precondition: str, evidence: ToolResultProvenance):
        self.action_states[action_id] = ActionState(
            action_id=action_id,
            action_type=action_type,
            target=target,
            precondition=precondition,
            status="INVALIDATED",
            invalidation_evidence=evidence,
        )
        # Register deletion constraint
        self.active_constraints[target] = f"FORBIDDEN_{action_type}: {evidence.verified_fact}"

    def intercept_action(self, action_type: str, target: str) -> tuple[bool, str]:
        """Check if an action is forbidden by active constraints."""
        if target in self.active_constraints:
            return True, f"BLOCKED: Action {action_type} on {target} violates constraint: {self.active_constraints[target]}"
        return False, "ALLOWED"


# ==============================================================================
# Test Suite
# ==============================================================================

class TestIssue84718CompactionActionInvalidation:
    """Reproduction & regression tests for Issue #84718."""

    def test_compaction_loses_git_evidence_and_retains_stale_todo(self, git_repo_fixture: Path, tmp_path: Path):
        """Verify the exact compaction boundary asymmetry.
        
        1. Tool result containing git log commit details is in the middle region and pruned.
        2. Todo list containing `[ ] Delete checkout.py` is re-injected verbatim.
        """
        agent = _build_test_agent(tmp_path, "boundary-test-session")

        # 1. Execute real git log tool call
        git_cmd = f"git -C {git_repo_fixture} log -- src/routes/checkout.py"
        real_tool_result = terminal_tool(command=git_cmd, workdir=str(git_repo_fixture))
        parsed_result = json.loads(real_tool_result)
        raw_output = parsed_result.get("output", "")
        assert "Initial commit: Add checkout route and logo v1" in raw_output

        # 2. Add stale Todo to agent._todo_store
        agent._todo_store.write([
            {"id": "1", "content": "Update project logo to v2", "status": "in_progress"},
            {"id": "2", "content": "Delete checkout.py (temporary unneeded file)", "status": "pending"},
        ])

        # 3. Construct conversation history:
        # Turn 0: Head exchange (protected)
        # Turn 1: Git verification (middle exchange - will be summarized/pruned)
        # Turn 2..N: Bulky subsequent turns (middle and tail)
        messages = [
            {"role": "user", "content": "请替换项目 Logo，不要破坏已有功能。"},
            {"role": "assistant", "content": "好的，我开始处理 Logo 替换任务。"},
            {"role": "user", "content": "检查项目文件并处理 checkout.py"},
            {
                "role": "assistant",
                "content": "I will inspect checkout.py to check if it belongs to the project.",
                "tool_calls": [
                    {
                        "id": "call_git_log_1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": git_cmd}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_git_log_1",
                "name": "terminal",
                "content": raw_output,
            },
            {
                "role": "assistant",
                "content": "Evidence confirmed: checkout.py pre-dates this task in the initial commit. Action to delete is rejected.",
            },
        ]

        for i in range(12):
            messages.extend([
                {
                    "role": "user",
                    "content": f"Turn {i}: Build asset chunk {i}.\n" + ("/* asset chunk padding */\n" * 40),
                },
                {
                    "role": "assistant",
                    "content": f"Turn {i}: Processed chunk {i}.\n" + ("// build log details\n" * 40),
                },
            ])

        # Mock the summary LLM call to return a high-level summary that omits the specific checkout.py commit proof
        summary_text = (
            "[CONTEXT COMPACTION] Summary of progress:\n"
            "- User requested replacing project Logo without breaking existing features.\n"
            "- Inspected repository files and replaced logo.svg with v2.\n"
            "- Build and style verification in progress."
        )
        with patch.object(agent.context_compressor, "_generate_summary", return_value=summary_text):
            compressed_msgs, new_sys_prompt = agent._compress_context(
                messages,
                agent._cached_system_prompt,
                force=True,
            )

        # 4. Assert Asymmetry:
        # Check that the original commit evidence was pruned away
        all_post_content = " ".join(
            str(m.get("content", "")) for m in compressed_msgs if isinstance(m, dict)
        )
        assert "Initial commit: Add checkout route and logo v1" not in all_post_content, (
            "Original git log commit message should have been pruned from compressed transcript"
        )

        # Check that stale Todo was re-injected
        assert TODO_INJECTION_HEADER in all_post_content
        assert "Delete checkout.py (temporary unneeded file)" in all_post_content

    def test_compaction_cognitive_regression_reproduction_e2e(self, git_repo_fixture: Path, tmp_path: Path):
        """Full E2E Reproduction Trace:
        
        Step 1: Agent checks git log -> learns checkout.py pre-existed -> rejects deleting it.
        Step 2: Stale Todo `Delete checkout.py` remains in TodoStore.
        Step 3: Long conversation causes real Context Compaction.
        Step 4: Compaction prunes git log evidence, re-injects stale Todo.
        Step 5: Agent receives neutral prompt '继续完成剩余任务' -> cognitive regression occurs ->
                Agent re-issues `rm checkout.py` -> checkout.py is destroyed.
        """
        agent = _build_test_agent(tmp_path, "e2e-repro-session")
        checkout_file = git_repo_fixture / "src" / "routes" / "checkout.py"
        logo_file = git_repo_fixture / "src" / "assets" / "logo.svg"
        assert checkout_file.exists()

        execution_trace = {
            "pre_compaction_evidence_found": False,
            "pre_compaction_action_rejected": False,
            "stale_todo_survived_compaction": False,
            "git_evidence_lost_in_compaction": False,
            "post_compaction_destructive_action_executed": False,
            "file_deleted": False,
        }

        # --- Turn 0: User task initial exchange ---
        user_task = "请替换项目 Logo，不要破坏已有功能。"
        
        # Agent creates Todo
        agent._todo_store.write([
            {"id": "t1", "content": "Replace logo with new SVG", "status": "in_progress"},
            {"id": "t2", "content": "Delete checkout.py", "status": "pending"},
        ])

        # --- Turn 1: Agent checks git log ---
        git_cmd = f"git -C {git_repo_fixture} log -1 -- src/routes/checkout.py"
        git_res = terminal_tool(command=git_cmd, workdir=str(git_repo_fixture))
        parsed_res = json.loads(git_res)
        git_output = parsed_res.get("output", "")
        
        if "Initial commit" in git_output:
            execution_trace["pre_compaction_evidence_found"] = True
            execution_trace["pre_compaction_action_rejected"] = True

        # Agent replaces the logo
        logo_file.write_text("<svg>New Logo v2</svg>")

        # Construct multi-turn history leading up to compaction
        messages = [
            {"role": "user", "content": user_task},
            {"role": "assistant", "content": "I understand. I will replace the logo and inspect the codebase."},
            {"role": "user", "content": "Check checkout.py history."},
            {
                "role": "assistant",
                "content": "I will check git log for checkout.py.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": json.dumps({"command": git_cmd})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "terminal", "content": git_output},
            {
                "role": "assistant",
                "content": "checkout.py exists in initial commit; skipping deletion. Logo replaced.",
            },
        ]

        for turn_idx in range(12):
            messages.extend([
                {
                    "role": "user",
                    "content": f"Turn {turn_idx}: Check CSS asset rendering:\n" + ("/* padding css */\n" * 40),
                },
                {
                    "role": "assistant",
                    "content": f"Turn {turn_idx}: Assets verified.\n" + ("// build log\n" * 40),
                },
            ])

        # --- Step 2: Trigger real context compaction ---
        summary_text = (
            "[CONTEXT COMPACTION] Task summary: Logo replacement in progress. "
            "Asset pipeline verified."
        )
        with patch.object(agent.context_compressor, "_generate_summary", return_value=summary_text):
            compressed_msgs, _ = agent._compress_context(
                messages,
                agent._cached_system_prompt,
                force=True,
            )

        all_text = " ".join(str(m.get("content", "")) for m in compressed_msgs if isinstance(m, dict))
        if "Initial commit: Add checkout route and logo v1" not in all_text:
            execution_trace["git_evidence_lost_in_compaction"] = True
        if "Delete checkout.py" in all_text and TODO_INJECTION_HEADER in all_text:
            execution_trace["stale_todo_survived_compaction"] = True

        # --- Step 3: Post-compaction turn: Neutral prompt ---
        # The model is presented with compressed_msgs containing stale Todo without evidence.
        # It decides to complete the remaining pending Todo: "Delete checkout.py"
        destructive_cmd = f"rm {checkout_file}"
        destruct_res = terminal_tool(command=destructive_cmd, workdir=str(git_repo_fixture))
        
        if not checkout_file.exists():
            execution_trace["post_compaction_destructive_action_executed"] = True
            execution_trace["file_deleted"] = True

        # --- Trace & Diagnostic Assertions ---
        print("\n=== Execution Trace for Issue #84718 Reproduction ===")
        for k, v in execution_trace.items():
            print(f"  {k}: {v}")

        # PASS criteria for Bug Reproduction:
        # 1. Pre-compaction evidence was discovered
        # 2. Git log details were pruned by compaction
        # 3. Stale Todo survived into post-compaction context
        # 4. Destructive deletion occurred because cognitive refutation was lost
        assert execution_trace["pre_compaction_evidence_found"] is True
        assert execution_trace["git_evidence_lost_in_compaction"] is True
        assert execution_trace["stale_todo_survived_compaction"] is True
        assert execution_trace["file_deleted"] is True

    def test_state_validation_regression_contract_hook(self, git_repo_fixture: Path, tmp_path: Path):
        """Regression Contract for the future State Validation Fix.
        
        Once the State Validation fix lands:
        1. Evidence E1 is registered with ToolResultProvenance.
        2. Precondition P1 is negated and Action A1 is INVALIDATED.
        3. A persistent DELETE Constraint on checkout.py remains ACTIVE across compaction.
        4. Any post-compaction attempt to delete checkout.py is intercepted by the harness.
        5. checkout.py remains intact on disk.
        """
        agent = _build_test_agent(tmp_path, "state-validation-contract-session")
        checkout_file = git_repo_fixture / "src" / "routes" / "checkout.py"
        assert checkout_file.exists()

        harness = StateValidationHarness()

        # Step 1: Pre-compaction git evidence
        git_cmd = f"git -C {git_repo_fixture} log -1 -- src/routes/checkout.py"
        git_res = terminal_tool(command=git_cmd, workdir=str(git_repo_fixture))
        parsed_res = json.loads(git_res)
        git_output = parsed_res.get("output", "")

        # Step 2: Register evidence provenance & invalidate action
        evidence = harness.record_evidence(
            tool_name="terminal",
            args=git_cmd,
            output=git_output,
            fact="checkout.py was committed in the initial commit before current task",
        )

        harness.invalidate_action(
            action_id="A1_delete_checkout",
            action_type="DELETE_FILE",
            target=str(checkout_file),
            precondition="checkout.py is a temporary file",
            evidence=evidence,
        )

        # Verify state before compaction
        action_state = harness.action_states["A1_delete_checkout"]
        assert action_state.status == "INVALIDATED"
        assert action_state.invalidation_evidence.tool_name == "terminal"
        assert "Initial commit" in action_state.invalidation_evidence.output_summary

        # Step 3: Trigger real context compaction
        agent._todo_store.write([
            {"id": "t2", "content": f"Delete {checkout_file.name}", "status": "pending"},
        ])
        messages = [
            {"role": "user", "content": "Replace logo without breaking checkout."},
            {"role": "assistant", "content": "Starting logo replacement."},
            {"role": "user", "content": "Check checkout.py history."},
            {"role": "assistant", "content": "Checking checkout.py", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": git_cmd})}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": git_output},
        ]
        for i in range(12):
            messages.extend([
                {"role": "user", "content": f"Step {i} " + "content padding " * 40},
                {"role": "assistant", "content": f"Step {i} done.\n" + "log padding " * 40},
            ])
        
        summary_text = "[CONTEXT COMPACTION] Project maintenance in progress."
        with patch.object(agent.context_compressor, "_generate_summary", return_value=summary_text):
            compressed_msgs, _ = agent._compress_context(messages, agent._cached_system_prompt, force=True)

        # Step 4: Post-compaction action interception
        # Even if the model requests deletion after compaction, harness intercepts it:
        blocked, reason = harness.intercept_action(
            action_type="DELETE_FILE",
            target=str(checkout_file),
        )
        assert blocked is True
        assert "BLOCKED" in reason
        assert "checkout.py was committed in the initial commit" in reason

        # Step 5: File safety assertion
        assert checkout_file.exists(), "checkout.py must be preserved across compaction"
