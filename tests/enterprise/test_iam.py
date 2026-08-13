"""Tests for the native IAM: principals, roles, bindings, restrictions."""

from __future__ import annotations

import pytest

from enterprise.contracts import AuthzRequest
from enterprise.errors import (
    AuthorizationError,
    ConflictError,
    RestrictionError,
    ValidationError,
)
from enterprise.iam import (
    SCOPE_INSTALLATION,
    SCOPE_NAMESPACE,
    SCOPE_RESOURCE,
    SUBJECT_GROUP,
    SUBJECT_PRINCIPAL,
    SUBJECT_SERVICE_PRINCIPAL,
    SUBJECT_WORKLOAD_IDENTITY,
    AccessBinding,
    Group,
    IAMStore,
    OCCIAMAdapter,
    Principal,
    Restriction,
    Role,
    ServicePrincipal,
    WorkloadIdentity,
)

DEPLOY = "hermes.agents.deploy"
READ = "hermes.agents.read"
LIST = "hermes.agents.list"


@pytest.fixture()
def store(tmp_path):
    s = IAMStore(tmp_path / "iam.db")
    yield s
    s.close()


@pytest.fixture()
def adapter(store):
    return OCCIAMAdapter(store)


@pytest.fixture()
def alice(store):
    return store.create_principal(
        Principal(issuer="https://idp.example", subject="alice",
                  display_name="Alice")
    )


@pytest.fixture()
def deployer(store):
    return store.create_role(Role(name="deployer", permissions=(DEPLOY,)))


def req(principal, action=DEPLOY, kind="Agent", namespace="acme",
        resource="bot", principal_kind=SUBJECT_PRINCIPAL):
    return AuthzRequest(
        principal=principal.uid if hasattr(principal, "uid") else principal,
        principal_kind=principal_kind,
        action=action,
        kind=kind,
        namespace=namespace,
        resource=resource,
    )


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

class TestIdentity:
    def test_unknown_identity_denied(self, adapter):
        with pytest.raises(AuthorizationError):
            adapter.authorize(req("no-such-uid"))

    def test_resolve_principal_unknown_raises(self, adapter):
        with pytest.raises(AuthorizationError):
            adapter.resolve_principal("https://idp.example", "ghost")

    def test_resolve_principal_known(self, adapter, alice):
        got = adapter.resolve_principal("https://idp.example", "alice")
        assert got.uid == alice.uid

    def test_duplicate_identity_conflicts(self, store, alice):
        with pytest.raises(ConflictError):
            store.create_principal(
                Principal(issuer="https://idp.example", subject="alice")
            )

    def test_principal_with_no_bindings_denied(self, adapter, alice):
        with pytest.raises(AuthorizationError):
            adapter.authorize(req(alice))


# ---------------------------------------------------------------------------
# Bindings grant
# ---------------------------------------------------------------------------

class TestBindingGrants:
    def test_binding_grants_exact_action(self, store, adapter, alice, deployer):
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_PRINCIPAL,
            subject_uid=alice.uid, scope=SCOPE_NAMESPACE, namespace="acme",
        ))
        adapter.authorize(req(alice))  # no raise == allow

    def test_binding_does_not_grant_other_action(self, store, adapter, alice,
                                                 deployer):
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_PRINCIPAL,
            subject_uid=alice.uid, scope=SCOPE_NAMESPACE, namespace="acme",
        ))
        with pytest.raises(AuthorizationError):
            adapter.authorize(req(alice, action=READ))

    def test_installation_binding_covers_namespace_request(self, store, adapter,
                                                           alice, deployer):
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_PRINCIPAL,
            subject_uid=alice.uid, scope=SCOPE_INSTALLATION,
        ))
        adapter.authorize(req(alice, namespace="acme"))
        adapter.authorize(req(alice, namespace="globex", resource=None))

    def test_namespace_binding_does_not_cover_other_namespace(
            self, store, adapter, alice, deployer):
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_PRINCIPAL,
            subject_uid=alice.uid, scope=SCOPE_NAMESPACE, namespace="acme",
        ))
        with pytest.raises(AuthorizationError):
            adapter.authorize(req(alice, namespace="globex"))

    def test_exact_resource_binding_covers_only_that_resource(
            self, store, adapter, alice):
        store.create_role(Role(name="reader", permissions=(READ, LIST)))
        store.create_binding(AccessBinding(
            role="reader", subject_kind=SUBJECT_PRINCIPAL,
            subject_uid=alice.uid, scope=SCOPE_RESOURCE, namespace="acme",
            resource_kind="Agent", resource_name="bot",
        ))
        adapter.authorize(req(alice, action=READ, resource="bot"))
        # sibling resource in the same namespace: denied
        with pytest.raises(AuthorizationError):
            adapter.authorize(req(alice, action=READ, resource="other-bot"))
        # namespace-level list (no exact resource): denied
        with pytest.raises(AuthorizationError):
            adapter.authorize(req(alice, action=LIST, resource=None))


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

