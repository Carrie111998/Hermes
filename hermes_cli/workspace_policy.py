"""Per-board workspace policy — the M3a confinement contract's assertion set.

SCOPE — READ THIS FIRST
-----------------------
This protects against **accidental escape and cooperative execution**. It does
**not** protect against **arbitrary malicious same-UID activity**.

Every assertion here is a path predicate, a file check, or a string match —
none is an OS-level sandbox. A deliberately adversarial process running as your
user defeats all of them: it can `chdir` after launch, rewrite the config this
module reads, or write `kanban.db` directly. The real containment for evaluation
work is the origin-less fixture plus absent credentials; these checks stop the
failure that actually happened.

WHY THIS EXISTS
---------------
During M2a a worker launched from the wrong directory searched the filesystem,
found a live production checkout whose default branch auto-deploys, and ran a
command there. Denials were added and the issue declared closed; a later run
still *started* in the wrong place. The reference implementation
(`sandbox/preflight.py`) then refused two real dispatches — but it was advisory
to a cooperative harness: a dispatch that did not call it got no protection.

This moves that check into the dispatch path, where it cannot be skipped, and
makes it declarative per board rather than per harness.

MODES, AND AN HONEST DEFAULT
----------------------------
``open`` (the default when no policy is configured) runs only the launch checks
that already shipped: a workspace must be absolute, plannable, and a directory.
Existing boards keep working unchanged.

``sandbox`` runs the full contract. **A board that is not in sandbox mode does
not satisfy the confinement contract**, and this module says so rather than
implying otherwise: :func:`policy_status` reports exactly which assertions ran.

That is a deliberate trade. Refusing every dispatch on every existing board for
want of a config key would be a worse failure than the one being prevented — but
"configured open" must never be mistaken for "contained".

CREDENTIAL HANDLING
-------------------
Assertion 8 needs to know whether live key material sits in the sandbox
``HERMES_HOME``. Values are **never** returned, logged, or included in refusal
messages: matches report a path and a pattern name only. Operators who need to
pin a specific key can configure its SHA-256 digest — a digest comparison never
reveals the secret.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_cli.dispatch_confinement import PreflightRefusal

MODE_OPEN = "open"
MODE_SANDBOX = "sandbox"

# Files whose presence in a fixture means credentials or deploy config leaked in.
SECRET_FILENAMES = (
    ".env", ".env.local", ".env.production", ".env.development",
    ".npmrc", ".netrc", "credentials.json", "service-account.json",
)
SECRET_DIRNAMES = (".vercel", ".next", ".aws", ".ssh", ".gnupg")

# Name-shaped credential detection for HERMES_HOME. Content is matched against
# provider key prefixes; the matched VALUE is never retained.
_KEY_PREFIXES = (
    b"sk-", b"sk-ant-", b"sk-or-", b"ghp_", b"gho_", b"github_pat_",
    b"AKIA", b"xoxb-", b"xoxp-", b"AIza", b"up_",
)
_CREDENTIAL_FILE_HINTS = ("key", "token", "secret", "credential", "auth")

# Never write a glob containing a literal space: fnmatch is literal on
# whitespace, so "*git push*" is defeated by "git   push". Use "*a*b*" form.
_SPACE_IN_GLOB = " "


@dataclass(frozen=True)
class WorkspacePolicy:
    board: str
    mode: str = MODE_OPEN
    allowed_roots: tuple = ()
    protected_paths: tuple = ()
    hermes_home_root: Optional[str] = None
    require_origin_less: bool = True
    require_no_alternates: bool = True
    require_no_secrets: bool = True
    require_no_untracked_source: bool = True
    require_single_query_deny: bool = True
    required_deny_globs: tuple = ()
    prohibited_commands: tuple = ()
    allowed_commands: tuple = ()
    forbidden_key_sha256: tuple = ()

    @property
    def is_sandbox(self) -> bool:
        return self.mode == MODE_SANDBOX


@dataclass
class PolicyReport:
    """What actually ran, so "passed" can never be read as "all 20 passed"."""

    board: str
    mode: str
    passed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def contract_satisfied(self) -> bool:
        return self.mode == MODE_SANDBOX and not self.skipped


def _as_tuple(value: Any) -> tuple:
    if not value:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v) for v in value if str(v).strip())
    return (str(value),)


def resolve_policy(board: Optional[str] = None, *, config: Any = None) -> WorkspacePolicy:
    """Resolve ``kanban.workspace_policy`` for a board.

    Board-specific settings override the top-level defaults. A board with no
    entry inherits the top level, which defaults to ``open``.
    """
    slug = (board or "").strip() or "default"
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    root = ((config or {}).get("kanban") or {}).get("workspace_policy") or {}
    board_cfg = (root.get("boards") or {}).get(slug) or {}

    def pick(key, default=None):
        if key in board_cfg:
            return board_cfg[key]
        return root.get(key, default)

    return WorkspacePolicy(
        board=slug,
        mode=str(pick("mode", MODE_OPEN)).strip().lower() or MODE_OPEN,
        allowed_roots=_as_tuple(pick("allowed_roots")),
        protected_paths=_as_tuple(pick("protected_paths")),
        hermes_home_root=(pick("hermes_home_root") or None),
        require_origin_less=bool(pick("require_origin_less", True)),
        require_no_alternates=bool(pick("require_no_alternates", True)),
        require_no_secrets=bool(pick("require_no_secrets", True)),
        require_no_untracked_source=bool(pick("require_no_untracked_source", True)),
        require_single_query_deny=bool(pick("require_single_query_deny", True)),
        required_deny_globs=_as_tuple(pick("required_deny_globs")),
        prohibited_commands=_as_tuple(pick("prohibited_commands")),
        allowed_commands=_as_tuple(pick("allowed_commands")),
        forbidden_key_sha256=_as_tuple(pick("forbidden_key_sha256")),
    )


# ---------------------------------------------------------------------------
# Assertions 1-2, 5, 11-15 — path authorization
# ---------------------------------------------------------------------------


def _under(path: str, root: str) -> bool:
    """True when *path* is *root* or lives beneath it, by real path."""
    try:
        p = Path(os.path.realpath(path))
        r = Path(os.path.realpath(root))
    except (OSError, ValueError):
        return False
    return p == r or r in p.parents


def assert_path_authorized(task_id: str, path: str, policy: WorkspacePolicy) -> None:
    """The workspace must be inside an authorized root and outside every
    protected one.

    Protected paths are checked on the **realpath**, so a symlink spelled
    innocently cannot smuggle a worker into a canonical checkout — the defect
    a reviewer demonstrated against the previous predicate, which canonicalized
    the spelling but never asked whether the destination was allowed.
    """
    real = os.path.realpath(path)

    for pattern in policy.protected_paths:
        if _SPACE_IN_GLOB in pattern:
            raise PreflightRefusal(
                f"workspace_policy for board {policy.board!r} contains a glob "
                f"with a literal space ({pattern!r}); fnmatch is literal on "
                f"whitespace, so such a pattern is trivially evaded. Use "
                f"'*a*b*' form."
            )
        if fnmatch.fnmatch(real, pattern) or fnmatch.fnmatch(path, pattern):
            raise PreflightRefusal(
                f"task {task_id} workspace {real!r} matches protected path "
                f"{pattern!r}; refusing to launch a worker in a canonical, "
                f"live, or otherwise protected checkout"
            )

    if policy.is_sandbox:
        if not policy.allowed_roots:
            raise PreflightRefusal(
                f"board {policy.board!r} is in sandbox mode but declares no "
                f"allowed_roots; refusing rather than treating 'unspecified' "
                f"as 'anywhere'"
            )
        if not any(_under(real, root) for root in policy.allowed_roots):
            raise PreflightRefusal(
                f"task {task_id} workspace {real!r} is outside every authorized "
                f"root {list(policy.allowed_roots)}"
            )


# ---------------------------------------------------------------------------
# Assertions 3-6 — fixture state
# ---------------------------------------------------------------------------


def _git_dir(path: str) -> Optional[Path]:
    p = Path(path) / ".git"
    if p.is_dir():
        return p
    if p.is_file():
        # A linked worktree: .git is a file pointing at the real gitdir.
        try:
            text = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            return target if target.exists() else None
    return None


def assert_fixture_state(task_id: str, path: str, policy: WorkspacePolicy) -> List[str]:
    """Origin-less, alternate-free, secret-free, and free of untracked source.

    ``git push`` having nowhere to go is defence in depth *beneath* the deny
    globs, not a substitute for them. History cleanliness is asserted by the
    fixture builder, not here — a full object-graph audit on every dispatch is
    not affordable; what is checked here is that the fixture cannot reach a
    canonical repository.
    """
    checks: List[str] = []
    gitdir = _git_dir(path)

    if policy.require_origin_less and gitdir is not None:
        config_path = gitdir / "config"
        try:
            text = config_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if '[remote "' in text:
            raise PreflightRefusal(
                f"task {task_id} fixture {path!r} has a git remote configured; "
                f"an evaluation fixture must be origin-less so a push has "
                f"nowhere to go"
            )
        checks.append("origin-less")

    if policy.require_no_alternates and gitdir is not None:
        alternates = gitdir / "objects" / "info" / "alternates"
        if alternates.exists():
            raise PreflightRefusal(
                f"task {task_id} fixture {path!r} has an alternate object "
                f"database ({alternates}); the canonical repository's objects "
                f"are reachable from it"
            )
        checks.append("no-alternates")

    if policy.require_no_secrets:
        for name in SECRET_FILENAMES:
            if (Path(path) / name).exists():
                raise PreflightRefusal(
                    f"task {task_id} fixture {path!r} contains {name}; refusing "
                    f"to dispatch a worker into a directory carrying secrets or "
                    f"deploy configuration"
                )
        for name in SECRET_DIRNAMES:
            if (Path(path) / name).is_dir():
                raise PreflightRefusal(
                    f"task {task_id} fixture {path!r} contains {name}/; refusing "
                    f"to dispatch a worker into a directory carrying secrets or "
                    f"deploy configuration"
                )
        checks.append("no-secrets")

    if policy.require_no_untracked_source and gitdir is not None:
        import subprocess

        try:
            out = subprocess.run(
                ["git", "-C", path, "status", "--porcelain", "--untracked-files=normal"],
                capture_output=True, text=True, timeout=30,
            )
            untracked = [
                ln[3:] for ln in out.stdout.splitlines() if ln.startswith("?? ")
            ]
        except Exception:
            untracked = []
        if untracked:
            raise PreflightRefusal(
                f"task {task_id} fixture {path!r} has untracked files "
                f"({untracked[:5]}); an evaluation fixture must be built from a "
                f"clean archive so the answer cannot be sitting in the tree"
            )
        checks.append("no-untracked-source")

    return checks


# ---------------------------------------------------------------------------
# Assertions 7-8 — the sandbox HERMES_HOME
# ---------------------------------------------------------------------------


def _looks_like_credential_file(path: Path) -> bool:
    name = path.name.lower()
    return any(hint in name for hint in _CREDENTIAL_FILE_HINTS)


def scan_for_key_material(
    home: str, *, forbidden_sha256: tuple = ()
) -> List[str]:
    """Report PATHS that carry credential-shaped material. Never values.

    Returns a list of ``"<path> (<reason>)"`` strings. No secret is returned,
    logged, or placed in a refusal message: the caller learns *where* to look,
    never *what* is there. Digest comparison lets an operator pin a specific
    forbidden key without the digest revealing it.
    """
    findings: List[str] = []
    root = Path(home)
    if not root.is_dir():
        return findings
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            try:
                if fpath.stat().st_size > 1_000_000:
                    continue
                blob = fpath.read_bytes()
            except OSError:
                continue
            if forbidden_sha256:
                digest = hashlib.sha256(blob).hexdigest()
                if digest in forbidden_sha256:
                    findings.append(f"{fpath} (matches a forbidden key digest)")
                    continue
            if any(prefix in blob for prefix in _KEY_PREFIXES):
                findings.append(f"{fpath} (contains provider key material)")
            elif _looks_like_credential_file(fpath) and blob.strip():
                findings.append(f"{fpath} (credential-shaped filename)")
    return findings


def assert_hermes_home(task_id: str, policy: WorkspacePolicy,
                       *, home: Optional[str] = None) -> List[str]:
    resolved = home or os.environ.get("HERMES_HOME") or ""
    if not resolved:
        raise PreflightRefusal(
            f"task {task_id}: HERMES_HOME is not set, so the worker's "
            f"configuration root cannot be confined"
        )
    if policy.hermes_home_root and not _under(resolved, policy.hermes_home_root):
        raise PreflightRefusal(
            f"task {task_id}: HERMES_HOME {resolved!r} is outside the sandbox "
            f"root {policy.hermes_home_root!r}"
        )
    findings = scan_for_key_material(
        resolved, forbidden_sha256=policy.forbidden_key_sha256
    )
    if findings:
        raise PreflightRefusal(
            f"task {task_id}: live key material is present under HERMES_HOME. "
            f"Locations only (values are never read out): {findings[:5]}"
        )
    return ["hermes-home-confined", "no-live-key"]


# ---------------------------------------------------------------------------
# Assertions 9, 10, 16-20 — the resolved guard matrix
# ---------------------------------------------------------------------------


def assert_command_guards(task_id: str, policy: WorkspacePolicy,
                          *, config: Any = None) -> List[str]:
    """Re-run the guard matrix against the RESOLVED config, every dispatch.

    Re-executed per dispatch rather than once at setup: a config edit between
    two dispatches must not silently widen what a worker may run.
    """
    checks: List[str] = []
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    approvals = (config or {}).get("approvals") or {}

    if policy.require_single_query_deny:
        mode = str(approvals.get("single_query_mode", "")).strip().lower()
        if mode != "deny":
            raise PreflightRefusal(
                f"task {task_id}: approvals.single_query_mode is {mode!r}, not "
                f"'deny'. Kanban workers run as single-query (-q) sessions, and "
                f"'deny' is what blocks execute_code entirely for them"
            )
        checks.append("single_query_mode=deny")

    if policy.required_deny_globs:
        configured = set(_as_tuple(approvals.get("deny")))
        missing = [g for g in policy.required_deny_globs if g not in configured]
        if missing:
            raise PreflightRefusal(
                f"task {task_id}: approvals.deny is missing required globs "
                f"{missing}"
            )
        bad = [g for g in configured if _SPACE_IN_GLOB in g]
        if bad:
            raise PreflightRefusal(
                f"task {task_id}: approvals.deny contains globs with literal "
                f"spaces {bad}; fnmatch is literal on whitespace, so "
                f"'git   push' evades them. Use '*a*b*' form."
            )
        checks.append("deny-globs-present")

    if policy.prohibited_commands:
        leaked = [c for c in policy.prohibited_commands
                  if not _command_is_blocked(c, approvals)]
        if leaked:
            raise PreflightRefusal(
                f"task {task_id}: prohibited commands are not blocked by the "
                f"resolved deny set: {leaked[:5]}"
            )
        checks.append("prohibited-matrix-blocked")

    if policy.allowed_commands:
        false_positives = [c for c in policy.allowed_commands
                           if _command_is_blocked(c, approvals)]
        if false_positives:
            raise PreflightRefusal(
                f"task {task_id}: legitimate commands are blocked by the deny "
                f"set: {false_positives[:5]}"
            )
        checks.append("no-false-positives")

    return checks


def _command_is_blocked(command: str, approvals: dict) -> bool:
    """True when the resolved deny set matches *command*.

    Matched against the deobfuscated variants Hermes already computes, so
    ``GIT_DIR=. git   push`` is judged the same as ``git push``.
    """
    patterns = _as_tuple(approvals.get("deny"))
    if not patterns:
        return False
    try:
        from tools.approval import _command_detection_variants as _variants

        candidates = list(_variants(command))
    except Exception:
        candidates = [command, " ".join(command.split())]
    if command not in candidates:
        candidates.append(command)
    for cand in candidates:
        for pattern in patterns:
            if fnmatch.fnmatch(cand, pattern):
                return True
    return False


# ---------------------------------------------------------------------------
# The single entry point the dispatcher calls
# ---------------------------------------------------------------------------


def enforce(task_id: str, intended_path: str, *, board: Optional[str] = None,
            policy: Optional[WorkspacePolicy] = None,
            config: Any = None) -> PolicyReport:
    """Run every assertion this board's policy demands. Raises to refuse.

    Called from the dispatcher BEFORE the task is claimed, so a refusal creates
    no claim, no ``task_runs`` row, no session, and no worker.
    """
    pol = policy or resolve_policy(board, config=config)
    report = PolicyReport(board=pol.board, mode=pol.mode)

    assert_path_authorized(task_id, intended_path, pol)
    report.passed.append("path-authorized")

    if not pol.is_sandbox:
        report.skipped.extend([
            "allowed-root", "fixture-state", "hermes-home", "command-guards",
        ])
        return report

    report.passed.extend(assert_fixture_state(task_id, intended_path, pol))
    report.passed.extend(assert_hermes_home(task_id, pol))
    report.passed.extend(assert_command_guards(task_id, pol, config=config))
    return report


def policy_status(board: Optional[str] = None, *, config: Any = None) -> dict:
    """Machine-readable "is this board actually contained?" answer."""
    pol = resolve_policy(board, config=config)
    return {
        "board": pol.board,
        "mode": pol.mode,
        "contract_satisfied": pol.is_sandbox,
        "note": (
            "sandbox mode: the full confinement assertion set runs before every "
            "dispatch"
            if pol.is_sandbox else
            "open mode: only launch-directory checks run. This board does NOT "
            "satisfy the confinement contract."
        ),
    }
