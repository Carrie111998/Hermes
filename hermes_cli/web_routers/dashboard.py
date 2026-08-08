"""Read-only dashboard page discovery API."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from hermes_cli.dashboard_pages import list_dashboard_pages

router = APIRouter()


@router.get("/api/dashboard/pages")
async def get_dashboard_pages(query: Optional[str] = None):
    """List canonical dashboard pages safe to expose to UI and agents."""
    pages = list_dashboard_pages(query)
    return {"count": len(pages), "pages": pages}
