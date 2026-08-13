# PR #35601 Review Map

The profile export path is one security boundary. Implementation is grouped in `hermes_cli/profile_export.py`; `hermes_cli/profiles.py::export_profile()` remains the orchestration entry point, and import behavior is unchanged.

## Review order

### 1. Path classification and copy filtering

**Invariant:** credential files, backups, private keys, canonical credential trees, and SQLite sidecars do not enter staging, while safe same-named project paths remain portable.

- Code: `hermes_cli/profile_export.py:67-497`
- Orchestration: `hermes_cli/profiles.py:1869-1976`
- Tests: `TestIsSensitiveExportName`, `TestCredentialExclusion`

### 2. Symlink and extra-file policy

**Invariant:** export never follows or archives a profile symlink. Desktop `extra_files` cannot reintroduce excluded paths or write through a staged symlink. This matches import's rejection of link members.

- Code: `hermes_cli/profile_export.py:440-450`
- Extra files: `hermes_cli/profiles.py:1892-1927`
- Tests: `TestProfileExportSymlinkPolicy`, extra-file and staged-symlink cases in `TestCredentialExclusion` and `TestExportSecretScrub`

### 3. WAL-consistent SQLite snapshot

**Invariant:** every header-confirmed SQLite file is copied through SQLite's backup API, preserving committed WAL-only rows without archiving transient sidecars.

- Code: `hermes_cli/profile_export.py:542-572`, `1006-1070`
- Tests: WAL snapshot and SQLite sidecar cases in `TestCredentialExclusion`

### 4. SQLite residue removal and semantic verification

**Invariant:** deleted secret residue is removed only from the disposable snapshot. Compaction is accepted only when schema, semantic pragmas, tables, row identity, storage classes, and values remain exact.

- Code: `hermes_cli/profile_export.py:713-938`
- Tests: deleted-residue, source-preservation, semantic-drift, and rowid-shadowing cases in `TestExportSQLiteSecretInspection`

### 5. Live SQLite content inspection

**Invariant:** secret-shaped schema, TEXT, BLOB, UTF-16, URL credentials, and WAL-only live rows fail closed. Clean binary and hint-dense databases remain exportable.

- Code: `hermes_cli/profile_export.py:574-611`, `940-1003`
- Tests: `TestExportSQLiteSecretInspection`

### 6. Extension-independent staged-file scrubbing

**Invariant:** every staged regular file is inspected regardless of suffix. UTF-8 is redacted with bounded memory; encoded or binary secret witnesses fail closed; clean files remain byte-identical.

- Code: `hermes_cli/profile_export.py:585-711`, `1073-1116`
- Redactor support: `agent/redact.py`
- Tests: `TestExportSecretScrub`, `TestExtensionIndependentExportScrub`, `tests/agent/test_redact.py`

### 7. Atomic archive publication

**Invariant:** the archive is published with `os.replace()` from a temporary regular file in the destination directory. An output symlink is replaced, never followed, and failure leaves no partial archive.

- Code: `hermes_cli/profile_export.py:500-539`
- Tests: `TestAtomicArchivePublication`

## Why this stays atomic

Each layer closes a separate route through the same shareable archive boundary. Partial combinations are unsafe: a WAL-safe snapshot can leak live credentials, generic scrubbing can corrupt SQLite, and safe staging can still overwrite an external target at publication. The focused module makes the layers independently reviewable without presenting an incomplete subset as merge-safe.

## Final validation

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_profile_export_credentials.py \
  tests/hermes_cli/test_profiles.py \
  tests/hermes_cli/test_profile_distribution.py \
  tests/agent/test_redact.py

scripts/run_tests.sh \
  tests/test_redaction_registry.py \
  tests/agent/test_redact.py

python -m ruff check \
  hermes_cli/profile_export.py hermes_cli/profiles.py \
  hermes_cli/profile_distribution.py agent/redact.py \
  tests/hermes_cli/test_profile_export_credentials.py \
  tests/hermes_cli/test_profiles.py tests/agent/test_redact.py

python -m compileall -q \
  hermes_cli/profile_export.py hermes_cli/profiles.py agent/redact.py

git diff --check
```

Also rerun the focused adversarial probes and review the complete exact-head diff after the mechanical extraction.
