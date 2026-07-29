"""Tests for Feishu adapter outbound markdown payload construction.

Reproduces the bug tracked in hermes-agent issue #52786:
`_build_outbound_payload` was force-downgrading any message containing a
markdown pipe table to ``msg_type=text``, so Feishu clients rendered the raw
pipe-and-dash source instead of a table.  Empirically current Feishu clients
render ``post``+``md`` tables natively, so the downgrade branch must be removed.

These tests guard the fix.  They invoke the real adapter via the project's
plugin-loader helper so that no ``sys.path`` / ``sys.modules`` games are
needed.
"""

from __future__ import annotations

import json

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_adapter = load_plugin_adapter("feishu")

def _call_build_outbound_payload(content: str) -> tuple[str, str]:
    """Invoke ``_build_outbound_payload`` on a bare adapter instance.

    ``_build_outbound_payload`` is a method that only uses module-level
    helpers (``_MARKDOWN_TABLE_RE``, ``_MARKDOWN_HINT_RE``,
    ``_build_markdown_post_payload``) and never touches ``self.*``, so a bare
    object is sufficient.
    """
    inst = object.__new__(_adapter.FeishuAdapter)
    return inst._build_outbound_payload(content)


def _md_texts_from_post_payload(payload_str: str) -> list[str]:
    """Pull every ``{tag:'md', text:'...'}`` element out of a Feishu post payload.

    Real payload shape::

        {"zh_cn": {"content": [[{"tag": "md", "text": "..."}], ...]}}

    Helpers and tests need to introspect the ``md`` blocks regardless of
    locale, so we walk the structure generically.
    """
    payload = json.loads(payload_str)
    if not isinstance(payload, dict):
        return []
    texts: list[str] = []
    for lang_val in payload.values():
        if not isinstance(lang_val, dict):
            continue
        content = lang_val.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, list):
                candidates = block
            else:
                candidates = [block]
            for el in candidates:
                if isinstance(el, dict) and el.get("tag") == "md":
                    texts.append(el.get("text", ""))
    return texts


def test_markdown_table_uses_post_not_text():
    """Regression test for issue #52786 (and its older sibling #23938).

    A message whose only markdown is a table must take the ``post`` path,
    not be downgraded to plain text.
    """
    content = (
        "| col A | col B |\n"
        "| ----- | ----- |\n"
        "| 1     | 2     |"
    )
    msg_type, payload_str = _call_build_outbound_payload(content)
    assert msg_type == "post", (
        f"expected 'post' for a markdown table (issue #52786), got {msg_type!r}; "
        "the table-downgrade branch in _build_outbound_payload has been re-introduced"
    )
    md_texts = _md_texts_from_post_payload(payload_str)
    assert md_texts, f"post payload must include at least one md element; got {payload_str!r}"
    joined = "".join(md_texts)
    assert "col A" in joined and "|" in joined, (
        "table text was lost or reformatted when switching from text to post"
    )


def test_plain_text_without_markdown_still_uses_text():
    """Negative control: a message with no markdown hints and no table must
    still go to plain text.  Guards against accidentally promoting everything
    to ``post``."""
    msg_type, _ = _call_build_outbound_payload("just a plain sentence with no markup")
    assert msg_type == "text"


def test_existing_markdown_heading_still_uses_post():
    """Sanity: the existing ``post`` path (heading / list / code / bold /
    link) must still work after the table downgrade is removed."""
    msg_type, payload_str = _call_build_outbound_payload("# hello world\n")
    assert msg_type == "post"
    md_texts = _md_texts_from_post_payload(payload_str)
    assert md_texts, f"expected at least one md element; got {payload_str!r}"
    assert any("hello world" in t for t in md_texts), (
        f"expected 'hello world' in md elements; got {md_texts!r}"
    )


