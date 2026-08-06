"""Backend git operations for the desktop coding rail + Codex-style review pane.

The desktop's git affordances (coding-rail status, worktree lanes, review pane,
branch switch) run as Electron-local git on the user's machine. On a *remote*
gateway those would operate on the wrong filesystem, so this module mirrors them
over the dashboard's authenticated REST surface — the same pattern as ``/api/fs``.

Everything shells out to the system ``git`` (and ``gh`` for ship info / PRs).
Reads degrade to ``None`` / empty on a non-repo; mutations raise so the renderer
can surface a toast. Callers pass an already path-hardened ``cwd``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hermes_cli._subprocess_compat import noninteractive_git_env
from hermes_constants import get_hermes_home

_GIT_TIMEOUT = 30
_GH_TIMEOUT = 30
_MAX_BUFFER = 32 * 1024 * 1024
_UNTRACKED_LINE_MAX_BYTES = 1024 * 1024
_UNTRACKED_SCAN_CAP = 500
_COMMIT_CONTEXT_DIFF_MAX_CHARS = 120_000
_COMMIT_CONTEXT_UNTRACKED_MAX = 80
_TRUNK_BRANCHES = ("main", "master")
_PUSH_APPROVAL_TTL_SECONDS = 10 * 60
_push_approvals: dict[str, dict] = {}
_push_approvals_lock = threading.Lock()


def _push_store_connection() -> sqlite3.Connection:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    database = home / "workspace-push-requests.db"
    connection = sqlite3.connect(database, timeout=30)
    if os.name != "nt":
        database.chmod(0o600)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_push_requests (
            request_id TEXT PRIMARY KEY,
            ciphertext BLOB NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0,
            expires_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _push_store_key() -> bytes:
    path = get_hermes_home() / "workspace-push-requests.key"
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise RuntimeError("Push approval key is invalid.")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = path.read_bytes()
        if len(existing) != 32:
            raise RuntimeError("Push approval key is invalid.")
        return existing
    try:
        os.write(descriptor, key)
    finally:
        os.close(descriptor)
    return key


def _encrypt_push_record(request_id: str, record: dict) -> bytes:
    nonce = secrets.token_bytes(12)
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return nonce + AESGCM(_push_store_key()).encrypt(nonce, payload, request_id.encode("utf-8"))


def _decrypt_push_record(request_id: str, ciphertext: bytes) -> dict:
    nonce, encrypted = ciphertext[:12], ciphertext[12:]
    payload = AESGCM(_push_store_key()).decrypt(
        nonce,
        encrypted,
        request_id.encode("utf-8"),
    )
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Push approval record is invalid.")
    return value


def _persist_push_record(record: dict, *, timestamp: float) -> None:
    request = record["request"]
    request_id = str(request["requestId"])
    expires_at = datetime.fromisoformat(str(request["expiresAt"]).replace("Z", "+00:00")).timestamp()
    with _push_store_connection() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO workspace_push_requests(
                request_id,ciphertext,consumed,expires_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                request_id,
                _encrypt_push_record(request_id, record),
                int(bool(record.get("consumed"))),
                expires_at,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "DELETE FROM workspace_push_requests WHERE consumed=1 OR expires_at<=?",
            (timestamp,),
        )


def _load_push_record(request_id: str) -> dict | None:
    with _push_store_connection() as connection:
        row = connection.execute(
            "SELECT ciphertext,consumed FROM workspace_push_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
    if row is None:
        return None
    record = _decrypt_push_record(request_id, bytes(row["ciphertext"]))
    record["consumed"] = bool(row["consumed"])
    return record


def _consume_push_record(request_id: str, *, timestamp: float) -> dict:
    connection = _push_store_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT ciphertext,consumed,expires_at FROM workspace_push_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None or bool(row["consumed"]):
            raise RuntimeError("Push approval request is unknown or already consumed.")
        if float(row["expires_at"]) <= timestamp:
            connection.execute(
                "UPDATE workspace_push_requests SET consumed=1,updated_at=? WHERE request_id=?",
                (timestamp, request_id),
            )
            connection.commit()
            raise RuntimeError("Push approval request expired.")
        connection.execute(
            "UPDATE workspace_push_requests SET consumed=1,updated_at=? "
            "WHERE request_id=? AND consumed=0",
            (timestamp, request_id),
        )
        connection.commit()
        record = _decrypt_push_record(request_id, bytes(row["ciphertext"]))
        record["consumed"] = True
        return record
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _git(cwd: str, args: list[str], *, timeout: int = _GIT_TIMEOUT) -> tuple[int, str, str]:
    """Run ``git`` in ``cwd``. Returns (returncode, stdout, stderr); never raises
    on a non-zero exit (callers decide what an error means).

    Runs non-interactively (stdin nulled, ``GIT_TERMINAL_PROMPT=0``): these
    calls serve authenticated REST requests from the dashboard/desktop, so a
    credential prompt from ``fetch``/``push``/``pull`` could never be answered
    — it would just hang the request until the timeout. Failing fast surfaces
    the real auth error in the toast instead."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=noninteractive_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return 1, "", "git invocation failed"
    return proc.returncode, proc.stdout, proc.stderr


