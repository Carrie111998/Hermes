"""Cron provider/model routing manifest and drift guard.

This module is deliberately file-based and read-only by default: the host updater
uses ``check`` to decide whether newly merged Hermes source may be restarted.
Only the explicit ``capture`` / ``restore`` commands write user state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from hermes_cli.fallback_config import get_fallback_chain, resolve_entry_api_key

MANIFEST_SCHEMA_VERSION = 2

RouteResolver = Callable[[dict[str, Any]], object]


def _read_jobs(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
    if not isinstance(jobs, list):
        raise ValueError(f"Cron registry {path} does not contain a jobs list")
    return [job for job in jobs if isinstance(job, dict)]


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"YAML file {path} must contain a mapping")
    return raw


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalise_route(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"provider": "", "model": "", "base_url": ""}
    return {
        "provider": str(value.get("provider") or ""),
        "model": str(value.get("model") or ""),
        "base_url": str(value.get("base_url") or ""),
    }


_FALLBACK_SOURCE_KEYS = ("fallback_providers", "fallback_model")
# Manifest values are later eligible for manual restore. Keep this deliberately
# narrow: unknown metadata can be a credential under a provider-specific name.
# Add a field only when it is proven routing-relevant and non-sensitive.
_ALLOWED_FALLBACK_ENTRY_KEYS = frozenset(
    {"provider", "model", "base_url", "key_env", "api_key_env", "api_mode"}
)


def _safe_fallback_value(value: Any) -> Any:
    """Copy only approved non-secret fallback-entry metadata into a manifest."""
    if isinstance(value, dict):
        copied: dict[str, Any] = {}
        for key, child in value.items():
            key_name = str(key)
            if key_name not in _ALLOWED_FALLBACK_ENTRY_KEYS:
                normalized = "".join(char for char in key_name.lower() if char.isalnum())
                credential_like = any(token in normalized for token in ("key", "token", "secret", "password", "auth"))
                problem = "inline credential field" if credential_like else "Unsupported fallback metadata field"
                raise ValueError(
                    f"{problem} '{key_name}'; allowlisted routing metadata only"
                )
            if child is not None and not isinstance(child, (str, int, float, bool)):
                raise ValueError(f"Unsupported fallback metadata value for '{key_name}'")
            copied[key_name] = child
        return copied
    if isinstance(value, list):
        return [_safe_fallback_value(item) for item in value]
    raise ValueError(f"Fallback configuration must be a mapping or list, got {type(value).__name__}")


def _fallback_sources(config: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _safe_fallback_value(config[key])
        for key in _FALLBACK_SOURCE_KEYS
        if key in config
    }


def _canonical_fallback_route(entry: dict[str, Any]) -> dict[str, Any]:
    route = _safe_fallback_value(entry)
    if not isinstance(route, dict):  # defensive; get_fallback_chain guarantees dicts.
        raise ValueError("Effective fallback entry must be a mapping")
    route["provider"] = str(route.get("provider") or "").strip()
    route["model"] = str(route.get("model") or "").strip()
    route["base_url"] = str(route.get("base_url") or "").strip().rstrip("/")
    return route


def _effective_fallback_routes(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [_canonical_fallback_route(entry) for entry in get_fallback_chain(config)]


def resolve_runtime_route(route: dict[str, Any]) -> str | None:
    """Resolve the same provider runtime snapshot used by the scheduler.

    This validates credentials/provider configuration only. It does not create an
    agent or perform an inference request, so an updater guard never spends model
    quota while checking an upgraded source tree.
    """
    provider = str(route.get("provider") or "").strip()
    model = str(route.get("model") or "").strip()
    if not provider or not model:
        return "route is missing provider or model"
    kwargs: dict[str, Any] = {"requested": provider}
    base_url = str(route.get("base_url") or "").strip()
    if base_url:
        kwargs["explicit_base_url"] = base_url
    if str(route.get("source") or "").startswith("fallback:"):
        kwargs["target_model"] = model
        api_key = resolve_entry_api_key(route)
        if api_key:
            kwargs["explicit_api_key"] = api_key
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(**kwargs)
    except Exception as exc:
        return f"{exc.__class__.__name__}: {exc}"
    if not isinstance(runtime, dict) or not str(runtime.get("provider") or "").strip():
        return "runtime provider resolver returned no provider"
    return None


def build_routing_policy(jobs: Iterable[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical policy managed by the routing guard.

    Only enabled model-backed jobs participate. no_agent jobs deliberately do
    not have a provider/model routing contract, even when stale fields remain
    in their JSON representation.
    """
    agent_jobs: list[dict[str, Any]] = []
    for job in jobs:
        if not job.get("enabled", True) or job.get("no_agent"):
            continue
        route = _normalise_route(job)
        agent_jobs.append(
            {
                "job_id": str(job.get("id") or ""),
                "provider": route["provider"],
                "model": route["model"],
                "base_url": route["base_url"],
            }
        )
    agent_jobs.sort(key=lambda item: item["job_id"])

    return {
        "fallback_sources": _fallback_sources(config),
        "fallback_routes": _effective_fallback_routes(config),
        "agent_jobs": agent_jobs,
    }


