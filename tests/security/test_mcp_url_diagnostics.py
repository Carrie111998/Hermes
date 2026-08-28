"""Security regressions for opaque capabilities in MCP diagnostics."""

import logging
from types import SimpleNamespace


CAPABILITY = "q7ZP3mN8vK2xR9tW4cY6bH1jF5sL0dG7"
CAPABILITY_URL = f"https://mcp.example.invalid/api/connect/{CAPABILITY}/events"
ORDINARY_URL = "https://docs.example.invalid/guides/setup/index.html"


def test_url_projection_covers_structural_capability_shapes():
    from agent.redact import redact_diagnostic_text

    capabilities = (
        "550e8400-e29b-41d4-a716-446655440000",
        "q7ZP3mN8vK2xR9tW",
        "abcdefghijklmnopqrstuvwxyzabcdef",
    )

    for capability in capabilities:
        rendered = redact_diagnostic_text(
            f"failed at https://mcp.example.invalid/connect/{capability}/events; "
            f"documentation: {ORDINARY_URL}",
            force=True,
        )
        assert capability not in rendered
        assert "https://mcp.example.invalid/connect/<redacted>/events" in rendered
        assert ORDINARY_URL in rendered


def test_url_projection_covers_query_fragment_and_userinfo_capabilities():
    from agent.redact import redact_diagnostic_text

    query_capability = "q7ZP3mN8vK2xR9tW4cY6bH1jF5sL0dG7"
    fragment_capability = "m8Qv2Nx7Kp4Rt9Wy6Bc3Dh1F"
    userinfo_capability = "u7Qm3Zx9Vp2Ks8Rw5Cd1"
    url = (
        f"https://{userinfo_capability}@mcp.example.invalid/connect"
        f"?relay={query_capability}&view=public#{fragment_capability}"
    )

    rendered = redact_diagnostic_text(f"request failed for {url}", force=True)

    for capability in (query_capability, fragment_capability, userinfo_capability):
        assert capability not in rendered
    assert "https://" + "***" + "@mcp.example.invalid/connect" in rendered
    assert "relay=<redacted>&view=public#<redacted>" in rendered


def test_url_projection_covers_bare_query_capability_and_preserves_ordinary_query():
    from agent.redact import project_diagnostic_urls

    url = f"https://mcp.example.invalid/callback?{CAPABILITY}"
    rendered = project_diagnostic_urls(
        f"request failed for {url}; docs: {ORDINARY_URL}?printable"
    )

    assert CAPABILITY not in rendered
    assert url not in rendered
    assert "https://mcp.example.invalid/callback?<redacted>" in rendered
    assert f"{ORDINARY_URL}?printable" in rendered


def test_url_projection_covers_single_encoded_reserved_character_capabilities():
    from agent.redact import project_diagnostic_urls

    encoded_capabilities = (
        f"{CAPABILITY[:18]}%2F{CAPABILITY[18:]}",
        f"{CAPABILITY[:18]}%26{CAPABILITY[18:]}",
        f"{CAPABILITY[:18]}%3F{CAPABILITY[18:]}",
    )
    urls = (
        f"https://mcp.example.invalid/connect/{encoded_capabilities[0]}/events",
        f"https://mcp.example.invalid/callback?relay={encoded_capabilities[1]}",
        f"https://mcp.example.invalid/#relay={encoded_capabilities[2]}",
    )

    for url, encoded_capability in zip(urls, encoded_capabilities, strict=True):
        rendered = project_diagnostic_urls(f"request failed for {url}")

        assert encoded_capability not in rendered
        assert CAPABILITY not in rendered
        assert "<redacted>" in rendered


def test_url_projection_covers_capability_split_by_repeated_encoded_separators():
    from agent.redact import project_diagnostic_urls

    encoded_capability = "%2F".join(
        CAPABILITY[index : index + 8] for index in range(0, len(CAPABILITY), 8)
    )
    url = f"https://mcp.example.invalid/connect/{encoded_capability}/events"
    rendered = project_diagnostic_urls(f"request failed for {url}")

    assert encoded_capability not in rendered
    assert rendered == (
        "request failed for https://mcp.example.invalid/connect/<redacted>/events"
    )


