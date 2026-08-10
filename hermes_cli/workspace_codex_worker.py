"""Audited, optional standalone Codex CLI worker for workspace runners."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AUDITED_CODEX_VERSION = "0.146.1"
AUDITED_CODEX_PACKAGE_INTEGRITY = (
    "sha512-f51R56E/G15soLhf5l5pWUiM+mGHK0NdLozOtzjRoAa+bA20hgWrkyxE/"
    "fpwCnuGQM6XNdktHYtK9xQ7bPIbTA=="
)
AUDITED_CODEX_DARWIN_ARM64_INTEGRITY = (
    "sha512-zvXxiGRuKCOoFQyh7lRC2SHwomM2VNzHralyeP+i6fxLjQ5grpmnfJpnLs54+"
    "GkK7K5nEpkj7chqjlKbMELgZw=="
)
AUDITED_CODEX_DARWIN_ARM64_BINARY_SHA256 = (
    "35d248101b211d6248ad4e6b8c1d441fe81236da87afb9f3e9ea51a049e9f179"
)
AUDITED_CODEX_WRAPPER_SHA256 = (
    "134063e133f0b4244fa3b251acf973d4fe4b4aeeacbdc135211bf480f59f1477"
)
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_VERSION_RE = re.compile(r"(?:codex-cli\s+)?(?P<version>\d+\.\d+\.\d+)\s*$")


@dataclass(frozen=True)
class CodexAuditManifest:
    version: str
    package_integrity: str
    platform_integrity: str
    artifact_sha256: frozenset[str]


DEFAULT_CODEX_AUDIT_MANIFEST = CodexAuditManifest(
    version=AUDITED_CODEX_VERSION,
    package_integrity=AUDITED_CODEX_PACKAGE_INTEGRITY,
    platform_integrity=AUDITED_CODEX_DARWIN_ARM64_INTEGRITY,
    artifact_sha256=frozenset(
        {
            AUDITED_CODEX_DARWIN_ARM64_BINARY_SHA256,
            AUDITED_CODEX_WRAPPER_SHA256,
        }
    ),
)


@dataclass(frozen=True)
class _Artifact:
    path: Path
    role: str
    sha256: str


@dataclass(frozen=True)
class _Policy:
    artifacts: tuple[_Artifact, ...]
    codex_home: Path
    package_integrity: str
    platform_integrity: str
    version: str


class CodexWorker:
    """Run a pre-audited Codex binary without granting Git push credentials."""

    def __init__(
        self,
        *,
        policy: _Policy,
        manifest: CodexAuditManifest,
        state_dir: Path,
    ) -> None:
        self.policy = policy
        self.manifest = manifest
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.state_dir.chmod(0o700)
        self._verify_policy()

    @classmethod
    def from_policy(
        cls,
        path: str | Path,
        *,
        manifest: CodexAuditManifest = DEFAULT_CODEX_AUDIT_MANIFEST,
        state_dir: str | Path,
    ) -> "CodexWorker":
        policy_path = Path(path).expanduser().resolve(strict=True)
        if os.name != "nt" and stat.S_IMODE(policy_path.stat().st_mode) & 0o077:
            raise ValueError("Codex worker policy must be mode 0600")
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
        allowed = {
            "artifacts",
            "codex_home",
            "package_integrity",
            "platform_integrity",
            "version",
        }
        if not isinstance(raw, dict) or set(raw) != allowed:
            raise ValueError("Codex worker policy fields are invalid")
        raw_artifacts = raw["artifacts"]
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise ValueError("Codex worker artifacts are required")
        artifacts: list[_Artifact] = []
        for item in raw_artifacts:
            if not isinstance(item, dict) or set(item) != {"path", "role", "sha256"}:
                raise ValueError("Codex worker artifact fields are invalid")
            digest = str(item["sha256"]).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Codex worker artifact digest is invalid")
            role = str(item["role"])
            if role not in {"executable", "supporting"}:
                raise ValueError("Codex worker artifact role is invalid")
            artifacts.append(
                _Artifact(
                    path=Path(str(item["path"])).expanduser().resolve(strict=True),
                    role=role,
                    sha256=digest,
                )
            )
        policy = _Policy(
            artifacts=tuple(artifacts),
            codex_home=Path(str(raw["codex_home"])).expanduser().resolve(),
            package_integrity=str(raw["package_integrity"]),
            platform_integrity=str(raw["platform_integrity"]),
            version=str(raw["version"]),
        )
        return cls(policy=policy, manifest=manifest, state_dir=Path(state_dir))

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _verify_policy(self) -> Path:
        if self.policy.version != self.manifest.version:
            raise ValueError("Codex worker version is not audited")
        if self.policy.package_integrity != self.manifest.package_integrity:
            raise ValueError("Codex worker package integrity is not audited")
        if self.policy.platform_integrity != self.manifest.platform_integrity:
            raise ValueError("Codex worker platform integrity is not audited")
        executable: Path | None = None
        observed: set[str] = set()
        for artifact in self.policy.artifacts:
            if not artifact.path.is_file():
                raise ValueError("Codex worker artifact is missing")
            if os.name != "nt" and stat.S_IMODE(artifact.path.stat().st_mode) & 0o022:
                raise ValueError("Codex worker artifact must not be group/world writable")
            digest = self._digest(artifact.path)
            if digest != artifact.sha256:
                raise ValueError("Codex worker artifact digest changed")
            if digest not in self.manifest.artifact_sha256:
                raise ValueError("Codex worker artifact digest is not audited")
            observed.add(digest)
            if artifact.role == "executable":
                if executable is not None:
                    raise ValueError("Codex worker policy has multiple executables")
                executable = artifact.path
        if executable is None:
            raise ValueError("Codex worker executable is required")
        if observed != set(self.manifest.artifact_sha256):
            raise ValueError("Codex worker policy does not cover every audited artifact")
        return executable

    def _environment(self) -> dict[str, str]:
        home = self.state_dir / "home"
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            home.chmod(0o700)
        environment = {
            "CODEX_HOME": str(self.policy.codex_home),
            "GIT_ASKPASS": "/usr/bin/false",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(home),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        }
        for key in ("LANG", "LC_ALL", "TERM", "TMPDIR"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        return environment

    @staticmethod
    def _git_head(workdir: Path, environment: dict[str, str]) -> str:
        completed = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            raise ValueError("Codex worker requires a Git worktree")
        return completed.stdout.strip()

    def _version(self, executable: Path, environment: dict[str, str]) -> str:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=10,
        )
        match = _VERSION_RE.search(completed.stdout.strip())
        if completed.returncode != 0 or match is None:
            raise ValueError("Codex worker version probe failed")
        version = match.group("version")
        if version != self.policy.version:
            raise ValueError("Codex worker version changed")
        return version

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)

    @staticmethod
    def _last_message(events: list[dict[str, Any]]) -> str | None:
        for event in reversed(events):
            item = event.get("item")
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
            if isinstance(event.get("message"), str):
                return event["message"]
        return None

    def run(
        self,
        *,
        prompt: str,
        workdir: str | Path,
        timeout_seconds: float = 1800,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("Codex worker prompt is required")
        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("Codex worker timeout is invalid")
        root = Path(workdir).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Codex worker directory is invalid")
        executable = self._verify_policy()
        environment = self._environment()
        version = self._version(executable, environment)
        before_head = self._git_head(root, environment)
        guarded_prompt = (
            "Operate only inside the current worktree. Do not commit, push, fetch, change remotes, "
            "read credentials, or bypass the sandbox. Leave all changes uncommitted for review.\n\n"
            f"Task:\n{prompt}"
        )
        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                [
                    str(executable),
                    "exec",
                    "--json",
                    "--sandbox",
                    "workspace-write",
                    "-",
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=os.name != "nt",
            )
            assert process.stdin is not None
            process.stdin.write(guarded_prompt.encode("utf-8"))
            process.stdin.close()
            deadline = started + timeout_seconds
            output_exceeded = False
            timed_out = False
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._stop_process(process)
                    break
                if stdout_file.tell() + stderr_file.tell() > _MAX_OUTPUT_BYTES:
                    output_exceeded = True
                    self._stop_process(process)
                    break
                time.sleep(0.05)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(_MAX_OUTPUT_BYTES).decode("utf-8", errors="replace")
            stderr = stderr_file.read(_MAX_OUTPUT_BYTES).decode("utf-8", errors="replace")
        self._verify_policy()
        after_head = self._git_head(root, environment)
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        head_changed = before_head != after_head
        ok = (
            process.returncode == 0
            and not timed_out
            and not output_exceeded
            and not head_changed
        )
        error: str | None = None
        if timed_out:
            error = "Codex worker timed out"
        elif output_exceeded:
            error = "Codex worker output limit exceeded"
        elif head_changed:
            error = "Codex worker changed Git HEAD and was quarantined"
        elif process.returncode != 0:
            error = (stderr.strip() or "Codex worker failed")[-2000:]
        return {
            "artifact_sha256": self._digest(executable),
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": error,
            "event_count": len(events),
            "last_message": self._last_message(events),
            "ok": ok,
            "quarantined": head_changed,
            "version": version,
        }
