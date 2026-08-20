"""Declarative provider catalog and tenant lifecycle state."""
from __future__ import annotations

import yaml

from ..config import Settings
from .models import DatasetDefinition
from .providers.base import CatalogProvider, Provider
from .providers.bright_data import BrightDataVerifier
from .providers.ted import TedVerifier
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
            payload = json_dump(definition.model_dump(mode="json"))
            exists = db.one(
                "SELECT source_id FROM dataset_definitions WHERE company_id=? AND source_id=?",
                (company_id, definition.source_id),
            )
            if exists:
                # The definition is catalog-owned and the row caches a copy, so
                # a catalog edit — a new adapter_mode, a retirement — would
                # otherwise never reach a tenant that had already been seeded.
                # installed/enabled are the tenant's and are left alone.
                db.execute(
                    "UPDATE dataset_definitions SET definition=?,health=? "
                    "WHERE company_id=? AND source_id=?",
                    (payload, definition.health, company_id, definition.source_id),
                )
                continue
            db.execute(
                "INSERT INTO dataset_definitions VALUES(?,?,?,?,?,?,?,?)",
                (company_id, definition.source_id, 1, int(definition.default_enabled),
                 payload, definition.health, None, stamp),
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
        api_key = settings.brightdata_api_key.strip()
        if not settings.brightdata_enabled:
            supplied[bright_data.source_id] = UnavailableCandidateVerifier(
                bright_data,
                "disabled",
            )
        elif not api_key:
            supplied[bright_data.source_id] = UnavailableCandidateVerifier(
                bright_data,
                "credential_required",
            )
        else:
            verifier = BrightDataVerifier(
                api_key,
                settings.brightdata_unlocker_zone,
            )
            verifier.definition = bright_data
            supplied[bright_data.source_id] = verifier
    # TED needs no credential, so there is nothing to gate on: it is either in
    # the catalog or it is not. Tenants still opt in per source.
    ted = next((item for item in definitions if item.source_id == "ted"), None)
    if ted is not None and ted.source_id not in supplied:
        verifier = TedVerifier()
        verifier.definition = ted
        supplied[ted.source_id] = verifier
    return ProviderRegistry(definitions, supplied)