def test_url_projection_covers_high_entropy_numeric_capability():
    from agent.redact import project_diagnostic_urls

    numeric_capability = "3141592653589793238462643383279502884197"
    url = f"https://mcp.example.invalid/connect/{numeric_capability}/events"
    rendered = project_diagnostic_urls(f"request failed for {url}")

    assert numeric_capability not in rendered
    assert rendered == (
        "request failed for https://mcp.example.invalid/connect/<redacted>/events"
    )


def test_url_projection_preserves_ordinary_single_encoded_component():
    from agent.redact import project_diagnostic_urls

    url = "https://docs.example.invalid/guides/setup%20guide/index.html"

    assert project_diagnostic_urls(f"documentation: {url}") == f"documentation: {url}"


def test_url_projection_preserves_human_readable_hyphenated_slugs():
    from agent.redact import project_diagnostic_urls

    slugs = (
        "getting-started-with-python3",
        "api-reference-v2-endpoints",
        "ultra-widget-model-1234",
    )

    for slug in slugs:
        url = f"https://docs.example.invalid/guides/{slug}"
        assert project_diagnostic_urls(f"documentation: {url}") == f"documentation: {url}"


def test_configured_mcp_url_projection_redacts_every_non_root_component():
    from agent.redact import project_configured_mcp_url

    assert project_configured_mcp_url("https://mcp.example.invalid/") == (
        "https://mcp.example.invalid/"
    )
    for configured_url in (
        "https://mcp.example.invalid/english-word-shaped-token",
        "https://mcp.example.invalid/?relay=public-looking",
        "https://mcp.example.invalid/#public-looking",
        "https://" + "fabricated-user" + "@mcp.example.invalid/",
    ):
        rendered = project_configured_mcp_url(configured_url)
        assert "english-word-shaped-token" not in rendered
        assert "public-looking" not in rendered
        assert "fabricated-user" not in rendered
        assert rendered == "https://mcp.example.invalid/<redacted>"

    assert project_configured_mcp_url("not-a-url") == "<redacted>"


def test_url_projection_fails_closed_for_double_encoding_ambiguity():
    from agent.redact import project_diagnostic_urls

    ambiguous_component = f"{CAPABILITY[:18]}%252F{CAPABILITY[18:]}"
    url = f"https://mcp.example.invalid/connect/{ambiguous_component}/events"
    rendered = project_diagnostic_urls(f"request failed for {url}")

    assert ambiguous_component not in rendered
    assert rendered == (
        "request failed for https://mcp.example.invalid/connect/<redacted>/events"
    )


def test_url_projection_covers_capability_before_diagnostic_suffixes():
    from agent.redact import project_diagnostic_urls

    for suffix in ("`", "\x1b[0m", "—", "\\", "|"):
        rendered = project_diagnostic_urls(
            f"request failed for https://mcp.example.invalid/connect/{CAPABILITY}{suffix}"
        )

        assert CAPABILITY not in rendered
        assert "<redacted>" in rendered


def test_url_projection_fails_closed_for_malformed_authority():
    from agent.redact import project_diagnostic_urls

    rendered = project_diagnostic_urls(
        f"request failed for https://[broken.invalid/connect/{CAPABILITY}/events"
    )

    assert CAPABILITY not in rendered
    assert rendered == "request failed for https://<redacted>"


def test_url_projection_covers_capability_in_structured_fragment():
    from agent.redact import project_diagnostic_urls

    url = f"https://mcp.example.invalid/#/connect/{CAPABILITY}"
    rendered = project_diagnostic_urls(f"request failed for {url}")

    assert CAPABILITY not in rendered
    assert rendered == (
        "request failed for https://mcp.example.invalid/#/connect/<redacted>"
    )


def test_url_projection_covers_capability_in_query_like_fragment():
    from agent.redact import project_diagnostic_urls

    url = f"https://mcp.example.invalid/#/connect?relay={CAPABILITY}&view=public"
    rendered = project_diagnostic_urls(f"request failed for {url}")

    assert CAPABILITY not in rendered
    assert rendered == (
        "request failed for "
        "https://mcp.example.invalid/#/connect?relay=<redacted>&view=public"
    )


