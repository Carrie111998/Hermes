from __future__ import annotations

import sqlite3

from server.db import Database, now
from server.lead_research.candidates import CandidateRepository
from tests.server.lead_research.test_vertical_slice import make_research_client


def _csv(name: str) -> bytes:
    slug = name.lower().replace(" ", "-")
    return (
        "source_record_id,company_name,country,categories\n"
        f"{slug},{name},DE,industrial valve\n"
    ).encode()


def _seed_companies(db: Database) -> None:
    stamp = now()
    for company_id in ("cmp_a", "cmp_b"):
        db.execute(
            "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (company_id, company_id, "active", "{}", stamp, stamp),
        )


def test_candidate_search_unions_public_and_owner_private_only(tmp_path):
    db = Database(tmp_path / "visible.db")
    _seed_companies(db)
    repo = CandidateRepository(db)
    repo.import_file(
        "pub", "1", "public.csv", _csv("Public Valve GmbH"),
        owner_company_id=None, visibility="service_public",
    )
    repo.import_file(
        "a", "1", "a.csv", _csv("A Private Valve AS"),
        owner_company_id="cmp_a", visibility="tenant_private",
    )
    repo.import_file(
        "b", "1", "b.csv", _csv("B Private Valve BV"),
        owner_company_id="cmp_b", visibility="tenant_private",
    )

    names = {
        row.normalized_name
        for row in repo.select(
            company_id="cmp_a", countries=[], product_terms=["valve"], limit=20,
        )
    }

    assert names == {"public valve gmbh", "a private valve as"}


def test_existing_unowned_datasets_backfill_as_service_public(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE candidate_datasets (
            dataset_id TEXT NOT NULL, version TEXT NOT NULL, source_filename TEXT NOT NULL,
            raw_hash TEXT NOT NULL, imported_at REAL NOT NULL, record_count INTEGER NOT NULL,
            PRIMARY KEY(dataset_id, version)
        );
        INSERT INTO candidate_datasets VALUES('legacy','1','legacy.csv','abc',0,0);
        """
    )
    connection.close()

    migrated = Database(path)
    row = migrated.one(
        "SELECT visibility,owner_company_id FROM candidate_datasets WHERE dataset_id='legacy'",
    )

    assert row["visibility"] == "service_public"
    assert row["owner_company_id"] is None


def test_customer_upload_route_always_creates_tenant_private_supply():
    app, client, headers, company_id = make_research_client()

    uploaded = client.post(
        "/api/v1/candidate-datasets",
        headers=headers,
        files={"file": ("private.csv", _csv("Private Valve AG"), "text/csv")},
    )

    assert uploaded.status_code == 201, uploaded.text
    row = app.state.db.one(
        "SELECT owner_company_id,visibility FROM candidate_datasets WHERE dataset_id=?",
        (uploaded.json()["dataset_id"],),
    )
    assert dict(row) == {"owner_company_id": company_id, "visibility": "tenant_private"}
    other = client.post(
        "/api/v1/admin/companies", headers=headers, json={"name": "Other tenant"},
    ).json()
    assert CandidateRepository(app.state.db).select(
        company_id=other["id"], countries=["DE"], product_terms=["private valve"], limit=10,
    ) == []
