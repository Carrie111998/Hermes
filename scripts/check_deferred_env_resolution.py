#!/usr/bin/env python3
"""AST triage for the "resolved TOO LATE" HERMES_HOME bug class.

A callback registered with ``atexit`` / ``weakref.finalize`` / ``signal.signal``
/ ``__del__`` outlives the scope that registered it. Under pytest that gap spans
``monkeypatch`` teardown, so a callback that resolves ``get_hermes_home()`` when
it FIRES writes to the **restored real home** instead of the per-test tempdir.

The known instance dumped 574 junk records into the user's live
``~/.hermes/profiles/main/logs/gateway-exit-diag.log``, each stamped with a
pytest PID and indistinguishable from a real gateway event. Grep cannot find
this class: there is no module-level constant to match, and the resolution sits
*inside* a function — which is the prescribed fix for the sibling "resolved too
EARLY" bug. See the GBrain page ``concepts/import-time-hermes-home-snapshot-bug``,
section "Variant: resolved TOO LATE (deferred callbacks)".

What it does: enumerate every deferred registration, resolve each callback to a
real function definition, then walk its callees looking for a call that reaches
an env resolver AND a write.

THIS IS TRIAGE, NOT PROOF
-------------------------
A hit means "this callback can reach an env-derived path and a write" — not
"this callback is buggy". It still flags the correctly-FIXED
``hermes_cli/gateway_diag.py``, because ``write_diag`` retains a lazy fallback
for its non-deferred callers (which resolve live, correctly, because the process
still holds the home it was launched with).

Adjudicate every hit by asking one question:

    Does the REGISTRATION site capture the path and pass it in?

If yes, the hit is already correct — the lazy fallback it reaches is only for
direct callers. If no, the callback resolves at fire time and is a real leak.

Note also that a resolver can itself be a write:
``tools/environments/base.py::get_sandbox_dir()`` ends in ``p.mkdir(...)``, so
merely *resolving* it creates the directory. Do not assume a read-only-looking
call is harmless.

Resolution rules (each one was paid for)
----------------------------------------
* Follow ``from X import f as g`` edges into module X's def of ``f``, and follow
  local ``a = b`` aliases. The reference instance is two modules and two renames
  from its resolution — without both edges it is invisible.
* REFUSE bare-name matching across modules. A first draft that resolved a called
  name to any same-named def anywhere returned 177 hits over 1129 modules,
  almost all ``list.append`` / ``cursor.execute`` / ``str.replace`` collisions.
  A name that does not resolve through an import edge or a local scope is left
  unresolved.
* Keep the write set NARROW (see ``_BARE_ATTR_WRITES`` / ``_QUALIFIED_WRITES``).
  ``.append`` / ``.write`` / ``.commit`` / ``.execute`` / ``.replace`` are
  excluded on purpose: they collide with in-memory ops and ``str.replace``.
  ``os.replace`` is in the set only in its qualified form.

Calibration — the acceptance test
--------------------------------
A version that finds ZERO on the unfixed tree is broken regardless of how clean
its output looks. Verified 2026-08-11:

* Tree WITHOUT commit 9223e5783 (``d6434ece5^``): flags
  ``hermes_cli/gateway.py:5149`` — the ``atexit.register(_atexit_hook)`` line —
  with the chain ``_atexit_hook -> _exit_diag(=write_diag)`` reaching
  ``get_hermes_home`` in ``hermes_cli/gateway_diag.py``. That is the reference
  instance: two modules and two renames from its resolution.
* Branch ``claude/gateway-diag-atexit-leak``: the hit MOVES to
  ``hermes_cli/gateway_diag.py`` (``register_exit_hook``'s inner ``_hook``) and
  ``hermes_cli/gateway.py`` drops out. The surviving hit is the fixed form —
  benign per the triage caveat above.
* 7 candidates on the deployed branch (``5908bfe42``) with threads excluded,
  out of 51 registrations over ~1120 modules.

The synthetic regression tests in
``tests/scripts/test_check_deferred_env_resolution.py`` lock each resolution
rule, including a positive control that fails if the import-rename edge is
dropped. They do NOT replace the calibration above.

Runtime is ~1-3 min on this repo (dominated by ``ast.parse``, and by whatever
else is competing for the machine).

Usage:
    # Scan the repo this script lives in
    python scripts/check_deferred_env_resolution.py

    # Scan another checkout (calibration against a pre-fix tree)
    python scripts/check_deferred_env_resolution.py --root /path/to/checkout

    # Also flag daemon thread/timer targets. A daemon thread can outlive the
    # env it was started under too, but NOBODY HAS ADJUDICATED THESE. It takes
    # the candidate count from 7 to 68 (61 thread + the same 7 atexit) and the
    # registration count from 51 to 211, measured 2026-08-11. The GBrain page
    # records ~56 from an earlier, rougher pass — 68 is what this script finds.
    python scripts/check_deferred_env_resolution.py --include-threads

    # Machine-readable
    python scripts/check_deferred_env_resolution.py --json

Exit status:
    0 — no candidates found
    1 — candidates found (EXPECTED on this repo; see the triage caveat)
    2 — script error
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── What counts as resolving an env-derived path ─────────────────────────────
# These four are globally unique identifiers, so they may be matched by bare
# name when resolution fails. Every ``events/paths.py`` accessor also counts,
# but is detected structurally (they all funnel into get_default_hermes_root,
# so the callee walk reaches it anyway) rather than by name — "_root" and
# "mailbox_root" are exactly the kind of generic name that produced the 177.
ENV_RESOLVERS = frozenset({
    "get_hermes_home",
    "get_default_hermes_root",
    "get_process_hermes_home",
    "_get_process_hermes_home",
})

# Modules whose functions are, in themselves, an env resolution.
ENV_RESOLVER_MODULES = frozenset({"events.paths"})

# ── What counts as a write ───────────────────────────────────────────────────
# Attribute calls distinctive enough to match unqualified (these are Path
# methods; nothing else in the tree defines them).
_BARE_ATTR_WRITES = frozenset({
    "mkdir",
    "write_text",
    "write_bytes",
    "touch",
    "unlink",
})

# Qualified writes. ``replace`` MUST stay qualified — bare ``.replace`` is
# str.replace and was a top-3 source of the 177 false hits.
_QUALIFIED_WRITES = frozenset({
    ("os", "makedirs"),
    ("os", "replace"),
    ("shutil", "rmtree"),
    ("shutil", "copy"),
    ("shutil", "copy2"),
    ("shutil", "copyfile"),
    ("shutil", "copytree"),
    ("shutil", "copyfileobj"),
    ("json", "dump"),
    ("sqlite3", "connect"),
})

# Builtin called by bare name.
_BARE_NAME_WRITES = frozenset({"open"})

# ── Scan scope ───────────────────────────────────────────────────────────────
# GOTCHA: these are matched against the path RELATIVE to the repo root. This
# checkout may itself live under ``.claude/worktrees/<name>/``, and matching
# against the absolute path's parts makes ``.claude`` swallow the repo ROOT —
# the scan then walks zero files and reports success.
SKIP_DIR_NAMES = frozenset({
    ".git",
    ".claude",
    ".worktrees",
    ".venv",
    ".venv-mempalace",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "site-packages",
    "build",
    "dist",
    "tests",
    "tests-js",
    "test",
})

# Follow at most this many call hops out from a callback. The reference chain
# is 3 hops; anything much deeper is noise, not evidence.
MAX_DEPTH = 8


# ─────────────────────────────────────────────────────────────────────────────
# Scope model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Binding:
    """An import binding: ``kind`` is 'module' (import X as Y) or 'from'."""

    kind: str
    module: str
    name: Optional[str] = None


@dataclass
class Scope:
    module: "ModuleInfo"
    parent: Optional["Scope"] = None
    is_class: bool = False
    enclosing_class: Optional["Scope"] = None
    defs: dict = field(default_factory=dict)
    imports: dict = field(default_factory=dict)
    aliases: dict = field(default_factory=dict)


@dataclass
class ModuleInfo:
    name: str
    path: Path
    rel: str
    tree: ast.Module
    is_package: bool = False
    scope: Scope = None  # type: ignore[assignment]
    # id(node) -> Scope, for the node kinds that are ever looked up. Recording
    # EVERY node costs ~5M dict entries on this repo and dominates the runtime.
    node_scope: dict = field(default_factory=dict)
    # id(FunctionDef|Lambda|ClassDef) -> the child Scope it opens
    child_scope: dict = field(default_factory=dict)


class Index:
    """Dotted-name -> module lookup, parsing LAZILY.

    ``ast.parse`` dominates the runtime (~140ms/file on this repo's larger
    modules), so only two sets of modules get parsed: those that could host a
    registration at all (cheap substring prefilter), and those that resolution
    actually walks into. That is ~350 of 1120 rather than all of them.
    """

    def __init__(self) -> None:
        self._paths: dict[str, tuple[Path, str, bool]] = {}
        self._parsed: dict[str, Optional[ModuleInfo]] = {}
        self._by_rel: dict[str, str] = {}

    def register(self, dotted: str, path: Path, rel: str, is_package: bool) -> None:
        self._paths[dotted] = (path, rel, is_package)
        self._by_rel[rel] = dotted

    @property
    def discovered(self) -> int:
        return len(self._paths)

    @property
    def parsed(self) -> int:
        return sum(1 for m in self._parsed.values() if m is not None)

    def get(self, dotted: str) -> Optional[ModuleInfo]:
        if dotted in self._parsed:
            return self._parsed[dotted]
        entry = self._paths.get(dotted)
        if entry is None:
            self._parsed[dotted] = None
            return None
        path, rel, is_package = entry
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8", errors="replace"), filename=str(path)
            )
        except (SyntaxError, ValueError, OSError):
            self._parsed[dotted] = None
            return None
        mod = ModuleInfo(name=dotted, path=path, rel=rel, tree=tree, is_package=is_package)
        # Cache BEFORE indexing: a self-referential import would otherwise recurse.
        self._parsed[dotted] = mod
        mod.scope = Scope(module=mod)
        _index_body(mod.tree.body, mod.scope, mod)
        return mod

    def get_rel(self, rel: str) -> Optional[ModuleInfo]:
        dotted = self._by_rel.get(rel)
        return self.get(dotted) if dotted is not None else None


def _module_name(rel: Path) -> str:
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(parts)


def _iter_py_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        try:
            rel = path.relative_to(root)
        except ValueError:  # pragma: no cover — rglob guarantees containment
            continue
        # Relative-path filtering: see SKIP_DIR_NAMES.
        if any(part in SKIP_DIR_NAMES for part in rel.parts[:-1]):
            continue
        name = rel.name
        if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
            continue
        yield path


# Anything that could possibly host a deferred registration. Deliberately
# LOOSE: over-matching costs a parse, under-matching silently drops coverage,
# and a detector that quietly finds nothing is the failure mode to avoid.
# Bare ``signal`` is in here on purpose — ``import signal as _sig`` then
# ``_sig.signal(...)`` would slip past a tighter ``signal\.signal\(`` pattern.
_REGISTRATION_HINTS = re.compile(r"atexit|finalize|signal|__del__")
_THREAD_HINTS = re.compile(r"Thread|Timer")


def build_index(root: Path, include_threads: bool = False) -> tuple[Index, list[str]]:
    """Discover every module; return the index plus the registration-host names."""
    index = Index()
    hosts: list[str] = []
    for path in _iter_py_files(root):
        rel = path.relative_to(root)
        dotted = _module_name(rel)
        index.register(dotted, path, rel.as_posix(), rel.name == "__init__.py")
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _REGISTRATION_HINTS.search(source) or (
            include_threads and _THREAD_HINTS.search(source)
        ):
            hosts.append(dotted)
    return index, hosts


# Node kinds whose owning scope is ever queried: call sites (to resolve the
# callee) and function defs (``__del__`` registration sites).
_SCOPE_TRACKED = (ast.Call, ast.FunctionDef, ast.AsyncFunctionDef)


def _index_body(stmts: Iterable[ast.stmt], scope: Scope, mod: ModuleInfo) -> None:
    for stmt in stmts:
        _index_node(stmt, scope, mod)


def _index_node(node: ast.AST, scope: Scope, mod: ModuleInfo) -> None:
    """Attach ``node`` to ``scope``, opening child scopes for defs/lambdas."""
    if isinstance(node, _SCOPE_TRACKED):
        mod.node_scope[id(node)] = scope

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        scope.defs[node.name] = node
        child = Scope(
            module=mod,
            parent=scope,
            enclosing_class=scope if scope.is_class else scope.enclosing_class,
        )
        mod.child_scope[id(node)] = child
        # Decorators and defaults evaluate in the ENCLOSING scope.
        for extra in list(node.decorator_list) + _default_exprs(node.args):
            _index_node(extra, scope, mod)
        _index_body(node.body, child, mod)
        return

    if isinstance(node, ast.ClassDef):
        scope.defs[node.name] = node
        child = Scope(module=mod, parent=scope, is_class=True)
        mod.child_scope[id(node)] = child
        for extra in list(node.decorator_list) + list(node.bases):
            _index_node(extra, scope, mod)
        _index_body(node.body, child, mod)
        return

    if isinstance(node, ast.Lambda):
        child = Scope(
            module=mod,
            parent=scope,
            enclosing_class=scope.enclosing_class,
        )
        mod.child_scope[id(node)] = child
        _index_node(node.body, child, mod)
        return

    if isinstance(node, ast.Import):
        for alias in node.names:
            scope.imports[alias.asname or alias.name.split(".")[0]] = Binding(
                kind="module",
                module=alias.name,
            )
        return

    if isinstance(node, ast.ImportFrom):
        target = _absolute_module(node, mod)
        if target is not None:
            for alias in node.names:
                if alias.name == "*":
                    continue
                scope.imports[alias.asname or alias.name] = Binding(
                    kind="from",
                    module=target,
                    name=alias.name,
                )
        return

    if isinstance(node, ast.Assign):
        # ``a = b`` local alias — the second of the two renames in the
        # reference chain.
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Name)
        ):
            scope.aliases[node.targets[0].id] = node.value.id

    # Compound statements (If / Try / With / For / ...) share the scope, so a
    # ``try: from X import f`` inside a function body still binds correctly.
    for child_node in ast.iter_child_nodes(node):
        _index_node(child_node, scope, mod)


def _default_exprs(args: ast.arguments) -> list[ast.AST]:
    out: list[ast.AST] = [d for d in args.defaults if d is not None]
    out.extend(d for d in args.kw_defaults if d is not None)
    return out


def _absolute_module(node: ast.ImportFrom, mod: "ModuleInfo") -> Optional[str]:
    """Turn ``from ..x import y`` into a dotted module name."""
    if not node.level:
        return node.module
    parts = mod.name.split(".")
    # A package's ``__init__`` IS its own package; a plain module's package is
    # its parent. Getting this wrong silently drops relative import edges.
    package = parts if mod.is_package else parts[:-1]
    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base) if base else None


# ─────────────────────────────────────────────────────────────────────────────
# Name resolution
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Target:
    module: str
    node: ast.AST
    label: str


def _lookup(name: str, scope: Optional[Scope]) -> tuple[Optional[Scope], Optional[str]]:
    """Walk the scope chain; return (scope, kind) where kind is def/alias/import."""
    while scope is not None:
        if name in scope.defs:
            return scope, "def"
        if name in scope.aliases:
            return scope, "alias"
        if name in scope.imports:
            return scope, "import"
        scope = scope.parent
    return None, None


def resolve_callable(
    expr: ast.AST,
    scope: Optional[Scope],
    index: Index,
    _seen: Optional[set] = None,
) -> Optional[Target]:
    """Resolve a called expression to a concrete function def, or None.

    Never falls back to bare-name matching across modules — an unresolvable
    name stays unresolved. That refusal is the whole reason this returns a
    usable number of hits.
    """
    if _seen is None:
        _seen = set()
    if scope is None:
        return None

    if isinstance(expr, ast.Lambda):
        return Target(scope.module.name, expr, "<lambda>")

    if isinstance(expr, ast.Name):
        key = (id(scope), expr.id)
        if key in _seen:
            return None
        _seen.add(key)

        found, kind = _lookup(expr.id, scope)
        if found is None:
            return None
        if kind == "def":
            node = found.defs[expr.id]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return Target(found.module.name, node, expr.id)
            return None
        if kind == "alias":
            return resolve_callable(
                ast.Name(id=found.aliases[expr.id], ctx=ast.Load()), found, index, _seen
            )
        binding = found.imports[expr.id]
        if binding.kind == "module":
            return None
        target_mod = index.get(binding.module)
        if target_mod is None or binding.name is None:
            return None
        return resolve_callable(
            ast.Name(id=binding.name, ctx=ast.Load()), target_mod.scope, index, _seen
        )

    if isinstance(expr, ast.Attribute):
        base = expr.value
        if isinstance(base, ast.Name):
            if base.id == "self" and scope.enclosing_class is not None:
                node = scope.enclosing_class.defs.get(expr.attr)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return Target(scope.module.name, node, f"self.{expr.attr}")
                return None
            module_name = _module_of(base.id, scope, index)
            if module_name is None:
                return None
            target_mod = index.get(module_name)
            if target_mod is None:
                return None
            return resolve_callable(
                ast.Name(id=expr.attr, ctx=ast.Load()), target_mod.scope, index, _seen
            )
    return None


def _module_of(name: str, scope: Optional[Scope], index: Index) -> Optional[str]:
    """Resolve a bare name to the module it is bound to, if any."""
    found, kind = _lookup(name, scope)
    if found is None or kind != "import":
        return None
    binding = found.imports[name]
    if binding.kind == "module":
        return binding.module
    # ``from pkg import submodule`` — only a module if pkg.submodule is indexed.
    candidate = f"{binding.module}.{binding.name}"
    return candidate if index.get(candidate) else None


def _qualifier_name(name: str, scope: Optional[Scope]) -> str:
    """Best-effort stdlib module name behind a qualifier (``import os as _os``)."""
    found, kind = _lookup(name, scope)
    if found is not None and kind == "import":
        binding = found.imports[name]
        if binding.kind == "module":
            return binding.module.split(".")[0]
    return name


# ─────────────────────────────────────────────────────────────────────────────
# Call classification
# ─────────────────────────────────────────────────────────────────────────────


def _iter_calls(node: ast.AST) -> Iterator[ast.Call]:
    """Calls made in a function's own body — nested defs are NOT descended.

    A nested def is only reachable if something calls it, and resolution
    handles that edge. Descending blindly would attribute a nested helper's
    writes to a callback that never invokes it.
    """
    stack: list[ast.AST] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        stack.extend(node.body)
    elif isinstance(node, ast.Lambda):
        stack.append(node.body)
    else:  # pragma: no cover — callers only pass defs/lambdas
        stack.append(node)

    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(current, ast.Call):
            yield current
        stack.extend(ast.iter_child_nodes(current))


def _is_write(call: ast.Call, scope: Optional[Scope]) -> Optional[str]:
    func = call.func
    if isinstance(func, ast.Name):
        if func.id in _BARE_NAME_WRITES:
            return func.id
        return None
    if isinstance(func, ast.Attribute):
        if func.attr in _BARE_ATTR_WRITES:
            return f".{func.attr}"
        if isinstance(func.value, ast.Name):
            qualifier = _qualifier_name(func.value.id, scope)
            if (qualifier, func.attr) in _QUALIFIED_WRITES:
                return f"{qualifier}.{func.attr}"
    return None


def _is_env_resolver(call: ast.Call, target: Optional[Target]) -> Optional[str]:
    if target is not None:
        if target.module in ENV_RESOLVER_MODULES:
            return f"{target.module}.{target.label}"
        node = target.node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ENV_RESOLVERS:
            return node.name
        return None
    # Unresolved fallback, restricted to the four globally-unique identifiers.
    func = call.func
    ident = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return ident if ident in ENV_RESOLVERS else None


# ─────────────────────────────────────────────────────────────────────────────
# Reachability walk
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Evidence:
    name: str
    rel: str
    line: int
    chain: list


def _hop_label(func: ast.AST, resolved: Target) -> str:
    """Name the hop as it reads at the CALL SITE, and expose the rename.

    ``_exit_diag(=write_diag)`` is the whole point of the exercise: it shows
    the alias/import edge that a bare grep cannot cross.
    """
    if isinstance(func, ast.Name):
        callsite = func.id
    elif isinstance(func, ast.Attribute):
        callsite = func.attr
    else:
        return resolved.label
    return callsite if callsite == resolved.label else f"{callsite}(={resolved.label})"


def walk_callback(
    target: Target, index: Index
) -> tuple[Optional[Evidence], Optional[Evidence]]:
    """Depth-first walk of a callback's callees for an env resolution and a write."""
    env_hit: Optional[Evidence] = None
    write_hit: Optional[Evidence] = None
    visited: set = set()
    stack: list[tuple[Target, list[str], int]] = [(target, [target.label], 0)]

    while stack:
        current, chain, depth = stack.pop()
        key = (current.module, id(current.node))
        if key in visited or depth > MAX_DEPTH:
            continue
        visited.add(key)

        mod = index.get(current.module)
        if mod is None:
            continue
        scope = mod.child_scope.get(id(current.node))

        for call in _iter_calls(current.node):
            call_scope = mod.node_scope.get(id(call), scope)
            resolved = resolve_callable(call.func, call_scope, index)

            env_name = _is_env_resolver(call, resolved)
            if env_name is not None:
                if env_hit is None:
                    env_hit = Evidence(env_name, mod.rel, call.lineno, list(chain))
                continue  # an env resolver is a leaf — do not walk into it

            write_name = _is_write(call, call_scope)
            if write_name is not None and write_hit is None:
                write_hit = Evidence(write_name, mod.rel, call.lineno, list(chain))

            if resolved is not None:
                target_mod = index.get(resolved.module)
                if target_mod is not None:
                    stack.append(
                        (resolved, chain + [_hop_label(call.func, resolved)], depth + 1)
                    )

        if env_hit is not None and write_hit is not None:
            break

    return env_hit, write_hit


