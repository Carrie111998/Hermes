"""Native IAM for the Hermes Enterprise control plane (OCC).

Model (mirroring the platform spec):

  Principal          - immutable external identity (issuer + subject) with
                       display fields. Created out-of-band by administration;
                       admission can only *resolve*, never create.
  ServicePrincipal   - non-human identity scoped to the installation or to
                       exactly one namespace.
  WorkloadIdentity   - identity of one running agent workload; always bound
                       to exactly one agent in exactly one namespace.
  Group              - installation- or namespace-scoped set of principal uids.
  Role               - named set of exact permission strings
                       (e.g. ``hermes.agents.deploy``). Roles never contain
                       deny rules.
  AccessBinding      - role -> subject at installation, namespace, or
                       exact-resource scope. Bindings are the ONLY way
                       permission is granted.
  Restriction        - deny patterns ``action:Kind[:name]`` that NARROW an
                       otherwise-allowed operation. Restrictions never grant.

Authorization semantics (OCCIAMAdapter.authorize):

  1. Resolve the requesting subject; unknown identity is a denial.
  2. Collect applicable bindings (direct + via group membership).
  3. Allow only if some binding's role grants the exact action at a scope
     that CONTAINS the request:
       installation binding  ⊇ any request
       namespace binding     ⊇ requests in that same namespace
       exact-resource binding⊇ only that exact (kind, namespace, name)
  4. Apply Restrictions (installation-wide + same-namespace): any matching
     deny pattern raises RestrictionError even when a binding allowed.
  5. No applicable binding -> AuthorizationError. Fail closed, always.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .contracts import AuthzRequest, IAMAdapter
from .errors import AuthorizationError, ConflictError, NotFoundError, RestrictionError, ValidationError
from .resources import new_uid, now_ts, validate_name

# ---------------------------------------------------------------------------
# Subject / scope vocabulary
# ---------------------------------------------------------------------------

SUBJECT_PRINCIPAL = "principal"
SUBJECT_SERVICE_PRINCIPAL = "service-principal"
SUBJECT_WORKLOAD_IDENTITY = "workload-identity"
SUBJECT_GROUP = "group"

SUBJECT_KINDS = (
    SUBJECT_PRINCIPAL,
    SUBJECT_SERVICE_PRINCIPAL,
    SUBJECT_WORKLOAD_IDENTITY,
    SUBJECT_GROUP,
)

SCOPE_INSTALLATION = "installation"
SCOPE_NAMESPACE = "namespace"
SCOPE_RESOURCE = "resource"

BINDING_SCOPES = (SCOPE_INSTALLATION, SCOPE_NAMESPACE, SCOPE_RESOURCE)

_PERMISSION_RE = re.compile(r"^[a-z0-9]+(\.[a-zA-Z0-9_-]+)+$")
_DENY_PATTERN_RE = re.compile(r"^[^:\s]+:[^:\s]+(:[^:\s]+)?$")


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Immutable external identity. (issuer, subject) is the durable key."""

    issuer: str
    subject: str
    display_name: str = ""
    email: str = ""
    uid: str = field(default_factory=new_uid)
    created_at: float = field(default_factory=now_ts)

    def validate(self) -> None:
        if not self.issuer or not isinstance(self.issuer, str):
            raise ValidationError("Principal.issuer must be a non-empty string")
        if not self.subject or not isinstance(self.subject, str):
            raise ValidationError("Principal.subject must be a non-empty string")


@dataclass(frozen=True)
class ServicePrincipal:
    """Non-human identity scoped to the installation or exactly one namespace."""

    name: str
    namespace: str | None = None  # None => installation-scoped
    description: str = ""
    uid: str = field(default_factory=new_uid)
    created_at: float = field(default_factory=now_ts)

    def validate(self) -> None:
        validate_name(self.name, "ServicePrincipal")
        if self.namespace is not None:
            validate_name(self.namespace, "Namespace")