def _git_out(cwd: str, args: list[str]) -> str:
    """stdout of a git command, or "" on any failure."""
    code, out, _ = _git(cwd, args)
    return out if code == 0 else ""


def _git_ok(cwd: str, args: list[str]) -> None:
    """Run a git mutation, raising RuntimeError with stderr on failure."""
    code, _, err = _git(cwd, args)
    if code != 0:
        raise RuntimeError(err.strip() or f"git {' '.join(args)} failed")


def _is_dir(cwd: str) -> bool:
    try:
        return Path(cwd).is_dir()
    except OSError:
        return False


# ── shared helpers ───────────────────────────────────────────────────────────


def resolve_rename_path(raw: str) -> str:
    """``old => new`` (and ``dir/{old => new}/f``) → the NEW path, so a row
    addresses the real file for diff/stage."""
    path = str(raw or "").strip()
    if " => " not in path:
        return path
    head, _, tail = path.partition("{")
    if tail and "}" in tail:
        inner, _, suffix = tail.partition("}")
        _, _, to = inner.partition(" => ")
        return f"{head}{to}{suffix}".replace("//", "/")
    return path.split(" => ")[-1].strip()


def _numstat(cwd: str, args: list[str]) -> dict[str, tuple[int, int]]:
    """``git diff --numstat`` → {path: (added, removed)}; binary files (``-``) → 0."""
    out = _git_out(cwd, ["diff", "--numstat", *args])
    counts: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added = 0 if parts[0] == "-" else int(parts[0] or 0)
        removed = 0 if parts[1] == "-" else int(parts[1] or 0)
        counts[resolve_rename_path(parts[2])] = (added, removed)
    return counts


def _untracked_insertions(cwd: str, rel: str) -> int:
    """Line count of an untracked file (newlines + a final unterminated line),
    so the review tree can show +N for new files. Binary / oversized → 0."""
    try:
        target = Path(cwd) / rel
        st = target.stat()
        if not os.path.isfile(target) or st.st_size > _UNTRACKED_LINE_MAX_BYTES:
            return 0
        data = target.read_bytes()
        if b"\0" in data:
            return 0
        lines = data.count(b"\n")
        return lines + 1 if data and not data.endswith(b"\n") else lines
    except OSError:
        return 0


def _fill_untracked_counts(cwd: str, files: list[dict]) -> None:
    for file in files:
        if file["status"] == "?" and file["added"] == 0 and file["removed"] == 0:
            file["added"] = _untracked_insertions(cwd, file["path"])


def _branch_base(cwd: str) -> str | None:
    """Merge-base with the remote default branch for "all branch changes"."""
    candidates: list[str] = []
    head = _git_out(cwd, ["rev-parse", "--abbrev-ref", "origin/HEAD"]).strip()
    if head:
        candidates.append(head)
    candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        base = _git_out(cwd, ["merge-base", "HEAD", ref]).strip()
        if base:
            return base
    return None


def _default_branch_name(cwd: str) -> str | None:
    """The repo's trunk name ("main"/"master"/…), preferring origin/HEAD."""
    head = _git_out(cwd, ["rev-parse", "--abbrev-ref", "origin/HEAD"]).strip()
    if head and head != "origin/HEAD":
        return head.split("/", 1)[-1]
    for ref in (
        "refs/heads/main",
        "refs/heads/master",
        "refs/remotes/origin/main",
        "refs/remotes/origin/master",
    ):
        code, _, _ = _git(cwd, ["rev-parse", "--verify", "--quiet", ref])
        if code == 0:
            return ref.split("/")[-1]
    return None


# ── porcelain v2 status parsing ──────────────────────────────────────────────


def _walk_entries(raw: str):
    """Yield (tag, xy, path) per changed file from ``git status --porcelain=v2 -z``,
    skipping branch headers and the rename/copy origin-path records. One walker
    feeds the rail, the review list, and the commit flow."""
    records = raw.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        tag = rec[0] if rec else ""
        if tag == "?":
            yield "?", "??", rec[2:]
        elif tag == "u":
            yield "u", rec.split(" ")[1], rec.split(" ", 10)[-1]
        elif tag in ("1", "2"):
            xy = rec.split(" ")[1]
            path = rec.split(" ", 8)[-1] if tag == "1" else rec.split(" ", 9)[-1]
            if tag == "2":
                i += 1  # rename/copy: the origin path is the next NUL record
            yield tag, xy, resolve_rename_path(path)
        i += 1


def _entry_staged(tag: str, xy: str) -> bool:
    """A tracked entry whose index (staged) code is set."""
    return tag in ("1", "2") and xy[0] not in (".", "?")


