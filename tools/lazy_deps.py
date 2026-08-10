"""
Lazy dependency installer for opt-in Hermes Agent backends.

Many Hermes features (Mistral TTS, ElevenLabs TTS, Honcho memory, Bedrock,
Slack, Matrix, etc.) require Python packages that not every user needs. The
historical approach was to bundle them all under ``pyproject.toml`` extras
(``hermes-agent[all]``) and install them eagerly at setup time. That has
two problems:

1. **Fragility.** When one extra's transitive dependency becomes
   unavailable on PyPI (quarantined for malware, yanked, broken upload),
   the *entire* ``[all]`` resolve fails and fresh installs silently fall
   back to a stripped tier — losing 10+ unrelated extras at once.

2. **Bloat.** A user who only ever talks to one provider pulls hundreds
   of packages they will never import.

The lazy-install pattern fixes both. Backends call :func:`ensure` at the
top of their first-import path. If the deps are missing, ``ensure`` checks
the ``security.allow_lazy_installs`` config flag (default true) and runs
a venv-scoped pip install. If the user has explicitly disabled lazy
installs, ``ensure`` raises :class:`FeatureUnavailable` with a clear
remediation hint pointing at ``hermes tools`` or the manual pip command.

Security model:

* **Venv-scoped by default.** Installs target ``sys.executable`` in the
  active venv. We never touch the system Python.
* **Durable-target mode (immutable images).** When the deployment seals the
  agent's own venv (the Docker image sets ``HERMES_DISABLE_LAZY_INSTALLS=1``
  and makes ``/opt/hermes`` read-only), setting
  ``HERMES_LAZY_INSTALL_TARGET`` redirects lazy installs to a writable
  directory on the durable data volume (e.g. ``/opt/data/lazy-packages``).
  That directory is **appended to the end of ``sys.path``** — never
  prepended, never exported via ``PYTHONPATH`` — so the agent's own
  site-packages wins every name collision. A package installed this way can
  only ADD new importable modules; it can never shadow, downgrade, or break
  a module the core already ships. The worst a bad/incompatible backend
  package can do is fail to import and report itself unavailable — the agent
  core stays healthy. This is the structural guarantee that a lazily
  installed package cannot brick Hermes, which is what made it safe to seal
  the venv in the first place. Compiled-wheel safety across image rebuilds
  is handled by an ABI/Python-version stamp on the target subdir (see
  :func:`_ensure_target_ready`).
* **PyPI by package name only.** Specs may be ``"package>=1.0,<2"`` etc.
  We do NOT support ``--index-url`` overrides, ``git+https://``, file:
  paths, or any other input that could be hijacked by a malicious config.
* **Allowlist.** Only specs that appear in :data:`LAZY_DEPS` can be
  installed via this path. A typo in feature name doesn't get the user
  install-anything semantics.
* **Opt-out.** Setting ``security.allow_lazy_installs: false`` in
  ``config.yaml`` disables runtime installs in BOTH modes. Users in
  restricted networks or strict security postures can pin themselves to
  whatever was installed at setup time.
* **Offline detection.** If the install fails (offline, mirror down,
  PyPI 404 / quarantine), we surface the failure as
  :class:`FeatureUnavailable` with the actual pip stderr — no silent
  retries, no caching of bad state.

Adding a new backend:

1. Add an entry to :data:`LAZY_DEPS` with the package specs.
2. At the top of the backend module's import path, call
   ``ensure("feature.name")`` inside a try/except that converts
   :class:`FeatureUnavailable` to a useful runtime error.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli._subprocess_compat import windows_hide_flags
from hermes_cli import _pip_security

logger = logging.getLogger(__name__)

# pip is only used as a fallback when uv is unavailable.  Keep that
# venv-managed path on the security floor from issue #41374 instead of
# trusting whatever version ensurepip happened to bundle with Python.
_MIN_PIP_VERSION = _pip_security.MIN_PIP_VERSION
_MIN_PIP_SPEC = _pip_security.MIN_PIP_SPEC
_PYNACL_SECURITY_SPEC = "PyNaCl==1.6.2"
# A stale core PyNaCl installation must not constrain the ordinary durable
# transaction.  The target is append-only, so the install path separately
# rejects stale core copies of every explicitly managed package rather than
# allowing a target copy to be reported as active when core wins on sys.path.
_CONSTRAINT_EXCLUDED_PACKAGES = frozenset({"pynacl"})
# These packages carry security-sensitive exact/floor contracts in
# ``LAZY_DEPS`` or the published extras.  Their metadata is evidence for a
# repair decision, so a missing ``packaging`` module or an unstable/malformed
# version must fail closed instead of falling back to presence-only behavior.
_SECURITY_VERSIONED_PACKAGES = frozenset(
    {
        "aiohttp",
        "anthropic",
        "cryptography",
        "httplib2",
        "idna",
        "pynacl",
        "pyasn1",
        "python-multipart",
        "requests",
        "starlette",
        "urllib3",
    }
)

# Manifest-driven installs do not go through the static ``LAZY_DEPS`` map, so
# keep the small set of dependency floors that must hold for every runtime
# install in one explicit policy table.  A manifest may still choose a newer
# version or a compatible range, but it must prove that every candidate is at
# or above the floor before it reaches pip.
_SECURITY_INSTALL_FLOORS = {
    "pynacl": "1.6.2",
    "aiohttp": "3.14.3",
    "idna": "3.15",
    "pip": "26.1.2",
}

# Core metadata is normally produced by trusted build tooling, but the
# durable-target constraint file is later parsed by pip as an argument-like
# requirements file.  Keep the interpolation boundary deliberately narrower
# than Core Metadata: only canonical package names and stable release tokens
# may cross it.  In particular, a metadata name beginning with ``--`` must
# never become a pip option such as ``--index-url``.
_CONSTRAINT_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_CONSTRAINT_VERSION_RE = re.compile(r"\d+(?:\.\d+)*(?:\.post\d+)?\Z")


# =============================================================================
# Allowlist of lazy-installable backends.
#
# Keys are dot-separated feature names ("namespace.backend"). Values are
# tuples of pip-installable specs that match the corresponding extra in
# pyproject.toml. The framework enforces that only specs from this map
# can flow into the pip install command.
# =============================================================================


LAZY_DEPS: dict[str, tuple[str, ...]] = {
    # ─── Inference providers ───────────────────────────────────────────────
    # Native Anthropic SDK — needed when provider=anthropic (not via
    # OpenRouter / aggregators which use the openai SDK).
    "provider.anthropic": ("anthropic==0.87.0",),  # CVE-2026-34450, CVE-2026-34452
    # AWS Bedrock provider
    "provider.bedrock": ("boto3==1.42.89",),
    # Google Vertex AI provider — OAuth2 token minting for the Gemini
    # OpenAI-compatible endpoint. Only loaded when provider=vertex is selected;
    # google-auth is NOT in [all] so plain installs don't carry it.
    "provider.vertex": (
        "google-auth==2.55.1",
        "pyasn1==0.6.4",
    ),
    # Microsoft Foundry — Entra ID auth (managed identity, workload identity,
    # service principal, az login, VS Code, azd, PowerShell). Only loaded
    # when model.auth_mode=entra_id is selected; key-based azure-foundry
    # users never pay this import.
    "provider.azure_identity": ("azure-identity==1.25.3",),

    # ─── Web search backends ───────────────────────────────────────────────
    "search.exa": ("exa-py==2.10.2",),
    "search.firecrawl": ("firecrawl-py==4.17.0", "aiohttp==3.14.3"),
    "search.parallel": ("parallel-web==0.4.2",),

    # ─── Monitoring ─────────────────────────────────────────────────────────
    # OTLP gateway monitoring export. Lazily installed on first use of
    # monitoring.gateway_health_export / monitoring.export.otlp. Tracks the
    # `otlp` extra in pyproject.toml — bump both together.
    "export.otlp": (
        "opentelemetry-sdk==1.39.1",
        "opentelemetry-exporter-otlp-proto-http==1.39.1",
    ),

    # ─── TTS providers ─────────────────────────────────────────────────────
    # Pinned to exact versions to match pyproject.toml's no-ranges policy
    # (see comment at top of [project.dependencies]). When bumping, update
    # both this map AND the corresponding extra in pyproject.toml.
    #
    # mistralai pin tracks the `mistral` extra in pyproject.toml. PyPI
    # quarantined the project 2026-05-12 (malicious 2.4.6, Mini Shai-Hulud);
    # 2.4.6 was removed and clean releases resumed (2.4.7, 2.4.8). Voxtral
    # STT + TTS share the same SDK.
    "tts.mistral": ("mistralai==2.4.8",),
    "tts.edge": ("edge-tts==7.2.7", "aiohttp==3.14.3"),
    "tts.elevenlabs": ("elevenlabs==1.59.0",),

    # ─── Speech-to-text providers ──────────────────────────────────────────
    "stt.mistral": ("mistralai==2.4.8",),
    "stt.faster_whisper": (
        "faster-whisper==1.2.1",
        "sounddevice==0.5.5",
        "numpy==2.4.3",
    ),
    # SILK voice-note decoding (WeChat/QQ .silk voice messages). pilk is a
    # small silk-v3 codec binding; installed on first .silk transcription.
    "stt.silk": ("pilk==0.2.4",),

    # ─── Wake word ("Hey Hermes") engines ──────────────────────────────────
    # Keep in sync with the `wake` extra in pyproject.toml. openWakeWord is the
    # free, local default (ONNX runtime); Porcupine is the premium engine.
    # openWakeWord's ONNX embedding model returns near-zero scores on macOS
    # ARM64 (dscripka/openWakeWord#336), so the wake word runs on the tflite
    # backend there. Upstream declares tflite-runtime for Linux only;
    # ai-edge-litert is the macOS equivalent, bridged in tools/wake_word.py.
    # It lives in its own feature because lazy-dep specs cannot carry PEP 508
    # environment markers (_spec_is_safe rejects ";"), so the platform gate is
    # applied by the caller instead.
    "wake.openwakeword.tflite": (
        "ai-edge-litert==2.1.6",
    ),
    "wake.openwakeword": (
        "openwakeword==0.6.0",
        "onnxruntime==1.27.0",
        "sounddevice==0.5.5",
        "numpy==2.4.3",
    ),
    # Open-vocabulary keyword spotting: any typed phrase, zero training.
    # sentencepiece is required by sherpa_onnx.text2token (runtime phrase
    # tokenization) even though sherpa-onnx doesn't declare it.
    "wake.sherpa": (
        "sherpa-onnx==1.13.4",
        "sentencepiece==0.2.2",
        "sounddevice==0.5.5",
        "numpy==2.4.3",
    ),
    "wake.porcupine": (
        "pvporcupine==4.0.3",
        "sounddevice==0.5.5",
        "numpy==2.4.3",
    ),

    # ─── Image generation backends ─────────────────────────────────────────
    "image.fal": ("fal-client==0.13.1",),

    # ─── Memory providers ──────────────────────────────────────────────────
    "memory.honcho": ("honcho-ai==2.2.0",),
    "memory.hindsight": ("hindsight-client==0.6.1", "aiohttp==3.14.3"),
    # supermemory + mem0 are opt-in cloud memory providers with their own
    # SDKs. On the published Docker image the agent venv is sealed
    # (HERMES_DISABLE_LAZY_INSTALLS=1) and lazy installs are redirected to the
    # durable target — so, like honcho/hindsight, these MUST go through
    # ensure() to be installable there. Without an allowlist entry + an
    # ensure() call at the import site, the SDK never installs on a hosted
    # instance and the provider silently reports itself unavailable.
    "memory.supermemory": ("supermemory==3.50.0",),
    "memory.mem0": ("mem0ai==2.0.10",),

    # ─── Messaging platforms (lazy-installable on demand) ──────────────────
    "platform.telegram": ("python-telegram-bot[webhooks]==22.8",),
    # brotlicffi gives aiohttp a working 2-arg Decompressor.process() for
    # Discord CDN's Brotli-encoded attachments. Without it, aiohttp falls
    # back to google's `Brotli` package (1-arg API), and any .txt/.md/.doc
    # uploaded to the Discord gateway fails to decode at att.read() with
    # "Can not decode content-encoding: br" — see #12511 / #15744.
    # Keep the voice-only dependencies explicit instead of requesting
    # discord.py[voice].  discord.py 2.7.1's published voice metadata still
    # declares PyNaCl<1.6, which conflicts with the security floor below;
    # davey is the only additional voice dependency needed by the adapter.
    "platform.discord": (
        "discord.py==2.7.1",
        "davey==0.1.4",
        "brotlicffi==1.2.0.1",
        # PyNaCl's runtime imports cffi. Keep presence explicit because the
        # exact PyNaCl --no-deps recovery cannot install transitive packages.
        "cffi",
        # Keep this direct pin because the lazy uv/pip invocation may run
        # outside the project root and therefore cannot rely on pyproject's
        # resolver metadata or uv override-dependencies.
        "PyNaCl==1.6.2",
        # discord.py pulls aiohttp transitively (>=3.7.4,<4) as its HTTP
        # backbone. Pin the patched floor here too so the lazy Discord path
        # can't keep an already-installed vulnerable aiohttp satisfying that
        # range — mirrors the messaging extra and platform.slack.
        "aiohttp==3.14.3",  # prior CVEs + GHSA-cq5v-8q36-5273/GHSA-mfx4-hv73-q22v/GHSA-mq44-7p77-q5h7
    ),
    "platform.slack": (
        "slack-bolt==1.30.0",
        "slack-sdk==3.43.0",
        "aiohttp==3.14.3",  # prior CVEs + GHSA-cq5v-8q36-5273/GHSA-mfx4-hv73-q22v/GHSA-mq44-7p77-q5h7
    ),
    "platform.matrix": (
        "mautrix[encryption]==0.21.1",
        "aiosqlite==0.22.1",
        "asyncpg==0.31.0",
        "aiohttp-socks==0.11.0",
        # mautrix (aiohttp>=3,<4) and aiohttp-socks (aiohttp>=3.10.0) only cap
        # aiohttp transitively, so a vulnerable already-installed aiohttp still
        # satisfies both — pin the patched floor here too, like platform.discord.
        "aiohttp==3.14.3",  # prior CVEs + GHSA-cq5v-8q36-5273/GHSA-mfx4-hv73-q22v/GHSA-mq44-7p77-q5h7
    ),
    "platform.dingtalk": (
        "dingtalk-stream==0.24.3",
        "qrcode==7.4.2",
        "aiohttp==3.14.3",
    ),
    # DingTalk's AI-card/robot SDK is optional.  Keep it out of the core
    # Stream Mode contract so a missing or broken card dependency cannot
    # prevent ordinary text messaging from starting.  The adapter requests
    # this feature only when a card template is configured.
    "platform.dingtalk_card": ("alibabacloud-dingtalk==2.2.42",),
    "platform.feishu": (
        "lark-oapi==1.6.8",
        "qrcode==7.4.2",
        # The webhook transport imports aiohttp.web independently of lark-oapi.
        "aiohttp==3.14.3",
        # Websocket mode is the default Feishu transport and must remain
        # usable after a lazy install, even when it was absent at import time.
        "websockets==15.0.1",
    ),
    # WeCom callback-mode adapter — parses untrusted XML POST bodies. Pulls
    # defusedxml protects callback XML and aiohttp provides the callback web
    # server. Keep both explicit because callback-mode can be lazy-installed
    # independently of the broader messaging extras.
    "platform.wecom_callback": ("defusedxml==0.7.1", "aiohttp==3.14.3"),
    # Microsoft Teams adapter — microsoft-teams-apps pulls a heavy tree
    # (microsoft-teams-api/cards/common, dependency-injector, msal). Lazy-
    # installed on demand like every other messaging platform; also exposed
    # as the `teams` extra in pyproject for packagers / explicit installs.
    "platform.teams": ("microsoft-teams-apps==2.0.13.4", "aiohttp==3.14.3"),  # aiohttp 3.14.3: prior CVEs + GHSA-cq5v-8q36-5273/GHSA-mfx4-hv73-q22v/GHSA-mq44-7p77-q5h7

    # ─── Terminal backends ─────────────────────────────────────────────────
    "terminal.modal": ("modal==1.3.4", "aiohttp==3.14.3"),
    "terminal.daytona": ("daytona==0.155.0", "aiohttp==3.14.3"),
    "terminal.vercel": ("vercel==0.7.2",),

    # ─── Skills ────────────────────────────────────────────────────────────
    "skill.google_workspace": (
        "google-api-python-client==2.194.0",
        "google-auth==2.55.1",
        "google-auth-oauthlib==1.3.1",
        "google-auth-httplib2==0.3.1",
        # Transitive via google-api-python-client/google-auth-httplib2; keep explicit
        # so lazy installs do not resolve vulnerable transitives: httplib2 0.31.2
        # (GHSA-j5g9-f88f-gfj3 decompression bomb DoS), stale pyasn1/google-auth.
        "httplib2==0.32.0",
        "pyasn1==0.6.4",
    ),
    "skill.youtube": ("youtube-transcript-api==1.2.4",),

    # ─── Tools ─────────────────────────────────────────────────────────────
    # ACP adapter (VS Code / Zed / JetBrains integration)
    "tool.acp": ("agent-client-protocol==0.9.0",),
    # Dashboard (`hermes dashboard`)
    "tool.dashboard": (
        "fastapi==0.133.1",
        "uvicorn[standard]==0.41.0",
        "starlette==1.3.1",  # CVE-2026-48710 (BadHost) — keep lazy-install in sync with pyproject [web]
        "python-multipart==0.0.32",  # FastAPI UploadFile/Form for streaming uploads (NS-501)
    ),
    # Vision image-resize recovery (Pillow). Pillow is now a CORE dependency
    # (pyproject `dependencies`), so this entry is a belt-and-suspenders fallback
    # for stripped/source-build installs that somehow dropped it. The vision
    # call site uses prompt=False so it can never raise a blocking input()
    # prompt mid-session (#40490).
    "tool.vision": ("Pillow==12.3.0",),
    # Document-to-Markdown extraction for read_file (firecrawl-anydoc, Rust
    # core, imports as `anydoc`). Widens read_file's auto-extraction beyond
    # the stdlib .ipynb/.docx/.xlsx to PDF, legacy Office (.doc/.ppt/.xls),
    # OpenDocument, RTF, and EPUB. Installed on first read of such a file;
    # the call site uses prompt=False so read_file never blocks on a prompt.
    # NOTE: lazy-only for now — no pyproject `doc-extract` extra until the
    # package clears the uv exclude-newer 14-day quarantine (first release
    # 2026-08-04); add the mirrored extra then.
    "tool.doc_extract": ("firecrawl-anydoc==0.1.6",),
    # Computer Use (cua-driver) — the MCP client SDK used to spawn and talk
    # to the cua-driver process over stdio. Matches the `mcp` / `computer-use`
    # extras in pyproject.toml. The one-liner installer pulls this in via
    # `[all]`; lazy-installing here covers lean / partial / broken-extra
    # installs so computer_use never dead-ends on `No module named 'mcp'`.
    "tool.computer_use": (
        "mcp==2.0.0",
        "httpx2==2.7.0",  # mcp 2.x HTTP stack — keep in sync with pyproject [computer-use]
        "starlette==1.3.1",  # CVE-2026-48710 — keep in sync with pyproject [computer-use]
    ),
    # HF Agent Trace Viewer upload (hermes trace upload / /upload-trace).
    #
    # huggingface-hub is a SHARED dependency: transformers (pulled by
    # sentence-transformers for local Hindsight embeddings) requires
    # >=1.5.0,<2, and faster-whisper/tokenizers depend on it transitively.
    # Because active_features() marks a feature active from mere package
    # presence, the `hermes update` lazy-refresh pass re-asserts THIS pin on
    # every install where hub is present — so an exact pin below 1.5.0
    # force-downgrades the shared package and breaks Hindsight startup
    # (#60783). Policy: keep the exact pin (no ranges — security posture),
    # but it MUST stay inside transformers' accepted window and MUST match
    # uv.lock so the whole tree converges on ONE hub version
    # (tests/test_project_metadata.py enforces both). When bumping: update
    # here AND `uv lock --upgrade-package huggingface-hub` in lockstep.
    "tool.trace_upload": ("huggingface-hub==1.24.0",),
}


# Conservative regex for spec validation — package name plus optional
# version range. Reject anything that looks like a URL, file path, or shell
# metacharacter.
_SAFE_SPEC = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*"        # package name
    r"(?:\[[A-Za-z0-9_,\-]+\])?"            # optional [extras]
    r"(?:[<>=!~]=?[A-Za-z0-9_.\-+,*<>=!~]+)?"  # optional version specifier
    r"$"
)


class FeatureUnavailable(RuntimeError):
    """A lazily-installable feature is missing and cannot be made available.

    Either the deps were never installed and the user has disabled lazy
    installs, or the install attempt failed.
    """

    def __init__(self, feature: str, missing: tuple[str, ...], reason: str):
        self.feature = feature
        self.missing = missing
        self.reason = reason
        super().__init__(self._format())

    def _format(self) -> str:
        spec_list = " ".join(repr(s) for s in self.missing)
        return (
            f"Feature {self.feature!r} unavailable: {self.reason}. "
            f"To enable manually: uv pip install {spec_list}  "
            f"(or: pip install {spec_list})."
        )


@dataclass(frozen=True)
class _InstallResult:
    success: bool
    stdout: str
    stderr: str


# =============================================================================
# Internals
# =============================================================================


def _pip_version_meets_floor(output: str) -> bool:
    """Return whether ``pip --version`` output proves the required floor."""
    return _pip_security.pip_version_meets_floor(output)


def _ensure_pip_floor(
    pip_cmd: list[str], *, timeout: int = 120
) -> tuple[bool, str]:
    """Require a venv-managed pip at or above the security floor.

    ``ensurepip`` only guarantees that *some* pip is present.  If its bundled
    version is below the floor, upgrade it explicitly and verify the result;
    an unavailable or unverifiable floor is a hard failure for the fallback
    install path.
    """

    return _pip_security.ensure_pip_floor(
        pip_cmd,
        timeout=timeout,
        runner=subprocess.run,
        creationflags=windows_hide_flags(),
    )


# Environment variable that redirects lazy installs away from the (sealed)
# agent venv and into a writable directory on a durable volume. Set by the
# Docker image to /opt/data/lazy-packages. This is an internal bridge var,
# not user-facing config: the user-facing knob remains
# security.allow_lazy_installs in config.yaml. When unset, lazy installs go
# into the active venv as before.
_LAZY_TARGET_ENV = "HERMES_LAZY_INSTALL_TARGET"

# Name of the stamp file written into the target dir recording the Python
# X.Y + ABI it was populated for. If a container rebuild bumps the
# interpreter, compiled wheels (.so) in the durable store would be ABI-
# incompatible; we detect the mismatch and wipe the store so packages get
# re-resolved against the new interpreter rather than importing a stale .so.
_TARGET_STAMP_NAME = ".python-abi"


def _python_abi_tag() -> str:
    """A stable token identifying the running interpreter's ABI.

    Combines the X.Y version with the EXT_SUFFIX (which encodes the ABI
    tag and platform, e.g. ``cpython-313-x86_64-linux-gnu``). Two
    interpreters that can share compiled wheels produce the same token.
    """
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    ext = sysconfig.get_config_var("EXT_SUFFIX") or ""
    return f"{ver}:{ext}"


def _lazy_install_target() -> Optional[Path]:
    """Return the durable install-target dir, or None for venv-scoped mode.

    Returns a path only when :data:`_LAZY_TARGET_ENV` is set to a non-empty
    value. The directory is created on demand by :func:`_ensure_target_ready`.
    """
    raw = os.environ.get(_LAZY_TARGET_ENV, "").strip()
    if not raw:
        return None
    return Path(raw)


def _ensure_target_ready(target: Path) -> Optional[str]:
    """Create the target dir and validate its ABI stamp.

    If the stamp is missing, any existing contents are treated as untrusted
    and wiped before the target is used. If it is present but records a
    different interpreter ABI than the one now running (e.g. the container
    image was rebuilt onto a newer Python), the directory's contents are
    likewise wiped and the stamp rewritten, so stale compiled wheels can't
    be imported against an incompatible interpreter. Every wipe is verified;
    a target that cannot be cleared fails closed.

    Returns ``None`` on success, or an error string if the directory can't
    be created / written (e.g. read-only mount, permission error).
    """
    want = _python_abi_tag()
    stamp = target / _TARGET_STAMP_NAME
    try:
        # Never follow a user-controlled target-root symlink.  The unstamped
        # and ABI-mismatch paths below recursively remove the target contents;
        # accepting a symlink here would let that wipe an unrelated directory.
        if target.is_symlink():
            return f"lazy install target {target} must not be a symlink"
        if target.exists():
            have = ""
            try:
                have = stamp.read_text(encoding="utf-8").strip()
            except (OSError, FileNotFoundError):
                have = ""
            if not have:
                logger.info(
                    "Lazy install target %s has no ABI stamp; wiping "
                    "untrusted contents before activation.",
                    target,
                )
            elif have != want:
                logger.info(
                    "Lazy install target %s was built for ABI %r but running "
                    "ABI is %r; wiping stale packages.",
                    target, have, want,
                )
            if not have or have != want:
                for child in target.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                remaining = tuple(target.iterdir())
                if remaining:
                    names = ", ".join(str(child.name) for child in remaining[:3])
                    return (
                        f"lazy install target {target} could not be cleared; "
                        f"remaining entries: {names}"
                    )
        target.mkdir(parents=True, exist_ok=True)
        stamp.write_text(want, encoding="utf-8")
    except OSError as e:
        return f"lazy install target {target} is not writable: {e}"
    return None


def _activate_target_on_syspath(target: Path) -> None:
    """Append the durable target to ``sys.path`` so its packages import.

    Appended to the END (never prepended) so the agent's own venv
    site-packages takes precedence on every name collision. Idempotent.
    Deliberately do not use :func:`site.addsitedir`: processing a ``.pth``
    file executes arbitrary import statements, which would turn a writable
    durable package directory into a code-execution hook at every startup.
    Packages and namespace packages directly under the target remain
    importable through the ordinary ``sys.path`` entry.
    """
    target_str = str(target)
    if target_str not in sys.path:
        sys.path.append(target_str)
    # importlib.metadata caches the path-based distribution finder; clear it
    # so a just-activated dir is visible to version() checks this process.
    try:
        import importlib
        importlib.invalidate_caches()
    except Exception:
        pass


def activate_durable_lazy_target() -> None:
    """Public: wire the durable lazy-install target onto ``sys.path``.

    Safe no-op when :data:`_LAZY_TARGET_ENV` is unset or the directory does
    not yet exist. Called once early in process startup (before backends
    import) so packages installed into the durable store on a previous run
    are importable on this run. Never raises.
    """
    target = _lazy_install_target()
    if target is None:
        return
    try:
        if not target.exists():
            return
        error = _ensure_target_ready(target)
        if error:
            logger.debug("Refusing to activate durable lazy target: %s", error)
            return
        _activate_target_on_syspath(target)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Failed to activate durable lazy target %s: %s", target, e)


def _allow_lazy_installs() -> bool:
    """Return whether lazy installs are permitted in this environment.

    Resolution order:

    1. ``security.allow_lazy_installs: false`` in config.yaml is an absolute
       opt-out — it disables installs in BOTH venv-scoped and durable-target
       modes. This is the user-facing kill switch.
    2. ``HERMES_DISABLE_LAZY_INSTALLS=1`` seals the *agent venv* (set by the
       immutable Docker image). It blocks venv-scoped installs — UNLESS a
       durable install target is configured, in which case installs are
       redirected there (a path that structurally cannot break the sealed
       venv) and are therefore allowed.

    Defaults to True. If config is unreadable we fail open (allow), because
    refusing to install would lock people out of their own backends; the
    decision to block is an explicit user opt-in.
    """
    # (1) Config kill switch wins in every mode.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        cfg = None
    if cfg is not None:
        sec = cfg.get("security") or {}
        if not bool(sec.get("allow_lazy_installs", True)):
            return False

    # (2) Sealed-venv env var: blocks ONLY when there is no safe durable
    # target to redirect into. With a target set, the install goes to the
    # data volume (append-only on sys.path), so the seal is preserved.
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1":
        return _lazy_install_target() is not None

    return True


def _unsupported_feature_reason(feature: str) -> Optional[str]:
    """Return why a lazy feature cannot work on this host, or ``None``.

    This is a platform capability gate, not a security policy gate. It keeps
    known-impossible installs out of both first-use lazy installation and the
    ``hermes update`` lazy-refresh pass.
    """
    if sys.platform == "win32" and feature == "platform.matrix":
        return (
            "unsupported on Windows: Matrix E2EE depends on python-olm, "
            "which has no Windows wheel and requires make + libolm to build "
            "from sdist. Run Hermes under WSL to use Matrix on Windows."
        )
    return None


def _spec_is_safe(spec: str) -> bool:
    """Reject pip specs that contain URLs, paths, or shell metacharacters."""
    if not spec or len(spec) > 200:
        return False
    if any(ch in spec for ch in (";", "|", "&", "`", "$", "\n", "\r", "\t", "\\")):
        return False
    if spec.startswith(("-", "/", ".")) or "://" in spec or "@" in spec:
        return False
    return bool(_SAFE_SPEC.match(spec))


def _pkg_name_from_spec(spec: str) -> str:
    """Extract the bare package name from a pip spec.

    ``"slack-bolt>=1.18.0,<2"`` → ``"slack-bolt"``
    ``"mautrix[encryption]>=0.20"`` → ``"mautrix"``
    """
    m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*)", spec)
    return m.group(1) if m else spec


def _normalize_distribution_name(name: str) -> str:
    """Normalize distribution names according to the PEP 503 spelling."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _specifier_from_spec(spec: str) -> str:
    """Extract just the version-specifier portion of a pip spec.

    ``"honcho-ai==2.2.0"`` → ``"==2.2.0"``
    ``"mautrix[encryption]>=0.20,<1"`` → ``">=0.20,<1"``
    ``"package"`` → ``""`` (no version constraint)
    """
    # Strip the package name + optional [extras] block.
    m = re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(?:\[[A-Za-z0-9_,\-]+\])?", spec)
    if not m:
        return ""
    return spec[m.end():]


