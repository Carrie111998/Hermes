"""Tests for the skill_manage tool schema shape.

Guards the description text that spells out per-action required params
for `skill_manage` — the load-bearing fix for description-driven models
(e.g. Grok/DeepSeek) that omit required params when the schema only lists
`action`/`name` in `required[]` without stating what each action itself
needs. Mirrors tests/cron/test_cronjob_schema.py for the analogous
cronjob_tools.py fix.

`edit` was retired upstream (#95697) — `patch` now absorbs full-rewrite
via `content` alone (mutually exclusive with `old_string`/`new_string`),
so `patch` has either/or requirements rather than a flat AND-list like
every other action. It's tested separately from the simple actions below.
"""

import pytest

from tools.skill_manager_tool import SKILL_MANAGE_SCHEMA

ACTIONS_IN_ORDER = ["create", "patch", "delete", "write_file", "remove_file"]

# Simple actions: a flat AND-list of extra fields required beyond the
# globally-required `action`/`name`. `patch` is excluded — its requirement
# is either/or, not AND, and gets its own test below.
SIMPLE_REQUIRED_EXTRA_FIELDS_BY_ACTION = {
    "create": ["content"],
    "delete": [],
    "write_file": ["file_path", "file_content"],
    "remove_file": ["file_path"],
}

ACTION_SPECIFIC_FIELDS = {"content", "old_string", "new_string", "file_path", "file_content"}

# Every (field, action) pair where the field's own description should
# independently name that action as needing it — the cross-check source of
# truth, decoupled from the required[] AND/OR shape above since content is
# required by create (AND) but only conditionally by patch (OR-branch).
FIELD_ACTION_CONSISTENCY_PAIRS = [
    ("content", "create"),
    ("content", "patch"),
    ("old_string", "patch"),
    ("new_string", "patch"),
    ("file_path", "write_file"),
    ("file_content", "write_file"),
    ("file_path", "remove_file"),
]


def _action_description() -> str:
    return SKILL_MANAGE_SCHEMA["parameters"]["properties"]["action"]["description"]


def test_skill_manage_schema_action_description_has_required_params_header():
    """`action` description must lead with the per-action requirements callout."""
    assert "Required params per action" in _action_description()


def test_skill_manage_schema_action_enum_matches_documented_actions():
    """Every enum value must be documented — catches a new action added to
    `enum` without updating the description (drift a hardcoded action list
    wouldn't catch), and catches a retired action (like 'edit') lingering
    in the description after removal from `enum`."""
    enum_actions = SKILL_MANAGE_SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert set(enum_actions) == set(ACTIONS_IN_ORDER)
    action_desc = _action_description()
    for action in enum_actions:
        assert action in action_desc


def _clause_for(action: str) -> str:
    """Slice out the description text covering just this action's clause.

    Actions are documented in enum order, each as "<action> (...)"; slicing
    between one action's name and the next (rather than paren-matching) is
    robust to clauses containing their own parens.
    """
    desc = _action_description()
    start = desc.index(f"{action} (")
    idx = ACTIONS_IN_ORDER.index(action)
    if idx + 1 < len(ACTIONS_IN_ORDER):
        end = desc.index(f"{ACTIONS_IN_ORDER[idx + 1]} (", start)
    else:
        end = len(desc)
    return desc[start:end]


@pytest.mark.parametrize("action,extra_fields", SIMPLE_REQUIRED_EXTRA_FIELDS_BY_ACTION.items())
def test_skill_manage_schema_per_action_requirements(action, extra_fields):
    """Each simple action's clause must open with "requires: name" and
    mention every extra field it needs somewhere in its clause (not
    necessarily contiguous — e.g. write_file's clause interleaves an
    inline example between file_path and file_content)."""
    clause = _clause_for(action)
    assert clause.startswith(f"{action} (requires: name")
    for field in extra_fields:
        assert field in clause, f"{action}'s clause doesn't mention required field {field!r}: {clause!r}"


def test_skill_manage_schema_patch_documents_either_or_requirement():
    """`patch`'s clause must mention name plus both the targeted-replacement
    fields and the full-rewrite field, and signal they're mutually
    exclusive alternatives rather than an AND-list like every other
    action — otherwise a model can't tell it shouldn't pass both."""
    clause = _clause_for("patch")
    assert clause.startswith("patch (requires: name")
    for field in ("old_string", "new_string", "content"):
        assert field in clause, f"patch's clause doesn't mention {field!r}: {clause!r}"
    assert "not both" in clause or "either" in clause, (
        f"patch's clause doesn't signal old_string/new_string and content "
        f"are mutually exclusive alternatives: {clause!r}"
    )


@pytest.mark.parametrize("field,action", FIELD_ACTION_CONSISTENCY_PAIRS)
def test_skill_manage_schema_field_descriptions_agree_with_action_summary(field, action):
    """The `action` description and each individual field's own description
    are two independent sources of the same requirement — nothing enforces
    they stay in sync. A field description could drop its "required for X"
    callout (or the action summary could drift) without the tests above
    noticing, since each only checks one side. This checks both agree: for
    every (field, action) pair the action summary implies, that field's
    own description must also name the action.
    """
    field_desc = SKILL_MANAGE_SCHEMA["parameters"]["properties"][field]["description"]
    assert f"'{action}'" in field_desc, (
        f"{field!r}'s own description doesn't mention {action!r}, but the "
        f"action summary implies {action!r} needs {field!r}"
    )


def test_skill_manage_schema_required_array_stays_minimal():
    """`required[]` stays minimal — only the fields required by every
    action, i.e. `action` and `name`.

    The schema intentionally does NOT promote action-specific fields into
    the top-level required array because they're only mandatory for
    specific actions, not universally — the description text carries the
    conditional requirement instead, same pattern as CRONJOB_SCHEMA. This
    guards against the naive "fix" of a model-omission bug by just adding
    a field to `required[]`, which would break every action that doesn't
    need that field.
    """
    required = set(SKILL_MANAGE_SCHEMA["parameters"]["required"])
    assert required == {"action", "name"}
    assert not required & ACTION_SPECIFIC_FIELDS