def _classify(tag: str, xy: str, path: str) -> dict:
    y = xy[1] if len(xy) > 1 else "."
    return {
        "path": path,
        "staged": _entry_staged(tag, xy),
        "unstaged": tag == "?" or (tag in ("1", "2") and y not in (".", "?")),
        "untracked": tag == "?",
        "conflicted": tag == "u",
    }


def _status_letter(tag: str, xy: str) -> str:
    if tag in ("?", "u"):
        return tag.upper() if tag == "u" else "?"
    code = xy[0] if xy[0] != "." else (xy[1] if len(xy) > 1 else ".")
    return (code if code != "." else "M").upper()


# ── coding rail ──────────────────────────────────────────────────────────────


def repo_status(cwd: str) -> dict | None:
    """Compact working-tree status for the coding rail. None on a non-repo."""
    if not _is_dir(cwd):
        return None

    code, raw, _ = _git(cwd, ["status", "--porcelain=v2", "--branch", "-z"])
    if code != 0:
        return None

    branch: str | None = None
    detached = False
    ahead = behind = 0
    for rec in raw.split("\0"):
        if rec.startswith("# branch.head "):
            head = rec[len("# branch.head ") :]
            detached = head == "(detached)"
            branch = None if detached else head
        elif rec.startswith("# branch.ab "):
            for tok in rec.split()[2:]:
                if tok.startswith("+"):
                    ahead = int(tok[1:] or 0)
                elif tok.startswith("-"):
                    behind = int(tok[1:] or 0)

    files = [_classify(tag, xy, path) for tag, xy, path in _walk_entries(raw)]

    # +/- vs HEAD (tracked), then fold in untracked insertions — `git diff HEAD`
    # ignores them, so a new-file-only turn would otherwise read +0 (bounded scan).
    added = removed = 0
    for a, r in _numstat(cwd, ["HEAD"]).values():
        added += a
        removed += r
    added += sum(_untracked_insertions(cwd, f["path"]) for f in files[:_UNTRACKED_SCAN_CAP] if f["untracked"])

    return {
        "branch": branch,
        "defaultBranch": _default_branch_name(cwd),
        "detached": detached,
        "ahead": ahead,
        "behind": behind,
        "staged": sum(f["staged"] for f in files),
        "unstaged": sum(f["unstaged"] for f in files),
        "untracked": sum(f["untracked"] for f in files),
        "conflicted": sum(f["conflicted"] for f in files),
        "changed": len(files),
        "added": added,
        "removed": removed,
        "files": files[:200],
    }


# ── review pane ──────────────────────────────────────────────────────────────


def review_list(cwd: str, scope: str, base_ref: str | None) -> dict:
    """Changed files for a scope. Mirrors the Electron reviewList shapes."""
    if not _is_dir(cwd):
        return {"files": [], "base": None}

    if scope in ("branch", "lastTurn"):
        base = _branch_base(cwd) if scope == "branch" else base_ref
        if not base:
            return {"files": [], "base": None}
        rng = f"{base}...HEAD" if scope == "branch" else base
        files = [
            {"path": path, "added": a, "removed": r, "status": "M", "staged": False}
            for path, (a, r) in _numstat(cwd, [rng]).items()
        ]
        if scope == "lastTurn":
            seen = {f["path"] for f in files}
            _, raw, _ = _git(cwd, ["status", "--porcelain=v2", "-z"])
            files += [
                {"path": path, "added": 0, "removed": 0, "status": "?", "staged": False}
                for tag, _xy, path in _walk_entries(raw)
                if tag == "?" and path not in seen
            ]
        files.sort(key=lambda f: f["path"])
        _fill_untracked_counts(cwd, files)
        return {"files": files, "base": base}

    code, raw, _ = _git(cwd, ["status", "--porcelain=v2", "-z"])
    if code != 0:
        return {"files": [], "base": None}
    staged = _numstat(cwd, ["--cached"])
    unstaged = _numstat(cwd, [])

    files = []
    for tag, xy, path in _walk_entries(raw):
        sa, sr = staged.get(path, (0, 0))
        ua, ur = unstaged.get(path, (0, 0))
        files.append(
            {
                "path": path,
                "added": sa + ua,
                "removed": sr + ur,
                "status": _status_letter(tag, xy),
                "staged": _entry_staged(tag, xy),
            }
        )
    files.sort(key=lambda f: f["path"])
    _fill_untracked_counts(cwd, files)
    return {"files": files, "base": None}


def review_diff(cwd: str, file_path: str, scope: str, base_ref: str | None, staged: bool) -> str:
    if not _is_dir(cwd):
        return ""
    if scope == "branch":
        base = _branch_base(cwd)
        return _git_out(cwd, ["diff", f"{base}...HEAD", "--", file_path]) if base else ""
    if scope == "lastTurn":
        return _git_out(cwd, ["diff", base_ref, "--", file_path]) if base_ref else ""
    if staged:
        return _git_out(cwd, ["diff", "--cached", "--", file_path])
    worktree = _git_out(cwd, ["diff", "--", file_path])
    if worktree.strip():
        return worktree
    # Untracked: synthesize an all-add diff (exits non-zero by design).
    _, out, _ = _git(cwd, ["diff", "--no-index", "--", os.devnull, file_path])
    return out


