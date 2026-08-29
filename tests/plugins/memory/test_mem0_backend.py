"""Tests for Mem0Backend abstraction — PlatformBackend, OSSBackend, SelfHostedBackend."""

import copy
import pytest

from plugins.memory.mem0._backend import (
    Mem0Backend,
    PlatformBackend,
    OSSBackend,
    SelfHostedBackend,
)


class FakePlatformClient:
    """Fake MemoryClient for PlatformBackend tests."""

    def __init__(self):
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        return {"results": [{"id": "m1", "memory": "fact1", "score": 0.9}]}

    def get_all(self, **kwargs):
        self.calls.append(("get_all", kwargs))
        return {"count": 1, "next": None, "results": [{"id": "m1", "memory": "fact1"}]}

    def add(self, messages, **kwargs):
        self.calls.append(("add", messages, kwargs))
        return {"status": "PENDING", "event_id": "evt-1"}

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return {"id": kwargs["memory_id"], "text": kwargs["text"]}

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))


class TestPlatformBackend:

    def _make(self):
        client = FakePlatformClient()
        backend = PlatformBackend.__new__(PlatformBackend)
        backend._client = client
        return backend, client

    def test_search_forwards_params(self):
        backend, client = self._make()
        result = backend.search("test query", filters={"user_id": "u1"}, top_k=5)
        assert client.calls[0][0] == "search"
        assert client.calls[0][1] == "test query"
        assert client.calls[0][2]["filters"] == {"user_id": "u1"}
        assert client.calls[0][2]["top_k"] == 5


    def test_add_forwards_kwargs(self):
        backend, client = self._make()
        msgs = [{"role": "user", "content": "hi"}]
        result = backend.add(msgs, user_id="u1", agent_id="hermes", infer=False)
        call = client.calls[0]
        assert call[2]["user_id"] == "u1"
        assert call[2]["infer"] is False
        # metadata kwarg should be omitted entirely when not provided so we
        # don't surprise older mem0 client versions with an unknown kwarg.
        assert "metadata" not in call[2]


    def test_update_forwards(self):
        backend, client = self._make()
        backend.update("m1", "new text")
        assert client.calls[0][1] == {"memory_id": "m1", "text": "new text"}

    def test_delete_forwards(self):
        backend, client = self._make()
        backend.delete("m1")
        assert client.calls[0][1] == {"memory_id": "m1"}


class FakeOSSMemory:
    """Fake mem0.Memory for OSSBackend tests."""

    def __init__(self):
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        return {"results": [{"id": "m1", "memory": "fact1", "score": 0.8}]}

    def get_all(self, **kwargs):
        self.calls.append(("get_all", kwargs))
        return {"results": [{"id": "m1", "memory": "fact1"}]}

    def add(self, messages, **kwargs):
        self.calls.append(("add", messages, kwargs))
        return {"results": [{"id": "m1", "memory": "fact1", "event": "ADD"}]}

    def update(self, memory_id, **kwargs):
        self.calls.append(("update", memory_id, kwargs))
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id):
        self.calls.append(("delete", memory_id))
        return {"message": "Memory deleted successfully!"}


class TestOSSBackend:

    def _make(self):
        memory = FakeOSSMemory()
        backend = OSSBackend.__new__(OSSBackend)
        backend._memory = memory
        return backend, memory


    def test_legacy_api_base_aliases_are_normalized_before_mem0_init(self, monkeypatch):
        import sys
        import types

        captured = {}

        class Memory:
            @staticmethod
            def from_config(config):
                captured.update(config)
                return FakeOSSMemory()

        # OSSBackend.__init__ does `from mem0 import Memory`. mem0 is a lazy
        # optional dep absent from CI's env, so inject a stub module rather
        # than importing the real package (which would ModuleNotFoundError).
        stub_mem0 = types.ModuleType("mem0")
        stub_mem0.Memory = Memory  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mem0", stub_mem0)
        raw = {
            "llm": {
                "provider": "openai",
                "config": {"model": "gpt-5-mini", "api_base": "https://llm.example/v1"},
            },
            "embedder": {
                "provider": "ollama",
                "config": {"model": "nomic-embed-text", "api_base": "http://ollama:11434"},
            },
            "vector_store": {"provider": "qdrant", "config": {}},
        }
        before = copy.deepcopy(raw)

        OSSBackend(raw)

        assert captured["llm"]["config"]["openai_base_url"] == "https://llm.example/v1"
        assert captured["embedder"]["config"]["ollama_base_url"] == "http://ollama:11434"
        assert "api_base" not in captured["llm"]["config"]
        assert "api_base" not in captured["embedder"]["config"]
        assert raw == before


