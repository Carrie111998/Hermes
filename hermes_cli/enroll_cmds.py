"""CLI helpers: hermes agent add  /  hermes db connect."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


def cmd_agent_add(args: Any) -> None:
    """Server-side: create a one-time enroll code and optionally wait for the PC."""
    from hermes_storage.enroll_flow import (
        control_plane_dir,
        create_enroll_code,
        guess_lan_ip,
        load_pending,
        normalize_code,
    )

    cp = Path(getattr(args, "control_dir", None) or control_plane_dir())
    ttl = int(getattr(args, "ttl", None) or 300)
    profile = getattr(args, "profile", None) or "default"
    name = getattr(args, "name", None) or None
    wait = getattr(args, "wait", True)
    port = int(getattr(args, "port", None) or 8743)
    hosts = getattr(args, "hosts", None) or ""

    pending = create_enroll_code(profile=profile, name=name, ttl_seconds=ttl, cp=cp)
    lan = guess_lan_ip()

    print("")
    print("=" * 56)
    print("  Hermes agent enrollment")
    print("=" * 56)
    print(f"  One-time code :  {pending.code}")
    print(f"  Valid for     :  {ttl // 60} min")
    print(f"  Profile       :  {profile}")
    if name:
        print(f"  Suggested name:  {name}")
    print("")
    print("  On the agent PC run:")
    print("")
    print(f"    hermes db connect")
    print("")
    print("  When asked, enter:")
    print(f"    Address :  {lan}:{port}")
    print(f"    Code    :  {pending.code}")
    print("")
    print(f"  After connect, the PC talks to Mongo + orchestrator with its cert.")
    print(f"  Orchestrator (mTLS): https://{lan}:8744  — no cert ⇒ connection dropped")
    print("=" * 56)
    print("")

    if not wait:
        print("Not waiting (--no-wait). Start enroll API if needed:")
        print("  docker compose --profile enroll up -d   # in deploy/control-plane")
        return

    # Temporary enroll listener that accepts this code (and any other pending).
    import os

    os.environ["HERMES_CONTROL_DIR"] = str(cp)
    if hosts:
        os.environ["HERMES_MONGO_HOSTS"] = hosts
    os.environ["HERMES_ENROLL_PORT"] = str(port)

    # Import handler after env is set
    sys.path.insert(0, str(cp / "scripts"))
    # Load enroll_server from deploy path
    enroll_path = cp / "scripts" / "enroll_server.py"
    if not enroll_path.is_file():
        # Fallback: use inline minimal server from hermes_storage
        from hermes_storage.enroll_wait_server import serve_until_code_used

        print(f"Waiting up to {ttl}s for the agent to connect on 0.0.0.0:{port} …")
        print("(Ctrl+C to cancel)\n")
        ok = serve_until_code_used(code=pending.code, port=port, ttl=ttl, cp=cp)
        if ok:
            print(f"\n✓ Agent connected and enrolled ({pending.code}).")
        else:
            print("\n✗ Timed out — code expired. Run hermes agent add again.")
            raise SystemExit(1)
        return

    # Run enroll_server Handler in-process with stop-on-redeem
    from importlib.machinery import SourceFileLoader

    mod = SourceFileLoader("hermes_enroll_server", str(enroll_path)).load_module()
    mod.CONTROL = cp
    mod.PORT = port
    if hosts:
        mod.HOSTS = hosts

    used = {"ok": False}
    original_post = mod.Handler.do_POST

    def do_POST(self):  # type: ignore[no-untyped-def]
        original_post(self)
        # Check if our code was consumed
        p = load_pending(pending.code, cp)
        if p and p.used:
            used["ok"] = True

    mod.Handler.do_POST = do_POST  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("0.0.0.0", port), mod.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Waiting up to {ttl}s for the agent to connect on 0.0.0.0:{port} …")
    print("(Ctrl+C to cancel)\n")
    deadline = time.time() + ttl
    try:
        while time.time() < deadline:
            p = load_pending(pending.code, cp)
            if p and p.used:
                used["ok"] = True
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nCancelled.")
        server.shutdown()
        raise SystemExit(1)
    server.shutdown()
    if used["ok"]:
        p = load_pending(pending.code, cp)
        who = (p.used_by if p else None) or "agent"
        print(f"\n✓ Agent connected and enrolled as '{who}'.")
        print("  They can run: hermes storage status")
    else:
        print("\n✗ Timed out — code expired. Run hermes agent add again.")
        raise SystemExit(1)


def cmd_db_connect(args: Any) -> None:
    """Agent-side: connect to control plane with one-time code, install certs."""
    from hermes_constants import get_hermes_home
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.enroll_flow import (
        extract_bundle_archive,
        install_bundle_into_home,
        normalize_code,
    )
    from hermes_storage.factory import reset_storage
    from hermes_storage.machine_id import compute_machine_id

    host = getattr(args, "host", None) or ""
    code = getattr(args, "code", None) or ""
    name = getattr(args, "name", None) or ""
    hermes_home = Path(getattr(args, "hermes_home", None) or get_hermes_home())

    if not host:
        host = _prompt("Control-plane address (IP:port)", "127.0.0.1:8743")
    if not code:
        code = _prompt("One-time code")
    code = normalize_code(code)
    if not code:
        print("Code is required.")
        raise SystemExit(1)

    if not name:
        default_name = compute_machine_id()
        # Shorter default: hostname-ish first segment
        default_name = default_name.split("_")[0][:24] or default_name
        name = _prompt("Name for this PC", default_name)

    host = host.strip()
    if not host.startswith("http://") and not host.startswith("https://"):
        host = "http://" + host
    enroll_url = host.rstrip("/") + "/enroll"

    body = json.dumps({"code": code, "name": name}).encode("utf-8")
    req = Request(
        enroll_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(f"\nConnecting to {enroll_url} …")
    try:
        with urlopen(req, timeout=60) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(err_body).get("error") or err_body
        except Exception:
            err = err_body or str(exc)
        print(f"Enrollment failed: {err}")
        raise SystemExit(1)
    except URLError as exc:
        print(f"Cannot reach control plane: {exc.reason}")
        print("Check the address and that `hermes agent add` is waiting on the server.")
        raise SystemExit(1)

    if "json" in content_type:
        print(f"Unexpected response: {data.decode('utf-8', errors='replace')}")
        raise SystemExit(1)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="hermes-enroll-") as tmp:
        bundle_dir = extract_bundle_archive(data, Path(tmp))
        install_bundle_into_home(bundle_dir, hermes_home)

    reset_bootstrap_cache()
    reset_storage()
    print(f"✓ Wrote bootstrap + certs to {hermes_home}")
    print("  Checking connection…")
    try:
        from hermes_storage import get_storage, is_mongo_mode

        if not is_mongo_mode():
            print("Warning: bootstrap not detected after write.")
            raise SystemExit(1)
        storage = get_storage(force=True)
        storage.client.admin.command("ping")
        storage.register_presence()
        print(f"✓ Mongo OK  (machine_id={storage.machine_id})")
        print("  Next: hermes cluster status")
    except Exception as exc:
        print(f"Bootstrap installed, but Mongo ping failed: {exc}")
        print("  Check HERMES_MONGO_HOSTS on the server and network/firewall.")
        raise SystemExit(1)