def file_diff_vs_head(cwd: str, file_path: str) -> str:
    """Working-tree-vs-HEAD diff for one file (the preview's diff view). Unlike
    review_diff, never all-adds a clean tracked file; only a genuinely untracked one."""
    if not _is_dir(cwd):
        return ""
    head = _git_out(cwd, ["diff", "HEAD", "--", file_path])
    if head.strip():
        return head
    status = _git_out(cwd, ["status", "--porcelain", "--", file_path])
    if not status.strip().startswith("??"):
        return ""
    _, out, _ = _git(cwd, ["diff", "--no-index", "--", os.devnull, file_path])
    return out


def review_stage(cwd: str, file_path: str | None) -> dict:
    _git_ok(cwd, ["add", "--", file_path] if file_path else ["add", "-A"])
    return {"ok": True}


def review_unstage(cwd: str, file_path: str | None) -> dict:
    _git_ok(cwd, ["reset", "-q", "HEAD", "--", file_path] if file_path else ["reset", "-q", "HEAD"])
    return {"ok": True}


def review_revert(cwd: str, file_path: str | None) -> dict:
    """Discard changes back to the committed state (restore tracked, remove untracked)."""
    target = ["--", file_path] if file_path else ["--", "."]
    _git(cwd, ["checkout", "HEAD", *target])
    _git(cwd, ["clean", "-fd", *target])
    return {"ok": True}


def review_rev_parse(cwd: str, ref: str | None) -> str | None:
    out = _git_out(cwd, ["rev-parse", ref or "HEAD"]).strip()
    return out or None


def review_commit(cwd: str, message: str, push: bool) -> dict:
    """Commit locally; network writes always require a separate approval."""
    if push:
        raise RuntimeError("Push approval is required; commit and push are separate actions.")

    _, raw, _ = _git(cwd, ["status", "--porcelain=v2", "-z"])
    if not any(_entry_staged(tag, xy) for tag, xy, _ in _walk_entries(raw)):
        _git_ok(cwd, ["add", "-A"])
    _git_ok(cwd, ["commit", "-m", message])
    return {"ok": True}


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _push_url_identity(cwd: str, raw_url: str) -> tuple[str, str, str]:
    effective = raw_url.strip()
    parsed = urlsplit(effective)
    if parsed.scheme:
        if parsed.scheme == "file":
            display = f"local:{Path(parsed.path).name}"
        else:
            hostname = parsed.hostname or ""
            netloc = f"{hostname}:{parsed.port}" if parsed.port else hostname
            display = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    elif re.match(r"^(?:[^@\s]+@)?[^:/\s]+:.+", effective):
        display = re.sub(r"^[^@\s]+@", "", effective)
    else:
        effective = os.path.realpath(os.path.join(cwd, effective))
        display = f"local:{Path(effective).name}"
    return effective, display, hashlib.sha256(effective.encode()).hexdigest()


def _derive_push_snapshot(cwd: str) -> dict:
    if _git_out(cwd, ["status", "--porcelain"]).strip():
        raise RuntimeError("Commit or discard working tree changes before requesting push approval.")

    branch = _git_out(cwd, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    if not branch or branch == "HEAD":
        raise RuntimeError("A named branch is required before pushing.")

    tracking = _git_out(
        cwd, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    ).strip()
    remote = tracking.split("/", 1)[0] if "/" in tracking else "origin"
    if not _git_out(cwd, ["remote", "get-url", remote]).strip():
        raise RuntimeError(f"Git remote '{remote}' is not configured.")

    push_urls = [
        value.strip()
        for value in _git_out(cwd, ["remote", "get-url", "--push", "--all", remote]).splitlines()
        if value.strip()
    ]
    if len(push_urls) != 1:
        raise RuntimeError("Exactly one Git push destination is required before push approval.")
    effective_push_url, remote_url, remote_url_digest = _push_url_identity(cwd, push_urls[0])

    commit_sha = _git_out(cwd, ["rev-parse", "HEAD"]).strip()
    base_ref = tracking or _branch_base(cwd)
    commit_range = f"{base_ref}..HEAD" if base_ref else "HEAD"
    commits = _git_out(cwd, ["log", "--format=%H%x00%P%x00%s", commit_range])
    if not commits.strip():
        raise RuntimeError("There are no local commits to push.")

    diff = (
        _git_out(cwd, ["diff", "--binary", f"{base_ref}...HEAD"])
        if base_ref
        else _git_out(cwd, ["show", "--binary", "--format=fuller", "--no-ext-diff", "HEAD"])
    )
    digest_payload = {
        "baseRef": base_ref,
        "commitSha": commit_sha,
        "commits": commits,
        "destinationBranch": branch,
        "diff": diff,
        "remote": remote,
        "remoteUrl": remote_url,
        "remoteUrlDigest": remote_url_digest,
    }
    change_set_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "changeSetDigest": change_set_digest,
        "commitSha": commit_sha,
        "destinationBranch": branch,
        "effectivePushUrl": effective_push_url,
        "remote": remote,
        "remoteUrl": remote_url,
        "remoteUrlDigest": remote_url_digest,
    }


