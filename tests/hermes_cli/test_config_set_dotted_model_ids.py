"""Dotted model IDs must survive `hermes config set` key resolution (#91607).

Model IDs routinely contain dots (`glm-5.3`, `gpt-5.5`,
`claude-opus-4.5`). Splitting every dot as a nesting delimiter silently
wrote a different hierarchy (`glm-5` → `3`) while printing success — the
setting had no effect because the runtime looks up the literal model ID.

Fix contract:
- an existing literal key spanning several segments wins (edit case);
- quoted segments (`zai."glm-5.3".x`) keep dots literally (create case);
- plain legacy paths behave exactly as before.
"""

import pytest

from hermes_cli.config import (
    _MISSING,
    _get_nested,
    _set_nested,
    _split_dotted_key,
    _unset_nested,
)


# ─────────────────────────────────────────────────────────────────────
# Segment splitter
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key,expected", [
    ("terminal.backend", ["terminal", "backend"]),
    ("model_overrides.zai.glm-5.3.supports_reasoning",
     ["model_overrides", "zai", "glm-5", "3", "supports_reasoning"]),
    ('model_overrides.zai."glm-5.3".supports_reasoning',
     ["model_overrides", "zai", "glm-5.3", "supports_reasoning"]),
    ("model_overrides.zai.'gpt-5.5'.max_tokens",
     ["model_overrides", "zai", "gpt-5.5", "max_tokens"]),
])
def test_split_dotted_key(key, expected):
    assert _split_dotted_key(key) == expected


# ─────────────────────────────────────────────────────────────────────
# Existing literal keys win over splitting (the common edit case)
# ─────────────────────────────────────────────────────────────────────


def test_set_descends_through_existing_dotted_model_key():
    cfg = {"model_overrides": {"zai": {"glm-5.3": {"supports_reasoning": False}}}}

    _set_nested(cfg, "model_overrides.zai.glm-5.3.supports_reasoning", True)

    assert cfg["model_overrides"]["zai"]["glm-5.3"]["supports_reasoning"] is True
    assert "glm-5" not in cfg["model_overrides"]["zai"], (
        "must not create a nested glm-5/3 hierarchy beside the literal key"
    )


def test_get_resolves_existing_dotted_model_key():
    cfg = {"model_overrides": {"zai": {"glm-5.3": {"max_tokens": 4096}}}}

    assert _get_nested(cfg, "model_overrides.zai.glm-5.3.max_tokens") == 4096


def test_unset_removes_leaf_under_existing_dotted_model_key():
    cfg = {
        "model_overrides": {
            "zai": {"glm-5.3": {"max_tokens": 4096, "enabled": True}}
        }
    }

    assert _unset_nested(cfg, "model_overrides.zai.glm-5.3.max_tokens") is True
    assert _get_nested(cfg, "model_overrides.zai.glm-5.3.max_tokens") is _MISSING
    # The dotted model container survives with its sibling content — and no
    # nested glm-5/3 garbage was ever created.
    assert cfg["model_overrides"]["zai"]["glm-5.3"] == {"enabled": True}
    assert "glm-5" not in cfg["model_overrides"]["zai"]


def test_longest_literal_key_wins():
    cfg = {"a": {"b.c.d": {"leaf": 1}, "b.c": {"other": 2}}}

    # "a.b.c.d.leaf" must descend through literal "b.c.d", not "b.c" + new "d".
    _set_nested(cfg, "a.b.c.d.leaf", 2)
    assert cfg["a"]["b.c.d"]["leaf"] == 2

    # And reads of "a.b.c.other" still resolve through literal "b.c".
    assert _get_nested(cfg, "a.b.c.other") == 2


# ─────────────────────────────────────────────────────────────────────
# Quoted-segment creation path
# ─────────────────────────────────────────────────────────────────────


def test_quoted_segment_creates_single_model_key():
    cfg = {}
    _set_nested(cfg, 'model_overrides.zai."glm-5.3".supports_reasoning', True)

    assert cfg == {
        "model_overrides": {"zai": {"glm-5.3": {"supports_reasoning": True}}}
    }
    assert _get_nested(cfg, 'model_overrides.zai."glm-5.3".supports_reasoning') is True


def test_quoted_segment_roundtrip_unset():
    cfg = {"overrides": {"keep": 1}}
    _set_nested(cfg, 'overrides."claude-opus-4.5".enabled', True)
    assert _unset_nested(cfg, 'overrides."claude-opus-4.5".enabled') is True
    # The quoted model key is pruned when empty; unrelated siblings survive.
    assert cfg == {"overrides": {"keep": 1}}


# ─────────────────────────────────────────────────────────────────────
# Legacy behavior unchanged
# ─────────────────────────────────────────────────────────────────────


def test_legacy_plain_paths_unchanged():
    cfg = {}
    _set_nested(cfg, "terminal.backend", "docker")
    assert cfg == {"terminal": {"backend": "docker"}}

    cfg_list = {"providers": [{"name": "a"}, {"name": "b"}]}
    _set_nested(cfg_list, "providers.1.name", "c")
    assert cfg_list["providers"][1]["name"] == "c"
    assert _get_nested(cfg_list, "providers.0.name") == "a"


# ─────────────────────────────────────────────────────────────────────
# Unset through lists: container-first parent pairing + pruning
# (sweeper review on #91607 fix — parents must record the CONTAINER)
# ─────────────────────────────────────────────────────────────────────


def test_unset_through_list_pops_empty_slot():
    cfg = {"providers": [{"name": "a", "opts": {}}, {"name": "b"}]}
    # Removing the last key inside providers[0].opts empties opts...
    assert _unset_nested(cfg, "providers.0.opts") is True
    assert _get_nested(cfg, "providers.0.opts") is _MISSING
    # ...and pruning removes name too, the whole empty slot is popped.
    assert _unset_nested(cfg, "providers.0.name") is True
    assert cfg == {"providers": [{"name": "b"}]}


def test_unset_through_list_chain_prunes_to_root_value():
    cfg = {"deep": [{"inner": {"leaf": 1}}]}
    assert _unset_nested(cfg, "deep.0.inner.leaf") is True
    # leaf → inner pruned → slot 0 popped → deep becomes an empty list;
    # user-authored empty lists are preserved, so only `deep` remains.
    assert cfg == {"deep": []}


def test_unset_through_list_keeps_nonempty_siblings():
    cfg = {"routes": [{"path": "/x", "keep": 1}, {"path": "/y"}]}
    assert _unset_nested(cfg, "routes.0.path") is True
    assert cfg["routes"][0] == {"keep": 1}
    assert len(cfg["routes"]) == 2


# ─────────────────────────────────────────────────────────────────────
# Scalar literal keys: readable when final, never over-consuming
# ─────────────────────────────────────────────────────────────────────


def test_scalar_literal_key_readable_as_final_target():
    cfg = {"x.y": 5}
    assert _get_nested(cfg, "x.y") == 5


def test_scalar_literal_key_does_not_swallow_descending_segments():
    cfg = {"x.y": 5}
    # x → y → z cannot resolve through a scalar; no over-consumption.
    assert _get_nested(cfg, "x.y.z") is _MISSING
