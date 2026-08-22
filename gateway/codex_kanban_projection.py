"""Compatibility facade for the modular Codex-to-Kanban projection.

The implementation lives under :mod:`gateway.codex`; this module keeps Phase
2 imports and ``python -m gateway.codex_kanban_projection`` stable.
"""

from __future__ import annotations

import logging

from gateway.codex.kanban_cli import reconciliation_main
from gateway.codex.kanban_contract import (
    PROJECTION_DEPENDENCY_CONTRACT,
    probe_projection_dependency,
)
from gateway.codex.kanban_projection import (
    _bounded,
    _bounded_document,
    _unix_time,
    CodexKanbanProjector,
)
from gateway.codex.kanban_receipts import (
    _NOTIFICATION_PHASES,
    _utc_now,
    ProjectionReceiptStore,
)
from gateway.codex.kanban_reconciliation import (
    CodexKanbanReconciler,
    read_projection_status,
)
from gateway.codex.kanban_settings import (
    KanbanProjectionSettings,
    load_kanban_projection_settings,
)


logger = logging.getLogger(__name__)


if __name__ == "__main__":
    raise SystemExit(reconciliation_main())