def review_create_push_request(cwd: str, now: float | None = None) -> dict:
    timestamp = time.time() if now is None else now
    snapshot = _derive_push_snapshot(cwd)
    effective_push_url = snapshot.pop("effectivePushUrl")
    request = {
        **snapshot,
        "createdAt": _iso_timestamp(timestamp),
        "expiresAt": _iso_timestamp(timestamp + _PUSH_APPROVAL_TTL_SECONDS),
        "requestId": str(uuid.uuid4()),
    }
    record = {
        "consumed": False,
        "cwd": os.path.realpath(cwd),
        "effectivePushUrl": effective_push_url,
        "request": request,
    }

    with _push_approvals_lock:
        for request_id, existing in list(_push_approvals.items()):
            expires_at = datetime.fromisoformat(
                existing["request"]["expiresAt"].replace("Z", "+00:00")
            ).timestamp()
            if existing["consumed"] or expires_at <= timestamp:
                del _push_approvals[request_id]
        _push_approvals[request["requestId"]] = record
        _persist_push_record(record, timestamp=timestamp)

    return request


def review_push_approved(cwd: str, decision: dict, now: float | None = None) -> dict:
    timestamp = time.time() if now is None else now
    request_id = str(decision.get("requestId") or "")

    with _push_approvals_lock:
        record = _consume_push_record(request_id, timestamp=timestamp)
        _push_approvals[request_id] = record

    request = record["request"]
    expires_at = datetime.fromisoformat(request["expiresAt"].replace("Z", "+00:00")).timestamp()
    if expires_at <= timestamp:
        raise RuntimeError("Push approval request expired.")
    if os.path.realpath(cwd) != record["cwd"]:
        raise RuntimeError("Push approval repository changed.")
    if decision.get("approved") is not True or not str(decision.get("approvedBy") or "").strip():
        raise RuntimeError("Push approval was not granted.")

    bound_fields = (
        "requestId",
        "changeSetDigest",
        "commitSha",
        "remote",
        "remoteUrl",
        "remoteUrlDigest",
        "destinationBranch",
        "createdAt",
        "expiresAt",
    )
    if any(decision.get(field) != request[field] for field in bound_fields):
        raise RuntimeError("Push approval no longer matches the requested change set.")

    live = _derive_push_snapshot(cwd)
    live_fields = (
        "changeSetDigest",
        "commitSha",
        "remote",
        "remoteUrl",
        "remoteUrlDigest",
        "destinationBranch",
    )
    if any(live[field] != request[field] for field in live_fields):
        raise RuntimeError("Repository changed after push approval was requested.")

    _git_ok(
        cwd,
        [
            "push",
            record["effectivePushUrl"],
            f'{request["commitSha"]}:refs/heads/{request["destinationBranch"]}',
        ],
    )
    _git_ok(
        cwd,
        [
            "update-ref",
            f'refs/remotes/{request["remote"]}/{request["destinationBranch"]}',
            request["commitSha"],
        ],
    )
    _git_ok(
        cwd,
        [
            "branch",
            "--set-upstream-to",
            f'{request["remote"]}/{request["destinationBranch"]}',
            request["destinationBranch"],
        ],
    )
    return {"commitSha": request["commitSha"], "ok": True}


def review_push_approved_by_request_id(decision: dict, now: float | None = None) -> dict:
    request_id = str(decision.get("requestId") or "")
    with _push_approvals_lock:
        record = _push_approvals.get(request_id) or _load_push_record(request_id)
        cwd = str(record.get("cwd") or "") if record else ""
    if not cwd:
        raise RuntimeError("Push approval request is unknown or already consumed.")
    return review_push_approved(cwd, decision, now=now)


def review_push(cwd: str) -> dict:
    raise RuntimeError("Push approval is required.")


def review_commit_context(cwd: str) -> dict:
    """Diff of what WILL commit + recent subjects, for drafting a commit message."""
    if not _is_dir(cwd):
        return {"diff": "", "recent": ""}
    code, raw, _ = _git(cwd, ["status", "--porcelain=v2", "-z"])
    if code != 0:
        return {"diff": "", "recent": ""}
    entries = list(_walk_entries(raw))

    has_staged = any(_entry_staged(tag, xy) for tag, xy, _ in entries)
    diff = _git_out(cwd, ["diff", "--cached"]) if has_staged else _git_out(cwd, ["diff", "HEAD"])
    if len(diff) > _COMMIT_CONTEXT_DIFF_MAX_CHARS:
        omitted = len(diff) - _COMMIT_CONTEXT_DIFF_MAX_CHARS
        diff = f"{diff[:_COMMIT_CONTEXT_DIFF_MAX_CHARS]}\n# diff truncated: {omitted} chars omitted\n"

    untracked = [path for tag, _xy, path in entries if tag == "?"]
    if untracked:
        visible = untracked[:_COMMIT_CONTEXT_UNTRACKED_MAX]
        note = "\n# New (untracked) files:\n" + "".join(f"#   {p}\n" for p in visible)
        if len(untracked) > len(visible):
            note += f"#   ... {len(untracked) - len(visible)} more omitted\n"
        diff = f"{diff}{note}" if diff else note

    return {"diff": diff or "", "recent": _git_out(cwd, ["log", "-n", "10", "--pretty=format:%s"]).strip()}


