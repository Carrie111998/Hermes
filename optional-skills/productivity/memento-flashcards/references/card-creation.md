# Memento — Card Creation

### Creating Cards from Facts

### Activation Rules

Not every factual statement should become a flashcard. Use this three-tier check:

1. **Explicit intent** — the user mentions "memento", "flashcard", "remember this", "save this card", "add a card", or similar phrasing that clearly requests a flashcard → **create the card directly**, no confirmation needed.
2. **Implicit intent** — the user sends a factual statement without mentioning flashcards (e.g. "The speed of light is 299,792 km/s") → **ask first**: "Want me to save this as a Memento flashcard?" Only create the card if the user confirms.
3. **No intent** — the message is a coding task, a question, instructions, normal conversation, or anything that is clearly not a fact to memorize → **do NOT activate this skill at all**. Let other skills or default behavior handle it.

When activation is confirmed (tier 1 directly, tier 2 after confirmation), generate a flashcard:

**Step 1:** Turn the statement into a Q/A pair. Use this format internally:

```
Turn the factual statement into a front-back pair.
Return exactly two lines:
Q: <question text>
A: <answer text>

Statement: "{statement}"
```

Rules:
- The question should test recall of the key fact
- The answer should be concise and direct

**Step 2:** Call the script to store the card:

```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py add \
  --question "What year did World War 2 end?" \
  --answer "1945" \
  --collection "History"
```

If the user doesn't specify a collection, use `"General"` as the default.

The script outputs JSON confirming the created card.

### Manual Card Creation

When the user explicitly asks to create a flashcard, ask them for:
1. The question (front of card)
2. The answer (back of card)
3. The collection name (optional — default to `"General"`)

Then call `memento_cards.py add` as above.
