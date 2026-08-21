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
        b"source_record_id,company_name,country,domain,aliases,categories,buyer_types,provenance_url\n"
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
        ("domain", "ftp://atlas.example", "http or https URL"),
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


def test_jsonl_candidate_corpus_imports_each_utf8_json_line(db):
    """Treating JSONL as one JSON object would discard valid source rows."""
    payload = b"\n".join([
        b'{"source_record_id":"atlas-1","company_name":"Atlas Kitchens GmbH","country":"DE",'
        b'"domain":"https://atlas.example","categories":["Built-in ovens"]}',
        b'{"source_record_id":"north-2","company_name":"Northstar Retail","country":"FR",'
        b'"domain":"https://northstar.example","categories":["Refrigerators"]}',
    ]) + b"\n"

    report = CandidateRepository(db).import_file("kitchen-appliances", "2026-09", "candidates.jsonl", payload)

    assert report.imported == 2
    assert [record.source_record_id for record in CandidateRepository(db).select(
        countries=[], product_terms=[], limit=10,
    )] == ["atlas-1", "north-2"]


def test_json_candidate_payload_is_rejected_without_writes(db):
    """Accepting a JSON envelope would violate the JSON Lines import contract."""
    payload = b'{"candidates":[{"source_record_id":"atlas-1","company_name":"Atlas","country":"DE"}]}'

    with pytest.raises(CandidateImportValidationError, match="JSON Lines"):
        CandidateRepository(db).import_file("kitchen-appliances", "2026-09", "candidates.json", payload)

    assert db.one("SELECT COUNT(*) AS n FROM candidate_datasets")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM candidate_records")["n"] == 0


@pytest.mark.parametrize(
    "field",
    ["source_record_id", "company_name"],
)
def test_import_requires_canonical_identity_fields(db, field):
    """Alias keys would make source identity depend on undocumented input shape."""
    row = {"source_record_id": "atlas-1", "company_name": "Atlas", "country": "DE"}
    value = row.pop(field)
    row[{"source_record_id": "id", "company_name": "name"}[field]] = value

    with pytest.raises(CandidateImportValidationError, match=field):
        CandidateRepository(db).import_file(
            "kitchen-appliances", "2026-09", "candidates.jsonl", json.dumps(row).encode() + b"\n",
        )

    assert db.one("SELECT COUNT(*) AS n FROM candidate_datasets")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM candidate_records")["n"] == 0


@pytest.mark.parametrize(
    "second,reason",
    [
        (
            {"source_record_id": "atlas-2", "company_name": "Other Atlas", "country": "FR",
             "domain": "https://ATLAS.example/elsewhere"},
            "Duplicate normalized domain",
        ),
        (
            {"source_record_id": "atlas-2", "company_name": " atlas kitchens ", "country": "DE",
             "domain": "https://other.example"},
            "Duplicate normalized company_name and country",
        ),
    ],
)
def test_duplicate_normalized_candidate_identity_rejects_entire_file(db, second, reason):
    """Identity collisions must not leave a partially imported shared corpus."""
    first = {
        "source_record_id": "atlas-1", "company_name": "Atlas Kitchens", "country": "DE",
        "domain": "https://www.atlas.example",
    }
    payload = (json.dumps(first) + "\n" + json.dumps(second) + "\n").encode()

    with pytest.raises(CandidateImportConflict, match=reason):
        CandidateRepository(db).import_file("kitchen-appliances", "2026-09", "candidates.jsonl", payload)

    assert db.one("SELECT COUNT(*) AS n FROM candidate_datasets")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM candidate_records")["n"] == 0


def test_bare_domain_is_rejected_without_writes(db):
    """A bare hostname cannot establish validated HTTP(S) provenance."""
    payload = b'{"source_record_id":"atlas-1","company_name":"Atlas","country":"DE","domain":"atlas.example"}\n'

    with pytest.raises(CandidateImportValidationError, match="http or https URL"):
        CandidateRepository(db).import_file("kitchen-appliances", "2026-09", "candidates.jsonl", payload)

    assert db.one("SELECT COUNT(*) AS n FROM candidate_datasets")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM candidate_records")["n"] == 0


def test_selection_skips_settled_identities_and_still_fills_the_limit(db, candidate_csv):
    """A rerun must spend its batch on unsettled companies, not on skips.

    Excluding after the limit would return a page mostly consumed by already
    validated companies, which is the re-verification cost this exists to stop.
    """
    repo = CandidateRepository(db)
    repo.import_file("kitchen-appliances", "2026-08", "candidates.csv", candidate_csv)

    both = repo.select(countries=["DE", "FR"], product_terms=[], limit=2)
    assert {record.country for record in both} == {"DE", "FR"}

    remaining = repo.select(
        countries=["DE", "FR"],
        product_terms=[],
        limit=1,
        exclude={("atlas kitchens gmbh", "DE")},
    )
    assert [record.country for record in remaining] == ["FR"]


def test_selection_without_exclusions_is_unchanged(db, candidate_csv):
    repo = CandidateRepository(db)
    repo.import_file("kitchen-appliances", "2026-08", "candidates.csv", candidate_csv)

    assert len(repo.select(countries=[], product_terms=[], limit=10, exclude=set())) == 2
    assert len(repo.select(countries=[], product_terms=[], limit=10)) == 2


def test_only_the_newest_version_of_a_corpus_is_selected(db, candidate_csv):
    """A correction ships as a new version beside the old one, not over it.

    Both versions live in the same table, so without a version filter every
    company in a corrected corpus is verified twice at full request cost, and
    the superseded row's stale facts compete with the fix. This is how the
    kitchen-appliance corpus kept selecting its pre-correction category.
    """
    repo = CandidateRepository(db)
    repo.import_file("kitchen-appliances", "1", "candidates.csv", candidate_csv)
    corrected = candidate_csv.replace(b"Kitchen appliances", b"Household appliances")
    repo.import_file("kitchen-appliances", "2", "candidates.csv", corrected)

    assert len(repo.select(countries=[], product_terms=[], limit=10)) == 2
    assert len(repo.select(countries=[], product_terms=["household appliances"], limit=10)) == 2
    assert repo.select(countries=[], product_terms=["kitchen appliances"], limit=10) == []
    assert {r.version for r in repo.select(countries=[], product_terms=[], limit=10)} == {"2"}


def test_a_double_digit_version_supersedes_a_single_digit_one(db, candidate_csv):
    """Versions are free text; "10" must not sort below "9"."""
    repo = CandidateRepository(db)
    repo.import_file("kitchen-appliances", "9", "candidates.csv", candidate_csv)
    repo.import_file("kitchen-appliances", "10", "candidates.csv", candidate_csv)

    assert {r.version for r in repo.select(countries=[], product_terms=[], limit=10)} == {"10"}


def test_each_dataset_keeps_its_own_newest_version(db, candidate_csv):
    """One corpus getting a v2 must not hide another still on v1."""
    repo = CandidateRepository(db)
    repo.import_file("kitchen-appliances", "1", "candidates.csv", candidate_csv)
    repo.import_file("kitchen-appliances", "2", "candidates.csv", candidate_csv)
    other = candidate_csv.replace(b"atlas-1", b"ted-1").replace(b"north-2", b"ted-2")
    repo.import_file("ted-appliances", "1", "candidates.csv", other)

    selected = repo.select(countries=[], product_terms=[], limit=10)
    assert {(r.dataset_id, r.version) for r in selected} == {
        ("kitchen-appliances", "2"), ("ted-appliances", "1"),
    }
