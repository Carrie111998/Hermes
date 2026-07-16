"""Lead-research provider adapters."""

from .base import CatalogProvider, Provider
from .fixture import FixtureProvider

__all__ = ["CatalogProvider", "FixtureProvider", "Provider"]
