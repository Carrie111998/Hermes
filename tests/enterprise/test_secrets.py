"""Tests for enterprise.secrets: brokered, value-free secret access."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import urllib.error
import urllib.request

import pytest

from enterprise.audit import AuditLog
from enterprise.contracts import IAMAdapter, SecretDriver
from enterprise.errors import AuthorizationError, SecretAccessError
from enterprise.resources import Kind, NamespacePhase, Resource, ResourceMeta
from enterprise.secrets import (
    EnvFileSecretDriver,
    SecretBrokerService,
    VaultHttpSecretDriver,
)
from enterprise.store import ResourceStore

NS = "acme"
WI = "wi-bot-1"
SECRET_VALUE = "sk-super-secret-raw-value"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class AllowAllIAM(IAMAdapter):
    name = "allow-all"

    def __init__(self):
        self.requests = []

    def authorize(self, request):
        self.requests.append(request)


class DenyAllIAM(IAMAdapter):
    name = "deny-all"

    def authorize(self, request):
        raise AuthorizationError("computer says no")


class LeakyDriver(SecretDriver):
    """Rigged driver whose result embeds a secret-like key."""

    name = "leaky"

    def exists(self, backend, key):
        return True

    def use(self, backend, key, operation, params):
        return {"ok": True, "api_key": SECRET_VALUE}


def mk(kind: Kind, name: str, namespace=None, spec=None, status=None) -> Resource:
    return Resource(
        meta=ResourceMeta(kind=kind.value, name=name, namespace=namespace),
        spec=spec or {},
        status=status or {},
    )


@pytest.fixture()
def store(tmp_path):
    s = ResourceStore(tmp_path / "occ.db")
    s.create(mk(Kind.NAMESPACE, NS))
    s.update_status(Kind.NAMESPACE, NS, None,
                    {"phase": NamespacePhase.READY.value})
    yield s
    s.close()


@pytest.fixture()
def audit(tmp_path):
    a = AuditLog(tmp_path / "audit.db")
    yield a
    a.close()


@pytest.fixture()
def envfile(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text(
        f"# dev secrets\nLLM_KEY={SECRET_VALUE}\nOTHER=whatever\n",
        encoding="utf-8",
    )
    return str(path)


def seed(store, envfile, *, revision_phase="Active", workload_identity=WI,
         secrets_snapshot=("llm-key",), driver_name="envfile"):
    """Create broker + secret + revision resources for one scenario."""
    store.create(mk(Kind.SECRET_BROKER, "dev-broker", NS,
                    spec={"driver": driver_name,
                          "backend": {"path": envfile}}))
    store.create(mk(Kind.SECRET, "llm-key", NS,
                    spec={"broker": "dev-broker", "key": "LLM_KEY"}))
    spec = {
        "agent": "bot",
        "agentUid": "uid-1",
        "workloadIdentity": workload_identity,
        "harness": {"name": "hermes", "version": "1.0"},
        "configuration": {"model": "hermes-4"},
        "computeDriver": "local",
        "sandboxDriver": "none",
    }
    if secrets_snapshot is not None:
        spec["secrets"] = list(secrets_snapshot)
    store.create(mk(Kind.AGENT_REVISION, "bot-rev-1", NS, spec=spec,
                    status={"phase": revision_phase}))


def make_service(store, audit, iam=None, drivers=None):
    return SecretBrokerService(
        store, audit, iam or AllowAllIAM(),
        drivers if drivers is not None else {"envfile": EnvFileSecretDriver()},
    )


def broker_call(svc, operation="mint_scoped_token", params=None,
                workload_identity=WI, secret_name="llm-key"):
    return svc.broker_operation(
        NS, workload_identity, "bot-rev-1", secret_name, operation,
        params if params is not None else {"audience": "api.example.com",
                                           "exp": 1234567890},
    )


def deny_rows(audit):
    return [r for r in audit.query(namespace=NS) if r["outcome"] == "deny"]


# ---------------------------------------------------------------------------
# Broker service
# ---------------------------------------------------------------------------


class TestBrokerHappyPath:
    def test_mint_scoped_token(self, store, audit, envfile):
        seed(store, envfile)
        svc = make_service(store, audit)
        result = broker_call(svc)
        expected = hmac.new(
            SECRET_VALUE.encode(), b"api.example.com1234567890",
            hashlib.sha256,
        ).hexdigest()
        assert result == {"token": expected, "exp": 1234567890}
        assert SECRET_VALUE not in json.dumps(result)

    def test_probe_fingerprint_stable_and_value_free(self, store, audit, envfile):
        seed(store, envfile)
        svc = make_service(store, audit)
        r1 = broker_call(svc, operation="http_bearer_probe", params={})
        r2 = broker_call(svc, operation="http_bearer_probe", params={})
        assert r1 == r2
        assert r1["ok"] is True
        assert r1["fingerprint"] == hashlib.sha256(
            SECRET_VALUE.encode()).hexdigest()[:12]
        assert SECRET_VALUE not in json.dumps(r1)

    def test_allow_audited(self, store, audit, envfile):
        seed(store, envfile)
        broker_call(make_service(store, audit))
        rows = audit.query(namespace=NS)
        assert len(rows) == 1
        row = rows[0]
        assert (row["outcome"], row["actor"], row["resource"]) == \
            ("allow", WI, "llm-key")
        assert SECRET_VALUE not in json.dumps(row, default=str)


class TestBrokerDenials:
    @pytest.mark.parametrize("phase", ["Candidate", "Retired"])
    def test_non_active_revision_denied(self, store, audit, envfile, phase):
        seed(store, envfile, revision_phase=phase)
        with pytest.raises(SecretAccessError, match=phase):
            broker_call(make_service(store, audit))
        assert len(deny_rows(audit)) == 1

    def test_workload_identity_mismatch_denied(self, store, audit, envfile):
        seed(store, envfile)
        with pytest.raises(SecretAccessError, match="identity mismatch"):
            broker_call(make_service(store, audit),
                        workload_identity="wi-imposter")
        assert len(deny_rows(audit)) == 1

    def test_secret_not_in_snapshot_denied(self, store, audit, envfile):
        seed(store, envfile, secrets_snapshot=("some-other-secret",))
        # The other secret must exist for the snapshot list to be plausible;
        # denial is about *this* secret not being referenced.
        store.create(mk(Kind.SECRET, "some-other-secret", NS,
                        spec={"broker": "dev-broker", "key": "OTHER"}))
        with pytest.raises(SecretAccessError, match="does not reference"):
            broker_call(make_service(store, audit))
        assert len(deny_rows(audit)) == 1

    def test_missing_snapshot_secrets_list_denied(self, store, audit, envfile):
        seed(store, envfile, secrets_snapshot=None)
        with pytest.raises(SecretAccessError, match="does not reference"):
            broker_call(make_service(store, audit))
        assert len(deny_rows(audit)) == 1

    def test_iam_deny_propagates_and_audits(self, store, audit, envfile):
        seed(store, envfile)
        svc = make_service(store, audit, iam=DenyAllIAM())
        with pytest.raises(AuthorizationError):
            broker_call(svc)
        rows = deny_rows(audit)
        assert len(rows) == 1 and "iam denied" in rows[0]["reason"]

    def test_iam_receives_exact_request(self, store, audit, envfile):
        seed(store, envfile)
        iam = AllowAllIAM()
        broker_call(make_service(store, audit, iam=iam))
        (req,) = iam.requests
        assert (req.principal, req.principal_kind) == (WI, "workload-identity")
        assert (req.action, req.kind) == ("hermes.secrets.use", "Secret")
        assert (req.namespace, req.resource) == (NS, "llm-key")

    def test_unknown_driver_denied_no_fallback(self, store, audit, envfile):
        seed(store, envfile, driver_name="mystery")
        # Configured drivers include a working envfile driver — it must
        # never be used as a fallback for an unknown selection.
        with pytest.raises(SecretAccessError, match="unknown driver"):
            broker_call(make_service(store, audit))
        assert len(deny_rows(audit)) == 1

    def test_leaky_driver_result_scrubbed(self, store, audit, envfile):
        seed(store, envfile, driver_name="leaky")
        svc = make_service(store, audit, drivers={"leaky": LeakyDriver()})
        with pytest.raises(SecretAccessError, match="withheld"):
            broker_call(svc)
        rows = deny_rows(audit)
        assert len(rows) == 1
        assert SECRET_VALUE not in json.dumps(rows[0], default=str)

    def test_missing_revision_denied(self, store, audit, envfile):
        seed(store, envfile)
        svc = make_service(store, audit)
        with pytest.raises(SecretAccessError, match="not found"):
            svc.broker_operation(NS, WI, "no-such-rev", "llm-key",
                                 "http_bearer_probe", {})
        assert len(deny_rows(audit)) == 1


# ---------------------------------------------------------------------------
# EnvFileSecretDriver
# ---------------------------------------------------------------------------


class TestEnvFileDriver:
    def test_exists(self, envfile):
        d = EnvFileSecretDriver()
        assert d.exists({"path": envfile}, "LLM_KEY") is True
        assert d.exists({"path": envfile}, "NOPE") is False
        assert d.exists({"path": "/no/such/file"}, "LLM_KEY") is False

    def test_unsupported_operation_rejected(self, envfile):
        with pytest.raises(SecretAccessError, match="not a permitted"):
            EnvFileSecretDriver().use({"path": envfile}, "LLM_KEY",
                                      "read_raw_value", {})

    def test_mint_requires_audience_and_exp(self, envfile):
        with pytest.raises(SecretAccessError, match="audience"):
            EnvFileSecretDriver().use({"path": envfile}, "LLM_KEY",
                                      "mint_scoped_token", {})

    def test_missing_key_rejected(self, envfile):
        with pytest.raises(SecretAccessError, match="cannot serve"):
            EnvFileSecretDriver().use({"path": envfile}, "NOPE",
                                      "http_bearer_probe", {})


# ---------------------------------------------------------------------------
# VaultHttpSecretDriver (no live network — urllib is monkeypatched)
# ---------------------------------------------------------------------------


VAULT_BACKEND = {"addr": "https://vault.example.com", "mount": "kv",
                 "tokenEnv": "TEST_VAULT_TOKEN"}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TestVaultDriver:
    def test_use_hits_correct_url_and_headers(self, monkeypatch):
        monkeypatch.setenv("TEST_VAULT_TOKEN", "vault-tok-123")
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = dict(req.header_items())
            seen["timeout"] = timeout
            body = {"data": {"data": {"value": SECRET_VALUE}}}
            return FakeResponse(json.dumps(body).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = VaultHttpSecretDriver().use(
            VAULT_BACKEND, "prod/llm", "http_bearer_probe", {})
        assert seen["url"] == "https://vault.example.com/v1/kv/data/prod/llm"
        assert seen["headers"].get("X-vault-token") == "vault-tok-123"
        assert seen["timeout"] == 10
        assert result["fingerprint"] == hashlib.sha256(
            SECRET_VALUE.encode()).hexdigest()[:12]
        assert SECRET_VALUE not in json.dumps(result)

    def test_exists_uses_metadata_endpoint(self, monkeypatch):
        monkeypatch.setenv("TEST_VAULT_TOKEN", "vault-tok-123")
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            return FakeResponse(json.dumps({"data": {}}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert VaultHttpSecretDriver().exists(VAULT_BACKEND, "prod/llm") is True
        assert seen["url"] == \
            "https://vault.example.com/v1/kv/metadata/prod/llm"

    def test_exists_false_on_404(self, monkeypatch):
        monkeypatch.setenv("TEST_VAULT_TOKEN", "vault-tok-123")

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "not found",
                                         None, None)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert VaultHttpSecretDriver().exists(VAULT_BACKEND, "gone") is False

    def test_http_error_maps_to_secret_access_error(self, monkeypatch):
        monkeypatch.setenv("TEST_VAULT_TOKEN", "vault-tok-123")

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "forbidden",
                                         None, None)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(SecretAccessError, match="HTTP 403"):
            VaultHttpSecretDriver().use(VAULT_BACKEND, "prod/llm",
                                        "http_bearer_probe", {})

    def test_network_error_maps_to_secret_access_error(self, monkeypatch):
        monkeypatch.setenv("TEST_VAULT_TOKEN", "vault-tok-123")

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(SecretAccessError, match="unreachable"):
            VaultHttpSecretDriver().use(VAULT_BACKEND, "prod/llm",
                                        "http_bearer_probe", {})

    def test_missing_token_env_denies(self, monkeypatch):
        monkeypatch.delenv("TEST_VAULT_TOKEN", raising=False)
        with pytest.raises(SecretAccessError, match="TEST_VAULT_TOKEN"):
            VaultHttpSecretDriver().use(VAULT_BACKEND, "prod/llm",
                                        "http_bearer_probe", {})
