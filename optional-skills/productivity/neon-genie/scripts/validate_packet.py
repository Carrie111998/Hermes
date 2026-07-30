#!/usr/bin/env python3
"""Validate a JSON packet against a Neon Genie schema (stdlib only).

Supports a practical JSON Schema subset:
  - type (object/array/string/number/integer/boolean/null)
  - required
  - properties (recursive)
  - items (for arrays)
  - enum
  - minLength / minItems (when present)

Usage:
  python scripts/validate_packet.py --packet path.json --type opportunity
  python scripts/validate_packet.py --packet path.json --schema schemas/opportunity-packet.schema.json
  python scripts/validate_packet.py --packet path.json --type receipt --strict-authority
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import paths as ng_paths  # noqa: E402

PACKET_TYPE_TO_SCHEMA = {
    "opportunity": "opportunity-packet.schema.json",
    "product": "product-packet.schema.json",
    "fragmentation": "fragmentation-packet.schema.json",
    "zero_option": "zero-option-packet.schema.json",
    "zero-option": "zero-option-packet.schema.json",
    "agentic": "agentic-service-graph.schema.json",
    "agentic_service": "agentic-service-graph.schema.json",
    "commercial": "commercial-simulation.schema.json",
    "evidence": "evidence-intelligence-packet.schema.json",
    "evidence_intelligence": "evidence-intelligence-packet.schema.json",
    "memetic": "memetic-pressure-packet.schema.json",
    "audit": "audit-delivery-packet.schema.json",
    "audit_delivery": "audit-delivery-packet.schema.json",
    "wayfinder": "wayfinder-execution-packet.schema.json",
    "receipt": "run-receipt.schema.json",
    "run_receipt": "run-receipt.schema.json",
    "envelope": "run-envelope.schema.json",
}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"FAIL: cannot read JSON {path}: {exc}") from exc


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def type_matches(value: Any, expected: str) -> bool:
    actual = type_name(value)
    if expected == "number":
        return actual in {"number", "integer"}
    if expected == "integer":
        return actual == "integer"
    return actual == expected


def validate_schema(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema must be object"]

    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        if not type_matches(instance, expected_type):
            errors.append(
                f"{path}: expected type {expected_type}, got {type_name(instance)}"
            )
            return errors
    elif isinstance(expected_type, list):
        if not any(type_matches(instance, t) for t in expected_type if isinstance(t, str)):
            errors.append(
                f"{path}: expected one of {expected_type}, got {type_name(instance)}"
            )
            return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value {instance!r} not in enum {schema['enum']}")

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")

    if isinstance(instance, str) and "pattern" in schema:
        if not re.search(str(schema["pattern"]), instance):
            errors.append(f"{path}: string does not match pattern {schema['pattern']}")

    if isinstance(instance, str) and "minLength" in schema:
        if len(instance) < int(schema["minLength"]):
            errors.append(f"{path}: string shorter than minLength {schema['minLength']}")

    if isinstance(instance, list) and "minItems" in schema:
        if len(instance) < int(schema["minItems"]):
            errors.append(f"{path}: array shorter than minItems {schema['minItems']}")

    if isinstance(instance, dict) and schema.get("type", "object") in ("object", None, ["object"]):
        required = schema.get("required") or []
        for key in required:
            if key not in instance:
                errors.append(f"{path}: missing required property: {key}")
        props = schema.get("properties") or {}
        for key, subschema in props.items():
            if key in instance and isinstance(subschema, dict) and subschema:
                # Only recurse when subschema has real constraints
                if any(
                    k in subschema
                    for k in ("type", "required", "properties", "items", "enum", "const", "pattern")
                ):
                    errors.extend(
                        validate_schema(instance[key], subschema, f"{path}.{key}")
                    )

    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        if item_schema:
            for i, item in enumerate(instance):
                errors.extend(validate_schema(item, item_schema, f"{path}[{i}]"))

    return errors


def authority_checks(instance: Any) -> list[str]:
    """Extra advisory guards when --strict-authority is set."""
    errors: list[str] = []
    if not isinstance(instance, dict):
        return errors
    if instance.get("grants_execution") is True:
        errors.append("authority: grants_execution must not be true")
    if instance.get("authority") in {"execute", "execution", "spend", "publish"}:
        errors.append(f"authority: forbidden authority value {instance.get('authority')!r}")
    auth = instance.get("authority")
    if isinstance(auth, dict):
        for k in ("execution", "spending", "publishing"):
            if auth.get(k) is True:
                errors.append(f"authority.{k} must not be true in advisory packets")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Neon Genie packet JSON")
    parser.add_argument("--packet", required=True, type=Path, help="Path to packet JSON")
    parser.add_argument(
        "--type",
        dest="packet_type",
        help=f"Packet type alias: {', '.join(sorted(set(PACKET_TYPE_TO_SCHEMA)))}",
    )
    parser.add_argument("--schema", type=Path, help="Explicit schema path")
    parser.add_argument(
        "--strict-authority",
        action="store_true",
        help="Fail if packet claims execution/spend/publish authority",
    )
    args = parser.parse_args(argv)

    if not args.packet.is_file():
        print(f"FAIL: packet not found: {args.packet}", file=sys.stderr)
        return 1

    if args.schema:
        schema_path = args.schema if args.schema.is_absolute() else SKILL_ROOT / args.schema
    elif args.packet_type:
        key = args.packet_type.strip().lower().replace(" ", "_")
        name = PACKET_TYPE_TO_SCHEMA.get(key)
        if not name:
            print(f"FAIL: unknown packet type: {args.packet_type}", file=sys.stderr)
            print(f"  known: {', '.join(sorted(set(PACKET_TYPE_TO_SCHEMA)))}", file=sys.stderr)
            return 1
        try:
            schema_path = ng_paths.schema_file(name)
        except FileNotFoundError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
    else:
        print("FAIL: provide --type or --schema", file=sys.stderr)
        return 1

    # Allow --schema schemas/foo.json even on hub layout (references/schemas/)
    if args.schema and not schema_path.is_file():
        alt = str(args.schema).replace("\\", "/")
        if alt.startswith("schemas/"):
            try:
                schema_path = ng_paths.schema_file(Path(alt).name)
            except FileNotFoundError:
                pass

    if not schema_path.is_file():
        print(f"FAIL: schema not found: {schema_path}", file=sys.stderr)
        return 1

    packet = load_json(args.packet)
    schema = load_json(schema_path)
    if not isinstance(schema, dict):
        print("FAIL: schema root must be an object", file=sys.stderr)
        return 1

    errors = validate_schema(packet, schema)
    if args.strict_authority:
        errors.extend(authority_checks(packet))

    if errors:
        print(f"FAIL: {args.packet} vs {schema_path.name}", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"PASS: packet validates against {schema_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