@dataclass(frozen=True)
class WorkloadIdentity:
    """Identity of one agent workload; belongs to exactly one agent."""

    name: str
    agent: str        # owning Agent name
    namespace: str    # always namespace-scoped
    uid: str = field(default_factory=new_uid)
    created_at: float = field(default_factory=now_ts)

    def validate(self) -> None:
        validate_name(self.name, "WorkloadIdentity")
        validate_name(self.agent, "Agent")
        if not self.namespace:
            raise ValidationError(
                f"WorkloadIdentity {self.name!r} requires a namespace; workload "
                "identities are always namespace-scoped"
            )
        validate_name(self.namespace, "Namespace")


@dataclass(frozen=True)
class Group:
    """Installation- or namespace-scoped set of principal uids."""

    name: str
    namespace: str | None = None
    members: tuple[str, ...] = ()
    uid: str = field(default_factory=new_uid)
    created_at: float = field(default_factory=now_ts)

    def validate(self) -> None:
        validate_name(self.name, "Group")
        if self.namespace is not None:
            validate_name(self.namespace, "Namespace")
        for m in self.members:
            if not m or not isinstance(m, str):
                raise ValidationError(
                    f"Group {self.name!r} members must be principal uids"
                )


@dataclass(frozen=True)
class Role:
    """Named set of exact permission strings. Roles only ever grant."""

    name: str
    permissions: tuple[str, ...] = ()
    uid: str = field(default_factory=new_uid)
    created_at: float = field(default_factory=now_ts)

    def validate(self) -> None:
        validate_name(self.name, "Role")
        if not self.permissions:
            raise ValidationError(f"Role {self.name!r} must grant at least one permission")
        for p in self.permissions:
            if not isinstance(p, str) or not _PERMISSION_RE.match(p):
                raise ValidationError(
                    f"Role {self.name!r} permission {p!r} is invalid: expected "
                    "dotted action like 'hermes.agents.deploy'"
                )


@dataclass(frozen=True)
class AccessBinding:
    """role -> subject at installation, namespace, or exact-resource scope."""

    role: str                 # Role name
    subject_kind: str         # one of SUBJECT_KINDS
    subject_uid: str
    scope: str                # one of BINDING_SCOPES
    namespace: str | None = None      # required for namespace/resource scope*
    resource_kind: str | None = None  # required for resource scope
    resource_name: str | None = None  # required for resource scope
    uid: str = field(default_factory=new_uid)
    created_at: float = field(default_factory=now_ts)

    # *resource-scope bindings on installation-scoped resource kinds carry
    #  namespace=None.

    def validate(self) -> None:
        validate_name(self.role, "Role")
        if self.subject_kind not in SUBJECT_KINDS:
            raise ValidationError(
                f"AccessBinding.subject_kind must be one of {SUBJECT_KINDS}"
            )
        if not self.subject_uid:
            raise ValidationError("AccessBinding.subject_uid is required")
        if self.scope not in BINDING_SCOPES:
            raise ValidationError(f"AccessBinding.scope must be one of {BINDING_SCOPES}")
        if self.scope == SCOPE_INSTALLATION:
            if self.namespace is not None or self.resource_name is not None:
                raise ValidationError(
                    "installation-scope bindings carry no namespace or resource"
                )
        elif self.scope == SCOPE_NAMESPACE:
            if not self.namespace:
                raise ValidationError("namespace-scope bindings require a namespace")
            if self.resource_name is not None or self.resource_kind is not None:
                raise ValidationError(
                    "namespace-scope bindings must not name an exact resource"
                )
            validate_name(self.namespace, "Namespace")
        else:  # SCOPE_RESOURCE
            if not self.resource_kind or not self.resource_name:
                raise ValidationError(
                    "resource-scope bindings require resource_kind and resource_name"
                )
            if self.namespace is not None:
                validate_name(self.namespace, "Namespace")


