"""
Applies the relative-time resolution guidance patch to:
  - plugins/memory/hindsight/__init__.py  (UPDATE_MEMORY_SCHEMA description + property docs)
  - tests/plugins/memory/test_hindsight_provider.py  (new schema test)

Run from the repo root:
    python apply_relative_time_patch.py

Each edit is applied via a unique, exact string match. If a match isn't
found (or isn't unique), the script stops and reports which edit failed
instead of silently corrupting the file.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

PLUGIN_PATH = REPO_ROOT / "plugins" / "memory" / "hindsight" / "__init__.py"
TEST_PATH = REPO_ROOT / "tests" / "plugins" / "memory" / "test_hindsight_provider.py"


def apply_edit(path: Path, old: str, new: str, label: str) -> None:
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    count = text.count(old)
    if count == 0:
        print(f"[FAIL] {label}: old text not found in {path}")
        print("       (maybe already applied, or the file has drifted — check manually)")
        sys.exit(1)
    if count > 1:
        print(f"[FAIL] {label}: old text matched {count} times in {path} (expected exactly 1)")
        sys.exit(1)
    text = text.replace(old, new, 1)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"[OK]   {label}")


def main() -> None:
    if not PLUGIN_PATH.exists():
        print(f"[FAIL] Not found: {PLUGIN_PATH} — run this script from the repo root.")
        sys.exit(1)
    if not TEST_PATH.exists():
        print(f"[FAIL] Not found: {TEST_PATH} — run this script from the repo root.")
        sys.exit(1)

    # --- Edit 1: UPDATE_MEMORY_SCHEMA description ---------------------------
    old_description = '''    "description": (
        "Correct or time-anchor a memory already stored via hindsight_retain. "
        "Use occurred_start/occurred_end to set when the fact actually happened "
        "(retain always stamps write time, not event time), and/or text/context "
        "to fix the stored content. Only world/experience facts can be curated — "
        "observations are derived and regenerate from their sources, so updating "
        "one has no effect. Timestamps are ISO-8601 strings; pass an empty string "
        "to clear a field, omit a field to leave it unchanged."
    ),'''

    new_description = '''    "description": (
        "Correct or time-anchor a memory already stored via hindsight_retain. "
        "Use occurred_start/occurred_end to set when the fact actually happened "
        "(retain always stamps write time, not event time), and/or text/context "
        "to fix the stored content. Only world/experience facts can be curated — "
        "observations are derived and regenerate from their sources, so updating "
        "one has no effect. Timestamps are ISO-8601 strings; pass an empty string "
        "to clear a field, omit a field to leave it unchanged.\\n\\n"
        "Resolving relative times: compute against the current prompt time, not "
        "the memory's original write time. Roughly: 'yesterday' -> minus 24 "
        "hours, 'last week' -> minus 7 days, 'two hours ago' -> minus 2 hours, "
        "and so on for other relative phrasing. If the fact describes a single "
        "moment (an event, a discrete observation) rather than a span, set "
        "occurred_start and occurred_end to the same resolved timestamp. If it "
        "describes a duration, set occurred_start and occurred_end to the "
        "actual distinct start and end times — they don't have to match."
    ),'''

    apply_edit(PLUGIN_PATH, old_description, new_description, "UPDATE_MEMORY_SCHEMA description")

    # --- Edit 2: occurred_start / occurred_end property docs ----------------
    old_props = '''            "occurred_start": {"type": "string", "description": "ISO-8601 timestamp for when the fact started/occurred. Empty string clears it."},
            "occurred_end": {"type": "string", "description": "ISO-8601 timestamp for when the fact ended. Empty string clears it."},'''

    new_props = '''            "occurred_start": {"type": "string", "description": "ISO-8601 timestamp for when the fact started/occurred. For a point-in-time fact, same value as occurred_end. Empty string clears it."},
            "occurred_end": {"type": "string", "description": "ISO-8601 timestamp for when the fact ended. For a point-in-time fact, same value as occurred_start. Empty string clears it."},'''

    apply_edit(PLUGIN_PATH, old_props, new_props, "occurred_start/occurred_end property docs")

    # --- Edit 3: new test in test_hindsight_provider.py ----------------------
    old_test = '''    def test_update_memory_schema_requires_memory_id(self):
        assert UPDATE_MEMORY_SCHEMA["name"] == "hindsight_update_memory"
        props = UPDATE_MEMORY_SCHEMA["parameters"]["properties"]
        assert set(props) == {"memory_id", "occurred_start", "occurred_end", "text", "context"}
        assert UPDATE_MEMORY_SCHEMA["parameters"]["required"] == ["memory_id"]
'''

    new_test = '''    def test_update_memory_schema_requires_memory_id(self):
        assert UPDATE_MEMORY_SCHEMA["name"] == "hindsight_update_memory"
        props = UPDATE_MEMORY_SCHEMA["parameters"]["properties"]
        assert set(props) == {"memory_id", "occurred_start", "occurred_end", "text", "context"}
        assert UPDATE_MEMORY_SCHEMA["parameters"]["required"] == ["memory_id"]

    def test_update_memory_schema_documents_relative_time_resolution(self):
        # Guidance the model needs to resolve "yesterday"/"last week"/etc.
        # against the current prompt time, and to distinguish point-in-time
        # facts (start == end) from durations (distinct start/end).
        description = UPDATE_MEMORY_SCHEMA["description"]
        for phrase in ("yesterday", "last week", "two hours ago"):
            assert phrase in description
        assert "current prompt time" in description
'''

    apply_edit(TEST_PATH, old_test, new_test, "new relative-time schema test")

    print("\nAll edits applied. Next steps:")
    print("  python -m pytest tests/plugins/memory/test_hindsight_provider.py -q")
    print("  ruff check plugins/memory/hindsight/__init__.py tests/plugins/memory/test_hindsight_provider.py")


if __name__ == "__main__":
    main()
