# Task 1 Report

## Scope
Implemented the strict, immutable, observational `activity_policy` registry, frozen inventory coverage, and wheel/sdist metadata. No live `jobs.json` or runtime behavior was changed.

## RED evidence
- `python -m pytest tests/activity_policy/test_registry.py tests/test_packaging_metadata.py -q` initially failed during collection with `ModuleNotFoundError: No module named activity_policy`.
- The duplicate-key test subsequently failed as intended with `DID NOT RAISE PolicyError`, proving PyYAML duplicate activity IDs needed explicit rejection.

## GREEN evidence
- `python -m pytest tests/activity_policy/test_registry.py tests/test_packaging_metadata.py -q`: **37 passed in 4.89s**.
- Registry is observe-only, fail-closed for malformed declarations, case-sensitive for aliases, and returns `None` for unmapped legacy aliases.
- Frozen fixture contains only reviewed `id` and `name` fields and all aliases resolve.

## Static and packaging evidence
- `python -m ruff check activity_policy tests/activity_policy tests/test_packaging_metadata.py`: **All checks passed**.
- `python -m build --wheel --sdist --outdir .superpowers/sdd/dist-task-1`: succeeded in the target checkout.
- Direct archive inspection: **wheel+sdist contain activity_policy/policies.yaml**.

## Concerns
- None affecting Task 1. Generated build artifacts under `.superpowers/sdd/dist-task-1` are evidence only and are not intended for staging.


## Review fix: YAML error normalization
- RED: `python -m pytest tests/activity_policy/test_registry.py -q` produced **2 failed, 25 passed in 6.54s**. The malformed document leaked `yaml.parser.ParserError`; the sequence mapping key leaked `TypeError: unhashable type: 'list'`.
- Fix: `_load_yaml()` now preserves existing `PolicyError` declarations and normalizes all other YAML parsing/construction failures to `PolicyError("invalid policy YAML")` using `raise ... from exc`, retaining the original cause. Focused tests call `ActivityRegistry.load()` and assert both the public exception and concrete cause type/content.
- Exact verification command: `python -m pytest tests/activity_policy/test_registry.py tests/test_packaging_metadata.py -q && python -m ruff check activity_policy/registry.py tests/activity_policy/test_registry.py tests/test_packaging_metadata.py`
- Output: **39 passed in 6.85s**; **All checks passed!**
- Review-fix commit: `5e38c3546`.
