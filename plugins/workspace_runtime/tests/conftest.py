"""conftest — make the workspace_runtime plugin importable in tests.

The plugin lives under ``plugins/workspace_runtime/`` and has an
``__init__.py``, so it IS a regular Python package. We just need to make
sure the parent ``plugins/`` directory is on ``sys.path`` at test
collection time so ``from workspace_runtime.discovery import ...`` works.
"""

import sys
from pathlib import Path

# conftest.py is at plugins/workspace_runtime/tests/conftest.py.
# parent.parent -> plugins/workspace_runtime, parent.parent.parent -> plugins/
_PLUGIN_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PARENT))
