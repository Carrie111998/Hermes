"""Tests for Mem0Backend abstraction — PlatformBackend, OSSBackend, SelfHostedBackend."""

import copy
import importlib
import json
import os
import sys
import types
from dataclasses import dataclass, field
from types import SimpleNamespace

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


@dataclass
class _FakeMem0State:
    factory_registrations: list = field(default_factory=list)
    from_config_calls: int = 0
    clients: list = field(default_factory=list)
    requests: list = field(default_factory=list)


def _install_fake_mem0(monkeypatch):
    """Install a small mem0 2.0.10-shaped surface for OSS backend tests."""

    state = _FakeMem0State()

    class BaseLlmConfig:
        def __init__(
            self,
            model=None,
            temperature=0.1,
            api_key=None,
            max_tokens=2000,
            top_p=0.1,
            top_k=1,
            enable_vision=False,
            vision_details="auto",
            reasoning_effort=None,
            http_client_proxies=None,
            is_reasoning_model=None,
            **kwargs,
        ):
            self.model = model
            self.temperature = temperature
            self.api_key = api_key
            self.max_tokens = max_tokens
            self.top_p = top_p
            self.top_k = top_k
            self.enable_vision = enable_vision
            self.vision_details = vision_details
            self.reasoning_effort = reasoning_effort
            self.http_client_proxies = http_client_proxies
            self.is_reasoning_model = is_reasoning_model
            for name, value in kwargs.items():
                setattr(self, name, value)

    class OpenAIConfig(BaseLlmConfig):
        def __init__(
            self,
            model=None,
            temperature=0.1,
            api_key=None,
            max_tokens=2000,
            top_p=0.1,
            top_k=1,
            enable_vision=False,
            vision_details="auto",
            reasoning_effort=None,
            http_client_proxies=None,
            is_reasoning_model=None,
            openai_base_url=None,
            models=None,
            route="fallback",
            openrouter_base_url=None,
            site_url=None,
            app_name=None,
            store=None,
            response_callback=None,
        ):
            super().__init__(
                model=model,
                temperature=temperature,
                api_key=api_key,
                max_tokens=max_tokens,
                top_p=top_p,
                top_k=top_k,
                enable_vision=enable_vision,
                vision_details=vision_details,
                reasoning_effort=reasoning_effort,
                http_client_proxies=http_client_proxies,
                is_reasoning_model=is_reasoning_model,
            )
            self.openai_base_url = openai_base_url
            self.models = models
            self.route = route
            self.openrouter_base_url = openrouter_base_url
            self.site_url = site_url
            self.app_name = app_name
            self.store = store
            self.response_callback = response_callback

    class LLMBase:
        def __init__(self, config=None):
            self.config = config or BaseLlmConfig()
            if not hasattr(self.config, "model"):
                raise ValueError("Configuration must have a 'model' attribute")

        def _get_supported_params(self, **kwargs):
            if self.config.is_reasoning_model:
                return {
                    name: kwargs[name]
                    for name in ("messages", "response_format", "tools", "tool_choice")
                    if name in kwargs
                }
            params = {
                "temperature": self.config.temperature,
                "top_p": self.config.top_p,
                "max_tokens": self.config.max_tokens,
            }
            params.update(kwargs)
            return params

    class OpenAILLM(LLMBase):
        @staticmethod
        def _parse_response(response, tools):
            if not tools:
                return response.choices[0].message.content
            parsed = {
                "content": response.choices[0].message.content,
                "tool_calls": [],
            }
            for tool_call in response.choices[0].message.tool_calls or []:
                parsed["tool_calls"].append(
                    {
                        "name": tool_call.function.name,
                        "arguments": json.loads(tool_call.function.arguments),
                    }
                )
            return parsed

    class Factory:
        provider_to_class = {
            "openai": ("mem0.llms.openai.OpenAILLM", OpenAIConfig),
            "ollama": ("mem0.llms.openai.OpenAILLM", BaseLlmConfig),
        }

        @classmethod
        def register_provider(cls, name, class_path, config_class=None):
            cls.provider_to_class[name] = (
                class_path,
                config_class or BaseLlmConfig,
            )
            state.factory_registrations.append((name, class_path, config_class))

        @classmethod
        def create(cls, provider_name, config=None, **kwargs):
            class_path, config_class = cls.provider_to_class[provider_name]
            if config is None:
                config = config_class(**kwargs)
            elif isinstance(config, dict):
                config = config_class(**config)
            module_name, class_name = class_path.rsplit(".", 1)
            llm_class = getattr(importlib.import_module(module_name), class_name)
            return llm_class(config)

    class MemoryConfig:
        def __init__(self, **config):
            llm = config["llm"]
            if llm["provider"] not in {"openai", "ollama"}:
                raise ValueError(
                    f"Unsupported LLM provider: {llm['provider']}"
                )
            self.llm = SimpleNamespace(
                provider=llm["provider"],
                config=copy.deepcopy(llm.get("config", {})),
            )
            embedder = config["embedder"]
            self.embedder = SimpleNamespace(
                provider=embedder["provider"],
                config=copy.deepcopy(embedder.get("config", {})),
            )
            vector_store = config["vector_store"]
            self.vector_store = SimpleNamespace(
                provider=vector_store["provider"],
                config=copy.deepcopy(vector_store.get("config", {})),
            )
            self.version = config.get("version", "v1.1")

    class Memory:
        instances = []

        def __init__(self, config):
            self.config = config
            self.llm = Factory.create(config.llm.provider, config.llm.config)
            self.embedding_model = SimpleNamespace(
                provider=config.embedder.provider,
                config=config.embedder.config,
            )
            self.vector_store = SimpleNamespace(
                provider=config.vector_store.provider,
                config=config.vector_store.config,
            )
            type(self).instances.append(self)

        @classmethod
        def from_config(cls, config):
            # This mirrors mem0 2.0.10: validation rejects the private provider
            # before the factory gets a chance to resolve its registration.
            state.from_config_calls += 1
            return cls(MemoryConfig(**config))

    class FakeOpenAI:
        def __init__(self, *, api_key, base_url):
            self.api_key = api_key
            self.base_url = base_url
            state.clients.append(self)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        def _create(self, **params):
            state.requests.append(params)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="direct answer",
                            tool_calls=[
                                SimpleNamespace(
                                    function=SimpleNamespace(
                                        name="remember",
                                        arguments='{"fact": "tea"}',
                                    )
                                )
                            ],
                        )
                    )
                ]
            )

    package_names = {
        "mem0": types.ModuleType("mem0"),
        "mem0.configs": types.ModuleType("mem0.configs"),
        "mem0.configs.llms": types.ModuleType("mem0.configs.llms"),
        "mem0.llms": types.ModuleType("mem0.llms"),
        "mem0.utils": types.ModuleType("mem0.utils"),
        "mem0.configs.base": types.ModuleType("mem0.configs.base"),
        "mem0.configs.llms.base": types.ModuleType("mem0.configs.llms.base"),
        "mem0.configs.llms.openai": types.ModuleType("mem0.configs.llms.openai"),
        "mem0.llms.base": types.ModuleType("mem0.llms.base"),
        "mem0.llms.openai": types.ModuleType("mem0.llms.openai"),
        "mem0.utils.factory": types.ModuleType("mem0.utils.factory"),
        "openai": types.ModuleType("openai"),
    }
    setattr(package_names["mem0"], "Memory", Memory)
    setattr(package_names["mem0.configs.base"], "MemoryConfig", MemoryConfig)
    setattr(package_names["mem0.configs.llms.base"], "BaseLlmConfig", BaseLlmConfig)
    setattr(package_names["mem0.configs.llms.openai"], "OpenAIConfig", OpenAIConfig)
    setattr(package_names["mem0.llms.base"], "LLMBase", LLMBase)
    setattr(package_names["mem0.llms.openai"], "OpenAILLM", OpenAILLM)
    setattr(package_names["mem0.utils.factory"], "LlmFactory", Factory)
    setattr(package_names["openai"], "OpenAI", FakeOpenAI)
    for name, module in package_names.items():
        if name in {"mem0", "mem0.configs", "mem0.configs.llms", "mem0.llms", "mem0.utils"}:
            module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)

    # The class-path registration imports this module after the fake mem0
    # surface is installed, so it binds to the test doubles above.
    monkeypatch.delitem(
        sys.modules, "plugins.memory.mem0._openai_llm", raising=False
    )
    return state, Memory, Factory


