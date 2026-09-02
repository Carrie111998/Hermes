"""`hermes install` — install the SSH-layer file/image bridge client.

`hermes install shellctl` is run on the HERMES HOST (the box you SSH
into). It:

  1. ensures the bridge assets exist under the workspace,
  2. generates (or reuses) a shared bridge token,
  3. prints the exact `~/.ssh/config` snippet + the one-line client
     install command the user pastes on THEIR machine
     (Mac/Linux/WSL/PuTTY host).

Design: zero-dependency client (Python stdlib only), no sudo, no
ControlMaster requirement (per-connection RemoteForward works for plain
`ssh` and tmux-wrapping `sshp` alike). The client is served for copy
via `hermes install shellctl --print-client`.
"""
from __future__ import annotations

import argparse
import os
import shlex
import secrets
import shutil
import sys
from pathlib import Path

from hermes_constants import get_hermes_home

_DEFAULT_PORT = 8765

# Canonical source location (ships with the hermes_cli package).
_CANONICAL_DIR = Path(__file__).resolve().parent / "shellctl_assets"


def _shellctl_dir() -> Path:
    return get_hermes_home() / "shellctl"


def _token_file() -> Path:
    return _shellctl_dir() / "bridge-token"


def _client_file() -> Path:
    return _shellctl_dir() / "hermes-shellctl"


def _bridge_file() -> Path:
    return _shellctl_dir() / "hermes-shellbridge"


def _ensure_assets() -> None:
    shellctl_dir = _shellctl_dir()
    shellctl_dir.mkdir(parents=True, exist_ok=True)
    for name in ("hermes-shellctl", "hermes-shellbridge"):
        src = _CANONICAL_DIR / name
        dst = shellctl_dir / name
        # When HERMES_HOME points somewhere whose shellctl dir IS the
        # canonical dir, src == dst — nothing to copy, just fix perms.
        if src.resolve() != dst.resolve():
            if src.is_file() and (
                not dst.is_file()
                or src.stat().st_mtime > dst.stat().st_mtime
            ):
                shutil.copy2(src, dst)
        if dst.is_file():
            dst.chmod(0o755)


def _get_or_make_token() -> str:
    token_file = _token_file()
    if token_file.is_file():
        tok = token_file.read_text(encoding="utf-8").strip()
        if tok:
            token_file.chmod(0o600)
            return tok
    tok = secrets.token_hex(24)
    try:
        fd = os.open(
            token_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        existing = token_file.read_text(encoding="utf-8").strip()
        if existing:
            token_file.chmod(0o600)
            return existing
        raise RuntimeError(f"empty shellctl token file: {token_file}")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(tok + "\n")
    return tok


def _write_private(path: Path, content: str) -> None:
    """Atomically replace a private file without a permissive creation window."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _print_client() -> int:
    client_file = _client_file()
    if not client_file.is_file():
        _ensure_assets()
    if not client_file.is_file():
        print("error: client asset missing", file=sys.stderr)
        return 1
    sys.stdout.write(client_file.read_text(encoding="utf-8"))
    return 0


def cmd_install_shellctl(args: argparse.Namespace) -> int:
    if getattr(args, "print_client", False):
        return _print_client()

    _ensure_assets()
    token = _get_or_make_token()
    port = int(getattr(args, "port", _DEFAULT_PORT) or _DEFAULT_PORT)
    host_hint = getattr(args, "ssh_host", "") or "your-hermes-host"
    allowed_root = str(getattr(args, "allowed_root", "") or "").strip()
    if not allowed_root:
        from hermes_cli.config import load_config

        shellctl_cfg = (load_config() or {}).get("shellctl") or {}
        if isinstance(shellctl_cfg, dict):
            allowed_root = str(shellctl_cfg.get("allowed_root") or "").strip()

    token_file = _token_file()
    bridge_file = _bridge_file()
    bridge_env = _shellctl_dir() / "bridge.env"
    remote_client_cmd = "hermes install shellctl --print-client"
    remote_token_cmd = "cat " + shlex.quote(str(token_file))
    daemon_cmd = (
        "python3 ~/.hermes-shellctl daemon --port %d "
        "--token-file ~/.hermes-shellctl-token" % port
    )
    if allowed_root:
        daemon_cmd += " --allowed-root " + shlex.quote(allowed_root)

    bar = "=" * 72
    print(bar)
    print(" Hermes shellctl: SSH file and image bridge")
    print(bar)
    print()
    print("The bridge token is a shared secret stored only in:")
    print(f"  {token_file} (mode 0600)")
    print("The commands below copy it over SSH without putting its value in")
    print("shell history or a process argument.")
    print()
    print("STEP 1: On YOUR machine, save the client and token:")
    print(
        "  ssh %s %s > ~/.hermes-shellctl"
        % (shlex.quote(host_hint), shlex.quote(remote_client_cmd))
    )
    print(
        "  (umask 077; ssh %s %s > ~/.hermes-shellctl-token)"
        % (shlex.quote(host_hint), shlex.quote(remote_token_cmd))
    )
    print("  chmod 700 ~/.hermes-shellctl")
    print("  chmod 600 ~/.hermes-shellctl-token")
    print()
    print("STEP 2: Add this block to ~/.ssh/config on YOUR machine:")
    print()
    print(f"  Host {host_hint}")
    print(f"      RemoteForward 127.0.0.1:{port} 127.0.0.1:{port}")
    print("      ExitOnForwardFailure yes")
    print()
    print("STEP 3: Start the client daemon on YOUR machine:")
    print(f"  {daemon_cmd}")
    print()
    print("STEP 4: SSH in normally, then verify on the Hermes host:")
    print(
        "  set -a; . %s; set +a; python3 %s ping"
        % (shlex.quote(str(bridge_env)), shlex.quote(str(bridge_file)))
    )
    print()
    print("If SSH reports 'remote port forwarding failed', another SSH")
    print("session already owns that RemoteForward. Close the old session or")
    print("choose a different --port, update both endpoints, and reinstall.")
    print()
    print("Then in the TUI: /get <local-path>, /send <file>, or /paste")
    print(bar)

    # Persist the resolved config so host bridge callers can load it.
    cfg = bridge_env
    _write_private(
        cfg,
        f"HERMES_SHELLCTL_URL=http://127.0.0.1:{port}\n"
        f"HERMES_SHELLCTL_TOKEN={token}\n"
        f"HERMES_SHELLCTL_PORT={port}\n",
    )
    return 0


def register_cli(install_parser: argparse.ArgumentParser) -> None:
    """Attach `install` subcommands to the given parser."""
    sub = install_parser.add_subparsers(
        dest="install_target", required=True
    )
    sc = sub.add_parser(
        "shellctl",
        help="Install the SSH file/image bridge client (image/pdf "
             "over SSH)",
    )
    sc.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"bridge port (default {_DEFAULT_PORT})",
    )
    sc.add_argument(
        "--ssh-host",
        default="",
        help="the Host alias you SSH to (for the printed snippet)",
    )
    sc.add_argument(
        "--allowed-root",
        default="",
        help=(
            "restrict client-side pulls to this directory tree; defaults "
            "to shellctl.allowed_root in config.yaml"
        ),
    )
    sc.add_argument(
        "--print-client",
        action="store_true",
        help="print the client script to stdout (for piping to a "
             "file)",
    )
    sc.set_defaults(func=cmd_install_shellctl)


def install_command(args: argparse.Namespace) -> int:
    func = getattr(args, "func", None)
    if func is None or func is install_command:
        print("usage: hermes install shellctl", file=sys.stderr)
        return 2
    return func(args)
