"""Mixin extracted verbatim from ``run_agent.py`` (godfile extraction wave 1).

The methods in this module were moved character-for-character from the
``AIAgent`` class in ``run_agent.py``; class attributes referenced via
``self.``/``cls.`` still resolve through the MRO on ``AIAgent``.
"""

from typing import Any, Dict, List

from agent.trajectory import save_trajectory as _save_trajectory_to_file


class TrajectoryMixin:
    def _format_tools_for_system_message(self) -> str:
        """Forwarder — see ``agent.system_prompt.format_tools_for_system_message``."""
        from agent.system_prompt import format_tools_for_system_message
        return format_tools_for_system_message(self)

    def _convert_to_trajectory_format(self, messages: List[Dict[str, Any]], user_query: str, completed: bool) -> List[Dict[str, Any]]:
        """Forwarder — see ``agent.agent_runtime_helpers.convert_to_trajectory_format``."""
        from agent.agent_runtime_helpers import convert_to_trajectory_format
        return convert_to_trajectory_format(self, messages, user_query, completed)

    def _save_trajectory(self, messages: List[Dict[str, Any]], user_query: str, completed: bool):
        """
        Save conversation trajectory to JSONL file.
        
        Args:
            messages (List[Dict]): Complete message history
            user_query (str): Original user query
            completed (bool): Whether the conversation completed successfully
        """
        if not self.save_trajectories:
            return
        
        trajectory = self._convert_to_trajectory_format(messages, user_query, completed)
        _save_trajectory_to_file(trajectory, self.model, completed)
