"""CLI handlers for the oh-my-hermes Hermes plugin."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from hermes_constants import display_hermes_home, get_hermes_home

from . import _submodule_root, _upstream_skills_root


def _skill_destination(name: str) -> Path:
    return get_hermes_home() / "skills" / name


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _install_skills(force: bool = False) -> dict:
    source = _upstream_skills_root()
    if not source.is_dir():
        return {"ok": False, "error": f"Skills directory missing: {source}"}

    installed = []
    skipped = []
    errors = []
    for skill in sorted(source.iterdir()):
        if not skill.is_dir() or not (skill / "SKILL.md").is_file():
            continue
        destination = _skill_destination(skill.name)
        if destination.exists() or destination.is_symlink():
            if not force:
                skipped.append(skill.name)
                continue
            _remove_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.symlink_to(skill.resolve(), target_is_directory=True)
            action = "symlink"
        except (OSError, NotImplementedError):
            try:
                shutil.copytree(skill, destination)
                action = "copy"
            except OSError as exc:
                errors.append({"skill": skill.name, "error": str(exc)})
                continue
        installed.append({"skill": skill.name, "action": action})

    return {
        "ok": not errors,
        "source": str(source),
        "destination": f"{display_hermes_home()}/skills",
        "installed": installed,
        "skipped": skipped,
        "errors": errors,
        "count": len(installed),
    }


def _status() -> dict:
    source = _submodule_root()
    skills = _upstream_skills_root()
    installed = sum(
        1
        for skill in skills.iterdir()
        if skill.is_dir() and (skill / "SKILL.md").is_file()
    ) if skills.is_dir() else 0
    destination = get_hermes_home() / "skills"
    linked = sum(
        1
        for skill in skills.iterdir()
        if skill.is_dir()
        and (skill / "SKILL.md").is_file()
        and (_skill_destination(skill.name).exists() or _skill_destination(skill.name).is_symlink())
    ) if skills.is_dir() else 0
    return {
        "ok": source.is_dir() and skills.is_dir(),
        "plugin": "oh-my-hermes",
        "submodule": str(source),
        "submodule_present": source.is_dir(),
        "skills_source": str(skills),
        "skills_available": installed,
        "skills_linked_or_copied": linked,
        "skills_destination": str(destination),
        "ready": source.is_dir() and skills.is_dir() and linked >= installed,
    }


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="oh_my_hermes_command")
    subs.add_parser("status", help="Show submodule and workflow skill readiness")
    install = subs.add_parser("install", help="Install workflow skills into the active Hermes profile")
    install.add_argument("--force", action="store_true", help="Replace existing skill directories")
    update = subs.add_parser("update", help="Update the submodule and refresh workflow skills")
    update.add_argument("--force", action="store_true", help="Replace existing skill directories")
    update.add_argument("--no-fetch", action="store_true", help="Skip git fetch/pull and only refresh skills")


def _print(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok", False) else 1


def oh_my_hermes_command(args: argparse.Namespace) -> int:
    command = getattr(args, "oh_my_hermes_command", None)
    if command == "status":
        return _print(_status())
    if command == "install":
        return _print({"status": _status(), "install": _install_skills(getattr(args, "force", False)), "ok": True})
    if command == "update":
        if not getattr(args, "no_fetch", False):
            return _print({
                "ok": False,
                "error": "Automatic git update is intentionally not performed by the plugin; update the submodule explicitly, then rerun with --no-fetch.",
                "hint": "git -C vendor/oh-my-hermes pull --ff-only",
            })
        return _print({"status": _status(), "install": _install_skills(getattr(args, "force", False)), "ok": True})
    print("usage: hermes oh-my-hermes {status,install,update}")
    return 2
