"""Docker lifecycle for the gateway e2e suite.

Builds (or reuses) the ``hermes-agent`` image, then for each provider boots a
throwaway container running ``gateway run`` with the OpenAI-compatible API
server enabled, an ephemeral ``HERMES_HOME`` (so the real ``~/.hermes`` is
never touched), and the provider's key injected via ``-e``. Waits for the
server to answer ``/health`` on a mapped localhost port, then hands back a
:class:`~tests.e2e.gateway.http_client.GatewayClient`.

Everything shells out to the ``docker`` CLI — no SDK dependency — so the suite
runs anywhere Docker Desktop / engine is on PATH.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from .http_client import GatewayClient
from .providers import ResolvedProvider

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGE_TAG = os.environ.get("HERMES_E2E_IMAGE", "hermes-agent")
CONTAINER_PORT = 8642  # gateway API server's in-container listen port
API_KEY = "e2e-probe-key"
# Build can be slow (Node, Playwright, Python deps); first boot also runs config
# migration, skills sync, and runtime lazy-dep installs. Both are generously
# bounded and env-overridable.
BUILD_TIMEOUT = float(os.environ.get("HERMES_E2E_BUILD_TIMEOUT", "1800"))
READY_TIMEOUT = float(os.environ.get("HERMES_E2E_READY_TIMEOUT", "300"))


class DockerUnavailable(RuntimeError):
    """Raised when the docker CLI is missing or the daemon is unreachable."""


def docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _image_exists(tag: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", tag], capture_output=True
    )
    return result.returncode == 0


def ensure_image(*, rebuild: bool = False) -> str:
    """Build ``IMAGE_TAG`` from the repo root unless it already exists.

    Set ``HERMES_E2E_REBUILD=1`` to force a rebuild (e.g. after pulling
    upstream changes — the whole point of this suite).
    """
    rebuild = rebuild or os.environ.get("HERMES_E2E_REBUILD") == "1"
    if _image_exists(IMAGE_TAG) and not rebuild:
        return IMAGE_TAG
    print(f"\n[e2e] building image {IMAGE_TAG} from {REPO_ROOT} ...", flush=True)
    subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "."],
        cwd=REPO_ROOT,
        check=True,
        timeout=BUILD_TIMEOUT,
    )
    return IMAGE_TAG


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_config(home: Path, provider: ResolvedProvider) -> None:
    lines = [
        "model:",
        f'  provider: "{provider.spec.id}"',
        f'  default: "{provider.model}"',
    ]
    if provider.spec.base_url:
        lines.append(f'  base_url: "{provider.spec.base_url}"')
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class GatewayContainer:
    """A single dockerized gateway bound to one provider."""

    def __init__(self, provider: ResolvedProvider, home: Path):
        self.provider = provider
        self.home = home
        self.host_port = _free_port()
        self.name = f"hermes-e2e-{provider.id}-{uuid.uuid4().hex[:8]}"
        self.client = GatewayClient(
            base_url=f"http://127.0.0.1:{self.host_port}", api_key=API_KEY
        )

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> GatewayClient:
        self.home.mkdir(parents=True, exist_ok=True)
        _write_config(self.home, self.provider)
        cmd = [
            "docker", "run", "-d", "--name", self.name,
            "-e", f"HERMES_UID={os.getuid()}",
            "-e", f"HERMES_GID={os.getgid()}",
            "-e", "API_SERVER_ENABLED=true",
            "-e", f"API_SERVER_KEY={API_KEY}",
            "-e", "API_SERVER_HOST=0.0.0.0",
            "-e", f"API_SERVER_PORT={CONTAINER_PORT}",
        ]
        for key, value in self.provider.container_env.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [
            "-v", f"{self.home}:/opt/data",
            "-p", f"127.0.0.1:{self.host_port}:{CONTAINER_PORT}",
            IMAGE_TAG, "gateway", "run",
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        try:
            self._wait_ready()
        except Exception:
            self._dump_logs()
            self.stop()
            raise
        return self.client

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        start = time.monotonic()
        last = ""
        attempt = 0
        print(
            f"[e2e] waiting for {self.name} ({self.provider.id}) /health on "
            f"127.0.0.1:{self.host_port} (up to {READY_TIMEOUT:.0f}s) ...",
            flush=True,
        )
        while time.monotonic() < deadline:
            if not self._running():
                self._dump_logs()
                raise RuntimeError(f"container {self.name} exited before becoming ready")
            try:
                resp = self.client.get("/health", auth=False)
                if resp.status == 200:
                    print(f"[e2e] {self.name} ready after {time.monotonic() - start:.0f}s", flush=True)
                    return
                last = f"HTTP {resp.status}"
            except (OSError, http.client.HTTPException) as err:
                # During boot the docker port-proxy accepts then closes the
                # connection (RemoteDisconnected) or refuses it (URLError);
                # both are transient — keep polling. (URLError ⊂ OSError.)
                last = f"{type(err).__name__}: {err}".rstrip(": ")
            attempt += 1
            if attempt % 5 == 0:
                print(
                    f"[e2e]   still booting ({time.monotonic() - start:.0f}s, last: {last})",
                    flush=True,
                )
            time.sleep(2.0)
        self._dump_logs()
        raise TimeoutError(
            f"gateway {self.name} ({self.provider.id}) not ready after "
            f"{READY_TIMEOUT:.0f}s (last: {last})"
        )

    def _running(self) -> bool:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.name],
            capture_output=True, text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _dump_logs(self) -> None:
        logs = subprocess.run(
            ["docker", "logs", "--tail", "200", self.name],
            capture_output=True, text=True,
        )
        banner = f"---- docker logs ({self.name}) ----"
        print(f"\n{banner}\n{logs.stdout}\n{logs.stderr}\n{'-' * len(banner)}", flush=True)

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
