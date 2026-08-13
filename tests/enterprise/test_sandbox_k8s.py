"""Tests for the k8s-baseline SandboxDriver (enterprise/drivers/sandbox_k8s.py)."""

from __future__ import annotations

import json

import pytest

from enterprise.contracts import WorkloadRef
from enterprise.drivers.sandbox_k8s import K8sSandboxDriver
from enterprise.errors import DriverError

UID = "rev123abc"


def make_ref(**handle_extra) -> WorkloadRef:
    handle = {"deployment": "agent-demo", "revision_uid": UID}
    handle.update(handle_extra)
    return WorkloadRef(
        revision_uid=UID,
        namespace="team-a",
        workload_identity="wi-demo",
        driver="k8s-baseline",
        handle=handle,
    )


def deployment_json(pod_sc=None, containers=None) -> str:
    if containers is None:
        containers = [
            {
                "name": "harness",
                "securityContext": {
                    "readOnlyRootFilesystem": True,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
            }
        ]
    if pod_sc is None:
        pod_sc = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
    return json.dumps(
        {
            "kind": "Deployment",
            "metadata": {"name": "agent-demo", "namespace": "team-a"},
            "spec": {
                "template": {
                    "spec": {"securityContext": pod_sc, "containers": containers}
                }
            },
        }
    )


class Recorder:
    """Monkeypatched _kubectl: records calls, replays canned responses."""

    def __init__(self, responses):
        # responses: list of (predicate-substring-tuple, output-or-exception)
        self.responses = list(responses)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, *, input_data=None):
        self.calls.append((list(args), input_data))
        for needles, result in self.responses:
            if all(n in args for n in needles):
                if isinstance(result, Exception):
                    raise result
                return result
        return ""

    def applied_manifests(self):
        return [
            json.loads(inp) for args, inp in self.calls if "apply" in args and inp
        ]

    def patches(self):
        out = []
        for args, _ in self.calls:
            if args and args[0] == "patch":
                out.append(json.loads(args[args.index("-p") + 1]))
        return out


@pytest.fixture()
def driver():
    return K8sSandboxDriver()


def install(monkeypatch, driver, responses=()):
    rec = Recorder(responses)
    monkeypatch.setattr(driver, "_kubectl", rec)
    return rec


# ---------------------------------------------------------------- supports()


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({}, True),  # defaults: isolated
        ({"network": "isolated"}, True),
        ({"network": "open"}, True),
        ({"network": "egress-allowlist", "egressAllow": ["10.0.0.5:443"]}, True),
        (
            {
                "network": "egress-allowlist",
                "egressAllow": ["api.example.com:443", "10.1.2.3:8443"],
                "readOnlyRootFilesystem": True,
                "allowPrivilegeEscalation": False,
            },
            True,
        ),
        ({"network": "isolated", "readOnlyRootFilesystem": True}, True),
        # unknown key -> unsupported (partial support is unsupported)
        ({"network": "isolated", "gpuPassthrough": True}, False),
        # unknown network mode
        ({"network": "vpn-only"}, False),
        # wildcard hosts cannot be expressed by a NetworkPolicy
        ({"network": "egress-allowlist", "egressAllow": ["*:443"]}, False),
        ({"network": "egress-allowlist", "egressAllow": ["*.example.com:443"]}, False),
        # malformed entries
        ({"network": "egress-allowlist", "egressAllow": ["no-port"]}, False),
        ({"network": "egress-allowlist", "egressAllow": ["host:notaport"]}, False),
        ({"network": "egress-allowlist", "egressAllow": ["host:70000"]}, False),
        ({"network": "egress-allowlist", "egressAllow": []}, False),
        ({"network": "egress-allowlist"}, False),
        # allowlist outside egress-allowlist mode is meaningless
        ({"network": "isolated", "egressAllow": ["10.0.0.5:443"]}, False),
        # non-bool booleans
        ({"readOnlyRootFilesystem": "yes"}, False),
    ],
)
def test_supports_truth_table(driver, policy, expected):
    assert driver.supports(policy) is expected


def test_supports_non_dict(driver):
    assert driver.supports(None) is False  # type: ignore[arg-type]


# ----------------------------------------------------------------- enforce()


