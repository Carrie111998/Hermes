---
name: humanizer
description: "Humanize text: strip AI-isms and add real voice."
version: 2.5.1
author: Siqi Chen (@blader, https://github.com/blader/humanizer), ported by Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [writing, editing, humanize, anti-ai-slop, voice, prose, text]
    category: creative
    homepage: https://github.com/blader/humanizer
    related_skills: [songwriting-and-ai-music]
---

# Humanizer: Remove AI Writing Patterns

Identify and remove signs of AI-generated text to make writing sound natural and human. Based on Wikipedia's "Signs of AI writing" guide (maintained by WikiProject AI Cleanup), derived from observations of thousands of AI-generated text instances.

**Key insight:** LLMs use statistical algorithms to guess what should come next. The result tends toward the most statistically likely completion, which is how the telltale patterns below get baked in.

## When to use this skill

Load this skill whenever the user asks to:
- "humanize", "de-AI", "de-slop", or "un-ChatGPT" a piece of text
- rewrite something so it doesn't sound like it was written by an LLM
- edit a draft (blog post, essay, PR description, docs, memo, email, tweet, resume bullet) to sound more natural
- match their voice in writing they're producing
- review text for AI tells before publishing

Also apply this skill to **your own** output when writing user-facing prose — release notes, PR descriptions, documentation, long-form explanations, summaries. Hermes's baseline voice already strips most of these, but a focused pass catches what slips through.

## Reference map

| To do this | Read |
|---|---|
| Scan for AI tells — all 29 patterns, words to watch, before/after fixes | `references/pattern-catalog.md` |
| Match a user's writing sample, or add real voice instead of sterile prose | `references/voice-and-soul.md` |
| See the detailed 10-step process and a complete slop-to-human rewrite | `references/worked-example.md` |
| Credit the upstream source, license, or explain what the Hermes port changed | `references/attribution.md` |

Load `pattern-catalog.md` for any real editing pass — it is the substance of this skill. Load `voice-and-soul.md` whenever the user supplied a voice sample or the text is first-person/opinion writing.

## How the text arrives

1. **Inline** — user pastes the text directly into the message. Work on it in-place, reply with the rewrite.
2. **File** — user points at a file. Use `read_file` to load it, then `patch` or `write_file` to apply edits. For markdown docs in a repo, a targeted `patch` per section is cleaner than rewriting the whole file.
3. **Voice calibration sample** — user provides an additional sample of their own writing (inline or by file path) and asks you to match it. Read the sample first, then rewrite. See `references/voice-and-soul.md`.

## Non-negotiables

- **Always show the rewrite to the user.** For file edits, show a diff or the changed section — never silently overwrite.
- **Preserve meaning.** Removing AI-isms must not drop facts, claims, caveats, or citations. If a sentence only exists to inflate significance, cutting it is correct; if it carries information, rewrite it instead.
- **Never invent facts to replace vagueness.** The catalog's "after" examples add specifics because the source had them. Do not fabricate names, dates, studies, or numbers to make prose sound concrete — ask the user instead.
- **Match the intended tone** (formal, casual, technical). If a voice sample was provided, match the sample, not this skill's default voice.
- **Do the final anti-AI pass.** Never hand back the first draft as the final answer.

## Minimal workflow

1. Load `references/pattern-catalog.md` and scan the text for the 29 patterns.
2. Rewrite the problematic sections, preserving meaning.
3. Add voice — opinions, varied rhythm, first person where it fits (`references/voice-and-soul.md`).
4. Present the draft.
5. Ask yourself: "What makes the below so obviously AI generated?" Answer briefly with the remaining tells.
6. Revise once more and present the final version.
7. If the text came from a file, apply the edit with `patch` (targeted) or `write_file` (full rewrite) and show what changed.

## Output format

1. Draft rewrite
2. "What makes the below so obviously AI generated?" (brief bullets)
3. Final rewrite
4. A brief summary of changes made (optional, if helpful)

## Attribution

Ported from [blader/humanizer](https://github.com/blader/humanizer) (MIT), itself based on [Wikipedia: Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing). Full provenance in `references/attribution.md`; original license in `LICENSE`.
