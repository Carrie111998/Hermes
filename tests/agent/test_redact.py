"""Tests for agent.redact -- secret masking in logs and output."""

import json
import logging
import re

import pytest

from agent.redact import (
    _mask_token,
    redact_cdp_url,
    redact_sensitive_text,
    RedactingFormatter,
)


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    """Ensure HERMES_REDACT_SECRETS is not disabled by prior test imports."""
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    # Also patch the module-level snapshot so it reflects the cleared env var
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


class TestKnownPrefixes:




    def test_gitlab_token_prefixes(self):
        """GitLab token families redact via their literal prefixes.

        Ported from openclaw/openclaw#112954; follow-up invited in #4541.
        """
        tokens = [
            # NOTE: every token is prefix + suffix CONCATENATION so no
            # contiguous token literal exists in this file — GitHub push
            # protection blocks realistic GitLab-token-shaped literals.
            "glpat-" + "Zx9AbCdEfGhIjKlMnOpQ",       # personal access token
            "gloas-" + "a" * 64,                     # OAuth application secret
            "gldt-" + "AbCdEfGhIjKlMnOpQrSt",        # deploy token
            "glrt-" + "t1_AbCdEfGhIjKlMnOpQrSt",     # runner auth token
            "glrt-" + "A" * 27 + ".01." + "a" * 9,   # routable (dotted) runner token
            "glrtr-" + "B" * 27 + ".01." + "b" * 9,  # routable runner registration
            "glcbt-" + "a1B2_AbCdEfGhIjKlMnOpQ",     # CI/CD job token
            "glptt-" + "c" * 40,                     # pipeline trigger token
            "glft-" + "AbCdEfGhIjKlMnOp",            # feed token
            "glimt-" + "AbCdEfGhIjKlMnOpQrStUvWxY",  # incoming mail token
            "glagent-" + "d" * 50,                   # agent (KAS) token
            "glsoat-" + "AbCdEfGhIjKlMnOpQrSt",      # service-account token
            "glffct-" + "AbCdEfGhIjKlMnOpQrSt",      # feature-flags client token
            "glwt-" + "AbCdEfGhIjKlMnOpQrSt",        # workspace token
            "GR1348941" + "E" * 20,                  # legacy runner registration
        ]
        for token in tokens:
            result = redact_sensitive_text(f"leaked {token} in output")
            secret_body = token.split("-", 1)[-1] if "-" in token else token[9:]
            assert secret_body not in result, f"{token!r} survived redaction: {result!r}"

    def test_gitlab_prefix_requires_word_boundary_and_length(self):
        """Prose and embedded identifiers must not false-positive."""
        for benign in [
            "the glossary explains gitlab tokens",   # no prefix at all
            "glpat-short",                            # suffix under 10 chars
            "myglpat-AbCdEfGhIjKlMnOpQrSt",           # embedded — lookbehind blocks
        ]:
            assert redact_sensitive_text(benign) == benign

    def test_slack_token(self):
        token = "xoxb-" + "0" * 12 + "-" + "a" * 14
        result = redact_sensitive_text(token)
        assert "a" * 14 not in result





    def test_fireworks_keys(self):
        samples = [
            "fw-" + "A" * 40,
            "fw_" + "B" * 40,
            "fpk_" + "C" * 40,
        ]

        for token in samples:
            result = redact_sensitive_text(f"provider error {token}")
            assert token not in result
            assert "..." in result

    def test_short_fireworks_like_words_unchanged(self):
        text = "fw-tooshort fw_tooshort fpk_tooshort"
        assert redact_sensitive_text(text) == text




class TestEnvAssignments:
    def test_export_api_key(self):
        text = "export OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012"
        result = redact_sensitive_text(text)
        assert "OPENAI_API_KEY=" in result
        assert "abc123def456" not in result


    def test_non_secret_env_unchanged(self):
        text = "HOME=/home/user"
        result = redact_sensitive_text(text)
        assert result == text






    def test_export_whitespace_preserved(self):
        # Regression: #4367 — whitespace before uppercase env var must be preserved
        text = "export SECRET_TOKEN=mypassword"
        result = redact_sensitive_text(text)
        assert result.startswith("export ")
        assert "SECRET_TOKEN=" in result
        assert "mypassword" not in result


class TestEnvLookupPreserved:
    """Programmatic env var lookups must not be corrupted (issue #2852)."""

    def test_os_getenv_single_quote_uppercase_key(self):
        text = "MY_API_KEY=os.getenv('OPENAI_API_KEY')"
        assert redact_sensitive_text(text, force=True) == text






    def test_real_env_value_still_redacted(self):
        text = "HOMEASSISTANT_TOKEN=eyJhbGciOiJIUzI1NiJ9.abc123.xyz"
        result = redact_sensitive_text(text, force=True)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result


    def test_multiline_prose_with_code_snippet(self):
        text = """Set it up like this:
    HA_TOKEN=os.getenv('HOMEASSISTANT_TOKEN')
    if not HA_TOKEN:
        raise ValueError('Missing credentials')"""
        result = redact_sensitive_text(text, force=True)
        assert "os.getenv('HOMEASSISTANT_TOKEN')" in result







class TestJsonFields:
    def test_json_api_key(self):
        text = '{"apiKey": "sk-proj-abc123def456ghi789jkl012"}'
        result = redact_sensitive_text(text)
        assert "abc123def456" not in result


    def test_json_non_secret_unchanged(self):
        text = '{"name": "John", "model": "gpt-4"}'
        result = redact_sensitive_text(text)
        assert result == text


class TestAuthHeaders:





    def test_authorization_prose_unchanged(self):
        # "authorization" without a colon-delimited value is plain prose.
        text = "the authorization model is fully open"
        assert redact_sensitive_text(text) == text

    def test_token_flush_against_double_quote_preserves_quote(self):
        # Regression for #43083: a token sitting flush against a closing
        # double quote must NOT pull that quote into the mask. Greedy \S+
        # used to eat it, turning value corruption into syntax corruption
        # (unterminated quote → shell EOF).
        text = 'curl -H "Authorization: Bearer sk-abcdef1234567890"'
        result = redact_sensitive_text(text)
        assert "sk-abcdef1234567890" not in result
        assert result.count('"') == 2, result  # both quotes survive
        assert result.endswith('"'), result



class TestApiKeyHeaders:
    def test_x_api_key_header_masked(self):
        text = "x-api-key: opaque-provider-key-1234567890"
        result = redact_sensitive_text(text)
        assert "x-api-key:" in result
        assert "opaque-provider-key" not in result

    def test_x_api_key_in_curl_command_masked(self):
        text = 'curl -H "x-api-key: sk-local-VERYsecret-999888" https://api.example.com'
        result = redact_sensitive_text(text)
        assert "VERYsecret" not in result
        assert "https://api.example.com" in result

    def test_api_key_header_masked(self):
        text = "api-key: anotherOpaqueSecret1234567"
        result = redact_sensitive_text(text)
        assert "anotherOpaqueSecret" not in result


class TestTelegramTokens:
    def test_bot_token(self):
        text = "bot123456789:ABCDEfghij-KLMNopqrst_UVWXyz12345"
        result = redact_sensitive_text(text)
        assert "ABCDEfghij" not in result
        assert "123456789:***" in result

    def test_raw_token(self):
        text = "12345678901:ABCDEfghijKLMNopqrstUVWXyz1234567890"
        result = redact_sensitive_text(text)
        assert "ABCDEfghij" not in result


class TestPassthrough:
    def test_empty_string(self):
        assert redact_sensitive_text("") == ""



    def test_non_string_input_dict_coerced_and_redacted(self):
        result = redact_sensitive_text({"token": "sk-proj-abc123def456ghi789jkl012"})
        assert "abc123def456" not in result





class TestRedactingFormatter:
    def test_formats_and_redacts(self):
        formatter = RedactingFormatter("%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Key is sk-proj-abc123def456ghi789jkl012",
            args=(),
            exc_info=None,
        )
        result = formatter.format(record)
        assert "abc123def456" not in result
        assert "sk-pro" in result


class TestPrintenvSimulation:
    """Simulate what happens when the agent runs `env` or `printenv`."""

    def test_full_env_dump(self):
        env_dump = """HOME=/home/user
PATH=/usr/local/bin:/usr/bin
OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012mno345
OPENROUTER_API_KEY=sk-or-v1-reallyLongSecretKeyValue12345678
FIRECRAWL_API_KEY=fc-shortkey123456789012
TELEGRAM_BOT_TOKEN=bot987654321:ABCDEfghij-KLMNopqrst_UVWXyz12345
SHELL=/bin/bash
USER=teknium"""
        result = redact_sensitive_text(env_dump)
        # Secrets should be masked
        assert "abc123def456" not in result
        assert "reallyLongSecretKey" not in result
        assert "ABCDEfghij" not in result
        # Non-secrets should survive
        assert "HOME=/home/user" in result
        assert "SHELL=/bin/bash" in result
        assert "USER=teknium" in result


