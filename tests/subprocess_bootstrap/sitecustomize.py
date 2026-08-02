"""Propagate managed-scope isolation into Python subprocesses spawned by tests.

This module is loaded only because the pytest harness prepends this directory to
``PYTHONPATH``. Production Hermes code contains no environment-variable bypass.
"""

import os
from importlib.util import find_spec
from pathlib import Path

_TEST_DEFAULT = os.environ.get("_HERMES_TEST_MANAGED_DEFAULT")
if _TEST_DEFAULT and find_spec("hermes_cli") is not None:
    from hermes_cli import managed_scope as _managed_scope

    _managed_scope._DEFAULT_MANAGED_DIR = Path(_TEST_DEFAULT)
    _managed_scope._managed_dir_is_trusted = (
        lambda path: Path(path).resolve() if Path(path).is_dir() else None
    )
    _managed_scope._managed_stat_is_trusted = lambda _file_stat: True
    _managed_scope._managed_ancestor_stat_is_trusted = lambda _directory_stat: True