def _atomic_yaml_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(value, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def capture_manifest(*, jobs_path: Path, config_path: Path, manifest_path: Path) -> dict[str, Any]:
    """Capture the current approved routing policy to an atomic YAML manifest."""
    policy = build_routing_policy(_read_jobs(jobs_path), _read_yaml_mapping(config_path))
    missing = [item["job_id"] for item in policy["agent_jobs"] if not item["provider"] or not item["model"]]
    if missing:
        raise ValueError(f"Cannot capture unpinned active agent jobs: {', '.join(missing)}")
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "agent_job_count": len(policy["agent_jobs"]),
        "policy": policy,
        "policy_sha256": _canonical_hash(policy),
    }
    _atomic_yaml_write(manifest_path, manifest)
    return manifest


def check_manifest(
    *,
    jobs_path: Path,
    config_path: Path,
    manifest_path: Path,
    resolver: RouteResolver | None = None,
) -> dict[str, Any]:
    """Compare current routing with manifest and optionally validate all routes.

    ``resolver`` must be side-effect-free. It receives route mappings and returns
    ``None``/``True`` for success or a false/string detail for failure.
    """
    if not manifest_path.exists():
        return {"ok": False, "problems": [{"kind": "missing_manifest", "path": str(manifest_path)}]}
    manifest = _read_yaml_mapping(manifest_path)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return {
            "ok": False,
            "problems": [
                {
                    "kind": "unsupported_schema",
                    "expected": MANIFEST_SCHEMA_VERSION,
                    "actual": manifest.get("schema_version"),
                }
            ],
        }
    expected = manifest.get("policy")
    if not isinstance(expected, dict):
        return {"ok": False, "problems": [{"kind": "invalid_manifest", "path": str(manifest_path)}]}

    actual = build_routing_policy(_read_jobs(jobs_path), _read_yaml_mapping(config_path))
    expected_hash = str(manifest.get("policy_sha256") or "")
    manifest_policy_hash = _canonical_hash(expected)
    actual_hash = _canonical_hash(actual)
    problems: list[dict[str, Any]] = []
    if not expected_hash or expected_hash != manifest_policy_hash:
        problems.append(
            {
                "kind": "manifest_hash_mismatch",
                "stored_sha256": expected_hash or None,
                "computed_sha256": manifest_policy_hash,
            }
        )
    if expected != actual:
        problems.append(
            {
                "kind": "manifest_mismatch",
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
            }
        )

    if resolver is not None:
        routes: list[dict[str, Any]] = []
        for job in actual["agent_jobs"]:
            routes.append({"source": f"job:{job['job_id']}", **_normalise_route(job)})
        for index, fallback in enumerate(actual["fallback_routes"]):
            routes.append({**fallback, "source": f"fallback:{index}"})
        seen: set[tuple[str, str, str]] = set()
        for route in routes:
            key = (route["provider"], route["model"], route["base_url"])
            if key in seen:
                continue
            seen.add(key)
            if not route["provider"] or not route["model"]:
                problems.append({"kind": "unpinned_route", "route": route})
                continue
            result = resolver(route)
            if result is not None and result is not True:
                problems.append(
                    {
                        "kind": "unresolvable_route",
                        "route": route,
                        "detail": "resolver returned false" if result is False else str(result),
                    }
                )

    return {
        "ok": not problems,
        "expected_sha256": expected_hash,
        "actual_sha256": actual_hash,
        "agent_job_count": len(actual["agent_jobs"]),
        "problems": problems,
    }


def _atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_bytes_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _restore_backup(path: Path) -> Path:
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.routing-restore-{suffix}.bak")
    backup.write_bytes(path.read_bytes())
    os.chmod(backup, 0o600)
    return backup


