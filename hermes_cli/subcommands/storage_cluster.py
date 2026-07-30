"""``hermes storage|cluster|machine`` CLI subcommands."""

from __future__ import annotations

from typing import Callable


def build_storage_parser(subparsers, *, cmd_storage: Callable) -> None:
    parser = subparsers.add_parser(
        "storage",
        help="Mongo remote storage (bootstrap, migrate)",
        description="Manage Hermes MongoDB remote storage",
    )
    storage_sub = parser.add_subparsers(dest="storage_command")

    migrate = storage_sub.add_parser(
        "migrate",
        help="Import local $HERMES_HOME into Mongo",
    )
    migrate.add_argument(
        "--from-home",
        default=None,
        help="Path to local Hermes home (default: current HERMES_HOME)",
    )
    migrate.set_defaults(func=cmd_storage)

    status = storage_sub.add_parser("status", help="Show Mongo storage status")
    status.set_defaults(func=cmd_storage)

    init = storage_sub.add_parser(
        "init-bootstrap",
        help="Write a bootstrap.yaml template",
    )
    init.add_argument("--uri", required=True, help="MongoDB connection URI")
    init.add_argument("--profile", default="default")
    init.add_argument("--machine-id", default=None)
    init.add_argument(
        "--auth-mode",
        default="uri",
        choices=["uri", "scram", "x509"],
        help="uri/scram = credentials in URI; x509 = client certificate",
    )
    init.add_argument("--tls-ca", default=None, help="Path to CA cert (relative to bootstrap ok)")
    init.add_argument("--tls-cert", default=None, help="Path to agent PEM (cert+key)")
    init.set_defaults(func=cmd_storage)


def build_cluster_parser(subparsers, *, cmd_cluster: Callable) -> None:
    parser = subparsers.add_parser(
        "cluster",
        help="Multi-PC agent cluster (status / activate)",
        description="Register and switch active Hermes agents across machines",
    )
    cluster_sub = parser.add_subparsers(dest="cluster_command")

    st = cluster_sub.add_parser("status", help="List online nodes and active agent")
    st.set_defaults(func=cmd_cluster)

    act = cluster_sub.add_parser("activate", help="Activate a node (with messaging handoff)")
    act.add_argument("target", help="node_id, machine_id, or hostname")
    act.add_argument("--reason", default="cli")
    act.set_defaults(func=cmd_cluster)


def build_machine_parser(subparsers, *, cmd_machine: Callable) -> None:
    parser = subparsers.add_parser(
        "machine",
        help="Per-PC overlay (cwd, docker, MCP, browser…)",
        description="View and edit machine-local config overlays stored in Mongo",
    )
    machine_sub = parser.add_subparsers(dest="machine_command")

    show = machine_sub.add_parser("show", help="Show this (or given) machine overlay")
    show.add_argument("--id", default=None, help="machine_id (default: this PC)")
    show.set_defaults(func=cmd_machine)

    lst = machine_sub.add_parser("list", help="List known machines")
    lst.set_defaults(func=cmd_machine)

    set_ov = machine_sub.add_parser("set-overlay", help="Set overlay JSON for a machine")
    set_ov.add_argument("--id", default=None, help="machine_id (default: this PC)")
    set_ov.add_argument("--file", required=True, help="Path to YAML/JSON overlay file")
    set_ov.set_defaults(func=cmd_machine)


def build_agent_parser(subparsers, *, cmd_agent: Callable) -> None:
    parser = subparsers.add_parser(
        "agent",
        help="Enroll agent PCs (control-plane): hermes agent add",
        description="Server-side agent enrollment with one-time codes",
    )
    agent_sub = parser.add_subparsers(dest="agent_command")

    add = agent_sub.add_parser(
        "add",
        help="Create a one-time code and wait for the PC to connect",
    )
    add.add_argument("--name", default=None, help="Suggested machine name for the PC")
    add.add_argument("--profile", default="default")
    add.add_argument("--ttl", type=int, default=300, help="Code lifetime in seconds (default 300)")
    add.add_argument("--port", type=int, default=8743, help="Enroll listen port")
    add.add_argument(
        "--hosts",
        default=None,
        help="Mongo hosts written into agent bootstrap (host:27017,host:27018,…)",
    )
    add.add_argument(
        "--control-dir",
        default=None,
        help="Path to deploy/control-plane (default: auto-detect)",
    )
    add.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="Only print the code; do not listen for the agent",
    )
    add.set_defaults(func=cmd_agent, wait=True)


def build_db_parser(subparsers, *, cmd_db: Callable) -> None:
    parser = subparsers.add_parser(
        "db",
        help="Connect this PC to remote Mongo (hermes db connect)",
        description="Agent-side database enrollment",
    )
    db_sub = parser.add_subparsers(dest="db_command")

    connect = db_sub.add_parser(
        "connect",
        help="Enter control-plane address + one-time code; receive certs automatically",
    )
    connect.add_argument("--host", default=None, help="Control plane IP:port (e.g. 192.168.1.10:8743)")
    connect.add_argument("--code", default=None, help="One-time code from hermes agent add")
    connect.add_argument("--name", default=None, help="Name for this PC (cert CN)")
    connect.add_argument("--hermes-home", default=None, help="Override HERMES_HOME")
    connect.set_defaults(func=cmd_db)
