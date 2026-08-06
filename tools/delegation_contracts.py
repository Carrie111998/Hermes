"""Strikte Hermes-Verträge für Delegationsaufgaben und Review-Gates.

Die Modelle sind bewusst außerhalb der Core-Tool-Schemata gehalten. So kann
Hermes Delegationsresultate prüfen, ohne die Prompt-Caching-Oberfläche oder die
bestehenden Model-Tools zu verändern.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictContract(BaseModel):
    """Gemeinsame strikte Basis für alle Delegationsverträge."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LaneTask(StrictContract):
    """Normalisierte Eingabe für einen einzelnen Delegationsschritt."""

    task_id: NonEmptyText = Field(description="Stabile ID innerhalb des Dispatches")
    goal: NonEmptyText = Field(description="Konkretes, überprüfbares Ziel")
    role: Literal["leaf", "orchestrator"]
    context: str = ""
    workdir: str | None = None
    parent_task_id: str | None = None


class LaneResult(StrictContract):
    """Strukturiertes Ergebnis eines Delegationsschritts."""

    task_id: NonEmptyText
    status: Literal["completed", "failed", "blocked"]
    summary: NonEmptyText
    artifacts: list[NonEmptyText] = Field(default_factory=list)
    verification: list[NonEmptyText] = Field(default_factory=list)
    error: str | None = None


class ReviewDecision(StrictContract):
    """Explizite Entscheidung eines Review-Gates."""

    task_id: NonEmptyText
    decision: Literal["approve", "request_changes", "reject"]
    rationale: NonEmptyText
    required_changes: list[NonEmptyText] = Field(default_factory=list)


def validate_contract(contract_type: str, payload: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validiert einen Vertrag und liefert modellfreundliche Fehlermeldungen."""

    contract = {
        "LaneTask": LaneTask,
        "LaneResult": LaneResult,
        "ReviewDecision": ReviewDecision,
    }.get(contract_type)
    if contract is None:
        return False, [f"Unbekannter Vertragstyp: {contract_type}"]

    try:
        contract.model_validate(payload)
    except Exception as exc:
        return False, [str(exc)]
    return True, []