def test_url_projection_fails_closed_for_invalid_port_authority():
    from agent.redact import project_diagnostic_urls

    url = f"https://mcp.example.invalid:{CAPABILITY}/status"
    rendered = project_diagnostic_urls(f"request failed for {url}")

    assert CAPABILITY not in rendered
    assert rendered == "request failed for https://<redacted>"


def test_url_projection_fails_closed_for_forbidden_authority_characters():
    from agent.redact import project_diagnostic_urls

    for separator in ("\\", "|", "%"):
        url = f"https://mcp.example.invalid{separator}{CAPABILITY}/status"
        rendered = project_diagnostic_urls(f"request failed for {url}")

        assert CAPABILITY not in rendered
        assert rendered == "request failed for https://<redacted>"


def test_url_projection_preserves_valid_authority_only_ipv6_url():
    from agent.redact import project_diagnostic_urls

    url = "https://[2001:db8::1]"

    assert project_diagnostic_urls(f"request failed for {url}") == (
        f"request failed for {url}"
    )


def test_url_projection_and_diagnostic_boundaries(capsys, monkeypatch):
    from agent.redact import RedactingFormatter, redact_diagnostic_text
    from hermes_cli import mcp_config
    from model_tools import _sanitize_tool_error

    projected = redact_diagnostic_text(
        f"failed at {CAPABILITY_URL}; documentation: {ORDINARY_URL}", force=True
    )
    assert CAPABILITY not in projected
    assert "https://mcp.example.invalid/api/connect/<redacted>/events" in projected
    assert ORDINARY_URL in projected

    stream = __import__("io").StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    record = logging.LogRecord(
        "tools.mcp_tool",
        logging.ERROR,
        __file__,
        1,
        "MCP exception: %s",
        (RuntimeError(CAPABILITY_URL),),
        None,
    )
    handler.handle(record)
    full_log = stream.getvalue()
    assert CAPABILITY not in full_log
    assert CAPABILITY_URL not in full_log
    assert "<redacted>" in full_log

    tool_error = _sanitize_tool_error(f"connection failed: {CAPABILITY_URL}")
    assert CAPABILITY not in tool_error
    assert CAPABILITY_URL not in tool_error
    assert "<redacted>" in tool_error

    monkeypatch.setattr(
        mcp_config,
        "_get_mcp_servers",
        lambda: {"fabricated": {"url": "${env:FABRICATED_MCP_URL}"}},
    )
    monkeypatch.setenv("FABRICATED_MCP_URL", CAPABILITY_URL)
    monkeypatch.setattr(
        mcp_config,
        "_probe_single_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(CAPABILITY_URL)),
    )
    mcp_config.cmd_mcp_test(SimpleNamespace(name="fabricated"))
    captured = capsys.readouterr()
    full_cli_output = captured.out + captured.err
    assert "fabricated" in full_cli_output
    assert "Transport: HTTP" in full_cli_output
    assert "FABRICATED_MCP_URL" in full_cli_output
    assert CAPABILITY not in full_cli_output
    assert CAPABILITY_URL not in full_cli_output
    assert "<redacted>" in full_cli_output


def test_mcp_test_sanitizes_successful_server_controlled_output(capsys, monkeypatch):
    from hermes_cli import mcp_config

    capability_desc_url = f"https://m.invalid/{CAPABILITY}"
    ordinary_tool_url = "https://docs.example.invalid/help"
    monkeypatch.setattr(
        mcp_config,
        "_get_mcp_servers",
        lambda: {"fabricated": {"url": "https://mcp.example.invalid/safe"}},
    )
    monkeypatch.setattr(
        mcp_config,
        "_probe_single_server",
        lambda *_args, **_kwargs: [
            (f"https://mcp.example.invalid/tools/{CAPABILITY}", "capability tool"),
            ("capability_desc", f"Docs: {capability_desc_url}"),
            ("ordinary_tool", f"Docs: {ordinary_tool_url}"),
        ],
    )

    mcp_config.cmd_mcp_test(SimpleNamespace(name="fabricated"))
    captured = capsys.readouterr()
    full_cli_output = captured.out + captured.err
    assert CAPABILITY not in full_cli_output
    assert CAPABILITY_URL not in full_cli_output
    assert "https://mcp.example.invalid/tools/<redacted>" in full_cli_output
    assert "https://m.invalid/<redacted>" in full_cli_output
    assert ordinary_tool_url in full_cli_output


