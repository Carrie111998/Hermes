"""Pytest config for the stress/ subdirectory.

The stress scripts are ``__main__``-executable programs, not pytest
modules: their logic lives under ``main()`` or behind a ``@scenario``
decorator, and not one of them defines a module-level ``test_*`` function
or ``Test*`` class. Collecting them directly would import eight modules,
run their top-level code, and find zero tests.

So they stay ignored here, and ``test_stress_entrypoints.py`` is the one
module pytest collects: it runs each script as a subprocess — the
documented, supported way to run them — and asserts the exit code. Those
tests carry the ``stress`` marker and are deselected by default; see
``addopts`` in pyproject.toml. Run them with ``pytest tests/stress -m
stress``.
"""
from pathlib import Path

_HERE = Path(__file__).parent
_COLLECTED = {"conftest.py", "test_stress_entrypoints.py"}

# Ignore every other module in this directory by name, including any script
# added later, so a new stress script can never silently become a
# zero-test pytest module.
collect_ignore = sorted(
    p.name for p in _HERE.glob("*.py") if p.name not in _COLLECTED
)
