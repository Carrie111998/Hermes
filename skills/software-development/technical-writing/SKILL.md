---
name: technical-writing
description: "Checklist for Hermes docs, runbooks, AGENTS.md, PR descriptions, and commit messages. Not product UI copy."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [docs, writing, pull-requests, contributing, gardener]
    related_skills: [hermes-agent-skill-authoring, requesting-code-review, github-pr-workflow]
---

# Technical writing checklist

Use this when writing or reviewing Hermes documentation and PR prose: `README.md`, `CONTRIBUTING.md`, `AGENTS.md`, `website/docs/`, runbooks, PR descriptions, and commit messages. Do not use it for product UI copy.

There is no separate docs-gardener tree in this repo. Load this skill instead.

## 1. Pick one document mode

Choose first. Do not mix modes in one page.

| Mode | Job |
|---|---|
| Tutorial | Walk a new reader through one successful path |
| How-to | Solve one concrete task |
| Reference | List facts, flags, paths, and contracts |
| Explanation | Why the design is this way |

`CONTRIBUTING.md` is mostly how-to plus reference. `website/docs/developer-guide/architecture.md` is explanation. A PR description is how-to: what changed, how to test.

## 2. Write like a developer talking to a developer

- Lead with the action: `Run scripts/run_tests.sh`, not "You may want to consider running the test script."
- One thought per sentence. If you need "and" for a second instruction, start a new sentence.
- Name the thing. Prefer `hermes_cli/skills_hub.py` over "the skills module" when a path exists.
- Prefer the imperative in procedures. Prefer the present tense in reference.

## 3. Remove ambiguous grammar

Write for readers who do not share your first language.

- Do not use "this" or "it" when two nouns are in play. Repeat the noun.
- Do not use "should" when you mean must or must not. Say the rule.
- Do not stack hedges ("might possibly", "fairly unique").
- Do not use unexplained we/our. Name the actor: the CLI, the gateway, the reviewer.

## 4. Ground every symbol in this repo

- Paths, commands, flags, and env vars must exist on the branch you are editing. Open the file or run the command before you cite it.
- Do not invent counts. If you need a count, show the command that produced it on this branch.
- For generated or changing counts, paste the regeneration command next to the number.

Examples of honest count commands:

```bash
git grep -l "HERMES_HOME" -- skills | wc -l
rg -c "def should_allow_install" tools/skills_guard.py
```

Do not copy those numbers into a doc unless you just ran the command.

## Procedure

1. Name the document mode in a heading or the first sentence.
2. State the reader and the outcome.
3. Put each instruction in its own sentence.
4. Check every path and command against the tree.
5. Drop any count you cannot regenerate.
6. Read once for leftover "this/it/should/might".

## Before / after

Bad (mixed mode, hedge, stale path, invented count):

```markdown
You might want to look at the skills hub if things feel off. This has about
12 install paths and they are all documented in docs/skills.md.
```

Good (how-to, one thought, real paths, no count):

```markdown
If `hermes skills install` exits non-zero after a successful write, read the
scan-report print in `hermes_cli/skills_hub.py`. The report is built by
`format_scan_report()` in `tools/skills_guard.py`.
```

Bad (PR description):

```markdown
Improved various docs and fixed some stuff around skills.
```

Good (PR description):

```markdown
Add `skills/software-development/technical-writing/SKILL.md` for docs and PR
prose. Link it from CONTRIBUTING.md under Pull Request Process. No code
paths change. Closes #78355.
```

## Pitfalls

- Do not import Cursor-only skills, slash commands, or runtime names.
- Do not turn this checklist into a style essay. Keep it short.
- Do not "fix" UI strings with this skill.

## Verification

- Every path in the draft exists (`test -e <path>` or open the file).
- Every command in the draft is copied from the repo or from `--help` on this branch.
- A second reader can tell the document mode from the first heading.