class TestSecretCapturePayloadRedaction:
    def test_secret_value_field_redacted(self):
        text = '{"success": true, "secret_value": "sk-test-secret-1234567890"}'
        result = redact_sensitive_text(text)
        assert "sk-test-secret-1234567890" not in result



class TestElevenLabsTavilyExaKeys:
    """Regression tests for ElevenLabs (sk_), Tavily (tvly-), and Exa (exa_) keys."""

    def test_elevenlabs_key_redacted(self):
        text = "ELEVENLABS_API_KEY=sk_abc123def456ghi789jklmnopqrstu"
        result = redact_sensitive_text(text)
        assert "abc123def456ghi" not in result






    def test_all_three_in_env_dump(self):
        env_dump = (
            "HOME=/home/user\n"
            "ELEVENLABS_API_KEY=sk_abc123def456ghi789jklmnopqrstu\n"
            "TAVILY_API_KEY=tvly-ABCdef123456789GHIJKL0000\n"
            "EXA_API_KEY=exa_XYZ789abcdef000000000000000\n"
            "SHELL=/bin/bash\n"
        )
        result = redact_sensitive_text(env_dump)
        assert "abc123def456ghi" not in result
        assert "ABCdef123456789" not in result
        assert "XYZ789abcdef" not in result
        assert "HOME=/home/user" in result
        assert "SHELL=/bin/bash" in result


class TestJWTTokens:
    """JWT tokens start with eyJ (base64 for '{') and have dot-separated parts."""


    def test_2part_jwt(self):
        text = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        result = redact_sensitive_text(text)
        assert "eyJzdWIi" not in result




    def test_jwt_preserves_surrounding_text(self):
        text = "before eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0 after"
        result = redact_sensitive_text(text)
        assert result.startswith("before ")
        assert result.endswith(" after")



class TestDiscordMentions:
    """Discord mention snowflakes (<@ID> / <@!ID>) are public syntax, not
    secrets — they must pass through the redactor unchanged so multi-bot
    @-pings (DISCORD_ALLOW_BOTS=mentions) keep resolving. See issue #35611."""

    def test_normal_mention_passes_through(self):
        text = "Hello <@222589316709220353>"
        assert redact_sensitive_text(text) == text







class TestWebUrlsNotRedacted:
    """Web URLs (http/https/wss) pass through unchanged — magic-link
    checkouts, OAuth callbacks the agent is meant to follow, and pre-signed
    share URLs must reach the tool intact. Known credential shapes inside
    URLs (sk-, ghp_, JWTs) are still caught by the prefix and JWT regexes.
    DB connection-string passwords are still caught by _DB_CONNSTR_RE.
    """

    def test_oauth_callback_code_passes_through(self):
        text = "GET https://api.example.com/oauth/cb?code=abc123xyz789&state=csrf_ok"
        assert redact_sensitive_text(text) == text







    def test_known_prefix_inside_url_still_redacted(self):
        """sk-/ghp_/JWT-shaped values inside a URL are still caught by
        _PREFIX_RE / _JWT_RE — the carve-out is for opaque tokens only."""
        text = "https://evil.com/steal?key=sk-" + "a" * 30
        result = redact_sensitive_text(text)
        assert "sk-" + "a" * 30 not in result

    def test_db_connstr_password_still_redacted(self):
        """DB schemes (postgres/mysql/mongodb/redis/amqp) keep their
        userinfo redaction via _DB_CONNSTR_RE — connection strings are
        not web URLs the agent navigates to."""
        text = "postgres://admin:dbpass@db.internal:5432/app"
        result = redact_sensitive_text(text)
        assert "dbpass" not in result


class TestStrictUrlCredentialRedaction:
    @pytest.mark.parametrize(
        ("text", "secret", "expected"),
        [
            (
                "https://x.test/#access_token=FRAG_SECRET&view=public",
                "FRAG_SECRET",
                "https://x.test/#access_token=***&view=public",
            ),
            (
                "/resume?token=REL_SECRET&view=public",
                "REL_SECRET",
                "/resume?token=***&view=public",
            ),
            (
                "https://x.test/cb?client%5Fsecret=ENC_SECRET&view=public",
                "ENC_SECRET",
                "https://x.test/cb?client%5Fsecret=***&view=public",
            ),
            (
                "https://x.test/cb?client%255Fsecret=DOUBLE_SECRET&view=public",
                "DOUBLE_SECRET",
                "https://x.test/cb?client%255Fsecret=***&view=public",
            ),
            (
                "/resume?token=SEMICOLON_SECRET;view=public",
                "SEMICOLON_SECRET",
                "/resume?token=***;view=public",
            ),
            (
                "//user:NET_SECRET@x.test/path",
                "NET_SECRET",
                "//user:***@x.test/path",
            ),
        ],
    )
    def test_masks_all_url_reference_forms_only_when_opted_in(
        self, text, secret, expected
    ):
        assert redact_sensitive_text(text) == text

        result = redact_sensitive_text(text, redact_url_credentials=True)

        assert secret not in result
        assert result == expected

    def test_similarly_named_public_params_remain_unchanged(self):
        text = "/metrics?token_count=17&session_id=public"
        assert redact_sensitive_text(text, redact_url_credentials=True) == text


class TestBareTokenUserinfoRedaction:
    """Regression tests for #6396 — a bare credential in URL userinfo
    (``scheme://TOKEN@host``, no ``user:pass`` colon) is redacted. This is the
    git-remote-with-embedded-password shape. The colon form ``user:pass@`` and
    query-string tokens are deliberately left to pass through (#34029) so
    magic-link / OAuth round-trip skills keep working — see
    TestWebUrlsNotRedacted for those invariants.
    """

    def test_git_remote_bare_password_redacted(self):
        """Exact bug scenario: password in a git remote URL."""
        text = (
            "git remote set-url origin "
            "https://MYPASSWORDWASDISLAYEDHERE@github.com/unclehowell/FCUK.git"
        )
        result = redact_sensitive_text(text)
        assert "MYPASSWORDWASDISLAYEDHERE" not in result
        assert "@github.com" in result
        assert "unclehowell/FCUK.git" in result

    def test_ssh_bare_token_redacted(self):
        text = "ssh://longtoken1234567@gitlab.com/project.git"
        result = redact_sensitive_text(text)
        assert "longtoken1234567" not in result
        assert "@gitlab.com" in result

    def test_ftp_bare_token_redacted(self):
        text = "ftp://ftptoken123456@ftp.example.com/files"
        result = redact_sensitive_text(text)
        assert "ftptoken123456" not in result


    def test_user_pass_form_still_passes_through(self):
        """The ``user:pass@`` colon form must NOT be redacted (#34029)."""
        text = "URL: https://user:supersecretpw@host.example.com/path"
        assert redact_sensitive_text(text) == text

    def test_short_username_not_redacted(self):
        """Short userinfo (git, admin, deploy) below the 8-char floor passes."""
        for text in (
            "https://git@github.com/user/repo.git",
            "https://admin@example.com/x",
            "https://deploy@host.com/y",
        ):
            assert redact_sensitive_text(text) == text

    def test_email_in_path_not_redacted(self):
        """An ``@`` in a path/query is not userinfo — the token class stops at
        ``/``, so emails after the first slash are never treated as a credential."""
        for text in (
            "https://example.com/search?q=user@example.com",
            "https://example.com/users/john@doe.com/profile",
        ):
            assert redact_sensitive_text(text) == text




class TestFormBodyRedaction:
    """Form-urlencoded body redaction (k=v&k=v with no other text)."""

    def test_pure_form_body(self):
        text = "password=mysecret&username=bob&token=opaqueValue"
        result = redact_sensitive_text(text)
        assert "mysecret" not in result
        assert "opaqueValue" not in result
        assert "username=bob" in result


    def test_non_form_text_unchanged(self):
        """Sentences with `&` should NOT trigger form redaction."""
        text = "I have password=foo and other things"  # contains spaces
        result = redact_sensitive_text(text)
        # The space breaks the form regex; passthrough expected.
        assert "I have" in result

    def test_multiline_text_not_form(self):
        """Multi-line text is never treated as form body."""
        text = "first=1\nsecond=2"
        # Should pass through (still subject to other redactors)
        assert "first=1" in redact_sensitive_text(text)


