"""Parsing and atomic insertion for tenant product catalog uploads."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Any

from pydantic import BaseModel, Field

from .db import json_dump, new_id, now


class ProductImportRow(BaseModel):
    row_number: int
    product_name: str = Field(min_length=1)
    category: str | None = None
    model: str | None = None
    aliases: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class ProductImportValidationError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("Product catalog contains invalid rows")
        self.errors = errors


class ProductImportConflict(ValueError):
    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("Product catalog conflicts with this tenant catalog")
        self.errors = errors


def normalized_product_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _error(row_number: int, field: str, message: str) -> dict[str, Any]:
    return {"row_number": row_number, "field": field, "message": message}


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _read_records(filename: str, content: bytes) -> list[tuple[int, dict[str, Any]]]:
    suffix = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProductImportValidationError([_error(0, "file", "File must be UTF-8 encoded")]) from exc
    if suffix == "csv":
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "product_name" not in reader.fieldnames:
            raise ProductImportValidationError([_error(1, "product_name", "CSV must include a product_name column")])
        return [(number, dict(record)) for number, record in enumerate(reader, start=2)]
    if suffix != "json":
        raise ProductImportValidationError([_error(0, "file", "Only .csv and .json product catalogs are supported")])
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProductImportValidationError([_error(0, "file", "File is not valid JSON")]) from exc
    if isinstance(payload, dict):
        payload = payload.get("products")
    if not isinstance(payload, list):
        raise ProductImportValidationError([_error(0, "file", "JSON must be an array of products")])
    errors = [_error(number, "row", "Each product must be an object")
              for number, record in enumerate(payload, start=1) if not isinstance(record, dict)]
    if errors:
        raise ProductImportValidationError(errors)
    return list(enumerate(payload, start=1))


def _parse_row(source: dict[str, Any], row_number: int) -> ProductImportRow:
    name = _clean_text(source.get("product_name"))
    if not name:
        raise ProductImportValidationError([_error(row_number, "product_name", "Field required")])
    raw_aliases = source.get("aliases")
    if raw_aliases in (None, ""):
        aliases = []
    elif isinstance(raw_aliases, str):
        aliases = [alias.strip() for alias in raw_aliases.split(";") if alias.strip()]
    elif isinstance(raw_aliases, list) and all(isinstance(alias, str) for alias in raw_aliases):
        aliases = [alias.strip() for alias in raw_aliases if alias.strip()]
    else:
        raise ProductImportValidationError([
            _error(row_number, "aliases", "Aliases must be a semicolon-separated string or a list of strings"),
        ])
    known = {"product_name", "category", "model", "aliases"}
    return ProductImportRow(
        row_number=row_number,
        product_name=name,
        category=_clean_text(source.get("category")),
        model=_clean_text(source.get("model")),
        aliases=aliases,
        extra={key: value for key, value in source.items() if key not in known and value not in (None, "")},
    )


def parse_product_catalog(filename: str, content: bytes) -> list[ProductImportRow]:
    """Validate all rows before a transaction is opened."""
    rows: list[ProductImportRow] = []
    errors: list[dict[str, Any]] = []
    for row_number, source in _read_records(filename, content):
        try:
            rows.append(_parse_row(source, row_number))
        except ProductImportValidationError as exc:
            errors.extend(exc.errors)
    if not rows and not errors:
        errors.append(_error(0, "file", "Product catalog is empty"))
    if errors:
        raise ProductImportValidationError(errors)
    return rows


def _product_data(row: ProductImportRow) -> dict[str, Any]:
    data = {**row.extra, "product_name": row.product_name}
    if row.category is not None:
        data["category"] = row.category
    if row.model is not None:
        data["model"] = row.model
    if row.aliases:
        data["aliases"] = row.aliases
    return data


def import_products(db, company_id: str, rows: list[ProductImportRow]) -> dict:
    """Insert valid rows in one company-scoped transaction."""
    normalized_rows = [(row, normalized_product_name(row.product_name)) for row in rows]
    seen: set[str] = set()
    conflicts: list[dict[str, Any]] = []
    for row, normalized in normalized_rows:
        if normalized in seen:
            conflicts.append(_error(row.row_number, "product_name", "Duplicate product name in this file"))
        seen.add(normalized)
    if seen:
        placeholders = ",".join("?" for _ in seen)
        existing = {record["normalized_name"] for record in db.all(
            f"SELECT normalized_name FROM products WHERE company_id=? AND normalized_name IN ({placeholders})",
            (company_id, *seen),
        )}
        conflicts.extend(
            _error(row.row_number, "product_name", "A product with this name already exists")
            for row, normalized in normalized_rows if normalized in existing
        )
    if conflicts:
        raise ProductImportConflict(conflicts)

    products = []
    try:
        with db.transaction() as conn:
            for row, normalized in normalized_rows:
                product_id, stamp, data = new_id("prd"), now(), _product_data(row)
                conn.execute(
                    "INSERT INTO products(id,company_id,name,normalized_name,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (product_id, company_id, row.product_name, normalized, json_dump(data), stamp, stamp),
                )
                products.append({"id": product_id, "company_id": company_id,
                                 "product_name": row.product_name, **data,
                                 "created_at": stamp, "updated_at": stamp})
    except sqlite3.IntegrityError as exc:
        raise ProductImportConflict([_error(0, "product_name", "A product with this name already exists")]) from exc
    return {"imported": len(products), "products": products}