# ── ship flow (gh) ───────────────────────────────────────────────────────────


def _gh(cwd: str, args: list[str]) -> tuple[bool, str]:
    if not shutil.which("gh"):
        return False, ""
    # Same non-interactive contract as _git: these serve REST requests, so gh
    # must fail fast instead of prompting (GH_PROMPT_DISABLED is gh's own
    # documented kill-switch for interactive prompts).
    env = noninteractive_git_env()
    env["GH_PROMPT_DISABLED"] = "1"
    try:
        proc = subprocess.run(
            ["gh", *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=_GH_TIMEOUT,
            stdin=subprocess.DEVNULL, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return proc.returncode == 0, proc.stdout or ""


def review_ship_info(cwd: str) -> dict:
    """Git push readiness plus gh availability/auth and this branch's PR."""
    if not _is_dir(cwd):
        return {"ghReady": False, "pr": None, "pushAvailable": False}

    try:
        _derive_push_snapshot(cwd)
        push_available = True
    except RuntimeError:
        push_available = False

    auth_ok, _ = _gh(cwd, ["auth", "status"])
    if not auth_ok:
        return {"ghReady": False, "pr": None, "pushAvailable": push_available}
    view_ok, out = _gh(cwd, ["pr", "view", "--json", "url,state,number"])
    if not view_ok:
        return {"ghReady": True, "pr": None, "pushAvailable": push_available}
    try:
        pr = json.loads(out)
    except json.JSONDecodeError:
        return {"ghReady": True, "pr": None, "pushAvailable": push_available}
    if pr and pr.get("url"):
        return {
            "ghReady": True,
            "pr": {
                "url": pr["url"],
                "state": pr.get("state"),
                "number": pr.get("number"),
            },
            "pushAvailable": push_available,
        }
    return {"ghReady": True, "pr": None, "pushAvailable": push_available}


# GraphQL asks per branch, so the answer can't be crowded out the way a
# `gh pr list` page can. Aliases let one request carry many branches; 50 keeps
# the document well inside GitHub's node budget.
_PR_QUERY_BRANCH_CHUNK = 50
_PR_QUERY_BRANCH_CAP = 300


_PR_NODE_FIELDS = "number state isDraft isCrossRepository title url headRefName"


def _pr_query(owner: str, name: str, branches: list[str], numbers: list[int]) -> str:
    fields = [
        f"b{i}: pullRequests(headRefName: {json.dumps(branch)}, first: 5, "
        f"orderBy: {{field: CREATED_AT, direction: DESC}}) "
        f"{{ nodes {{ {_PR_NODE_FIELDS} }} }}"
        for i, branch in enumerate(branches)
    ]
    # A PR recovered from a transcript is known by number, and asking for it
    # directly also tells us its branch — so it lands in the same by-branch map
    # as everything else.
    fields += [f"n{i}: pullRequest(number: {n}) {{ {_PR_NODE_FIELDS} }}" for i, n in enumerate(numbers)]
    return (
        f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
        + "\n".join(fields)
        + "\n} }"
    )


def _pr_payload(pr: dict) -> dict:
    return {
        "branch": str(pr.get("headRefName")),
        "draft": bool(pr.get("isDraft")),
        "number": int(pr.get("number") or 0),
        "state": str(pr.get("state") or "").lower(),
        "title": str(pr.get("title") or ""),
        "url": str(pr.get("url") or ""),
    }


def review_pr_list(cwd: str, branches: list[str], numbers: list[int] = None) -> dict:
    """The PRs on the given branches (plus any asked for by number). Asks GitHub
    about the branches we actually have sessions on rather than listing the
    repo's newest PRs and hoping ours are in the page."""
    if not _is_dir(cwd):
        return {"ghReady": False, "prs": []}
    wanted = list(dict.fromkeys(str(b) for b in (branches or []) if b))[:_PR_QUERY_BRANCH_CAP]
    by_number = list(dict.fromkeys(int(n) for n in (numbers or []) if n))[:_PR_QUERY_BRANCH_CAP]
    if not wanted and not by_number:
        return {"ghReady": False, "prs": []}
    repo_ok, repo_out = _gh(cwd, ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    owner, _, name = repo_out.strip().partition("/")
    if not repo_ok or not owner or not name:
        # gh missing, unauthenticated, or no GitHub remote — all "nothing to badge".
        return {"ghReady": False, "prs": []}

    prs: list[dict] = []
    chunks = [
        (wanted[i : i + _PR_QUERY_BRANCH_CHUNK], [])
        for i in range(0, len(wanted), _PR_QUERY_BRANCH_CHUNK)
    ] + [
        ([], by_number[i : i + _PR_QUERY_BRANCH_CHUNK])
        for i in range(0, len(by_number), _PR_QUERY_BRANCH_CHUNK)
    ]
    for branch_chunk, number_chunk in chunks:
        ok, out = _gh(cwd, ["api", "graphql", "-f", f"query={_pr_query(owner, name, branch_chunk, number_chunk)}"])
        if not ok:
            continue
        try:
            repository = (json.loads(out).get("data") or {}).get("repository") or {}
        except json.JSONDecodeError:
            continue  # A malformed chunk drops its branches; the rest still resolve.
        for key, field in repository.items():
            if not field:
                continue
            if key.startswith("n"):
                # Asked for by number, so it's ours by construction — a fork PR
                # can't be recovered from our own transcript.
                if field.get("headRefName"):
                    prs.append(_pr_payload(field))
                continue
            # Fork PRs share our branch namespace: a contributor's `main` is how
            # a session sitting on trunk ends up badged with a stranger's closed
            # PR. Only this repo's own branches describe our sessions.
            nodes = field.get("nodes") or []
            pr = next((n for n in nodes if n and not n.get("isCrossRepository")), None)
            if pr and pr.get("headRefName"):
                prs.append(_pr_payload(pr))
    return {"ghReady": True, "prs": prs}


def review_create_pr(cwd: str) -> dict:
    """Create a PR only after the approved commit is already pushed."""
    upstream = _git_out(
        cwd, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    ).strip()
    ahead = _git_out(cwd, ["rev-list", "--count", "@{u}..HEAD"]).strip() if upstream else ""
    if not upstream or ahead != "0":
        raise RuntimeError("Approve and complete the push before creating a pull request.")

    branch = _git_out(cwd, ["branch", "--show-current"]).strip()
    remote = _git_out(cwd, ["config", "--get", f"branch.{branch}.remote"]).strip()
    remote_url = _git_out(cwd, ["remote", "get-url", "--push", remote]).strip()
    parsed = urlsplit(remote_url)
    if parsed.scheme:
        host = (parsed.hostname or "").lower()
        repo_path = parsed.path
    else:
        match = re.fullmatch(r"(?:[^@\s]+@)?([^:/\s]+):(.+)", remote_url)
        host = match.group(1).lower() if match else ""
        repo_path = match.group(2) if match else ""
    github_repo = repo_path.lstrip("/").removesuffix(".git")
    if host != "github.com" or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_repo):
        raise RuntimeError("The upstream remote is not a canonical GitHub repository.")

    created, out = _gh(cwd, ["pr", "create", "--fill", "--repo", github_repo])
    if not created:
        raise RuntimeError("gh pr create failed (is gh installed and authenticated?)")
    url = next((line for line in reversed(out.strip().splitlines()) if line.strip()), "")
    return {"url": url}


# ── worktrees & branches ─────────────────────────────────────────────────────


def _parse_worktrees(out: str) -> list[dict]:
    trees: list[dict] = []
    cur: dict | None = None
    for line in out.split("\n"):
        if line.startswith("worktree "):
            if cur:
                trees.append(cur)
            cur = {"path": line[9:].strip(), "branch": None, "detached": False, "bare": False, "locked": False}
        elif cur is None:
            continue
        elif line.startswith("branch "):
            cur["branch"] = line[7:].strip().replace("refs/heads/", "", 1)
        elif line == "detached":
            cur["detached"] = True
        elif line == "bare":
            cur["bare"] = True
        elif line.startswith("locked"):
            cur["locked"] = True
    if cur:
        trees.append(cur)
    return trees


def worktree_list(cwd: str) -> list[dict]:
    out = _git_out(cwd, ["worktree", "list", "--porcelain"])
    if not out:
        return []
    return [
        {
            "path": tree["path"],
            "branch": tree["branch"],
            "isMain": index == 0,
            "detached": tree["detached"],
            "locked": tree["locked"],
        }
        for index, tree in enumerate(_parse_worktrees(out))
    ]


def _main_root(cwd: str) -> str:
    for tree in worktree_list(cwd):
        if tree["isMain"]:
            return tree["path"]
    return cwd


def _sanitize_branch(name: str) -> str:
    value = str(name or "")
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^\w./-]", "", value)
    value = re.sub(r"-{2,}", "-", value)
    value = re.sub(r"/{2,}", "/", value)
    value = re.sub(r"\.{2,}", ".", value)
    return re.sub(r"^[-./]+|[-./]+$", "", value)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower())
    slug = re.sub(r"^-+|-+$", "", slug)[:40].rstrip("-")
    return slug or "work"