class TestLowercaseDottedConfigKeys:
    """Issue #16413 — config-file passwords in lowercase/dotted/colon keys
    must be redacted. The uppercase _ENV_ASSIGN_RE missed these, leaking
    `spring.datasource.password=...` and `password: ...` from `cat`'d config
    files. Carve-outs: prose, code (#4367), and web URLs are left untouched.
    """







    def test_properties_file_dump(self):
        text = (
            "server.port=8080\n"
            "spring.datasource.username=admin\n"
            "spring.datasource.password=Sup3rS3cret!\n"
            "logging.level.root=INFO"
        )
        result = redact_sensitive_text(text)
        assert "Sup3rS3cret!" not in result
        assert "server.port=8080" in result  # non-secret keys preserved
        assert "username=admin" in result

    # --- carve-outs: must NOT redact ---

    def test_prose_mid_sentence_password_unchanged(self):
        # Not line-anchored, not dotted → conversational text, leave alone.
        text = "I have password=foo and other things"
        assert redact_sensitive_text(text) == text





class TestConfigKeyRedosResistance:
    """The dotted-key patterns must not backtrack exponentially (ReDoS).

    Before the possessive-quantifier rewrite, a non-matching run of ~40
    dotted segments took ~30ms and doubled every ~4 segments; 100 segments
    would effectively hang the redactor (it runs on every log line).
    """

    def test_long_dotted_run_completes_fast(self):
        import time

        # 100 dotted segments with no '=' — worst case for the old pattern.
        text = ".".join(["segment"] * 100) + " end"
        t0 = time.perf_counter()
        assert redact_sensitive_text(text) == text
        assert time.perf_counter() - t0 < 2.0

    def test_long_dotted_run_with_keyword_completes_fast(self):
        """Exercise _CFG_DOTTED_RE directly (bypasses the keyword pre-gate).

        The pre-gate skips the regex when no secret keyword is present, so
        test_long_dotted_run_completes_fast only guards the pre-gate.  This
        test includes a keyword but no '=' so the regex runs and must still
        complete quickly thanks to the possessive quantifiers.
        """
        import time

        text = ".".join(["segment"] * 100) + ".token end"
        t0 = time.perf_counter()
        assert redact_sensitive_text(text) == text
        assert time.perf_counter() - t0 < 2.0

    def test_long_dotted_secret_still_redacted(self):
        # Possessive quantifiers must not change matching behavior.
        text = ".".join(["seg"] * 50) + ".password=Sup3rS3cret!"
        result = redact_sensitive_text(text)
        assert "Sup3rS3cret!" not in result
        assert ".password=" in result

    def test_yaml_assign_redos_resistance(self):
        """_YAML_ASSIGN_RE must not backtrack excessively on long inputs."""
        import time

        # 100 lines of a long dotted key with a secret keyword but no
        # matching colon-value form — stresses the regex without matching.
        line = "a." * 50 + "token not_an_assignment"
        text = "\n".join([line] * 100)
        t0 = time.perf_counter()
        redact_sensitive_text(text)
        assert time.perf_counter() - t0 < 2.0

    def test_yaml_assign_secret_still_redacted(self):
        # Possessive quantifiers must not change YAML matching behavior.
        text = "spring.datasource.password: hunter2"
        result = redact_sensitive_text(text)
        assert "hunter2" not in result
        assert "password:" in result


class TestXaiToken:
    KEY = "xai-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstu"

    def test_bare_token_masked(self):
        result = redact_sensitive_text(f"using key {self.KEY}", force=True)
        assert self.KEY not in result
        assert "xai-AB" in result


    def test_too_short_not_masked(self):
        short = "xai-tooshort"
        result = redact_sensitive_text(f"text {short} here", force=True)
        assert short in result




class TestDbConnstrCodeOutput:
    """Regression tests for issue #33801 — _DB_CONNSTR_RE corrupting code output.

    Two distinct flaws, both confined to displayed tool OUTPUT (read_file /
    terminal / execute_code), never the on-disk content:

    1. The password group ``[^@]+`` was greedy across newlines, so on a
       multi-line block it scanned past the DSN line to the next stray ``@``
       (e.g. a Python ``@decorator``), replacing everything in between with
       ``***`` — dropping lines and concatenating the next one.
    2. An f-string DSN template (``f"postgresql://{user}:{pass}@{host}"``) is
       not a live credential, but was redacted anyway. Under ``code_file=True``
       a pure ``{...}`` brace password is now preserved.
    """

    MULTILINE = (
        '            return f"postgresql://{auth}@{self.pg_host}:'
        '{self.pg_port}/{self.pg_database}"\n'
        "\n"
        '    @model_validator(mode="after")\n'
        '    def _validate_critical_settings(self) -> "Settings":'
    )





    def test_literal_connstr_still_redacted_with_code_file(self):
        """A real password in a literal DSN is still masked under code_file."""
        text = "postgresql://admin:realpassword@db.internal:5432/app"
        result = redact_sensitive_text(text, code_file=True, force=True)
        assert "realpassword" not in result
        assert "***" in result

    def test_literal_connstr_redacted_all_schemes(self):
        for scheme, secret in [
            ("postgres", "pgsecret1234"),
            ("mysql", "mysqlsecret99"),
            ("redis", "redissecret77"),
            ("mongodb+srv", "mongosecret55"),
            ("amqp", "amqpsecret33"),
        ]:
            text = f"{scheme}://user:{secret}@host:1234/db"
            result = redact_sensitive_text(text, code_file=True, force=True)
            assert secret not in result, scheme

    def test_literal_connstr_in_log_line_redacted(self):
        text = "connected via postgres://user:s3cr3tpw@host:5432/db ok"
        result = redact_sensitive_text(text, force=True)
        assert "s3cr3tpw" not in result


class TestTerminalOutputRedaction:
    """is_env_dump_command + redact_terminal_output — issue #43025.

    Terminal/process stdout must be redacted on every surface (foreground
    `terminal` AND background `process(poll/log/wait)`). Env-dump commands get
    the ENV-assignment pass so opaque tokens (no vendor prefix) are masked;
    other commands stay on the code_file path to avoid false positives.
    """

    def test_is_env_dump_command_detection(self):
        from agent.redact import is_env_dump_command
        assert is_env_dump_command("printenv")
        assert is_env_dump_command("env")
        assert is_env_dump_command("env | grep API")
        assert is_env_dump_command("set")
        assert is_env_dump_command("export")
        assert is_env_dump_command("declare -x")
        assert is_env_dump_command("cat /tmp/x && printenv")
        assert not is_env_dump_command("python app.py")
        assert not is_env_dump_command("cat config.py")
        assert not is_env_dump_command("printf 'TOKEN=x'")
        assert not is_env_dump_command("")
        assert not is_env_dump_command(None)




    def test_disabled_passes_through(self, monkeypatch):
        from agent.redact import redact_terminal_output
        monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
        out = "CUSTOM_TOKEN=zzzopaque1234567890abcdef"
        red = redact_terminal_output(out, "printenv")
        assert "zzzopaque1234567890abcdef" in red


class TestFileReadNonReusableRedaction:
    """#35519: prefix-matched credentials in FILE CONTENT (read_file /
    search_files / cat) must be redacted to a NON-REUSABLE sentinel — not a
    head/tail mask that looks like a real-but-truncated key and gets written
    back to config (corrupting the credential -> 401)."""

    GHP = "ghp_S1abcdefghijklmnopqrstuvwxyz0Pn2T"  # realistic GitHub PAT shape
    SK = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


    def test_file_read_does_not_leak_secret_body(self):
        """Crucial: file_read must NOT expose the real key (no un-redact)."""
        out = redact_sensitive_text(f"token: {self.GHP}", force=True, file_read=True)
        # No run of the secret body survives.
        assert "S1abcdefghij" not in out
        assert self.GHP not in out
        assert "Pn2T" not in out  # not even the tail (the old mask kept it)

    def test_file_read_sentinel_is_not_a_plausible_key(self):
        """The sentinel can't be mistaken for / written back as a usable key:
        the old mask was a 13-char `ghp_S1...Pn2T` that broke GitHub auth when
        an agent re-saved it. The sentinel is syntactically invalid as a token
        (contains « » … and ':'), so it can't round-trip into a dead key."""
        out = redact_sensitive_text(f"GITHUB_PERSONAL_ACCESS_TOKEN: {self.GHP}",
                                    force=True, file_read=True)
        masked = out.split(": ", 1)[1].strip()
        # Not a bare token: contains the sentinel delimiters.
        assert masked.startswith("«") and masked.endswith("»")
        assert "…" in masked





class TestFireworksToken:
    KEY = "fw_" + "A" * 40

    def test_bare_token_masked(self):
        result = redact_sensitive_text(f"fireworks error: key {self.KEY}", force=True)
        assert self.KEY not in result
        assert "fw_AA" in result


    def test_too_short_not_masked(self):
        short = "fw_tooshort"
        result = redact_sensitive_text(f"text {short} here", force=True)
        assert short in result



