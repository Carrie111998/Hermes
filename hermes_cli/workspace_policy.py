"""Per-board workspace policy — the M3a confinement contract, as a registry.

SCOPE — READ THIS FIRST
-----------------------
This protects against **accidental escape and cooperative execution**. It does
**not** protect against **arbitrary malicious same-UID activity**.

Every assertion is a path predicate, a file check, or a call into Hermes' own
guards — none is an OS-level sandbox. A deliberately adversarial process running
as your user defeats all of them: it can `chdir` after launch, rewrite the config
this module reads, edit the attestation it verifies, or write `kanban.db`
directly. What these checks stop is the failure that actually happened: a worker
launched from the wrong directory that searched the filesystem, found a live
production checkout whose default branch auto-deploys, and ran a command there.

EVIDENCE, NOT MODE
------------------
An earlier revision reported ``contract_satisfied`` from ``mode == sandbox``
without executing anything. A board with no protected paths, no deny globs, no
command matrices and no git fixture reported the contract satisfied on five
assertions. That is fixed here: every one of the contract's **twenty**
assertions is a registry entry that must record ``PASS`` against the **actual
final workspace and a verified launch**. ``contract_satisfied`` is true only
when all twenty did. A missing, skipped, disabled, approximated or unrun check
makes it false — there is no path to true that does not run the check.

FAIL CLOSED MEANS INABILITY IS REFUSAL
--------------------------------------
The same revision treated "could not inspect" as "clean": a git config that
could not be read became "no remotes", a failed ``git status`` became "no
untracked files", and a non-git directory passed a policy demanding an
origin-less git fixture. In sandbox mode every such case is now a refusal.

MODES
-----
``open`` is **explicitly unconfined legacy behaviour**, kept so existing boards
keep working. It runs the launch checks only, never claims the contract, and is
labelled unconfined on every status surface. ``sandbox`` is the contract.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_cli.dispatch_confinement import PreflightRefusal

MODE_OPEN = "open"
MODE_SANDBOX = "sandbox"

PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

ATTESTATION_FILENAME = ".hermes-fixture-attestation.json"
ATTESTATION_VERSION = 1

# The contract's twenty pre-dispatch assertions, verbatim in intent from
# M3A-CONFINEMENT-CONTRACT.md §4. The registry is the specification: a check
# that is not here cannot count, and one that is here cannot be quietly dropped.
ASSERTIONS: tuple = (
    (1, "process-cwd-is-the-fixture"),
    (2, "fixture-under-sandbox-root"),
    (3, "git-has-zero-remotes"),
    (4, "no-object-alternates"),
    (5, "no-secret-files"),
    (6, "no-untracked-source"),
    (7, "hermes-home-under-sandbox-root"),
    (8, "no-live-key-in-hermes-home"),
    (9, "single-query-mode-deny"),
    (10, "required-deny-globs-present"),
    (11, "protected-paths-declared"),
    (12, "workspace-not-protected"),
    (13, "allowed-root-identity-pinned"),
    (14, "fixture-attestation-valid"),
    (15, "fixture-is-a-git-repository"),
    (16, "prohibited-command-matrix-blocked"),
    (17, "allowed-commands-not-false-positived"),
    (18, "shipped-guard-blocks-git-push"),
    (19, "shipped-guard-blocks-vercel"),
    (20, "shipped-guard-refuses-execute-code"),
)
ASSERTION_NAMES: Dict[int, str] = {i: n for i, n in ASSERTIONS}
REQUIRED_ASSERTION_IDS: frozenset = frozenset(ASSERTION_NAMES)

SECRET_FILENAMES = (
    ".env", ".env.local", ".env.production", ".env.development",
    ".npmrc", ".netrc", "credentials.json", "service-account.json",
)
SECRET_DIRNAMES = (".vercel", ".next", ".aws", ".ssh", ".gnupg")

_KEY_PREFIXES = (
    b"sk-", b"sk-ant-", b"sk-or-", b"ghp_", b"gho_", b"github_pat_",
    b"AKIA", b"xoxb-", b"xoxp-", b"AIza", b"up_",
)
_CREDENTIAL_FILE_HINTS = ("key", "token", "secret", "credential", "auth")
_SPACE = " "

# Assertions 18-20 are executed against these, through Hermes' own guards.
GUARD_PROBE_PUSH = "git push origin main"
GUARD_PROBE_VERCEL = "vercel --prod"
GUARD_PROBE_CODE = "import subprocess; subprocess.run(['git', 'push'])"


@dataclass(frozen=True)
class WorkspacePolicy:
    board: str
    mode: str = MODE_OPEN
    allowed_roots: tuple = ()
    protected_paths: tuple = ()
    hermes_home_root: Optional[str] = None
    required_deny_globs: tuple = ()
    prohibited_commands: tuple = ()
    allowed_commands: tuple = ()
    forbidden_key_sha256: tuple = ()

    @property
    def is_sandbox(self) -> bool:
        return self.mode == MODE_SANDBOX


@dataclass
class AssertionResult:
    id: int
    name: str
    status: str
    detail: str = ""


@dataclass
class PolicyReport:
    """What ran, what did not, and why — per assertion.

    ``contract_satisfied`` is deliberately not derivable from configuration.
    """

    board: str
    mode: str
    results: List[AssertionResult] = field(default_factory=list)

    def record(self, assertion_id: int, status: str, detail: str = "") -> None:
        self.results.append(AssertionResult(
            id=assertion_id, name=ASSERTION_NAMES[assertion_id],
            status=status, detail=detail,
        ))

    def _ids(self, status: str) -> set:
        return {r.id for r in self.results if r.status == status}

    @property
    def passed(self) -> set:
        return self._ids(PASS)

    @property
    def failed(self) -> set:
        return self._ids(FAIL)

    @property
    def skipped(self) -> set:
        return self._ids(SKIPPED)

    @property
    def missing(self) -> set:
        return REQUIRED_ASSERTION_IDS - {r.id for r in self.results}

    @property
    def contract_satisfied(self) -> bool:
        return (
            self.mode == MODE_SANDBOX
            and not self.failed
            and not self.skipped
            and not self.missing
            and self.passed == REQUIRED_ASSERTION_IDS
        )

    def summary(self) -> dict:
        return {
            "board": self.board,
            "mode": self.mode,
            "contract_satisfied": self.contract_satisfied,
            "passed": sorted(self.passed),
            "failed": sorted(self.failed),
            "skipped": sorted(self.skipped),
            "missing": sorted(self.missing),
        }


def _as_tuple(value: Any) -> tuple:
    if not value:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v) for v in value if str(v).strip())
    return (str(value),)


def resolve_policy(board: Optional[str] = None, *, config: Any = None) -> WorkspacePolicy:
    """Resolve ``kanban.workspace_policy``.

    A config that cannot be loaded resolves to ``open`` — and ``open`` never
    claims the contract, so an unreadable config can only ever *lose*
    confinement claims, never gain them. A sandbox board's own settings are
    validated separately by :func:`assert_policy_wellformed`, which refuses
    rather than defaulting.
    """
    slug = (board or "").strip() or "default"
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception as exc:
            raise PreflightRefusal(
                f"workspace policy configuration could not be loaded: {exc}"
            ) from exc
    kanban_cfg = (config or {}).get("kanban") or {}
    if not isinstance(kanban_cfg, dict):
        raise PreflightRefusal("kanban config is malformed (expected a mapping)")
    root = kanban_cfg.get("workspace_policy", {})
    if root in (None, ""):
        root = {}
    if not isinstance(root, dict):
        raise PreflightRefusal(
            "kanban.workspace_policy is malformed (expected a mapping)"
        )
    boards = root.get("boards", {})
    if boards in (None, ""):
        boards = {}
    if not isinstance(boards, dict):
        raise PreflightRefusal(
            "kanban.workspace_policy.boards is malformed (expected a mapping)"
        )
    board_cfg = boards.get(slug) or {}
    if not isinstance(board_cfg, dict):
        raise PreflightRefusal(
            f"kanban.workspace_policy.boards.{slug} is malformed"
        )

    def pick(key, default=None):
        return board_cfg[key] if key in board_cfg else root.get(key, default)

    policy = WorkspacePolicy(
        board=slug,
        mode=str(pick("mode", MODE_OPEN)).strip().lower() or MODE_OPEN,
        allowed_roots=_as_tuple(pick("allowed_roots")),
        protected_paths=_as_tuple(pick("protected_paths")),
        hermes_home_root=(pick("hermes_home_root") or None),
        required_deny_globs=_as_tuple(pick("required_deny_globs")),
        prohibited_commands=_as_tuple(pick("prohibited_commands")),
        allowed_commands=_as_tuple(pick("allowed_commands")),
        forbidden_key_sha256=_as_tuple(pick("forbidden_key_sha256")),
    )
    if policy.mode not in {MODE_OPEN, MODE_SANDBOX}:
        raise PreflightRefusal(
            f"board {policy.board!r} declares unknown workspace-policy mode "
            f"{policy.mode!r}; expected 'open' or 'sandbox'"
        )
    return policy


def assert_policy_wellformed(policy: WorkspacePolicy) -> None:
    """A sandbox board must declare every mandatory field, non-empty.

    There is no way to disable a mandatory assertion. An earlier revision let
    ``require_origin_less: false`` (and friends) silently switch checks off while
    still reporting the contract satisfied; those switches no longer exist. A
    board that needs relaxations is an ``open`` board, and open never claims the
    contract.
    """
    if policy.mode not in {MODE_OPEN, MODE_SANDBOX}:
        raise PreflightRefusal(
            f"board {policy.board!r} declares unknown workspace-policy mode "
            f"{policy.mode!r}; expected 'open' or 'sandbox'"
        )
    if not policy.is_sandbox:
        return
    required = {
        "allowed_roots": policy.allowed_roots,
        "protected_paths": policy.protected_paths,
        "hermes_home_root": policy.hermes_home_root,
        "required_deny_globs": policy.required_deny_globs,
        "prohibited_commands": policy.prohibited_commands,
        "allowed_commands": policy.allowed_commands,
    }
    missing = sorted(k for k, v in required.items() if not v)
    if missing:
        raise PreflightRefusal(
            f"board {policy.board!r} is in sandbox mode but declares no "
            f"{missing}; 'unspecified' must never be read as 'unrestricted'"
        )
    for glob in tuple(policy.protected_paths) + tuple(policy.required_deny_globs):
        if _SPACE in glob:
            raise PreflightRefusal(
                f"board {policy.board!r} declares the glob {glob!r}, which "
                f"contains a literal space. fnmatch is literal on whitespace, "
                f"so 'git   push' evades it. Use '*a*b*' form."
            )


# ---------------------------------------------------------------------------
# Path identity
# ---------------------------------------------------------------------------


def _identity(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _under(path: str, root: str) -> bool:
    try:
        p = Path(os.path.realpath(path))
        r = Path(os.path.realpath(root))
    except (OSError, ValueError):
        return False
    return p == r or r in p.parents


def pin_allowed_roots(policy: WorkspacePolicy) -> Dict[str, tuple]:
    """Capture the filesystem identity of every authorized root.

    A root can be a symlink. Resolving it fresh on each check means it can be
    retargeted between policy evaluation and worker release, and the later check
    simply follows it. Pinning identity here is what makes
    :func:`revalidate_allowed_roots` meaningful.
    """
    pinned: Dict[str, tuple] = {}
    for root in policy.allowed_roots:
        ident = _identity(root)
        if ident is None:
            raise PreflightRefusal(
                f"board {policy.board!r} authorized root {root!r} does not "
                f"exist or cannot be stat'd"
            )
        pinned[root] = ident
    return pinned


def revalidate_allowed_roots(task_id: str, authorized, policy: WorkspacePolicy,
                             pinned: Optional[Dict[str, tuple]] = None) -> None:
    """Re-check the roots, and that the workspace is still inside one.

    Runs while the worker is still held at the start barrier.
    """
    if not policy.is_sandbox:
        return
    current = pin_allowed_roots(policy)
    if pinned:
        for root, ident in pinned.items():
            if current.get(root) != ident:
                raise PreflightRefusal(
                    f"task {task_id}: authorized root {root!r} changed identity "
                    f"after policy evaluation (symlink retargeted or directory "
                    f"replaced)"
                )
    workspace = getattr(authorized, "path", str(authorized))
    if not any(_under(workspace, root) for root in policy.allowed_roots):
        raise PreflightRefusal(
            f"task {task_id}: final workspace {workspace!r} is not inside any "
            f"authorized root {list(policy.allowed_roots)}"
        )


# ---------------------------------------------------------------------------
# Git fixture state — inability to inspect is refusal
# ---------------------------------------------------------------------------


def _git_dir(path: str) -> Path:
    p = Path(path) / ".git"
    if p.is_dir():
        return p
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            raise PreflightRefusal(
                f"fixture {path!r}: .git indirection could not be read ({exc})"
            ) from exc
        if not text.startswith("gitdir:"):
            raise PreflightRefusal(
                f"fixture {path!r}: .git file is not a gitdir indirection"
            )
        target = Path(text.split(":", 1)[1].strip())
        if not target.exists():
            raise PreflightRefusal(
                f"fixture {path!r}: .git points at {target}, which does not exist"
            )
        return target
    raise PreflightRefusal(
        f"fixture {path!r} is not a git repository; an evaluation fixture must "
        f"be an origin-less git checkout so its history can be asserted"
    )


def _git(path: str, *args: str) -> str:
    """Run git, refusing on failure, timeout, or a missing binary."""
    try:
        out = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as exc:
        raise PreflightRefusal(
            f"fixture {path!r}: git is not installed, so fixture state cannot "
            f"be asserted"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PreflightRefusal(
            f"fixture {path!r}: `git {' '.join(args)}` timed out"
        ) from exc
    if out.returncode != 0:
        raise PreflightRefusal(
            f"fixture {path!r}: `git {' '.join(args)}` failed "
            f"(rc={out.returncode}): {(out.stderr or '').strip()[:200]}"
        )
    return out.stdout


def assert_no_remotes(path: str) -> None:
    if _git(path, "remote").strip():
        raise PreflightRefusal(
            f"fixture {path!r} has a git remote; an evaluation fixture must be "
            f"origin-less so a push has nowhere to go"
        )


def assert_no_alternates(path: str) -> None:
    gitdir = _git_dir(path)
    alternates = gitdir / "objects" / "info" / "alternates"
    if alternates.exists():
        raise PreflightRefusal(
            f"fixture {path!r} has an alternate object database ({alternates}); "
            f"the canonical repository's objects are reachable from it"
        )


def assert_no_untracked_source(path: str) -> None:
    out = _git(path, "status", "--porcelain", "--untracked-files=normal")
    untracked = [
        ln[3:] for ln in out.splitlines()
        # The attestation is written INTO the fixture by the builder, so it is
        # untracked by construction. Excluding it is not a loophole: its own
        # contents are what assertion 14 verifies.
        if ln.startswith("?? ") and ln[3:].strip() != ATTESTATION_FILENAME
    ]
    if untracked:
        raise PreflightRefusal(
            f"fixture {path!r} has untracked files ({untracked[:5]}); an "
            f"evaluation fixture must be built from a clean archive so the "
            f"answer cannot be sitting in the tree"
        )


def assert_no_secret_files(path: str) -> None:
    base = Path(path)
    for name in SECRET_FILENAMES:
        if (base / name).exists():
            raise PreflightRefusal(
                f"fixture {path!r} contains {name}; refusing to dispatch a "
                f"worker into a directory carrying secrets or deploy config"
            )
    for name in SECRET_DIRNAMES:
        if (base / name).is_dir():
            raise PreflightRefusal(
                f"fixture {path!r} contains {name}/; refusing to dispatch a "
                f"worker into a directory carrying secrets or deploy config"
            )


# ---------------------------------------------------------------------------
# Fixture-build attestation
# ---------------------------------------------------------------------------


def fixture_identity(path: str) -> dict:
    """Immutable git identity: HEAD, refs, object inventory, reflogs, alternates.

    Cheap enough to recompute per dispatch on an evaluation fixture, which is
    what makes a build-time attestation verifiable at dispatch time instead of
    re-auditing the object graph.
    """
    gitdir = _git_dir(path)
    head = _git(path, "rev-parse", "HEAD").strip()
    refs = _git(path, "show-ref", "--head")
    objects = _git(path, "rev-list", "--objects", "--all")
    reflog_digest = "none"
    logs = gitdir / "logs"
    if logs.exists():
        h = hashlib.sha256()
        for dirpath, _d, files in os.walk(logs):
            for fname in sorted(files):
                try:
                    h.update((Path(dirpath) / fname).read_bytes())
                except OSError as exc:
                    raise PreflightRefusal(
                        f"fixture {path!r}: reflog {fname} could not be read "
                        f"({exc})"
                    ) from exc
        reflog_digest = h.hexdigest()
    # Remotes are part of the attested identity: an origin-less fixture that
    # later gains a remote has drifted from what was audited, even though its
    # objects and refs are untouched.
    remotes = _git(path, "remote", "-v")
    return {
        "version": ATTESTATION_VERSION,
        "head": head,
        "remotes_sha256": hashlib.sha256(remotes.encode()).hexdigest(),
        "refs_sha256": hashlib.sha256(refs.encode()).hexdigest(),
        "objects_sha256": hashlib.sha256(objects.encode()).hexdigest(),
        "object_count": len([ln for ln in objects.splitlines() if ln.strip()]),
        "reflog_sha256": reflog_digest,
        "has_alternates": (gitdir / "objects" / "info" / "alternates").exists(),
    }


def build_fixture_attestation(path: str, *, build_source: str = "",
                              policy_version: str = "") -> dict:
    """Builder-side: record what this fixture was, once, at build time.

    INTEGRITY, NOT SECURITY. The attestation lives in the fixture and is
    writable by the same user as everything else here. It proves the fixture has
    not *drifted* since it was built; it cannot stop someone who edits both the
    fixture and the attestation.
    """
    identity = fixture_identity(path)
    identity["build_source"] = build_source
    identity["policy_version"] = policy_version
    target = Path(path) / ATTESTATION_FILENAME
    target.write_text(json.dumps(identity, indent=2, sort_keys=True),
                      encoding="utf-8")
    return identity


def verify_fixture_attestation(task_id: str, path: str) -> None:
    """Dispatch-side: the fixture is still exactly what the builder attested."""
    target = Path(path) / ATTESTATION_FILENAME
    if not target.exists():
        raise PreflightRefusal(
            f"task {task_id}: fixture {path!r} carries no build attestation "
            f"({ATTESTATION_FILENAME}); its history cannot be asserted at "
            f"dispatch time"
        )
    try:
        recorded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreflightRefusal(
            f"task {task_id}: fixture attestation could not be read ({exc})"
        ) from exc
    if recorded.get("version") != ATTESTATION_VERSION:
        raise PreflightRefusal(
            f"task {task_id}: fixture attestation version "
            f"{recorded.get('version')!r} is not {ATTESTATION_VERSION}"
        )
    current = fixture_identity(path)
    for key in ("head", "refs_sha256", "objects_sha256", "object_count",
                "reflog_sha256", "has_alternates", "remotes_sha256"):
        if recorded.get(key) != current.get(key):
            raise PreflightRefusal(
                f"task {task_id}: fixture {path!r} has drifted from its build "
                f"attestation ({key} differs); rebuild the fixture"
            )


# ---------------------------------------------------------------------------
# HERMES_HOME
# ---------------------------------------------------------------------------


def scan_for_key_material(home: str, *, forbidden_sha256: tuple = (),
                          strict: bool = True) -> List[str]:
    """Report PATHS carrying credential-shaped material. Never values.

    Eligible local files ARE read, in binary, for prefix and digest detection.
    Matched credential values are never returned, logged, or placed in a refusal
    message: the caller learns *where* to look, never *what* is there. An
    eligible file that cannot be read is reported as a finding under ``strict``
    — inability to inspect is not evidence of cleanliness.
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
            except OSError as exc:
                if strict:
                    findings.append(f"{fpath} (unreadable: {exc.strerror})")
                continue
            if forbidden_sha256:
                if hashlib.sha256(blob).hexdigest() in forbidden_sha256:
                    findings.append(f"{fpath} (matches a forbidden key digest)")
                    continue
            if any(prefix in blob for prefix in _KEY_PREFIXES):
                findings.append(f"{fpath} (contains provider key material)")
            elif any(h in fname.lower() for h in _CREDENTIAL_FILE_HINTS) and blob.strip():
                findings.append(f"{fpath} (credential-shaped filename)")
    return findings