def test_mcp_test_sanitizes_untrusted_url_environment_reference(capsys, monkeypatch):
    from hermes_cli import mcp_config

    malicious_ref = "https://" + "m.invalid/" + CAPABILITY
    configured_url = "${env:" + malicious_ref + "}"
    monkeypatch.setattr(
        mcp_config,
        "_get_mcp_servers",
        lambda: {"fabricated": {"url": configured_url}},
    )
    monkeypatch.setattr(
        mcp_config,
        "_probe_single_server",
        lambda *_args, **_kwargs: [],
    )

    mcp_config.cmd_mcp_test(SimpleNamespace(name="fabricated"))
    captured = capsys.readouterr()
    full_cli_output = captured.out + captured.err

    assert CAPABILITY not in full_cli_output
    assert malicious_ref not in full_cli_output
    assert "URL environment: <redacted>" in full_cli_output


def test_mcp_add_sanitizes_url_errors_and_server_controlled_tools(capsys, monkeypatch):
    from hermes_cli import mcp_config

    monkeypatch.setattr(mcp_config, "_get_mcp_servers", lambda: {})
    monkeypatch.setattr(mcp_config, "validate_mcp_server_entry", lambda *_args: [])
    monkeypatch.setattr(mcp_config, "_confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        mcp_config,
        "_probe_single_server",
        lambda *_args, **_kwargs: [
            (f"https://mcp.example.invalid/tools/{CAPABILITY}", CAPABILITY_URL)
        ],
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    mcp_config.cmd_mcp_add(
        SimpleNamespace(
            name="fabricated",
            url=CAPABILITY_URL,
            mcp_command=None,
            args=[],
            auth=None,
            preset=None,
            env=None,
            connect_timeout=None,
        )
    )
    full_cli_output = capsys.readouterr().out

    assert CAPABILITY not in full_cli_output
    assert CAPABILITY_URL not in full_cli_output
    assert "<redacted>" in full_cli_output


def test_mcp_list_never_renders_configured_http_url(capsys, monkeypatch):
    from hermes_cli import mcp_config

    monkeypatch.setattr(
        mcp_config,
        "_get_mcp_servers",
        lambda: {"fabricated": {"url": CAPABILITY_URL}},
    )

    mcp_config.cmd_mcp_list()
    full_cli_output = capsys.readouterr().out

    assert "fabricated" in full_cli_output
    assert "HTTP" in full_cli_output
    assert CAPABILITY not in full_cli_output
    assert CAPABILITY_URL not in full_cli_output


def test_mcp_configure_sanitizes_server_controlled_checklist_labels(
    capsys, monkeypatch
):
    from hermes_cli import curses_ui, mcp_config

    labels_seen = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        mcp_config,
        "_get_mcp_servers",
        lambda: {"fabricated": {"url": "https://mcp.example.invalid/safe"}},
    )
    monkeypatch.setattr(
        mcp_config,
        "_probe_single_server",
        lambda *_args, **_kwargs: [("fabricated_tool", CAPABILITY_URL)],
    )

    def _capture_labels(_title, labels, pre_selected):
        labels_seen.extend(labels)
        return pre_selected

    monkeypatch.setattr(curses_ui, "curses_checklist", _capture_labels)

    mcp_config.cmd_mcp_configure(SimpleNamespace(name="fabricated"))

    assert CAPABILITY not in "".join(labels_seen)
    assert "<redacted>" in "".join(labels_seen)
    assert "No changes made" in capsys.readouterr().out


def test_mcp_specific_error_sanitizer_uses_central_projection():
    from tools.mcp_tool import _sanitize_error

    rendered = _sanitize_error(f"transport failed at {CAPABILITY_URL}")

    assert CAPABILITY not in rendered
    assert CAPABILITY_URL not in rendered
    assert "https://mcp.example.invalid/api/connect/<redacted>/events" in rendered