def test_enforce_isolated_renders_default_deny(monkeypatch, driver):
    rec = install(monkeypatch, driver, [(("get", "deployment"), deployment_json())])
    driver.enforce(make_ref(), {"network": "isolated"})

    (manifest,) = rec.applied_manifests()
    assert manifest["kind"] == "NetworkPolicy"
    assert manifest["metadata"]["name"] == f"hermes-sandbox-{UID}"
    spec = manifest["spec"]
    assert spec["podSelector"]["matchLabels"] == {"app.hermes/revision-uid": UID}
    assert set(spec["policyTypes"]) == {"Ingress", "Egress"}
    assert spec["ingress"] == []
    assert spec["egress"] == []  # default deny both directions

    # securityContext patch went through
    (patch,) = rec.patches()
    pod = patch["spec"]["template"]["spec"]
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    csc = pod["containers"][0]["securityContext"]
    assert csc["capabilities"] == {"drop": ["ALL"]}
    assert csc["readOnlyRootFilesystem"] is True
    assert csc["allowPrivilegeEscalation"] is False


def test_enforce_egress_allowlist_renders_exact_rules(monkeypatch, driver):
    rec = install(monkeypatch, driver, [(("get", "deployment"), deployment_json())])
    policy = {
        "network": "egress-allowlist",
        "egressAllow": ["10.0.0.5:443", "api.example.com:8443"],
    }
    driver.enforce(make_ref(), policy)

    (manifest,) = rec.applied_manifests()
    egress = manifest["spec"]["egress"]
    assert {"to": [{"ipBlock": {"cidr": "10.0.0.5/32"}}],
            "ports": [{"protocol": "TCP", "port": 443}]} in egress
    assert {"ports": [{"protocol": "TCP", "port": 8443}]} in egress
    assert len(egress) == 2
    ann = manifest["metadata"]["annotations"]["policy.hermes/egress-allow"]
    assert json.loads(ann) == ["10.0.0.5:443", "api.example.com:8443"]
    assert manifest["spec"]["ingress"] == []


def test_enforce_open_applies_no_network_policy(monkeypatch, driver):
    rec = install(monkeypatch, driver, [(("get", "deployment"), deployment_json())])
    driver.enforce(make_ref(), {"network": "open"})
    assert rec.applied_manifests() == []
    delete_calls = [args for args, _ in rec.calls if args[0] == "delete"]
    assert delete_calls and "networkpolicy" in delete_calls[0]


def test_enforce_unsupported_policy_raises(monkeypatch, driver):
    rec = install(monkeypatch, driver)
    with pytest.raises(DriverError):
        driver.enforce(make_ref(), {"network": "isolated", "mystery": 1})
    assert rec.calls == []  # never touched the cluster


def test_enforce_kubectl_failure_is_driver_error(monkeypatch, driver):
    install(monkeypatch, driver, [(("apply",), DriverError("apply refused"))])
    with pytest.raises(DriverError, match="apply refused"):
        driver.enforce(make_ref(), {"network": "isolated"})


# ------------------------------------------------------------------ verify()


