"""``${VAR}`` expansion for config dicts, split out of ``hermes_cli.config``.

Why its own module
------------------
The function is eight lines of ``re`` + ``os.environ`` and has no config
dependencies at all, but it lived in ``hermes_cli/config.py`` -- a 7,000-line
module that costs **126 modules** to import. ``hermes send`` needs exactly this
function (plus ``get_hermes_home``, which ``hermes_cli.config`` only re-exports
from ``hermes_constants``) to bridge ``~/.hermes/config.yaml`` into the
environment, and nothing else from that module.

``hermes_cli.config`` imports ``expand_env_vars`` back under its historical
private name, so every existing ``from hermes_cli.config import
_expand_env_vars`` -- cli.py, cron/jobs.py, cron/scheduler.py, gateway/run.py,
hermes_cli/managed_scope.py, and tests/hermes_cli/test_config_env_expansion.py
-- keeps resolving to this same function object.

Regression test: ``tests/hermes_cli/test_send_import_cost.py``.
"""

from __future__ import annotations

import os
import re

_ENV_REF = re.compile(r"\${([^}]+)}")


def expand_env_vars(obj):
    """Recursively expand ``${VAR}`` references in config values.

    Only string values are processed; dict keys, numbers, booleans, and
    None are left untouched.  Unresolved references (variable not in
    ``os.environ``) are kept verbatim so callers can detect them.
    """
    if isinstance(obj, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars(item) for item in obj]
    return obj


__all__ = ["expand_env_vars"]
