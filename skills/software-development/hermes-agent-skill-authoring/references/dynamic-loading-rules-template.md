---
title: "Dynamic Loading Rules Section Template"
---

# Dynamic Loading Rules — body section template

**Architecture rule:** Progressive disclosure — SKILL.md body stays operational
core. Bulky material lives in `references/`, `scripts/`, `templates/` and is
loaded **by task scope**, never all at once. This is not a router/index/hub
skill and not a selectable "catalog" skill.

## When section is required vs optional

| Skill has… | Section |
|---|---|
| **2+** files under `references/` (or equivalent bulky docs the body only points at) | **Required** |
| One short reference + body already covers the workflow | Optional / omit (prefer a one-line "load when…" pointer) |
| No `references/`, only thin `scripts/` invoked via `terminal` | Optional / omit |
| Framework / multi-domain skill (OWASP-style categories, multi-family guides) | **Required** — priority-based loading is the whole point |

## What this section is *not*

| Anti-pattern | Why |
|---|---|
| A skill that is *only* a table of "load skill X / Y / Z" | Banned router/index/hub |
| "Read all of `references/` before starting" | Context blow-up; defeats progressive disclosure |
| Machine-local absolute paths | Review-blocking; use skill-relative `references/...` |
| Vague "see references as needed" with no scope→file map | Unactionable — agent loads everything or nothing |

## Template: paste into SKILL.md body

Replace bracketed placeholders. Keep rules as checkable if→then lines, not essays.

```markdown
## Dynamic Loading Rules

Progressive disclosure: keep this body as the operational core.
Load bundled resources **only when a rule below matches the current task**.
Default: **load no reference files** until a match. Prefer `read_file` on the
specific path; run scripts via `terminal` without pasting their source into context.

**Scripts are excluded from this table.** Deterministic helpers in `scripts/`
should be invoked via `terminal` without reading their source into context.
Only add a row here when a script requires a reference doc to interpret its output.

### Scope → resource map

| When the task involves… | Load (skill-relative) |
|---|---|
| [e.g. secret/credential scanning] | `references/secret-detection.md` (not other category files) |
| [e.g. access control only] | `references/a01-broken-access-control.md` (not full category set) |
| [e.g. multi-category review] | Shared methodology first (`references/methodology.md`), then only category files in scope |
| [e.g. compose with MML] | `references/message-composition.md` (not config deep-dives) |
| [e.g. first-time config] | `references/configuration.md` (not composition guide) |

Negative exclusions go inline in the Load cell only when the excluded set is
small and informative — do not invent a separate "Do not load" column.

### Priority and load discipline

1. Match the **narrowest** row that covers the user request (most-specific wins).
2. **As a heuristic, limit per-turn loads to 3–4 reference files** unless the
   user explicitly requests a full baseline. This is **not a hard rule**.
3. Prefer executing `scripts/<helper>.py` via `terminal` over `read_file` of the
   script source.
4. Never pre-load "just in case." Re-evaluate after each phase of the Procedure.

### How to load

```
read_file(path="references/<file>.md")
terminal(command="python3 scripts/<helper>.py ...", timeout=...)
```

Paths are **skill-relative** (under this skill's directory), never machine-local.

### Completion criterion (agent-facing)

Before heavy Procedure work: every loaded reference is justified by a row above;
unrelated references remain unread; prefer staying near the 3–4 file heuristic
unless the user explicitly requested a full pass.

### Author verification (author-facing)

Ensure **every file under `references/` is named in at least one row** of the
scope map. Unreferenced files are orphaned and should be removed or mapped.
```

## Relationship to Safety & Enforcement

| Concern | Where it lives | When it runs |
|---|---|---|
| Preconditions / PII / auth / rate limits | `## Safety & Enforcement` + `scripts/policy.py` | Every invocation before side effects |
| Which docs enter context | `## Dynamic Loading Rules` | Only when body alone is insufficient for the task |

Do not conflate them. Loading a reference is not a substitute for a code-level guard.

## Structural test skeleton

Runtime "did the model follow the rules?" is not CI-testable without a live
agent. Test what *is* deterministic: the skill package's structure and the
rules prose.

Conventions: stdlib + pytest + `unittest.mock` only, no live network.
Run: `scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q`

### Import / path note

Tests should locate the skill via **repo-relative** path
(`skills/<category>/<name>/` or `optional-skills/...`), never `$HOME`.

