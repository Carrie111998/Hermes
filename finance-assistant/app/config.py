from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class BankConfig:
    senders: list[str]
    subject_keywords: list[str]
    statement_keywords: list[str]
    gmail_senders: list[str]
    gmail_subject_keywords: list[str]
    gmail_attachment_extensions: list[str]


@dataclass(frozen=True, slots=True)
class AppConfig:
    banks: dict[str, BankConfig]
    categories: dict[str, list[str]]
    fee_keywords: list[str]


def _read_yaml(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or default


def load_config(config_dir: str | Path) -> AppConfig:
    directory = Path(config_dir)
    fallback = Path(__file__).resolve().parents[1] / "config"
    banks_data = _read_yaml(directory / "banks.yaml", _read_yaml(fallback / "banks.yaml", {"banks": {}})).get("banks", {})
    categories_data = _read_yaml(directory / "categories.yaml", _read_yaml(fallback / "categories.yaml", {"categories": {}})).get("categories", {})
    rules_data = _read_yaml(directory / "rules.yaml", _read_yaml(fallback / "rules.yaml", {"fees": {"keywords": []}}))
    banks = {
        name: BankConfig(
            senders=list(value.get("senders", [])),
            subject_keywords=list(value.get("subject_keywords", [])),
            statement_keywords=list(value.get("statement_keywords", [])),
            gmail_senders=list(value.get("gmail", {}).get("senders", value.get("senders", []))),
            gmail_subject_keywords=list(value.get("gmail", {}).get("subject_keywords", value.get("subject_keywords", []))),
            gmail_attachment_extensions=list(value.get("gmail", {}).get("attachment_extensions", [".pdf"])),
        )
        for name, value in banks_data.items()
    }
    return AppConfig(
        banks=banks,
        categories={name: list(values or []) for name, values in categories_data.items()},
        fee_keywords=list(rules_data.get("fees", {}).get("keywords", [])),
    )
