---
name: memento-flashcards
description: >-
  Spaced-repetition flashcard system. Create cards from facts or text,
  chat with flashcards using free-text answers graded by the agent,
  generate quizzes from YouTube transcripts, review due cards with
  adaptive scheduling, and export/import decks as CSV.
version: 1.0.0
author: Memento AI
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [Education, Flashcards, Spaced Repetition, Learning, Quiz, YouTube]
    requires_toolsets: [terminal]
    category: productivity
---

# Memento Flashcards — Spaced-Repetition Flashcard Skill

## Overview

Memento gives you a local, file-based flashcard system with spaced-repetition scheduling.
Users can chat with their flashcards by answering in free text and having the agent grade the response before scheduling the next review.
Use it whenever the user wants to:

- **Remember a fact** — turn any statement into a Q/A flashcard
- **Study with spaced repetition** — review due cards with adaptive intervals and agent-graded free-text answers
- **Quiz from a YouTube video** — fetch a transcript and generate a 5-question quiz
- **Manage decks** — organise cards into collections, export/import CSV

All card data lives in a single JSON file. No external API keys are required — you (the agent) generate flashcard content and quiz questions directly.

User-facing response style for Memento Flashcards:
- Use plain text only. Do not use Markdown formatting in replies to the user.
- Keep review and quiz feedback brief and neutral. Avoid extra praise, pep, or long explanations.

## When to Use

Use this skill when the user wants to:
- Save facts as flashcards for later review
- Review due cards with spaced repetition
- Generate a quiz from a YouTube video transcript
- Import, export, inspect, or delete flashcard data

Do not use this skill for general Q&A, coding help, or non-memory tasks.

## Quick Reference

| User intent | Action |
|---|---|
| "Remember that X" / "save this as a flashcard" | Generate a Q/A card, call `memento_cards.py add` |
| Sends a fact without mentioning flashcards | Ask "Want me to save this as a Memento flashcard?" — only create if confirmed |
| "Create a flashcard" | Ask for Q, A, collection; call `memento_cards.py add` |
| "Review my cards" | Call `memento_cards.py due`, present cards one-by-one |
| "Quiz me on [YouTube URL]" | Call `youtube_quiz.py fetch VIDEO_ID`, generate 5 questions, call `memento_cards.py add-quiz` |
| "Export my cards" | Call `memento_cards.py export --output PATH` |
| "Import cards from CSV" | Call `memento_cards.py import --file PATH --collection NAME` |
| "Show my stats" | Call `memento_cards.py stats` |
| "Delete a card" | Call `memento_cards.py delete --id ID` |
| "Delete a collection" | Call `memento_cards.py delete-collection --collection NAME` |

## Card Storage

Cards are stored in a JSON file at:

```
~/.hermes/skills/productivity/memento-flashcards/data/cards.json
```

**Never edit this file directly.** Always use `memento_cards.py` subcommands. The script handles atomic writes (write to temp file, then rename) to prevent corruption.

The file is created automatically on first use.

## Procedure — routing table

Load the reference for the branch you are in. All scripts live in `scripts/memento_cards.py` and `scripts/youtube_quiz.py`.

| Intent | Do this |
|---|---|
| Turn a fact into a card; three-tier activation rules (explicit / implicit / no intent); Q/A generation format; manual card creation | read `references/card-creation.md` |
| Review due cards; exact free-text grading interaction pattern; feedback wording; `rate` command; retire override; spaced-repetition interval table | read `references/review-flow.md` |
| Quiz from a YouTube URL; transcript fetch; the 5-question generation rules; validation; `add-quiz`; one-by-one quiz presentation | read `references/youtube-quiz.md` |
| Export or import CSV decks; statistics output fields | read `references/decks-and-stats.md` |

## Red lines

- **Never edit `cards.json` directly.** Always go through `scripts/memento_cards.py` subcommands.
- **Never activate on tier 3 (no intent).** Coding tasks, questions, and normal conversation are not flashcards.
- **Never skip feedback.** Every answer the user gives MUST receive visible feedback — grade plus the correct answer — before the next question.
- **Plain text only.** No Markdown in replies to the user; keep feedback brief and neutral.

## Shortest end-to-end skeleton

```bash
CARDS=~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py

# 1. Store a card
python3 "$CARDS" add --question "What year did World War 2 end?" --answer "1945" --collection "History"

# 2. Fetch what is due, present one question, wait for the user's free-text answer
python3 "$CARDS" due

# 3. Tell the user the grade + correct answer, then record the rating
python3 "$CARDS" rate --id CARD_ID --rating easy --user-answer "what the user said"
```

## Pitfalls

- **Never edit `cards.json` directly** — always use the script subcommands to avoid corruption
- **Transcript failures** — some YouTube videos have no English transcript or have transcripts disabled; inform the user and suggest another video
- **Optional dependency** — `youtube_quiz.py` needs `youtube-transcript-api`; if missing, tell the user to run `pip install youtube-transcript-api`
- **Large imports** — CSV imports with thousands of rows work fine but the JSON output may be verbose; summarize the result for the user
- **Video ID extraction** — support both `youtube.com/watch?v=ID` and `youtu.be/ID` URL formats

## Verification

Verify the helper scripts directly:

```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py stats
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py add --question "Capital of France?" --answer "Paris" --collection "General"
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py due
```

If you are testing from the repo checkout, run:

```bash
pytest tests/skills/test_memento_cards.py tests/skills/test_youtube_quiz.py -q
```

Agent-level verification:
- Start a review and confirm feedback is plain text, brief, and always includes the correct answer before the next card
- Run a YouTube quiz flow and confirm each answer receives visible feedback before the next question
