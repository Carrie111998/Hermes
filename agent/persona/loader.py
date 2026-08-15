"""Fail-closed loader for bundled, owner-controlled Persona Canon snapshots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .schema import PersonaKernel, PersonaValidationError

SUPPORTED_CANON_VERSION = "1.0.0"
_CANON_DIR = Path(__file__).with_name("canon")
from .registry import REGISTRY

class PersonaCanonError(ValueError):
    """Raised when Canon cannot be verified exactly."""

def canonical_payload(data: Mapping[str, Any]) -> bytes:
    payload = {key: value for key, value in data.items() if key != "checksum"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

def calculate_checksum(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(data)).hexdigest()

def load_persona_kernel(persona_id: str, *, canon_dir: Path | None = None) -> PersonaKernel:
    registration = REGISTRY.get(persona_id)
    if registration is None:
        raise PersonaCanonError(f"unknown persona_id: {persona_id}")
    path = (canon_dir or _CANON_DIR) / registration.filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PersonaCanonError(f"cannot load verified Persona Canon: {persona_id}") from exc
    try:
        kernel = PersonaKernel.from_mapping(data)
    except PersonaValidationError as exc:
        raise PersonaCanonError(str(exc)) from exc
    if kernel.canon_version != registration.supported_version:
        raise PersonaCanonError(f"unsupported canon_version: {kernel.canon_version}; expected {SUPPORTED_CANON_VERSION}")
    actual = calculate_checksum(data)
    if actual != kernel.checksum:
        raise PersonaCanonError(f"Persona Canon checksum mismatch for {persona_id}: expected {kernel.checksum}, got {actual}")
    return kernel
