"""Tests for the KubernetesComputeDriver (kubectl-backed)."""

from __future__ import annotations

import json
import re

import pytest

from enterprise.contracts import WorkloadRef
from enterprise.drivers.kubernetes import KubernetesComputeDriver
from enterprise.errors import DriverError
from enterprise.resources import Kind, Resource, ResourceMeta


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_revision(**spec_overrides) -> Resource:
    spec = {
        "agent": "support-bot",
        "agentUid": "a" * 32,
        "workloadIdentity": "wi-support-bot",
        "harness": {"name": "hermes", "version": "1.9.0", "image": "ghcr.io/nous/hermes-harness:1.9.0"},
        "configuration": {"config": {"model": "hermes-4"}},
        "computeDriver": "kubernetes",
        "sandboxDriver": "gvisor",
        "sandboxPolicy": {
            "network": "isolated",
            "resources": {
                "requests": {"cpu": "500m", "memory": "512Mi"},
                "limits": {"cpu": "2", "memory": "2Gi"},
            },
        },
    }
    spec.update(spec_overrides)
    res = Resource(
        meta=ResourceMeta(
            kind=Kind.AGENT_REVISION.value,
            name="support-bot-rev-1",
            namespace="acme",
        ),
        spec=spec,
    )
    res.validate()
    return res