class TestRedactCdpUrl:
    """redact_cdp_url() is the single chokepoint for CDP endpoint log redaction.

    Unlike the global pass (which deliberately lets web-URL query params and
    userinfo through for OAuth/magic-link workflows), CDP endpoint credentials
    are pure secrets and must always be masked. Both the browser tool's
    session/discovery logs and the supervisor's attach-timeout error route
    through this helper.
    """


    def test_masks_multiple_query_credentials(self):
        url = "wss://provider.example/session?token=aaa-secret&apikey=bbb-secret"
        out = redact_cdp_url(url)
        assert "aaa-secret" not in out
        assert "bbb-secret" not in out




    def test_none_returns_empty(self):
        assert redact_cdp_url(None) == ""


class TestKeywordWordBoundary:
    """Ported from nearai/ironclaw#6129 — a secret keyword embedded inside a
    larger prose word (``Secretary`` ⊃ ``secret``, ``tokenizer`` ⊃ ``token``,
    ``authored`` ⊃ ``auth``) must NOT trigger the lowercase/dotted/YAML config
    passes. Real key shapes (separators, camelCase, acronyms, plurals, common
    concatenated compounds, all-caps env style) must keep redacting.
    """

    # ── prose words embedding a keyword are preserved ──────────────────

    def test_secretary_yaml_value_preserved(self):
        text = "Secretary: JanetYellen1234567890"
        assert redact_sensitive_text(text) == text








    # ── real key shapes still redact ────────────────────────────────────

    def test_separator_keys_still_redacted(self):
        for text in (
            "client_secret: abc123def456ghi789jkl",
            "auth_token: xyz789xyz789xyz789xyz",
            "my_secret: topvalue123456789012345",
            "db.password=hunter2verylongpassword",
        ):
            result = redact_sensitive_text(text)
            assert result != text, text

    def test_camelcase_keys_still_redacted(self):
        for text in (
            "clientSecret: abc123def456ghi789jkl",
            "secretKey: abc123def456ghi789jklmno",
            "APIToken: abc123def456ghi789jklmn",
        ):
            result = redact_sensitive_text(text)
            assert result != text, text


    def test_plural_keys_still_redacted(self):
        text = "secrets: hunter2hunter2hunter2hh"
        result = redact_sensitive_text(text)
        assert "hunter2hunter2hunter2hh" not in result


class TestSerializedJsonStaysParseable:
    """Redacting a serialized JSON payload must not corrupt the JSON.

    Same defect class as #43083 (``_AUTH_HEADER_RE``'s greedy ``\\S+`` eating a
    closing quote), at the sibling patterns that fix did not touch: the
    KEY=VALUE passes and ``_SECRET_HEADER_RE``. Each captures an unquoted value
    bounded by "not whitespace", which also admits the quote that closes the
    enclosing JSON string. Masking a value under the 18-char floor to a bare
    ``***`` then deleted that quote, and every in-tree consumer that reparses
    the redacted text degrades differently:

    * ``agent_runtime_helpers.dump_api_request_debug`` — swallows the
      ``JSONDecodeError``, so no request dump lands for exactly the API
      failures the dump exists to explain.
    * ``agent.trace_upload._tool_calls_to_blocks`` — raises
      ``TraceRedactionError`` and refuses the whole upload.
    * ``tools.kanban_tools`` — ``except json.JSONDecodeError: pass`` leaves the
      *original unredacted* dict bound, so the secret is persisted verbatim.
      That one is a redaction bypass, covered by
      ``TestRedactThenReparseConsumers`` below.

    Asserted as an invariant — redact(serialize(x)) parses — rather than as
    fixed output, so any future pattern that eats a delimiter fails here.
    """

    # (content, the secret that must not survive). Short values (< the 18-char
    # mask floor) mask to a bare "***" and so have no tail that could
    # accidentally carry the delimiter — that is what made the corruption
    # visible; long values pin the other branch.
    #
    # ``api_key: ab`` is deliberately absent: _YAML_ASSIGN_RE is line-anchored
    # and a serialized-JSON line starts with its own key, so that pass does not
    # fire inside JSON by design. Listing it would assert a behaviour the
    # redactor never promised.
    SECRET_BEARING_CONTENT = [
        ("export MY_TOKEN=xyz", "xyz"),
        ("export MY_TOKEN=abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
        ("DB_PASSWORD=pw1", "pw1"),
        ("spring.datasource.password=ab", "ab"),
        ("spring.datasource.password=averylongpassword123", "averylongpassword123"),
        ("value at end of string MY_TOKEN=ab", "ab"),
        ("A_TOKEN=q1 B_PASSWORD=q2", "q2"),
        ('MY_TOKEN="quotedshortvalue"', "quotedshortvalue"),
        ("MY_TOKEN='sq'", "sq"),
        # _SECRET_HEADER_RE — the sibling pattern with the same bare (\S+)
        # value class. Short values mask to "***" and so exposed the same
        # delimiter deletion; a curl command carrying one is an ordinary thing
        # to find inside a request dump or a tool-call argument.
        ("x-api-key: abc123", "abc123"),
        ("x-auth-token: sh0rt", "sh0rt"),
        ("api-key: pw1", "pw1"),
        ("x-api-key: averylongheadervalue123456", "averylongheadervalue123456"),
    ]

    @pytest.mark.parametrize("content,secret", SECRET_BEARING_CONTENT)
    def test_redacted_json_still_parses(self, content, secret):
        payload = {"messages": [{"role": "user", "content": content}]}
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)

        redacted = redact_sensitive_text(serialized, force=True)

        # The assertion under test: the payload survives the round trip.
        reparsed = json.loads(redacted)
        assert list(reparsed) == ["messages"]
        assert reparsed["messages"][0]["role"] == "user"

    @pytest.mark.parametrize("content,secret", SECRET_BEARING_CONTENT)
    def test_secret_still_masked_inside_json(self, content, secret):
        """Preserving the delimiter must not preserve the secret with it."""
        payload = {"messages": [{"role": "user", "content": content}]}
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)

        redacted = redact_sensitive_text(serialized, force=True)

        # Assert on the *parsed* value, not the raw text, so a pass can never
        # come from the JSON having been mangled into something unparseable.
        content_out = json.loads(redacted)["messages"][0]["content"]
        assert f"={secret}" not in content_out
        assert f"='{secret}'" not in content_out
        assert f'="{secret}"' not in content_out

    def test_mask_tail_is_the_value_not_the_delimiter(self):
        """A long value's preserved tail comes from the secret, not the quote.

        Before the fix the captured value included the closing quote, so
        ``mask_secret``'s 4-char tail was ``lue"`` — the JSON happened to stay
        parseable, but the displayed tail was wrong and one real character of
        the value was hidden behind the delimiter.
        """
        serialized = json.dumps(
            {"c": "MY_TOKEN=averylongsecretvalue"}, ensure_ascii=False, indent=2
        )

        value = json.loads(redact_sensitive_text(serialized, force=True))["c"]

        assert value.endswith("alue")
        assert not value.endswith('lue"')

    def test_compact_json_closing_brace_is_preserved(self):
        """Un-indented JSON: the value absorbs the closing brace too.

        ``json.dumps`` without ``indent=`` leaves no whitespace after the value,
        so the "not whitespace" value class captures ``xyz"}`` — the delimiter
        run, not just one quote. ``plugins/platforms/google_chat/adapter.py``
        redacts an un-indented dump, so this shape is a real call path.
        """
        serialized = json.dumps({"c": "MY_TOKEN=xyz"}, ensure_ascii=False)

        redacted = redact_sensitive_text(serialized, force=True)

        assert json.loads(redacted)["c"] == "MY_TOKEN=***"

    def test_plain_shell_text_unquoted_value_unchanged(self):
        """Outside a container there is no delimiter to split — mask as before."""
        result = redact_sensitive_text("PGPASSWORD=hunter2 psql -h db", force=True)
        assert result == "PGPASSWORD=*** psql -h db"

    def test_apostrophe_in_value_is_not_treated_as_delimiter(self):
        """Only a *trailing* quote is split, and the secret is still masked."""
        result = redact_sensitive_text("MY_TOKEN=ab'cd", force=True)
        assert "ab'cd" not in result
        assert "MY_TOKEN=" in result

    def test_interior_quote_value_is_fully_masked(self):
        """A value containing a quote must not survive past the quote.

        This is the property that rules out the other candidate fix — copying
        #43083's value-class exclusion (``[^\\s\"']+``) onto these patterns.
        That would stop the capture *at* the interior quote and re-emit the
        rest verbatim: ``PGPASSWORD=p'ass`` -> ``PGPASSWORD=***'ass``. Bearer
        tokens never contain quotes (#43083's stated rationale), but a
        password may, so the exclusion is safe there and lossy here.
        """
        for text, leaked_tail in [
            ("PGPASSWORD=p'ass psql", "ass"),
            ("MY_SECRET=he\"llo", "llo"),
            ("DB_PASSWORD=x'y'z", "y'z"),
            ("x-api-key: ab'cd", "cd"),
        ]:
            result = redact_sensitive_text(text, force=True)
            assert leaked_tail not in result, (text, result)

    def test_quoted_header_value_keeps_both_quotes(self):
        """A header value wrapped in its own quotes stays balanced.

        ``x-api-key: "abc"`` must mask to ``x-api-key: "***"``. Splitting only
        the trailing quote would emit ``***"`` — parseable, but visibly
        unbalanced, and the same shape #43083 asserts for Authorization
        (``result.count('"') == 2``).
        """
        for quote in ('"', "'"):
            text = f"x-api-key: {quote}quoted{quote}"
            result = redact_sensitive_text(text, force=True)
            assert "quoted" not in result
            assert result.count(quote) == 2, result

    def test_shell_command_quote_survives(self):
        """The non-JSON half of the same defect: a quoted curl header.

        Direct parallel to #43083's ``test_token_flush_against_double_quote``,
        which pins this contract for ``Authorization:``. Without it the closing
        quote vanishes and the command no longer parses (shell EOF).
        """
        for quote in ('"', "'"):
            text = f"curl -H {quote}x-api-key: abc123{quote} https://api.example.com"
            result = redact_sensitive_text(text, force=True)
            assert "abc123" not in result
            assert result.count(quote) == 2, result
            assert "https://api.example.com" in result

    @pytest.mark.parametrize(
        "template,secret",
        [
            ("sh -c {q}export MY_TOKEN={s}{q}", "xyz"),
            ("docker run -e {q}DB_PASSWORD={s}{q}", "pw1"),
            ("ssh host {q}PGPASSWORD={s} psql{q}", "pw1"),
            ("curl -H {q}x-api-key: {s}{q}", "abc123"),
        ],
    )
    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_quoted_shell_command_stays_balanced(self, template, secret, quote):
        """A quote opened *before* the key must still be closed after masking.

        The most common real shape of this defect, and it needs no JSON at all:
        the shell quote wrapping the whole command is what the greedy value
        class eats. ``sh -c 'export MY_TOKEN=xyz'`` masked to
        ``sh -c 'export MY_TOKEN=***`` — an unterminated quote, i.e. shell EOF.
        Asserted as balanced quote counts, the same contract #43083 pinned for
        ``Authorization:``.
        """
        text = template.format(q=quote, s=secret)

        result = redact_sensitive_text(text, force=True)

        assert secret not in result, result
        assert result.count(quote) % 2 == 0, result
        assert result.endswith(quote), result