@dataclass(frozen=True)
class Restriction:
    """Deny patterns 'action:Kind[:name]'. Narrows; never grants."""

    name: str
    deny: tuple[str, ...]
    namespace: str | None = None  # None => installation-wide
    uid: str = field(default_factory=new_uid)
    created_at: float = field(default_factory=now_ts)

    def validate(self) -> None:
        validate_name(self.name, "Restriction")
        if self.namespace is not None:
            validate_name(self.namespace, "Namespace")
        if not self.deny:
            raise ValidationError(
                f"Restriction {self.name!r} requires at least one deny pattern; "
                "Restrictions can only narrow, never grant"
            )
        for pat in self.deny:
            if not isinstance(pat, str) or not _DENY_PATTERN_RE.match(pat):
                raise ValidationError(
                    f"Restriction {self.name!r} pattern {pat!r} is invalid: "
                    "expected 'action:Kind[:name]'"
                )


_ENTITY_TABLES: dict[type, str] = {
    Principal: "iam_principals",
    ServicePrincipal: "iam_service_principals",
    WorkloadIdentity: "iam_workload_identities",
    Group: "iam_groups",
    Role: "iam_roles",
    AccessBinding: "iam_bindings",
    Restriction: "iam_restrictions",
}


def _to_doc(entity: Any) -> str:
    return json.dumps(asdict(entity))


