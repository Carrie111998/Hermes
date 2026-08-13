"""Controller tests: deploy choreography, rollback, namespace lifecycle, CLI.

All drivers are in-memory fakes that record their call order into one shared
event list, so the tests can assert the EXACT choreography the platform spec
requires.
"""

from __future__ import annotations

import json

import pytest

from enterprise import cli as ent_cli
from enterprise.audit import AuditLog
from enterprise.contracts import (
    AuthzRequest,
    ComputeDriver,
    DriverRegistry,
    IAMAdapter,
    SandboxDriver,
    WorkloadRef,
)
from enterprise.controller import Controller, InMemoryGatewayManager
from enterprise.errors import (
    AuthorizationError,
    DeploymentError,
    DriverError,
    RollbackError,
)
from enterprise.resources import Kind, NamespacePhase, Resource, ResourceMeta, RevisionPhase
from enterprise.store import ResourceStore

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeComputeDriver(ComputeDriver):
    name = "fake-compute"

    def __init__(self, events: list[tuple[str, ...]]):
        self.events = events
        self.fail_provision = False
        self.fail_start = False
        self.fail_start_uids: set[str] = set()
        self.torn_down: list[str] = []

    def provision_candidate(self, revision: Resource) -> WorkloadRef:
        self.events.append(("provision", revision.meta.name))
        if self.fail_provision:
            raise DriverError("provision blew up")
        return WorkloadRef(
            revision_uid=revision.meta.uid,
            namespace=revision.meta.namespace or "",
            workload_identity=str(revision.spec.get("workloadIdentity", "")),
            driver=self.name,
        )

    def workload_ready(self, ref: WorkloadRef) -> bool:
        return True

    def start_harness(self, ref: WorkloadRef) -> None:
        self.events.append(("start", ref.revision_uid))
        if self.fail_start or ref.revision_uid in self.fail_start_uids:
            raise DriverError("start blew up")

    def stop_harness(self, ref: WorkloadRef) -> None:
        self.events.append(("stop", ref.revision_uid))

    def teardown(self, ref: WorkloadRef) -> None:
        self.events.append(("teardown", ref.revision_uid))
        self.torn_down.append(ref.revision_uid)


class FakeSandboxDriver(SandboxDriver):
    name = "fake-sandbox"

    def __init__(self, events: list[tuple[str, ...]]):
        self.events = events
        self.supports_policy = True
        self.fail_enforce = False
        self.fail_verify = False

    def supports(self, policy):
        return self.supports_policy

    def enforce(self, ref: WorkloadRef, policy) -> None:
        self.events.append(("enforce", ref.revision_uid))
        if self.fail_enforce:
            raise DriverError("enforce blew up")

    def verify(self, ref: WorkloadRef, policy) -> None:
        self.events.append(("verify", ref.revision_uid))
        if self.fail_verify:
            raise DriverError("verify blew up")


class RecordingGateway(InMemoryGatewayManager):
    def __init__(self, events: list[tuple[str, ...]]):
        super().__init__()
        self.events = events
        self.fail_enable = False
        self.fail_enable_uids: set[str] = set()

    def prepare_route(self, namespace, revision_uid):
        self.events.append(("prepare_route", revision_uid))
        super().prepare_route(namespace, revision_uid)

    def enable_route(self, namespace, revision_uid):
        self.events.append(("enable_route", revision_uid))
        if self.fail_enable or revision_uid in self.fail_enable_uids:
            raise DriverError("enable_route blew up")
        super().enable_route(namespace, revision_uid)

    def disable_route(self, namespace, revision_uid):
        self.events.append(("disable_route", revision_uid))
        super().disable_route(namespace, revision_uid)


class AllowAllIAM(IAMAdapter):
    name = "allow-all"

    def __init__(self):
        self.requests: list[AuthzRequest] = []

    def authorize(self, request: AuthzRequest) -> None:
        self.requests.append(request)


