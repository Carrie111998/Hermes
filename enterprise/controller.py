"""Deployment choreography + namespace lifecycle for Hermes Enterprise.

The Controller owns every state transition in the control plane. Drivers and
adapters consume admitted intent at their boundary; they never own resources
or select themselves. The single deploy path implemented here is:

    authorize -> resolve references -> per-reference authorize
    -> namespace gateway ready -> sandbox supports(ENTIRE policy)
    -> immutable AgentRevision snapshot -> provision candidate
    -> enforce + verify containment (BEFORE start)
    -> prepare non-serving route -> retire previous revision
    -> activate candidate (sole active) -> start harness -> enable route

Every failure before activation leaves the previous revision serving,
marks the candidate Failed, and tears its workload down. A failure after
activation triggers rollback to the previous revision; an unverifiable
rollback raises RollbackError and leaves the agent inactive with all
routes disabled.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any

from .audit import AuditLog
from .contracts import (
    AuthzRequest,
    ComputeDriver,
    DriverRegistry,
    IAMAdapter,
    SandboxDriver,
    WorkloadRef,
)
from .errors import (
    AuthorizationError,
    DeploymentError,
    NotFoundError,
    RollbackError,
)
from .resources import (
    Kind,
    NamespacePhase,
    Resource,
    ResourceMeta,
    RevisionPhase,
)
from .store import ResourceStore

# ---------------------------------------------------------------------------
# Gateway management
# ---------------------------------------------------------------------------


class GatewayManager(ABC):
    """Manages the per-namespace ingress gateway and per-revision routes.

    Routes are prepared non-serving so activation is a flip, not a build.
    """

    @abstractmethod
    def ensure_gateway(self, namespace: str) -> bool:
        """Ensure the namespace gateway exists; return True when ready."""

    @abstractmethod
    def prepare_route(self, namespace: str, revision_uid: str) -> None:
        """Create a non-serving route for the revision. Must not serve."""

    @abstractmethod
    def enable_route(self, namespace: str, revision_uid: str) -> None:
        """Begin serving traffic to the revision's route."""

    @abstractmethod
    def disable_route(self, namespace: str, revision_uid: str) -> None:
        """Stop serving traffic to the revision's route."""

    @abstractmethod
    def teardown_gateway(self, namespace: str) -> None:
        """Remove the namespace gateway and all its routes."""


class InMemoryGatewayManager(GatewayManager):
    """In-process gateway manager for tests and local development."""

    def __init__(self) -> None:
        self.gateways: set[str] = set()
        # (namespace, revision_uid) -> serving?
        self.routes: dict[tuple[str, str], bool] = {}
        self.calls: list[tuple[str, ...]] = []

    def ensure_gateway(self, namespace: str) -> bool:
        self.calls.append(("ensure_gateway", namespace))
        self.gateways.add(namespace)
        return True

    def prepare_route(self, namespace: str, revision_uid: str) -> None:
        self.calls.append(("prepare_route", namespace, revision_uid))
        self.routes[(namespace, revision_uid)] = False

    def enable_route(self, namespace: str, revision_uid: str) -> None:
        self.calls.append(("enable_route", namespace, revision_uid))
        self.routes[(namespace, revision_uid)] = True

    def disable_route(self, namespace: str, revision_uid: str) -> None:
        self.calls.append(("disable_route", namespace, revision_uid))
        self.routes[(namespace, revision_uid)] = False

    def teardown_gateway(self, namespace: str) -> None:
        self.calls.append(("teardown_gateway", namespace))
        self.gateways.discard(namespace)
        for key in [k for k in self.routes if k[0] == namespace]:
            del self.routes[key]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

_READ_ACTIONS: dict[Kind, str] = {
    Kind.CONFIGURATION: "hermes.configurations.read",
    Kind.HARNESS: "hermes.harnesses.read",
    Kind.CHANNEL: "hermes.channels.read",
    Kind.SECRET: "hermes.secrets.read",
    Kind.SANDBOX_POLICY: "hermes.sandboxpolicies.read",
}