def assert_hermes_home(task_id: str, policy: WorkspacePolicy,
                       *, home: Optional[str] = None) -> None:
    resolved = home or os.environ.get("HERMES_HOME") or ""
    if not resolved:
        raise PreflightRefusal(
            f"task {task_id}: HERMES_HOME is not set, so the worker's "
            f"configuration root cannot be confined"
        )
    if not os.path.isdir(resolved):
        raise PreflightRefusal(
            f"task {task_id}: HERMES_HOME {resolved!r} is not a directory"
        )
    if not policy.hermes_home_root or not _under(resolved, policy.hermes_home_root):
        raise PreflightRefusal(
            f"task {task_id}: HERMES_HOME {resolved!r} is outside the sandbox "
            f"root {policy.hermes_home_root!r}"
        )


def assert_no_live_key(task_id: str, policy: WorkspacePolicy,
                       *, home: Optional[str] = None) -> None:
    resolved = home or os.environ.get("HERMES_HOME") or ""
    findings = scan_for_key_material(
        resolved, forbidden_sha256=policy.forbidden_key_sha256, strict=True
    )
    if findings:
        raise PreflightRefusal(
            f"task {task_id}: credential material (or unreadable files) under "
            f"HERMES_HOME. Locations only, values are never read out: "
            f"{findings[:5]}"
        )