class DenyIAM(IAMAdapter):
    name = "deny"

    def __init__(self, deny_actions: set[str]):
        self.deny_actions = deny_actions

    def authorize(self, request: AuthzRequest) -> None:
        if request.action in self.deny_actions:
            raise AuthorizationError(f"denied: {request.action}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NS = "team-a"
AGENT = "helper"
ACTOR = "alice"


@pytest.fixture
def world(tmp_path):
    events: list[tuple[str, ...]] = []
    store = ResourceStore(tmp_path / "resources.db")
    audit = AuditLog(tmp_path / "audit.db")
    compute = FakeComputeDriver(events)
    sandbox = FakeSandboxDriver(events)
    gateway = RecordingGateway(events)
    iam = AllowAllIAM()
    registry = DriverRegistry()
    registry.select("compute", compute)
    registry.select("sandbox", sandbox)
    ctl = Controller(store, audit, registry, gateway, iam)
    yield {
        "events": events, "store": store, "audit": audit,
        "compute": compute, "sandbox": sandbox, "gateway": gateway,
        "iam": iam, "registry": registry, "ctl": ctl,
    }
    store.close()
    audit.close()


def seed(w, with_policy=True):
    ctl, store = w["ctl"], w["store"]
    ns = ctl.ensure_namespace(NS, actor=ACTOR)
    assert ns.status["phase"] == NamespacePhase.READY.value
    store.create(Resource(
        meta=ResourceMeta(kind=Kind.HARNESS.value, name="hermes"),
        spec={"version": "1.2.3", "image": "ghcr.io/nous/hermes:1.2.3"},
    ))
    store.create(Resource(
        meta=ResourceMeta(kind=Kind.CONFIGURATION.value, name="cfg", namespace=NS),
        spec={"config": {"model": "hermes-4", "temperature": 0.7}},
    ))
    store.create(Resource(
        meta=ResourceMeta(kind=Kind.CHANNEL.value, name="tg", namespace=NS),
        spec={"platform": "telegram"},
    ))
    agent_spec = {
        "harness": "hermes", "configuration": "cfg",
        "channels": ["tg"], "secrets": [],
    }
    if with_policy:
        store.create(Resource(
            meta=ResourceMeta(kind=Kind.SANDBOX_POLICY.value, name="strict",
                              namespace=NS),
            spec={"network": "isolated", "readOnlyRootFilesystem": True},
        ))
        agent_spec["sandboxPolicy"] = "strict"
    store.create(Resource(
        meta=ResourceMeta(kind=Kind.AGENT.value, name=AGENT, namespace=NS),
        spec=agent_spec,
    ))


def rev(w, name):
    return w["store"].get(Kind.AGENT_REVISION, name, NS)


# ---------------------------------------------------------------------------
# Deploy choreography
# ---------------------------------------------------------------------------


def test_deploy_happy_path_exact_call_order(world):
    seed(world)
    ctl, events = world["ctl"], world["events"]

    rev1 = ctl.deploy(AGENT, NS, ACTOR)
    assert rev1.status["phase"] == RevisionPhase.ACTIVE.value
    assert rev1.meta.name == f"{AGENT}-rev-1"

    events.clear()
    rev2 = ctl.deploy(AGENT, NS, ACTOR)
    assert rev2.status["phase"] == RevisionPhase.ACTIVE.value

    kinds = [e[0] for e in events]
    assert kinds == [
        "provision",        # candidate provisioned
        "enforce",          # containment established
        "verify",           # containment verified BEFORE start
        "prepare_route",    # non-serving route
        "stop",             # previous harness stopped
        "disable_route",    # previous route disabled
        "start",            # candidate harness started
        "enable_route",     # candidate route enabled
    ]
    # stop targets the previous revision; start targets the candidate
    assert events[4] == ("stop", rev1.meta.uid)
    assert events[6] == ("start", rev2.meta.uid)
    assert events[7] == ("enable_route", rev2.meta.uid)

    assert rev(world, rev1.meta.name).status["phase"] == RevisionPhase.RETIRED.value
    assert ctl.active_revision(AGENT, NS).meta.name == rev2.meta.name
    # candidate route serving, previous route not
    assert world["gateway"].routes[(NS, rev2.meta.uid)] is True
    assert world["gateway"].routes[(NS, rev1.meta.uid)] is False


def test_deploy_blocked_when_sandbox_unsupported(world):
    seed(world)
    world["sandbox"].supports_policy = False
    with pytest.raises(DeploymentError, match="does not support"):
        world["ctl"].deploy(AGENT, NS, ACTOR)
    assert world["store"].list(Kind.AGENT_REVISION, NS) == []
    assert ("provision",) not in [e[:1] for e in world["events"]] or all(
        e[0] != "provision" for e in world["events"]
    )


def test_provision_failure_leaves_previous_active(world):
    seed(world)
    ctl = world["ctl"]
    rev1 = ctl.deploy(AGENT, NS, ACTOR)

    world["compute"].fail_provision = True
    with pytest.raises(DeploymentError, match="provisioning failed"):
        ctl.deploy(AGENT, NS, ACTOR)

    assert ctl.active_revision(AGENT, NS).meta.name == rev1.meta.name
    failed = rev(world, f"{AGENT}-rev-2")
    assert failed.status["phase"] == RevisionPhase.FAILED.value
    # previous route untouched
    assert world["gateway"].routes[(NS, rev1.meta.uid)] is True


@pytest.mark.parametrize("stage", ["enforce", "verify"])
def test_containment_failure_leaves_previous_active(world, stage):
    seed(world)
    ctl = world["ctl"]
    rev1 = ctl.deploy(AGENT, NS, ACTOR)

    setattr(world["sandbox"], f"fail_{stage}", True)
    with pytest.raises(DeploymentError, match="containment failed"):
        ctl.deploy(AGENT, NS, ACTOR)

    assert ctl.active_revision(AGENT, NS).meta.name == rev1.meta.name
    failed = rev(world, f"{AGENT}-rev-2")
    assert failed.status["phase"] == RevisionPhase.FAILED.value
    # candidate workload torn down
    assert failed.meta.uid in world["compute"].torn_down
    assert world["gateway"].routes[(NS, rev1.meta.uid)] is True


def test_start_failure_triggers_rollback_to_previous(world):
    seed(world)
    ctl = world["ctl"]
    rev1 = ctl.deploy(AGENT, NS, ACTOR)

    # Fail start only for the NEW candidate; the rollback re-start of the
    # previous revision must succeed.
    def selective_start(self, ref):
        self.events.append(("start", ref.revision_uid))
        if ref.revision_uid != rev1.meta.uid:
            raise DriverError("start blew up")

    world["compute"].start_harness = selective_start.__get__(world["compute"])

    with pytest.raises(DeploymentError, match="rolled back"):
        ctl.deploy(AGENT, NS, ACTOR)

    active = ctl.active_revision(AGENT, NS)
    assert active is not None and active.meta.name == rev1.meta.name
    assert rev(world, f"{AGENT}-rev-2").status["phase"] == RevisionPhase.RETIRED.value
    assert world["gateway"].routes[(NS, rev1.meta.uid)] is True


def test_unverifiable_rollback_leaves_no_active_revision(world):
    seed(world)
    ctl = world["ctl"]
    rev1 = ctl.deploy(AGENT, NS, ACTOR)

    # Every start fails: candidate start fails, and the rollback re-start of
    # the previous revision also fails -> RollbackError.
    world["compute"].fail_start = True
    world["compute"].fail_start_uids = set()

    def all_fail(self, ref):
        self.events.append(("start", ref.revision_uid))
        raise DriverError("start blew up")

    world["compute"].start_harness = all_fail.__get__(world["compute"])

    with pytest.raises(RollbackError):
        ctl.deploy(AGENT, NS, ACTOR)

    assert ctl.active_revision(AGENT, NS) is None
    # all routes disabled
    assert not any(world["gateway"].routes.values())


def test_unauthorized_deploy_creates_no_revision(world):
    seed(world)
    world["ctl"].iam = DenyIAM({"hermes.agents.deploy"})
    with pytest.raises(AuthorizationError):
        world["ctl"].deploy(AGENT, NS, ACTOR)
    assert world["store"].list(Kind.AGENT_REVISION, NS) == []
    assert all(e[0] != "provision" for e in world["events"])


def test_per_reference_authz_denial_blocks_deploy(world):
    seed(world)
    world["ctl"].iam = DenyIAM({"hermes.secrets.read", "hermes.channels.read"})
    with pytest.raises(AuthorizationError):
        world["ctl"].deploy(AGENT, NS, ACTOR)
    assert world["store"].list(Kind.AGENT_REVISION, NS) == []


def test_revisions_numbered_monotonically_and_stable_identity(world):
    seed(world)
    ctl = world["ctl"]
    names, identities = [], []
    for _ in range(3):
        r = ctl.deploy(AGENT, NS, ACTOR)
        names.append(r.meta.name)
        identities.append(r.spec["workloadIdentity"])
    assert names == [f"{AGENT}-rev-{i}" for i in (1, 2, 3)]
    assert identities == [f"wi-{AGENT}"] * 3

    # snapshots are immutable + embed pinned drivers and contents
    r = rev(world, names[-1])
    assert r.spec["computeDriver"] == "fake-compute"
    assert r.spec["sandboxDriver"] == "fake-sandbox"
    assert r.spec["configuration"] == {"model": "hermes-4", "temperature": 0.7}
    assert r.spec["harness"] == {
        "name": "hermes", "version": "1.2.3", "image": "ghcr.io/nous/hermes:1.2.3"
    }
    assert r.spec["sandboxPolicy"]["network"] == "isolated"


def test_explicit_rollback_to_retired_revision(world):
    seed(world)
    ctl = world["ctl"]
    rev1 = ctl.deploy(AGENT, NS, ACTOR)
    rev2 = ctl.deploy(AGENT, NS, ACTOR)

    restored = ctl.rollback(AGENT, NS, ACTOR)
    assert restored.meta.name == rev1.meta.name
    assert restored.status["phase"] == RevisionPhase.ACTIVE.value
    assert rev(world, rev2.meta.name).status["phase"] == RevisionPhase.RETIRED.value
    assert world["gateway"].routes[(NS, rev1.meta.uid)] is True
    assert world["gateway"].routes[(NS, rev2.meta.uid)] is False


def test_explicit_rollback_unverifiable_fails_closed(world):
    seed(world)
    ctl = world["ctl"]
    ctl.deploy(AGENT, NS, ACTOR)
    rev1 = ctl.active_revision(AGENT, NS)
    ctl.deploy(AGENT, NS, ACTOR)

    def all_fail(self, ref):
        self.events.append(("start", ref.revision_uid))
        raise DriverError("start blew up")

    world["compute"].start_harness = all_fail.__get__(world["compute"])
    with pytest.raises(RollbackError):
        ctl.rollback(AGENT, NS, ACTOR)
    assert ctl.active_revision(AGENT, NS) is None
    assert not any(world["gateway"].routes.values())


def test_audit_rows_written_for_deploy_and_rollback(world):
    seed(world)
    ctl = world["ctl"]
    ctl.deploy(AGENT, NS, ACTOR)
    ctl.deploy(AGENT, NS, ACTOR)
    ctl.rollback(AGENT, NS, ACTOR)

    rows = world["audit"].query(limit=200)
    actions = {r["action"] for r in rows}
    assert "hermes.agents.deploy" in actions
    assert "hermes.agents.rollback" in actions
    assert "hermes.agentrevisions.create" in actions
    assert "hermes.namespaces.create" in actions
    deploy_applied = [
        r for r in rows
        if r["action"] == "hermes.agents.deploy" and r["outcome"] == "applied"
    ]
    assert len(deploy_applied) == 2
    rolled = [r for r in rows if r["outcome"] == "rolled-back"]
    assert rolled and rolled[0]["actor"] == ACTOR


def test_namespace_gateway_failure_marks_failed(world):
    ctl = world["ctl"]

    def boom(namespace):
        raise RuntimeError("gateway down")

    world["gateway"].ensure_gateway = boom
    ns = ctl.ensure_namespace("broken-ns", actor=ACTOR)
    assert ns.status["phase"] == NamespacePhase.FAILED.value


# ---------------------------------------------------------------------------
# CLI smoke test (memory driver)
# ---------------------------------------------------------------------------


def test_cli_smoke_deploy_through_memory_driver(tmp_path, capsys):
    home = str(tmp_path / "ent-home")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"model": "hermes-4"}))

    def run(*argv):
        rc = ent_cli.main(["--home", home, *argv])
        assert rc == 0, capsys.readouterr()
        return capsys.readouterr().out

    run("init")
    out = run("ns", "create", "team-a")
    assert json.loads(out)["phase"] == "Ready"
    run("harness", "register", "hermes", "--version", "1.0.0",
        "--image", "ghcr.io/nous/hermes:1.0.0")
    run("config", "put", "cfg", "-n", "team-a", "-f", str(cfg))
    run("agent", "create", "helper", "-n", "team-a",
        "--harness", "hermes", "--configuration", "cfg")
    out = run("agent", "deploy", "helper", "-n", "team-a")
    deployed = json.loads(out)
    assert deployed["revision"] == "helper-rev-1"
    assert deployed["phase"] == "Active"
    assert deployed["workloadIdentity"] == "wi-helper"

    out = run("list", "AgentRevision", "-n", "team-a")
    assert [r["name"] for r in json.loads(out)] == ["helper-rev-1"]

    out = run("audit", "tail", "--limit", "50")
    actions = {r["action"] for r in json.loads(out)}
    assert "hermes.agents.deploy" in actions
