"""Kubernetes ComputeDriver — provisions candidate workloads via kubectl.

Design notes:

  * Shells out to ``kubectl`` (binary path configurable); no python
    kubernetes client dependency, stdlib only.
  * A *candidate* workload is a Deployment with ``replicas=0`` — the harness
    literally cannot start until the controller calls :meth:`start_harness`,
    which scales to 1 only after containment is verified.
  * Every kubectl invocation goes through :meth:`_kubectl` so tests can
    monkeypatch the seam and no other subprocess path exists.
  * Manifests never embed secret values. The pod only receives non-secret
    platform coordinates (revision UID, namespace); secret access is always
    brokered out-of-band by a SecretDriver.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

from ..contracts import ComputeDriver, WorkloadRef
from ..errors import DriverError
from ..resources import Resource

#: Prefix that maps a platform Namespace onto its backing k8s namespace.
K8S_NAMESPACE_PREFIX = "hermes-"

_LABEL_AGENT = "app.hermes/agent"
_LABEL_REVISION_UID = "app.hermes/revision-uid"


class KubernetesComputeDriver(ComputeDriver):
    """Provisions AgentRevision workloads as Kubernetes Deployments."""

    name = "kubernetes"

    def __init__(
        self,
        *,
        kubectl_path: str = "kubectl",
        kubeconfig: str | None = None,
        context: str | None = None,
        start_timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> None:
        self.kubectl_path = kubectl_path
        self.kubeconfig = kubeconfig
        self.context = context
        self.start_timeout = start_timeout
        self.poll_interval = poll_interval

    # -- kubectl seam -----------------------------------------------------

    def _kubectl(self, args: list[str], stdin: str | None = None) -> str:
        """Run one kubectl command; return stdout or raise DriverError."""
        cmd = [self.kubectl_path]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            cmd += ["--context", self.context]
        cmd += args
        try:
            proc = subprocess.run(
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:  # kubectl binary missing/unrunnable
            raise DriverError(f"kubectl unavailable: {exc}") from exc
        if proc.returncode != 0:
            raise DriverError(
                f"kubectl {' '.join(args)} failed "
                f"(exit {proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    # -- naming / rendering ------------------------------------------------

    @staticmethod
    def _k8s_namespace(platform_namespace: str) -> str:
        return f"{K8S_NAMESPACE_PREFIX}{platform_namespace}"

    @staticmethod
    def _deployment_name(revision: Resource) -> str:
        return f"{revision.spec['agent']}-{revision.meta.uid[:12]}"

    def _render_service_account(self, revision: Resource) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": revision.spec["workloadIdentity"],
                "namespace": self._k8s_namespace(revision.meta.namespace or ""),
                "labels": {_LABEL_AGENT: revision.spec["agent"]},
            },
        }

    def _render_deployment(self, revision: Resource) -> dict[str, Any]:
        spec = revision.spec
        k8s_ns = self._k8s_namespace(revision.meta.namespace or "")
        name = self._deployment_name(revision)
        labels = {
            _LABEL_AGENT: spec["agent"],
            _LABEL_REVISION_UID: revision.meta.uid,
        }
        container: dict[str, Any] = {
            "name": "harness",
            "image": spec["harness"]["image"],
            "env": [
                {"name": "HERMES_REVISION_UID", "value": revision.meta.uid},
                {"name": "HERMES_NAMESPACE", "value": revision.meta.namespace},
            ],
        }
        resources = self._render_resources(spec.get("sandboxPolicy") or {})
        if resources:
            container["resources"] = resources
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": k8s_ns, "labels": dict(labels)},
            "spec": {
                # Candidate workloads must not run the harness: zero replicas.
                "replicas": 0,
                "selector": {"matchLabels": dict(labels)},
                "template": {
                    "metadata": {"labels": dict(labels)},
                    "spec": {
                        "serviceAccountName": spec["workloadIdentity"],
                        "containers": [container],
                    },
                },
            },
        }

    @staticmethod
    def _render_resources(policy: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for section in ("requests", "limits"):
            values = policy.get("resources", {}).get(section)
            if isinstance(values, dict) and values:
                out[section] = dict(values)
        return out

    # -- ComputeDriver interface --------------------------------------------

    def provision_candidate(self, revision: Resource) -> WorkloadRef:
        k8s_ns = self._k8s_namespace(revision.meta.namespace or "")
        # ServiceAccount first: the Deployment's pod spec references it.
        self._kubectl(
            ["apply", "-f", "-"],
            stdin=json.dumps(self._render_service_account(revision)),
        )
        self._kubectl(
            ["apply", "-f", "-"],
            stdin=json.dumps(self._render_deployment(revision)),
        )
        return WorkloadRef(
            revision_uid=revision.meta.uid,
            namespace=revision.meta.namespace or "",
            workload_identity=revision.spec["workloadIdentity"],
            driver=self.name,
            handle={
                "deployment": self._deployment_name(revision),
                "k8s_namespace": k8s_ns,
            },
        )

    def _get_deployment(self, ref: WorkloadRef) -> dict[str, Any]:
        out = self._kubectl(
            [
                "get",
                "deployment",
                ref.handle["deployment"],
                "-n",
                ref.handle["k8s_namespace"],
                "-o",
                "json",
            ]
        )
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise DriverError(f"unparseable deployment state: {exc}") from exc

    def workload_ready(self, ref: WorkloadRef) -> bool:
        obj = self._get_deployment(ref)
        generation = obj.get("metadata", {}).get("generation", 0)
        observed = obj.get("status", {}).get("observedGeneration", -1)
        return observed >= generation

    def start_harness(self, ref: WorkloadRef) -> None:
        self._scale(ref, 1)
        deadline = time.monotonic() + self.start_timeout
        while True:
            status = self._get_deployment(ref).get("status", {})
            if status.get("availableReplicas") == 1:
                return
            if time.monotonic() >= deadline:
                raise DriverError(
                    f"harness for revision {ref.revision_uid} did not become "
                    f"available within {self.start_timeout}s"
                )
            time.sleep(self.poll_interval)

    def stop_harness(self, ref: WorkloadRef) -> None:
        self._scale(ref, 0)
        deadline = time.monotonic() + self.start_timeout
        while True:
            status = self._get_deployment(ref).get("status", {})
            if not status.get("replicas"):  # 0 or absent => stopped
                return
            if time.monotonic() >= deadline:
                raise DriverError(
                    f"harness for revision {ref.revision_uid} did not stop "
                    f"within {self.start_timeout}s"
                )
            time.sleep(self.poll_interval)

    def teardown(self, ref: WorkloadRef) -> None:
        self._kubectl(
            [
                "delete",
                "deployment",
                ref.handle["deployment"],
                "-n",
                ref.handle["k8s_namespace"],
                "--ignore-not-found",
            ]
        )

    # -- internals ----------------------------------------------------------

    def _scale(self, ref: WorkloadRef, replicas: int) -> None:
        self._kubectl(
            [
                "scale",
                "deployment",
                ref.handle["deployment"],
                "-n",
                ref.handle["k8s_namespace"],
                f"--replicas={replicas}",
            ]
        )
