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

