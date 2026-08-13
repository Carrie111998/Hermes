"""Brokered secret access for Hermes Enterprise.

Workloads never read secret values. A workload asks the SecretBrokerService
to *perform an operation that needs a secret* (prove possession, mint a
scoped derived token); the service verifies the caller end-to-end,
dispatches to the installation-selected SecretDriver, and returns only the
operation result. Every verification step is fail-closed and every attempt
— allow and deny alike — produces an audit record that never carries a
secret value.

Verification order (each failure denies; nothing later runs):

  1. The named AgentRevision exists in the namespace, is phase ``Active``,
     and its ``spec.workloadIdentity`` matches the caller. Candidate and
     retired revisions cannot act.
  2. The revision's immutable snapshot references the secret in its
     ``secrets`` list. A snapshot without a ``secrets`` list grants nothing.
  3. The authoritative IAMAdapter allows ``hermes.secrets.use`` for this
     exact workload identity on this exact Secret.
  4. The Secret resolves to its SecretBroker in the same namespace.
  5. The broker's driver name resolves to a configured SecretDriver —
     unknown names deny, they never fall back.
  6. The driver executes the operation backend-side.
  7. The driver result is scrubbed: any secret-like content denies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .contracts import AuthzRequest, IAMAdapter, SecretDriver
from .errors import (
    AuthorizationError,
    NotFoundError,
    SecretAccessError,
)
from .resources import Kind, Resource, RevisionPhase, _find_secretlike_keys
from .store import ResourceStore

_ACTION = "hermes.secrets.use"

#: Result keys that are sanctioned *derived* outputs. A minted scoped token
#: is an HMAC of the secret — non-reversible and short-lived — so the key
#: name matches the secret-shape detector without carrying the secret.
#: Everything else that looks secret-like in a driver result is a leak.
_SANCTIONED_RESULT_KEYS = frozenset({"token"})

_SUPPORTED_OPERATIONS = ("http_bearer_probe", "mint_scoped_token")


def _derive(value: str, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """Shared value-free derivations used by both bundled drivers.

    ``http_bearer_probe``  -> proof of possession (truncated digest).
    ``mint_scoped_token``  -> short-lived token derived via HMAC-SHA256,
                              keyed by the secret, bound to audience+exp.
    """
    if operation == "http_bearer_probe":
        fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return {"ok": True, "fingerprint": fingerprint}
    if operation == "mint_scoped_token":
        audience = params.get("audience")
        exp = params.get("exp")
        if not isinstance(audience, str) or not audience or exp is None:
            raise SecretAccessError(
                "mint_scoped_token requires params 'audience' and 'exp'"
            )
        msg = (audience + str(exp)).encode("utf-8")
        token = hmac.new(value.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        return {"token": token, "exp": exp}
    raise SecretAccessError(
        f"operation {operation!r} is not a permitted secret operation "
        f"(supported: {', '.join(_SUPPORTED_OPERATIONS)})"
    )


# ---------------------------------------------------------------------------
# Broker service
# ---------------------------------------------------------------------------


class SecretBrokerService:
    """Mediates every secret-backed operation for workloads.

    There is no path from a workload to a secret value through this
    service: drivers execute operations backend-side and return derived,
    value-free results, which are additionally scrubbed here.
    """

    def __init__(
        self,
        store: ResourceStore,
        audit: AuditLog,
        iam_adapter: IAMAdapter,
        drivers: dict[str, SecretDriver],
    ) -> None:
        self._store = store
        self._audit = audit
        self._iam = iam_adapter
        self._drivers = dict(drivers)

    # -- public API --------------------------------------------------------

    def broker_operation(
        self,
        namespace: str,
        workload_identity: str,
        revision_name: str,
        secret_name: str,
        operation: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})

        def deny(reason: str) -> SecretAccessError:
            self._record(
                namespace, workload_identity, secret_name, "deny", reason
            )
            return SecretAccessError(reason)

        # (1) Active revision owned by this workload identity.
        try:
            revision = self._store.get(Kind.AGENT_REVISION, revision_name, namespace)
        except NotFoundError:
            raise deny(
                f"AgentRevision {revision_name!r} not found in namespace "
                f"{namespace!r}"
            ) from None
        phase = revision.status.get("phase", RevisionPhase.CANDIDATE.value)
        if phase != RevisionPhase.ACTIVE.value:
            raise deny(
                f"AgentRevision {revision_name!r} is {phase}; only Active "
                "revisions may use secrets"
            )
        if revision.spec.get("workloadIdentity") != workload_identity:
            raise deny(
                f"workload identity mismatch for AgentRevision {revision_name!r}"
            )

        # (2) The admitted snapshot must reference the secret.
        snapshot_secrets = revision.spec.get("secrets")
        if not isinstance(snapshot_secrets, list) or secret_name not in snapshot_secrets:
            raise deny(
                f"AgentRevision {revision_name!r} snapshot does not reference "
                f"Secret {secret_name!r}"
            )

        # (3) Authoritative authorization. A deny (or unavailable authority)
        # raises AuthorizationError; we audit and let it propagate.
        try:
            self._iam.authorize(
                AuthzRequest(
                    principal=workload_identity,
                    principal_kind="workload-identity",
                    action=_ACTION,
                    kind=Kind.SECRET.value,
                    namespace=namespace,
                    resource=secret_name,
                )
            )
        except AuthorizationError as exc:
            self._record(
                namespace, workload_identity, secret_name, "deny",
                f"iam denied: {exc}",
            )
            raise

        # (4) Resolve Secret -> SecretBroker (same namespace only).
        try:
            secret = self._store.get(Kind.SECRET, secret_name, namespace)
        except NotFoundError:
            raise deny(
                f"Secret {secret_name!r} not found in namespace {namespace!r}"
            ) from None
        broker_name = secret.spec.get("broker")
        if not isinstance(broker_name, str) or not broker_name:
            raise deny(f"Secret {secret_name!r} carries no broker reference")
        try:
            broker = self._store.get(Kind.SECRET_BROKER, broker_name, namespace)
        except NotFoundError:
            raise deny(
                f"SecretBroker {broker_name!r} not found in namespace "
                f"{namespace!r}"
            ) from None

        # (5) Exact driver selection — unknown names deny, never fall back.
        driver_name = broker.spec.get("driver")
        driver = self._drivers.get(driver_name)
        if driver is None:
            raise deny(
                f"SecretBroker {broker_name!r} selects unknown driver "
                f"{driver_name!r}; no fallback is permitted"
            )

        # (6) Execute backend-side.
        backend = dict(broker.spec.get("backend") or {})
        key = secret.spec.get("key")
        if not isinstance(key, str) or not key:
            raise deny(f"Secret {secret_name!r} carries no backend key")
        try:
            result = driver.use(backend, key, operation, params)
        except SecretAccessError as exc:
            self._record(
                namespace, workload_identity, secret_name, "deny",
                f"driver refused: {exc}",
            )
            raise

        # (7) Scrub: derived results only; anything secret-shaped denies.
        self._scrub(result, namespace, workload_identity, secret_name)

        # (8) Audit the allow.
        self._record(
            namespace, workload_identity, secret_name, "allow",
            f"operation {operation!r} brokered via {driver_name!r}",
            detail={"operation": operation, "revision": revision_name,
                    "broker": broker_name, "driver": driver_name},
        )
        return result

    # -- internals ----------------------------------------------------------

    def _scrub(
        self,
        result: Any,
        namespace: str,
        workload_identity: str,
        secret_name: str,
    ) -> None:
        if not isinstance(result, dict):
            self._record(
                namespace, workload_identity, secret_name, "deny",
                "driver returned a non-dict result",
            )
            raise SecretAccessError("driver result must be a dict")
        checkable = {
            k: v for k, v in result.items() if k not in _SANCTIONED_RESULT_KEYS
        }
        leaked = _find_secretlike_keys(checkable)
        if leaked:
            self._record(
                namespace, workload_identity, secret_name, "deny",
                f"driver result contained secret-like keys: {sorted(leaked)}",
            )
            raise SecretAccessError(
                "driver result contained secret-like content and was withheld"
            )

    def _record(
        self,
        namespace: str,
        workload_identity: str,
        secret_name: str,
        outcome: str,
        reason: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._audit.record(
            actor=workload_identity,
            actor_kind="workload-identity",
            action=_ACTION,
            kind=Kind.SECRET.value,
            namespace=namespace,
            resource=secret_name,
            outcome=outcome,
            reason=reason,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


class EnvFileSecretDriver(SecretDriver):
    """Dev/test backend: KEY=VALUE lines in a plain file.

    ``backend`` config: ``{"path": "<file path>"}``. Values never leave the
    driver — only derivations do.
    """

    name = "envfile"

    def exists(self, backend: dict[str, Any], key: str) -> bool:
        try:
            values = self._load(backend)
        except SecretAccessError:
            return False
        return key in values

    def use(
        self,
        backend: dict[str, Any],
        key: str,
        operation: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        values = self._load(backend)
        if key not in values:
            raise SecretAccessError(f"backend cannot serve key {key!r}")
        return _derive(values[key], operation, params)

    @staticmethod
    def _load(backend: dict[str, Any]) -> dict[str, str]:
        path = backend.get("path")
        if not isinstance(path, str) or not path:
            raise SecretAccessError("envfile backend requires a 'path'")
        file = Path(path)
        if not file.is_file():
            raise SecretAccessError(f"envfile backend path {path!r} unreadable")
        values: dict[str, str] = {}
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
        return values


class VaultHttpSecretDriver(SecretDriver):
    """HashiCorp Vault KV v2 over stdlib urllib.

    ``backend`` config: ``{"addr": ..., "mount": ..., "tokenEnv": ...}``.
    The Vault token is read from the named environment variable at call
    time — backend credentials are never stored in platform resources.
    """

    name = "vault-kv2"

    _TIMEOUT = 10

    def exists(self, backend: dict[str, Any], key: str) -> bool:
        try:
            self._request(backend, f"metadata/{key}")
        except SecretAccessError as exc:
            if getattr(exc, "status", None) == 404:
                return False
            raise
        return True

    def use(
        self,
        backend: dict[str, Any],
        key: str,
        operation: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        doc = self._request(backend, f"data/{key}")
        data = ((doc or {}).get("data") or {}).get("data")
        if not isinstance(data, dict) or not data:
            raise SecretAccessError(f"vault returned no data for key {key!r}")
        if "value" in data:
            value = data["value"]
        elif len(data) == 1:
            value = next(iter(data.values()))
        else:
            raise SecretAccessError(
                f"vault key {key!r} is ambiguous: expected a 'value' field "
                "or a single-field secret"
            )
        if not isinstance(value, str) or not value:
            raise SecretAccessError(f"vault key {key!r} has no usable value")
        return _derive(value, operation, params)

    # -- internals ----------------------------------------------------------

    def _request(self, backend: dict[str, Any], suffix: str) -> dict[str, Any]:
        addr = backend.get("addr")
        mount = backend.get("mount")
        token_env = backend.get("tokenEnv")
        if (
            not isinstance(addr, str) or not addr
            or not isinstance(mount, str) or not mount
            or not isinstance(token_env, str) or not token_env
        ):
            raise SecretAccessError(
                "vault-kv2 backend requires 'addr', 'mount' and 'tokenEnv'"
            )
        token = os.environ.get(token_env, "")
        if not token:
            raise SecretAccessError(
                f"vault token env var {token_env!r} is not set"
            )
        url = f"{addr.rstrip('/')}/v1/{mount.strip('/')}/{suffix}"
        req = urllib.request.Request(
            url, headers={"X-Vault-Token": token}, method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            err = SecretAccessError(f"vault HTTP {exc.code} for {url}")
            err.status = exc.code  # type: ignore[attr-defined]
            raise err from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SecretAccessError(f"vault unreachable: {exc}") from exc
        try:
            doc = json.loads(body)
        except ValueError as exc:
            raise SecretAccessError("vault returned invalid JSON") from exc
        if not isinstance(doc, dict):
            raise SecretAccessError("vault returned an unexpected document")
        return doc