class TestGroups:
    def test_group_membership_grants(self, store, adapter, alice, deployer):
        group = store.create_group(Group(name="platform-team",
                                         members=(alice.uid,)))
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_GROUP,
            subject_uid=group.uid, scope=SCOPE_NAMESPACE, namespace="acme",
        ))
        adapter.authorize(req(alice))

    def test_non_member_not_granted(self, store, adapter, alice, deployer):
        bob = store.create_principal(
            Principal(issuer="https://idp.example", subject="bob"))
        group = store.create_group(Group(name="platform-team",
                                         members=(alice.uid,)))
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_GROUP,
            subject_uid=group.uid, scope=SCOPE_NAMESPACE, namespace="acme",
        ))
        with pytest.raises(AuthorizationError):
            adapter.authorize(req(bob))

    def test_group_member_must_be_known_principal(self, store):
        with pytest.raises(ValidationError):
            store.create_group(Group(name="ghosts", members=("nope",)))


# ---------------------------------------------------------------------------
# Restrictions
# ---------------------------------------------------------------------------

class TestRestrictions:
    def test_restriction_narrows_allowed_op(self, store, adapter, alice,
                                            deployer):
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_PRINCIPAL,
            subject_uid=alice.uid, scope=SCOPE_INSTALLATION,
        ))
        adapter.authorize(req(alice))  # allowed before the restriction
        store.create_restriction(Restriction(
            name="freeze", deny=(f"{DEPLOY}:Agent",), namespace="acme",
        ))
        with pytest.raises(RestrictionError):
            adapter.authorize(req(alice))
        # other namespace unaffected by the namespaced restriction
        adapter.authorize(req(alice, namespace="globex"))

    def test_installation_restriction_applies_everywhere(self, store, adapter,
                                                         alice, deployer):
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_PRINCIPAL,
            subject_uid=alice.uid, scope=SCOPE_INSTALLATION,
        ))
        store.create_restriction(Restriction(
            name="global-freeze", deny=(f"{DEPLOY}:Agent",),
        ))
        with pytest.raises(RestrictionError):
            adapter.authorize(req(alice, namespace="globex"))

    def test_exact_name_restriction(self, store, adapter, alice, deployer):
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_PRINCIPAL,
            subject_uid=alice.uid, scope=SCOPE_INSTALLATION,
        ))
        store.create_restriction(Restriction(
            name="protect-bot", deny=(f"{DEPLOY}:Agent:bot",), namespace="acme",
        ))
        with pytest.raises(RestrictionError):
            adapter.authorize(req(alice, resource="bot"))
        adapter.authorize(req(alice, resource="other-bot"))

    def test_restriction_cannot_grant(self, store, adapter, alice):
        # A restriction naming an action gives no permission by itself.
        store.create_restriction(Restriction(
            name="noise", deny=(f"{READ}:Agent",), namespace="acme",
        ))
        with pytest.raises(AuthorizationError):
            adapter.authorize(req(alice))  # still no binding -> denied

    def test_empty_deny_rejected(self, store):
        with pytest.raises(ValidationError):
            store.create_restriction(Restriction(name="empty", deny=()))


# ---------------------------------------------------------------------------
# Scope confinement at binding creation
# ---------------------------------------------------------------------------