def _is_security_versioned_spec(spec: str) -> bool:
    """Whether ``spec`` requires strict stable-version evidence."""

    package = _pkg_name_from_spec(spec).replace("_", "-").lower()
    return package in _SECURITY_VERSIONED_PACKAGES


def _security_install_spec_error(spec: str) -> str | None:
    """Return a reason when an arbitrary manifest spec misses a security floor.

    ``install_specs`` accepts package names from runtime manifests rather than
    the checked-in allowlist.  For the packages with known security floors, a
    bare name or an upper-bound-only requirement is not enough evidence: pip
    could resolve a vulnerable release.  Require an explicit lower-bound,
    compatible, or exact requirement whose version is at least the floor.
    ``packaging`` is already a normal Hermes dependency; if it is unavailable
    while validating an untrusted manifest, fail closed instead of guessing.
    """
    package = _normalize_distribution_name(_pkg_name_from_spec(spec))
    floor_text = _SECURITY_INSTALL_FLOORS.get(package)
    if floor_text is None:
        return None

    spec_tail = _specifier_from_spec(spec)
    if not spec_tail:
        return f"{package} requires an explicit security floor >= {floor_text}"

    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version

        floor = Version(floor_text)
        specifiers = SpecifierSet(spec_tail)
    except (ImportError, InvalidSpecifier, InvalidVersion, TypeError, ValueError):
        return f"{package} has an unverifiable security floor >= {floor_text}"

    # A lower-bound, compatible, or exact clause at/above the floor proves
    # that pip cannot select a vulnerable release.  Wildcard exact matches
    # (for example ``==3.14.*``) are intentionally rejected because they also
    # admit releases below the fixed floor.
    for requirement in specifiers:
        if requirement.operator not in {">=", ">", "~=", "==", "==="}:
            continue
        if "*" in requirement.version:
            continue
        try:
            bound = Version(requirement.version)
        except InvalidVersion:
            continue
        if bound >= floor:
            return None

    return f"{package} requires an explicit security floor >= {floor_text}"