def _from_doc(cls: type, doc: str) -> Any:
    data = json.loads(doc)
    for key in ("members", "permissions", "deny"):
        if key in data and isinstance(data[key], list):
            data[key] = tuple(data[key])
    return cls(**data)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS iam_principals (
    uid     TEXT PRIMARY KEY,
    issuer  TEXT NOT NULL,
    subject TEXT NOT NULL,
    doc     TEXT NOT NULL,
    UNIQUE (issuer, subject)
);
CREATE TABLE IF NOT EXISTS iam_service_principals (
    uid       TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    namespace TEXT,
    doc       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_iam_sp_identity
    ON iam_service_principals(COALESCE(namespace, ''), name);
CREATE TABLE IF NOT EXISTS iam_workload_identities (
    uid       TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    agent     TEXT NOT NULL,
    namespace TEXT NOT NULL,
    doc       TEXT NOT NULL,
    UNIQUE (namespace, name)
);
CREATE TABLE IF NOT EXISTS iam_groups (
    uid       TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    namespace TEXT,
    doc       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_iam_groups_identity
    ON iam_groups(COALESCE(namespace, ''), name);
CREATE TABLE IF NOT EXISTS iam_roles (
    uid  TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    doc  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS iam_bindings (
    uid         TEXT PRIMARY KEY,
    role        TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_uid TEXT NOT NULL,
    scope       TEXT NOT NULL,
    namespace   TEXT,
    doc         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_iam_bindings_subject ON iam_bindings(subject_uid);
CREATE TABLE IF NOT EXISTS iam_restrictions (
    uid       TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    namespace TEXT,
    doc       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_iam_restrictions_identity
    ON iam_restrictions(COALESCE(namespace, ''), name);
"""


class IAMStore:
    """SQLite-backed store for IAM entities (one per Installation).

    Enforces the structural invariants at write time:

      * (issuer, subject) is unique across Principals
      * namespace-scoped ServicePrincipals/Groups may only receive bindings
        within their own namespace
      * WorkloadIdentity bindings only for its own namespace, or exact
        resources within it — never installation-wide
      * every binding references an existing Role and an existing subject
    """

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_principal(self, principal: Principal) -> Principal:
        principal.validate()
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO iam_principals (uid, issuer, subject, doc)"
                    " VALUES (?,?,?,?)",
                    (principal.uid, principal.issuer, principal.subject,
                     _to_doc(principal)),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"Principal for {principal.issuer!r}/{principal.subject!r} "
                    "already exists"
                ) from exc
        return principal

    def create_service_principal(self, sp: ServicePrincipal) -> ServicePrincipal:
        sp.validate()
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO iam_service_principals (uid, name, namespace, doc)"
                    " VALUES (?,?,?,?)",
                    (sp.uid, sp.name, sp.namespace, _to_doc(sp)),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"ServicePrincipal {sp.name!r} already exists in "
                    f"{sp.namespace or 'installation scope'}"
                ) from exc
        return sp

    def create_workload_identity(self, wi: WorkloadIdentity) -> WorkloadIdentity:
        wi.validate()
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO iam_workload_identities"
                    " (uid, name, agent, namespace, doc) VALUES (?,?,?,?,?)",
                    (wi.uid, wi.name, wi.agent, wi.namespace, _to_doc(wi)),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"WorkloadIdentity {wi.namespace}/{wi.name} already exists"
                ) from exc
        return wi

    def create_group(self, group: Group) -> Group:
        group.validate()
        with self._lock, self._conn:
            for member in group.members:
                if self._get_doc("iam_principals", member) is None:
                    raise ValidationError(
                        f"Group {group.name!r} member {member!r} is not a "
                        "known principal uid"
                    )
            try:
                self._conn.execute(
                    "INSERT INTO iam_groups (uid, name, namespace, doc)"
                    " VALUES (?,?,?,?)",
                    (group.uid, group.name, group.namespace, _to_doc(group)),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"Group {group.name!r} already exists in "
                    f"{group.namespace or 'installation scope'}"
                ) from exc
        return group

    def create_role(self, role: Role) -> Role:
        role.validate()
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO iam_roles (uid, name, doc) VALUES (?,?,?)",
                    (role.uid, role.name, _to_doc(role)),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"Role {role.name!r} already exists") from exc
        return role

    def create_binding(self, binding: AccessBinding) -> AccessBinding:
        binding.validate()
        with self._lock, self._conn:
            if self.get_role(binding.role) is None:
                raise ValidationError(
                    f"AccessBinding references unknown Role {binding.role!r}"
                )
            subject = self._resolve_subject(binding.subject_kind, binding.subject_uid)
            if subject is None:
                raise ValidationError(
                    f"AccessBinding references unknown {binding.subject_kind} "
                    f"{binding.subject_uid!r}"
                )
            self._check_binding_scope(binding, subject)
            self._conn.execute(
                "INSERT INTO iam_bindings (uid, role, subject_kind, subject_uid,"
                " scope, namespace, doc) VALUES (?,?,?,?,?,?,?)",
                (binding.uid, binding.role, binding.subject_kind,
                 binding.subject_uid, binding.scope, binding.namespace,
                 _to_doc(binding)),
            )
        return binding

    def create_restriction(self, restriction: Restriction) -> Restriction:
        restriction.validate()
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO iam_restrictions (uid, name, namespace, doc)"
                    " VALUES (?,?,?,?)",
                    (restriction.uid, restriction.name, restriction.namespace,
                     _to_doc(restriction)),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"Restriction {restriction.name!r} already exists in "
                    f"{restriction.namespace or 'installation scope'}"
                ) from exc
        return restriction

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_principal_by_identity(self, issuer: str, subject: str) -> Principal | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT doc FROM iam_principals WHERE issuer=? AND subject=?",
                (issuer, subject),
            ).fetchone()
        return _from_doc(Principal, row["doc"]) if row else None

    def get_principal(self, uid: str) -> Principal | None:
        doc = self._get_doc("iam_principals", uid)
        return _from_doc(Principal, doc) if doc else None

    def get_service_principal(self, uid: str) -> ServicePrincipal | None:
        doc = self._get_doc("iam_service_principals", uid)
        return _from_doc(ServicePrincipal, doc) if doc else None

    def get_workload_identity(self, uid: str) -> WorkloadIdentity | None:
        doc = self._get_doc("iam_workload_identities", uid)
        return _from_doc(WorkloadIdentity, doc) if doc else None

    def get_group(self, uid: str) -> Group | None:
        doc = self._get_doc("iam_groups", uid)
        return _from_doc(Group, doc) if doc else None

    def get_role(self, name: str) -> Role | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT doc FROM iam_roles WHERE name=?", (name,)
            ).fetchone()
        return _from_doc(Role, row["doc"]) if row else None

    def bindings_for_subjects(self, subject_uids: list[str]) -> list[AccessBinding]:
        if not subject_uids:
            return []
        qs = ",".join("?" for _ in subject_uids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT doc FROM iam_bindings WHERE subject_uid IN ({qs})",
                subject_uids,
            ).fetchall()
        return [_from_doc(AccessBinding, r["doc"]) for r in rows]

    def groups_containing(self, principal_uid: str) -> list[Group]:
        with self._lock:
            rows = self._conn.execute("SELECT doc FROM iam_groups").fetchall()
        groups = [_from_doc(Group, r["doc"]) for r in rows]
        return [g for g in groups if principal_uid in g.members]

    def restrictions_for(self, namespace: str | None) -> list[Restriction]:
        """Installation-wide restrictions plus those of ``namespace``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc FROM iam_restrictions WHERE namespace IS NULL"
                " OR namespace=?",
                (namespace,),
            ).fetchall()
        return [_from_doc(Restriction, r["doc"]) for r in rows]

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------

    def _get_doc(self, table: str, uid: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT doc FROM {table} WHERE uid=?", (uid,)  # noqa: S608
            ).fetchone()
        return row["doc"] if row else None

    def _resolve_subject(self, subject_kind: str, subject_uid: str) -> Any:
        if subject_kind == SUBJECT_PRINCIPAL:
            return self.get_principal(subject_uid)
        if subject_kind == SUBJECT_SERVICE_PRINCIPAL:
            return self.get_service_principal(subject_uid)
        if subject_kind == SUBJECT_WORKLOAD_IDENTITY:
            return self.get_workload_identity(subject_uid)
        if subject_kind == SUBJECT_GROUP:
            return self.get_group(subject_uid)
        return None

    @staticmethod
    def _check_binding_scope(binding: AccessBinding, subject: Any) -> None:
        """Namespace-scoped subjects may only hold bindings inside their
        namespace; WorkloadIdentities may never hold installation bindings."""
        subject_ns = getattr(subject, "namespace", None)
        if isinstance(subject, WorkloadIdentity):
            if binding.scope == SCOPE_INSTALLATION:
                raise ValidationError(
                    f"WorkloadIdentity {subject.name!r} cannot receive an "
                    "installation-scope binding; workload identities are "
                    f"confined to namespace {subject.namespace!r}"
                )
            if binding.namespace != subject.namespace:
                raise ValidationError(
                    f"WorkloadIdentity {subject.name!r} is confined to namespace "
                    f"{subject.namespace!r}; binding targets "
                    f"{binding.namespace!r}"
                )
            return
        if subject_ns is None:
            return  # installation-scoped subject: any binding scope is fine
        if binding.scope == SCOPE_INSTALLATION:
            raise ValidationError(
                f"{type(subject).__name__} {getattr(subject, 'name', subject_ns)!r} "
                f"is scoped to namespace {subject_ns!r} and cannot receive an "
                "installation-scope binding"
            )
        if binding.namespace != subject_ns:
            raise ValidationError(
                f"{type(subject).__name__} {getattr(subject, 'name', '')!r} is "
                f"scoped to namespace {subject_ns!r}; binding targets "
                f"{binding.namespace!r}"
            )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OCCIAMAdapter(IAMAdapter):
    """Native (OCC-owned) authorization authority.

    Deny-by-default: permission exists only where an AccessBinding grants the
    exact action at a containing scope, and Restrictions can only remove
    permission, never add it.
    """

    name = "occ-native"

    def __init__(self, store: IAMStore):
        self._store = store

    # -- identity ------------------------------------------------------

    def resolve_principal(self, issuer: str, subject: str) -> Principal:
        """Admission resolves identities; it can never create them."""
        principal = self._store.get_principal_by_identity(issuer, subject)
        if principal is None:
            raise AuthorizationError(
                f"unknown principal {issuer!r}/{subject!r}: identities are "
                "created by administration, never at admission"
            )
        return principal

    # -- authorization ---------------------------------------------------

    def authorize(self, request: AuthzRequest) -> None:
        subject_uids = self._subject_uids(request)
        bindings = self._store.bindings_for_subjects(subject_uids)
        allowed = any(self._binding_allows(b, request) for b in bindings)
        if not allowed:
            raise AuthorizationError(
                f"{request.principal_kind} {request.principal!r} has no binding "
                f"granting {request.action!r} on {self._target(request)}"
            )
        self._apply_restrictions(request)

    def _subject_uids(self, request: AuthzRequest) -> list[str]:
        """The requesting subject's uid plus the uids of groups it is in.

        Unknown identity -> AuthorizationError (fail closed).
        """
        kind, uid = request.principal_kind, request.principal
        if kind not in (SUBJECT_PRINCIPAL, SUBJECT_SERVICE_PRINCIPAL,
                        SUBJECT_WORKLOAD_IDENTITY):
            raise AuthorizationError(
                f"unsupported principal kind {kind!r}"
            )
        subject = self._store._resolve_subject(kind, uid)
        if subject is None:
            raise AuthorizationError(
                f"unknown {kind} {uid!r}: unverifiable identity is a denial"
            )
        uids = [uid]
        if kind == SUBJECT_PRINCIPAL:
            uids.extend(g.uid for g in self._store.groups_containing(uid))
        return uids

    def _binding_allows(self, binding: AccessBinding, request: AuthzRequest) -> bool:
        role = self._store.get_role(binding.role)
        if role is None or request.action not in role.permissions:
            return False
        return self._scope_contains(binding, request)

    @staticmethod
    def _scope_contains(binding: AccessBinding, request: AuthzRequest) -> bool:
        if binding.scope == SCOPE_INSTALLATION:
            return True
        if binding.scope == SCOPE_NAMESPACE:
            return (request.namespace is not None
                    and request.namespace == binding.namespace)
        # exact-resource binding covers only that exact resource
        return (
            request.resource is not None
            and binding.resource_kind == request.kind
            and binding.resource_name == request.resource
            and binding.namespace == request.namespace
        )

    def _apply_restrictions(self, request: AuthzRequest) -> None:
        for restriction in self._store.restrictions_for(request.namespace):
            for pattern in restriction.deny:
                if _pattern_matches(pattern, request):
                    raise RestrictionError(
                        f"Restriction {restriction.name!r} denies "
                        f"{request.action!r} on {self._target(request)} "
                        f"(pattern {pattern!r})"
                    )

    @staticmethod
    def _target(request: AuthzRequest) -> str:
        parts = [request.kind]
        if request.namespace:
            parts.append(request.namespace)
        if request.resource:
            parts.append(request.resource)
        return "/".join(parts)


def _pattern_matches(pattern: str, request: AuthzRequest) -> bool:
    """Match a deny pattern 'action:Kind[:name]' against a request.

    '*' matches any value in the action or kind position. A pattern with a
    name segment matches only that exact resource name.
    """
    parts = pattern.split(":")
    if len(parts) not in (2, 3):
        return False  # malformed patterns never match (they also never grant)
    action, kind = parts[0], parts[1]
    if action not in ("*", request.action):
        return False
    if kind not in ("*", request.kind):
        return False
    if len(parts) == 3:
        return request.resource is not None and parts[2] == request.resource
    return True
