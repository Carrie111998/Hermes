"""Compatibility loader for the SessionDB schema mixin.

The implementation remains in one mutable module namespace so callers and
tests that patch ``hermes_state_schema`` still patch the globals used by the
schema code.  The small wrapper also owns the class-assembly seam needed by
#90173: once ``hermes_state.SessionDB`` has defined its historical
``archive_and_compact`` method, replace that one method with the bounded
staging implementation while retaining the original as a private
comparison/rollback seam.

Keeping this seam here avoids adding more code to the grandfathered
``hermes_state.py`` godfile and preserves every other SessionDB method
byte-for-byte.
"""

from __future__ import annotations

import sys
from typing import Any

from agent import hermes_state_schema_impl as _impl
from agent.state_compaction import archive_and_compact as _bounded_archive_and_compact


_OriginalSessionSchemaMixin = _impl.SessionSchemaMixin


class SessionSchemaMixin(_OriginalSessionSchemaMixin):
    """Schema mixin plus the bounded compaction method assembly hook."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        historical = cls.__dict__.get("archive_and_compact")
        if historical is None:
            return
        cls._archive_and_compact_legacy = historical
        cls.archive_and_compact = _bounded_archive_and_compact


# Preserve hermes_state_schema as the one mutable namespace.  Existing
# methods keep the implementation module's globals, and imports/monkeypatches
# through the public module name resolve to that exact object.
_impl.SessionSchemaMixin = SessionSchemaMixin
sys.modules[__name__] = _impl