class TestOSSBackend:

    def _make(self):
        memory = FakeOSSMemory()
        backend = OSSBackend.__new__(OSSBackend)
        backend._memory = memory
        return backend, memory


    def test_legacy_api_base_aliases_are_normalized_before_mem0_init(self, monkeypatch):
        state, Memory, factory = _install_fake_mem0(monkeypatch)
        raw = {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "gpt-5-mini",
                    "api_key": "openai-sentinel",
                    "api_base": "https://llm.example/v1",
                },
            },
            "embedder": {
                "provider": "ollama",
                "config": {"model": "nomic-embed-text", "api_base": "http://ollama:11434"},
            },
            "vector_store": {"provider": "qdrant", "config": {}},
        }
        before = copy.deepcopy(raw)
        environment = dict(os.environ)

        OSSBackend(raw)

        assert len(Memory.instances) == 1
        captured = Memory.instances[0].config
        assert captured.llm.provider == "hermes_openai"
        assert captured.llm.config["openai_base_url"] == "https://llm.example/v1"
        assert captured.embedder.provider == "ollama"
        assert captured.embedder.config["ollama_base_url"] == "http://ollama:11434"
        assert "api_base" not in captured.llm.config
        assert "api_base" not in captured.embedder.config
        assert factory.provider_to_class["hermes_openai"][1].__name__ == "OpenAIConfig"
        assert len(state.factory_registrations) == 1
        assert state.from_config_calls == 0
        assert raw == before
        assert dict(os.environ) == environment

    def test_direct_openai_uses_openai_credentials_and_request_shape(self, monkeypatch):
        state, _, factory = _install_fake_mem0(monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-sentinel")
        monkeypatch.setenv("OPENAI_API_KEY", "env-openai-sentinel")

        module = importlib.import_module("plugins.memory.mem0._openai_llm")
        callback_calls = []
        config = factory.provider_to_class["openai"][1](
            model="gpt-5-mini",
            api_key="configured-openai-sentinel",
            openai_base_url="https://openai.example/v1",
            models=["router-model"],
            route="lowest-latency",
            site_url="https://hermes.example",
            app_name="Hermes",
            store=True,
            response_callback=lambda *args: callback_calls.append(args),
        )
        adapter = module.DirectOpenAILLM(config)
        assert adapter.config.is_reasoning_model is True
        tools = [
            {
                "type": "function",
                "function": {"name": "remember", "parameters": {}},
            }
        ]

        result = adapter.generate_response(
            [{"role": "user", "content": "remember tea"}],
            response_format={"type": "json_object"},
            tools=tools,
            tool_choice="required",
        )

        assert len(state.clients) == 1
        client = state.clients[0]
        assert client.api_key == "configured-openai-sentinel"
        assert client.base_url == "https://openai.example/v1"
        request = state.requests[0]
        assert request["model"] == "gpt-5-mini"
        assert request["tools"] == tools
        assert request["tool_choice"] == "required"
        assert request["response_format"] == {"type": "json_object"}
        assert request["store"] is True
        assert "models" not in request
        assert "route" not in request
        assert "extra_headers" not in request
        assert "temperature" not in request
        assert "top_p" not in request
        assert "max_tokens" not in request
        assert result == {
            "content": "direct answer",
            "tool_calls": [{"name": "remember", "arguments": {"fact": "tea"}}],
        }
        assert len(callback_calls) == 1
        assert callback_calls[0][0] is adapter
        assert callback_calls[0][2] == request

    def test_direct_openai_preserves_explicit_non_reasoning_override(self, monkeypatch):
        state, _, factory = _install_fake_mem0(monkeypatch)
        config = factory.provider_to_class["openai"][1](
            model="gpt-5-mini",
            api_key="configured-openai-sentinel",
            is_reasoning_model=False,
        )

        module = importlib.import_module("plugins.memory.mem0._openai_llm")
        adapter = module.DirectOpenAILLM(config)
        adapter.generate_response([{"role": "user", "content": "remember tea"}])

        assert adapter.config.is_reasoning_model is False
        request = state.requests[0]
        assert request["temperature"] == 0.1
        assert request["top_p"] == 0.1
        assert request["max_tokens"] == 2000

    def test_direct_openai_defaults_missing_model_to_reasoning_safe_mini(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "environment-openai-sentinel")
        _install_fake_mem0(monkeypatch)

        module = importlib.import_module("plugins.memory.mem0._openai_llm")
        adapter = module.DirectOpenAILLM()

        assert adapter.config.model == "gpt-5-mini"
        assert adapter.config.is_reasoning_model is True

    def test_direct_openai_uses_openai_environment_when_config_omits_values(self, monkeypatch):
        state, _, factory = _install_fake_mem0(monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-sentinel")
        monkeypatch.setenv("OPENAI_API_KEY", "env-openai-sentinel")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://env-openai.example/v1")

        module = importlib.import_module("plugins.memory.mem0._openai_llm")
        config = factory.provider_to_class["openai"][1](model="gpt-5-mini")
        adapter = module.DirectOpenAILLM(config)

        assert len(state.clients) == 1
        assert state.clients[0].api_key == "env-openai-sentinel"
        assert state.clients[0].base_url == "https://env-openai.example/v1"

    def test_missing_openai_key_fails_before_client_and_hides_router_secret(self, monkeypatch):
        state, _, factory = _install_fake_mem0(monkeypatch)
        router_secret = "router-secret-sentinel"
        monkeypatch.setenv("OPENROUTER_API_KEY", router_secret)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        module = importlib.import_module("plugins.memory.mem0._openai_llm")
        config = factory.provider_to_class["openai"][1](
            model="gpt-5-mini",
            api_key=None,
        )

        with pytest.raises(ValueError) as exc_info:
            module.DirectOpenAILLM(config)

        assert "OpenAI API key" in str(exc_info.value)
        assert router_secret not in str(exc_info.value)
        assert state.clients == []
        assert state.requests == []

    def test_registration_is_idempotent_and_clients_keep_instance_config(self, monkeypatch):
        state, Memory, factory = _install_fake_mem0(monkeypatch)
        first = {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "gpt-5-mini",
                    "api_key": "first-openai-sentinel",
                    "openai_base_url": "https://first.example/v1",
                },
            },
            "embedder": {"provider": "ollama", "config": {}},
            "vector_store": {"provider": "qdrant", "config": {}},
        }
        second = {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "gpt-5-mini",
                    "api_key": "second-openai-sentinel",
                    "openai_base_url": "https://second.example/v1",
                },
            },
            "embedder": {"provider": "ollama", "config": {}},
            "vector_store": {"provider": "qdrant", "config": {}},
        }
        first_before = copy.deepcopy(first)
        second_before = copy.deepcopy(second)

        OSSBackend(first)
        OSSBackend(second)

        assert len(state.factory_registrations) == 1
        assert factory.provider_to_class["hermes_openai"][0].endswith(
            "_openai_llm.DirectOpenAILLM"
        )
        assert [
            (client.api_key, client.base_url) for client in state.clients
        ] == [
            ("first-openai-sentinel", "https://first.example/v1"),
            ("second-openai-sentinel", "https://second.example/v1"),
        ]
        assert len(Memory.instances) == 2
        assert state.from_config_calls == 0
        assert first == first_before
        assert second == second_before

    def test_ollama_bypasses_direct_openai_adapter(self, monkeypatch):
        state, Memory, factory = _install_fake_mem0(monkeypatch)
        raw = {
            "llm": {
                "provider": "ollama",
                "config": {
                    "model": "llama3.1:8b",
                    "api_base": "http://ollama:11434",
                },
            },
            "embedder": {
                "provider": "ollama",
                "config": {
                    "model": "nomic-embed-text",
                    "api_base": "http://ollama:11434",
                },
            },
            "vector_store": {"provider": "qdrant", "config": {}},
        }
        before = copy.deepcopy(raw)

        OSSBackend(raw)

        assert len(Memory.instances) == 1
        assert state.from_config_calls == 1
        assert Memory.instances[0].config.llm.provider == "ollama"
        assert Memory.instances[0].config.embedder.provider == "ollama"
        assert "hermes_openai" not in factory.provider_to_class
        assert state.clients == []
        assert raw == before