def test_mcp_error_sanitizer_uses_configured_url_context_for_slug_capability():
    from tools.mcp_tool import _sanitize_error

    configured_url = "https://mcp.example.invalid/english-word-shaped-token"
    rendered = _sanitize_error(
        f"transport failed at {configured_url}",
        configured_url=configured_url,
    )

    assert "english-word-shaped-token" not in rendered
    assert rendered == (
        "transport failed at https://mcp.example.invalid/<redacted>"
    )


def test_mcp_connect_error_uses_configured_url_context_for_slug_capability():
    from tools.mcp_tool import _format_connect_error

    configured_url = "https://mcp.example.invalid/english-word-shaped-token"
    rendered = _format_connect_error(
        RuntimeError(f"transport failed at {configured_url}"),
        configured_url=configured_url,
    )

    assert "english-word-shaped-token" not in rendered
    assert rendered == (
        "transport failed at https://mcp.example.invalid/<redacted>"
    )


def test_mcp_error_sanitizer_masks_explicit_url_credential_keys():
    from tools.mcp_tool import _sanitize_error

    secret = "fabricated-signature-value"
    rendered = _sanitize_error(
        f"transport failed at https://mcp.example.invalid/callback?signature={secret}&view=public"
    )

    assert secret not in rendered
    assert "?[REDACTED]&view=public" in rendered


def test_mcp_test_uses_configured_url_context_for_slug_capability(
    capsys, monkeypatch
):
    from hermes_cli import mcp_config

    configured_url = "https://mcp.example.invalid/english-word-shaped-token"
    monkeypatch.setattr(
        mcp_config,
        "_get_mcp_servers",
        lambda: {"fabricated": {"url": configured_url}},
    )
    monkeypatch.setattr(
        mcp_config,
        "_probe_single_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(configured_url)),
    )

    mcp_config.cmd_mcp_test(SimpleNamespace(name="fabricated"))
    captured = capsys.readouterr()
    full_cli_output = captured.out + captured.err

    assert "english-word-shaped-token" not in full_cli_output
    assert "https://mcp.example.invalid/<redacted>" in full_cli_output


def test_diagnostic_projection_masks_named_url_credentials():
    from agent.redact import redact_diagnostic_text

    secret = "fabricated-query-capability"
    url = f"https://mcp.example.invalid/callback?token={secret}&view=public"
    rendered = redact_diagnostic_text(f"request failed for {url}", force=True)

    assert secret not in rendered
    assert "https://mcp.example.invalid/callback?token=***&view=public" in rendered


def test_acp_logging_suppresses_httpx2_info():
    from acp_adapter.entry import _setup_logging

    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_root_level = root.level
    httpx2_logger = logging.getLogger("httpx2")
    previous_httpx2_level = httpx2_logger.level
    try:
        httpx2_logger.setLevel(logging.NOTSET)
        _setup_logging()
        assert httpx2_logger.level == logging.WARNING
    finally:
        for handler in root.handlers:
            if handler not in previous_handlers:
                handler.close()
        root.handlers[:] = previous_handlers
        root.setLevel(previous_root_level)
        httpx2_logger.setLevel(previous_httpx2_level)


def test_httpx2_info_is_suppressed_in_verbose_log_output(capsys):
    import hermes_logging

    logger = logging.getLogger("httpx2")
    previous_level = logger.level
    root = logging.getLogger()
    previous_root_level = root.level
    added = []
    try:
        for handler in list(root.handlers):
            if getattr(handler, "_hermes_verbose", False):
                root.removeHandler(handler)
        before = set(root.handlers)
        logger.setLevel(logging.NOTSET)
        hermes_logging.setup_verbose_logging()
        added = [handler for handler in root.handlers if handler not in before]
        logger.info("fabricated httpx2 request detail")
        logger.warning("fabricated httpx2 warning")
        output = capsys.readouterr().err
        assert "fabricated httpx2 request detail" not in output
        assert "fabricated httpx2 warning" in output
    finally:
        for handler in added:
            root.removeHandler(handler)
            handler.close()
        logger.setLevel(previous_level)
        root.setLevel(previous_root_level)
