# Memento — YouTube Quiz Generation

### YouTube Quiz Generation

When the user sends a YouTube URL and wants a quiz:

**Step 1:** Extract the video ID from the URL (e.g. `dQw4w9WgXcQ` from `https://www.youtube.com/watch?v=dQw4w9WgXcQ`).

**Step 2:** Fetch the transcript:

```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/youtube_quiz.py fetch VIDEO_ID
```

This returns `{"title": "...", "transcript": "..."}` or an error.

If the script reports `missing_dependency`, tell the user to install it:
```bash
pip install youtube-transcript-api
```

**Step 3:** Generate 5 quiz questions from the transcript. Use these rules:

```
You are creating a 5-question quiz for a podcast episode.
Return ONLY a JSON array with exactly 5 objects.
Each object must contain keys 'question' and 'answer'.

Selection criteria:
- Prioritize important, surprising, or foundational facts.
- Skip filler, obvious details, and facts that require heavy context.
- Never return true/false questions.
- Never ask only for a date.

Question rules:
- Each question must test exactly one discrete fact.
- Use clear, unambiguous wording.
- Prefer What, Who, How many, Which.
- Avoid open-ended Describe or Explain prompts.

Answer rules:
- Each answer must be under 240 characters.
- Lead with the answer itself, not preamble.
- Add only minimal clarifying detail if needed.
```

Use the first 15,000 characters of the transcript as context. Generate the questions yourself (you are the LLM).

**Step 4:** Validate the output is valid JSON with exactly 5 items, each having non-empty `question` and `answer` strings. If validation fails, retry once.

**Step 5:** Store quiz cards:

```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py add-quiz \
  --video-id "VIDEO_ID" \
  --questions '[{"question":"...","answer":"..."},...]' \
  --collection "Quiz - Episode Title"
```

The script deduplicates by `video_id` — if cards for that video already exist, it skips creation and reports the existing cards.

**Step 6:** Present questions one-by-one using the same free-text grading flow:
1. Show "Question 1/5: ..." and wait for the user's answer. Never include the answer or any hint about revealing it.
2. Wait for the user to answer in their own words
3. Grade their answer using the grading prompt (see "Reviewing Due Cards" section)
4. **IMPORTANT: You MUST reply to the user with feedback before doing anything else.** Show the grade, the correct answer, and when the card is next due. Do NOT silently skip to the next question. Keep it short and plain-text. Example: "Not quite. Answer: {answer}. Next review tomorrow."
5. **After showing feedback**, call the rate command and then show the next question in the same message:
```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py rate \
  --id CARD_ID --rating easy --user-answer "what the user said"
```
6. Repeat. Every answer MUST receive visible feedback before the next question.