class TestDelimiterSplitNeverDisclosesSecretBytes:
    """The delimiter split must not become a channel for secret bytes.

    ``_split_trailing_delimiter`` re-emits the run it split off. If that run is
    taken purely on shape (``["'][}\\],]*$``), a secret whose own tail happens
    to look structural gets republished verbatim: ``MY_TOKEN=a'}}}}…`` printed
    32 of 33 characters that the bare ``***`` had covered. A redactor that
    emits *more* plaintext than before is a regression, not a fix.

    Two invariants, both about what is actually **open** at the match:

    * the trailing quote must be the quote left unclosed there — *present*
      earlier in the text is not enough;
    * the structural run after it may only go as far as it closes the
      containers open there, so a secret's own ``}}}}`` is cut at the open
      depth.

    These tests pin those requirements rather than the current output, so
    widening the run class again fails here.
    """

    # (secret, the substring that must not be republished). Each ends in a run
    # that _TRAILING_DELIMITER_RE matches on shape alone.
    STRUCTURAL_TAIL_SECRETS = [
        ("Tr0ub4dor&3'}", "'}"),
        ("a" + "'" + "}" * 31, "'" + "}" * 31),
        ("s3cret'}],", "'}],"),
        ("pw'}]},", "'}]},"),
        ('hunter2"]', '"]'),
        ("pw',", "',"),
    ]

    @pytest.mark.parametrize("secret,structural_tail", STRUCTURAL_TAIL_SECRETS)
    def test_no_opening_quote_means_tail_belongs_to_the_secret(
        self, secret, structural_tail
    ):
        """With nothing opened earlier, the whole value is the secret."""
        result = redact_sensitive_text(f"MY_TOKEN={secret}", force=True)

        assert structural_tail not in result, result
        assert secret not in result, result

    # Prefixes that put a quote earlier in the subject *without* leaving one
    # open. A membership test ("does this quote appear earlier?") passes all of
    # these and re-emits the run verbatim; a parity test ("is one open here?")
    # rejects them. Every case above is prefix-free, so without these a mutant
    # that always splits would survive the suite.
    #
    # Split by quote parity, because the two halves have different achievable
    # contracts:
    #
    # * BALANCED — the prefix's quotes close each other, so parity at the key is
    #   the same as with no prefix at all. Full behaviour, repair included.
    # * ODD — the prefix leaves a quote nominally open (a prose apostrophe, or
    #   one belonging to an earlier secret). Parity is *inverted* for the rest
    #   of the subject: this is the residual, and it is a property of reading
    #   quotes without a grammar, not of this fix. Anti-disclosure still holds;
    #   repair is not attempted (see the two tests below).
    BALANCED_QUOTE_PREFIXES = [
        pytest.param("echo 'hi' && ", id="closed-shell-string"),
        pytest.param('he said "hi" then ', id="closed-double-quote"),
        pytest.param('{"a": 1, "b": 2} ', id="closed-json-object"),
    ]
    ODD_QUOTE_PREFIXES = [
        pytest.param("it's fine, ", id="prose-apostrophe"),
        pytest.param("couldn't parse; ", id="prose-contraction"),
        pytest.param("A_TOKEN=ab'cd ", id="quote-inside-earlier-secret"),
    ]
    CLOSED_QUOTE_PREFIXES = BALANCED_QUOTE_PREFIXES + ODD_QUOTE_PREFIXES

    @pytest.mark.parametrize("prefix", CLOSED_QUOTE_PREFIXES)
    @pytest.mark.parametrize("secret,structural_tail", STRUCTURAL_TAIL_SECRETS)
    def test_closed_quote_earlier_does_not_enable_the_split(
        self, secret, structural_tail, prefix
    ):
        """An earlier quote that is already closed must not enable the split.

        The distinction is *open* versus merely *present*. A lone apostrophe in
        prose, an already-closed shell string, or a quote belonging to a
        different secret all put the character earlier in the subject while
        leaving nothing open at this position — so the trailing run still
        belongs to the secret and must be masked with it.

        Holds for both parities. Where the prefix leaves a quote nominally open
        (``ODD_QUOTE_PREFIXES``) the split does fire, but the open-container
        stack is empty in flat prose, so the run is cut to nothing and only the
        quote itself is re-emitted — never the structural tail this asserts on.
        """
        result = redact_sensitive_text(f"{prefix}MY_TOKEN={secret}", force=True)

        assert structural_tail not in result, result

    @pytest.mark.parametrize("prefix", BALANCED_QUOTE_PREFIXES)
    def test_closed_quote_prefix_still_repairs_a_later_open_container(self, prefix):
        """Parity must not over-reject: a genuinely open quote still splits.

        The counterpart to the test above. After a closed quote earlier in the
        subject, a *newly* opened one must still be recognised, or the repair
        this fix exists for would regress on any text containing prose.
        """
        result = redact_sensitive_text(f"{prefix}sh -c 'export MY_TOKEN=xyz'", force=True)

        assert "xyz" not in result, result
        assert result.endswith("'"), result

    @pytest.mark.parametrize("prefix", ODD_QUOTE_PREFIXES)
    def test_odd_quote_prefix_masks_but_does_not_repair(self, prefix):
        """The documented residual, asserted as what it is.

        An unbalanced quote earlier in the subject — a prose apostrophe, or one
        inside an earlier secret — inverts parity for everything after it, so
        the quote that really does close this shell string reads as *closing*
        rather than opening and the repair is skipped. Reading quotes without a
        grammar cannot tell the two apart.

        What must still hold, and is what this pins: the secret is masked, and
        the outcome is no worse than ``upstream/main``, which eats the closing
        quote on every one of these inputs (verified in a clean worktree). So
        this asserts masking, not corruption — a future improvement that repairs
        the quote here should not have to edit this test.
        """
        result = redact_sensitive_text(f"{prefix}sh -c 'export MY_TOKEN=xyz'", force=True)

        assert "xyz" not in result, result
        assert "MY_TOKEN=***" in result, result

    def test_escaped_quote_does_not_flip_parity(self):
        """A ``\\"`` inside serialized JSON is content, not a delimiter.

        ``json.dumps`` escapes an embedded quote, so a naive parity count would
        see it as opening a string and mis-track every delimiter after it.
        """
        payload = {"note": 'he said "hi"', "c": "MY_TOKEN=ab"}
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)

        reparsed = json.loads(redact_sensitive_text(serialized, force=True))

        assert sorted(reparsed) == ["c", "note"]
        assert "MY_TOKEN=ab" not in reparsed["c"]

    @pytest.mark.parametrize("note", ['say "hi', 'a"b"c"d', 'trailing"', '"leading'])
    @pytest.mark.parametrize("label,kwargs", [("compact", {}), ("indent", {"indent": 2})])
    def test_odd_escaped_quote_count_does_not_flip_parity(self, note, label, kwargs):
        """An *odd* number of escaped quotes is what actually has teeth.

        With an even count, parity is unchanged whether or not the scanner
        honours the backslash, so an even-count fixture cannot catch a scanner
        that ignores escapes. Each ``note`` here serializes to an odd number of
        ``\\"``, so ignoring the backslash inverts parity at the key and the
        closing quote is eaten again — the exact corruption this PR removes.
        """
        payload = {"note": note, "c": "MY_TOKEN=ab"}
        serialized = json.dumps(payload, ensure_ascii=False, **kwargs)
        assert serialized.count('\\"') % 2 == 1, serialized

        reparsed = json.loads(redact_sensitive_text(serialized, force=True))

        assert reparsed["note"] == note
        assert reparsed["c"] == "MY_TOKEN=***"

    @pytest.mark.parametrize("prefix", BALANCED_QUOTE_PREFIXES)
    @pytest.mark.parametrize("secret,structural_tail", STRUCTURAL_TAIL_SECRETS)
    def test_balanced_prefix_discloses_exactly_what_the_baseline_does(
        self, secret, structural_tail, prefix
    ):
        """With parity balanced, the redacted value is byte-identical to base.

        Nothing is open at the key, so no split may fire and the emitted value
        is exactly ``_mask_token(secret)`` — the unpatched behaviour. This is
        the strict form of "never emit more plaintext than upstream": not a
        bound, an equality.
        """
        result = redact_sensitive_text(f"{prefix}MY_TOKEN={secret}", force=True)

        _, _, emitted = result.rpartition("MY_TOKEN=")
        assert emitted == _mask_token(secret), (emitted, result)

    @pytest.mark.parametrize("prefix", ODD_QUOTE_PREFIXES)
    @pytest.mark.parametrize("secret,structural_tail", STRUCTURAL_TAIL_SECRETS)
    def test_odd_prefix_discloses_at_most_one_quote_beyond_the_baseline(
        self, secret, structural_tail, prefix
    ):
        """The residual, measured instead of excused.

        An unbalanced quote earlier in the subject leaves one nominally open, so
        a secret ending in that quote does split. These subjects are flat — no
        ``{`` or ``[`` is open — so the justified structural run is empty and the
        re-emitted suffix is at most the single quote character. Every longer
        suffix of the secret, including the structural tail, is gone.

        Asserted as ``base`` or ``base + the one quote``, so the residual cannot
        grow silently: widening the run class, or dropping the stack check, emits
        more than this and fails here.
        """
        result = redact_sensitive_text(f"{prefix}MY_TOKEN={secret}", force=True)

        _, _, emitted = result.rpartition("MY_TOKEN=")
        baseline = _mask_token(secret)
        allowed = {baseline}
        # The split only fires when the secret's own trailing quote is the one
        # the prefix left open.
        match = re.search(r"([\"'])[}\],]*$", secret)
        if match and match.group(1) == "'":
            allowed.add(_mask_token(secret[: match.start()]) + "'")
        assert emitted in allowed, (emitted, sorted(allowed), result)

    @pytest.mark.parametrize("secret,structural_tail", STRUCTURAL_TAIL_SECRETS)
    def test_disclosure_never_exceeds_the_baseline(self, secret, structural_tail):
        """No suffix of the secret survives the mask.

        Stronger than the test above and the property that actually matters:
        for every suffix of the secret, that suffix must not appear in the
        output. Catches a partial re-emission as well as a whole-run one.
        """
        result = redact_sensitive_text(f"MY_TOKEN={secret}", force=True)

        for start in range(len(secret)):
            assert secret[start:] not in result, (secret[start:], result)

    def test_delimiter_is_still_split_when_a_quote_was_opened(self):
        """The repair must survive the anti-disclosure requirement.

        Same structural tail, but now a quote is genuinely open before the key,
        so the trailing quote is a delimiter and must be re-emitted.
        """
        result = redact_sensitive_text("sh -c 'export MY_TOKEN=xyz'", force=True)

        assert result == "sh -c 'export MY_TOKEN=***'"

    def test_empty_value_keeps_its_container_delimiter(self):
        """A key with no value at all must not eat the closing quote.

        ``MY_TOKEN=`` captures only the delimiter, which an earlier guard
        skipped — leaving the same unterminated-quote corruption the fix exists
        to remove.
        """
        result = redact_sensitive_text("sh -c 'export MY_TOKEN='", force=True)
        assert result.count("'") == 2, result

        payload = json.dumps({"c": "MY_TOKEN=", "b": 1}, ensure_ascii=False, indent=2)
        reparsed = json.loads(redact_sensitive_text(payload, force=True))
        assert sorted(reparsed) == ["b", "c"]