def _is_satisfied(spec: str) -> bool:
    """Is ``spec`` already satisfied in the current env?

    Checks both presence AND version. If the package is installed at a
    version outside the spec's range, returns False so the caller will
    upgrade/downgrade to the pinned version. This is what makes
    ``hermes update`` propagate pin bumps in :data:`LAZY_DEPS` to already-
    installed backends instead of silently leaving stale versions in place.

    Ordinary non-security specs retain the historical presence-only fallback
    when ``packaging`` is unavailable. Security-managed specs do not: their
    installed metadata must prove a stable release, otherwise the caller must
    perform the repair.
    """
    pkg = _pkg_name_from_spec(spec)
    try:
        from importlib.metadata import PackageNotFoundError, version
    except Exception:
        return False
    try:
        installed = version(pkg)
    except PackageNotFoundError:
        return False
    except Exception:
        return False

    spec_tail = _specifier_from_spec(spec)
    if not spec_tail:
        # Bare ``"package"`` — no version constraint, presence is enough.
        return True

    strict_security = _is_security_versioned_spec(spec)

    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ImportError:
        # A security repair cannot be skipped without version evidence.
        return False if strict_security else True

    if strict_security and _pip_security.stable_version_tuple(installed) is None:
        # PEP 440 equality accepts local versions (e.g. 1.6.2+vendor), while
        # prerelease/dev/unknown suffixes can also be numerically misleading.
        # Accept only a stable release, with optional .postN, as evidence.
        return False

    if strict_security and spec_tail.startswith("==") and "," not in spec_tail:
        # ``packaging`` treats ``==1.6.2.post1`` as distinct from
        # ``==1.6.2``. For the security floor, a stable post-release of the
        # pinned release is intentionally acceptable; it contains the same
        # release line plus the post-release fixes. Keep this narrow and only
        # apply it when both sides are otherwise stable.
        expected_text = spec_tail[2:]
        if _pip_security.stable_version_tuple(expected_text) is not None:
            try:
                installed_version = Version(installed)
                expected_version = Version(expected_text)
            except InvalidVersion:
                return False
            if (
                expected_version.post is None
                and installed_version.post is not None
                and installed_version.release == expected_version.release
            ):
                return True

    try:
        return Version(installed) in SpecifierSet(spec_tail)
    except (InvalidSpecifier, InvalidVersion, Exception):
        # Malformed spec or installed version we can't parse. A security
        # contract must repair/fail closed; ordinary lazy specs retain the
        # historical no-churn fallback.
        return False if strict_security else True


