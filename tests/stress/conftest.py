"""Keep standalone stress scripts out of normal pytest collection.

The files in this directory are ``__main__``-executable programs, not pytest
modules. The dedicated stress workflow invokes them directly; running pytest
on this directory intentionally collects no tests.
"""


collect_ignore_glob = [
    # These scripts have top-level entry points and are run as
    # `python tests/stress/<name>.py`, not as pytest modules.
    "*.py",
]
