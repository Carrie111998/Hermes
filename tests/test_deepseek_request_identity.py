from agent.provider_request_identity import (
    RequestSecretSnapshot,
    apply_deepseek_request_identity,
    load_deepseek_identity_secret,
)


URL = "https://ai.homelab.samaschke.de/v1"
KW = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]}


def apply(kwargs=KW, **overrides):
    args = dict(
        provider="homelab",
        model="deepseek-v4-flash",
        base_url=URL,
        identity_secret="stable-gateway-secret",
    )
    args.update(overrides)
    return apply_deepseek_request_identity(
        dict(kwargs), api_request_id="turn:api:1", **args
    )


def test_secret_loader_is_lazy_for_nonmatching_requests():
    calls = []

    result = apply_deepseek_request_identity(
        dict(KW),
        api_request_id="turn:api:1",
        provider="other",
        model="deepseek-v4-flash",
        base_url=URL,
        identity_secret_loader=lambda: calls.append(True) or "x" * 32,
    )
    assert result == KW
    assert calls == []


def test_gate_requires_provider_https_exact_host_and_model():
    for args in (
        {"provider": "other"},
        {"base_url": "http://ai.homelab.samaschke.de"},
        {"base_url": "https://ai.homelab.samaschke.de.evil"},
        {"base_url": "https://evil.ai.homelab.samaschke.de"},
    ):
        original = dict(KW)
        assert apply(original, **args) == original
        assert "extra_headers" not in original

    unsupported = {**KW, "model": "deepseek-v3"}
    assert apply(unsupported) == unsupported
    assert "extra_headers" not in unsupported


def test_destination_fingerprint_preserves_path_and_query_semantics():
    base = apply(KW, base_url=URL)
    trailing_slash = apply(KW, base_url=URL + "/")
    changed_path = apply(KW, base_url="https://ai.homelab.samaschke.de/experimental")
    first_query_order = apply(KW, base_url=URL + "?route=a&route=b")
    second_query_order = apply(KW, base_url=URL + "?route=b&route=a")
    keys = {
        base["extra_headers"]["Idempotency-Key"],
        trailing_slash["extra_headers"]["Idempotency-Key"],
        changed_path["extra_headers"]["Idempotency-Key"],
        first_query_order["extra_headers"]["Idempotency-Key"],
        second_query_order["extra_headers"]["Idempotency-Key"],
    }
    assert len(keys) == 5


def test_route_or_client_overrides_disable_provider_policy():
    for override in (
        {"base_url": "https://other.example/v1"},
        {"api_base": "https://other.example/v1"},
        {"provider": "other"},
        {"client": object()},
        {"http_client": object()},
    ):
        result = apply({**KW, **override})
        assert "extra_headers" not in result


def test_post_middleware_payload_model_controls_the_gate():
    rewritten_away = apply(
        {**KW, "model": "homelab/gpt-5.6-luna"},
        model="deepseek-v4-flash",
    )
    assert "extra_headers" not in rewritten_away

    rewritten_to_deepseek = apply(
        {**KW, "model": "homelab/deepseek-v4-flash"},
        model="homelab/gpt-5.6-luna",
    )
    assert "X-Request-ID" in rewritten_to_deepseek["extra_headers"]
    assert "Idempotency-Key" in rewritten_to_deepseek["extra_headers"]


def test_provider_and_payload_model_aliases_are_supported():
    for model in (
        "homelab/deepseek-v4-flash",
        "homelab/deepseek-v4-flash-0731",
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
    ):
        result = apply(
            {**KW, "model": model},
            model="homelab/gpt-5.6-luna",
            provider="HOMELAB",
        )
        assert "X-Request-ID" in result["extra_headers"]
        assert "Idempotency-Key" in result["extra_headers"]


def test_existing_headers_are_preserved_case_insensitively():
    headers = {"x-request-id": "caller-id", "IDEMPOTENCY-KEY": "caller-key", "X-Other": "v"}
    result = apply({**KW, "extra_headers": headers})
    assert result["extra_headers"] == headers