# ---------------------------------------------------------------------------
# Command guards — the shipped ones, executed directly
# ---------------------------------------------------------------------------


def _resolved_approvals(config: Any = None) -> dict:
    if config is not None:
        return (config or {}).get("approvals") or {}
    try:
        from hermes_cli.config import load_config_readonly

        return (load_config_readonly() or {}).get("approvals") or {}
    except Exception as exc:
        raise PreflightRefusal(
            f"the resolved approvals config could not be read ({exc}); "
            f"command guards cannot be asserted"
        ) from exc


def assert_single_query_deny(task_id: str, config: Any = None) -> None:
    mode = str(_resolved_approvals(config).get("single_query_mode", "")).strip().lower()
    if mode != "deny":
        raise PreflightRefusal(
            f"task {task_id}: approvals.single_query_mode is {mode!r}, not "
            f"'deny'. Kanban workers run as single-query (-q) sessions, and "
            f"'deny' is what blocks execute_code entirely for them"
        )


def assert_required_deny_globs(task_id: str, policy: WorkspacePolicy,
                               config: Any = None) -> None:
    configured = set(_as_tuple(_resolved_approvals(config).get("deny")))
    missing = [g for g in policy.required_deny_globs if g not in configured]
    if missing:
        raise PreflightRefusal(
            f"task {task_id}: approvals.deny is missing required globs {missing}"
        )
    bad = [g for g in configured if _SPACE in g]
    if bad:
        raise PreflightRefusal(
            f"task {task_id}: approvals.deny contains globs with literal spaces "
            f"{bad}; 'git   push' evades them. Use '*a*b*' form."
        )


