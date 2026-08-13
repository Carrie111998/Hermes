"""Kubernetes baseline SandboxDriver.

Enforces the admitted SandboxPolicy for one candidate workload with two
mechanisms:

  * NetworkPolicy — ``network: isolated`` renders a default-deny policy
    (both directions, no rules); ``network: egress-allowlist`` renders a
    default-deny-ingress policy whose egress section is built from the
    ``egressAllow`` ``host:port`` entries; ``network: open`` renders no
    NetworkPolicy at all (and removes a stale one on enforce).
  * securityContext — the Deployment pod template is patched to run as
    non-root under the RuntimeDefault seccomp profile, with all
    capabilities dropped and the policy's ``readOnlyRootFilesystem`` /
    ``allowPrivilegeEscalation`` booleans applied to every container.

``verify`` re-reads the live objects via ``kubectl get -o json`` and
independently compares them against what the policy requires. Any
mismatch, missing object, or kubectl failure raises DriverError:
unverifiable containment blocks activation.

All cluster access flows through the single ``_kubectl`` seam so tests
(and alternative transports) can substitute it wholesale.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from typing import Any

from ..contracts import SandboxDriver, WorkloadRef
from ..errors import DriverError

#: Policy keys this driver understands. Anything else => unsupported.
_KNOWN_KEYS = frozenset(
    {"network", "egressAllow", "readOnlyRootFilesystem", "allowPrivilegeEscalation"}
)

_NETWORK_MODES = ("isolated", "egress-allowlist", "open")

#: Label the compute driver stamps on candidate pods.
_REVISION_LABEL = "app.hermes/revision-uid"

#: Annotation carrying the canonical allowlist for hostname entries that a
#: vanilla NetworkPolicy cannot express as an ipBlock. Verification treats
#: it as part of the enforced state.
_EGRESS_ANNOTATION = "policy.hermes/egress-allow"


def _parse_egress_entry(entry: Any) -> tuple[str, int]:
    """Parse one ``host:port`` allowlist entry. Raise ValueError if the
    entry is malformed or names a host this driver cannot express."""
    if not isinstance(entry, str) or ":" not in entry:
        raise ValueError(f"egress entry {entry!r} is not 'host:port'")
    host, _, port_s = entry.rpartition(":")
    host = host.strip()
    if not host or "*" in host:
        # Wildcards cannot be expressed by a NetworkPolicy ipBlock/rule.
        raise ValueError(f"egress host {host!r} is not expressible")
    try:
        port = int(port_s)
    except ValueError as exc:
        raise ValueError(f"egress port {port_s!r} is not an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"egress port {port} out of range")
    return host, port


class K8sSandboxDriver(SandboxDriver):
    """Baseline containment on Kubernetes via NetworkPolicy + securityContext."""

    name = "k8s-baseline"

    def __init__(self, *, kubectl: str = "kubectl", context: str | None = None) -> None:
        self._binary = kubectl
        self._context = context

    # ------------------------------------------------------------------ seam

    def _kubectl(self, args: list[str], *, input_data: str | None = None) -> str:
        """Run one kubectl invocation and return stdout. DriverError on any
        failure. Single seam: every cluster interaction goes through here."""
        cmd = [self._binary]
        if self._context:
            cmd += ["--context", self._context]
        cmd += args
        try:
            proc = subprocess.run(
                cmd, input=input_data, capture_output=True, text=True, timeout=120
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DriverError(f"kubectl invocation failed: {exc}") from exc
        if proc.returncode != 0:
            raise DriverError(
                f"kubectl {' '.join(args)} failed (rc={proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        return proc.stdout

    # ------------------------------------------------------------- contracts

    def supports(self, policy: dict[str, Any]) -> bool:
        if not isinstance(policy, dict):
            return False
        if set(policy) - _KNOWN_KEYS:
            return False  # partial support is unsupported
        network = policy.get("network", "isolated")
        if network not in _NETWORK_MODES:
            return False
        allow = policy.get("egressAllow")
        if network == "egress-allowlist":
            if not isinstance(allow, list) or not allow:
                return False
            try:
                for entry in allow:
                    _parse_egress_entry(entry)
            except ValueError:
                return False
        elif allow is not None:
            return False  # an allowlist is meaningless outside egress-allowlist
        for key in ("readOnlyRootFilesystem", "allowPrivilegeEscalation"):
            if key in policy and not isinstance(policy[key], bool):
                return False
        return True

    def enforce(self, ref: WorkloadRef, policy: dict[str, Any]) -> None:
        if not self.supports(policy):
            raise DriverError(
                f"driver {self.name!r} cannot enforce the full policy: {policy!r}"
            )
        network = policy.get("network", "isolated")
        np_name = self._netpol_name(ref)
        if network == "open":
            # No NetworkPolicy for open — remove any stale one.
            self._kubectl(
                [
                    "delete", "networkpolicy", np_name,
                    "-n", ref.namespace, "--ignore-not-found",
                ]
            )
        else:
            manifest = self._render_network_policy(ref, policy)
            self._kubectl(
                ["apply", "-n", ref.namespace, "-f", "-"],
                input_data=json.dumps(manifest),
            )
        self._patch_security_context(ref, policy)

    def verify(self, ref: WorkloadRef, policy: dict[str, Any]) -> None:
        if not self.supports(policy):
            raise DriverError(
                f"driver {self.name!r} cannot verify a policy it does not "
                f"support: {policy!r}"
            )
        self._verify_network(ref, policy)
        self._verify_security_context(ref, policy)

    # ------------------------------------------------------------- rendering

    def _revision_uid(self, ref: WorkloadRef) -> str:
        return str(ref.handle.get("revision_uid") or ref.revision_uid)

    def _netpol_name(self, ref: WorkloadRef) -> str:
        return f"hermes-sandbox-{self._revision_uid(ref)}"

    def _deployment_name(self, ref: WorkloadRef) -> str:
        name = ref.handle.get("deployment")
        if not name:
            raise DriverError(
                "workload handle carries no 'deployment' id; cannot enforce "
                "securityContext"
            )
        return str(name)

    def _expected_egress_rules(self, policy: dict[str, Any]) -> list[dict[str, Any]]:
        rules: list[dict[str, Any]] = []
        for entry in sorted(set(policy.get("egressAllow", []))):
            host, port = _parse_egress_entry(entry)
            ports = [{"protocol": "TCP", "port": port}]
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                # Non-wildcard hostname: enforce the port; the hostname itself
                # is recorded in the policy annotation (verified below).
                rules.append({"ports": ports})
            else:
                cidr = f"{host}/32" if ip.version == 4 else f"{host}/128"
                rules.append({"to": [{"ipBlock": {"cidr": cidr}}], "ports": ports})
        return rules

    def _expected_annotation(self, policy: dict[str, Any]) -> str:
        return json.dumps(sorted(set(policy.get("egressAllow", []))))

    def _render_network_policy(
        self, ref: WorkloadRef, policy: dict[str, Any]
    ) -> dict[str, Any]:
        network = policy.get("network", "isolated")
        spec: dict[str, Any] = {
            "podSelector": {
                "matchLabels": {_REVISION_LABEL: self._revision_uid(ref)}
            },
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
        }
        annotations = {_EGRESS_ANNOTATION: self._expected_annotation(policy)}
        if network == "isolated":
            spec["egress"] = []
        else:  # egress-allowlist
            spec["egress"] = self._expected_egress_rules(policy)
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": self._netpol_name(ref),
                "namespace": ref.namespace,
                "labels": {_REVISION_LABEL: self._revision_uid(ref)},
                "annotations": annotations,
            },
            "spec": spec,
        }

    def _expected_pod_security_context(self) -> dict[str, Any]:
        return {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        }

    def _expected_container_security_context(
        self, policy: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "readOnlyRootFilesystem": bool(policy.get("readOnlyRootFilesystem", True)),
            "allowPrivilegeEscalation": bool(
                policy.get("allowPrivilegeEscalation", False)
            ),
            "capabilities": {"drop": ["ALL"]},
        }

    # ----------------------------------------------------------- enforcement

    def _get_deployment(self, ref: WorkloadRef) -> dict[str, Any]:
        out = self._kubectl(
            [
                "get", "deployment", self._deployment_name(ref),
                "-n", ref.namespace, "-o", "json",
            ]
        )
        try:
            obj = json.loads(out)
        except json.JSONDecodeError as exc:
            raise DriverError(f"unparseable deployment readback: {exc}") from exc
        if not isinstance(obj, dict):
            raise DriverError("unparseable deployment readback: not an object")
        return obj

    def _patch_security_context(self, ref: WorkloadRef, policy: dict[str, Any]) -> None:
        deployment = self._get_deployment(ref)
        containers = (
            deployment.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        names = [c.get("name") for c in containers if c.get("name")]
        if not names:
            raise DriverError(
                f"deployment {self._deployment_name(ref)!r} has no named "
                "containers to contain"
            )
        csc = self._expected_container_security_context(policy)
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "securityContext": self._expected_pod_security_context(),
                        "containers": [
                            {"name": n, "securityContext": dict(csc)} for n in names
                        ],
                    }
                }
            }
        }
        self._kubectl(
            [
                "patch", "deployment", self._deployment_name(ref),
                "-n", ref.namespace,
                "--type", "strategic",
                "-p", json.dumps(patch),
            ]
        )

    # ----------------------------------------------------------- verification

    def _verify_network(self, ref: WorkloadRef, policy: dict[str, Any]) -> None:
        network = policy.get("network", "isolated")
        np_name = self._netpol_name(ref)
        if network == "open":
            # Open means NO NetworkPolicy may target this workload.
            out = self._kubectl(
                [
                    "get", "networkpolicy", np_name,
                    "-n", ref.namespace, "-o", "json", "--ignore-not-found",
                ]
            )
            if out.strip():
                raise DriverError(
                    f"network=open but NetworkPolicy {np_name!r} exists"
                )
            return

        out = self._kubectl(
            ["get", "networkpolicy", np_name, "-n", ref.namespace, "-o", "json"]
        )
        try:
            obj = json.loads(out)
        except json.JSONDecodeError as exc:
            raise DriverError(f"unparseable NetworkPolicy readback: {exc}") from exc
        spec = obj.get("spec") or {}

        selector = (spec.get("podSelector") or {}).get("matchLabels") or {}
        if selector.get(_REVISION_LABEL) != self._revision_uid(ref):
            raise DriverError(
                f"NetworkPolicy {np_name!r} does not select this revision "
                f"(selector={selector!r})"
            )
        if set(spec.get("policyTypes") or []) != {"Ingress", "Egress"}:
            raise DriverError(
                f"NetworkPolicy {np_name!r} policyTypes must cover Ingress "
                f"and Egress, got {spec.get('policyTypes')!r}"
            )
        if spec.get("ingress"):
            raise DriverError(
                f"NetworkPolicy {np_name!r} carries ingress rules; ingress "
                "must be default-deny"
            )

        actual = self._rule_set(spec.get("egress") or [])
        expected = self._rule_set(
            [] if network == "isolated" else self._expected_egress_rules(policy)
        )
        if actual != expected:
            raise DriverError(
                f"NetworkPolicy {np_name!r} egress rules drifted from policy: "
                f"applied={sorted(actual)!r} expected={sorted(expected)!r}"
            )

        if network == "egress-allowlist":
            annotations = (obj.get("metadata") or {}).get("annotations") or {}
            if annotations.get(_EGRESS_ANNOTATION) != self._expected_annotation(policy):
                raise DriverError(
                    f"NetworkPolicy {np_name!r} allowlist annotation drifted "
                    "from policy"
                )

    @staticmethod
    def _rule_set(rules: list[dict[str, Any]]) -> set[str]:
        return {json.dumps(rule, sort_keys=True) for rule in rules}

    def _verify_security_context(
        self, ref: WorkloadRef, policy: dict[str, Any]
    ) -> None:
        deployment = self._get_deployment(ref)
        pod_spec = deployment.get("spec", {}).get("template", {}).get("spec", {})

        pod_sc = pod_spec.get("securityContext") or {}
        for key, want in self._expected_pod_security_context().items():
            if pod_sc.get(key) != want:
                raise DriverError(
                    f"pod securityContext.{key} is {pod_sc.get(key)!r}, "
                    f"expected {want!r}"
                )

        containers = pod_spec.get("containers") or []
        if not containers:
            raise DriverError("deployment readback has no containers")
        want_csc = self._expected_container_security_context(policy)
        for container in containers:
            csc = container.get("securityContext") or {}
            for key, want in want_csc.items():
                got = csc.get(key)
                if key == "capabilities":
                    got_drop = set((got or {}).get("drop") or [])
                    if got_drop != set(want["drop"]):
                        raise DriverError(
                            f"container {container.get('name')!r} does not "
                            f"drop ALL capabilities (drop={sorted(got_drop)!r})"
                        )
                elif got != want:
                    raise DriverError(
                        f"container {container.get('name')!r} "
                        f"securityContext.{key} is {got!r}, expected {want!r}"
                    )
