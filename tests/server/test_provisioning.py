"""Safe demo-account provisioning contract."""
from __future__ import annotations

import json
import sqlite3

import pytest

from server.auth import verify_password
from server.__main__ import _read_password_file
from server.db import Database, json_load, now
from server.provisioning import OPERATIONAL_TABLES, assert_clean_tenant, provision_demo_account


TEST_PASSWORD = "test-only-password"
REQUIRED_STEPS = {
    "company-identity",
    "positioning",
    "products",
    "internal-sales-data",
    "target-markets",
}


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "provisioning.db")


def provision(db, *, profile=None, sources=None):
    return provision_demo_account(
        db,
        email="efe@anexa-arelvia.com",
        password=TEST_PASSWORD,
        company_profile=profile or {"name": "Silverline", "website": "https://example.invalid"},
        onboarding_sources=sources or [{"url": "https://example.invalid", "retrieved_at": 1.0}],
    )


def test_provisioned_silverline_is_onboarded_and_operationally_empty(db):
    result = provision(db)

    assert result["onboarding_status"] == "completed"
    company_id = result["company_id"]
    onboarding = db.one("SELECT completed_steps FROM onboarding WHERE company_id=?", (company_id,))
    assert set(json_load(onboarding["completed_steps"])) == REQUIRED_STEPS
    assert db.one("SELECT COUNT(*) AS n FROM products WHERE company_id=?", (company_id,))["n"] == 0
    assert json_load(db.one(
        "SELECT data FROM company_sections WHERE company_id=? AND section='products'", (company_id,),
    )["data"]) == {}
    assert json_load(db.one(
        "SELECT data FROM company_sections WHERE company_id=? AND section='market_preferences'", (company_id,),
    )["data"]) == {}
    for table in OPERATIONAL_TABLES:
        assert db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE company_id=?", (company_id,))["n"] == 0


def test_provisioning_converges_on_one_updated_account(db):
    first = provision(db)
    second = provision(
        db,
        profile={"name": "Silverline Updated", "website": "https://updated.example.invalid"},
        sources=[{"url": "https://updated.example.invalid", "retrieved_at": 2.0}],
    )

    assert second["company_id"] == first["company_id"]
    assert second["user_id"] == first["user_id"]
    assert db.one("SELECT COUNT(*) AS n FROM companies")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM users")["n"] == 1
    profile = json_load(db.one(
        "SELECT data FROM company_sections WHERE company_id=? AND section='profile'", (first["company_id"],),
    )["data"])
    assert profile["name"] == "Silverline Updated"
    assert profile["public_sources"] == [{"url": "https://updated.example.invalid", "retrieved_at": 2.0}]


@pytest.mark.parametrize("table", OPERATIONAL_TABLES)
def test_clean_tenant_rejects_a_row_in_each_operational_table(table):
    conn = sqlite3.connect(":memory:")
    for name in OPERATIONAL_TABLES:
        conn.execute(f"CREATE TABLE {name} (company_id TEXT)")
    conn.execute(f"INSERT INTO {table}(company_id) VALUES(?)", ("company_demo",))

    with pytest.raises(RuntimeError, match=table):
        assert_clean_tenant(conn, "company_demo")


def test_provisioning_refuses_to_replace_an_operational_tenant(db):
    result = provision(db)
    db.execute(
        "INSERT INTO selected_countries(company_id,country_code,created_at) VALUES(?,?,?)",
        (result["company_id"], "DE", now()),
    )

    with pytest.raises(RuntimeError, match="selected_countries"):
        provision(db)


def test_provisioning_stores_a_verifiable_password_without_returning_it(db):
    result = provision(db)
    stored = db.one("SELECT password_hash FROM users WHERE id=?", (result["user_id"],))["password_hash"]

    assert verify_password(TEST_PASSWORD, stored)
    assert not verify_password(TEST_PASSWORD + "-incorrect", stored)
    assert TEST_PASSWORD not in json.dumps(result)


def test_password_file_must_be_owner_restricted(tmp_path):
    password_file = tmp_path / "demo-password.txt"
    password_file.write_text(TEST_PASSWORD + "\n", encoding="utf-8")
    password_file.chmod(0o600)

    assert _read_password_file(password_file) == TEST_PASSWORD

    password_file.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        _read_password_file(password_file)