class _FakeCollectionInfo:
    def __init__(self, dims: int):
        class _Vectors:
            def __init__(self, size):
                self.size = size
        self.config = type("C", (), {"params": type("P", (), {"vectors": _Vectors(dims)})()})()


class _FakeQdrantClient:
    """Fake QdrantClient that tracks calls — no file locks."""
    def __init__(self, *, existing_dims: int | None = 8, collection_name: str = "mem0"):
        self._existing_dims = existing_dims
        self._collection_name = collection_name
        self.deleted = False
        self.creations = []

    def collection_exists(self, name: str) -> bool:
        return self._existing_dims is not None and name == self._collection_name

    def get_collection(self, name: str):
        return _FakeCollectionInfo(self._existing_dims)

    def delete_collection(self, name: str):
        self.deleted = True
        self._existing_dims = None  # collection no longer exists

    def create_collection(self, **kwargs):
        self.creations.append(kwargs)
        # Update dims so get_collection() reflects the new collection
        vc = kwargs.get("vectors_config")
        if vc is not None and hasattr(vc, "size"):
            self._existing_dims = vc.size
        elif not self._existing_dims:
            self._existing_dims = 0  # placeholder if unknown


class _FakeVectorStore:
    """Fake vector store that wraps a fake QdrantClient."""
    def __init__(self, client: _FakeQdrantClient, on_disk: bool = False):
        self.client = client
        self.on_disk = on_disk

    def create_col(self, vector_size: int, on_disk: bool):
        """Recreate the collection — update dims on the fake client."""
        self.client._existing_dims = vector_size


