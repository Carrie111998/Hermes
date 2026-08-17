"""Pytest config for the stress/ subdirectory.

These files are manual, __main__-executable stress entry points. Run them
directly; pytest intentionally does not collect them.
"""

collect_ignore_glob = [
    # The stress scripts have top-level code and hard-coded paths; they're
    # meant to run as `python tests/stress/<name>.py`, not as pytest modules.
    "*.py",
]
