"""Hermes execution environment backends.

Each backend provides the same interface (BaseEnvironment ABC) for running
shell commands in a specific execution context: local, Docker, SSH,
Singularity, Modal, Daytona, or Vercel Sandbox. (Modal additionally has
direct and Nous-managed modes, selected via terminal.modal_mode.)

The terminal_tool.py factory (_create_environment) selects the backend
based on the TERMINAL_ENV configuration.
"""

from tools.environments.base import BaseEnvironment
from tools.environments.cua_fleet import CuaFleetConfig, CuaFleetDesktopProvider

__all__ = ["BaseEnvironment", "CuaFleetConfig", "CuaFleetDesktopProvider"]