class TestOSSBackendRecreateQdrantDims:
    """Verify _recreate_qdrant_if_dims_changed uses Memory's own client."""

    def _make_backend(self, client: _FakeQdrantClient, collection_name: str = "mem0"):
        backend = OSSBackend.__new__(OSSBackend)
        vs = _FakeVectorStore(client)
        memory = type("M", (), {
            "vector_store": vs,
            "collection_name": collection_name,
        })()
        backend._memory = memory
        return backend

    def test_dims_match_no_delete(self):
        """When collection dims match expected, nothing happens."""
        client = _FakeQdrantClient(existing_dims=384)
        backend = self._make_backend(client)
        backend._recreate_qdrant_if_dims_changed(384)
        assert not client.deleted

    def test_dims_mismatch_recreates_collection(self):
        """When collection dims differ, collection is deleted AND recreated."""
        client = _FakeQdrantClient(existing_dims=128)
        backend = self._make_backend(client)
        vs = backend._memory.vector_store
        original_create_col = vs.create_col
        called = []
        def tracking_create_col(vector_size, on_disk):
            called.append((vector_size, on_disk))
            return original_create_col(vector_size, on_disk)
        vs.create_col = tracking_create_col

        backend._recreate_qdrant_if_dims_changed(384)

        assert client.deleted, "Collection should be deleted on dim mismatch"
        assert len(called) == 1, "create_col should be called exactly once"
        assert called[0] == (384, False), "Should recreate with expected dims"

    def test_missing_collection_noop(self):
        """When collection doesn't exist, nothing happens."""
        client = _FakeQdrantClient(existing_dims=None)
        backend = self._make_backend(client)
        backend._recreate_qdrant_if_dims_changed(384)
        assert not client.deleted

    def test_no_vector_store_client_noop(self):
        """When Memory has no vector_store.client, nothing happens."""
        backend = OSSBackend.__new__(OSSBackend)
        backend._memory = type("M", (), {"vector_store": None, "collection_name": "mem0"})()
        backend._recreate_qdrant_if_dims_changed(384)
        # Should not raise

    def test_uses_memory_own_client(self):
        """Verify the method accesses Memory's vector_store.client, not a new QdrantClient."""
        client = _FakeQdrantClient(existing_dims=128)
        backend = self._make_backend(client)
        vs = backend._memory.vector_store
        called = []
        original = vs.create_col
        def tracking_create_col(vector_size, on_disk):
            called.append((vector_size, on_disk))
            return original(vector_size, on_disk)
        vs.create_col = tracking_create_col

        backend._recreate_qdrant_if_dims_changed(384)

        assert called, "create_col was called on Memory's own vector_store"
        assert client.deleted

    def test_no_vector_store_itself_noop(self):
        """When Memory.vector_store is None, nothing happens."""
        backend = OSSBackend.__new__(OSSBackend)
        backend._memory = type("M", (), {"vector_store": None, "collection_name": "mem0"})()
        backend._recreate_qdrant_if_dims_changed(384)
        # Should not raise

    def test_dims_none_skips_delete(self):
        """When Qdrant reports None dims, nothing happens."""
        class _NoDimsCollectionInfo:
            class _Vectors:
                size = None
            config = type("C", (), {"params": type("P", (), {"vectors": _Vectors()})()})()

        class _NoDimsQdrantClient(_FakeQdrantClient):
            def get_collection(self, name):
                return _NoDimsCollectionInfo()

        client = _NoDimsQdrantClient(existing_dims=384)
        backend = self._make_backend(client)
        backend._recreate_qdrant_if_dims_changed(512)
        assert not client.deleted

    def test_on_disk_respected(self):
        """The vector store's on_disk setting is passed to create_col."""
        client = _FakeQdrantClient(existing_dims=128)
        vs = _FakeVectorStore(client, on_disk=True)
        backend = OSSBackend.__new__(OSSBackend)
        memory = type("M", (), {"vector_store": vs, "collection_name": "mem0"})()
        backend._memory = memory
        called = []
        original = vs.create_col
        def tracking(vector_size, on_disk):
            called.append((vector_size, on_disk))
            return original(vector_size, on_disk)
        vs.create_col = tracking

        backend._recreate_qdrant_if_dims_changed(384)

        assert client.deleted
        assert called[0] == (384, True), "on_disk=True should be forwarded"

    def test_missing_create_col_does_not_delete(self):
        """When vector store lacks create_col, the collection is NOT deleted
        (bare create_collection would produce a degraded collection)."""
        client = _FakeQdrantClient(existing_dims=128)

        class _VSWoCreate:
            def __init__(self, c):
                self.client = c
                self.on_disk = False

        vs = _VSWoCreate(client)
        backend = OSSBackend.__new__(OSSBackend)
        memory = type("M", (), {"vector_store": vs, "collection_name": "mem0"})()
        backend._memory = memory

        backend._recreate_qdrant_if_dims_changed(384)

        assert not client.deleted, "Should NOT delete when create_col is absent"

    def test_partial_failure_triggers_fallback(self, caplog):
        """When delete succeeds but create_col raises, the fallback is attempted."""
        import logging
        caplog.set_level(logging.WARNING)

        class _RaisingVectorStore:
            def __init__(self):
                self.client = _FakeQdrantClient(existing_dims=128)
                self.on_disk = False
            def create_col(self, vector_size, on_disk):
                raise RuntimeError("create_col failed: connection refused")

        vs = _RaisingVectorStore()
        backend = OSSBackend.__new__(OSSBackend)
        memory = type("M", (), {
            "vector_store": vs,
            "collection_name": "mem0",
        })()
        backend._memory = memory

        backend._recreate_qdrant_if_dims_changed(384)

        assert vs.client.deleted, "Collection should still be deleted"
        # The fallback (bare client.create_collection) should have been called
        assert len(vs.client.creations) == 1, "Fallback create_collection should be called"
        fallback_kwargs = vs.client.creations[0]
        assert fallback_kwargs["collection_name"] == "mem0"
        assert "attempting fallback" in caplog.text


