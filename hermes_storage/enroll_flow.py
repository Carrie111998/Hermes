"""One-time-code enrollment: server issues codes, agents redeem for X.509 bundles."""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import socket
import string
import subprocess
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CODE_ALPHABET = string.ascii_uppercase + string.digits
# Avoid ambiguous chars
CODE_ALPHABET = CODE_ALPHABET.replace("O", "").replace("0", "").replace("I", "").replace("1", "")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def control_plane_dir() -> Path:
    override = os.environ.get("HERMES_CONTROL_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # Repo checkout layout
    here = Path(__file__).resolve().parents[1]
    candidate = here / "deploy" / "control-plane"
    if candidate.is_dir():
        return candidate
    # Fallback next to HERMES_HOME
    from hermes_constants import get_hermes_home

    return (get_hermes_home() / "control-plane").resolve()


def pending_dir(cp: Optional[Path] = None) -> Path:
    path = (cp or control_plane_dir()) / "enroll_pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_code(*, groups: int = 2, group_len: int = 4) -> str:
    parts = []
    for _ in range(groups):
        parts.append("".join(secrets.choice(CODE_ALPHABET) for _ in range(group_len)))
    return "-".join(parts)


def normalize_code(code: str) -> str:
    return code.strip().upper().replace(" ", "")


@dataclass
class PendingEnroll:
    code: str
    profile: str = "default"
    name: Optional[str] = None  # optional suggested machine name
    created_at: str = ""
    expires_at: float = 0.0
    used: bool = False
    used_at: Optional[str] = None
    used_by: Optional[str] = None

    def is_expired(self) -> bool:
        return time.time() > float(self.expires_at)

    def is_valid(self) -> bool:
        return (not self.used) and (not self.is_expired())


def _pending_path(code: str, cp: Optional[Path] = None) -> Path:
    return pending_dir(cp) / f"{normalize_code(code)}.json"


def save_pending(pending: PendingEnroll, cp: Optional[Path] = None) -> Path:
    path = _pending_path(pending.code, cp)
    path.write_text(json.dumps(asdict(pending), indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_pending(code: str, cp: Optional[Path] = None) -> Optional[PendingEnroll]:
    path = _pending_path(code, cp)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return PendingEnroll(**raw)
    except Exception as exc:
        logger.warning("Bad pending enroll file %s: %s", path, exc)
        return None


def create_enroll_code(
    *,
    profile: str = "default",
    name: Optional[str] = None,
    ttl_seconds: int = 300,
    cp: Optional[Path] = None,
) -> PendingEnroll:
    """Create a one-time enroll code valid for ``ttl_seconds`` (default 5 min)."""
    code = generate_code()
    now = utcnow()
    pending = PendingEnroll(
        code=code,
        profile=profile or "default",
        name=name,
        created_at=now.isoformat(),
        expires_at=time.time() + max(60, int(ttl_seconds)),
        used=False,
    )
    save_pending(pending, cp)
    return pending


def mark_used(code: str, *, used_by: str, cp: Optional[Path] = None) -> None:
    pending = load_pending(code, cp)
    if not pending:
        return
    pending.used = True
    pending.used_at = utcnow().isoformat()
    pending.used_by = used_by
    save_pending(pending, cp)


def redeem_code(code: str, *, machine_name: str, cp: Optional[Path] = None) -> PendingEnroll:
    """Validate and consume a one-time code. Raises ValueError on failure."""
    pending = load_pending(code, cp)
    if pending is None:
        raise ValueError("Invalid or unknown code")
    if pending.used:
        raise ValueError("Code already used")
    if pending.is_expired():
        raise ValueError("Code expired — ask the server to run: hermes agent add")
    # Prefer admin-suggested name if agent didn't override meaningfully
    return pending


def _run(cmd: list[str], *, cwd: Optional[Path] = None) -> None:
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)


def issue_agent_bundle(
    *,
    name: str,
    profile: str = "default",
    cp: Optional[Path] = None,
    hosts: Optional[str] = None,
    replica_set: str = "rs0",
) -> Path:
    """Issue X.509 cert + bootstrap.yaml via enroll-agent.sh (or openssl fallback).

    Returns path to the bundle directory.
    """
    cp = cp or control_plane_dir()
    script = cp / "scripts" / "enroll-agent.sh"
    out = cp / "bundles" / name
    env = os.environ.copy()
    if hosts:
        env["HERMES_MONGO_HOSTS"] = hosts
    env["HERMES_REPLICA_SET"] = replica_set

    if script.is_file():
        _run(
            ["bash", str(script), "--name", name, "--profile", profile, "--out", str(out)],
            cwd=cp,
        )
        # patch env into subprocess — enroll-agent reads HERMES_MONGO_HOSTS from env/.env
        return out

    # Minimal openssl fallback when shell script missing
    return _issue_bundle_openssl(
        name=name, profile=profile, cp=cp, hosts=hosts or "localhost:27017", replica_set=replica_set
    )


def _issue_bundle_openssl(
    *,
    name: str,
    profile: str,
    cp: Path,
    hosts: str,
    replica_set: str,
    orchestrator_url: Optional[str] = None,
) -> Path:
    certs = cp / "certs"
    ca_crt = certs / "ca.crt"
    ca_key = certs / "ca.key"
    if not ca_crt.is_file() or not ca_key.is_file():
        raise RuntimeError(
            f"CA not found under {certs}. Run scripts/install-control-plane.sh first."
        )

    out = cp / "bundles" / name
    out_certs = out / "certs"
    out_certs.mkdir(parents=True, exist_ok=True)
    agent_dir = certs / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)

    key = agent_dir / "agent.key"
    csr = agent_dir / "agent.csr"
    crt = agent_dir / "agent.crt"
    pem = agent_dir / "agent.pem"
    ext = agent_dir / "agent.ext"

    _run(["openssl", "genrsa", "-out", str(key), "2048"])
    _run([
        "openssl", "req", "-new", "-key", str(key),
        "-subj", f"/O=Hermes/OU=hermes-agents/CN={name}",
        "-out", str(csr),
    ])
    ext.write_text(
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=clientAuth\n",
        encoding="utf-8",
    )
    _run([
        "openssl", "x509", "-req", "-in", str(csr),
        "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
        "-out", str(crt), "-days", "825", "-sha256", "-extfile", str(ext),
    ])
    pem.write_bytes(crt.read_bytes() + key.read_bytes())
    shutil.copy2(ca_crt, out_certs / "ca.crt")
    shutil.copy2(pem, out_certs / "agent.pem")

    uri = (
        f"mongodb://{hosts}/?replicaSet={replica_set}"
        f"&tls=true&authMechanism=MONGODB-X509&authSource=%24external"
    )
    first = hosts.split(",")[0].strip()
    orch_host = first.split(":")[0] or "localhost"
    orch = orchestrator_url or os.environ.get("HERMES_ORCHESTRATOR_URL") or f"https://{orch_host}:8744"
    boot = (
        f"# Hermes agent bootstrap — generated {utcnow().isoformat()}\n"
        f"# Mongo + orchestrator BOTH require this cert — no cert ⇒ dropped\n"
        f"mongo_uri: \"{uri}\"\n"
        f"profile: {profile}\n"
        f"machine_id: {name}\n"
        f"auth_mode: x509\n"
        f"shared_db: hermes_shared\n"
        f"tls:\n"
        f"  ca_file: certs/ca.crt\n"
        f"  cert_key_file: certs/agent.pem\n"
        f"orchestrator:\n"
        f"  url: \"{orch}\"\n"
    )
    (out / "bootstrap.yaml").write_text(boot, encoding="utf-8")
    return out


def pack_bundle_tar_gz(bundle_dir: Path) -> bytes:
    import io

    buf = io.BytesIO()
    parent = bundle_dir.parent
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in bundle_dir.rglob("*"):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(parent)))
    return buf.getvalue()


def install_bundle_into_home(bundle_dir: Path, hermes_home: Path) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "certs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle_dir / "bootstrap.yaml", hermes_home / "bootstrap.yaml")
    certs = bundle_dir / "certs"
    if certs.is_dir():
        for f in certs.iterdir():
            if f.is_file():
                shutil.copy2(f, hermes_home / "certs" / f.name)
    try:
        os.chmod(hermes_home / "bootstrap.yaml", 0o600)
        pem = hermes_home / "certs" / "agent.pem"
        if pem.is_file():
            os.chmod(pem, 0o600)
    except OSError:
        pass


def guess_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def extract_bundle_archive(data: bytes, dest: Path) -> Path:
    """Extract tar.gz bytes; return the bundle directory path."""
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        with tarfile.open(tmp_path, mode="r:gz") as tar:
            tar.extractall(dest)
        dirs = [p for p in dest.iterdir() if p.is_dir()]
        if not dirs:
            raise RuntimeError("Enrollment archive contained no bundle folder")
        return dirs[0]
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
