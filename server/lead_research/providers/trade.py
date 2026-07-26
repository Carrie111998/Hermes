"""Aggregate trade adapter boundary.

Live acquisition is deliberately configuration-gated. Recorded/manual rows are
normalized by :class:`CatalogProvider` and always remain market signals.
"""
from .base import CatalogProvider


class TradeProvider(CatalogProvider):
    pass