class TestOSSBackendRecreateQdrantIntegration:
    """Verify the collection is functional and correctly configured AFTER a dim-mismatch recreate."""

    def _make_backend(self, client: _FakeQdrantClient, collection_name: str = "mem0"):
        backend = OSSBackend.__new__(OSSBackend)
        vs = _FakeVectorStore(client)
        memory = type("M", (), {
            "vector_store": vs,
            "collection_name": collection_name,
        })()
        backend._memory = memory
        return backend

    def test_recreate_updates_collection_dims(self):
        """After recreate, get_collection() should return the new dimension size."""
        client = _FakeQdrantClient(existing_dims=128)
        backend = self._make_backend(client)

        backend._recreate_qdrant_if_dims_changed(384)

        info = client.get_collection("mem0")
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):
            first = next(iter(vectors.values()), None)
            new_dims = first.size if first else None
        else:
            new_dims = getattr(vectors, "size", None)
        assert new_dims == 384, (
            f"Collection dims should be updated to 384, got {new_dims}"
        )

    def test_recreate_preserves_on_disk(self):
        """After recreate, the on_disk config is passed through correctly."""
        client = _FakeQdrantClient(existing_dims=128)
        vs = _FakeVectorStore(client, on_disk=True)
        backend = OSSBackend.__new__(OSSBackend)
        memory = type("M", (), {
            "vector_store": vs,
            "collection_name": "mem0",
        })()
        backend._memory = memory

        backend._recreate_qdrant_if_dims_changed(384)

        info = client.get_collection("mem0")
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):
            first = next(iter(vectors.values()), None)
            new_dims = first.size if first else None
        else:
            new_dims = getattr(vectors, "size", None)
        assert new_dims == 384

    def test_recreate_does_not_affect_other_collections(self):
        """Only the target collection should be affected by recreate."""

        class _MultiColQdrantClient(_FakeQdrantClient):
            def __init__(self):
                super().__init__(existing_dims=128)
                self._other_dims = 256

            def collection_exists(self, name: str) -> bool:
                return name in ("mem0", "other_col")

            def get_collection(self, name: str):
                if name == "other_col":
                    return _FakeCollectionInfo(self._other_dims)
                return _FakeCollectionInfo(self._existing_dims)

            def delete_collection(self, name: str):
                super().delete_collection(name)
                if name == "other_col":
                    self._other_dims = None

            def create_collection(self, **kwargs):
                super().create_collection(**kwargs)
                if kwargs.get("collection_name") == "other_col":
                    self._other_dims = 256

        client = _MultiColQdrantClient()
        vs = _FakeVectorStore(client)
        backend = OSSBackend.__new__(OSSBackend)
        memory = type("M", (), {
            "vector_store": vs,
            "collection_name": "mem0",
        })()
        backend._memory = memory

        backend._recreate_qdrant_if_dims_changed(384)

        # Target collection should have new dims
        info = client.get_collection("mem0")
        vectors = info.config.params.vectors
        current = vectors.size if hasattr(vectors, "size") else None
        assert current == 384

        # Other collection should be untouched
        other = client.get_collection("other_col")
        other_vectors = other.config.params.vectors
        other_size = other_vectors.size if hasattr(other_vectors, "size") else None
        assert other_size == 256, "Other collections must not be affected"

    def test_recreate_fallback_creates_basic_collection(self, caplog):
        """When create_col raises, the fallback creates a basic collection."""
        import logging
        caplog.set_level(logging.WARNING)

        class _FallbackVectorStore:
            def __init__(self):
                self.client = _FakeQdrantClient(existing_dims=128)
                self.on_disk = False
            def create_col(self, vector_size, on_disk):
                raise RuntimeError("primary failed")

        vs = _FallbackVectorStore()
        backend = OSSBackend.__new__(OSSBackend)
        memory = type("M", (), {
            "vector_store": vs,
            "collection_name": "mem0",
        })()
        backend._memory = memory

        backend._recreate_qdrant_if_dims_changed(384)

        # Collection should still exist (via fallback)
        info = vs.client.get_collection("mem0")
        vectors = info.config.params.vectors
        current = vectors.size if hasattr(vectors, "size") else None
        assert current == 384, (
            "Fallback should create a collection with the expected dims"
        )
        assert vs.client.deleted
        assert "attempting fallback" in caplog.text

    def test_fallback_reported_in_creations(self):
        """Verify client.create_collection is called by the fallback path."""

        class _FallbackVStore:
            def __init__(self):
                self.client = _FakeQdrantClient(existing_dims=128)
                self.on_disk = False
            def create_col(self, vector_size, on_disk):
                raise RuntimeError("boom")

        vs = _FallbackVStore()
        backend = OSSBackend.__new__(OSSBackend)
        memory = type("M", (), {
            "vector_store": vs,
            "collection_name": "mem0",
        })()
        backend._memory = memory

        backend._recreate_qdrant_if_dims_changed(384)

        assert len(vs.client.creations) == 1
        kwargs = vs.client.creations[0]
        assert kwargs["collection_name"] == "mem0"
        # The vectors_config should contain the expected dims
        vc = kwargs.get("vectors_config")
        assert vc is not None, "vectors_config must be provided in fallback"
        assert hasattr(vc, "size"), "vectors_config should have size"
        assert vc.size == 384