def _oss_test_config(tmp_path):
    """Minimal OSS config: local qdrant under tmp, offline-capable providers."""
    return {
        "llm": {"provider": "openai", "config": {"model": "gpt-5-mini", "api_key": "sk-test"}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small", "api_key": "sk-test"}},
        "vector_store": {"provider": "qdrant", "config": {"path": str(tmp_path / "vs")}},
    }


class TestOSSHistoryDbScoping:
    """#97923: OSS history.db must land under the active Hermes profile, with
    operator-pinned paths (oss config key or MEM0_DIR) left to the SDK."""

    def _stub_mem0(self, monkeypatch):
        import sys
        import types

        captured = {}

        class Memory:
            @staticmethod
            def from_config(config):
                captured.update(config)
                return FakeOSSMemory()

        stub_mem0 = types.ModuleType("mem0")
        stub_mem0.Memory = Memory  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mem0", stub_mem0)
        return captured

    def test_history_db_scoped_to_hermes_home(self, monkeypatch, tmp_path):
        captured = self._stub_mem0(monkeypatch)
        monkeypatch.delenv("MEM0_DIR", raising=False)
        OSSBackend(_oss_test_config(tmp_path), hermes_home=str(tmp_path / "alpha"))
        assert captured["history_db_path"] == str(tmp_path / "alpha" / "mem0" / "history.db")
        # mem0ai's SQLiteManager does not create parent dirs; the fix must.
        assert (tmp_path / "alpha" / "mem0").is_dir()

    def test_history_db_not_injected_without_hermes_home(self, monkeypatch, tmp_path):
        captured = self._stub_mem0(monkeypatch)
        monkeypatch.delenv("MEM0_DIR", raising=False)
        OSSBackend(_oss_test_config(tmp_path))
        assert "history_db_path" not in captured

    def test_mem0_dir_env_keeps_sdk_default(self, monkeypatch, tmp_path):
        captured = self._stub_mem0(monkeypatch)
        monkeypatch.setenv("MEM0_DIR", str(tmp_path / "pinned"))
        OSSBackend(_oss_test_config(tmp_path), hermes_home=str(tmp_path / "alpha"))
        # Operator pinned MEM0_DIR: the SDK default (<MEM0_DIR>/history.db) wins.
        assert "history_db_path" not in captured

    def test_explicit_config_path_wins(self, monkeypatch, tmp_path):
        captured = self._stub_mem0(monkeypatch)
        monkeypatch.delenv("MEM0_DIR", raising=False)
        raw = _oss_test_config(tmp_path)
        raw["history_db_path"] = str(tmp_path / "pinned" / "custom.db")
        OSSBackend(raw, hermes_home=str(tmp_path / "alpha"))
        assert captured["history_db_path"] == str(tmp_path / "pinned" / "custom.db")


class TestOSSHistoryDbRealSDK:
    """#97923 integration: two profiles sharing one native HOME must get
    distinct history.db files from the real pinned mem0ai SDK.

    Skipped unless the ``mem0`` extra is installed (CI runs the stubbed
    tests above; this runs wherever mem0ai==2.0.10 is available)."""

    def test_two_profiles_get_distinct_history_dbs(self, monkeypatch, tmp_path):
        # Keep the SDK's import-time side effects (native ~/.mem0 dir,
        # telemetry store) inside the test sandbox.
        monkeypatch.setenv("HOME", str(tmp_path / "native-home"))
        monkeypatch.setenv("MEM0_TELEMETRY", "False")
        monkeypatch.delenv("MEM0_DIR", raising=False)
        pytest.importorskip("mem0")

        def backend_for(profile):
            cfg = _oss_test_config(tmp_path)
            cfg["vector_store"]["config"]["path"] = str(tmp_path / profile / "vs")
            return OSSBackend(cfg, hermes_home=str(tmp_path / profile))

        alpha, beta = backend_for("alpha"), backend_for("beta")
        alpha_path = alpha._memory.config.history_db_path
        beta_path = beta._memory.config.history_db_path
        assert alpha_path == str(tmp_path / "alpha" / "mem0" / "history.db")
        assert beta_path == str(tmp_path / "beta" / "mem0" / "history.db")
        assert alpha_path != beta_path
        # The SQLiteManager actually opened the profile-local file, and no
        # shared history.db appeared under the native home.
        assert alpha._memory.db.db_path == alpha_path
        assert (tmp_path / "alpha" / "mem0" / "history.db").exists()
        assert (tmp_path / "beta" / "mem0" / "history.db").exists()
        assert not (tmp_path / "native-home" / ".mem0" / "history.db").exists()


httpx = pytest.importorskip("httpx")


class _StubServer:
    """Records requests and serves the real self-hosted server's response shapes."""

    def __init__(self, rows=10):
        self.requests = []
        self._rows = [{"id": f"m{i}", "memory": f"f{i}"} for i in range(rows)]

    def handler(self, request):
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/search" and method == "POST":
            return httpx.Response(200, json={"results": [{"id": "m1", "memory": "tea", "score": 0.9}]})
        if path == "/memories" and method == "GET":
            top_k = int(request.url.params.get("top_k", len(self._rows)))
            return httpx.Response(200, json={"results": self._rows[:top_k]})
        if path == "/memories" and method == "POST":
            return httpx.Response(200, json={"results": [{"id": "new", "memory": "stored", "event": "ADD"}]})
        if path.startswith("/memories/") and method in ("PUT", "DELETE"):
            if path.endswith("/missing"):  # server 404s unknown ids
                return httpx.Response(404, json={"detail": "Memory not found"})
            verb = "updated" if method == "PUT" else "Memory deleted successfully"
            return httpx.Response(200, json={"message": verb})
        return httpx.Response(404, json={"detail": "not found"})


def _backend(server, api_key="adminkey", host="http://sh:8888"):
    """Build a SelfHostedBackend routed through the stub transport.

    Uses the real __init__ (via the injectable ``transport`` kwarg) so the
    constructor's header/base_url setup is exercised by every test here.
    """
    return SelfHostedBackend(
        api_key, host, transport=httpx.MockTransport(server.handler)
    )


class TestSelfHostedBackend:
    # --- constructor / auth setup (the crux of the bug) -------------------

    def test_init_uses_x_api_key_not_token_auth(self):
        b = SelfHostedBackend("adminkey", "http://sh:8888")
        assert b._client.headers["x-api-key"] == "adminkey"
        assert "authorization" not in b._client.headers  # NOT the cloud 'Token' scheme


    # --- search ----------------------------------------------------------


    # --- add / update / delete ------------------------------------------


    # --- error propagation (feeds the plugin's circuit breaker) ----------

    def test_http_error_raises(self):
        s = _StubServer()
        with pytest.raises(httpx.HTTPStatusError):
            _backend(s).delete("missing")  # 404 -> raise_for_status; 'not found' won't trip breaker
