"""Declarative provider catalog and tenant lifecycle state."""
from __future__ import annotations

from pathlib import Path

import yaml

from .models import DatasetDefinition
from .providers.base import CatalogProvider, Provider
from .providers.fixture import FixtureProvider
from .sectors import REFERENCE_DIR


CATALOG_PATH = REFERENCE_DIR / "provider-catalog.yaml"


class ProviderRegistry:
    def __init__(self, definitions: list[DatasetDefinition]):
        self.definitions = {item.source_id: item for item in definitions}
        if len(self.definitions) != len(definitions):
            raise ValueError("provider source ids must be unique")
        self.providers: dict[str, Provider] = {}
        for definition in definitions:
            provider = FixtureProvider(definition) if definition.adapter_mode == "fixture" else CatalogProvider(definition)
            self.providers[definition.source_id] = provider

    def get(self, source_id: str) -> Provider:
        try:
            return self.providers[source_id]
        except KeyError as exc:
            raise KeyError(f"unknown research source: {source_id}") from exc

    def list(self) -> list[DatasetDefinition]:
        return sorted(self.definitions.values(), key=lambda item: item.display_name.lower())

    def ensure_tenant(self, db, company_id: str, stamp: float) -> None:
        from ..db import json_dump
        for definition in self.list():
            exists = db.one(
                "SELECT source_id FROM dataset_definitions WHERE company_id=? AND source_id=?",
                (company_id, definition.source_id),
            )
            if exists:
                continue
            db.execute(
                "INSERT INTO dataset_definitions VALUES(?,?,?,?,?,?,?,?)",
                (company_id, definition.source_id, 1, int(definition.default_enabled),
                 json_dump(definition.model_dump(mode="json")), definition.health, None, stamp),
            )


def build_registry(path: Path = CATALOG_PATH) -> ProviderRegistry:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return ProviderRegistry([DatasetDefinition.model_validate(item) for item in raw])
