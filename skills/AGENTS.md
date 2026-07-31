# Skill Authoring Guide

Root [`AGENTS.md`](../AGENTS.md) still applies. This policy governs both
`skills/` and sibling `optional-skills/`; optional/heavy skills do not need a
duplicate policy file.

## Placement

Use `skills/` for built-in, broadly useful skills and `optional-skills/` for
heavier or niche capabilities. Scripts live in `scripts/`, references in
`references/`, and templates in `templates/` inside the skill directory.

## Frontmatter

- `description` is one sentence, at most 60 characters, ends with a period,
  describes capability rather than marketing.
- Audit `platforms:` against the scripts' real imports and commands. Prefer a
  cross-platform implementation; otherwise declare the actual OS set.
- Credit the human contributor first in `author`.
- Use the supported Hermes metadata fields for tags, category, related skills,
  and setup configuration.

## Instruction surface

Use the modern order: title and short introduction, `When to Use`,
`Prerequisites`, `How to Run`, `Quick Reference`, `Procedure`, `Pitfalls`,
`Verification`.

Skill prose names native Hermes tools or an explicitly configured MCP server.
Do not lead with shell substitutes for native tools. Third-party CLIs may be
invoked by a bundled script through `terminal`.

Keep progressive disclosure: the common path comes first. Ship parsers and
non-trivial repeated logic as scripts rather than asking the model to recreate
them.

## Tests and environment

Tests live at `tests/skills/test_<skill>_skill.py`, use stdlib, pytest, and
`unittest.mock`, and make no live network calls. Run them through
`scripts/run_tests.sh`.

Keep `.env.example` changes inside the skill's own clearly delimited block.
Credentials are collected through supported setup metadata and never through a
gateway conversation.

Full contributor guidance:
[`website/docs/developer-guide/creating-skills.md`](../website/docs/developer-guide/creating-skills.md).