def _is_present(spec: str) -> bool:
    """Cheap presence-only check (package name installed at any version).

    Used by :func:`active_features` to detect backends the user has
    previously activated, regardless of whether the version pin moved.
    """
    pkg = _pkg_name_from_spec(spec)
    try:
        from importlib.metadata import PackageNotFoundError, version
    except Exception:
        return False
    try:
        version(pkg)
        return True
    except PackageNotFoundError:
        return False
    except Exception:
        return False


_PACKAGE_IMPORT_ROOT_ALIASES: dict[str, tuple[str, ...]] = {
    # Distribution names do not always match their import package.
    "alibabacloud-dingtalk": ("alibabacloud_dingtalk",),
    "alibabacloud-tea-openapi": ("alibabacloud_tea_openapi",),
    "alibabacloud-tea-util": ("alibabacloud_tea_util",),
    "discord.py": ("discord",),
    "google-api-python-client": ("googleapiclient",),
    "google-auth-httplib2": ("google_auth_httplib2",),
    "google-auth-oauthlib": ("google_auth_oauthlib",),
    "lark-oapi": ("lark_oapi",),
    "microsoft-teams-apps": ("microsoft_teams",),
    "python-telegram-bot": ("telegram",),
    "slack-bolt": ("slack_bolt",),
    "slack-sdk": ("slack_sdk",),
}