def shipped_guard_blocks(command: str) -> bool:
    """Ask Hermes' OWN command guard, not a re-implementation.

    An earlier revision matched ``fnmatch`` against the deny list itself. That
    can drift from runtime semantics, and drift is exactly what an assertion is
    supposed to catch. This calls the shipped path so the assertion measures what
    a worker would actually hit.
    """
    from tools.approval import check_all_command_guards

    result = check_all_command_guards(command, "local")
    return not bool((result or {}).get("approved", True))


def shipped_guard_refuses_execute_code(code: str) -> bool:
    """Ask Hermes' OWN execute_code guard, in the condition a worker runs in.

    Kanban workers are single-query (``-q``) sessions, and the guard only
    consults ``approvals.single_query_mode`` when it is IN one — it is
    documented to auto-approve an ordinary local non-interactive session. Probing
    it without that condition would measure a session shape no worker ever has,
    and report a refusal that never happens.

    The condition is passed explicitly to the guard. It must not be expressed
    through ``os.environ``: the dispatcher can share a process with concurrent
    sessions, and process-global state would transiently taint them.
    """
    from tools.approval import check_execute_code_guard

    result = check_execute_code_guard(
        code, "local", assume_single_query_context=True
    )
    return not bool((result or {}).get("approved", True))


def assert_prohibited_matrix(task_id: str, policy: WorkspacePolicy) -> None:
    leaked = [c for c in policy.prohibited_commands if not shipped_guard_blocks(c)]
    if leaked:
        raise PreflightRefusal(
            f"task {task_id}: Hermes' own command guard does NOT block "
            f"{leaked[:5]}"
        )


