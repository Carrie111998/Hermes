"""Collection-time warm for the Feishu SDK.

Call :func:`warm_feishu_sdk` at MODULE level from any feishu test file whose
tests reach code that imports ``lark_oapi`` lazily — constructing a real
``FeishuAdapter`` (its ``__init__`` calls ``check_feishu_requirements()``), or
any handler that does a deferred ``import lark_oapi`` on first use.

Why the import has to be forced into collection
-----------------------------------------------
``lark_oapi`` is ~11,000 modules / 107 MB, and its top-level ``__init__``
eagerly pulls in every API namespace. Loading it costs tens of seconds even
with every ``.pyc`` already cached, and hundreds of seconds on a box running
the parallel gate.

Deferring that import is right for production — CLI startup must not pay it at
all — but deferral moves the cost out of module import and into whichever test
first touches the SDK. Collection is NOT covered by pytest's per-test
``--timeout``; a test body is. So under the nightly gate's ``--timeout=60`` the
first such test blew the cap, and because pytest-timeout's thread method kills
the whole process, every remaining test in the file went unreported: the file
surfaced as "no tests ran" rather than as one slow test. Six feishu files
failed that way on 2026-08-11 while ``test_feishu.py``, which already warmed
the SDK inline, passed.

Paying the identical one-time cost here, in an untimed phase, is not a new cost
— it is the same import relocated to a place where it cannot fail the file.

This is strictly best-effort and must never decide whether tests run. Files
gate their ``skipUnless`` on a separate on-disk spec probe (see
``test_feishu.py::_HAS_LARK_OAPI``, which uses ``PathFinder`` precisely so a
``sys.modules`` stub cannot flip the answer), so a failure here cannot silently
turn tests into SKIPPED.

Kept out of ``conftest.py`` on purpose: conftest is imported once per pytest
process and the per-file runner starts one process per test file, so anything
executed at conftest import would inflict the SDK load on all ~200 gateway test
files instead of the handful that need it.
"""

import sys


def warm_feishu_sdk() -> None:
    """Import ``lark_oapi`` now if it is installed; never raise."""
    if "lark_oapi" in sys.modules:
        # Already loaded, or deliberately stubbed by a test file that injects a
        # fake into sys.modules. Either way, do not touch it.
        return
    try:
        import lark_oapi  # noqa: F401
    except Exception:  # noqa: BLE001 — best-effort warm, never fatal
        pass