class TestDelimiterRunBoundedByOpenContainers:
    """The structural run is bounded by document state, not by a fixed cap.

    An earlier iteration of this fix capped the run at three characters to bound
    disclosure. That cap is not a property of anything: ``json.dumps`` defaults
    on a five-level payload close with ``"}]}}}`` — five structural characters —
    so the cap rejected the real container tail and left exactly the corruption
    the fix exists to remove, while still admitting three characters of a
    secret's own ``}}}}``.

    What bounds the run instead is the stack of containers actually open at the
    match: a closing run may only go as far as it closes them, in order. That
    admits a legitimate tail of any depth and cuts a secret's run at the open
    depth.
    """

    def test_nesting_deeper_than_the_old_cap_round_trips(self):
        """Five closers — the shape the {0,3} cap rejected."""
        payload = {"request": {"body": {"messages": [{"content": "export MY_TOKEN=xyz"}]}}}
        serialized = json.dumps(payload, ensure_ascii=False)
        assert serialized.endswith('"}]}}}'), serialized

        redacted = redact_sensitive_text(serialized, force=True)

        reparsed = json.loads(redacted)
        content = reparsed["request"]["body"]["messages"][0]["content"]
        assert content == "export MY_TOKEN=***"

    @pytest.mark.parametrize("depth", [1, 2, 4, 6, 9])
    def test_any_nesting_depth_round_trips(self, depth):
        """No cap: the tail is as long as the document is deep."""
        payload = {"c": "MY_TOKEN=xyz"}
        for _ in range(depth):
            payload = {"n": [payload]}
        serialized = json.dumps(payload, ensure_ascii=False)

        redacted = redact_sensitive_text(serialized, force=True)

        node = json.loads(redacted)
        for _ in range(depth):
            node = node["n"][0]
        assert node["c"] == "MY_TOKEN=***"

    def test_run_longer_than_the_open_depth_is_truncated(self):
        """A secret's own run is cut where the open containers run out.

        One ``{`` is open, so one ``}`` is justified; the other three belong to
        the secret and are dropped rather than republished.
        """
        result = redact_sensitive_text("{ it's MY_TOKEN=a'}}}}", force=True)

        assert result == "{ it's MY_TOKEN=***'}", result
        assert "}}" not in result, result

    def test_separator_needs_an_open_container(self):
        """``,`` closes nothing, so it is only legal while something is open."""
        inside = redact_sensitive_text('{"c": "MY_TOKEN=xyz", "b": 1}', force=True)
        assert json.loads(inside)["b"] == 1
        assert json.loads(inside)["c"] == "MY_TOKEN=***"

        # Flat prose: nothing is open, so the trailing comma is secret.
        flat = redact_sensitive_text("it's MY_TOKEN=pw',", force=True)
        assert "'," not in flat, flat

    @pytest.mark.parametrize(
        "run", [",", ",,", ",,,,", ",}", ",]", "},", "}],"]
    )
    def test_separator_is_accepted_only_as_the_final_character(self, run):
        """A separator ends the justified run — nothing may follow it.

        The stack bounds *closers*, but a separator closes nothing, so it cannot
        be bounded by depth: accepting a run of them would republish a secret's
        whole ``',,,,`` tail. In a real container tail a separator can appear
        only once and only last, because whatever follows one is the next member
        — never another closer. So ``}],`` is a legal capture and ``,}`` is not.

        Asserted through the public function, on a subject with one container
        open, so the bound is a behaviour and not an implementation detail.
        """
        result = redact_sensitive_text(f"{{ it's MY_TOKEN=pw'{run}", force=True)

        _, _, emitted = result.partition("MY_TOKEN=")
        # everything after the mask must be a legal tail: closers that the one
        # open "{" justifies, with at most one separator, and it last
        suffix = emitted[len("***") :]
        assert suffix.startswith("'"), (suffix, result)
        structural = suffix[1:]
        assert structural.count(",") <= 1, (suffix, result)
        assert "," not in structural[:-1], (suffix, result)
        assert len(structural.replace(",", "")) <= 1, (suffix, result)

    def test_mismatched_closer_is_not_justified(self):
        """``]`` does not close ``{`` — a stray closer is not a container tail."""
        result = redact_sensitive_text('{"c": "MY_TOKEN=xyz"]}', force=True)

        assert "xyz" not in result, result
        assert result == '{"c": "MY_TOKEN=***"', result

    def test_structural_characters_inside_a_string_are_content(self):
        """A ``{`` inside a JSON string does not open a container.

        Otherwise a value that merely mentions a brace would inflate the stack
        and justify a longer run than the document actually has open.
        """
        payload = {"note": "use {braces} and [brackets]", "c": "MY_TOKEN=xyz"}
        serialized = json.dumps(payload, ensure_ascii=False)

        reparsed = json.loads(redact_sensitive_text(serialized, force=True))

        assert reparsed["note"] == "use {braces} and [brackets]"
        assert reparsed["c"] == "MY_TOKEN=***"