def assert_allowed_matrix(task_id: str, policy: WorkspacePolicy) -> None:
    blocked = [c for c in policy.allowed_commands if shipped_guard_blocks(c)]
    if blocked:
        raise PreflightRefusal(
            f"task {task_id}: legitimate commands are blocked by the resolved "
            f"guard: {blocked[:5]}"
        )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _check(report: PolicyReport, assertion_id: int, fn) -> None:
    """Run one assertion, record PASS, or record FAIL and re-raise."""
    try:
        fn()
    except PreflightRefusal as exc:
        report.record(assertion_id, FAIL, str(exc))
        raise
    report.record(assertion_id, PASS)


def assert_path_authorized(task_id: str, path: str, policy: WorkspacePolicy,
                           report: Optional[PolicyReport] = None) -> None:
    real = os.path.realpath(path)
    for pattern in policy.protected_paths:
        if _SPACE in pattern:
            raise PreflightRefusal(
                f"workspace_policy for board {policy.board!r} contains a glob "
                f"with a literal space ({pattern!r}); use '*a*b*' form"
            )
        if fnmatch.fnmatch(real, pattern) or fnmatch.fnmatch(path, pattern):
            raise PreflightRefusal(
                f"task {task_id} workspace {real!r} matches protected path "
                f"{pattern!r}; refusing to launch a worker in a canonical, "
                f"live, or otherwise protected checkout"
            )
    if policy.is_sandbox and not any(
        _under(real, root) for root in policy.allowed_roots
    ):
        raise PreflightRefusal(
            f"task {task_id} workspace {real!r} is outside every authorized "
            f"root {list(policy.allowed_roots)}"
        )


