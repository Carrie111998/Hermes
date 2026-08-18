"""Regression tests for the stage2 API_SERVER_KEY bootstrap (OOF-285).

The gateway's loopback api_server refuses to start without a strong
API_SERVER_KEY, and hosted cron fires are forwarded through it — so the
stage2 keygen must succeed even when no ``.env`` exists yet. Historically it
was gated on ``[ -f "$HERMES_HOME/.env" ]`` while the first-boot seed that
was supposed to create ``.env`` silently no-oped (``.env.example`` was
excluded from the image by ``.dockerignore``), leaving 40%+ of the hosted
fleet with no api_server and every scheduled cron fire silently lost.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE2_HOOK = REPO_ROOT / "docker" / "stage2-hook.sh"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

KEY_LINE_RE = re.compile(r"^API_SERVER_KEY=[0-9a-f]{64}$", re.MULTILINE)


@pytest.fixture(scope="module")
def stage2_text() -> str:
    if not STAGE2_HOOK.exists():
        pytest.skip("docker/stage2-hook.sh not present in this checkout")
    return STAGE2_HOOK.read_text()


def _keygen_block(text: str) -> str:
    start = text.index("# --- Ensure a gateway api_server key exists")
    end = text.index("# .env holds API keys and secrets", start)
    return text[start:end]


def _path_guard_functions(text: str) -> str:
    start = text.index("path_has_symlink_component() {")
    end = text.index("\n\nchown_hermes_tree() {", start)
    return text[start:end]


def _run_keygen(stage2_text: str, home: Path) -> subprocess.CompletedProcess[str]:
    if shutil.which("sh") is None:
        pytest.skip("sh not available")
    script = (
        "set -u\n"
        f'HERMES_HOME="{home}"\n'
        # In tests we run unprivileged; as_hermes is a passthrough then.
        'as_hermes() { "$@"; }\n'
        f"{_path_guard_functions(stage2_text)}\n"
        f"{_keygen_block(stage2_text)}\n"
    )
    return subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_keygen_creates_env_when_missing(stage2_text: str, tmp_path: Path) -> None:
    """No .env at all (failed/absent first-boot seed) must still yield a key."""
    home = tmp_path / "home"
    home.mkdir()
    result = _run_keygen(stage2_text, home)
    assert result.returncode == 0, result.stderr
    env_path = home / ".env"
    assert env_path.is_file(), "keygen must create .env when it is missing"
    assert KEY_LINE_RE.search(env_path.read_text()), "generated key missing/malformed"
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600, f".env must be owner-only, got {oct(mode)}"


def test_keygen_appends_to_existing_env_without_key(
    stage2_text: str, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("OTHER=1\nAPI_SERVER_KEY=\n")
    result = _run_keygen(stage2_text, home)
    assert result.returncode == 0, result.stderr
    content = (home / ".env").read_text()
    assert "OTHER=1" in content
    assert KEY_LINE_RE.search(content)
    # Exactly one real key assignment. (The stale empty `API_SERVER_KEY=` line
    # is dropped by GNU sed in the production container; BSD sed on macOS dev
    # hosts silently skips the -i invocation, so don't assert its removal.)
    assert len(re.findall(r"^API_SERVER_KEY=..+$", content, re.MULTILINE)) == 1


def test_keygen_never_overwrites_operator_key(
    stage2_text: str, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("API_SERVER_KEY=operator-provided-key-123\n")
    result = _run_keygen(stage2_text, home)
    assert result.returncode == 0, result.stderr
    content = (home / ".env").read_text()
    assert content == "API_SERVER_KEY=operator-provided-key-123\n"


def test_keygen_refuses_symlinked_env(stage2_text: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside.env"
    outside.write_text("HIJACK=1\n")
    (home / ".env").symlink_to(outside)
    result = _run_keygen(stage2_text, home)
    assert result.returncode == 0, result.stderr
    assert "refusing append" in (result.stdout + result.stderr)
    assert outside.read_text() == "HIJACK=1\n", "must not write through symlink"


def test_dockerignore_keeps_env_example_template() -> None:
    """The first-boot seed copies /opt/hermes/.env.example -> $HERMES_HOME/.env.

    ``.env.*`` in .dockerignore matches the template, so an explicit
    ``!.env.example`` re-include must appear AFTER it (last match wins), and
    no later rule may exclude it again (OOF-285).
    """
    if not DOCKERIGNORE.exists():
        pytest.skip(".dockerignore not present in this checkout")
    lines = [ln.strip() for ln in DOCKERIGNORE.read_text().splitlines()]
    rules = [ln for ln in lines if ln and not ln.startswith("#")]
    verdict = "excluded"  # default: not matched -> included
    for rule in rules:
        negate = rule.startswith("!")
        pattern = rule.lstrip("!")
        if pattern in (".env.example",) or _dockerignore_match(pattern):
            verdict = "included" if negate else "excluded"
    assert verdict == "included", (
        ".env.example must survive .dockerignore — docker/stage2-hook.sh "
        "seeds $HERMES_HOME/.env from it on first boot"
    )


def _dockerignore_match(pattern: str) -> bool:
    """Minimal dockerignore glob match of ``pattern`` against '.env.example'."""
    import fnmatch

    if pattern.endswith("/"):
        return False
    return fnmatch.fnmatch(".env.example", pattern)
