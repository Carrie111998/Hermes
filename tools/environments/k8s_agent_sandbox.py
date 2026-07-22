"""Agent Sandbox execution environment.

Uses the k8s-agent-sandbox Python SDK to run commands in cloud sandboxes.
Supports persistent sandboxes: when enabled, sandboxes are stopped on cleanup
and resumed on next creation, preserving the filesystem across sessions.
"""

import logging
import math
import os
import shlex
import threading
from typing import TypedDict
from pathlib import Path

from tools.environments.base import (
    BaseEnvironment,
    _ThreadedProcessHandle,
)
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_mkdir_command,
    quoted_rm_command,
    unique_parent_dirs,
)

logger = logging.getLogger(__name__)


class ConnectionConfigParams(TypedDict):
    name: str
    port_forward_ready_timeout: int
    server_port: int
    router_namespace: str
    api_url: str
    gateway_name: str
    gateway_namespace: str
    gateway_ready_timeout: int
    use_pod_ip: bool


class K8sSandboxBackend(BaseEnvironment):
    """K8s-agent-sandbox cloud sandbox execution backend.

    Spawn-per-call via _ThreadedProcessHandle wrapping blocking SDK calls.
    cancel_fn wired to sandbox.stop() for interrupt support.
    Shell timeout wrapper preserved (SDK timeout unreliable).
    """
    def __init__(
        self,
        cwd: str,
        connection_config_args: ConnectionConfigParams,
        timeout: int = 60,
        task_id: str = "default",
        persistent_filesystem: bool = False
    ):
        super().__init__(cwd=cwd, timeout=timeout)

        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("terminal.k8s_agent_sandbox", prompt=False)
        except ImportError:
            pass
        except Exception as e:
            raise ImportError(str(e))
        from k8s_agent_sandbox import (
            SandboxClient,
        )
        from k8s_agent_sandbox.models import (
            SandboxDirectConnectionConfig,
            SandboxGatewayConnectionConfig,
            SandboxLocalTunnelConnectionConfig,
            SandboxInClusterConnectionConfig,
        )

        config_name = connection_config_args.get("name", "SandboxLocalTunnelConnectionConfig")
        if config_name == "SandboxLocalTunnelConnectionConfig":
            connection_config = SandboxLocalTunnelConnectionConfig(
                port_forward_ready_timeout=connection_config_args.get("port_forward_ready_timeout", 30),
                server_port=connection_config_args.get("server_port", 8888),
                router_namespace=connection_config_args.get("router_namespace", "agent-sandbox-system"),
            )
        elif config_name == "SandboxDirectConnectionConfig":
            connection_config = SandboxDirectConnectionConfig(
                api_url=connection_config_args.get("api_url", ""),
                server_port=connection_config_args.get("server_port", 8888),
            )
        elif config_name == "SandboxGatewayConnectionConfig":
            connection_config = SandboxGatewayConnectionConfig(
                gateway_name=connection_config_args.get("gateway_name", ""),
                gateway_namespace=connection_config_args.get("gateway_namespace", "default"),
                gateway_ready_timeout=connection_config_args.get("gateway_ready_timeout", 100),
                server_port=connection_config_args.get("server_port", 8888),
            )
        elif config_name == "SandboxInClusterConnectionConfig":
            connection_config = SandboxInClusterConnectionConfig(
                server_port=connection_config_args.get("port_forward_ready_timeout", 8888),
                use_pod_ip=connection_config_args.get("use_pod_ip", False),
            )
        else:
            raise ValueError(f"Not allowed connection config name: \"{config_name}\"")

        self._remote_home = ""
        self._task_id = task_id
        self._lock = threading.Lock()
        self._persistent = persistent_filesystem
        self._sandbox = None
        self.client = SandboxClient(connection_config=connection_config)

        if self._persistent:
            try:
                claim_name = self.client.list_all_sandboxes(label_selector=f"hermes_task_id={task_id}")[0]
                self._sandbox = self.client.get_sandbox(claim_name)
            except IndexError:
                logger.info(f"k8s-agent-sandbox: The requested sandbox with label_selector=\"hermes_task_id={task_id}\" wasn't found.")
                self._sandbox = None
            except Exception as e:
                logger.warning(f"k8s-agent-sandbox: Error: {e}\nhermes_task_id={task_id}")
                self._sandbox = None
        if self._sandbox is None:
            self._sandbox = self.client.create_sandbox(
                warmpool="simple-sandbox-warmpool",
                labels={"hermes_task_id": task_id},
            )
        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
            upload_fn=self._k8s_agent_sandbox_upload,
            delete_fn=self._k8s_agent_sandbox_delete,
            bulk_upload_fn=self._k8s_agent_sandbox_bulk_upload,
            bulk_download_fn=self._k8s_agent_sandbox_bulk_download,
        )
        self._sync_manager.sync(force=True)
        self.init_session()

    def _k8s_agent_sandbox_upload(self, host_path: str, remote_path: str):
        """Upload a single file via k8s-agent-sandbox Python SDK."""
        with open(host_path, "rb") as fi:
            content = fi.read()
        self._sandbox.files.write(path=remote_path, content=content)

    def _k8s_agent_sandbox_bulk_upload(self, files: list[tuple[str, str]]):
        if not files:
            return
        for host_path, remote_path in files:
            self._k8s_agent_sandbox_upload(host_path, remote_path)

    def _k8s_agent_sandbox_bulk_download(self, dest: Path):
        """Download remote .hermes/ dir as a tar archive."""
        rel_base = f"{self._remote_home}/.hermes".lstrip("/")
        rel_remote_tar = f"{rel_base}_sync.{os.getpid()}.tar"
        self._sandbox.commands.run(
            f"tar cf {shlex.quote(rel_remote_tar)} -C /app {shlex.quote(rel_base)}"
        )
        content = self._sandbox.files.read(rel_remote_tar)
        print("dest", dest)
        with open(dest, "wb") as fo:
            fo.write(content)
        try:
            self._sandbox.commands.run(f"bash -c \"rm -f {shlex.quote(rel_remote_tar)}\"")
        except Exception:
            pass

    def _k8s_agent_sandbox_delete(self, remote_paths: list[str]):
        self._sandbox.commands.run(quoted_rm_command(remote_paths))

    def _before_execute(self):
        """Syncs files via FileSyncManager."""
        self._sync_manager.sync()

    def _run_bash(
        self, cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None
    ):
        sandbox = self._sandbox

        if login:
            shell_cmd = f"bash -l -c {shlex.quote(cmd_string)}"
        else:
            shell_cmd = f"bash -c {shlex.quote(cmd_string)}"

        def exec_fn() -> tuple[str, int]:
            response = sandbox.commands.run(shell_cmd, timeout=timeout)
            return (response.stdout or response.stderr, response.exit_code)
        return _ThreadedProcessHandle(exec_fn=exec_fn)

    def cleanup(self):
        with self._lock:
            if self._sandbox is None:
                return

            if self._sync_manager:
                logger.info("k8s-agent-sandbox: syncing files from sandbox...")
                try:
                    self._sync_manager.sync_back()
                except Exception as e:
                    logger.warning("k8s-agent-sandbox: sync_back failed: %s", e)

            try:
                if not self._persistent:
                    claim_name = self._sandbox.claim_name
                    self._sandbox.terminate()
                    logger.info(f"k8s-agent-sandbox: deleted sandbox with claim name \"{claim_name}\"")
                else:
                    self._sandbox.close_connection()
                self._sandbox = None
                logger.info(f"k8s-agent-sandbox: clean up succeeded")
            except Exception as e:
                logger.warning(f"k8s-agent-sandbox: cleanup failed: {e}")
