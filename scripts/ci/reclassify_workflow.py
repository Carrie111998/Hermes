#!/usr/bin/env python3
"""Mechanically reclassify a workflow diff before validation gates.

The detector is deliberately conservative: an unknown or empty diff fails
closed into the complete route. It can consume explicit paths/counts locally or
the corresponding environment variables in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import PurePosixPath

_MAX_SMALL_FILES = 5
_MAX_SMALL_LINES = 200

_DEPENDENCY_FILES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
}

_SENSITIVE_COMPONENTS = {
    "auth",
    "authentication",
    "authorization",
    "iam",
    "permission",
    "permissions",
    "secret",
    "secrets",
    "credential",
    "credentials",
    "payment",
    "payments",
    "billing",
    "upload",
    "uploads",
    "migration",
    "migrations",
    "schema",
    "schemas",
    "infra",
    "infrastructure",
    "network",
    "security",
    "oauth",
    "sso",
    "session",
    "sessions",
    "cookie",
    "cookies",
    "export",
    "exports",
    "scripts",
    "tests",
}

_SENSITIVE_FILES = {
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".env",
    ".env.example",
}

_CONTENT_PATTERNS = {
    "auth": re.compile(r"\b(?:auth(?:entication|orization)?|oauth|sso)\b", re.I),
    "permission": re.compile(r"\b(?:permission|role|rbac|acl|admin)\b", re.I),
    "secret": re.compile(r"\b(?:secret|credential|api[_-]?key|access[_-]?token|private[_-]?key)\b", re.I),
    "sensitive_data": re.compile(r"\b(?:pii|personal[_ -]?data|sensitive[_ -]?data|encrypt|decrypt)\b", re.I),
    "payment": re.compile(r"\b(?:payment|billing|stripe|invoice)\b", re.I),
    "upload": re.compile(r"\b(?:upload|multipart|file[_ -]?input)\b", re.I),
    "migration": re.compile(r"\b(?:migration|schema|alter table|create table)\b", re.I),
    "public_contract": re.compile(r"\b(?:public api|openapi|graphql|webhook|export)\b", re.I),
    "network": re.compile(r"\b(?:iam|firewall|cors|network|subnet|security group)\b", re.I),
}


def _normalize(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./")


def _path_reason(path: str) -> str | None:
    normalized = _normalize(path)
    if not normalized:
        return None
    name = PurePosixPath(normalized).name
    if name in _DEPENDENCY_FILES or name.endswith((".lock", ".lockb")):
        return f"dependency_or_lockfile:{normalized}"
    if name in _SENSITIVE_FILES or normalized.startswith((".github/", "deploy/", "deployment/", "terraform/", "k8s/", "helm/")):
        return f"sensitive_path:{normalized}"
    components = {part.lower().replace("-", "_") for part in PurePosixPath(normalized).parts}
    if components & _SENSITIVE_COMPONENTS:
        return f"sensitive_path:{normalized}"
    lower_name = name.lower().replace("-", "_")
    if any(token in lower_name for token in _SENSITIVE_COMPONENTS):
        return f"sensitive_path:{normalized}"
    return None


def classify(
    files: list[str],
    *,
    added: int,
    deleted: int,
    initial_route: str = "standard",
    patch_text: str = "",
) -> dict[str, object]:
    if initial_route not in {"direct", "standard", "complete"}:
        raise ValueError("initial_route must be direct, standard, or complete")
    normalized = sorted({_normalize(path) for path in files if _normalize(path)})
    reasons: list[str] = []
    if not normalized:
        reasons.append("empty_diff")
    for path in normalized:
        if reason := _path_reason(path):
            reasons.append(reason)
    for label, pattern in _CONTENT_PATTERNS.items():
        if pattern.search(patch_text):
            reasons.append(f"sensitive_content:{label}")

    reasons = sorted(set(reasons))
    sensitive = bool(reasons)
    changed_lines = max(0, added) + max(0, deleted)
    small_by_size = (
        len(normalized) <= _MAX_SMALL_FILES
        and changed_lines <= _MAX_SMALL_LINES
    )
    parallel_allowed = small_by_size and not sensitive
    readers = ["general_review"]
    if sensitive:
        readers.append("downstream_security")
    schedule = "parallel_review_launch_qa" if parallel_allowed else "readers_then_execution"

    return {
        "sensitive": sensitive,
        "route": initial_route,
        "small_by_size": small_by_size,
        "parallel_allowed": parallel_allowed,
        "changed_files": len(normalized),
        "changed_lines": changed_lines,
        "required_readers": readers,
        "schedule": schedule,
        "reasons": reasons,
    }


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Changed paths; stdin is used when omitted")
    parser.add_argument(
        "--initial-route",
        choices=("direct", "standard", "complete"),
        default=os.environ.get("INITIAL_ROUTE", "standard"),
    )
    parser.add_argument("--added", type=int, default=None)
    parser.add_argument("--deleted", type=int, default=None)
    parser.add_argument("--patch-file")
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args(argv)

    paths = args.paths or sys.stdin.read().splitlines()
    patch_text = ""
    if args.patch_file:
        with open(args.patch_file, encoding="utf-8", errors="replace") as handle:
            patch_text = handle.read()
    added = args.added if args.added is not None else _env_int("DIFF_ADDED")
    deleted = args.deleted if args.deleted is not None else _env_int("DIFF_DELETED")
    result = classify(
        paths,
        added=added,
        deleted=deleted,
        initial_route=args.initial_route,
        patch_text=patch_text,
    )

    print(json.dumps(result, sort_keys=True))
    output_path = os.environ.get("GITHUB_OUTPUT") if args.github_output else None
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            for key in (
                "sensitive",
                "small_by_size",
                "parallel_allowed",
                "route",
                "schedule",
            ):
                value = result[key]
                if isinstance(value, bool):
                    value = str(value).lower()
                handle.write(f"{key}={value}\n")
            handle.write(f"required_readers={json.dumps(result['required_readers'])}\n")
            handle.write(f"reasons={json.dumps(result['reasons'])}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
