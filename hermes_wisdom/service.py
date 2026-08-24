"""Application service shared by Wisdom CLI, dashboard, and desktop APIs."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import webbrowser
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from hermes_constants import get_hermes_home, get_skills_dir
from tools.skill_usage import _find_skill_dir, is_bundled, is_hub_installed
from tools.skills_guard import scan_skill, should_allow_install
from tools.skillevaluator_scan import run_tier1_scan, tier1_advisory_enabled

from .client import WisdomClient, WisdomValidationError
from .compatibility import CompatibilityResult, detect_local_capabilities, evaluate
from .contract import (
    CONTRACT_PIN,
    PackageManifest,
    SystemSpecification,
    author_description_hash,
    canonical_json_bytes,
    sha256_address,
)
from .package import (
    PackagePolicyError,
    PreparedPackage,
    prepare_package,
    verify_content_files,
)
from .store import WisdomStore


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


def _config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        value = (load_config() or {}).get("wisdom") or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def portal_base_url() -> str:
    value = _config().get("portal_url")
    return (
        str(value).rstrip("/")
        if isinstance(value, str) and value.strip()
        else "https://portal.nousresearch.com"
    )


def _slug(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:64]
    if not SLUG_RE.fullmatch(candidate):
        raise PackagePolicyError(
            "skill name cannot be converted to a valid Wisdom slug"
        )
    return candidate


def _source_fingerprint(source: Path) -> str:
    rows: list[str] = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rows.append(
                f"{path.relative_to(source).as_posix()} {sha256_address(path.read_bytes())}\n"
            )
    return sha256_address("".join(rows).encode("utf-8"))


def _scan_summary(path: Path) -> dict[str, Any]:
    guard = scan_skill(path, source="community")
    allowed, reason = should_allow_install(guard)
    tier1 = run_tier1_scan(path) if tier1_advisory_enabled() else None
    return {
        "guard": {
            "verdict": guard.verdict,
            "allowed": allowed,
            "reason": reason,
            "findings": [
                {
                    "severity": finding.severity,
                    "category": finding.category,
                    "file": finding.file,
                    "line": finding.line,
                    "match": finding.match,
                }
                for finding in guard.findings
            ],
        },
        "skill_evaluator": (
            {
                "status": "available",
                "passed": tier1.passed,
                "findings": [
                    {
                        "check": finding.check,
                        "severity": finding.severity,
                        "file": finding.file,
                        "line": finding.line,
                        "message": finding.message,
                        "secrets_class": finding.is_secrets_class,
                    }
                    for finding in tier1.findings
                ],
                "incomplete_checks": tier1.incomplete_checks,
            }
            if tier1 and tier1.available
            else {
                "status": "disabled" if tier1 is None else "unavailable",
                "passed": None,
                "findings": [],
            }
        ),
    }


def _has_high_confidence_secret(scan: dict[str, Any]) -> bool:
    return any(
        item.get("secrets_class") and item.get("severity") in {"critical", "high"}
        for item in scan["skill_evaluator"]["findings"]
    )


def _extract_model_text(response: Any) -> str:
    from agent.auxiliary_client import extract_content_or_reasoning

    return extract_content_or_reasoning(response).strip()


def draft_description(skill_md: str) -> str:
    """Draft author copy with the configured model under normal routing rules."""
    from agent.auxiliary_client import call_llm

    response = call_llm(
        messages=[
            {
                "role": "system",
                "content": (
                    "Write one concise, outcome-oriented plain-text description of this Hermes skill. "
                    "Do not claim quality, safety, usage, popularity, or platform verification. "
                    "Return only the description, at most 600 characters."
                ),
            },
            {"role": "user", "content": skill_md[:24000]},
        ],
        temperature=0.2,
        max_tokens=220,
        timeout=60,
    )
    text = _extract_model_text(response)
    if not text:
        raise PackagePolicyError(
            "the configured model did not return an author description"
        )
    return text


class WisdomService:
    def __init__(
        self, *, store: WisdomStore | None = None, client: WisdomClient | None = None
    ) -> None:
        self.store = store or WisdomStore()
        self._client = client

    @property
    def client(self) -> WisdomClient:
        if self._client is None:
            self._client = WisdomClient(
                timeout=float(_config().get("request_timeout", 30))
            )
        return self._client

    def setup(self) -> dict[str, Any]:
        installation_id = self.store.installation_identity()
        capability = self.client.capability()
        registered = self.client.register_identity(installation_id)
        org_id = self.client.display_org_id
        if not org_id:
            raise WisdomValidationError(
                "team organization identity is missing from the current token"
            )
        self.store.verify_installation_identity(org_id)
        managed = get_skills_dir() / "_wisdom"
        managed.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = managed / ".active_org"
        marker.write_text(org_id + "\n", encoding="utf-8")
        try:
            marker.chmod(0o600)
        except OSError:
            pass
        recovered = self.reconcile_pending_install_records()
        candidates = self.scan_candidates()
        return {
            "ok": True,
            "installation_id": installation_id,
            "organization_id": org_id,
            "registered": registered,
            "capabilities": capability.get("capabilities", []),
            "display_scopes": list(self.client.display_scopes),
            "database": str(self.store.path),
            "managed_directory": str(managed / org_id),
            "candidate_count": len(candidates),
            "recovered_gateway_records": recovered,
            "disclosure": (
                "Candidate signals stay on this profile. Only owner-approved private draft bytes, "
                "author copy, manifest metadata, and managed-install state reach the Gateway."
            ),
        }

    def reconcile_pending_install_records(self) -> list[str]:
        """Retry final Gateway install records after a local commit succeeded."""
        recovered: list[str] = []
        installation_id = self.store.installation_identity()
        for operation in self.store.pending_operations():
            if (
                operation["kind"] != "install"
                or operation["phase"] != "local_ledger_committed"
            ):
                continue
            try:
                plan = json.loads(operation["payload_json"])
                self.client.record_install(
                    skill_id=plan["skill_id"],
                    installation_id=installation_id,
                    version=int(plan["version"]),
                    takedown_generation=int(plan["takedown_generation"]),
                    update_mode=plan.get("update_mode"),
                )
            except Exception:
                continue
            self.store.advance(operation["id"], "gateway_recorded", done=True)
            recovered.append(str(operation["entity_id"]))
        return recovered

    def status(self) -> dict[str, Any]:
        try:
            client = self.client
            live = True
            capability = client.capability()
            scopes = list(client.display_scopes)
            admin_gate = (
                client.identity.get("claims", {}).get("tool_gateway_admin") is True
            )
        except Exception as exc:
            live = False
            capability = {}
            scopes = []
            admin_gate = False
            error = str(exc)
        return {
            "configured": bool(_config().get("enabled", False)),
            "gateway_available": live,
            "error": None if live else error,
            "capability_advertised": "wisdom" in (capability.get("capabilities") or []),
            "display_scopes": scopes,
            "dogfood_admin_claim": admin_gate,
            "installation_id": self.store.installation_identity(),
            "verified_org_id": self.store.active_org_id(),
            "pending_operations": self.store.pending_operations(),
            "contract": asdict(CONTRACT_PIN),
        }

    def _eligible_paths(self) -> list[Path]:
        root = get_skills_dir().resolve()
        if not root.exists():
            return []
        paths: list[Path] = []
        for skill_md in sorted(root.rglob("SKILL.md")):
            try:
                rel = skill_md.relative_to(root)
            except ValueError:
                continue
            if any(
                part in {".archive", "_org", "_wisdom", ".hub"} for part in rel.parts
            ):
                continue
            path = skill_md.parent
            name = path.name
            if is_bundled(name) or is_hub_installed(name):
                continue
            paths.append(path)
        return paths

    def scan_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        qualified = {
            (str(event["skill_id"]), str(event["content_hash"])): event
            for event in self.store.local_events(kind="wisdom.candidate")
        }
        eligible_paths = self._eligible_paths()
        self.store.mark_missing_skills({str(path.resolve()) for path in eligible_paths})
        for path in eligible_paths:
            source_hash = _source_fingerprint(path)
            skill_id = self.store.register_skill(
                path, content_hash=source_hash, source_kind="local"
            )
            try:
                # Preparation remains manual; this dry structural pass rejects
                # scripts/templates/unsupported bytes without uploading anything.
                prepare_package(
                    path,
                    overlay_root=self.store.root / "scan-overlays",
                    author_description=f"Local skill {path.name}.",
                    owner="local-scan",
                    installation_id=self.store.installation_identity(),
                )
                eligibility = "eligible"
                reason = None
            except PackagePolicyError as exc:
                eligibility = "instruction_only_fork_required"
                reason = str(exc)
            event = qualified.get((skill_id, source_hash))
            candidates.append({
                "local_skill_id": skill_id,
                "name": path.name,
                "path": str(path),
                "content_hash": source_hash,
                "eligibility": eligibility,
                "reason": (
                    reason
                    if reason
                    else json.dumps(event["payload"]["local_reasons"], sort_keys=True)
                    if event
                    else None
                ),
                "qualification": (
                    str(event["qualification"]) if event else "manual_selection"
                ),
            })
        return candidates

    def scan(self, skill_name: str | None = None) -> dict[str, Any]:
        selected = self.scan_candidates()
        if skill_name:
            selected = [item for item in selected if item["name"] == skill_name]
            if not selected:
                raise PackagePolicyError(f"local skill not found: {skill_name}")
        return {
            "candidates": [
                {**item, "local_scan": _scan_summary(Path(item["path"]))}
                for item in selected
            ]
        }

    def suggest(
        self,
        skill_name: str | None = None,
        *,
        description: str | None = None,
        system_specification: dict[str, Any] | None = None,
        allow_private_secret_review: bool = False,
    ) -> dict[str, Any]:
        if not skill_name:
            return {"candidates": self.scan_candidates(), "network_submission": False}
        source = _find_skill_dir(skill_name)
        if source is None or source.resolve() not in {
            path.resolve() for path in self._eligible_paths()
        }:
            raise PackagePolicyError("skill is not eligible for Collective Wisdom")
        source_hash = _source_fingerprint(source)
        skill_id = self.store.register_skill(
            source, content_hash=source_hash, source_kind="local"
        )
        prepared = self.store.prepared_draft(skill_id, source_hash)
        if prepared is None:
            author_copy = description or draft_description(
                (source / "SKILL.md").read_text(encoding="utf-8")
            )
            local_package = prepare_package(
                source,
                overlay_root=self.store.root / "drafts",
                author_description=author_copy,
                owner=str(self.client.identity.get("owner")),
                installation_id=self.store.installation_identity(),
            )
            local_id = f"local:{skill_id}:{source_hash.removeprefix('sha256:')[:16]}"
            self.store.record_draft({
                "id": local_id,
                "skill_id": skill_id,
                "source_hash": source_hash,
                "overlay_path": str(local_package.overlay),
                "state": "prepared",
                "description": local_package.description,
                "content_hash": local_package.content_hash,
                "description_hash": local_package.description_hash,
                "manifest_hash": local_package.manifest_hash,
            })
            return {
                "network_submission": False,
                "local_draft_id": local_id,
                "overlay_path": str(local_package.overlay),
                "drafted_description": local_package.description,
                "system_specification": local_package.manifest.requirements.model_dump(
                    mode="json"
                ),
                "next_step": (
                    "Edit the author description and skill.manifest.json in the overlay, review every "
                    "file, then rerun `hermes wisdom suggest <skill> --description <approved-copy>`."
                ),
            }
        if description is None:
            manifest = PackageManifest.model_validate_json(
                (Path(prepared["overlay_path"]) / "skill.manifest.json").read_bytes()
            )
            return {
                "network_submission": False,
                "local_draft_id": prepared["id"],
                "overlay_path": prepared["overlay_path"],
                "drafted_description": prepared["description"],
                "system_specification": manifest.requirements.model_dump(mode="json"),
                "next_step": "Provide the owner-approved description to submit the edited overlay.",
            }
        if system_specification is None:
            raise PackagePolicyError(
                "submission requires explicit owner approval of the System Specification"
            )
        overlay = Path(prepared["overlay_path"])
        existing_manifest = PackageManifest.model_validate_json(
            (overlay / "skill.manifest.json").read_bytes()
        )
        approved_manifest = PackageManifest(
            schema_version=existing_manifest.schema_version,
            name=existing_manifest.name,
            requirements=SystemSpecification.model_validate(system_specification),
        )
        manifest_path = overlay / "skill.manifest.json"
        temporary_manifest = manifest_path.with_suffix(".json.pending")
        temporary_manifest.write_bytes(
            canonical_json_bytes(approved_manifest.model_dump(mode="json"))
        )
        temporary_manifest.chmod(0o600)
        os.replace(temporary_manifest, manifest_path)
        package = prepare_package(
            overlay,
            overlay_root=self.store.root / "submissions",
            author_description=description,
            owner=str(self.client.identity.get("owner")),
            installation_id=self.store.installation_identity(),
        )
        local_scan = _scan_summary(package.overlay)
        if local_scan["guard"]["allowed"] is False:
            raise PackagePolicyError(
                f"built-in guard blocked the exact staged package: {local_scan['guard']['reason']}"
            )
        if _has_high_confidence_secret(local_scan) and not allow_private_secret_review:
            raise PackagePolicyError(
                "high-confidence local secret finding paused upload; rerun with the explicit "
                "--send-for-owner-only-server-review confirmation"
            )
        if _source_fingerprint(source) != source_hash:
            raise PackagePolicyError(
                "source changed while the review overlay was being prepared"
            )
        self.client.upload_private_objects(package.objects)
        server = self.client.submit_draft(
            slug=_slug(skill_name),
            commit=package.commit,
            content_hash=package.content_hash,
            description=package.description,
        )
        self.store.set_draft_state(str(prepared["id"]), "submitted")
        self.store.record_draft({
            "id": server.id,
            "skill_id": skill_id,
            "source_hash": source_hash,
            "overlay_path": str(package.overlay),
            "draft_commit": server.draftCommit,
            "server_revision": server.updatedAt,
            "state": server.state,
            "description": server.authorDescription or "",
            "content_hash": server.contentHash,
            "description_hash": server.authorDescriptionHash
            or package.description_hash,
            "manifest_hash": server.packageManifestHash or package.manifest_hash,
        })
        return {
            "draft": server.model_dump(mode="json"),
            "local_scan": local_scan,
            "notice": "Draft bytes are owner-private; nothing is published until hash-bound approval.",
        }

    def review(
        self, draft_id: str, *, acknowledge: bool, portal: bool = False
    ) -> dict[str, Any]:
        reconstructed = self.client.reconstruct_draft(draft_id)
        draft = reconstructed.detail.draft
        manifest_body = next(
            body
            for path, _, body in reconstructed.files
            if path == "skill.manifest.json"
        )
        manifest_hash = sha256_address(manifest_body)
        if manifest_hash != draft.packageManifestHash:
            raise WisdomValidationError(
                "server draft package manifest hash does not match exact bytes"
            )
        description_hash = author_description_hash(draft.authorDescription or "")
        if description_hash != draft.authorDescriptionHash:
            raise WisdomValidationError(
                "server draft author description hash does not match displayed copy"
            )
        result = {
            "draft": draft.model_dump(mode="json"),
            "effective_policy": reconstructed.detail.effective_policy,
            "files": [
                {
                    "path": path,
                    "mode": mode,
                    "hash": sha256_address(body),
                    "content_utf8": body.decode("utf-8", errors="replace"),
                }
                for path, mode, body in reconstructed.files
            ],
            "hashes": {
                "content": reconstructed.content_hash,
                "author_description": description_hash,
                "package_manifest": manifest_hash,
            },
            "receipt": None,
        }
        if portal:
            url = f"{portal_base_url()}/wisdom/review/{draft_id}"
            webbrowser.open(url)
            result["portal_url"] = url
        if acknowledge:
            result["receipt"] = self.store.save_receipt(
                draft_id=draft_id,
                server_revision=draft.updatedAt,
                content_hash=reconstructed.content_hash,
                description_hash=description_hash,
                manifest_hash=manifest_hash,
            )
        return result

    def approve(self, draft_id: str) -> dict[str, Any]:
        receipt = self.store.receipt(draft_id)
        if not receipt:
            raise PackagePolicyError(
                "approval requires a fresh complete-package review receipt"
            )
        current = self.client.reconstruct_draft(draft_id)
        draft = current.detail.draft
        manifest_body = next(
            body for path, _, body in current.files if path == "skill.manifest.json"
        )
        expected = (
            current.content_hash,
            author_description_hash(draft.authorDescription or ""),
            sha256_address(manifest_body),
            draft.updatedAt,
        )
        received = (
            receipt["content_hash"],
            receipt["description_hash"],
            receipt["manifest_hash"],
            receipt["server_revision"],
        )
        if expected != received:
            raise PackagePolicyError(
                "review receipt is stale; review the complete server draft again"
            )
        approved = self.client.approve(
            draft_id,
            content_hash=receipt["content_hash"],
            description_hash=receipt["description_hash"],
            manifest_hash=receipt["manifest_hash"],
        )
        published = self.client.publish(draft_id, content_hash=receipt["content_hash"])
        self.store.consume_receipt(draft_id)
        return {"approved": approved.model_dump(mode="json"), "publication": published}

    def decline(self, draft_id: str) -> dict[str, Any]:
        result = self.client.decline(draft_id)
        local = self.store.draft(draft_id)
        if local:
            self.store.dismiss_candidate(
                str(local["skill_id"]), str(local["source_hash"])
            )
            self.store.set_draft_state(draft_id, "declined")
        self.store.consume_receipt(draft_id)
        return result

    def list_skills(self) -> dict[str, Any]:
        response = self.client.list_skills()
        return response.model_dump(mode="json")

    def show(self, skill_id: str) -> dict[str, Any]:
        return self.client.skill(skill_id).model_dump(mode="json")

    def versions(self, skill_id: str) -> list[dict[str, Any]]:
        return self.client.skill(skill_id).versions

    def _resolve_install_ref(self, reference: str) -> tuple[str, int | None]:
        parsed = urlparse(reference)
        raw = (
            parsed.path.rstrip("/").split("/")[-1]
            if parsed.scheme in {"http", "https"}
            else reference
        )
        if "@v" in raw:
            skill_id, raw_version = raw.rsplit("@v", 1)
            if not raw_version.isdigit() or int(raw_version) < 1:
                raise PackagePolicyError("invalid Wisdom version selector")
            return skill_id, int(raw_version)
        return raw, None

    def install_plan(
        self, reference: str, *, update_mode: str | None = None
    ) -> dict[str, Any]:
        skill_id, selected_version = self._resolve_install_ref(reference)
        detail = self.client.skill(skill_id)
        if detail.skill.get("state") != "active":
            raise PackagePolicyError("only active Wisdom skills can be installed")
        versions = detail.versions
        if not versions:
            raise PackagePolicyError("Wisdom skill has no published versions")
        version_number = selected_version or max(
            int(item["version"]) for item in versions
        )
        version_detail = self.client.version(skill_id, version_number)
        spec = PackageManifest.model_validate({
            "schema_version": 1,
            "name": str(detail.skill.get("slug") or skill_id),
            "requirements": version_detail.version.get("system_spec"),
        }).requirements
        compatibility = evaluate(spec, detect_local_capabilities())
        if compatibility.outcome == "blocked_pending_action":
            allowed = False
        else:
            allowed = True
        receipt = "wip_" + uuid.uuid4().hex
        plan = {
            "receipt": receipt,
            "skill_id": skill_id,
            "slug": str(detail.skill.get("slug") or skill_id),
            "version": version_number,
            "takedown_generation": int(detail.skill.get("takedown_generation", 0)),
            "update_mode": update_mode,
            "compatibility": asdict(compatibility),
            "allowed": allowed,
        }
        plan_dir = self.store.root / "plans"
        plan_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        plan_path = plan_dir / f"{receipt}.json"
        plan_path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
        plan_path.chmod(0o600)
        return plan

    def install_apply(
        self, receipt: str, *, accept_partial: bool = False
    ) -> dict[str, Any]:
        plan_path = self.store.root / "plans" / f"{receipt}.json"
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PackagePolicyError(
                "install plan receipt is missing or invalid"
            ) from exc
        outcome = plan["compatibility"]["outcome"]
        if not plan.get("allowed") or outcome == "blocked_pending_action":
            raise PackagePolicyError(
                "blocked compatibility requirements prevent activation"
            )
        if outcome in {"partial", "compatible_after_setup"} and not accept_partial:
            raise PackagePolicyError(
                "compatibility action is required; explicitly accept the plan"
            )
        response, files = self.client.content(plan["skill_id"], int(plan["version"]))
        exact_records, exact_hash = verify_content_files(files)
        if exact_hash != response.content_hash:
            raise WisdomValidationError("download changed after install planning")
        org_id = self.store.active_org_id()
        if not org_id:
            raise PackagePolicyError("run `hermes wisdom setup` before installing")
        managed_root = (get_skills_dir() / "_wisdom" / org_id).resolve()
        managed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = (managed_root / plan["slug"]).resolve()
        try:
            target.relative_to(managed_root)
        except ValueError as exc:
            raise PackagePolicyError(
                "managed install target escaped the Wisdom root"
            ) from exc
        operation = self.store.journal("install", plan["skill_id"], "downloaded", plan)
        staging = Path(tempfile.mkdtemp(prefix=f".{plan['slug']}-", dir=managed_root))
        try:
            for raw_path, mode, body in files:
                destination = staging.joinpath(*PurePosixPath(raw_path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(body)
                destination.chmod(0o600)
            local_scan = _scan_summary(staging)
            if local_scan["guard"]["allowed"] is False:
                raise PackagePolicyError(
                    f"built-in guard blocked installation: {local_scan['guard']['reason']}"
                )
            self.store.advance(operation, "staged")
            backup = managed_root / f".{plan['slug']}.previous"
            if backup.exists():
                shutil.rmtree(backup)
            if target.exists():
                os.replace(target, backup)
            os.replace(staging, target)
            self.store.advance(operation, "files_committed")
            baseline = {record.path: record.hash for record in exact_records}
            self.store.record_install({
                "skill_id": plan["skill_id"],
                "org_id": org_id,
                "slug": plan["slug"],
                "version": plan["version"],
                "content_hash": exact_hash,
                "baseline": baseline,
                "target_path": str(target),
                "update_mode": plan.get("update_mode") or "MANUAL",
            })
            self.store.advance(operation, "local_ledger_committed")
            installation_id = self.store.installation_identity()
            server = self.client.record_install(
                skill_id=plan["skill_id"],
                installation_id=installation_id,
                version=int(plan["version"]),
                takedown_generation=int(plan["takedown_generation"]),
                update_mode=plan.get("update_mode"),
            )
            self.store.advance(operation, "gateway_recorded", done=True)
            plan_path.unlink(missing_ok=True)
            if backup.exists():
                shutil.rmtree(backup)
            return {
                "installed": True,
                "skill_id": plan["skill_id"],
                "version": plan["version"],
                "path": str(target),
                "content_hash": exact_hash,
                "effective_update_mode": server.effective_update_mode,
                "compatibility": plan["compatibility"],
                "local_scan": local_scan,
            }
        finally:
            if staging.exists():
                shutil.rmtree(staging)