# ─────────────────────────────────────────────────────────────────────────────
# Registration sites
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Registration:
    kind: str
    rel: str
    line: int
    expr: Optional[ast.AST]
    scope: Optional[Scope]
    node: Optional[ast.AST] = None  # for __del__, the def itself


def _is_call_to(call: ast.Call, module: str, attr: str, scope: Optional[Scope]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == attr:
        if isinstance(func.value, ast.Name):
            return _qualifier_name(func.value.id, scope) == module
        return False
    if isinstance(func, ast.Name):
        found, kind = _lookup(func.id, scope)
        if found is not None and kind == "import":
            binding = found.imports[func.id]
            return binding.kind == "from" and binding.module == module and binding.name == attr
    return False


def _arg(call: ast.Call, position: int, keyword: Optional[str] = None) -> Optional[ast.AST]:
    if keyword is not None:
        for kw in call.keywords:
            if kw.arg == keyword:
                return kw.value
    if len(call.args) > position:
        return call.args[position]
    return None


def _is_daemon(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "daemon" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def find_registrations(
    index: Index, hosts: Iterable[str], include_threads: bool
) -> list[Registration]:
    out: list[Registration] = []
    for dotted in hosts:
        mod = index.get(dotted)
        if mod is None:
            continue
        for node in ast.walk(mod.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__del__":
                out.append(
                    Registration(
                        kind="__del__",
                        rel=mod.rel,
                        line=node.lineno,
                        expr=None,
                        scope=mod.node_scope.get(id(node)),
                        node=node,
                    )
                )
                continue
            if not isinstance(node, ast.Call):
                continue
            scope = mod.node_scope.get(id(node), mod.scope)

            if _is_call_to(node, "atexit", "register", scope):
                out.append(
                    Registration("atexit", mod.rel, node.lineno, _arg(node, 0, "func"), scope)
                )
            elif _is_call_to(node, "weakref", "finalize", scope):
                out.append(
                    Registration("finalize", mod.rel, node.lineno, _arg(node, 1), scope)
                )
            elif _is_call_to(node, "signal", "signal", scope):
                out.append(
                    Registration("signal", mod.rel, node.lineno, _arg(node, 1, "handler"), scope)
                )
            elif include_threads and _is_daemon(node):
                if _is_call_to(node, "threading", "Thread", scope):
                    out.append(
                        Registration("thread", mod.rel, node.lineno, _arg(node, 1, "target"), scope)
                    )
                elif _is_call_to(node, "threading", "Timer", scope):
                    out.append(
                        Registration("timer", mod.rel, node.lineno, _arg(node, 1, "function"), scope)
                    )
    out.sort(key=lambda r: (r.rel, r.line))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    kind: str
    rel: str
    line: int
    callback: str
    env: Evidence
    write: Evidence

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "file": self.rel,
            "line": self.line,
            "callback": self.callback,
            "env": {
                "name": self.env.name,
                "file": self.env.rel,
                "line": self.env.line,
                "chain": self.env.chain,
            },
            "write": {
                "name": self.write.name,
                "file": self.write.rel,
                "line": self.write.line,
                "chain": self.write.chain,
            },
        }


@dataclass
class ScanResult:
    candidates: list
    discovered: int
    parsed: int
    registrations: int


def scan(root: Path, include_threads: bool) -> ScanResult:
    index, hosts = build_index(root, include_threads)
    registrations = find_registrations(index, hosts, include_threads)

    candidates: list[Candidate] = []
    for reg in registrations:
        if reg.node is not None:
            host = index.get_rel(reg.rel)
            if host is None:
                continue
            target = Target(host.name, reg.node, "__del__")
        else:
            if reg.expr is None:
                continue
            target = resolve_callable(reg.expr, reg.scope, index)
            if target is None:
                continue
        env_hit, write_hit = walk_callback(target, index)
        if env_hit is None or write_hit is None:
            continue
        candidates.append(
            Candidate(reg.kind, reg.rel, reg.line, target.label, env_hit, write_hit)
        )

    return ScanResult(candidates, index.discovered, index.parsed, len(registrations))


def _print_report(result: ScanResult, include_threads: bool) -> None:
    candidates = result.candidates
    print(
        f"Scanned {result.discovered} modules ({result.parsed} parsed), "
        f"{result.registrations} deferred registrations"
        f"{' (threads included)' if include_threads else ''}."
    )
    if not candidates:
        print("✅ No callback reaches both an env resolver and a write.")
        return

    print(f"⚠️  {len(candidates)} candidate(s) reach an env resolver AND a write:\n")
    for c in candidates:
        print(f"  {c.rel}:{c.line}  [{c.kind}] -> {c.callback}")
        print(f"      env:   {c.env.name} at {c.env.rel}:{c.env.line}")
        print(f"             via {' -> '.join(c.env.chain)}")
        print(f"      write: {c.write.name} at {c.write.rel}:{c.write.line}")
        print(f"             via {' -> '.join(c.write.chain)}")
        print()
    print("TRIAGE ONLY. Adjudicate each: does the registration site capture the")
    print("path and pass it in? If yes, the hit is already correct.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Find deferred callbacks that resolve an env-derived path when they FIRE.",
    )
    p.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Checkout to scan (default: the repo this script lives in).",
    )
    p.add_argument(
        "--include-threads",
        action="store_true",
        help="Also flag daemon thread/timer targets. NOT adjudicated; 68 hits.",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a report.")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    result = scan(root, args.include_threads)

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "modules": result.discovered,
                    "parsed": result.parsed,
                    "registrations": result.registrations,
                    "candidates": [c.to_dict() for c in result.candidates],
                },
                indent=2,
            )
        )
    else:
        _print_report(result, args.include_threads)

    return 1 if result.candidates else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
