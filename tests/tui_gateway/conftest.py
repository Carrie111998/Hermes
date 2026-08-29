"""Shared isolation for the focused TUI-gateway test modules.

``tui_gateway.session_ownership`` keeps a process-global, bounded LRU of
retired runtime ids.  ``write_json()`` reads it to drop late events addressed
to a runtime that no longer exists instead of letting them fall back to stdio.
In production that tombstone is cleared by every real registration path
(``_init_session`` / ``_claim_or_reuse_live`` call ``forget_retired_session_id``),
so an id can never stay poisoned across a legitimate reuse.

Tests do not go through those paths: many modules here share generic ids like
``"s1"``, and several assign straight into ``server._sessions`` and then pop
the record. The tombstone left behind then silences ``_emit`` for that id in
every LATER module in the same process, which made the suite order-dependent
(``test_protocol.py`` failing only when it ran after such a module).

Clear the registry per test so module order cannot change outcomes.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_retired_session_ids():
    from tui_gateway import session_ownership

    with session_ownership._retired_session_ids_lock:
        session_ownership._retired_session_ids.clear()
    yield
    with session_ownership._retired_session_ids_lock:
        session_ownership._retired_session_ids.clear()