# Only compiled/security-critical roots need a process-restart boundary when
# their distribution is repaired. Pure-Python packages such as aiohttp can be
# rebound by their active adapter hooks; blocking every aiohttp submodule here
# would turn ordinary lazy refreshes into unnecessary restart requirements.
_PRELOADED_COMPILED_IMPORT_ROOTS: dict[str, tuple[str, ...]] = {
    "pynacl": ("nacl",),
    "cffi": ("_cffi_backend",),
    # Feishu's deferred SDK is imported only at connect time, but adapters
    # retain its module-level request/client classes for their whole process
    # lifetime.  A lazy repair cannot safely replace an already-loaded stale
    # lark-oapi tree in place; require a fresh Hermes process instead.
    "lark-oapi": ("lark_oapi",),
    # aiohttp is pure Python, but its active adapters retain module-level
    # references across a lazy repair.  Replacing its distribution in-process
    # can therefore leave stale web/client classes bound just like a compiled
    # package. Require a fresh Hermes process before reporting readiness.
    "aiohttp": ("aiohttp",),
}


def _managed_import_roots(spec: str) -> tuple[str, ...]:
    """Return import roots whose objects can be left stale by a repair."""
    package = _pkg_name_from_spec(spec).lower()
    aliases = _PACKAGE_IMPORT_ROOT_ALIASES.get(package)
    if aliases is not None:
        return aliases
    normalized = package.replace("-", "_").replace(".", "_")
    roots = [normalized]
    if package == "pynacl":
        roots.append("nacl")
    elif package == "cffi":
        roots.append("_cffi_backend")
    return tuple(dict.fromkeys(roots))


