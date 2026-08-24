"""Unit tests for the on-disk MCP schema cache (tools/mcp_schema_cache.py).

The module landed in #56832's extraction without its tests; these cover the
fingerprint keying, read/write round-trip, and invalidation behavior.
"""

import tools.mcp_schema_cache as msc


class TestConfigFingerprint:
    def test_stable_for_same_config(self):
        cfg = {"command": "npx", "args": ["-y", "@playwright/mcp"]}
        assert msc.config_fingerprint(cfg) == msc.config_fingerprint(dict(cfg))

    def test_changes_when_connection_config_changes(self):
        base = {"command": "npx", "args": ["-y", "@playwright/mcp"]}
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "args": ["-y", "@playwright/mcp", "--headless"]}
        )
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "command": "uvx"}
        )
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "tools": {"include": ["a"]}}
        )

    def test_ignores_non_connection_keys(self):
        base = {"command": "npx", "args": []}
        assert msc.config_fingerprint(base) == msc.config_fingerprint(
            {**base, "timeout": 5, "enabled": True, "lazy": True}
        )


class TestCacheRoundTrip:
    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(msc, "_cache_path", lambda: tmp_path / "cache.json")

    def test_write_then_read_with_matching_fingerprint(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        tools = [{"name": "t1", "description": "d", "inputSchema": {"type": "object"}}]
        msc.write_cache_entry("srv", "fp1", tools=tools, utility_tools=[])
        entry = msc.get_cached_entry("srv", "fp1")
        assert entry is not None
        assert msc.tools_from_cache_entry(entry) == tools
        assert msc.utility_tools_from_cache_entry(entry) == []
        assert msc.has_cached_entry("srv", "fp1")

    def test_fingerprint_mismatch_returns_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry("srv", "fp1", tools=[], utility_tools=[])
        assert msc.get_cached_entry("srv", "OTHER") is None
        assert not msc.has_cached_entry("srv", "OTHER")

    def test_missing_server_returns_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        assert msc.get_cached_entry("nope", "fp") is None

    def test_clear_cache_entry(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry("srv", "fp1", tools=[], utility_tools=[])
        msc.clear_cache_entry("srv")
        assert msc.get_cached_entry("srv", "fp1") is None

    def test_corrupt_cache_file_is_tolerated(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        (tmp_path / "cache.json").write_text("{not json", encoding="utf-8")
        assert msc.get_cached_entry("srv", "fp") is None
        # And writes recover the file.
        msc.write_cache_entry("srv", "fp", tools=[], utility_tools=[])
        assert msc.has_cached_entry("srv", "fp")

    def test_malformed_entry_shapes_are_tolerated(self):
        assert msc.tools_from_cache_entry({"tools": "nope"}) == []
        assert msc.utility_tools_from_cache_entry({}) == []


class TestCacheFileLocation:
    def test_cache_lives_under_hermes_home_cache_dir_with_0600(
        self, monkeypatch, tmp_path
    ):
        # Real path (no _cache_path monkeypatch): HERMES_HOME/cache/…, 0o600,
        # matching the discovery-cache precedent in tools/registry.py.
        import hermes_constants

        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        path = msc._cache_path()
        assert path == tmp_path / "cache" / "mcp_schema_cache.json"
        msc.write_cache_entry("srv", "fp", tools=[], utility_tools=[])
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600


class TestWriteReceipt:
    def test_identical_payload_rewrites_validation_receipt(self, monkeypatch, tmp_path):
        monkeypatch.setattr(msc, "_cache_path", lambda: tmp_path / "cache.json")
        saves = []
        real_save = msc._save_all

        def _counting_save(data):
            saves.append(1)
            real_save(data)

        monkeypatch.setattr(msc, "_save_all", _counting_save)
        tools = [{"name": "t1", "description": "d", "inputSchema": {}}]
        msc.write_cache_entry("srv", "fp1", tools=tools, utility_tools=[])
        assert len(saves) == 1
        msc.write_cache_entry("srv", "fp1", tools=list(tools), utility_tools=[])
        assert len(saves) == 2
        # Changed payload → rewrite.
        msc.write_cache_entry("srv", "fp2", tools=tools, utility_tools=[])
        assert len(saves) == 3


class TestSecurityContextIdentity:
    def test_partitions_protocol_headers_environment_auth_and_tls(self):
        base = {"url": "https://mcp.example.test/rpc"}
        variants = [
            {"protocol": "legacy"},
            {"headers": {"Authorization": "Bearer one"}},
            {"env": {"MCP_TENANT": "one"}},
            {"auth": "oauth", "oauth": {"issuer": "https://issuer-one.test"}},
            {"ssl_verify": False},
            {"client_cert": "cert-one.pem", "client_key": "key-one.pem"},
        ]
        baseline = msc.config_fingerprint(base)
        assert all(
            msc.config_fingerprint({**base, **variant}) != baseline
            for variant in variants
        )

    def test_profile_identity_header_partitions_active_principal(self, monkeypatch):
        import hermes_cli.profiles as profiles

        config = {
            "url": "https://mcp.example.test/rpc",
            "identity_header": {
                "name": "X-User-Id",
                "value_from": "profile",
            },
        }
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alice")
        alice = msc.config_fingerprint(config)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "bob")
        assert msc.config_fingerprint(config) != alice

    def test_epoch_partitions_all_entries(self, monkeypatch):
        config = {"command": "mcp-server"}
        current = msc.config_fingerprint(config)
        monkeypatch.setattr(msc, "CACHE_SCHEMA_EPOCH", 999)
        assert msc.config_fingerprint(config) != current

    def test_oauth_credential_rotation_changes_partition(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config = {
            "url": "https://mcp.example.test/rpc",
            "auth": "oauth",
        }
        token_dir = tmp_path / "mcp-tokens"
        token_dir.mkdir()
        token_path = token_dir / "srv.json"

        missing = msc.config_fingerprint(config, server_name="srv")
        token_path.write_text('{"access_token":"principal-a"}', encoding="utf-8")
        principal_a = msc.config_fingerprint(config, server_name="srv")
        token_path.write_text('{"access_token":"principal-b"}', encoding="utf-8")
        principal_b = msc.config_fingerprint(config, server_name="srv")

        assert len({missing, principal_a, principal_b}) == 3

    def test_tls_file_rotation_changes_partition(self, tmp_path):
        ca_bundle = tmp_path / "ca.pem"
        ca_bundle.write_text("first trust anchor", encoding="utf-8")
        config = {
            "url": "https://mcp.example.test/rpc",
            "ssl_verify": str(ca_bundle),
        }
        first = msc.config_fingerprint(config)
        ca_bundle.write_text("replacement trust anchor material", encoding="utf-8")
        assert msc.config_fingerprint(config) != first

    def test_cache_file_never_contains_plaintext_identity_values(self, monkeypatch, tmp_path):
        monkeypatch.setattr(msc, "_cache_path", lambda: tmp_path / "cache.json")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        secret = "cache-identity-secret"
        oauth_secret = "oauth-cache-identity-secret"
        token_dir = tmp_path / "mcp-tokens"
        token_dir.mkdir()
        (token_dir / "srv.json").write_text(
            f'{{"access_token":"{oauth_secret}"}}',
            encoding="utf-8",
        )
        config = {
            "url": "https://mcp.example.test/rpc",
            "auth": "oauth",
            "headers": {"Authorization": f"Bearer {secret}"},
            "env": {"MCP_SECRET": secret},
        }
        fingerprint = msc.config_fingerprint(config, server_name="srv")
        msc.write_cache_entry(
            "srv",
            fingerprint,
            config_digest=msc.config_digest(config),
            protocol_era="legacy",
            tools=[],
        )
        payload = (tmp_path / "cache.json").read_text(encoding="utf-8")
        assert secret not in payload
        assert oauth_secret not in payload
        assert f"Bearer {secret}" not in payload


class TestProtocolEraFreshness:
    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(msc, "_cache_path", lambda: tmp_path / "cache.json")

    def test_modern_zero_ttl_is_immediately_stale(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry(
            "srv",
            "fp",
            protocol_era="modern",
            tools=[{"name": "modern"}],
            ttl_ms=0,
        )
        assert msc.get_cached_entry("srv", "fp", protocol_era="modern") is None

    def test_modern_missing_ttl_fails_closed(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry(
            "srv",
            "fp",
            protocol_era="modern",
            tools=[{"name": "modern"}],
        )
        assert msc.get_cached_entry("srv", "fp", protocol_era="modern") is None

    def test_legacy_hintlessness_remains_usable(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry(
            "srv",
            "fp",
            protocol_era="legacy",
            tools=[{"name": "legacy"}],
        )
        entry = msc.get_cached_entry("srv", "fp", protocol_era="legacy")
        assert entry is not None
        assert "ttl_ms" not in entry

    def test_era_is_a_destructive_partition_boundary(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry(
            "srv",
            "fp",
            protocol_era="modern",
            tools=[{"name": "modern"}],
            ttl_ms=60_000,
        )
        msc.write_cache_entry(
            "srv",
            "fp",
            protocol_era="legacy",
            tools=[{"name": "legacy"}],
        )
        modern = msc.get_cached_entry("srv", "fp", protocol_era="modern")
        legacy = msc.get_cached_entry("srv", "fp", protocol_era="legacy")
        assert modern is not None
        assert legacy is not None
        assert modern["tools"][0]["name"] == "modern"
        assert legacy["tools"][0]["name"] == "legacy"
        assert len(msc._load_all()) == 2

    def test_config_digest_mismatch_is_a_miss(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry(
            "srv",
            "fp",
            config_digest="digest-a",
            protocol_era="legacy",
            tools=[],
        )
        assert (
            msc.get_cached_entry(
                "srv",
                "fp",
                config_digest="digest-b",
                protocol_era="legacy",
            )
            is None
        )

    def test_auto_hintless_legacy_receipt_expires(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        now = 1000.0
        monkeypatch.setattr(msc.time, "time", lambda: now)
        msc.write_cache_entry(
            "srv",
            "fp",
            protocol_era="legacy",
            tools=[{"name": "legacy"}],
        )
        assert msc.get_cached_entry("srv", "fp") is not None

        now += msc.MAX_TTL_MS / 1000.0 + 0.001
        assert msc.get_cached_entry("srv", "fp") is None

    def test_auto_selects_the_newest_valid_era_receipt(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        now = 2000.0
        monkeypatch.setattr(msc.time, "time", lambda: now)
        msc.write_cache_entry(
            "srv",
            "fp",
            protocol_era="modern",
            tools=[{"name": "modern"}],
            ttl_ms=60_000,
        )
        now += 1.0
        msc.write_cache_entry(
            "srv",
            "fp",
            protocol_era="legacy",
            tools=[{"name": "legacy"}],
        )

        entry = msc.get_cached_entry("srv", "fp")
        assert entry is not None
        assert entry["protocol_era"] == "legacy"

    def test_explicit_legacy_hintlessness_survives_the_auto_bound(
        self, monkeypatch, tmp_path
    ):
        self._isolate(monkeypatch, tmp_path)
        now = 3000.0
        monkeypatch.setattr(msc.time, "time", lambda: now)
        msc.write_cache_entry(
            "srv",
            "fp",
            protocol_era="legacy",
            tools=[{"name": "legacy"}],
        )

        now += msc.MAX_TTL_MS / 1000.0 + 10.0
        entry = msc.get_cached_entry("srv", "fp", protocol_era="legacy")
        assert entry is not None