class TestYamlAssignDelimiter:
    """``_YAML_ASSIGN_RE`` is the fourth pattern in this bug class.

    Its value class ``[^\\s&]++`` absorbs a trailing quote exactly like the
    KEY=VALUE and header patterns; the negative lookahead only rejects a
    *leading* quote, so a quoted value defers to ``_JSON_FIELD_RE`` while a
    trailing delimiter still reaches the masker.

    Reachable through ``gateway.run._redact_approval_command``, which runs the
    raw command string with ``code_file=False`` — so a multi-line command
    echoed into a chat approval prompt hits this pass.
    """

    @pytest.mark.parametrize("key", ["password", "api_key", "client_secret"])
    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_multiline_shell_string_stays_balanced(self, key, quote):
        text = f"sh -c {quote}\n{key}: xyz{quote}"

        result = redact_sensitive_text(text, force=True)

        assert "xyz" not in result, result
        assert result.count(quote) == 2, result
        assert result.endswith(quote), result

    def test_structural_tail_without_opening_quote_is_masked(self):
        """Same anti-disclosure requirement as the other patterns."""
        result = redact_sensitive_text("password: pw'}]", force=True)

        assert "'}]" not in result, result
        assert "pw" not in result, result


class TestRedactThenReparseConsumers:
    """The three in-tree callers that reparse redacted JSON, end to end.

    These replicate each consumer's real handling — including its exception
    handler — because the handler is what turns a corrupted document into the
    user-visible failure. Behaviour contracts, not output snapshots.
    """

    # Both serializers used by the real callers. Neither is
    # ``separators=(",", ":")``: dump_api_request_debug passes ``indent=2``,
    # trace_upload and kanban_tools use json.dumps' default ``", "`` item
    # separator. Both therefore leave whitespace after a string value.
    SERIALIZERS = [
        ("indent", {"indent": 2}),
        ("default", {}),
    ]

    SECRETS = [
        "export MY_TOKEN=xyz",
        "DB_PASSWORD=pw1",
        "spring.datasource.password=ab",
        "x-api-key: abc123",
        "psql; export MY_TOKEN=xyz",
    ]

    @pytest.mark.parametrize("secret_text", SECRETS)
    @pytest.mark.parametrize("label,kwargs", SERIALIZERS)
    def test_request_dump_payload_survives(self, secret_text, label, kwargs):
        """dump_api_request_debug: json.loads must not raise.

        The real function wraps everything in ``except Exception: return None``,
        so a raise here means no dump file at all.
        """
        payload = {"request": {"body": {"messages": [{"content": secret_text}]}}}
        serialized = json.dumps(payload, ensure_ascii=False, **kwargs)

        reparsed = json.loads(redact_sensitive_text(serialized, force=True))

        assert list(reparsed) == ["request"]

    @pytest.mark.parametrize("secret_text", SECRETS)
    def test_trace_upload_tool_args_survive(self, secret_text):
        """trace_upload: a corrupted reparse raises TraceRedactionError.

        ``_tool_calls_to_blocks`` re-serializes with json.dumps' defaults and
        refuses the entire upload when the result won't parse.
        """
        args = {"command": secret_text, "cwd": "/tmp"}

        reparsed = json.loads(redact_sensitive_text(json.dumps(args), force=True))

        assert sorted(reparsed) == ["command", "cwd"]
        assert reparsed["cwd"] == "/tmp"

    @pytest.mark.parametrize("secret_text", SECRETS)
    def test_kanban_metadata_is_not_left_unredacted(self, secret_text):
        """kanban_tools: ``except JSONDecodeError: pass`` keeps the raw dict.

        The most severe of the three — the handler leaves ``metadata`` bound to
        the original object, so a corrupted reparse means the unredacted secret
        is what gets persisted. Replicates tools/kanban_tools.py's five lines.
        """
        metadata = {"cmd": secret_text}
        original = dict(metadata)

        meta_json = redact_sensitive_text(json.dumps(metadata), force=True)
        try:
            metadata = json.loads(meta_json)
        except json.JSONDecodeError:
            pass  # the real handler

        assert metadata != original, "redaction silently bypassed"