def _preloaded_stale_module_conflicts(
    specs: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Find stale managed packages already loaded in this interpreter.

    Pip can replace a distribution's files and metadata without changing
    module objects already present in sys.modules. Rebinding those objects in
    place is unsafe, especially for compiled packages, so a lazy repair must
    stop at an explicit process-restart boundary.
    """
    conflicts: list[tuple[str, str]] = []
    loaded_names = tuple(
        name for name, module in sys.modules.items() if module is not None
    )
    for spec in specs:
        if _is_satisfied(spec):
            continue
        package = _pkg_name_from_spec(spec).replace("_", "-").lower()
        roots = _PRELOADED_COMPILED_IMPORT_ROOTS.get(package, ())
        if not roots:
            continue
        for name in loaded_names:
            if any(name == root or name.startswith(f"{root}.") for root in roots):
                conflicts.append((spec, name))
    return tuple(conflicts)


def _preloaded_stale_module_reason(
    conflicts: tuple[tuple[str, str], ...],
) -> str:
    details = ", ".join(f"{spec} ({name})" for spec, name in conflicts)
    return (
        "restart required before lazy repair: managed package module(s) "
        f"already loaded with stale metadata: {details}; restart Hermes "
        "before retrying so imports bind the repaired files"
    )


def _distribution_is_target_owned(spec: str, target: Path) -> bool:
    """Return whether the visible distribution for ``spec`` lives in target.

    Durable targets are appended to ``sys.path`` and may already be active in
    this process. A stale target-owned distribution is repairable in place;
    only a stale distribution resolved from outside the target is core-owned
    and must fail closed because the append-only target cannot shadow it.
    """

    try:
        from importlib.metadata import distribution

        location = Path(distribution(_pkg_name_from_spec(spec)).locate_file(""))
        target_root = target.resolve()
        location = location.resolve()
        return location == target_root or target_root in location.parents
    except Exception:
        # Unknown ownership is unsafe to treat as target-owned. The caller
        # therefore preserves the core fail-closed behavior.
        return False


def _core_constraints_file(
    exclude_packages: tuple[str, ...] = (),
) -> Optional[Path]:
    """Write a pip constraints file pinning every package already importable
    in the core environment to its installed version.

    Passed as ``--constraint`` for durable-target installs so the resolver
    pins shared transitive deps (httpx, pydantic, aiohttp, …) to the exact
    versions the core venv already ships, instead of pulling newer copies
    into the durable store. Two payoffs:

    * The durable store stays minimal — only genuinely-new packages land
      there; shared deps resolve to "already satisfied" against core.
    * A backend that *requires* a version conflicting with core fails loudly
      at install time (resolver conflict) rather than silently installing a
      shadowed copy that can never win on sys.path anyway.

    The PyNaCl security override is intentionally excluded. Explicitly
    managed backend packages supplied by the caller are excluded as well:
    their requested versions must not be turned into stale core constraints.
    Because the durable target is append-only, the caller rejects an already
    installed but stale core copy before running the transaction; otherwise
    the target copy would lose on sys.path and the stale core package would
    silently remain active.

    Returns the path to a temp constraints file, or None if enumeration
    failed (in which case the caller installs without constraints — still
    safe, just less tidy).
    """
    try:
        from importlib.metadata import distributions
    except ImportError:
        return None
    try:
        import tempfile
        lines = []
        seen = set()
        excluded = {
            _normalize_distribution_name(name)
            for name in _CONSTRAINT_EXCLUDED_PACKAGES
        }
        excluded.update(
            _normalize_distribution_name(_pkg_name_from_spec(spec))
            for spec in exclude_packages
        )
        for dist in distributions():
            name = dist.metadata["Name"] if dist.metadata else None
            ver = dist.version
            if not isinstance(name, str) or not isinstance(ver, str):
                continue
            key = _normalize_distribution_name(name)
            if not _CONSTRAINT_NAME_RE.fullmatch(key):
                continue
            version_text = ver.strip()
            if (
                not _CONSTRAINT_VERSION_RE.fullmatch(version_text)
                or _pip_security.stable_version_tuple(version_text) is None
            ):
                continue
            if key in excluded:
                continue
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{key}=={version_text}")
        if not lines:
            return None
        fd, path = tempfile.mkstemp(prefix="hermes-core-constraints-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(lines)) + "\n")
        return Path(path)
    except Exception as e:
        logger.debug("Could not build core constraints file: %s", e)
        return None


def _venv_pip_install(specs: tuple[str, ...], *, timeout: int = 300) -> _InstallResult:
    """Install ``specs`` using the uv → pip → ensurepip ladder.

    Two modes:

    * **Venv-scoped (default).** Installs into the active venv
      (``sys.executable``). Used on normal installs.
    * **Durable-target.** When :data:`_LAZY_TARGET_ENV` is set, installs into
      that directory via ``--target`` and constrains shared deps to the
      core venv's versions (see :func:`_core_constraints_file`). The target
      is append-only on ``sys.path`` so it can never shadow core. Used by
      the immutable Docker image to keep lazy installs off the sealed venv.

    Mirrors the strategy in ``hermes_cli.tools_config._pip_install`` but
    kept independent here so this module has no CLI dependency.
    """
    if not specs:
        return _InstallResult(True, "", "")

    preloaded_conflicts = _preloaded_stale_module_conflicts(specs)
    if preloaded_conflicts:
        return _InstallResult(
            False,
            "",
            _preloaded_stale_module_reason(preloaded_conflicts),
        )

    target = _lazy_install_target()
    constraints: Optional[Path] = None
    # Keep the exact security override in a separate transaction.  In
    # particular, a durable target may be paired with a stale core venv, and
    # uv/pip invoked outside the project do not consume pyproject's
    # override-dependencies.  The ordinary Discord requirements intentionally
    # omit discord.py's stale [voice] metadata; the exact transaction still
    # makes the security invariant explicit and repairs pre-existing installs.
    # Match the managed override by normalized distribution name and exact
    # version, not by spelling. Pip/metadata accept case and ``-_.``
    # variations (for example ``pynacl==1.6.2``), and leaving one of those
    # spellings in the regular resolver transaction can reintroduce the
    # discord.py voice upper bound before the exact repair runs.
    override_specs = tuple(
        spec
        for spec in specs
        if _normalize_distribution_name(_pkg_name_from_spec(spec))
        == _normalize_distribution_name(_pkg_name_from_spec(_PYNACL_SECURITY_SPEC))
        and _specifier_from_spec(spec) == "==1.6.2"
    )
    regular_specs = tuple(spec for spec in specs if spec not in override_specs)

    if target is not None:
        # Create/validate the target and clear ABI-incompatible contents before
        # inspecting ownership. A stale target-only distribution is then
        # eligible for repair; a stale core distribution remains protected by
        # the append-only sys.path invariant.
        err = _ensure_target_ready(target)
        if err:
            return _InstallResult(False, "", err)
        # An append-only durable target cannot override a package that is
        # already present in the sealed/core venv. Excluding these packages
        # from the generated constraints lets the resolver proceed when the
        # core version is absent or already compatible; this preflight makes
        # an incompatible core-owned package fail closed instead of silently
        # winning over the requested target version at import time.
        stale_core_specs = tuple(
            spec
            for spec in specs
            if (
                _is_present(spec)
                and not _is_satisfied(spec)
                and not _distribution_is_target_owned(spec, target)
            )
        )
        if stale_core_specs:
            return _InstallResult(
                False,
                "",
                "core package(s) "
                f"{', '.join(stale_core_specs)} must be repaired before "
                "durable lazy install; append-only sys.path cannot replace "
                "them, so refresh the core environment before enabling this "
                "feature",
            )
        constraints = _core_constraints_file(
            tuple(_pkg_name_from_spec(spec) for spec in specs)
        )

    target_args: list[str] = []
    if target is not None:
        # --target tells both uv and pip to install into an arbitrary dir.
        target_args = ["--target", str(target)]
    constraint_args: list[str] = []
    if constraints is not None:
        constraint_args = ["--constraint", str(constraints)]

    def _security_specs_satisfied() -> bool:
        """Re-read the active environment's exact security override state."""
        try:
            import importlib
            importlib.invalidate_caches()
            import importlib.metadata as _md
            if hasattr(_md, "_cache_clear"):
                _md._cache_clear()  # type: ignore[attr-defined]
        except Exception:
            pass
        if not override_specs:
            return True
        # PyNaCl imports cffi at runtime. The exact --no-deps retry cannot
        # supply that transitive dependency, so do not report a repaired
        # Discord feature unless cffi is present too.
        return all(_is_satisfied(spec) for spec in override_specs) and _is_present(
            "cffi"
        )

    project_root = Path(__file__).resolve().parents[1]
    project_args = (
        ["--project", str(project_root)]
        if (project_root / "pyproject.toml").is_file()
        else []
    )

    try:
        venv_root = Path(sys.executable).parent.parent
        from tools.environments.local import hermes_subprocess_env
        uv_env = hermes_subprocess_env(inherit_credentials=False)
        uv_env["VIRTUAL_ENV"] = str(venv_root)

        # Tier 1: uv (preferred — fast, doesn't need pip in the venv)
        # Managed uv first: $HERMES_HOME/bin is never on PATH, so a bare
        # which() misses the uv Hermes installed and falls through to the
        # slower pip tier. Deliberately a lookup and not ensure_uv(): this runs
        # mid-turn to install an optional dependency, and downloading uv +
        # migrating the Python runtime as a side effect of that is a far bigger
        # action than the caller asked for. Tier 2 pip covers the no-uv case.
        try:
            from hermes_cli.managed_uv import resolve_uv

            uv_bin = resolve_uv() or shutil.which("uv")
        except Exception:
            uv_bin = shutil.which("uv")
        if uv_bin:
            try:
                stdout = ""
                stderr = ""
                transactions = (
                    ((regular_specs, constraint_args), (override_specs, []))
                    if override_specs
                    else ((regular_specs, constraint_args),)
                )
                for transaction_specs, transaction_constraints in transactions:
                    if not transaction_specs:
                        continue
                    r = subprocess.run(
                        [
                            uv_bin,
                            "pip",
                            "install",
                            *project_args,
                            *target_args,
                            *transaction_constraints,
                            *transaction_specs,
                        ],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, env=uv_env,
                        stdin=subprocess.DEVNULL,
                        creationflags=windows_hide_flags(),
                    )
                    stdout += r.stdout or ""
                    stderr += r.stderr or ""
                    if r.returncode != 0:
                        if transaction_specs == override_specs and override_specs:
                            # A failed resolver/network attempt must not leave
                            # the feature looking repaired. Retry the exact
                            # package without dependency resolution: this is
                            # both a useful transient-network recovery and a
                            # final guard against discord.py's stale voice
                            # upper bound re-entering the transaction.
                            retry = subprocess.run(
                                [
                                    uv_bin,
                                    "pip",
                                    "install",
                                    "--no-deps",
                                    *project_args,
                                    *target_args,
                                    *override_specs,
                                ],
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                timeout=timeout,
                                env=uv_env,
                                stdin=subprocess.DEVNULL,
                                creationflags=windows_hide_flags(),
                            )
                            stdout += retry.stdout or ""
                            stderr += retry.stderr or ""
                            if retry.returncode == 0:
                                if target is not None:
                                    _activate_target_on_syspath(target)
                                if _security_specs_satisfied():
                                    continue
                            if not _security_specs_satisfied():
                                stderr += (
                                    "\nExact PyNaCl security repair failed or "
                                    "the active environment remains below "
                                    "PyNaCl==1.6.2 or is missing runtime cffi."
                                )
                        logger.debug("uv pip install failed: %s", r.stderr)
                        # A resolver failure is authoritative. Falling through
                        # to pip here would silently discard uv policy such as
                        # exclude-newer and could install a quarantined release.
                        return _InstallResult(False, stdout, stderr)
                    if transaction_specs == override_specs and override_specs:
                        if target is not None:
                            _activate_target_on_syspath(target)
                        if not _security_specs_satisfied():
                            stderr += (
                                "\nExact PyNaCl security repair succeeded but "
                                "the active environment is missing PyNaCl's "
                                "runtime cffi dependency."
                            )
                            return _InstallResult(False, stdout, stderr)
                if target is not None:
                    _activate_target_on_syspath(target)
                return _InstallResult(True, stdout, stderr)
            except subprocess.TimeoutExpired as e:
                logger.debug("uv invocation failed: %s", e)
                return _InstallResult(False, "", f"uv pip install timed out: {e}")
            except FileNotFoundError as e:
                # The resolved uv path disappeared between lookup and spawn.
                # In that narrow availability failure, the pip tier remains a
                # valid fallback because uv never evaluated the requirements.
                logger.debug("uv invocation failed: %s", e)

        # Tier 2: python -m pip (with ensurepip bootstrap if needed)
        pip_cmd = [sys.executable, "-m", "pip"]
        try:
            probe = subprocess.run(
                pip_cmd + ["--version"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
                stdin=subprocess.DEVNULL,
                creationflags=windows_hide_flags(),
            )
            if probe.returncode != 0:
                raise FileNotFoundError("pip not in venv")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            try:
                subprocess.run(
                    [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, check=True,
                    stdin=subprocess.DEVNULL,
                    creationflags=windows_hide_flags(),
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                return _InstallResult(False, "",
                                      f"pip not available and ensurepip failed: {e}")

        pip_ok, pip_error = _ensure_pip_floor(pip_cmd, timeout=120)
        if not pip_ok:
            return _InstallResult(False, "", pip_error)

        # Apply the same split transaction strategy as uv above.  In
        # particular, skip an empty regular transaction when PyNaCl is the
        # only missing spec; ``pip install`` with no requirements fails before
        # the exact security repair can run.
        try:
            stdout = ""
            stderr = ""
            if regular_specs:
                r = subprocess.run(
                    pip_cmd + ["install", *target_args, *constraint_args, *regular_specs],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
                    stdin=subprocess.DEVNULL,
                    creationflags=windows_hide_flags(),
                )
                stdout += r.stdout or ""
                stderr += r.stderr or ""
                if r.returncode != 0:
                    return _InstallResult(False, stdout, stderr)
            if override_specs:
                override = subprocess.run(
                    pip_cmd + ["install", *target_args, *override_specs],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
                    stdin=subprocess.DEVNULL,
                    creationflags=windows_hide_flags(),
                )
                stdout += override.stdout or ""
                stderr += override.stderr or ""
                if override.returncode != 0:
                    # The regular transaction is deliberately allowed to
                    # complete before this exact repair, but a failed second
                    # step must be retried and verified rather than returning
                    # a successful install with a vulnerable PyNaCl left in
                    # the venv.  ``--no-deps`` avoids re-entering stale voice
                    # metadata on the repair attempt.
                    retry = subprocess.run(
                        pip_cmd + ["install", "--no-deps", *target_args, *override_specs],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        stdin=subprocess.DEVNULL,
                        creationflags=windows_hide_flags(),
                    )
                    stdout += retry.stdout or ""
                    stderr += retry.stderr or ""
                    if retry.returncode == 0:
                        if target is not None:
                            _activate_target_on_syspath(target)
                        if _security_specs_satisfied():
                            return _InstallResult(True, stdout, stderr)
                    if not _security_specs_satisfied():
                        stderr += (
                            "\nExact PyNaCl security repair failed or the active "
                            "environment remains below PyNaCl==1.6.2 or is missing "
                            "runtime cffi."
                        )
                    return _InstallResult(False, stdout, stderr)
                if target is not None:
                    _activate_target_on_syspath(target)
                if not _security_specs_satisfied():
                    stderr += (
                        "\nExact PyNaCl security repair succeeded but the "
                        "active environment is missing PyNaCl's runtime cffi "
                        "dependency."
                    )
                    return _InstallResult(False, stdout, stderr)
            if target is not None:
                _activate_target_on_syspath(target)
            return _InstallResult(True, stdout, stderr)
        except subprocess.TimeoutExpired as e:
            return _InstallResult(False, "", f"pip install timed out: {e}")
        except Exception as e:
            return _InstallResult(False, "", f"pip install failed: {e}")
    finally:
        if constraints is not None:
            try:
                constraints.unlink()
            except OSError:
                pass


# =============================================================================
# Public API
# =============================================================================


def feature_specs(feature: str) -> tuple[str, ...]:
    """Return the registered specs for a feature, or raise KeyError."""
    if feature not in LAZY_DEPS:
        raise KeyError(f"Unknown lazy feature: {feature!r}")
    return LAZY_DEPS[feature]


def feature_missing(feature: str) -> tuple[str, ...]:
    """Return the subset of specs for ``feature`` not currently installed."""
    return tuple(s for s in feature_specs(feature) if not _is_satisfied(s))


def ensure(feature: str, *, prompt: bool = True) -> None:
    """Make sure all packages for ``feature`` are importable.

    If they're missing, attempts to install them in the active venv. Raises
    :class:`FeatureUnavailable` if the user has disabled lazy installs or
    if the install attempt fails.

    ``prompt``: when True (default) and stdin is a TTY, asks the user to
    confirm before installing. Non-interactive callers (gateway, cron,
    batch) get prompt=False and skip the confirmation — config flag is
    the gate in that case.
    """
    if feature not in LAZY_DEPS:
        raise FeatureUnavailable(
            feature, (), f"feature {feature!r} not in LAZY_DEPS allowlist"
        )

    missing = feature_missing(feature)
    if not missing:
        return

    preloaded_conflicts = _preloaded_stale_module_conflicts(missing)
    if preloaded_conflicts:
        raise FeatureUnavailable(
            feature,
            missing,
            _preloaded_stale_module_reason(preloaded_conflicts),
        )

    unsupported = _unsupported_feature_reason(feature)
    if unsupported:
        raise FeatureUnavailable(feature, missing, unsupported)

    # Package-manager installs (NixOS, and any other distro that ships Hermes
    # from a read-only store) cannot receive lazy pip installs: the venv's
    # site-packages lives in the store, so the uv -> pip -> ensurepip ladder
    # below burns ~15s bootstrapping ensurepip only to fail on a read-only
    # target. Fail fast with an actionable message instead.
    #
    # Skipped when a durable install target is configured: the container
    # deployment sets HERMES_MANAGED=true *and* HERMES_LAZY_INSTALL_TARGET
    # (a writable volume), where lazy installs legitimately work.
    #
    # The reason string starts with "unsupported " on purpose:
    # refresh_active_features classifies FeatureUnavailable by that prefix and
    # reports anything else as a hard failure rather than a skip.
    if _lazy_install_target() is None:
        try:
            from hermes_cli.config import get_managed_system

            managed_by = get_managed_system()
        except Exception:
            managed_by = ""  # config unreadable — proceed with the install
        if managed_by:
            raise FeatureUnavailable(
                feature, missing,
                f"unsupported on {managed_by}-managed installs: this build's "
                f"packages come from {managed_by}, so Hermes cannot install "
                f"them at runtime. Add the dependencies for {feature!r} via "
                f"{managed_by} (or run a pip/uv install of Hermes instead)."
            )

    # Validate every spec against the allowlist + safety regex. Belt and
    # braces — the keys-in-LAZY_DEPS check above already constrains this.
    for spec in missing:
        if not _spec_is_safe(spec):
            raise FeatureUnavailable(
                feature, missing,
                f"refusing to install unsafe spec {spec!r}"
            )

    if not _allow_lazy_installs():
        raise FeatureUnavailable(
            feature, missing,
            "lazy installs disabled (security.allow_lazy_installs=false)"
        )

    # Only show the interactive confirmation when we own a TTY and
    # prompt_toolkit isn't running.  A bare input() deadlocks when a
    # prompt_toolkit app owns the terminal because keystrokes route to
    # its event loop rather than stdin, so the prompt blocks forever.
    # Under the TUI we skip the prompt and proceed — lazy installs are
    # gated by security.allow_lazy_installs, so reaching here is
    # already user opt-in.
    _pt_active = False
    if "prompt_toolkit.application.current" in sys.modules:
        try:
            from prompt_toolkit.application.current import get_app_or_none
            _app = get_app_or_none()
            _pt_active = _app is not None and getattr(_app, "is_running", False)
        except Exception:
            _pt_active = False

    if prompt and not _pt_active and sys.stdin.isatty() and sys.stdout.isatty():
        spec_list = ", ".join(missing)
        try:
            answer = input(
                f"\nFeature {feature!r} requires: {spec_list}\n"
                f"Install into the active venv now? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer and answer not in {"y", "yes"}:
            raise FeatureUnavailable(
                feature, missing, "user declined install at prompt"
            )

    logger.info("Lazy-installing %s for feature %r", " ".join(missing), feature)
    result = _venv_pip_install(missing)
    if not result.success:
        # Surface the actual pip error so the user can debug PyPI-side
        # issues (404 quarantine, network down, etc.).
        snippet = (result.stderr or result.stdout or "").strip()
        if snippet:
            # Clip to a readable size — pip can dump pages of resolution traces.
            snippet = snippet[-2000:]
        raise FeatureUnavailable(
            feature, missing,
            f"pip install failed: {snippet or 'no error output'}"
        )

    # Verify post-install. importlib.metadata caches per-process, so if we
    # just installed something the cache may not see it without a refresh.
    try:
        import importlib.metadata as _md
        if hasattr(_md, "_cache_clear"):
            _md._cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass

    still_missing = feature_missing(feature)
    if still_missing:
        raise FeatureUnavailable(
            feature, still_missing,
            "install reported success but packages still not importable "
            "(may require Python restart)"
        )

    logger.info("Lazy install complete for feature %r", feature)


def is_available(feature: str) -> bool:
    """Return True if the feature's deps are already satisfied."""
    if feature not in LAZY_DEPS:
        return False
    return not feature_missing(feature)


def feature_install_command(feature: str, *, venv_pip: bool = False) -> Optional[str]:
    """Return the ``pip install`` command a user could run manually, or None.

    ``venv_pip=True`` targets the running interpreter's pip
    (``{sys.executable} -m pip install …``) — correct in every layout
    (default install, ``HERMES_HOME`` overrides, profile installs) and
    immune to Ubuntu 24.04's PEP 668 ``externally-managed-environment``
    failure that a bare/system ``pip install`` hint invites.  The default
    ``uv pip install`` form is kept for contexts that document uv usage.
    """
    if feature not in LAZY_DEPS:
        return None
    specs = LAZY_DEPS[feature]
    joined = " ".join(repr(s) for s in specs)
    if venv_pip:
        return f"{sys.executable} -m pip install {joined}"
    return "uv pip install " + joined


@dataclass
class InstallSpecsResult:
    """Outcome of :func:`install_specs` for one batch of pip specs.

    ``ok``       — install succeeded (or nothing was missing).
    ``blocked``  — installs are gated off (config kill switch, sealed venv
                   without a durable target) or a spec failed validation;
                   nothing was executed. ``reason`` explains why.
    ``command``  — human-readable description of what ran (for UIs/logs).
    """
    ok: bool
    blocked: bool = False
    reason: str = ""
    command: str = ""
    stdout: str = ""
    stderr: str = ""


def install_specs(specs: list[str] | tuple[str, ...], *, timeout: int = 300) -> InstallSpecsResult:
    """Install arbitrary (validated) pip specs through the lazy-install pipeline.

    This is the environment-aware install path for callers whose package
    lists come from data (e.g. memory-provider plugin manifests declaring
    ``pip_dependencies``) rather than the static :data:`LAZY_DEPS` allowlist.
    It applies the exact same environment routing as :func:`ensure`:

    * **Venv-scoped by default** — installs into ``sys.executable``'s venv.
    * **Durable-target on immutable images** — when the deployment seals the
      agent venv (``HERMES_DISABLE_LAZY_INSTALLS=1``) and sets
      ``HERMES_LAZY_INSTALL_TARGET``, installs are redirected to the writable
      data-volume dir (``--target`` + core-venv constraints), then activated
      on ``sys.path`` so the packages import in this process immediately.
    * **Gated** — honors ``security.allow_lazy_installs`` and refuses to run
      when the venv is sealed with no durable target (never attempts a write
      to a read-only tree; reports *why* instead of surfacing EROFS/EACCES).

    Every spec must pass :func:`_spec_is_safe` (no URLs, paths, or shell
    metacharacters). Unlike :func:`ensure`, unknown packages are permitted —
    the caller owns manifest trust; this function owns spec hygiene and
    environment routing.

    Never raises; inspect the returned :class:`InstallSpecsResult`.
    """
    cleaned = tuple(str(s).strip() for s in specs if str(s).strip())
    if not cleaned:
        return InstallSpecsResult(ok=True, command="")

    for spec in cleaned:
        if not _spec_is_safe(spec):
            return InstallSpecsResult(
                ok=False, blocked=True,
                reason=f"refusing to install unsafe spec {spec!r}",
            )
        security_error = _security_install_spec_error(spec)
        if security_error is not None:
            return InstallSpecsResult(
                ok=False,
                blocked=True,
                reason=f"refusing to install insecure spec {spec!r}: {security_error}",
            )

    if not _allow_lazy_installs():
        target = _lazy_install_target()
        if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1" and target is None:
            reason = (
                "runtime installs are disabled on this deployment: the agent "
                "environment is immutable and no writable install target is "
                "configured (HERMES_LAZY_INSTALL_TARGET)"
            )
        else:
            reason = "runtime installs disabled (security.allow_lazy_installs=false)"
        return InstallSpecsResult(ok=False, blocked=True, reason=reason)

    target = _lazy_install_target()
    display = "uv pip install " + (
        f"--target {target} " if target is not None else ""
    ) + " ".join(cleaned)

    logger.info("Installing pip specs %s (target=%s)", " ".join(cleaned), target or "venv")
    try:
        result = _venv_pip_install(cleaned, timeout=timeout)
    except Exception as exc:
        logger.warning("install_specs failed unexpectedly: %s", exc)
        return InstallSpecsResult(
            ok=False, command=display, stderr=f"install failed: {exc}"
        )

    # Freshly-installed dists must be visible to importers and metadata
    # checks in this same process (dashboard rechecks availability inline).
    try:
        import importlib
        importlib.invalidate_caches()
        import importlib.metadata as _md
        if hasattr(_md, "_cache_clear"):
            _md._cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass

    return InstallSpecsResult(
        ok=result.success,
        command=display,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def active_features() -> list[str]:
    """Return the list of features the user has ever lazy-installed.

    A feature counts as "active" if its anchor package (the first declared
    spec) is currently installed in the venv (presence check, ignoring
    version). We intentionally do NOT treat shared helper packages as proof
    that a backend was enabled: for example ``platform.matrix`` depends on
    generic packages like ``asyncpg``/``aiosqlite`` that can be installed for
    unrelated reasons, while the actual Matrix adapter anchor is ``mautrix``.
    Features the user has never enabled stay quiet.

    Used by ``hermes update`` to figure out which lazy backends need a
    refresh pass when pins move in :data:`LAZY_DEPS`.
    """
    active = []
    for feature, specs in LAZY_DEPS.items():
        if specs and _is_present(specs[0]):
            active.append(feature)
    return active


def refresh_active_features(*, prompt: bool = False) -> dict[str, str]:
    """Re-run ``ensure`` for every feature the user has previously activated.

    Returns a ``{feature: status}`` map where status is one of:
        ``"current"``  — pins already satisfied, no install run
        ``"refreshed"`` — pins were stale, reinstall succeeded
        ``"failed: <reason>"`` — install attempt failed; caller decides
                                  whether to surface it (we don't raise)
        ``"skipped: <reason>"`` — gated off (config flag, user decline)
        ``"restart-required: <reason>"`` — a stale loaded module requires a
                                             fresh Hermes process

    Intended for ``hermes update``. Never raises; lazy-install failures
    here must not block the rest of the update flow.
    """
    return _refresh_features(active_features(), prompt=prompt, restoring=False)


def restore_features(features: list[str]) -> dict[str, str]:
    """Restore features captured before an explicit managed-runtime rebuild.

    Feature names are checked against :data:`LAZY_DEPS`, and installs remain
    subject to ``security.allow_lazy_installs``. An explicit opt-out therefore
    leaves the captured feature absent and reports it as skipped.
    """
    return _refresh_features(features, prompt=False, restoring=True)


def _refresh_features(
    features: list[str], *, prompt: bool, restoring: bool
) -> dict[str, str]:
    """Refresh or restore a known set of allowlisted lazy features."""
    results: dict[str, str] = {}
    for feature in features:
        if feature not in LAZY_DEPS:
            continue
        missing = feature_missing(feature)
        if not missing:
            results[feature] = "current"
            continue

        unsupported = _unsupported_feature_reason(feature)
        if unsupported:
            results[feature] = f"skipped: {unsupported}"
            continue

        try:
            if restoring:
                ensure(feature, prompt=False)
                results[feature] = "restored"
            else:
                ensure(feature, prompt=prompt)
                results[feature] = "refreshed"
        except FeatureUnavailable as e:
            # Distinguish "user opted out" or platform-incompatible features
            # from install failures so the update command can render the
            # right non-error message.
            if (
                "lazy installs disabled" in str(e)
                or "declined" in str(e)
                or e.reason.startswith("unsupported ")
            ):
                results[feature] = f"skipped: {e.reason}"
            elif e.reason.startswith("restart required before lazy repair:"):
                results[feature] = f"restart-required: {e.reason}"
            else:
                results[feature] = f"failed: {e.reason}"
        except Exception as e:
            results[feature] = f"failed: {e}"
    return results


def ensure_and_bind(
    feature: str,
    importer: Callable[[], dict[str, Any]],
    target_globals: dict,
    *,
    prompt: bool = False,
) -> bool:
    """Ensure a feature is installed, then rebind names into the caller's globals.

    Combines :func:`ensure` with a post-install import step that rebinds
    module-level names.  This eliminates the error-prone pattern of manually
    listing every global that needs updating after lazy-install.

    ``importer`` is a zero-arg callable that returns a dict of
    ``{name: value}`` for all symbols the caller needs rebound.  It is called
    only after :func:`ensure` succeeds (or if the packages are already
    installed).

    Returns True on success, False if deps couldn't be installed or imported.

    Example usage in a platform adapter::

        def check_slack_requirements() -> bool:
            if SLACK_AVAILABLE:
                return True
            def _import():
                from slack_bolt.async_app import AsyncApp
                from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
                from slack_sdk.web.async_client import AsyncWebClient
                import aiohttp
                return {
                    "AsyncApp": AsyncApp,
                    "AsyncSocketModeHandler": AsyncSocketModeHandler,
                    "AsyncWebClient": AsyncWebClient,
                    "aiohttp": aiohttp,
                    "SLACK_AVAILABLE": True,
                }
            return ensure_and_bind("platform.slack", _import, globals(), prompt=False)
    """
    try:
        ensure(feature, prompt=prompt)
    except (FeatureUnavailable, Exception):
        return False

    try:
        bindings = importer()
    except Exception:
        return False

    target_globals.update(bindings)
    return True