qdrant_models = pytest.importorskip("qdrant_client.models")


class _RealMem0StyleVectorStore:
    """Vector store over a REAL QdrantClient, reproducing mem0's create_col.

    Mirrors ``mem0.vector_stores.qdrant.Qdrant``: the dense slot plus a ``bm25``
    sparse slot with the IDF modifier, and ``_has_bm25_slot`` — the flag mem0's
    insert path consults to decide whether to write the sparse vector.
    """

    def __init__(self, client, collection_name="mem0", on_disk=False):
        self.client = client
        self.collection_name = collection_name
        self.on_disk = on_disk
        self.is_local = True  # embedded Qdrant: no payload indexes, as in mem0
        self._has_bm25_slot = False

    def create_col(self, vector_size, on_disk, distance=None):
        from qdrant_client.models import (
            Distance,
            Modifier,
            SparseVectorParams,
            VectorParams,
        )

        if self.client.collection_exists(self.collection_name):
            return
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=VectorParams(
                size=vector_size, distance=distance or Distance.COSINE, on_disk=on_disk
            ),
            sparse_vectors_config={"bm25": SparseVectorParams(modifier=Modifier.IDF)},
        )
        self._has_bm25_slot = True


class _RealStoreFailingCreateCol(_RealMem0StyleVectorStore):
    """Same real client, but ``create_col`` fails — forces the fallback path."""

    def create_col(self, vector_size, on_disk, distance=None):
        raise RuntimeError("create_col failed: simulated backend error")


