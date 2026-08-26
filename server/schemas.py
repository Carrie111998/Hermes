"""Pydantic request models shared by the product API routes."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(ApiModel):
    email: str
    password: str


class RefreshRequest(ApiModel):
    refresh_token: str


class PasswordResetRequest(ApiModel):
    email: str


class PasswordResetConfirm(ApiModel):
    token: str
    password: str = Field(min_length=10)


class CompanyCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = None
    status: Literal["active", "disabled", "suspended"] = "active"
    data: dict[str, Any] = Field(default_factory=dict)


class CompanyPatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    legal_name: str | None = None
    data: dict[str, Any] | None = None


class UserCreate(ApiModel):
    email: str
    password: str | None = Field(default=None, min_length=10)
    role: Literal["admin", "customer"] = "customer"
    company_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class UserPatch(ApiModel):
    email: str | None = None
    role: Literal["admin", "customer"] | None = None
    company_id: str | None = None
    status: Literal["active", "disabled"] | None = None
    data: dict[str, Any] | None = None


class AssignCompany(ApiModel):
    company_id: str


class ResetPassword(ApiModel):
    password: str = Field(min_length=10)


class DataPatch(ApiModel):
    data: dict[str, Any] = Field(default_factory=dict)


class CountriesSelection(ApiModel):
    countries: list[str] = Field(max_length=5)



# --------------------------------------------------------------------------
# The onboarding contract (PRODUCT.md §6).
#
# `DataPatch` deliberately carries an open dict, because each company section
# holds a different shape. That openness used to reach storage: `_put_section`
# merged whatever arrived, so `founded_yr` persisted silently, `founded_year`
# stayed empty, and nothing failed. It is worse than a dropped field —
# `AgentRunService.company_context` hands every section to the model wholesale,
# so a typo does not vanish, it becomes context.
#
# This is the only place that decides whether a section key is real. §6.1 and
# §6.2 are reproduced field for field and locked to the document by
# tests/server/test_onboarding_contract.py; the remaining sections list the
# keys their readers actually use.
#
# Not covered here, and deliberately so:
#   * §6.3 product data lives in `products.data`, which also carries unmapped
#     catalog columns by design (`product_import._parse_row` keeps `extra`),
#     so an allowlist there would reject valid imports.
#   * §6.4 and §6.5 are upload categories, not section keys. They are
#     validated by `DOCUMENT_TYPES` in server/routes/knowledge.py.
SECTION_FIELDS: dict[str, frozenset[str]] = {
    # §6.1 Company identity, plus four keys the shipped code depends on:
    # `name` is what the Setup editor sends for `company_name` and what
    # `lead_research.backfill` reads, so both spellings are accepted rather
    # than one of them being silently dropped; `country`/`seller_countries`
    # feed the research profile; `public_sources` is written by provisioning.
    "profile": frozenset({
        "company_name", "legal_name", "website", "headquarters_country", "city",
        "founded_year", "industry", "sub_industry", "employee_count",
        "business_model", "main_language", "sales_regions_current",
        "sales_regions_target",
        "name", "country", "seller_countries", "public_sources",
    }),
    # §6.2 Company positioning, matched key for key by the Setup editor.
    "positioning": frozenset({
        "what_company_sells", "main_value_proposition", "quality_position",
        "price_position", "premium_or_mass_market", "main_differentiators",
        "certifications", "manufacturing_capacity", "export_capacity",
        "delivery_capabilities", "after_sales_support",
    }),
    # The catalog step confirms the product rows; the products themselves are
    # their own table (§6.3).
    "products": frozenset({"product_ids", "catalog_confirmed"}),
    # §6.4 arrives as uploads, so the step records which documents were
    # reviewed rather than the nineteen data categories themselves.
    "internal_sales_data": frozenset({
        "sources_reviewed", "document_ids", "public_sources",
    }),
    # §6.5, same shape: the contact lists are uploaded documents.
    "current_contacts": frozenset({"document_id", "contact_list_added"}),
    "market_preferences": frozenset({
        "target_markets", "no_research_markets", "no_outreach_markets",
        "target_countries", "languages", "product_term_translations",
    }),
    "integrations": frozenset({"email_provider", "connected"}),
    "brain_review": frozenset({"snapshot_id", "reviewed"}),
    "sales_preferences": frozenset({
        "connected_mailbox", "default_send_mode", "default_language",
        "languages", "default_cc_rule_id", "daily_email_limit",
        "daily_whatsapp_limit", "send_windows", "excluded_industries",
        "fixed_subject_line",
    }),
    "email_templates": frozenset({"templates"}),
}


def unknown_section_fields(section: str, patch: dict) -> list[str]:
    """Keys the section does not define, sorted.

    An unknown *section* raises KeyError on purpose. Section names are literals
    at their call sites, so a missing allowlist is a coding error that should
    fail on the first request in development, not a request error that lets an
    unvalidated section reach storage in production.
    """
    return sorted(key for key in patch if key not in SECTION_FIELDS[section])