class TestEscapedQuoteSurvivesRedaction:
    """Masking must not destroy the backslash that escapes a quote.

    The delimiter-preserving half of this module answers "don't consume the
    quote that closes the document." This class answers its twin: inside a
    serialized JSON string that quote appears as ``\\"``, and a value class that
    admits ``\\`` captures the ESCAPE with the secret. Destroy the escape and
    the quote left behind is bare, so the string ends one byte early — the same
    unparseable document, reached through the escape byte instead of the
    delimiter byte.

    Two distinct sites, one rule:

    * ``_ENV_ASSIGN_RE`` / ``_CFG_DOTTED_RE`` / ``_SECRET_HEADER_RE`` — the
      ``(\\S+)`` capture ends exactly at ``\\"`` when whitespace and more
      content follow it inside the same string, and the delimiter split then
      cut between the backslash and the quote, masking the escape away while
      re-emitting its quote bare.
    * ``_AUTH_HEADER_RE`` — its class excludes the quote (#43083) but not the
      backslash, so the capture ends ``…secret\\``. Above the 18-char mask floor
      the 4-char tail happened to re-emit that backslash; below it the bare
      ``***`` deleted it. Only short credentials corrupted, which made this look
      like a floor bug rather than an escape bug.

    Parametrized across the mask floor because the floor is what decides bare
    ``***`` versus a head/tail window, and that choice is exactly what decided
    whether the escape survived by accident.
    """

    # Secret lengths sweep 1..40 inside each test rather than as pytest params:
    # the contract is uniform in n, and 40 ids per template would triple this
    # file's case count for no extra signal. The failing n is reported in the
    # assertion message.
    LENGTHS = range(1, 41)

    # Drawn only from characters absent from every template below, so a short
    # secret can never coincide with the surrounding text ("k" appearing in
    # "x-api-key", "z" in "Authorization"). That keeps the assertion the strong,
    # unqualified ``secret not in value`` at every n — including n=1 — instead of
    # a minimum-length carve-out, which would stop testing the lower half of the
    # mask floor, i.e. exactly the band where the escape was destroyed.
    # ``_assert_no_incidental_overlap`` enforces the disjointness, so a template
    # added later that breaks it fails loudly rather than silently weakening the
    # sweep.
    SECRET_ALPHABET = "fgjmq0234567890"

    def _secret(self, n):
        alphabet = self.SECRET_ALPHABET
        return (alphabet * (n // len(alphabet) + 1))[:n]

    def _assert_no_incidental_overlap(self, template):
        """No character of a generated secret may occur in the literal text.

        Cheaper and stricter than checking substrings: if the template shares no
        character with the alphabet, no substring of any secret can appear in it.
        """
        literal = template.replace("{q}", "").replace("{s}", "")
        shared = set(literal) & set(self.SECRET_ALPHABET)
        assert not shared, (template, sorted(shared))

    # Content follows the escaped quote, so the value capture terminates *at*
    # ``\"`` — the shape that makes the escape reachable. One template per
    # affected pattern.
    ESCAPED_QUOTE_TEMPLATES = [
        pytest.param("sh -c {q}export MY_TOKEN={s}{q} && echo done", id="env-assign"),
        pytest.param("curl -H {q}x-api-key: {s}{q} --verbose", id="secret-header"),
        pytest.param("run {q}app.api.key={s}{q} now", id="cfg-dotted"),
    ]

    AUTH_HEADER_TEMPLATES = [
        pytest.param("curl -H {q}Authorization: Bearer {s}{q} -v", id="bearer"),
        pytest.param("curl -H {q}Authorization: Basic {s}{q} -v", id="basic"),
        pytest.param("curl -H {q}Authorization: {s}{q} -v", id="bare-credential"),
        pytest.param("curl -H {q}Proxy-Authorization: Bearer {s}{q} -v", id="proxy"),
        pytest.param("curl -H {q}Authorization: Bearer {s}{q}", id="bearer-at-end"),
    ]

    @pytest.mark.parametrize("template", ESCAPED_QUOTE_TEMPLATES)
    @pytest.mark.parametrize("quote", ['"', "'"])
    @pytest.mark.parametrize("indent", [None, 2])
    def test_escaped_quote_in_json_string_keeps_document_parseable(
        self, template, quote, indent
    ):
        """The document parses, the secret is gone, and the quote is still there.

        All three, because any one alone is satisfiable the wrong way: a
        document can parse because the secret survived unmasked, and a secret
        can be masked into an unparseable document. The third assertion is the
        one this fix adds — the inner quote must still be in the *decoded*
        value, which is only true if its escape survived masking.
        """
        for n in self.LENGTHS:
            secret = self._secret(n)
            inner = template.format(q=quote, s=secret)
            serialized = json.dumps(
                {"content": inner}, ensure_ascii=False, indent=indent
            )

            redacted = redact_sensitive_text(serialized, force=True)

            ctx = (n, quote, indent, serialized, redacted)
            value = json.loads(redacted)["content"]  # raises if corrupted
            assert secret not in value, ctx
            if quote == '"':
                # json.dumps only escapes ``"``. A single-quoted inner string
                # carries no escape, and the quote left open at the match is the
                # JSON string's own, so the redactor cannot tell that ``'`` from
                # content — it is masked with the value on every tree. Asserting
                # its survival would pin a promise the redactor never made.
                assert value.count('"') == inner.count('"'), ctx
        self._assert_no_incidental_overlap(template)

    @pytest.mark.parametrize("template", AUTH_HEADER_TEMPLATES)
    @pytest.mark.parametrize("quote", ['"', "'"])
    @pytest.mark.parametrize("indent", [None, 2])
    def test_authorization_credential_escape_survives(
        self, template, quote, indent
    ):
        """``_AUTH_HEADER_RE``'s class excludes the quote but not its escape.

        The residual left by #43083: excluding ``"`` from the credential class
        stops the capture from eating the quote, but the backslash in front of
        it is still a "not a quote, not whitespace" character, so the capture
        ends ``…secret\\`` inside a serialized document. Swept across the floor
        because that is where the old behaviour changed — a 4-char tail
        re-emitted the backslash, a bare ``***`` deleted it.
        """
        self._assert_no_incidental_overlap(template)
        for n in self.LENGTHS:
            secret = self._secret(n)
            inner = template.format(q=quote, s=secret)
            serialized = json.dumps(
                {"content": inner}, ensure_ascii=False, indent=indent
            )

            redacted = redact_sensitive_text(serialized, force=True)

            ctx = (n, quote, indent, serialized, redacted)
            value = json.loads(redacted)["content"]
            assert secret not in value, ctx

    @pytest.mark.parametrize("template", ESCAPED_QUOTE_TEMPLATES)
    @pytest.mark.parametrize("indent", [None, 2])
    def test_escaped_quote_when_secret_is_not_the_last_field(self, template, indent):
        """A following field is a different path through the split.

        When the secret's string is the document's last value, the capture runs
        past the escaped quote to the document's real closing ``"}``, and the
        ``$``-anchored trailing-delimiter search lands on that genuine
        delimiter. With a field after it, the capture stops at the escaped quote
        instead and the search lands on ``\\"`` — the corrupting case. Both
        shapes must hold, so both are asserted.
        """
        self._assert_no_incidental_overlap(template)
        for n in self.LENGTHS:
            secret = self._secret(n)
            inner = template.format(q='"', s=secret)
            serialized = json.dumps(
                {"a": inner, "b": 1}, ensure_ascii=False, indent=indent
            )

            redacted = redact_sensitive_text(serialized, force=True)

            ctx = (n, indent, serialized, redacted)
            reparsed = json.loads(redacted)
            assert reparsed["b"] == 1, ctx
            assert secret not in reparsed["a"], ctx
            assert reparsed["a"].count('"') == inner.count('"'), ctx

    # Characters whose count carries JSON/shell structure. A redactor may drop
    # them (they were the secret's own) but may never emit more than it received.
    STRUCTURAL_CHARS = "\"'{}[],"

    @staticmethod
    def _bare_quotes(text):
        """Count unescaped ``"`` — the ones JSON syntax actually owns."""
        count = index = 0
        while index < len(text):
            if text[index] == "\\":
                index += 2  # the escaped character is content, not syntax
                continue
            if text[index] == '"':
                count += 1
            index += 1
        return count

    @pytest.mark.parametrize(
        "template",
        ESCAPED_QUOTE_TEMPLATES + AUTH_HEADER_TEMPLATES,
    )
    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_redaction_never_manufactures_delimiters(self, template, quote):
        """Redaction may delete structure bytes, never invent them.

        The mechanism-level guard, stated over the text so it binds any future
        pattern rather than the three fixed here. Turning a source ``\\"`` into a
        bare ``"`` is precisely "manufacturing a delimiter": the byte count is
        unchanged but a content quote became a syntax quote, which is what ended
        the JSON string early. Counting *unescaped* quotes is what detects that;
        counting quotes alone does not.

        The trailing run is bounded by the input's own run rather than by
        ``1 + depth``: a document whose last string *content* ends in quotes or
        braces has a longer trailing run than its nesting depth, and that is the
        input's business, not the redactor's. ``1 + depth`` bounds the suffix the
        split re-emits (asserted in TestDelimiterSplitNeverDisclosesSecretBytes),
        not the document's tail.
        """
        for n in self.LENGTHS:
            inner = template.format(q=quote, s=self._secret(n))
            for indent in (None, 2):
                serialized = json.dumps(
                    {"content": inner}, ensure_ascii=False, indent=indent
                )

                redacted = redact_sensitive_text(serialized, force=True)

                ctx = (n, quote, indent, serialized, redacted)
                for char in self.STRUCTURAL_CHARS:
                    assert redacted.count(char) <= serialized.count(char), (char, ctx)
                assert self._bare_quotes(redacted) <= self._bare_quotes(serialized), ctx
                trailing = re.search(r"[\"'}\],]*$", redacted).group(0)
                original_trailing = re.search(r"[\"'}\],]*$", serialized).group(0)
                assert len(trailing) <= len(original_trailing), ctx

    # The shapes the reparsing consumers actually hand us, now carrying an
    # escaped quote. ``nested`` matters because the delimiter run the split may
    # re-emit is bounded by open-container depth, and depth > 1 is where a
    # too-generous bound would show up.
    CONSUMER_SHAPES = [
        pytest.param(lambda inner: {"content": inner}, ("content",), id="flat"),
        pytest.param(
            lambda inner: {"a": {"b": [{"content": inner}]}},
            ("a", "b", 0, "content"),
            id="nested-3-deep",
        ),
    ]

    @pytest.mark.parametrize("build,path", CONSUMER_SHAPES)
    @pytest.mark.parametrize("template", ESCAPED_QUOTE_TEMPLATES + AUTH_HEADER_TEMPLATES)
    def test_redacted_json_dump_round_trips_for_reparsing_consumers(
        self, build, path, template
    ):
        """kanban_tools' handler, driven by the escaped-quote shape.

        ``except json.JSONDecodeError: pass`` leaves ``metadata`` bound to the
        ORIGINAL unredacted dict, so a document this module corrupts is not a
        cosmetic failure — it is a redaction bypass that persists the secret
        verbatim. Asserted the way the consumer experiences it: the object it
        ends up storing must not be the raw one.
        """
        self._assert_no_incidental_overlap(template)
        for n in self.LENGTHS:
            secret = self._secret(n)
            inner = template.format(q='"', s=secret)
            metadata = build(inner)
            original = json.loads(json.dumps(metadata))  # deep copy

            meta_json = redact_sensitive_text(json.dumps(metadata), force=True)
            try:
                metadata = json.loads(meta_json)
            except json.JSONDecodeError:
                pass  # the real handler in tools/kanban_tools.py

            ctx = (n, template, meta_json)
            assert metadata != original, ("redaction silently bypassed", ctx)
            value = metadata
            for step in path:
                value = value[step]
            assert secret not in value, ctx