def _sparse_config(client, collection_name="mem0"):
    return client.get_collection(collection_name).config.params.sparse_vectors


def _dense_dims(client, collection_name="mem0"):
    vectors = client.get_collection(collection_name).config.params.vectors
    if isinstance(vectors, dict):
        first = next(iter(vectors.values()), None)
        return first.size if first else None
    return getattr(vectors, "size", None)


def _upsert_hybrid(client, collection_name, dims, point_id=1):
    """Write a point the way mem0 does when the bm25 slot exists.

    Raises if the collection has no ``bm25`` sparse slot — which is exactly how
    a degraded (dense-only) collection surfaces: it rejects writes.
    """
    from qdrant_client.models import PointStruct, SparseVector

    client.upsert(
        collection_name=collection_name,
        points=[
            PointStruct(
                id=point_id,
                vector={
                    "": [0.1] * dims,
                    "bm25": SparseVector(indices=[7, 42], values=[0.5, 0.9]),
                },
                payload={"user_id": "u1", "data": "likes tea"},
            )
        ],
    )


class TestQdrantRecreateRealContract:
    """Real-Qdrant tests: the recreate must preserve the full Mem0 contract.

    These use an actual in-memory ``QdrantClient`` (not a fake), so the
    assertions are against Qdrant's real collection config and its real
    accept/reject behaviour on writes.
    """

    def _backend(self, vs):
        backend = OSSBackend.__new__(OSSBackend)
        backend._memory = type("M", (), {
            "vector_store": vs,
            "collection_name": vs.collection_name,
        })()
        return backend

    def _client(self):
        from qdrant_client import QdrantClient
        return QdrantClient(location=":memory:")

    def _seed(self, vs, dims):
        """Create the pre-existing (wrong-dims) collection the way mem0 would."""
        vs.create_col(dims, vs.on_disk)
        assert vs._has_bm25_slot
        assert "bm25" in _sparse_config(vs.client)

    # --- primary path: create_col rebuilds the collection ------------------

    def test_primary_path_preserves_bm25_and_dims(self):
        client = self._client()
        vs = _RealMem0StyleVectorStore(client)
        self._seed(vs, 128)

        self._backend(vs)._recreate_qdrant_if_dims_changed(384)

        assert client.collection_exists("mem0"), "collection must exist after recreate"
        assert _dense_dims(client) == 384
        sparse = _sparse_config(client)
        assert sparse and "bm25" in sparse, "bm25 sparse slot must survive recreate"
        assert sparse["bm25"].modifier == qdrant_models.Modifier.IDF

    def test_primary_path_collection_accepts_hybrid_write_and_search(self):
        client = self._client()
        vs = _RealMem0StyleVectorStore(client)
        self._seed(vs, 128)

        self._backend(vs)._recreate_qdrant_if_dims_changed(384)

        _upsert_hybrid(client, "mem0", 384)
        hits = client.query_points(
            collection_name="mem0", query=[0.1] * 384, limit=5
        ).points
        assert len(hits) == 1
        assert hits[0].payload["data"] == "likes tea"

    # --- fallback path: create_col raises, we rebuild by hand --------------

    def test_fallback_preserves_bm25_sparse_config(self):
        """The regression under review: the fallback must NOT create a
        dense-only collection, which would later reject mem0's writes."""
        client = self._client()
        seed = _RealMem0StyleVectorStore(client)
        self._seed(seed, 128)

        vs = _RealStoreFailingCreateCol(client)
        vs._has_bm25_slot = True  # what mem0 believes after its own create_col
        self._backend(vs)._recreate_qdrant_if_dims_changed(384)

        assert client.collection_exists("mem0"), "fallback must recreate the collection"
        assert _dense_dims(client) == 384
        sparse = _sparse_config(client)
        assert sparse and "bm25" in sparse, (
            "fallback dropped the bm25 sparse slot — collection is degraded"
        )
        assert sparse["bm25"].modifier == qdrant_models.Modifier.IDF
        assert vs._has_bm25_slot is True, (
            "vector store's bm25 flag must stay true so insert() keeps working"
        )

    def test_fallback_collection_accepts_hybrid_write_and_search(self):
        """Operate on the collection after the fallback recreate."""
        client = self._client()
        self._seed(_RealMem0StyleVectorStore(client), 128)

        vs = _RealStoreFailingCreateCol(client)
        self._backend(vs)._recreate_qdrant_if_dims_changed(384)

        _upsert_hybrid(client, "mem0", 384)
        hits = client.query_points(
            collection_name="mem0", query=[0.1] * 384, limit=5
        ).points
        assert len(hits) == 1
        assert hits[0].payload["user_id"] == "u1"

    def test_dense_only_collection_would_reject_hybrid_write(self):
        """Pins down *why* the fallback must keep the sparse slot: a dense-only
        collection rejects the very write mem0's insert path performs."""
        from qdrant_client.models import Distance, VectorParams

        client = self._client()
        client.create_collection(
            collection_name="dense_only",
            vectors_config=VectorParams(size=384, distance=Distance.COSINE),
        )
        with pytest.raises(Exception):
            _upsert_hybrid(client, "dense_only", 384)

    # --- no-op paths, against the real client ------------------------------

    def test_matching_dims_leaves_collection_untouched(self):
        client = self._client()
        vs = _RealMem0StyleVectorStore(client)
        self._seed(vs, 384)
        _upsert_hybrid(client, "mem0", 384)

        self._backend(vs)._recreate_qdrant_if_dims_changed(384)

        assert client.count("mem0").count == 1, (
            "matching dims must not drop existing points"
        )
        assert "bm25" in _sparse_config(client)

    def test_missing_create_col_leaves_collection_intact(self):
        """Without create_col we cannot honour the contract, so we must not
        delete — the real collection and its data stay put."""
        client = self._client()
        seed = _RealMem0StyleVectorStore(client)
        self._seed(seed, 128)
        _upsert_hybrid(client, "mem0", 128)

        class _NoCreateCol:
            def __init__(self, c):
                self.client = c
                self.collection_name = "mem0"
                self.on_disk = False

        self._backend(_NoCreateCol(client))._recreate_qdrant_if_dims_changed(384)

        assert client.collection_exists("mem0")
        assert _dense_dims(client) == 128, "collection must be left as-is"
        assert client.count("mem0").count == 1


