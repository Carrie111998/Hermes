"""Private candidate corpus contracts.

Candidate data is a service-only input to future campaign evaluation.  It is
not tenant data and importing it must therefore never create a lead, research
row, organization, or evidence record.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from server.db import Database
from server.config import Settings
from server.lead_research.candidates import (
    CandidateImportConflict,
    CandidateImportValidationError,
    CandidateRepository,
)


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "candidates.db")


@pytest.fixture()
def candidate_csv():
    return (
        b"source_record_id,company_name,country,website,aliases,categories,buyer_types,provenance_url\n"
        b"atlas-1,Atlas Kitchens GmbH,de,HTTPS://WWW.Atlas.Example/path,Atlas;Atlas Kitchen,"
        b"Kitchen appliances;Built-in ovens,importer;distributor,https://registry.example/atlas-1\n"
        b"north-2,Northstar Retail,FR,https://northstar.example,Northstar,"
        b"Refrigerators;Kitchen appliances,retailer,https://registry.example/north-2\n"
    )


def test_candidate_import_never_creates_tenant_leads(db, candidate_csv):
    """Removing the service-only boundary would create tenant-visible rows."""
    repo = CandidateRepository(db)

    report = repo.import_file("kitchen-appliances", "2026-08", "candidates.csv", candidate_csv)

    assert report.imported == 2
    assert db.one("SELECT COUNT(*) AS n FROM leads")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM research")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM organizations")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM evidence_records")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM candidate_records")["n"] == 2


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("country", "XX", "ISO alpha-2"),
        ("website", "ftp://atlas.example", "http or https URL"),
        ("provenance_url", "not a url", "http or https URL"),
    ],
)
def test_invalid_candidate_values_reject_the_entire_import(db, candidate_csv, field, value, message):
    """Accepting a malformed candidate would poison later campaign selection."""
    header, first, second, _ = candidate_csv.decode().split("\n")
    columns = header.split(",")
    values = first.split(",")
    values[columns.index(field)] = value
    invalid = (header + "\n" + ",".join(values) + "\n" + second + "\n").encode()

    with pytest.raises(CandidateImportValidationError, match=message):
        CandidateRepository(db).import_file("kitchen-appliances", "2026-08", "candidates.csv", invalid)

    assert db.one("SELECT COUNT(*) AS n FROM candidate_datasets")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM candidate_records")["n"] == 0


def test_duplicate_source_record_id_rejects_entire_import_atomically(db, candidate_csv):
    """Dropping duplicate detection would make candidate identity ambiguous."""
    duplicate = candidate_csv + (
        b"atlas-1,Other Atlas,DE,https://other.example,Other,Kitchen appliances,"
        b"importer,https://registry.example/other\n"
    )

    with pytest.raises(CandidateImportConflict, match="Duplicate source_record_id"):
        CandidateRepository(db).import_file("kitchen-appliances", "2026-08", "candidates.csv", duplicate)

    assert db.one("SELECT COUNT(*) AS n FROM candidate_datasets")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM candidate_records")["n"] == 0


def test_dataset_version_is_immutable_and_duplicate_rejected(db, candidate_csv):
    """Overwriting an imported version would make its recorded hash untrustworthy."""
    repo = CandidateRepository(db)
    report = repo.import_file("kitchen-appliances", "2026-08", "candidates.csv", candidate_csv)

    with pytest.raises(CandidateImportConflict, match="immutable"):
        repo.import_file(
            "kitchen-appliances", "2026-08", "replacement.csv",
            candidate_csv.replace(b"Atlas", b"Changed"),
        )

    dataset = db.one("SELECT source_filename,raw_hash,record_count FROM candidate_datasets")
    assert dict(dataset) == {
        "source_filename": "candidates.csv",
        "raw_hash": hashlib.sha256(candidate_csv).hexdigest(),
        "record_count": 2,
    }
    assert report.raw_hash == dataset["raw_hash"]
    assert db.one("SELECT COUNT(*) AS n FROM candidate_records")["n"] == 2


def test_candidate_selection_filters_countries_and_product_terms(db, candidate_csv):
    """Ignoring either selector would show unrelated corpus rows to a campaign."""
    repo = CandidateRepository(db)
    repo.import_file("kitchen-appliances", "2026-08", "candidates.csv", candidate_csv)

    records = repo.select(countries=["DE"], product_terms=["oven"], limit=10)

    assert [(record.source_record_id, record.country, record.domain) for record in records] == [
        ("atlas-1", "DE", "atlas.example"),
    ]
    assert records[0].normalized_name == "atlas kitchens gmbh"
    assert records[0].data == {
        "aliases": ["Atlas", "Atlas Kitchen"],
        "buyer_types": ["importer", "distributor"],
        "categories": ["Kitchen appliances", "Built-in ovens"],
        "provenance_url": "https://registry.example/atlas-1",
    }


def test_candidate_import_cli_emits_only_corpus_identity(monkeypatch, tmp_path, candidate_csv, capsys):
    """Echoing candidate rows from the CLI would disclose a private corpus."""
    from server import __main__ as command

    source = tmp_path / "candidates.csv"
    source.write_bytes(candidate_csv)
    monkeypatch.setattr(command.Settings, "load", lambda: Settings(database_path=tmp_path / "cli.db"))

    command.main([
        "import-candidates", "--dataset-id", "kitchen-appliances", "--version", "2026-08", "--file", str(source),
    ])

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "dataset_id": "kitchen-appliances",
        "version": "2026-08",
        "count": 2,
        "raw_hash": hashlib.sha256(candidate_csv).hexdigest(),
    }
