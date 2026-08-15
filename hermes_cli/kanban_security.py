"""Composition root for the zero-authority Kanban path.

This file owns wiring only.  It deliberately contains no SQL, process launch,
tool dispatch, approval logic, publication adapter behavior, or reclaim policy.
"""

from __future__ import annotations

from pathlib import Path

from .kanban_store.database import connect
from .kanban_store.schema import migrate
from .kanban_surfaces.service import KanbanSecurityService


def open_security_store(board_database: str | Path):
    conn = connect(board_database)
    migrate(conn)
    return conn


def create_security_service(board_database: str | Path) -> KanbanSecurityService:
    return KanbanSecurityService(conn=open_security_store(board_database))