class KubectlRecorder:
    """Monkeypatch target for KubernetesComputeDriver._kubectl."""

    def __init__(self, responses=None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.responses = list(responses or [])

    def __call__(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append((list(args), stdin))
        if self.responses:
            resp = self.responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        return ""


def make_driver(recorder: KubectlRecorder, **kwargs) -> KubernetesComputeDriver:
    driver = KubernetesComputeDriver(poll_interval=0.0, **kwargs)
    driver._kubectl = recorder  # type: ignore[method-assign]
    return driver


def make_ref(driver: KubernetesComputeDriver, revision: Resource) -> WorkloadRef:
    return WorkloadRef(
        revision_uid=revision.meta.uid,
        namespace="acme",
        workload_identity=revision.spec["workloadIdentity"],
        driver=driver.name,
        handle={"deployment": "support-bot-" + revision.meta.uid[:12], "k8s_namespace": "hermes-acme"},
    )


def deployment_json(*, generation=3, observed=3, replicas=None, available=None) -> str:
    status = {"observedGeneration": observed}
    if replicas is not None:
        status["replicas"] = replicas
    if available is not None:
        status["availableReplicas"] = available
    return json.dumps(
        {"metadata": {"generation": generation}, "status": status}
    )


# ---------------------------------------------------------------------------
# provision_candidate
# ---------------------------------------------------------------------------


def test_provision_renders_zero_replica_deployment_from_revision():
    revision = make_revision()
    recorder = KubectlRecorder()
    driver = make_driver(recorder)

    ref = driver.provision_candidate(revision)

    # Two applies: ServiceAccount first, then Deployment.
    assert len(recorder.calls) == 2
    for args, _ in recorder.calls:
        assert args == ["apply", "-f", "-"]
    sa = json.loads(recorder.calls[0][1])
    dep = json.loads(recorder.calls[1][1])
    assert sa["kind"] == "ServiceAccount"
    assert dep["kind"] == "Deployment"
    assert sa["metadata"]["name"] == "wi-support-bot"
    assert sa["metadata"]["namespace"] == "hermes-acme"

    # Candidate = harness cannot start = zero replicas.
    assert dep["spec"]["replicas"] == 0
    assert dep["metadata"]["namespace"] == "hermes-acme"

    labels = dep["metadata"]["labels"]
    assert labels["app.hermes/agent"] == "support-bot"
    assert labels["app.hermes/revision-uid"] == revision.meta.uid

    pod = dep["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "wi-support-bot"
    container = pod["containers"][0]
    assert container["image"] == "ghcr.io/nous/hermes-harness:1.9.0"
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env == {
        "HERMES_REVISION_UID": revision.meta.uid,
        "HERMES_NAMESPACE": "acme",
    }
    assert container["resources"] == {
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "2", "memory": "2Gi"},
    }

    # Returned handle points at what was applied.
    assert ref.driver == "kubernetes"
    assert ref.revision_uid == revision.meta.uid
    assert ref.handle["k8s_namespace"] == "hermes-acme"
    assert ref.handle["deployment"] == dep["metadata"]["name"]


def test_provision_without_sandbox_policy_omits_resources():
    revision = make_revision()
    del revision.spec["sandboxPolicy"]
    recorder = KubectlRecorder()
    make_driver(recorder).provision_candidate(revision)
    dep = json.loads(recorder.calls[1][1])
    assert "resources" not in dep["spec"]["template"]["spec"]["containers"][0]


def test_manifests_contain_no_secretlike_env_values():
    revision = make_revision()
    recorder = KubectlRecorder()
    make_driver(recorder).provision_candidate(revision)
    secretlike = re.compile(r"(api[_-]?key|token|password|secret|credential)", re.I)
    for _, stdin in recorder.calls:
        manifest = json.loads(stdin)
        containers = (
            manifest.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        for container in containers:
            for env in container.get("env", []):
                assert not secretlike.search(env["name"]), env
        # Nothing secret-like anywhere in the serialized manifest keys/values.
        assert not secretlike.search(stdin)


# ---------------------------------------------------------------------------
# workload_ready
# ---------------------------------------------------------------------------


def test_workload_ready_true_when_observed_generation_caught_up():
    revision = make_revision()
    recorder = KubectlRecorder([deployment_json(generation=2, observed=2)])
    driver = make_driver(recorder)
    assert driver.workload_ready(make_ref(driver, revision)) is True
    args, _ = recorder.calls[0]
    assert args[:2] == ["get", "deployment"]
    assert "-o" in args and "json" in args
    assert "hermes-acme" in args


def test_workload_ready_false_when_observed_generation_stale():
    revision = make_revision()
    recorder = KubectlRecorder([deployment_json(generation=5, observed=4)])
    driver = make_driver(recorder)
    assert driver.workload_ready(make_ref(driver, revision)) is False


# ---------------------------------------------------------------------------
# start / stop harness
# ---------------------------------------------------------------------------


def test_start_harness_scales_to_one_and_polls_until_available():
    revision = make_revision()
    recorder = KubectlRecorder(
        [
            "",  # scale
            deployment_json(replicas=1, available=None),
            deployment_json(replicas=1, available=0),
            deployment_json(replicas=1, available=1),
        ]
    )
    driver = make_driver(recorder)
    driver.start_harness(make_ref(driver, revision))

    scale_args = recorder.calls[0][0]
    assert scale_args[:2] == ["scale", "deployment"]
    assert "--replicas=1" in scale_args
    # Polled three times before availability.
    gets = [args for args, _ in recorder.calls[1:]]
    assert all(args[:2] == ["get", "deployment"] for args in gets)
    assert len(gets) == 3


def test_start_harness_times_out_with_driver_error():
    revision = make_revision()
    recorder = KubectlRecorder(["", *[deployment_json(replicas=1, available=0)] * 50])
    driver = make_driver(recorder, start_timeout=0.0)
    with pytest.raises(DriverError, match="did not become available"):
        driver.start_harness(make_ref(driver, revision))


def test_stop_harness_scales_to_zero_and_verifies_stopped():
    revision = make_revision()
    recorder = KubectlRecorder(
        [
            "",  # scale
            deployment_json(replicas=1),
            deployment_json(replicas=0),
        ]
    )
    driver = make_driver(recorder)
    driver.stop_harness(make_ref(driver, revision))
    assert "--replicas=0" in recorder.calls[0][0]
    assert len(recorder.calls) == 3  # verified via polling, not assumed


def test_stop_harness_timeout_raises_driver_error():
    revision = make_revision()
    recorder = KubectlRecorder(["", *[deployment_json(replicas=1)] * 50])
    driver = make_driver(recorder, start_timeout=0.0)
    with pytest.raises(DriverError, match="did not stop"):
        driver.stop_harness(make_ref(driver, revision))


# ---------------------------------------------------------------------------
# teardown / error handling
# ---------------------------------------------------------------------------


def test_teardown_deletes_with_ignore_not_found():
    revision = make_revision()
    recorder = KubectlRecorder()
    driver = make_driver(recorder)
    driver.teardown(make_ref(driver, revision))
    args = recorder.calls[0][0]
    assert args[:2] == ["delete", "deployment"]
    assert "--ignore-not-found" in args


def test_kubectl_failure_surfaces_as_driver_error():
    revision = make_revision()
    recorder = KubectlRecorder([DriverError("kubectl apply failed (exit 1): forbidden")])
    driver = make_driver(recorder)
    with pytest.raises(DriverError, match="forbidden"):
        driver.provision_candidate(revision)


def test_real_kubectl_wrapper_nonzero_exit_raises(monkeypatch):
    """Exercise the actual _kubectl subprocess seam (no monkeypatched seam)."""
    import subprocess

    driver = KubernetesComputeDriver(kubectl_path="kubectl")

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "error: the server doesn't have a resource type"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(DriverError, match="exit 1"):
        driver._kubectl(["get", "deployment", "x"])


def test_kubectl_command_includes_kubeconfig_and_context(monkeypatch):
    import subprocess

    seen = {}

    class Proc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    driver = KubernetesComputeDriver(
        kubectl_path="/usr/local/bin/kubectl",
        kubeconfig="/tmp/kc",
        context="prod",
    )
    driver._kubectl(["get", "ns"])
    assert seen["cmd"][:5] == [
        "/usr/local/bin/kubectl",
        "--kubeconfig",
        "/tmp/kc",
        "--context",
        "prod",
    ]
