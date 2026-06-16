from gateway.principal_headers import parse_principal_scope_headers


def test_absent_principal_headers_return_empty_scope():
    scope, error = parse_principal_scope_headers({}, api_key_configured=False)

    assert scope == {}
    assert error is None


def test_principal_headers_require_api_key_authentication():
    scope, error = parse_principal_scope_headers(
        {"X-Hermes-Tenant-Id": "tenant-1"},
        api_key_configured=False,
    )

    assert scope == {}
    assert error == "Principal scope headers require API key authentication"


def test_parse_principal_scope_headers():
    scope, error = parse_principal_scope_headers(
        {
            "X-Hermes-Tenant-Id": "tenant-1",
            "X-Hermes-Workspace-Id": "workspace-1",
            "X-Hermes-Project-Id": "project-1",
            "X-Hermes-User-Id": "user-1",
            "X-Hermes-Roles": "admin, member",
            "X-Hermes-Sandbox-Id": "sandbox-1",
        },
        api_key_configured=True,
    )

    assert error is None
    assert scope == {
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "user_id": "user-1",
        "roles": ("admin", "member"),
        "sandbox_id": "sandbox-1",
    }


def test_principal_scope_headers_require_complete_identity():
    scope, error = parse_principal_scope_headers(
        {
            "X-Hermes-Tenant-Id": "tenant-1",
            "X-Hermes-Workspace-Id": "workspace-1",
            "X-Hermes-User-Id": "user-1",
        },
        api_key_configured=True,
    )

    assert scope == {}
    assert error == "Principal scope headers missing required fields: project_id"


def test_principal_headers_reject_control_characters():
    scope, error = parse_principal_scope_headers(
        {"X-Hermes-User-Id": "user\nbad"},
        api_key_configured=True,
    )

    assert scope == {}
    assert error == "X-Hermes-User-Id contains invalid control characters"


def test_principal_headers_reject_oversized_values():
    scope, error = parse_principal_scope_headers(
        {"X-Hermes-Workspace-Id": "x" * 5},
        api_key_configured=True,
        max_len=4,
    )

    assert scope == {}
    assert error == "X-Hermes-Workspace-Id is too long"
