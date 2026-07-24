# Memento — Export/Import CSV & Statistics

### Export/Import CSV

**Export:**
```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py export \
  --output ~/flashcards.csv
```

Produces a 3-column CSV: `question,answer,collection` (no header row).

**Import:**
```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py import \
  --file ~/flashcards.csv \
  --collection "Imported"
```

Reads a CSV with columns: question, answer, and optionally collection (column 3). If the collection column is missing, uses the `--collection` argument.

### Statistics

```bash
python3 ~/.hermes/skills/productivity/memento-flashcards/scripts/memento_cards.py stats
```

Returns JSON with:
- `total`: total card count
- `learning`: cards in active rotation
- `retired`: mastered cards
- `due_now`: cards due for review right now
- `collections`: breakdown by collection name