def enforce(task_id: str, intended_path: str, *, board: Optional[str] = None,
            policy: Optional[WorkspacePolicy] = None,
            config: Any = None) -> PolicyReport:
    """PRE-CLAIM gate. Judges the DECLARED destination, creating nothing.

    Deliberately partial: the fixture does not exist yet for a scratch or
    worktree task, so fixture-state assertions cannot run here and are recorded
    ``SKIPPED``. :func:`enforce_final` is what runs the complete set against the
    real artifact, while the worker is still held at the start barrier. This
    function therefore never reports the contract satisfied.
    """
    pol = policy or resolve_policy(board, config=config)
    report = PolicyReport(board=pol.board, mode=pol.mode)
    assert_policy_wellformed(pol)

    assert_path_authorized(task_id, intended_path, pol)
    report.record(2, PASS if pol.is_sandbox else SKIPPED,
                  "" if pol.is_sandbox else "open mode")
    report.record(12, PASS)
    report.record(11, PASS if pol.protected_paths else SKIPPED,
                  "" if pol.protected_paths else "no protected paths declared")

    for aid in sorted(REQUIRED_ASSERTION_IDS - {2, 11, 12}):
        report.record(aid, SKIPPED, "deferred to enforce_final (pre-claim stage)")
    return report


def enforce_final(task_id: str, workspace: str, *,
                  policy: WorkspacePolicy,
                  launch=None,
                  pinned_roots: Optional[Dict[str, tuple]] = None,
                  config: Any = None,
                  home: Optional[str] = None) -> PolicyReport:
    """FINAL gate, against the PROVISIONED workspace, before the worker runs.

    Called while the worker is held at the start barrier, so every assertion is
    made about the artifact the worker will actually see — not the anchor that
    was planned before provisioning, which an earlier revision checked instead.
    """
    report = PolicyReport(board=policy.board, mode=policy.mode)
    if not policy.is_sandbox:
        for aid in sorted(REQUIRED_ASSERTION_IDS):
            report.record(aid, SKIPPED, "open mode: explicitly unconfined")
        return report

    assert_policy_wellformed(policy)

    from hermes_cli.dispatch_confinement import VERIFIED

    def _cwd_verified():
        status = getattr(launch, "status", None)
        if status != VERIFIED:
            raise PreflightRefusal(
                f"task {task_id}: the worker's actual working directory was not "
                f"independently verified (status={status!r})"
            )
        observed = os.path.realpath(getattr(launch, "observed_cwd", "") or "")
        if observed != os.path.realpath(workspace):
            raise PreflightRefusal(
                f"task {task_id}: verified cwd {observed!r} is not the final "
                f"workspace {workspace!r}"
            )

    _check(report, 1, _cwd_verified)
    _check(report, 2, lambda: assert_path_authorized(task_id, workspace, policy))
    _check(report, 15, lambda: _git_dir(workspace))
    _check(report, 3, lambda: assert_no_remotes(workspace))
    _check(report, 4, lambda: assert_no_alternates(workspace))
    _check(report, 5, lambda: assert_no_secret_files(workspace))
    _check(report, 6, lambda: assert_no_untracked_source(workspace))
    _check(report, 7, lambda: assert_hermes_home(task_id, policy, home=home))
    _check(report, 8, lambda: assert_no_live_key(task_id, policy, home=home))
    _check(report, 9, lambda: assert_single_query_deny(task_id, config))
    _check(report, 10, lambda: assert_required_deny_globs(task_id, policy, config))
    _check(report, 11, lambda: assert_policy_wellformed(policy))
    _check(report, 12, lambda: assert_path_authorized(task_id, workspace, policy))
    _check(report, 13, lambda: revalidate_allowed_roots(
        task_id, workspace, policy, pinned_roots))
    _check(report, 14, lambda: verify_fixture_attestation(task_id, workspace))
    _check(report, 16, lambda: assert_prohibited_matrix(task_id, policy))
    _check(report, 17, lambda: assert_allowed_matrix(task_id, policy))
    _check(report, 18, lambda: _require(
        shipped_guard_blocks(GUARD_PROBE_PUSH),
        f"task {task_id}: Hermes' command guard does not block "
        f"{GUARD_PROBE_PUSH!r}"))
    _check(report, 19, lambda: _require(
        shipped_guard_blocks(GUARD_PROBE_VERCEL),
        f"task {task_id}: Hermes' command guard does not block "
        f"{GUARD_PROBE_VERCEL!r}"))
    _check(report, 20, lambda: _require(
        shipped_guard_refuses_execute_code(GUARD_PROBE_CODE),
        f"task {task_id}: Hermes' execute_code guard does not refuse a "
        f"push-issuing script"))
    return report


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PreflightRefusal(message)


