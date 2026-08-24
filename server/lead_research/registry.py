"""Declarative provider catalog and tenant lifecycle state."""
from __future__ import annotations

import yaml

from ..config import Settings
from .models import DatasetDefinition, SourceCapability
from .providers.base import CatalogProvider, Provider
from .providers.base import CandidateSource, StructuredFactSource
from .providers.bright_data import BrightDataVerifier
from .providers.corpus import CorpusProvider
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

    def source_capabilities(
        self,
        source_ids: list[str] | None = None,
        provider_overrides: dict | None = None,
    ) -> list[SourceCapability]:
        selected = set(source_ids or self.definitions)
        result: list[SourceCapability] = []
        for definition in self.list():
            if definition.source_id not in selected:
                continue
            provider = (provider_overrides or {}).get(
                definition.source_id, self.get(definition.source_id),
            )
            access_class = {
                "customer_upload": "customer_upload",
                "licensed": "licensed",
                "credentialed_public": "credentialed",
            }.get(definition.access_tier, "public")
            authority = (
                "customer" if access_class == "customer_upload"
                else "licensed" if access_class == "licensed"
                else "registry" if "authoritative_registry" in definition.capabilities
                else "credible"
            )
            result.append(SourceCapability(
                source_id=definition.source_id,
                candidate_discovery=isinstance(provider, CandidateSource),
                emitted_fields=frozenset(definition.emits),
                countries=frozenset(definition.countries),
                sector_ids=frozenset(definition.sector_ids),
                access_class=access_class,
                freshness_days={field: definition.freshness_days for field in definition.emits},
                max_concurrency=definition.max_concurrency,
                authority=authority,
                redistributable=definition.access_tier in {"public", "credentialed_public"},
                executable=callable(getattr(provider, "research_fields", None)),
            ))
        return result

    def customer_catalog(self) -> list[dict]:
        result = []
        for definition in self.list():
            provider = self.get(definition.source_id)
            executable = isinstance(provider, (CandidateSource, StructuredFactSource)) or callable(
                getattr(provider, "verify", None)
            )
            health = provider.health()
            if not executable or health.status not in {"active", "degraded"}:
                continue
            result.append({
                "id": definition.source_id,
                "display_name": definition.display_name,
                "runnable": True,
                "capabilities": list(definition.capabilities),
            })
        return result

    def admin_setup_catalog(self) -> list[dict]:
        runnable = {item["id"] for item in self.customer_catalog()}
        return [{
            "id": definition.source_id,
            "display_name": definition.display_name,
            "runnable": definition.source_id in runnable,
            "adapter_mode": definition.adapter_mode,
        } for definition in self.list()]

    def ensure_tenant(self, db, company_id: str, stamp: float) -> None:
        from ..db import json_dump
        # A source dropped from the catalog leaves a tenant row behind, and
        # catalog() resolves every row through the registry — so without this
        # the whole Data Sources page raises KeyError on the stale one. Evidence
        # and partitions keep their source_id; nothing here touches history.
        known = set(self.definitions)
        stale = [
            row["source_id"] for row in db.all(
                "SELECT source_id FROM dataset_definitions WHERE company_id=?", (company_id,)
            ) if row["source_id"] not in known
        ]
        for source_id in stale:
            db.execute(
                "DELETE FROM dataset_definitions WHERE company_id=? AND source_id=?",
                (company_id, source_id),
            )
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
    for source_id, build in (("ted", TedVerifier), ("customer-list-corpus", CorpusProvider)):
        definition = next((item for item in definitions if item.source_id == source_id), None)
        if definition is None or source_id in supplied:
            continue
        verifier = build()
        verifier.definition = definition
        supplied[source_id] = verifier
    return ProviderRegistry(definitions, supplied)