def _default_branch(cwd: str) -> str:
    remote = _git_out(
        cwd, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]
    ).strip().replace("origin/", "", 1)
    if remote:
        return remote
    configured = _git_out(cwd, ["config", "--get", "init.defaultBranch"]).strip()
    if configured:
        return configured
    for branch in _TRUNK_BRANCHES:
        if _git_out(cwd, ["show-ref", "--verify", f"refs/heads/{branch}"]).strip():
            return branch
    return ""


def _ensure_repo(cwd: str) -> None:
    """A new project folder may not be a repo (or has no commit to branch from);
    init it with a root commit so worktrees just work. No-op for a committed repo."""
    inside = _git_out(cwd, ["rev-parse", "--is-inside-work-tree"]).strip()
    needs_root = False
    if inside != "true":
        _git_ok(cwd, ["init"])
        needs_root = True
    else:
        code, _, _ = _git(cwd, ["rev-parse", "--verify", "HEAD"])
        needs_root = code != 0
    if needs_root:
        _git_ok(
            cwd,
            [
                "-c",
                "user.email=hermes@localhost",
                "-c",
                "user.name=Hermes",
                "commit",
                "--allow-empty",
                "-m",
                "Initial commit",
            ],
        )


def _unique_dir(base: str) -> str:
    candidate = base
    n = 1
    while os.path.exists(candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def worktree_add(cwd: str, options: dict) -> dict:
    _ensure_repo(cwd)
    root = _main_root(cwd)
    options = options or {}

    existing = _sanitize_branch(options.get("existingBranch") or "")
    if options.get("existingBranch"):
        if not existing:
            raise RuntimeError("Branch name is required.")
        if existing == _default_branch(root):
            _git_ok(root, ["switch", existing])
            return {"path": root, "branch": existing, "repoRoot": root}
        target = _unique_dir(os.path.join(root, ".worktrees", _slugify(existing)))
        _git_ok(root, ["worktree", "add", target, existing])
        return {"path": target, "branch": existing, "repoRoot": root}

    slug = _slugify(options.get("name") or f"work-{os.urandom(4).hex()}")
    branch = _sanitize_branch(options.get("branch") or "") or f"hermes/{slug}"
    target = _unique_dir(os.path.join(root, ".worktrees", slug))
    args = ["worktree", "add", "-b", branch, target]
    if options.get("base"):
        base = str(options["base"])
        # Remote-tracking branches may be stale or missing; fetch just that
        # branch so the local ref is up to date before branching. Ignore fetch
        # failures (offline / no remote) — git will use whatever local ref
        # exists, or raise a clear error below if the ref is entirely missing.
        if base.startswith("origin/"):
            remote_branch = base[len("origin/"):]
            _git(root, ["fetch", "origin", remote_branch])
        args.append(base)
    code, _, err = _git(root, args)
    if code != 0:
        if "already exists" in (err or "").lower():
            _git_ok(root, ["worktree", "add", target, branch])
        else:
            raise RuntimeError(err.strip() or "git worktree add failed")
    return {"path": target, "branch": branch, "repoRoot": root}


def worktree_remove(cwd: str, worktree_path: str, force: bool) -> dict:
    root = _main_root(cwd)
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(worktree_path)
    _git_ok(root, args)
    return {"removed": worktree_path}


def branch_list(cwd: str) -> list[dict]:
    out = _git_out(
        cwd, ["for-each-ref", "--format=%(refname:short)", "--sort=-committerdate", "refs/heads"]
    )
    if not out:
        return []
    trees = worktree_list(cwd)
    path_by_branch = {t["branch"]: t["path"] for t in trees if t["branch"]}
    trunk = _default_branch(cwd)
    return [
        {
            "name": name,
            "checkedOut": name in path_by_branch,
            "isDefault": bool(trunk and name == trunk),
            "worktreePath": path_by_branch.get(name),
        }
        for name in (line.strip() for line in out.split("\n"))
        if name
    ]


def branch_switch(cwd: str, branch: str) -> dict:
    target = _sanitize_branch(branch)
    if not target:
        raise RuntimeError("Branch name is required.")
    _git_ok(cwd, ["switch", target])
    return {"branch": target}


def base_branch_list(cwd: str) -> list[dict]:
    """Local heads + remote-tracking refs for the base-branch picker.

    The remote default (origin/HEAD) is flagged so the UI can preselect it.
    """
    out = _git_out(
        cwd,
        [
            "for-each-ref",
            "--format=%(refname:short)\t%(committerdate:iso)",
            "--sort=-committerdate",
            "refs/heads",
            "refs/remotes",
        ],
    )
    if not out:
        return []
    remote_default = _git_out(
        cwd, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]
    ).strip()
    local_default = _default_branch(cwd) if not remote_default else ""
    result: list[dict] = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        name = line.split("\t")[0]
        result.append(
            {
                "name": name,
                "isRemote": name.startswith("origin/"),
                # origin/HEAD when a remote exists; otherwise the local
                # default (main/master/init.defaultBranch) so a no-remote
                # repo still flags its trunk.
                "isDefault": bool(
                    (remote_default and name == remote_default)
                    or (not remote_default and local_default and name == local_default)
                ),
            }
        )
    return result
