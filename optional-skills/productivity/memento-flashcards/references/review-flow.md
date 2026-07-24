# Memento — Review Flow & Spaced Repetition

### Reviewing Due Cards

When the user wants to review, fetch all due cards:

```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py due
```

This returns a JSON array of cards where `next_review_at <= now`. If a collection filter is needed:

```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py due --collection "History"
```

**Review flow (free-text grading):**

Here is an example of the EXACT interaction pattern you must follow. The user answers, you grade them, tell them the correct answer, then rate the card.

**Example interaction:**

> **Agent:** What year did the Berlin Wall fall?
>
> **User:** 1991
>
> **Agent:** Not quite. The Berlin Wall fell in 1989. Next review is tomorrow.
> *(agent calls: memento_cards.py rate --id ABC --rating hard --user-answer "1991")*
>
> Next question: Who was the first person to walk on the moon?

**The rules:**

1. Show only the question. Wait for the user to answer.
2. After receiving their answer, compare it to the expected answer and grade it:
   - **correct** → user got the key fact right (even if worded differently)
   - **partial** → right track but missing the core detail
   - **incorrect** → wrong or off-topic
3. **You MUST tell the user the correct answer and how they did.** Keep it short and plain-text. Use this format:
   - correct: "Correct. Answer: {answer}. Next review in 7 days."
   - partial: "Close. Answer: {answer}. {what they missed}. Next review in 3 days."
   - incorrect: "Not quite. Answer: {answer}. Next review tomorrow."
4. Then call the rate command: correct→easy, partial→good, incorrect→hard.
5. Then show the next question.

```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py rate \
  --id CARD_ID --rating easy --user-answer "what the user said"
```

**Never skip step 3.** The user must always see the correct answer and feedback before you move on.

If no cards are due, tell the user: "No cards due for review right now. Check back later!"

**Retire override:** At any point the user can say "retire this card" to permanently remove it from reviews. Use `--rating retire` for this.

### Spaced Repetition Algorithm

The rating determines the next review interval:

| Rating | Interval | ease_streak | Status change |
|---|---|---|---|
| **hard** | +1 day | reset to 0 | stays learning |
| **good** | +3 days | reset to 0 | stays learning |
| **easy** | +7 days | +1 | if ease_streak >= 3 → retired |
| **retire** | permanent | reset to 0 | → retired |

- **learning**: card is actively in rotation
- **retired**: card won't appear in reviews (user has mastered it or manually retired it)
- Three consecutive "easy" ratings automatically retire a card