def netpol_json(egress, annotation=None, ingress=None, selector_uid=UID) -> str:
    meta: dict = {"name": f"hermes-sandbox-{UID}", "namespace": "team-a"}
    if annotation is not None:
        meta["annotations"] = {"policy.hermes/egress-allow": annotation}
    return json.dumps(
        {
            "kind": "NetworkPolicy",
            "metadata": meta,
            "spec": {
                "podSelector": {
                    "matchLabels": {"app.hermes/revision-uid": selector_uid}
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": ingress or [],
                "egress": egress,
            },
        }
    )


def test_verify_passes_on_matching_readback(monkeypatch, driver):
    policy = {
        "network": "egress-allowlist",
        "egressAllow": ["10.0.0.5:443", "api.example.com:8443"],
        "readOnlyRootFilesystem": True,
        "allowPrivilegeEscalation": False,
    }
    egress = [
        {"ports": [{"protocol": "TCP", "port": 8443}]},
        {"to": [{"ipBlock": {"cidr": "10.0.0.5/32"}}],
         "ports": [{"protocol": "TCP", "port": 443}]},
    ]
    install(
        monkeypatch,
        driver,
        [
            (
                ("get", "networkpolicy"),
                netpol_json(
                    egress,
                    annotation=json.dumps(["10.0.0.5:443", "api.example.com:8443"]),
                ),
            ),
            (("get", "deployment"), deployment_json()),
        ],
    )
    driver.verify(make_ref(), policy)  # no raise


def test_verify_isolated_passes(monkeypatch, driver):
    install(
        monkeypatch,
        driver,
        [
            (("get", "networkpolicy"), netpol_json([])),
            (("get", "deployment"), deployment_json()),
        ],
    )
    driver.verify(make_ref(), {"network": "isolated"})


def test_verify_raises_on_missing_network_policy(monkeypatch, driver):
    install(
        monkeypatch,
        driver,
        [
            (
                ("get", "networkpolicy"),
                DriverError('networkpolicies "hermes-sandbox-rev123abc" not found'),
            ),
            (("get", "deployment"), deployment_json()),
        ],
    )
    with pytest.raises(DriverError, match="not found"):
        driver.verify(make_ref(), {"network": "isolated"})


def test_verify_raises_on_egress_drift(monkeypatch, driver):
    # Cluster allows an extra destination the policy never admitted.
    drifted = [
        {"to": [{"ipBlock": {"cidr": "10.0.0.5/32"}}],
         "ports": [{"protocol": "TCP", "port": 443}]},
        {"to": [{"ipBlock": {"cidr": "203.0.113.9/32"}}],
         "ports": [{"protocol": "TCP", "port": 25}]},
    ]
    install(
        monkeypatch,
        driver,
        [
            (
                ("get", "networkpolicy"),
                netpol_json(drifted, annotation=json.dumps(["10.0.0.5:443"])),
            ),
            (("get", "deployment"), deployment_json()),
        ],
    )
    with pytest.raises(DriverError, match="egress rules drifted"):
        driver.verify(
            make_ref(),
            {"network": "egress-allowlist", "egressAllow": ["10.0.0.5:443"]},
        )


def test_verify_raises_on_ingress_rules_present(monkeypatch, driver):
    install(
        monkeypatch,
        driver,
        [
            (("get", "networkpolicy"), netpol_json([], ingress=[{"from": []}])),
            (("get", "deployment"), deployment_json()),
        ],
    )
    with pytest.raises(DriverError, match="ingress"):
        driver.verify(make_ref(), {"network": "isolated"})


def test_verify_raises_on_security_context_drift(monkeypatch, driver):
    containers = [
        {
            "name": "harness",
            "securityContext": {
                "readOnlyRootFilesystem": False,  # drift
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
        }
    ]
    install(
        monkeypatch,
        driver,
        [
            (("get", "networkpolicy"), netpol_json([])),
            (("get", "deployment"), deployment_json(containers=containers)),
        ],
    )
    with pytest.raises(DriverError, match="readOnlyRootFilesystem"):
        driver.verify(
            make_ref(), {"network": "isolated", "readOnlyRootFilesystem": True}
        )


def test_verify_raises_on_missing_capability_drop(monkeypatch, driver):
    containers = [
        {
            "name": "harness",
            "securityContext": {
                "readOnlyRootFilesystem": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["NET_RAW"]},
            },
        }
    ]
    install(
        monkeypatch,
        driver,
        [
            (("get", "networkpolicy"), netpol_json([])),
            (("get", "deployment"), deployment_json(containers=containers)),
        ],
    )
    with pytest.raises(DriverError, match="capabilities"):
        driver.verify(make_ref(), {"network": "isolated"})


def test_verify_open_requires_absent_network_policy(monkeypatch, driver):
    install(
        monkeypatch,
        driver,
        [
            (("get", "networkpolicy"), netpol_json([])),  # policy still exists
            (("get", "deployment"), deployment_json()),
        ],
    )
    with pytest.raises(DriverError, match="exists"):
        driver.verify(make_ref(), {"network": "open"})


def test_verify_kubectl_failure_is_driver_error(monkeypatch, driver):
    install(
        monkeypatch,
        driver,
        [
            (("get", "networkpolicy"), netpol_json([])),
            (("get", "deployment"), DriverError("connection refused")),
        ],
    )
    with pytest.raises(DriverError, match="connection refused"):
        driver.verify(make_ref(), {"network": "isolated"})


# --------------------------------------------------------------- kubectl seam


def test_kubectl_nonzero_exit_raises_driver_error(monkeypatch, driver):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "error: the server refused"

    monkeypatch.setattr(
        "enterprise.drivers.sandbox_k8s.subprocess.run", lambda *a, **k: Proc()
    )
    with pytest.raises(DriverError, match="refused"):
        driver._kubectl(["get", "pods"])


def test_kubectl_context_and_binary_configurable(monkeypatch):
    seen = {}

    class Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return Proc()

    monkeypatch.setattr("enterprise.drivers.sandbox_k8s.subprocess.run", fake_run)
    d = K8sSandboxDriver(kubectl="/usr/local/bin/kubectl", context="prod")
    assert d._kubectl(["get", "pods"]) == "ok"
    assert seen["cmd"][:3] == ["/usr/local/bin/kubectl", "--context", "prod"]
