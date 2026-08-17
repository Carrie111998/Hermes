"""Tenant-safe product catalog import contract."""
from __future__ import annotations

from tests.server.test_api_mvp import make_client


def test_product_import_is_tenant_scoped_and_atomic():
    """A bad row rejects the whole upload without changing this tenant's catalog."""
    _, client, headers, _ = make_client()
    good = b"product_name,category,aliases\nBuilt-in oven,Ovens,oven;electric oven\n"
    response = client.post(
        "/api/v1/products/import", headers=headers,
        files={"file": ("catalog.csv", good, "text/csv")},
    )
    assert response.status_code == 201
    assert response.json()["imported"] == 1

    second = client.post("/api/v1/admin/companies", headers=headers, json={"name": "Other tenant"})
    assert second.status_code == 201
    other_headers = {**headers, "X-Company-ID": second.json()["id"]}
    other = client.post(
        "/api/v1/products/import", headers=other_headers,
        files={"file": ("catalog.csv", good, "text/csv")},
    )
    assert other.status_code == 201
    assert len(client.get("/api/v1/products", headers=other_headers).json()) == 1

    bad = b"product_name,category\nHob,Hobs\n,Hobs\n"
    response = client.post(
        "/api/v1/products/import", headers=headers,
        files={"file": ("bad.csv", bad, "text/csv")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["errors"] == [
        {"row_number": 3, "field": "product_name", "message": "Field required"},
    ]
    assert len(client.get("/api/v1/products", headers=headers).json()) == 1


def test_product_import_accepts_products_envelope_and_preserves_source_fields():
    _, client, headers, _ = make_client()
    response = client.post(
        "/api/v1/products/import", headers=headers,
        files={"file": ("catalog.json", b'''{"products": [
          {"product_name": "Induction hob", "category": "Hobs", "aliases": ["hob"],
           "source_sku": "IH-90", "voltage": "220V"}
        ]}''', "application/json")},
    )
    assert response.status_code == 201
    assert response.json()["products"][0]["source_sku"] == "IH-90"
    product = client.get("/api/v1/products", headers=headers).json()[0]
    assert product["aliases"] == ["hob"]
    assert product["voltage"] == "220V"


def test_product_import_rejects_a_bare_json_array():
    _, client, headers, _ = make_client()
    response = client.post(
        "/api/v1/products/import", headers=headers,
        files={"file": ("catalog.json", b'[{"product_name":"Oven"}]', "application/json")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["errors"] == [
        {"row_number": 0, "field": "file", "message": "JSON must be an object containing a products array"},
    ]


def test_product_import_preserves_blank_and_null_unknown_source_fields():
    _, client, headers, _ = make_client()
    response = client.post(
        "/api/v1/products/import", headers=headers,
        files={"file": ("catalog.json", b'''{"products": [
          {"product_name": "Oven", "source_sku": null, "supplier_note": ""}
        ]}''', "application/json")},
    )
    assert response.status_code == 201
    imported = response.json()["products"][0]
    assert imported["source_sku"] is None
    assert imported["supplier_note"] == ""

    csv_response = client.post(
        "/api/v1/products/import", headers=headers,
        files={"file": ("catalog.csv", b"product_name,source_sku,supplier_note\nHob,,\n", "text/csv")},
    )
    assert csv_response.status_code == 201
    csv_product = csv_response.json()["products"][0]
    assert csv_product["source_sku"] == ""
    assert csv_product["supplier_note"] == ""


def test_product_import_rejects_existing_tenant_duplicate_without_partial_rows():
    _, client, headers, _ = make_client()
    first = client.post(
        "/api/v1/products/import", headers=headers,
        files={"file": ("catalog.csv", b"product_name\nOven\n", "text/csv")},
    )
    assert first.status_code == 201
    response = client.post(
        "/api/v1/products/import", headers=headers,
        files={"file": ("catalog.csv", b"product_name\nHob\n oven \n", "text/csv")},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["errors"][0]["row_number"] == 3
    assert [product["product_name"] for product in client.get("/api/v1/products", headers=headers).json()] == ["Oven"]


def test_import_rejects_casefold_duplicate_created_through_normal_product_endpoint():
    _, client, headers, _ = make_client()
    created = client.post("/api/v1/products", headers=headers, json={"product_name": "Straße"})
    assert created.status_code == 201
    response = client.post(
        "/api/v1/products/import", headers=headers,
        files={"file": ("catalog.csv", b"product_name\nSTRASSE\n", "text/csv")},
    )
    assert response.status_code == 409
    assert len(client.get("/api/v1/products", headers=headers).json()) == 1