def restore_manifest(*, jobs_path: Path, config_path: Path, manifest_path: Path) -> dict[str, Any]:
    """Restore routing-only fields from a verified captured manifest.

    This is a manual incident operation. Delivery target, prompt, schedule,
    skills, enabled state, and unrelated config keys remain untouched. Both
    registry files are rolled back to their original bytes if either replacement
    fails, so restore does not leave a partial routing state behind.
    """
    manifest = _read_yaml_mapping(manifest_path)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported manifest schema in {manifest_path}: {manifest.get('schema_version')!r}"
        )
    policy = manifest.get("policy")
    stored_hash = str(manifest.get("policy_sha256") or "")
    if not isinstance(policy, dict) or not stored_hash or stored_hash != _canonical_hash(policy):
        raise ValueError(f"Invalid or tampered manifest policy in {manifest_path}")

    expected_jobs = policy.get("agent_jobs")
    fallback_sources = policy.get("fallback_sources")
    if not isinstance(expected_jobs, list) or not isinstance(fallback_sources, dict):
        raise ValueError(f"Invalid manifest routing fields in {manifest_path}")

    expected_by_id = {
        str(item.get("job_id")): item
        for item in expected_jobs
        if isinstance(item, dict) and item.get("job_id")
    }
    raw_jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = raw_jobs.get("jobs", []) if isinstance(raw_jobs, dict) else raw_jobs
    if not isinstance(jobs, list):
        raise ValueError(f"Cron registry {jobs_path} does not contain a jobs list")

    restored_job_ids: list[str] = []
    for job in jobs:
        if not isinstance(job, dict) or not job.get("enabled", True) or job.get("no_agent"):
            continue
        expected = expected_by_id.get(str(job.get("id") or ""))
        if expected is None:
            continue
        changed = False
        for key in ("provider", "model", "base_url"):
            expected_value = str(expected.get(key) or "")
            if expected_value:
                if job.get(key) != expected_value:
                    job[key] = expected_value
                    changed = True
            elif key in job:
                job.pop(key)
                changed = True
        if changed:
            restored_job_ids.append(str(job.get("id")))

    config = _read_yaml_mapping(config_path)
    restored_config = dict(config)
    for key in _FALLBACK_SOURCE_KEYS:
        if key in fallback_sources:
            restored_config[key] = fallback_sources[key]
        else:
            restored_config.pop(key, None)
    fallback_changed = restored_config != config
    if not restored_job_ids and not fallback_changed:
        return {"restored_job_ids": [], "fallback_restored": False, "backups": []}

    original_jobs = jobs_path.read_bytes()
    original_config = config_path.read_bytes()
    changed_paths = []
    if restored_job_ids:
        changed_paths.append(jobs_path)
    if fallback_changed:
        changed_paths.append(config_path)
    backups = [str(_restore_backup(path)) for path in changed_paths]
    try:
        if restored_job_ids:
            _atomic_json_write(jobs_path, raw_jobs)
        if fallback_changed:
            _atomic_yaml_write(config_path, restored_config)
    except BaseException:
        rollback_errors: list[str] = []
        for path, original in ((jobs_path, original_jobs), (config_path, original_config)):
            if path not in changed_paths:
                continue
            try:
                _atomic_bytes_write(path, original)
            except BaseException as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                "Routing restore failed and rollback was incomplete: " + "; ".join(rollback_errors)
            )
        raise

    return {
        "restored_job_ids": restored_job_ids,
        "fallback_restored": fallback_changed,
        "backups": backups,
    }


def _default_paths() -> tuple[Path, Path, Path]:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    return home / "cron" / "jobs.json", home / "config.yaml", home / "state" / "cron-routing-manifest.yaml"


def _acquire_restore_lock() -> tuple[Path, object]:
    """Acquire the same exclusive tick lock the scheduler uses.

    Restore is a manual incident operation — it blocks until the lock is
    available, then holds it so no concurrent tick, mark_job_run, or job
    update can interleave with the cross-file registry writes.
    """
    import fcntl as _fcntl

    from hermes_constants import get_hermes_home

    lock_dir = get_hermes_home() / "cron"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".tick.lock"
    lock_fd = open(lock_path, "w", encoding="utf-8")
    _fcntl.flock(lock_fd.fileno(), _fcntl.LOCK_EX)  # blocking — this is manual
    return lock_path, lock_fd


def _release_restore_lock(lock_fd: object) -> None:
    """Release a restore lock, discarding the fd without closing twice."""
    import fcntl as _fcntl
    try:
        _fcntl.flock(lock_fd.fileno(), _fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_fd.close()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cron routing manifest guard")
    parser.add_argument("command", choices=("capture", "check", "restore"))
    parser.add_argument("--jobs-path", type=Path)
    parser.add_argument("--config-path", type=Path)
    parser.add_argument("--manifest-path", type=Path)
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Restore without acquiring the tick lock (offline / no Gateway running).",
    )
    args = parser.parse_args(argv)
    default_jobs, default_config, default_manifest = _default_paths()
    jobs_path = args.jobs_path or default_jobs
    config_path = args.config_path or default_config
    manifest_path = args.manifest_path or default_manifest
    if args.command == "capture":
        result = capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
    elif args.command == "restore":
        lock_fd = None
        try:
            if not args.no_lock:
                _, lock_fd = _acquire_restore_lock()
            result = restore_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
        finally:
            if lock_fd is not None:
                _release_restore_lock(lock_fd)
    else:
        result = check_manifest(
            jobs_path=jobs_path,
            config_path=config_path,
            manifest_path=manifest_path,
            resolver=resolve_runtime_route,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

    if not result.get("ok", True):
        problems = result.get("problems", [])
        if any(p.get("kind") == "missing_manifest" for p in problems):
            return 2  # bootstrap required — not a hard routing failure
        return 1  # routing or hash mismatch — needs investigation
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
