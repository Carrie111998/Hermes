"""hermes-enterprise CLI.

Invoked as ``python -m enterprise.cli`` (console-script wiring lands later).
Subcommands operate a local control plane rooted at ``--home``
(default ``~/.hermes/enterprise``): a SQLite resource store, an append-only
audit log, and — with the default ``--driver memory`` — in-process dev
drivers so the whole deploy path is demoable without Kubernetes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
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
from .controller import Controller, InMemoryGatewayManager
from .errors import EnterpriseError
from .resources import Kind, Resource, ResourceMeta
from .store import ResourceStore

DEFAULT_HOME = Path.home() / ".hermes" / "enterprise"

# ---------------------------------------------------------------------------
# Dev-mode drivers (used ONLY with --driver memory)
# ---------------------------------------------------------------------------


class MemoryComputeDriver(ComputeDriver):
    """In-process compute driver: tracks workloads in a dict. Dev/demo only."""

    name = "memory"

    def __init__(self) -> None:
        self.workloads: dict[str, dict[str, Any]] = {}

    def provision_candidate(self, revision: Resource) -> WorkloadRef:
        ref = WorkloadRef(
            revision_uid=revision.meta.uid,
            namespace=revision.meta.namespace or "",
            workload_identity=str(revision.spec.get("workloadIdentity", "")),
            driver=self.name,
        )
        self.workloads[ref.revision_uid] = {"ready": True, "harness": "stopped"}
        return ref

    def workload_ready(self, ref: WorkloadRef) -> bool:
        return bool(self.workloads.get(ref.revision_uid, {}).get("ready"))

    def start_harness(self, ref: WorkloadRef) -> None:
        self.workloads.setdefault(ref.revision_uid, {})["harness"] = "running"

    def stop_harness(self, ref: WorkloadRef) -> None:
        self.workloads.setdefault(ref.revision_uid, {})["harness"] = "stopped"

    def teardown(self, ref: WorkloadRef) -> None:
        self.workloads.pop(ref.revision_uid, None)


class MemorySandboxDriver(SandboxDriver):
    """Dev sandbox driver: 'enforces' any policy in memory. Dev/demo only."""

    name = "memory"

    def __init__(self) -> None:
        self.enforced: dict[str, dict[str, Any]] = {}

    def supports(self, policy: dict[str, Any]) -> bool:
        return True

    def enforce(self, ref: WorkloadRef, policy: dict[str, Any]) -> None:
        self.enforced[ref.revision_uid] = dict(policy)

    def verify(self, ref: WorkloadRef, policy: dict[str, Any]) -> None:
        if self.enforced.get(ref.revision_uid) != policy:
            from .errors import DriverError

            raise DriverError("containment not enforced for this workload")


class PermissiveIAMAdapter(IAMAdapter):
    """DEV-MODE ONLY: allows every action and warns loudly.

    Never wire this into a real installation; an IAM adapter that
    default-allows defeats the entire authorization model.
    """

    name = "permissive-dev"

    def __init__(self) -> None:
        self._warned = False

    def authorize(self, request: AuthzRequest) -> None:
        if not self._warned:
            print(
                "warning: PermissiveIAMAdapter is DEV-MODE ONLY — every "
                "action is allowed without authorization checks",
                file=sys.stderr,
            )
            self._warned = True


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _paths(home: Path) -> tuple[Path, Path]:
    return home / "resources.db", home / "audit.db"


def _build(args: argparse.Namespace) -> Controller:
    home = Path(args.home).expanduser()
    store_db, audit_db = _paths(home)
    store = ResourceStore(store_db)
    audit = AuditLog(audit_db)
    registry = DriverRegistry()
    if args.driver == "memory":
        registry.select("compute", MemoryComputeDriver())
        registry.select("sandbox", MemorySandboxDriver())
        iam: IAMAdapter = PermissiveIAMAdapter()
        gateway = InMemoryGatewayManager()
    else:
        raise SystemExit(
            f"driver {args.driver!r} is not wired yet; only 'memory' is "
            "available in this build"
        )
    return Controller(store, audit, registry, gateway, iam)


def _load_doc(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "PyYAML is not installed; provide a JSON file instead"
            ) from exc
        return dict(yaml.safe_load(text) or {})
    return dict(json.loads(text))


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    store_db, audit_db = _paths(home)
    ResourceStore(store_db).close()
    AuditLog(audit_db).close()
    print(f"initialized enterprise home at {home}")
    return 0


def _cmd_ns_create(args: argparse.Namespace) -> int:
    ctl = _build(args)
    ns = ctl.ensure_namespace(args.name, actor=args.actor)
    _print({"name": ns.meta.name, "phase": ns.status.get("phase")})
    return 0


def _cmd_ns_list(args: argparse.Namespace) -> int:
    ctl = _build(args)
    _print([
        {"name": r.meta.name, "phase": r.status.get("phase")}
        for r in ctl.store.list(Kind.NAMESPACE)
    ])
    return 0


def _cmd_harness_register(args: argparse.Namespace) -> int:
    ctl = _build(args)
    res = Resource(
        meta=ResourceMeta(kind=Kind.HARNESS.value, name=args.name),
        spec={"version": args.version, "image": args.image},
    )
    ctl.store.create(res)
    print(f"Harness/{args.name} registered ({args.version})")
    return 0


def _cmd_config_put(args: argparse.Namespace) -> int:
    ctl = _build(args)
    config = _load_doc(args.file)
    res = Resource(
        meta=ResourceMeta(
            kind=Kind.CONFIGURATION.value, name=args.name,
            namespace=args.namespace,
        ),
        spec={"config": config},
    )
    try:
        existing = ctl.store.get(Kind.CONFIGURATION, args.name, args.namespace)
        res.meta = existing.meta
        res.meta.generation = existing.meta.generation
        ctl.store.update_spec(res)
        print(f"Configuration/{args.namespace}/{args.name} updated")
    except EnterpriseError:
        ctl.store.create(res)
        print(f"Configuration/{args.namespace}/{args.name} created")
    return 0


def _cmd_agent_create(args: argparse.Namespace) -> int:
    ctl = _build(args)
    spec: dict[str, Any] = {
        "harness": args.harness,
        "configuration": args.configuration,
        "channels": args.channel or [],
        "secrets": args.secret or [],
    }
    if args.sandbox_policy:
        spec["sandboxPolicy"] = args.sandbox_policy
    res = Resource(
        meta=ResourceMeta(
            kind=Kind.AGENT.value, name=args.name, namespace=args.namespace
        ),
        spec=spec,
    )
    ctl.store.create(res)
    print(f"Agent/{args.namespace}/{args.name} created")
    return 0


def _cmd_agent_deploy(args: argparse.Namespace) -> int:
    ctl = _build(args)
    rev = ctl.deploy(args.name, args.namespace, actor=args.actor)
    _print({
        "revision": rev.meta.name,
        "phase": rev.status.get("phase"),
        "workloadIdentity": rev.spec.get("workloadIdentity"),
    })
    return 0


def _cmd_agent_rollback(args: argparse.Namespace) -> int:
    ctl = _build(args)
    rev = ctl.rollback(args.name, args.namespace, actor=args.actor)
    _print({"revision": rev.meta.name, "phase": rev.status.get("phase")})
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    ctl = _build(args)
    res = ctl.store.get(Kind(args.kind), args.name, args.namespace)
    _print(res.to_dict())
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    ctl = _build(args)
    _print([
        {
            "name": r.meta.name,
            "namespace": r.meta.namespace,
            "phase": r.status.get("phase"),
        }
        for r in ctl.store.list(Kind(args.kind), args.namespace)
    ])
    return 0


def _cmd_audit_tail(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser()
    _, audit_db = _paths(home)
    audit = AuditLog(audit_db)
    _print(audit.query(limit=args.limit))
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-enterprise",
        description="Hermes Enterprise control-plane CLI",
    )
    p.add_argument("--home", default=str(DEFAULT_HOME),
                   help="enterprise home directory (default: %(default)s)")
    p.add_argument("--driver", default="memory", choices=["memory"],
                   help="driver set (default: %(default)s; dev-mode)")
    p.add_argument("--actor", default="cli-user",
                   help="principal recorded in the audit log")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="create store/audit databases")
    sp.set_defaults(func=_cmd_init)

    ns = sub.add_parser("ns", help="namespace operations")
    nssub = ns.add_subparsers(dest="ns_command", required=True)
    sp = nssub.add_parser("create", help="create + reconcile a namespace")
    sp.add_argument("name")
    sp.set_defaults(func=_cmd_ns_create)
    sp = nssub.add_parser("list", help="list namespaces")
    sp.set_defaults(func=_cmd_ns_list)

    hr = sub.add_parser("harness", help="harness operations")
    hrsub = hr.add_subparsers(dest="harness_command", required=True)
    sp = hrsub.add_parser("register", help="register a Harness")
    sp.add_argument("name")
    sp.add_argument("--version", required=True)
    sp.add_argument("--image", required=True)
    sp.set_defaults(func=_cmd_harness_register)

    cf = sub.add_parser("config", help="configuration operations")
    cfsub = cf.add_subparsers(dest="config_command", required=True)
    sp = cfsub.add_parser("put", help="create/update a Configuration from file")
    sp.add_argument("name")
    sp.add_argument("--namespace", "-n", required=True)
    sp.add_argument("--file", "-f", required=True,
                    help="YAML or JSON file with the config contents")
    sp.set_defaults(func=_cmd_config_put)

    ag = sub.add_parser("agent", help="agent operations")
    agsub = ag.add_subparsers(dest="agent_command", required=True)
    sp = agsub.add_parser("create", help="create an Agent")
    sp.add_argument("name")
    sp.add_argument("--namespace", "-n", required=True)
    sp.add_argument("--harness", required=True)
    sp.add_argument("--configuration", required=True)
    sp.add_argument("--channel", action="append")
    sp.add_argument("--secret", action="append")
    sp.add_argument("--sandbox-policy")
    sp.set_defaults(func=_cmd_agent_create)
    sp = agsub.add_parser("deploy", help="deploy the agent's current spec")
    sp.add_argument("name")
    sp.add_argument("--namespace", "-n", required=True)
    sp.set_defaults(func=_cmd_agent_deploy)
    sp = agsub.add_parser("rollback", help="roll back to the last Retired revision")
    sp.add_argument("name")
    sp.add_argument("--namespace", "-n", required=True)
    sp.set_defaults(func=_cmd_agent_rollback)

    sp = sub.add_parser("get", help="get one resource")
    sp.add_argument("kind", choices=[k.value for k in Kind])
    sp.add_argument("name")
    sp.add_argument("--namespace", "-n")
    sp.set_defaults(func=_cmd_get)

    sp = sub.add_parser("list", help="list resources of a kind")
    sp.add_argument("kind", choices=[k.value for k in Kind])
    sp.add_argument("--namespace", "-n")
    sp.set_defaults(func=_cmd_list)

    au = sub.add_parser("audit", help="audit log operations")
    ausub = au.add_subparsers(dest="audit_command", required=True)
    sp = ausub.add_parser("tail", help="show recent audit records")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=_cmd_audit_tail)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except EnterpriseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
