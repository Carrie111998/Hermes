"""Canonical sector taxonomy and deterministic Markdown/CSV generation."""
from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


ROOT = Path(__file__).resolve().parents[2]
REFERENCE_DIR = ROOT / "skills" / "sales" / "lead-research" / "references"
SECTOR_YAML = REFERENCE_DIR / "sectors.yaml"
SECTOR_MD = REFERENCE_DIR / "sectors.md"
SECTOR_CSV = REFERENCE_DIR / "sectors.csv"


class Sector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sector_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    aliases: list[str] = Field(default_factory=list)
    hs_2022: list[str] = Field(default_factory=list)
    nace_rev2: list[str] = Field(default_factory=list)
    naics_2022: list[str] = Field(default_factory=list)
    cpv_2008: list[str] = Field(default_factory=list)
    cpc: list[str] = Field(default_factory=list)
    buyer_types: list[str]
    applicable_features: list[str]
    sourcing_terms: list[str] = Field(default_factory=list)
    default_source_categories: list[str]

    @model_validator(mode="after")
    def has_classification(self):
        if not (self.hs_2022 or self.cpv_2008 or self.cpc):
            raise ValueError("a sector needs at least one product/procurement classification")
        return self


def load_sectors(path: Path = SECTOR_YAML) -> tuple[Sector, ...]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    sectors = tuple(sorted((Sector.model_validate(item) for item in raw), key=lambda item: item.sector_id))
    if len({item.sector_id for item in sectors}) != len(sectors):
        raise ValueError("sector ids must be unique")
    return sectors


def _joined(values: list[str]) -> str:
    return ";".join(sorted(dict.fromkeys(str(value) for value in values)))


def render_sector_markdown(sectors: tuple[Sector, ...] | list[Sector]) -> str:
    lines = [
        "# Lead research sectors", "", "Generated from `sectors.yaml`. Do not edit by hand.", "",
        "| Sector | HS 2022 | NACE Rev. 2 | Buyer types | Applicable features |",
        "|---|---|---|---|---|",
    ]
    for sector in sorted(sectors, key=lambda item: item.sector_id):
        lines.append(
            f"| {sector.name} (`{sector.sector_id}`) | {_joined(sector.hs_2022) or '—'} | "
            f"{_joined(sector.nace_rev2) or '—'} | {_joined(sector.buyer_types)} | "
            f"{_joined(sector.applicable_features)} |"
        )
    return "\n".join(lines) + "\n"


def render_sector_csv(sectors: tuple[Sector, ...] | list[Sector]) -> str:
    fields = [
        "taxonomy_version", "sector_id", "name", "aliases", "hs_2022", "nace_rev2",
        "naics_2022", "cpv_2008", "cpc", "buyer_types", "applicable_features",
        "sourcing_terms", "default_source_categories",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for sector in sorted(sectors, key=lambda item: item.sector_id):
        row = {field: _joined(getattr(sector, field)) for field in fields[3:]}
        row.update({"taxonomy_version": "2026.1", "sector_id": sector.sector_id, "name": sector.name})
        writer.writerow(row)
    return stream.getvalue()


def generate(check: bool = False) -> bool:
    sectors = load_sectors()
    expected = {SECTOR_MD: render_sector_markdown(sectors), SECTOR_CSV: render_sector_csv(sectors)}
    stale = [path for path, content in expected.items() if not path.exists() or path.read_text(encoding="utf-8") != content]
    if check:
        if stale:
            print("stale sector artifacts: " + ", ".join(path.name for path in stale))
            return False
        print("sector taxonomy artifacts are current")
        return True
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    print(f"generated {len(expected)} sector artifacts")
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    raise SystemExit(0 if generate(args.check) else 1)


if __name__ == "__main__":
    main()
