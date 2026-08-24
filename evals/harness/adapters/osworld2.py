"""OSWorld 2.0 adapter (stub).

OSWorld 2.0 is a 108-task long-horizon computer-use benchmark from XLANG Lab
(arXiv:2606.29537, June 2026). Each task is a desktop workflow averaging ~1.6
hours of human effort and 318+ tool calls per task. Even Claude Opus 4.8
scores only 20.6% binary completion at 500 steps.

Source: https://osworld-v2.xlang.ai/

This stub documents what a real adapter will need. Implementation deferred
because it requires:
  - Docker VM provisioning for computer-use tasks
  - Screenshot capture + diff grader
  - 108 task definitions (or subset with limit=10 for baseline)
  - Per-task environment setup scripts

Status: NOT IMPLEMENTED — only the adapter class skeleton.
"""

from __future__ import annotations

from ..harness import AgentRun, GraderResult, Task


class OSWorld2Adapter:
    name = "osworld2"
    version = "v2.0 (2026-06)"
    task_count = 108

    def load_tasks(self, limit: int | None = None) -> list[Task]:
        """Fetch the task list from https://osworld-v2.xlang.ai/.

        Real implementation will:
          1. Clone the OSWorld 2.0 task definitions repo
          2. Parse each task JSON (id, prompt, expected_screenshot, env_setup_script)
          3. Return as Task list, optionally sliced by limit
        """
        raise NotImplementedError(
            "OSWorld 2.0 adapter requires the task definitions from "
            "https://osworld-v2.xlang.ai/ — fetch + parse + return."
        )

    def setup_environment(self, task: Task) -> str:
        """Provision a Docker VM with the desktop OS + starting state for this task.

        Real implementation will:
          1. Spin up a docker container with the right OS image (Ubuntu/GNOME, macOS, Windows)
          2. Run task.env_setup to put the system in the task's starting state
          3. Expose VNC + screenshot endpoints
          4. Return the container ID as env_handle
        """
        raise NotImplementedError(
            "OSWorld 2.0 environment setup needs docker + VNC provisioning."
        )

    def run_agent(self, task: Task, env_handle: str, model: str = "default") -> AgentRun:
        """Run Hermes Agent against the task. Hermes drives the desktop via
        computer_use tool (already available) which is OSWorld 2.0's expected
        input format. Capture screenshots + tool calls into the trajectory.
        """
        raise NotImplementedError(
            "OSWorld 2.0 agent execution needs computer_use tool wired to "
            "the docker container's VNC endpoint."
        )

    def grade(self, task: Task, run: AgentRun) -> GraderResult:
        """Compare final screenshot against expected_screenshot via pixel diff
        OR via task-specific verification script. OSWorld 2.0 supports both.
        """
        raise NotImplementedError(
            "OSWorld 2.0 grading uses pixel-diff against expected screenshot "
            "or task-specific verifier."
        )


def register() -> None:
    """Register this adapter with the harness registry."""
    from . import register as _register
    _register(OSWorld2Adapter.name, OSWorld2Adapter)