```python
"""Structural tests for Dynamic Loading Rules in <skill>."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Adjust to the skill under test (repo-relative from hermes-agent root).
SKILL_DIR = Path("skills/<category>/<name>")
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES = SKILL_DIR / "references"


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _reference_files() -> list[Path]:
    if not REFERENCES.is_dir():
        return []
    return sorted(p for p in REFERENCES.rglob("*.md") if p.is_file())


class TestDynamicLoadingRulesPresence:
    def test_multi_ref_skills_have_section(self):
        refs = _reference_files()
        if len(refs) < 2:
            pytest.skip("fewer than 2 reference files — section optional")
        text = _skill_text()
        assert re.search(r"^## Dynamic Loading Rules\s*$", text, re.M), (
            "skills with 2+ references/*.md must include ## Dynamic Loading Rules"
        )

    def test_section_declares_default_load_none_or_match_only(self):
        refs = _reference_files()
        if len(refs) < 2:
            pytest.skip("section not required")
        text = _skill_text()
        m = re.search(r"## Dynamic Loading Rules\n(.*?)(?=\n## |\Z)", text, re.S)
        assert m, "Dynamic Loading Rules section missing"
        body = m.group(1).lower()
        signals = ("load no", "only when", "when the task", "never pre-load", "default", "heuristic")
        assert any(s in body for s in signals), (
            "section must state default/scoped loading, not unbounded read of references/"
        )


class TestReferencePathsResolve:
    def test_backticked_reference_paths_exist(self):
        text = _skill_text()
        mentioned = set(re.findall(r"`(references/[^`]+)`", text))
        if not mentioned:
            pytest.skip("no backticked references/ paths in SKILL.md")
        missing = []
        for rel in mentioned:
            rel_path = rel.split("#", 1)[0]
            if not (SKILL_DIR / rel_path).is_file():
                missing.append(rel_path)
        assert not missing, f"SKILL.md references missing files: {missing}"

    def test_every_reference_file_is_mapped(self):
        refs = _reference_files()
        if len(refs) < 2:
            pytest.skip("section not required")
        text = _skill_text()
        orphans = []
        for path in refs:
            rel = path.relative_to(SKILL_DIR).as_posix()
            if rel not in text and path.name not in text:
                orphans.append(rel)
        assert not orphans, f"orphan references (not named in SKILL.md): {orphans}"

    def test_no_machine_local_reference_paths(self):
        text = _skill_text()
        bad = re.findall(r"`(/Users/[^`]+|/home/[^`]+|~/?\.hermes/skills/[^`]+)`", text)
        assert not bad, f"machine-local paths in skill: {bad}"


class TestNoLoadAllAntiPattern:
    def test_skill_md_avoids_load_all_phrasing(self):
        text = _skill_text().lower()
        banned = ["read all of references", "load all references", "load the entire references", "read every reference"]
        hits = [b for b in banned if b in text]
        assert not hits, f"load-all anti-pattern phrasing found: {hits}"


class TestScopeMapTableShape:
    def test_loading_rules_table_has_when_and_load_columns(self):
        refs = _reference_files()
        if len(refs) < 2:
            pytest.skip("section not required")
        text = _skill_text()
        m = re.search(r"## Dynamic Loading Rules\n(.*?)(?=\n## |\Z)", text, re.S)
        assert m
        section = m.group(1)
        assert "|" in section, "prefer a scope→resource table for scanability"
        header = section.lower()
        assert "when" in header or "task" in header
        assert "load" in header or "references/" in section
```

### Done checklist for loading-rule tests

- [ ] Section present when `len(references/*.md) >= 2`
- [ ] Every backticked `references/...` path in SKILL.md exists on disk
- [ ] Every file under `references/` is named somewhere in SKILL.md (no orphans)
- [ ] No machine-local paths
- [ ] No "load all references" phrasing
- [ ] Section states scoped / default-none discipline
- [ ] **Not required:** live model follow-through (document as residual risk in Pitfalls)
- [ ] **Not required:** numeric "≤4 files" as a CI assertion (heuristic only)

## Anti-patterns

1. Load-all default — "read everything in references/ first" — banned
2. Orphan references — files on disk never named in the scope map
3. Router skill disguised as loading rules — rules that only point at other skills
4. Inlining reference bodies back into SKILL.md — blows past the size target
5. Treating 3–4 files/turn as a hard rule or CI gate — it is a diagnostic heuristic
6. Testing only happy-path Procedure with zero structural checks on reference paths
7. Putting every `scripts/` helper in the scope table — only map when a ref is needed to interpret output