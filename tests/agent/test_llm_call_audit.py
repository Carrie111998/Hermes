from agent.llm_call_audit import audit_llm_call, key_fingerprint
from agent.usage_pricing import CanonicalUsage


def test_key_fingerprint_never_exposes_secret() -> None:
    assert key_fingerprint("sk-test-secret") != "sk-test-secret"
    assert len(key_fingerprint("sk-test-secret")) == 8
    assert key_fingerprint("") == "none"


def test_audit_llm_call_inserts_hashed_key(monkeypatch) -> None:
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params):
            calls.append((query, params))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setenv("SUPABASE_DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setattr("agent.llm_call_audit._connect", lambda dsn: Connection())

    ok = audit_llm_call(
        tenant_slug="tgg",
        session_id="session-1",
        provider="gemini",
        model="gemini-2.5-flash",
        api_key="sk-real-secret",
        usage=CanonicalUsage(input_tokens=100, output_tokens=20, cache_read_tokens=5),
        metadata={"api_mode": "chat_completions"},
    )

    assert ok is True
    assert calls
    params = calls[0][1]
    assert params[0] == "tgg"
    assert params[3] == "gemini-2.5-flash"
    assert params[4] == key_fingerprint("sk-real-secret")
    assert "sk-real-secret" not in repr(params)