class Controller:
    """Owns namespace lifecycle and the single deploy/rollback path."""

    def __init__(
        self,
        store: ResourceStore,
        audit: AuditLog,
        registry: DriverRegistry,
        gateway_manager: GatewayManager,
        iam_adapter: IAMAdapter,
    ) -> None:
        self.store = store
        self.audit = audit
        self.registry = registry
        self.gateway = gateway_manager
        self.iam = iam_adapter
        self._agent_locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- internals ---------------------------------------------------------

    def _agent_lock(self, namespace: str, agent_name: str) -> threading.Lock:
        key = (namespace, agent_name)
        with self._locks_guard:
            lock = self._agent_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._agent_locks[key] = lock
            return lock

    def _compute(self) -> ComputeDriver:
        return self.registry.get("compute")

    def _sandbox(self) -> SandboxDriver:
        return self.registry.get("sandbox")

    def _audit(self, **kw: Any) -> None:
        self.audit.record(**kw)

    # -- namespace lifecycle -------------------------------------------------

    def ensure_namespace(self, name: str, actor: str = "controller") -> Resource:
        """Create the Namespace if missing and reconcile it toward Ready.

        Ready requires the gateway to report ready; any gateway failure
        moves the namespace to Failed. Each transition is audited.
        """
        try:
            ns = self.store.get(Kind.NAMESPACE, name)
        except NotFoundError:
            ns = Resource(
                meta=ResourceMeta(kind=Kind.NAMESPACE.value, name=name),
                status={"phase": NamespacePhase.PENDING.value},
            )
            self.store.create(ns)
            self._audit(
                actor=actor, actor_kind="principal",
                action="hermes.namespaces.create", outcome="applied",
                kind=Kind.NAMESPACE.value, resource=name,
            )

        prev_phase = ns.status.get("phase", NamespacePhase.PENDING.value)
        try:
            ready = bool(self.gateway.ensure_gateway(name))
            phase = NamespacePhase.READY if ready else NamespacePhase.PENDING
            reason = None if ready else "gateway not ready"
        except Exception as exc:  # gateway failure -> Failed, fail-closed
            phase = NamespacePhase.FAILED
            reason = f"gateway error: {exc}"

        if phase.value != prev_phase:
            ns = self.store.update_status(
                Kind.NAMESPACE, name, None, {"phase": phase.value}
            )
            self._audit(
                actor=actor, actor_kind="principal",
                action="hermes.namespaces.reconcile",
                outcome="applied" if phase is not NamespacePhase.FAILED else "error",
                kind=Kind.NAMESPACE.value, resource=name,
                reason=reason,
                detail={"from": prev_phase, "to": phase.value},
            )
        return self.store.get(Kind.NAMESPACE, name)

    # -- helpers -------------------------------------------------------------

    def active_revision(self, agent_name: str, namespace: str) -> Resource | None:
        """The sole Active revision for the agent, or None."""
        for rev in self.store.list(Kind.AGENT_REVISION, namespace):
            if (
                rev.spec.get("agent") == agent_name
                and rev.status.get("phase") == RevisionPhase.ACTIVE.value
            ):
                return rev
        return None

    def _revisions_for(self, agent_name: str, namespace: str) -> list[Resource]:
        revs = [
            r
            for r in self.store.list(Kind.AGENT_REVISION, namespace)
            if r.spec.get("agent") == agent_name
        ]
        revs.sort(key=lambda r: int(r.spec.get("serial", 0)))
        return revs

    def _next_serial(self, agent_name: str, namespace: str) -> int:
        revs = self._revisions_for(agent_name, namespace)
        return (int(revs[-1].spec.get("serial", 0)) + 1) if revs else 1

    def _authorize(self, actor: str, action: str, kind: Kind,
                   namespace: str | None, resource: str | None) -> None:
        req = AuthzRequest(
            principal=actor,
            principal_kind="principal",
            action=action,
            kind=kind.value,
            namespace=namespace,
            resource=resource,
        )
        try:
            self.iam.authorize(req)
        except AuthorizationError:
            self._audit(
                actor=actor, actor_kind="principal", action=action,
                outcome="deny", kind=kind.value, namespace=namespace,
                resource=resource, reason="denied by IAM adapter",
            )
            raise
        self._audit(
            actor=actor, actor_kind="principal", action=action,
            outcome="allow", kind=kind.value, namespace=namespace,
            resource=resource,
        )

    @staticmethod
    def _workload_ref(revision: Resource, driver_name: str) -> WorkloadRef:
        return WorkloadRef(
            revision_uid=revision.meta.uid,
            namespace=revision.meta.namespace or "",
            workload_identity=str(revision.spec.get("workloadIdentity", "")),
            driver=driver_name,
        )

    # -- deploy ----------------------------------------------------------------

    def deploy(self, agent_name: str, namespace: str, actor: str) -> Resource:
        """Deploy the agent's current spec as a new immutable revision."""
        with self._agent_lock(namespace, agent_name):
            return self._deploy_locked(agent_name, namespace, actor)

    def _deploy_locked(self, agent_name: str, namespace: str, actor: str) -> Resource:
        # 1. Authorize deploy on the exact Agent.
        self._authorize(
            actor, "hermes.agents.deploy", Kind.AGENT, namespace, agent_name
        )

        agent = self.store.get(Kind.AGENT, agent_name, namespace)

        # 2. Resolve + per-reference authorize protected references.
        config = self.store.get(
            Kind.CONFIGURATION, agent.spec["configuration"], namespace
        )
        self._authorize(
            actor, _READ_ACTIONS[Kind.CONFIGURATION], Kind.CONFIGURATION,
            namespace, config.meta.name,
        )

        harness = self.store.get(Kind.HARNESS, agent.spec["harness"], None)
        self._authorize(
            actor, _READ_ACTIONS[Kind.HARNESS], Kind.HARNESS, None,
            harness.meta.name,
        )

        for ch_name in agent.spec.get("channels", []) or []:
            self.store.get(Kind.CHANNEL, ch_name, namespace)
            self._authorize(
                actor, _READ_ACTIONS[Kind.CHANNEL], Kind.CHANNEL,
                namespace, ch_name,
            )
        for sec_name in agent.spec.get("secrets", []) or []:
            self.store.get(Kind.SECRET, sec_name, namespace)
            self._authorize(
                actor, _READ_ACTIONS[Kind.SECRET], Kind.SECRET,
                namespace, sec_name,
            )

        policy: dict[str, Any] = {}
        policy_name = agent.spec.get("sandboxPolicy")
        if policy_name:
            policy_res = self.store.get(Kind.SANDBOX_POLICY, policy_name, namespace)
            self._authorize(
                actor, _READ_ACTIONS[Kind.SANDBOX_POLICY], Kind.SANDBOX_POLICY,
                namespace, policy_name,
            )
            policy = dict(policy_res.spec)

        # 3. Namespace gateway must be ready.
        ns = self.ensure_namespace(namespace, actor=actor)
        if ns.status.get("phase") != NamespacePhase.READY.value:
            self._audit(
                actor=actor, actor_kind="principal",
                action="hermes.agents.deploy", outcome="error",
                kind=Kind.AGENT.value, namespace=namespace,
                resource=agent_name,
                reason=f"namespace not Ready (phase={ns.status.get('phase')})",
            )
            raise DeploymentError(
                f"namespace {namespace!r} is not Ready; cannot deploy"
            )

        compute = self._compute()
        sandbox = self._sandbox()

        # 4. Sandbox driver must support the ENTIRE policy.
        if not sandbox.supports(policy):
            self._audit(
                actor=actor, actor_kind="principal",
                action="hermes.agents.deploy", outcome="deny",
                kind=Kind.AGENT.value, namespace=namespace,
                resource=agent_name,
                reason=(
                    f"sandbox driver {sandbox.name!r} cannot enforce the "
                    "entire SandboxPolicy; partial support is unsupported"
                ),
            )
            raise DeploymentError(
                f"sandbox driver {sandbox.name!r} does not support the "
                "entire SandboxPolicy"
            )

        # 5. Immutable AgentRevision snapshot.
        previous = self.active_revision(agent_name, namespace)
        serial = self._next_serial(agent_name, namespace)
        revision = Resource(
            meta=ResourceMeta(
                kind=Kind.AGENT_REVISION.value,
                name=f"{agent_name}-rev-{serial}",
                namespace=namespace,
            ),
            spec={
                "agent": agent_name,
                "agentUid": agent.meta.uid,
                "serial": serial,
                "workloadIdentity": f"wi-{agent_name}",
                "harness": {
                    "name": harness.meta.name,
                    "version": harness.spec["version"],
                    "image": harness.spec["image"],
                },
                "configuration": dict(config.spec.get("config", {})),
                "sandboxPolicy": policy,
                "computeDriver": self.registry.selected_name("compute"),
                "sandboxDriver": self.registry.selected_name("sandbox"),
            },
            status={"phase": RevisionPhase.CANDIDATE.value},
        )
        self.store.create(revision)
        self._audit(
            actor=actor, actor_kind="principal",
            action="hermes.agentrevisions.create", outcome="applied",
            kind=Kind.AGENT_REVISION.value, namespace=namespace,
            resource=revision.meta.name,
            detail={"serial": serial, "agent": agent_name},
        )

        ref: WorkloadRef | None = None

        def _fail_candidate(stage: str, exc: Exception) -> None:
            """Pre-activation failure: candidate Failed, workload torn down,
            previous revision and its route untouched."""
            self.store.update_status(
                Kind.AGENT_REVISION, revision.meta.name, namespace,
                {"phase": RevisionPhase.FAILED.value, "reason": f"{stage}: {exc}"},
            )
            if ref is not None:
                try:
                    compute.teardown(ref)
                except Exception:
                    pass  # best-effort teardown; candidate is already Failed
            self._audit(
                actor=actor, actor_kind="principal",
                action="hermes.agents.deploy", outcome="error",
                kind=Kind.AGENT.value, namespace=namespace,
                resource=agent_name,
                reason=f"deploy failed at {stage}: {exc}",
                detail={"revision": revision.meta.name, "stage": stage},
            )

        # 6. Provision candidate; enforce + verify containment BEFORE start.
        try:
            ref = compute.provision_candidate(revision)
        except Exception as exc:
            _fail_candidate("provision_candidate", exc)
            raise DeploymentError(f"provisioning failed: {exc}") from exc

        try:
            sandbox.enforce(ref, policy)
            sandbox.verify(ref, policy)
        except Exception as exc:
            _fail_candidate("sandbox containment", exc)
            raise DeploymentError(f"containment failed: {exc}") from exc

        self.store.update_status(
            Kind.AGENT_REVISION, revision.meta.name, namespace,
            {"phase": RevisionPhase.CONTAINED.value},
        )

        # 7. Prepare non-serving route.
        try:
            self.gateway.prepare_route(namespace, revision.meta.uid)
        except Exception as exc:
            _fail_candidate("prepare_route", exc)
            raise DeploymentError(f"route preparation failed: {exc}") from exc

        # 8. Retire previous active revision (stop harness, verify stopped).
        prev_ref: WorkloadRef | None = None
        if previous is not None:
            prev_ref = self._workload_ref(
                previous, str(previous.spec.get("computeDriver", compute.name))
            )
            try:
                compute.stop_harness(prev_ref)
                self.gateway.disable_route(namespace, previous.meta.uid)
            except Exception as exc:
                _fail_candidate("retire previous revision", exc)
                raise DeploymentError(
                    f"could not retire previous revision: {exc}"
                ) from exc
            self.store.update_status(
                Kind.AGENT_REVISION, previous.meta.name, namespace,
                {"phase": RevisionPhase.RETIRED.value},
            )
            self._audit(
                actor=actor, actor_kind="principal",
                action="hermes.agentrevisions.retire", outcome="applied",
                kind=Kind.AGENT_REVISION.value, namespace=namespace,
                resource=previous.meta.name,
            )

        # 9. Activate candidate (sole active), start harness, enable route.
        self.store.update_status(
            Kind.AGENT_REVISION, revision.meta.name, namespace,
            {"phase": RevisionPhase.ACTIVE.value},
        )
        try:
            compute.start_harness(ref)
            self.gateway.enable_route(namespace, revision.meta.uid)
        except Exception as exc:
            self._rollback_after_activation(
                agent_name, namespace, actor, revision, ref,
                previous, prev_ref, cause=exc,
            )
            raise DeploymentError(
                f"post-activation failure, rolled back: {exc}"
            ) from exc

        self._audit(
            actor=actor, actor_kind="principal",
            action="hermes.agents.deploy", outcome="applied",
            kind=Kind.AGENT.value, namespace=namespace, resource=agent_name,
            detail={"revision": revision.meta.name, "serial": serial},
        )
        return self.store.get(Kind.AGENT_REVISION, revision.meta.name, namespace)

    # -- rollback ----------------------------------------------------------

    def _rollback_after_activation(
        self,
        agent_name: str,
        namespace: str,
        actor: str,
        candidate: Resource,
        candidate_ref: WorkloadRef,
        previous: Resource | None,
        prev_ref: WorkloadRef | None,
        cause: Exception,
    ) -> None:
        """Roll back to `previous` after a post-activation failure.

        On unverifiable rollback the agent is left inactive with all routes
        disabled and RollbackError is raised (chained to the cause).
        """
        compute = self._compute()

        # Retire the failed candidate first: no traffic, no harness.
        try:
            self.gateway.disable_route(namespace, candidate.meta.uid)
        except Exception:
            pass
        try:
            compute.stop_harness(candidate_ref)
        except Exception:
            pass
        self.store.update_status(
            Kind.AGENT_REVISION, candidate.meta.name, namespace,
            {"phase": RevisionPhase.RETIRED.value,
             "reason": f"rolled back: {cause}"},
        )

        if previous is None or prev_ref is None:
            self._audit(
                actor=actor, actor_kind="principal",
                action="hermes.agents.rollback", outcome="error",
                kind=Kind.AGENT.value, namespace=namespace,
                resource=agent_name,
                reason=f"no previous revision to roll back to ({cause})",
            )
            raise RollbackError(
                f"post-activation failure and no previous revision; agent "
                f"{agent_name!r} is inactive: {cause}"
            ) from cause

        try:
            compute.start_harness(prev_ref)
            self.gateway.enable_route(namespace, previous.meta.uid)
        except Exception as exc:
            # Unverifiable rollback: everything stays down.
            try:
                self.gateway.disable_route(namespace, previous.meta.uid)
            except Exception:
                pass
            self._audit(
                actor=actor, actor_kind="principal",
                action="hermes.agents.rollback", outcome="error",
                kind=Kind.AGENT.value, namespace=namespace,
                resource=agent_name,
                reason=f"rollback unverifiable: {exc}",
                detail={"candidate": candidate.meta.name,
                        "previous": previous.meta.name},
            )
            raise RollbackError(
                f"rollback to {previous.meta.name!r} could not be verified; "
                f"agent {agent_name!r} left inactive with routes disabled"
            ) from exc

        self.store.update_status(
            Kind.AGENT_REVISION, previous.meta.name, namespace,
            {"phase": RevisionPhase.ACTIVE.value},
        )
        self._audit(
            actor=actor, actor_kind="principal",
            action="hermes.agents.rollback", outcome="rolled-back",
            kind=Kind.AGENT.value, namespace=namespace, resource=agent_name,
            reason=f"post-activation failure: {cause}",
            detail={"candidate": candidate.meta.name,
                    "restored": previous.meta.name},
        )

    def rollback(self, agent_name: str, namespace: str, actor: str) -> Resource:
        """Explicit rollback to the most recently Retired revision."""
        with self._agent_lock(namespace, agent_name):
            return self._rollback_locked(agent_name, namespace, actor)

    def _rollback_locked(self, agent_name: str, namespace: str,
                         actor: str) -> Resource:
        self._authorize(
            actor, "hermes.agents.rollback", Kind.AGENT, namespace, agent_name
        )
        revs = self._revisions_for(agent_name, namespace)
        retired = [
            r for r in revs
            if r.status.get("phase") == RevisionPhase.RETIRED.value
        ]
        if not retired:
            raise DeploymentError(
                f"agent {agent_name!r} has no Retired revision to roll back to"
            )
        target = retired[-1]
        current = self.active_revision(agent_name, namespace)
        compute = self._compute()

        # Stop the current active revision first (fail-closed).
        if current is not None:
            cur_ref = self._workload_ref(
                current, str(current.spec.get("computeDriver", compute.name))
            )
            try:
                compute.stop_harness(cur_ref)
                self.gateway.disable_route(namespace, current.meta.uid)
            except Exception as exc:
                self._audit(
                    actor=actor, actor_kind="principal",
                    action="hermes.agents.rollback", outcome="error",
                    kind=Kind.AGENT.value, namespace=namespace,
                    resource=agent_name,
                    reason=f"could not stop current revision: {exc}",
                )
                raise RollbackError(
                    f"could not stop current revision {current.meta.name!r}: {exc}"
                ) from exc
            self.store.update_status(
                Kind.AGENT_REVISION, current.meta.name, namespace,
                {"phase": RevisionPhase.RETIRED.value},
            )

        target_ref = self._workload_ref(
            target, str(target.spec.get("computeDriver", compute.name))
        )
        try:
            compute.start_harness(target_ref)
            self.gateway.enable_route(namespace, target.meta.uid)
        except Exception as exc:
            try:
                self.gateway.disable_route(namespace, target.meta.uid)
            except Exception:
                pass
            self._audit(
                actor=actor, actor_kind="principal",
                action="hermes.agents.rollback", outcome="error",
                kind=Kind.AGENT.value, namespace=namespace,
                resource=agent_name,
                reason=f"rollback unverifiable: {exc}",
            )
            raise RollbackError(
                f"rollback to {target.meta.name!r} could not be verified; "
                f"agent {agent_name!r} left inactive with routes disabled"
            ) from exc

        self.store.update_status(
            Kind.AGENT_REVISION, target.meta.name, namespace,
            {"phase": RevisionPhase.ACTIVE.value},
        )
        self._audit(
            actor=actor, actor_kind="principal",
            action="hermes.agents.rollback", outcome="rolled-back",
            kind=Kind.AGENT.value, namespace=namespace, resource=agent_name,
            detail={"restored": target.meta.name},
        )
        return self.store.get(Kind.AGENT_REVISION, target.meta.name, namespace)