def policy_status(board: Optional[str] = None, *, config: Any = None,
                  last_report: Optional[PolicyReport] = None) -> dict:
    """Configuration readiness, or evidence from a real run. Never inference.

    ``contract_satisfied`` is reported ONLY from a ``PolicyReport`` produced by
    :func:`enforce_final`. Without one this returns ``configured_ready`` — is
    this board *capable* of satisfying the contract — which is a different
    question and is labelled as such.
    """
    try:
        pol = resolve_policy(board, config=config)
    except PreflightRefusal as exc:
        return {"board": board or "default", "mode": "malformed",
                "configured_ready": False, "contract_satisfied": False,
                "note": f"policy is malformed: {exc}"}
    ready = False
    detail = ""
    if pol.is_sandbox:
        try:
            assert_policy_wellformed(pol)
            ready = True
        except PreflightRefusal as exc:
            detail = str(exc)
    status = {
        "board": pol.board,
        "mode": pol.mode,
        "configured_ready": ready,
        "contract_satisfied": (
            bool(last_report.contract_satisfied) if last_report else False
        ),
        "evidence": last_report.summary() if last_report else None,
        "note": (
            "open mode: EXPLICITLY UNCONFINED legacy behaviour. Launch checks "
            "only. This board does NOT satisfy the confinement contract."
            if not pol.is_sandbox else
            ("sandbox mode, policy complete: the twenty assertions run against "
             "the final workspace before each worker is released. "
             "contract_satisfied reflects the last recorded run, not the config."
             if ready else
             f"sandbox mode, policy INCOMPLETE: {detail}")
        ),
    }
    return status