def test_key_is_stable_across_independent_calls_with_same_service_secret():
    first = apply({**KW, "functions": [{"name": "one"}]})
    second = apply({**KW, "functions": [{"name": "one"}]})
    changed_legacy_body = apply({**KW, "functions": [{"name": "two"}]})
    assert first["extra_headers"]["Idempotency-Key"] == second["extra_headers"]["Idempotency-Key"]
    assert first["extra_headers"]["Idempotency-Key"] != changed_legacy_body["extra_headers"]["Idempotency-Key"]


def test_dedicated_secret_loader_reads_protected_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "idempotency.secret"
    secret_file.write_text("dedicated-secret-with-at-least-32-chars\n")
    secret_file.chmod(0o600)
    monkeypatch.setenv(
        "HERMES_DEEPSEEK_IDEMPOTENCY_SECRET_FILE", str(secret_file)
    )
    assert load_deepseek_identity_secret() == "dedicated-secret-with-at-least-32-chars"


def test_dedicated_secret_loader_rejects_insecure_special_and_oversized_files(
    monkeypatch, tmp_path
):
    secret_file = tmp_path / "insecure.secret"
    monkeypatch.setenv(
        "HERMES_DEEPSEEK_IDEMPOTENCY_SECRET_FILE", str(secret_file)
    )
    secret_file.write_text("x" * 32)
    secret_file.chmod(0o644)
    assert load_deepseek_identity_secret() is None

    target = tmp_path / "target.secret"
    target.write_text("y" * 32)
    target.chmod(0o600)
    secret_file.unlink()
    secret_file.symlink_to(target)
    assert load_deepseek_identity_secret() is None

    secret_file.unlink()
    secret_file.write_text("z" * 4097)
    secret_file.chmod(0o600)
    assert load_deepseek_identity_secret() is None


def test_dedicated_secret_loader_recovers_after_transient_failure(monkeypatch, tmp_path):
    secret_file = tmp_path / "late.secret"
    monkeypatch.setenv(
        "HERMES_DEEPSEEK_IDEMPOTENCY_SECRET_FILE", str(secret_file)
    )
    assert load_deepseek_identity_secret() is None
    secret_file.write_text("late-secret-with-at-least-32-characters")
    secret_file.chmod(0o600)
    assert load_deepseek_identity_secret() == "late-secret-with-at-least-32-characters"


def test_logical_request_snapshot_pins_secret_across_retries():
    secrets = iter(("a" * 32, "b" * 32))
    snapshot = RequestSecretSnapshot(lambda: next(secrets))
    assert snapshot() == "a" * 32
    assert snapshot() == "a" * 32


def test_dedicated_secret_loader_observes_atomic_rotation(monkeypatch, tmp_path):
    secret_file = tmp_path / "rotated.secret"
    monkeypatch.setenv(
        "HERMES_DEEPSEEK_IDEMPOTENCY_SECRET_FILE", str(secret_file)
    )
    secret_file.write_text("first-secret-with-at-least-32-characters")
    secret_file.chmod(0o600)
    assert load_deepseek_identity_secret().startswith("first-secret")
    replacement = tmp_path / "replacement.secret"
    replacement.write_text("second-secret-with-at-least-32-characters")
    replacement.chmod(0o600)
    replacement.replace(secret_file)
    assert load_deepseek_identity_secret().startswith("second-secret")


def test_missing_stable_secret_keeps_request_id_but_omits_idempotency():
    result = apply(KW, identity_secret=None)
    assert result["extra_headers"]["X-Request-ID"] == "turn:api:1"
    assert "Idempotency-Key" not in result["extra_headers"]


def test_malformed_stable_secret_fails_open():
    result = apply(KW, identity_secret="bad\ud800secret")
    assert result["extra_headers"]["X-Request-ID"] == "turn:api:1"
    assert "Idempotency-Key" not in result["extra_headers"]


def test_key_is_stable_for_ordering_and_changes_with_payload():
    first = apply({**KW, "extra_body": {"b": 2, "a": 1}})
    second = apply({**KW, "extra_body": {"a": 1, "b": 2}})
    changed = apply({**KW, "extra_body": {"a": 1, "b": 3}})
    assert first["extra_headers"]["Idempotency-Key"] == second["extra_headers"]["Idempotency-Key"]
    assert first["extra_headers"]["Idempotency-Key"] != changed["extra_headers"]["Idempotency-Key"]