class TestOSSBackendConstructorNoExtraClient:
    """Constructor-level: verify __init__ does NOT create a separate QdrantClient."""

    def test_init_does_not_create_extra_qdrant_client(self, monkeypatch):
        """When dims mismatch, the collection is recreated via Memory's
        vector_store, not via a temporary QdrantClient."""
        import sys
        import types

        # Track QdrantClient constructions
        qdrant_instances = []
        class QdrantClient:
            def __init__(self, **kwargs):
                qdrant_instances.append(kwargs)
            def collection_exists(self, name):
                return True
            def get_collection(self, name):
                return _FakeCollectionInfo(128)  # Mismatch!
            def delete_collection(self, name):
                pass
            def create_collection(self, **kwargs):
                pass
            def close(self):
                pass

        qdrant_client_module = types.ModuleType("qdrant_client")
        qdrant_client_module.QdrantClient = QdrantClient

        class FakeMemoryFromConfig:
            collection_name = "mem0"
            vector_store = _FakeVectorStore(_FakeQdrantClient(existing_dims=128))

            @staticmethod
            def from_config(config):
                m = FakeMemoryFromConfig()
                # Set the vector_store properly
                vs = _FakeVectorStore(_FakeQdrantClient(existing_dims=128))
                vs.on_disk = config.get("vector_store", {}).get("config", {}).get("on_disk", False)
                m.vector_store = vs
                m.collection_name = config.get("vector_store", {}).get("config", {}).get("collection_name", "mem0")
                return m

        mem0_module = types.ModuleType("mem0")
        mem0_module.Memory = FakeMemoryFromConfig

        # Also stub qdrant_client in sys.modules so OSSBackend won't try real import
        monkeypatch.setitem(sys.modules, "qdrant_client", qdrant_client_module)
        monkeypatch.setitem(sys.modules, "mem0", mem0_module)

        raw = {
            "llm": {
                "provider": "openai",
                "config": {"model": "gpt-4o-mini"},
            },
            "embedder": {
                "provider": "openai",
                "config": {"model": "text-embedding-3-small", "embedding_dims": 384},
            },
            "vector_store": {"provider": "qdrant", "config": {"path": "/tmp/test_qdrant"}},
        }

        backend = OSSBackend(raw)

        # Should have used the Memory's QdrantClient, not created a new one.
        assert len(qdrant_instances) == 0, (
            f"No QdrantClient should be created during __init__. "
            f"Got {len(qdrant_instances)}: {qdrant_instances}"
        )

        # Verify the vector store's collection was recreated on the dim mismatch.
        assert hasattr(backend._memory, "vector_store")
        assert backend._memory.vector_store.client.deleted


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
