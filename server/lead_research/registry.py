"""Declarative provider catalog and tenant lifecycle state."""
from __future__ import annotations

import yaml

from ..config import Settings
from .models import DatasetDefinition
from .providers.base import CatalogProvider, Provider
from .providers.bright_data import BrightDataVerifier
from .sectors import REFERENCE_DIR
from .verification import UnavailableCandidateVerifier


CATALOG_PATH = REFERENCE_DIR / "provider-catalog.yaml"


class ProviderRegistry:
    def __init__(self, definitions: list[DatasetDefinition], providers: dict[str, Provider] | None = None):
        self.definitions = {item.source_id: item for item in definitions}
        if len(self.definitions) != len(definitions):
            raise ValueError("provider source ids must be unique")
        supplied = providers or {}
        self.providers = {
            source_id: supplied.get(source_id, CatalogProvider(definition))
            for source_id, definition in self.definitions.items()
        }

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


def build_registry(
    settings: Settings | None = None,
    providers: dict[str, Provider] | None = None,
) -> ProviderRegistry:
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or []
    definitions = [DatasetDefinition.model_validate(item) for item in raw]
    supplied = dict(providers or {})
    bright_data = next(
        (item for item in definitions if item.source_id == "brightdata-web"),
        None,
    )
    if bright_data is not None and bright_data.source_id not in supplied:
        settings = settings or Settings.load()
        if not settings.brightdata_enabled:
            supplied[bright_data.source_id] = UnavailableCandidateVerifier(
                bright_data,
                "disabled",
            )
        elif not settings.brightdata_api_key:
            supplied[bright_data.source_id] = UnavailableCandidateVerifier(
                bright_data,
                "credential_required",
            )
        else:
            verifier = BrightDataVerifier(
                settings.brightdata_api_key,
                settings.brightdata_unlocker_zone,
            )
            verifier.definition = bright_data
            supplied[bright_data.source_id] = verifier
    return ProviderRegistry(definitions, supplied)