class TestBindingScopeConfinement:
    def test_ns_service_principal_rejects_installation_binding(
            self, store, deployer):
        sp = store.create_service_principal(
            ServicePrincipal(name="ci-bot", namespace="acme"))
        with pytest.raises(ValidationError):
            store.create_binding(AccessBinding(
                role="deployer", subject_kind=SUBJECT_SERVICE_PRINCIPAL,
                subject_uid=sp.uid, scope=SCOPE_INSTALLATION,
            ))

    def test_ns_service_principal_rejects_other_namespace_binding(
            self, store, deployer):
        sp = store.create_service_principal(
            ServicePrincipal(name="ci-bot", namespace="acme"))
        with pytest.raises(ValidationError):
            store.create_binding(AccessBinding(
                role="deployer", subject_kind=SUBJECT_SERVICE_PRINCIPAL,
                subject_uid=sp.uid, scope=SCOPE_NAMESPACE, namespace="globex",
            ))

    def test_installation_service_principal_may_bind_anywhere(
            self, store, adapter, deployer):
        sp = store.create_service_principal(ServicePrincipal(name="admin-bot"))
        store.create_binding(AccessBinding(
            role="deployer", subject_kind=SUBJECT_SERVICE_PRINCIPAL,
            subject_uid=sp.uid, scope=SCOPE_INSTALLATION,
        ))
        adapter.authorize(req(sp, principal_kind=SUBJECT_SERVICE_PRINCIPAL))

    def test_ns_group_rejects_installation_binding(self, store, deployer):
        group = store.create_group(Group(name="ns-team", namespace="acme"))
        with pytest.raises(ValidationError):
            store.create_binding(AccessBinding(
                role="deployer", subject_kind=SUBJECT_GROUP,
                subject_uid=group.uid, scope=SCOPE_INSTALLATION,
            ))

    def test_workload_identity_binding_outside_namespace_rejected(
            self, store, deployer):
        wi = store.create_workload_identity(
            WorkloadIdentity(name="bot-wi", agent="bot", namespace="acme"))
        with pytest.raises(ValidationError):
            store.create_binding(AccessBinding(
                role="deployer", subject_kind=SUBJECT_WORKLOAD_IDENTITY,
                subject_uid=wi.uid, scope=SCOPE_NAMESPACE, namespace="globex",
            ))
        with pytest.raises(ValidationError):
            store.create_binding(AccessBinding(
                role="deployer", subject_kind=SUBJECT_WORKLOAD_IDENTITY,
                subject_uid=wi.uid, scope=SCOPE_INSTALLATION,
            ))
        with pytest.raises(ValidationError):
            store.create_binding(AccessBinding(
                role="deployer", subject_kind=SUBJECT_WORKLOAD_IDENTITY,
                subject_uid=wi.uid, scope=SCOPE_RESOURCE, namespace="globex",
                resource_kind="Agent", resource_name="bot",
            ))

    def test_workload_identity_binding_in_own_namespace_ok(
            self, store, adapter):
        store.create_role(Role(name="reader", permissions=(READ,)))
        wi = store.create_workload_identity(
            WorkloadIdentity(name="bot-wi", agent="bot", namespace="acme"))
        store.create_binding(AccessBinding(
            role="reader", subject_kind=SUBJECT_WORKLOAD_IDENTITY,
            subject_uid=wi.uid, scope=SCOPE_RESOURCE, namespace="acme",
            resource_kind="Agent", resource_name="bot",
        ))
        adapter.authorize(req(wi, action=READ,
                              principal_kind=SUBJECT_WORKLOAD_IDENTITY))
        with pytest.raises(AuthorizationError):
            adapter.authorize(req(wi, action=READ, resource="other-bot",
                                  principal_kind=SUBJECT_WORKLOAD_IDENTITY))


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_binding_requires_known_role(self, store, alice):
        with pytest.raises(ValidationError):
            store.create_binding(AccessBinding(
                role="no-such-role", subject_kind=SUBJECT_PRINCIPAL,
                subject_uid=alice.uid, scope=SCOPE_INSTALLATION,
            ))

    def test_binding_requires_known_subject(self, store, deployer):
        with pytest.raises(ValidationError):
            store.create_binding(AccessBinding(
                role="deployer", subject_kind=SUBJECT_PRINCIPAL,
                subject_uid="ghost", scope=SCOPE_INSTALLATION,
            ))

    def test_role_requires_permissions(self, store):
        with pytest.raises(ValidationError):
            store.create_role(Role(name="empty", permissions=()))

    def test_workload_identity_requires_namespace(self):
        with pytest.raises(ValidationError):
            WorkloadIdentity(name="wi", agent="bot", namespace="").validate()

    def test_namespace_scope_binding_requires_namespace(self, alice, deployer,
                                                        store):
        with pytest.raises(ValidationError):
            store.create_binding(AccessBinding(
                role="deployer", subject_kind=SUBJECT_PRINCIPAL,
                subject_uid=alice.uid, scope=SCOPE_NAMESPACE,
            ))