def test_table_combined_with_other_markdown_does_not_downgrade():
    """A message that mixes a table with surrounding markdown must also
    take the ``post`` path.

    The old ``_MARKDOWN_TABLE_RE`` branch returned ``text`` unconditionally
    and stripped all the surrounding markdown formatting, so a Feishu
    reader saw literal pipes and lost the prose framing the table.
    """
    content = (
        "Here is the data:\n\n"
        "| col A | col B |\n"
        "| ----- | ----- |\n"
        "| 1     | 2     |\n\n"
        "Let me know."
    )
    msg_type, payload_str = _call_build_outbound_payload(content)
    assert msg_type == "post"
    md_texts = _md_texts_from_post_payload(payload_str)
    joined = "\n".join(md_texts)
    assert "Here is the data" in joined, (
        "leading prose was lost when downgrading a mixed-table message"
    )
    assert "col A" in joined, "table header was lost"
    assert "Let me know" in joined, "trailing prose was lost"


# ---------------------------------------------------------------------------
# Outbound @peer-bot mention conversion (rework of #64234 per sweeper review)
# ---------------------------------------------------------------------------


def _seed_registry_file(tmp_path, registry: dict, app_id: str = "") -> None:
    """Write a peer-bot registry JSON at the path _peer_registry_path() will look up."""
    suffix = f"_{app_id}" if app_id else ""
    (tmp_path / f"feishu_peer_bots{suffix}.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )


def _bare_adapter(app_id: str = ""):
    """Bare adapter instance (no __init__) — matches how the standalone send
    path constructs a transient sender. Caller must wrap with
    set_hermes_home_override(...) so _peer_registry_path() resolves correctly."""
    inst = object.__new__(_adapter.FeishuAdapter)
    inst._app_id = app_id
    inst._peer_registry_cache = None
    inst._peer_registry_mtime = 0.0
    return inst


def _all_elements(payload_str: str) -> list[dict]:
    """Flatten every element across every row of a Feishu post payload."""
    payload = json.loads(payload_str)
    out: list[dict] = []
    for lang_val in payload.values():
        if not isinstance(lang_val, dict):
            continue
        for row in lang_val.get("content", []):
            if isinstance(row, list):
                out.extend(el for el in row if isinstance(el, dict))
            elif isinstance(row, dict):
                out.append(row)
    return out


def test_outbound_converts_known_peer_mention_to_at_element(tmp_path):
    """Markdown content with @KnownPeer converts to a post payload containing
    a ``{tag:"at", user_id:"ou_known"}`` element. Regression for #64234."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    _seed_registry_file(tmp_path, registry={"KnownPeer": "ou_known"})
    token = set_hermes_home_override(str(tmp_path))
    try:
        inst = _bare_adapter()
        content = "Hey @KnownPeer can you review?"
        msg_type, payload_str = inst._build_outbound_payload(content)
    finally:
        reset_hermes_home_override(token)
    assert msg_type == "post"
    elements = _all_elements(payload_str)
    at_elements = [e for e in elements if e.get("tag") == "at"]
    assert len(at_elements) == 1
    assert at_elements[0].get("user_id") == "ou_known"
    md_texts = [e.get("text", "") for e in elements if e.get("tag") == "md"]
    assert any("Hey" in t for t in md_texts)
    assert any("can you review?" in t for t in md_texts)


def test_outbound_leaves_unknown_mention_in_markdown_text(tmp_path):
    """An @name NOT in the registry stays inline in the md text — no at element
    is emitted. Guards against over-eager conversion."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    _seed_registry_file(tmp_path, registry={"KnownPeer": "ou_known"})
    token = set_hermes_home_override(str(tmp_path))
    try:
        inst = _bare_adapter()
        msg_type, payload_str = inst._build_outbound_payload("ping @UnknownBot now")
    finally:
        reset_hermes_home_override(token)
    # No markdown hint, no known mention → text path. Content preserved verbatim.
    assert msg_type == "text"
    assert json.loads(payload_str) == {"text": "ping @UnknownBot now"}


def test_outbound_no_registry_falls_back_to_plain_md(tmp_path):
    """With no peer registry file, behavior is identical to pre-feature: md
    payload, no at elements. Backward-compat for single-profile/no-mention use."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(str(tmp_path))
    try:
        inst = _bare_adapter()
        msg_type, payload_str = inst._build_outbound_payload("# heading\n@Someone")
    finally:
        reset_hermes_home_override(token)
    assert msg_type == "post"
    elements = _all_elements(payload_str)
    assert not [e for e in elements if e.get("tag") == "at"]
    assert [e for e in elements if e.get("tag") == "md"]


def test_outbound_mention_inside_markdown_table_preserves_table(tmp_path):
    """Mention conversion must NOT downgrade a markdown table. The table stays
    as md; the @KnownPeer becomes an at element within the same row."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    _seed_registry_file(tmp_path, registry={"KnownPeer": "ou_known"})
    token = set_hermes_home_override(str(tmp_path))
    try:
        inst = _bare_adapter()
        content = (
            "ping @KnownPeer — here is the data:\n\n"
            "| col A | col B |\n"
            "| ----- | ----- |\n"
            "| 1     | 2     |\n"
        )
        msg_type, payload_str = inst._build_outbound_payload(content)
    finally:
        reset_hermes_home_override(token)
    assert msg_type == "post"
    elements = _all_elements(payload_str)
    assert [e for e in elements if e.get("tag") == "at"], "at element missing"
    md_texts = "".join(e.get("text", "") for e in elements if e.get("tag") == "md")
    assert "col A" in md_texts and "col B" in md_texts, "table text dropped"


def test_outbound_mention_inside_fenced_code_block_is_left_alone(tmp_path):
    """A fenced code block is isolated into its own md row; @names inside the
    code block must NOT be converted (they're code, not chat mentions). Only
    mentions in prose segments get converted."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    _seed_registry_file(tmp_path, registry={"KnownPeer": "ou_known"})
    token = set_hermes_home_override(str(tmp_path))
    try:
        inst = _bare_adapter()
        content = (
            "Hey @KnownPeer look:\n\n"
            "```\n@KnownPeer this is code\n```\n\n"
            "end"
        )
        msg_type, payload_str = inst._build_outbound_payload(content)
    finally:
        reset_hermes_home_override(token)
    assert msg_type == "post"
    elements = _all_elements(payload_str)
    # Exactly ONE at element — from the prose mention, not the code block.
    at_elements = [e for e in elements if e.get("tag") == "at"]
    assert len(at_elements) == 1
    md_texts = [e.get("text", "") for e in elements if e.get("tag") == "md"]
    # The code block text must survive untouched, still containing @KnownPeer.
    assert any("@KnownPeer this is code" in t for t in md_texts)


def test_standalone_bare_adapter_reads_persisted_registry(tmp_path):
    """The standalone/cron send path creates a transient adapter via __new__
    (skipping _hydrate_bot_identity). It must still resolve peer mentions by
    reading the persisted file — that's the whole reason the registry is
    file-backed rather than instance-local."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    # Simulate: profile A's resident adapter hydrated itself AND observed a
    # peer inbound, writing both into the shared registry file.
    _seed_registry_file(
        tmp_path, registry={"ObservedPeer": "ou_obs"}, app_id="cli_app_123"
    )
    token = set_hermes_home_override(str(tmp_path))
    try:
        # Bare transient sender — no __init__, no hydration, no inbound history.
        transient = _bare_adapter(app_id="cli_app_123")
        msg_type, payload_str = transient._build_outbound_payload(
            "cc @ObservedPeer on this"
        )
    finally:
        reset_hermes_home_override(token)
    assert msg_type == "post"
    elements = _all_elements(payload_str)
    at_elements = [e for e in elements if e.get("tag") == "at"]
    assert len(at_elements) == 1
    assert at_elements[0].get("user_id") == "ou_obs"


def test_inbound_normalize_harvests_peer_mention_to_registry(tmp_path):
    """When an inbound message @-mentions a peer bot, the adapter records
    name→open_id into the persisted registry so future outbound sends can
    convert @name to a real <at> element. This is the source the sweeper
    asked for: peer identity available to both gateway and standalone paths."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from plugins.platforms.feishu.adapter import FeishuMentionRef

    token = set_hermes_home_override(str(tmp_path))
    try:
        inst = _bare_adapter()
        inst._bot_open_id = "ou_self"
        inst._bot_user_id = ""
        inst._bot_name = "Self"

        peer_ref = FeishuMentionRef(
            name="InboundPeer", open_id="ou_inbound", is_all=False, is_self=False
        )
        inst._harvest_peer_mentions([peer_ref])

        # File should now contain InboundPeer → ou_inbound.
        path = tmp_path / "feishu_peer_bots.json"
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted.get("InboundPeer") == "ou_inbound"
    finally:
        reset_hermes_home_override(token)
