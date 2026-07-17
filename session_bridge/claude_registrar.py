"""Single-claim interactive ConPTY registrar for native Claude visibility."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .claude_adapter import ClaudeParseResult, ClaudeReadableSource
from .claude_visibility import (
    ClaudeVisibilityCandidate,
    ClaudeVisibilityClaim,
    ClaudeVisibilityIdentity,
    build_claude_registration_prompt,
    derive_claude_visibility_identity,
    validate_claude_visibility_identity_binding,
)
from .models import OriginKind, Provider, SessionProjection


_MAX_RESPONSE_CHARS = 65_536
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


class InteractivePty(Protocol):
    def read_until(self, timeout: float, *, prompt: str | None = None) -> str: ...
    def write(self, data: str) -> None: ...
    def wait(self, timeout: float) -> int: ...
    def terminate(self) -> None: ...
    def close(self) -> None: ...


class InteractivePtyFactory(Protocol):
    def spawn(self, argv: list[str], *, cwd: str) -> InteractivePty: ...


class ClaudeVisibilityStore(Protocol):
    def commit_claude_visibility_job(
        self, job_id: str, lease_digest: str, transcript_digest: str,
        visible_at: float,
    ) -> dict[str, object]: ...
    def retry_claude_visibility_job(
        self, job_id: str, lease_digest: str, error_code: str,
        next_attempt_at: float, detail: str,
    ) -> dict[str, object]: ...
    def fail_claude_visibility_job(
        self, job_id: str, lease_digest: str, error_code: str, detail: str,
    ) -> dict[str, object]: ...
    def record_claude_visibility_exact_id_absent(
        self, job_id: str, lease_digest: str, reserved_claude_uuid: str,
        attempt_ordinal: int, evidence_digest: str,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class ClaudeRegistrarOutcome:
    status: str
    job_id: str | None
    reserved_claude_uuid: str | None
    error_code: str | None = None
    detail: str = ""


class _TranscriptConflict(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WindowsConPtyFactory:
    """Production pywinpty factory; imports remain safe off Windows."""

    def spawn(self, argv: list[str], *, cwd: str) -> InteractivePty:
        if not sys.platform.startswith("win"):
            raise RuntimeError("pty unavailable")
        try:
            from winpty import PtyProcess
        except ImportError as exc:
            raise RuntimeError("pty unavailable") from exc
        try:
            process = PtyProcess.spawn(
                list(argv), cwd=cwd, env=os.environ.copy(), dimensions=(24, 120)
            )
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise RuntimeError("pty unavailable") from exc
        return _WinPtyProcess(process)


class _WinPtyProcess:
    def __init__(self, process: object) -> None:
        self._process = process
        self._closed = False

    def read_until(self, timeout: float, *, prompt: str | None = None) -> str:
        result: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

        def _read() -> None:
            chunks: list[str] = []
            try:
                while True:
                    chunk = self._process.read(4096)  # type: ignore[attr-defined]
                    if not chunk:
                        continue
                    text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
                    chunks.append(text)
                    joined = "".join(chunks)
                    exact_response = _exact_registered_suffix(joined)
                    if len(joined) > _MAX_RESPONSE_CHARS:
                        result.put(joined)
                        return
                    if exact_response is not None:
                        result.put(exact_response)
                        return
            except EOFError:
                result.put("".join(chunks))
            except BaseException as exc:
                result.put(exc)

        threading.Thread(target=_read, daemon=True).start()
        try:
            value = result.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if isinstance(value, BaseException):
            raise RuntimeError("PTY read unavailable") from value
        return value

    def write(self, data: str) -> None:
        self._process.write(data)  # type: ignore[attr-defined]

    def wait(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._process.isalive():  # type: ignore[attr-defined]
                return int(getattr(self._process, "exitstatus", 0) or 0)
            time.sleep(0.01)
        raise TimeoutError

    def terminate(self) -> None:
        try:
            self._process.terminate(force=True)  # type: ignore[attr-defined]
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._process.close()  # type: ignore[attr-defined]
        except Exception:
            pass


class ClaudeNativeRegistrar:
    """Processes exactly one already-leased Claude visibility claim."""

    def __init__(
        self,
        store: ClaudeVisibilityStore,
        source_adapter: ClaudeReadableSource,
        *,
        marker_secret: bytes,
        pty_factory: InteractivePtyFactory | None = None,
        claude_command: Sequence[str] = ("claude",),
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        process_timeout: float = 120.0,
        exit_timeout: float = 5.0,
        discovery_timeout: float = 15.0,
        retry_delay: float = 30.0,
        poll_interval: float = 0.1,
    ) -> None:
        self._store = store
        self._source = source_adapter
        self._secret = marker_secret
        self._factory = pty_factory or WindowsConPtyFactory()
        self._command = list(claude_command)
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._process_timeout = process_timeout
        self._exit_timeout = exit_timeout
        self._discovery_timeout = discovery_timeout
        self._retry_delay = retry_delay
        self._poll_interval = poll_interval

    def process(self, claim: ClaudeVisibilityClaim) -> ClaudeRegistrarOutcome:
        if not claim.claimed:
            return ClaudeRegistrarOutcome(claim.status, claim.job_id, claim.reserved_claude_uuid)
        try:
            candidate, identity = self._materialize_claim(claim)
        except ValueError:
            return self._fail(claim, "bridge_conflict", "claim authority conflict")

        if claim.lease_kind == "reconciliation":
            return self._reconcile(claim, candidate, identity)
        return self._launch(claim, candidate, identity)

    def _materialize_claim(
        self, claim: ClaudeVisibilityClaim
    ) -> tuple[ClaudeVisibilityCandidate, ClaudeVisibilityIdentity]:
        launch = claim.lease_kind == "launch"
        reconciliation = claim.lease_kind == "reconciliation"
        if not (launch or reconciliation):
            raise ValueError("missing lease authority")
        if launch != bool(claim.launch_permitted and claim.registration_reserved):
            raise ValueError("inconsistent launch authority")
        if reconciliation != bool(
            claim.requires_exact_id_reconciliation
            and not claim.launch_permitted
            and not claim.registration_reserved
        ):
            raise ValueError("inconsistent reconciliation authority")
        required_text = (
            claim.job_id,
            claim.source_session_id,
            claim.reserved_claude_uuid,
            claim.native_name,
            claim.source_cwd,
            claim.signed_marker,
            claim.lease_digest,
        )
        if any(not isinstance(value, str) or not value for value in required_text):
            raise ValueError("incomplete claim")
        if (
            not isinstance(claim.attempt_ordinal, int)
            or isinstance(claim.attempt_ordinal, bool)
            or claim.attempt_ordinal < 0
        ):
            raise ValueError("invalid attempt ordinal")
        if claim.source_provider not in (Provider.CODEX, Provider.HERMES):
            raise ValueError("invalid provider")
        assert claim.job_id is not None
        assert claim.source_session_id is not None
        assert claim.reserved_claude_uuid is not None
        assert claim.native_name is not None
        assert claim.source_cwd is not None
        assert claim.signed_marker is not None
        candidate = ClaudeVisibilityCandidate(
            source_session_id=claim.source_session_id,
            source_provider=claim.source_provider,
            native_name=claim.native_name,
            source_cwd=claim.source_cwd,
            git_root=claim.git_root,
            git_branch=claim.git_branch,
            git_head=claim.git_head,
            worktree_id=claim.worktree_id,
            eligible_at=0.0,
        )
        derived = derive_claude_visibility_identity(candidate, self._secret)
        identity = ClaudeVisibilityIdentity(
            job_id=claim.job_id,
            bridge_id=derived.bridge_id,
            idempotency_key=derived.idempotency_key,
            claude_uuid=claim.reserved_claude_uuid,
            signed_marker=claim.signed_marker,
        )
        validate_claude_visibility_identity_binding(candidate, identity, self._secret)
        return candidate, identity

    def _reconcile(
        self, claim: ClaudeVisibilityClaim, candidate: ClaudeVisibilityCandidate,
        identity: ClaudeVisibilityIdentity,
    ) -> ClaudeRegistrarOutcome:
        try:
            found = self._read_exact(identity.claude_uuid)
        except ValueError:
            return self._fail(claim, "uuid_conflict", "exact transcript identity conflict")
        except (OSError, RuntimeError):
            return self._retry(claim, "native_transcript_not_indexed", "exact transcript lookup unavailable")
        if found is None:
            evidence = hashlib.sha256(
                f"absent:{identity.claude_uuid}:{claim.attempt_ordinal}".encode()
            ).hexdigest()
            try:
                self._store.record_claude_visibility_exact_id_absent(
                    claim.job_id or "", claim.lease_digest or "", identity.claude_uuid,
                    claim.attempt_ordinal or 0, evidence,
                )
            except Exception:
                return ClaudeRegistrarOutcome("retry", claim.job_id, identity.claude_uuid,
                                               "session_bridge_unavailable", "store transition unavailable")
            return ClaudeRegistrarOutcome("absent", claim.job_id, identity.claude_uuid)
        return self._validate_and_commit(claim, candidate, identity, found)

    def _launch(
        self, claim: ClaudeVisibilityClaim, candidate: ClaudeVisibilityCandidate,
        identity: ClaudeVisibilityIdentity,
    ) -> ClaudeRegistrarOutcome:
        try:
            existing = self._read_exact(identity.claude_uuid)
        except ValueError:
            return self._retry(
                claim, "creation_ambiguous", "exact transcript requires reconciliation"
            )
        except (OSError, RuntimeError):
            return self._retry(
                claim, "native_transcript_not_indexed", "exact transcript lookup unavailable"
            )
        if existing is not None:
            return self._validate_and_commit(claim, candidate, identity, existing)

        argv = [*self._command, "--session-id", identity.claude_uuid,
                "--name", candidate.native_name, "--model", "haiku", "--tools", "",
                "--permission-mode", "dontAsk"]
        process: InteractivePty | None = None
        launched = False
        clean_exit = False
        try:
            process = self._factory.spawn(argv, cwd=candidate.source_cwd)
            launched = True
            prompt = build_claude_registration_prompt(candidate, identity, self._secret)
            process.write(f"\x1b[200~{prompt}\x1b[201~\r")
            output = process.read_until(self._process_timeout, prompt=prompt)
            if _is_authentication_failure(output):
                return self._retry(claim, "claude_authentication_unavailable", "Claude authentication unavailable")
            if not _has_exact_registered_response(output, prompt):
                return self._fail(claim, "bridge_conflict", "registration response malformed")
            process.write("/exit\r")
            if process.wait(self._exit_timeout) != 0:
                return self._retry(claim, "clean_exit_not_observed", "Claude did not exit cleanly")
            clean_exit = True
        except FileNotFoundError:
            return self._retry(claim, "claude_executable_unavailable", "Claude executable unavailable")
        except TimeoutError:
            return self._retry(claim, "creation_ambiguous", "registration result ambiguous")
        except RuntimeError:
            code = "creation_ambiguous" if launched else "pty_unavailable"
            return self._retry(claim, code, "interactive PTY unavailable")
        except Exception:
            code = "creation_ambiguous" if launched else "pty_unavailable"
            return self._retry(claim, code, "interactive registration unavailable")
        finally:
            if process is not None:
                if not clean_exit:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                try:
                    process.close()
                except Exception:
                    pass

        deadline = self._monotonic() + self._discovery_timeout
        while True:
            try:
                found = self._read_exact(identity.claude_uuid)
            except (OSError, RuntimeError, ValueError):
                found = None
            if found is not None:
                return self._validate_and_commit(claim, candidate, identity, found)
            if self._monotonic() >= deadline:
                return self._retry(claim, "native_transcript_not_indexed", "native transcript not indexed")
            self._sleep(self._poll_interval)

    def _read_exact(self, native_id: str) -> SessionProjection | None:
        path = self._source.find_native_session(native_id)
        if path is None:
            return None
        parsed: ClaudeParseResult = self._source.parse(Path(path))
        return parsed.projection

    def _validate_and_commit(
        self, claim: ClaudeVisibilityClaim, candidate: ClaudeVisibilityCandidate,
        identity: ClaudeVisibilityIdentity, projection: SessionProjection,
    ) -> ClaudeRegistrarOutcome:
        try:
            _validate_projection(projection, candidate, identity, self._secret)
        except _TranscriptConflict as exc:
            return self._fail(claim, exc.code, "exact transcript conflict")
        digest = projection.native_hash
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            digest = hashlib.sha256(json.dumps({
                "native_id": projection.native_id, "native_path": projection.native_path,
                "last_active": projection.last_active,
            }, sort_keys=True).encode()).hexdigest()
        try:
            self._store.commit_claude_visibility_job(
                claim.job_id or "", claim.lease_digest or "", digest, self._clock()
            )
        except Exception:
            return ClaudeRegistrarOutcome("retry", claim.job_id, identity.claude_uuid,
                                           "session_bridge_unavailable", "store transition unavailable")
        return ClaudeRegistrarOutcome("visible", claim.job_id, identity.claude_uuid)

    def _retry(self, claim: ClaudeVisibilityClaim, code: str, detail: str) -> ClaudeRegistrarOutcome:
        try:
            self._store.retry_claude_visibility_job(
                claim.job_id or "", claim.lease_digest or "", code,
                self._clock() + self._retry_delay, detail,
            )
        except Exception:
            code, detail = "session_bridge_unavailable", "store transition unavailable"
        return ClaudeRegistrarOutcome("retry", claim.job_id, claim.reserved_claude_uuid, code, detail)

    def _fail(self, claim: ClaudeVisibilityClaim, code: str, detail: str) -> ClaudeRegistrarOutcome:
        try:
            self._store.fail_claude_visibility_job(
                claim.job_id or "", claim.lease_digest or "", code, detail
            )
        except Exception:
            code, detail = "session_bridge_unavailable", "store transition unavailable"
        return ClaudeRegistrarOutcome("failed", claim.job_id, claim.reserved_claude_uuid, code, detail)


def _validate_projection(
    projection: SessionProjection, candidate: ClaudeVisibilityCandidate,
    identity: ClaudeVisibilityIdentity, marker_secret: bytes,
) -> None:
    if projection.provider is not Provider.CLAUDE or projection.native_id != identity.claude_uuid:
        raise _TranscriptConflict("uuid_conflict")
    if projection.cwd != candidate.source_cwd:
        raise _TranscriptConflict("cwd_conflict")
    if projection.title != candidate.native_name:
        raise _TranscriptConflict("name_conflict")
    if projection.origin_bridge_id != identity.bridge_id or projection.origin_kind is not OriginKind.BRIDGE_PLACEHOLDER:
        raise _TranscriptConflict("bridge_conflict")
    expected = build_claude_registration_prompt(candidate, identity, marker_secret)
    user_contents = [m.content for m in projection.messages if m.role == "user"]
    assistant_contents = [m.content for m in projection.messages if m.role == "assistant"]
    if expected not in user_contents:
        raise _TranscriptConflict("marker_conflict")
    if "REGISTERED" not in assistant_contents:
        raise _TranscriptConflict("bridge_conflict")


def _is_authentication_failure(output: str) -> bool:
    folded = output.casefold()
    return "authentication required" in folded or "not authenticated" in folded or "please log in" in folded


def _has_exact_registered_response(output: str, prompt: str) -> bool:
    if not isinstance(output, str) or len(output) > _MAX_RESPONSE_CHARS:
        return False
    cleaned = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", output)).replace("\r", "")
    prompt_lines = {line.strip() for line in prompt.splitlines()}
    meaningful: list[str] = []
    for raw in cleaned.splitlines():
        line = raw.strip()
        if not line or line in prompt_lines:
            continue
        for prefix in ("Claude>", ">"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if line in prompt_lines:
            continue
        if line:
            meaningful.append(line)
    return meaningful == ["REGISTERED"]


def _exact_registered_suffix(output: str) -> str | None:
    if not output.endswith(("\r", "\n")):
        return None
    cleaned = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", output)).replace("\r", "")
    lines = cleaned.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        for prefix in ("Claude>", ">"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if line == "REGISTERED":
            suffix = ["REGISTERED"]
            suffix.extend(
                remainder.strip()
                for remainder in lines[index + 1:]
                if remainder.strip()
            )
            return "\n".join(suffix) + "\n"
    return None


__all__ = [
    "ClaudeNativeRegistrar", "ClaudeRegistrarOutcome", "InteractivePty",
    "InteractivePtyFactory", "WindowsConPtyFactory",
]
