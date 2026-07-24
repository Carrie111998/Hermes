# Investigation Techniques

Concrete tool usage for each Phase 1 / Phase 2 step.

## Reading error messages

- Don't skip past errors or warnings — they often contain the exact solution.
- Read stack traces completely. Note line numbers, file paths, error codes.

**Action:** Use `read_file` on the relevant source files. Use `search_files` to find the error
string in the codebase.

## Checking recent changes

```bash
# Recent commits
git log --oneline -10

# Uncommitted changes
git diff

# Changes in specific file
git log -p --follow src/problematic_file.py | head -100
```

Look for: new dependencies, config changes, anything landed near the symptom.

## Gathering evidence in multi-component systems

**WHEN the system has multiple components** (API → service → database, CI → build → deploy),
**BEFORE proposing fixes, add diagnostic instrumentation.**

For EACH component boundary:

- Log what data enters the component
- Log what data exits the component
- Verify environment/config propagation
- Check state at each layer

Then: run once to gather evidence showing WHERE it breaks → analyze the evidence to identify the
failing component → investigate that specific component.

## Tracing data flow

**WHEN the error is deep in the call stack:**

- Where does the bad value originate?
- What called this function with the bad value?
- Keep tracing upstream until you find the source.
- Fix at the source, not at the symptom.

**Action:** Use `search_files` to trace references:

```python
# Find where the function is called
search_files("function_name(", path="src/", file_glob="*.py")

# Find where the variable is set
search_files("variable_name\\s*=", path="src/", file_glob="*.py")
```

## Finding working examples (Phase 2)

Locate similar working code in the same codebase — what works that's similar to what's broken?

```python
search_files("similar_pattern", path="src/", file_glob="*.py")
```

## Verifying a fix (Phase 4)

```bash
# Run the specific regression test
pytest tests/test_module.py::test_regression -v

# Run full suite — no regressions
pytest tests/ -q
```
