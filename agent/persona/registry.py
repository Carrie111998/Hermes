"""Data-only registry of Owner-approved Persona Canon snapshots."""
from dataclasses import dataclass

@dataclass(frozen=True)
class PersonaRegistration:
    persona_id: str
    filename: str
    supported_version: str = "1.0.0"
    status: str = "CONFIRMED"

REGISTRY = {item.persona_id: item for item in (
    PersonaRegistration("police_horitius", "police_horitius.json"),
    PersonaRegistration("curator_orchestra", "curator_orchestra.json"),
    PersonaRegistration("doctrina_share", "doctrina_share.json", status="PARTIAL"),
    PersonaRegistration("persona_gemini", "persona_gemini.json", status="PARTIAL"),
    PersonaRegistration("mercator_vale", "mercator_vale.json", status="PARTIAL"),
    PersonaRegistration("exor_verelden", "exor_verelden.json"),
    PersonaRegistration("ordinator_detailer", "ordinator_detailer.json", status="PARTIAL"),
    PersonaRegistration("beg_weag", "beg_weag.json"),
    PersonaRegistration("literary_reviser", "literary_reviser.json", status="PARTIAL"),
)}
