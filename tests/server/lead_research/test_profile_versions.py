from __future__ import annotations

from server.db import Database, now


def _seed_tenants(db: Database) -> None:
    stamp = now()
    for company_id, user_id in (("cmp_a", "usr_a"), ("cmp_b", "usr_b")):
        db.execute(
            "INSERT INTO companies(id,name,status,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (company_id, company_id, "active", "{}", stamp, stamp),
        )
        db.execute(
            "INSERT INTO users(id,email,role,company_id,status,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                user_id,
                f"{user_id}@example.test",
                "customer",
                company_id,
                "active",
                "{}",
                stamp,
                stamp,
            ),
        )


def test_profile_versions_are_immutable_and_tenant_scoped(tmp_path):
    from server.lead_research.models import CompanyResearchProfile
    from server.lead_research.profiles import ProfileRepository

    db = Database(tmp_path / "profiles.db")
    _seed_tenants(db)
    repo = ProfileRepository(db)
    first = repo.create_version(
        "cmp_a",
        "usr_a",
        CompanyResearchProfile(
            identity={"name": "Acme", "website": "https://acme.test"},
            seller_countries=["TR"],
            products=[
                {
                    "id": "prd_valve",
                    "name": "Vana",
                    "english_name": "Valve",
                    "hs_codes": ["8481"],
                    "sector_ids": ["industrial-machinery"],
                    "emphasis": 1.0,
                }
            ],
            market_preferences={"target_countries": ["DE"], "languages": ["de", "en"]},
            hidden_label_ids=["lbl_export_ready"],
            playbook_versions={"industrial-machinery": "1"},
        ),
    )

    second = repo.create_version(
        "cmp_a",
        "usr_a",
        first.profile.model_copy(update={"seller_countries": ["TR", "DE"]}),
    )

    assert first.id != second.id
    assert repo.get("cmp_a", first.id).profile.seller_countries == ["TR"]
    assert repo.current("cmp_a").id == second.id
    assert repo.get("cmp_b", first.id) is None
    assert repo.get("cmp_a", first.id).status == "superseded"
    assert repo.get("cmp_a", first.id).superseded_at is not None