def test_transport_settings_and_credentials_do_not_change_semantic_key():
    first = apply({
        **KW,
        "timeout": 30,
        "api_key": "low-entropy-secret-a",
        "extra_headers": {"Authorization": "Bearer secret-a"},
    })
    second = apply({
        **KW,
        "timeout": 900,
        "api_key": "low-entropy-secret-b",
        "extra_headers": {"Authorization": "Bearer secret-b"},
    })
    assert first["extra_headers"]["Idempotency-Key"] == second["extra_headers"]["Idempotency-Key"]
    assert first["extra_headers"]["Authorization"] == "Bearer secret-a"
    assert second["extra_headers"]["Authorization"] == "Bearer secret-b"


def test_semantic_route_headers_and_query_change_the_key():
    base = apply({
        **KW,
        "extra_headers": {"Authorization": "Bearer secret", "X-Route": "one"},
        "extra_query": {"provider": "a"},
    })
    changed_route = apply({
        **KW,
        "extra_headers": {"Authorization": "Bearer other", "X-Route": "two"},
        "extra_query": {"provider": "a"},
    })
    changed_query = apply({
        **KW,
        "extra_headers": {"Authorization": "Bearer secret", "X-Route": "one"},
        "extra_query": {"provider": "b"},
    })
    keys = {
        base["extra_headers"]["Idempotency-Key"],
        changed_route["extra_headers"]["Idempotency-Key"],
        changed_query["extra_headers"]["Idempotency-Key"],
    }
    assert len(keys) == 3


def test_runtime_controls_do_not_change_semantic_key():
    def first_callback():
        return None

    def second_callback():
        return None

    first = apply({**KW, "on_progress": first_callback, "max_retries": 1})
    second = apply({**KW, "on_progress": second_callback, "max_retries": 9})
    assert first["extra_headers"]["Idempotency-Key"] == second["extra_headers"]["Idempotency-Key"]


def test_semantic_header_names_are_case_insensitive():
    first = apply({**KW, "extra_headers": {"X-Route": "one"}})
    second = apply({**KW, "extra_headers": {"x-route": "one"}})
    assert first["extra_headers"]["Idempotency-Key"] == second["extra_headers"]["Idempotency-Key"]


def test_opaque_semantic_value_fails_open_instead_of_collapsing_by_type():
    class Opaque:
        __slots__ = ("value",)

        def __init__(self, value):
            self.value = value

    result = apply({**KW, "metadata": Opaque("distinct")})
    assert result["extra_headers"]["X-Request-ID"] == "turn:api:1"
    assert "Idempotency-Key" not in result["extra_headers"]


def test_cyclic_semantic_value_keeps_request_id_and_omits_idempotency_key():
    cyclic = []
    cyclic.append(cyclic)
    result = apply({**KW, "metadata": cyclic})
    assert result["extra_headers"]["X-Request-ID"] == "turn:api:1"
    assert "Idempotency-Key" not in result["extra_headers"]


def test_invalid_header_mapping_is_not_silently_replaced():
    class InvalidHeaders:
        def keys(self):
            raise TypeError("not a mapping")

    kwargs = {**KW, "extra_headers": InvalidHeaders()}
    result = apply(kwargs)
    assert result["extra_headers"] is kwargs["extra_headers"]


def test_invalid_request_id_uses_safe_deterministic_fallback():
    result = apply_deepseek_request_identity(
        dict(KW), api_request_id="bad\nrequest", provider="homelab",
        model="deepseek-v4-flash", base_url=URL,
        identity_secret="stable-gateway-secret",
    )
    request_id = result["extra_headers"]["X-Request-ID"]
    assert request_id.startswith("hermes-")
    assert len(request_id) == len("hermes-") + 32


def test_existing_extra_headers_are_retained():
    result = apply({**KW, "extra_headers": {"X-Custom": "keep"}})
    assert result["extra_headers"]["X-Custom"] == "keep"
    assert result["extra_headers"]["X-Request-ID"] == "turn:api:1"
