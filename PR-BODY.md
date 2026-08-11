<!-- native-links:v1 -->
Related #12650 #13970 #13538 #13988

## What does this PR do?

Closes the **Copilot token validation** defect class (#12650, #13970): `validate_copilot_token()` returned `True` for any non-empty token, so a classic PAT (`ghp_*`), a random string, or a stale token was accepted — and the Copilot API call failed later with no clear diagnosis.

**Fix:** reject tokens that don't match the supported prefixes (`gho_`, `github_pat_`, `ghu_`) with a message naming the supported formats. This is the fix from #13538 (author: **zhao**, cherry-picked with authorship preserved); #13988 (author: **giwaov**) implemented the same class fix and is credited here.

## Repro receipt (sha256)

`repro_copilot.py` — sha256 `8181023bbd6f9dd4681d946b468d3b3fd48e5f69690bcc8573a995e053cb8f41`

```bash
# baseline (origin/main @ 9d6c5a920c7)
python repro_copilot.py
# arbitrary token -> valid=True msg='OK'
# FAIL: arbitrary non-empty token accepted   (exit 1)

# this branch
python repro_copilot.py
# arbitrary token -> valid=False msg='Unsupported GitHub token format...'
# PASS: arbitrary tokens rejected, supported prefixes accepted   (exit 0)
```

## How to test

```bash
pytest tests/hermes_cli/test_copilot_auth.py -q
# 17 passed (includes prefix acceptance/rejection tests)
```

## What platforms were tested?

- Windows 11 native: `17 passed`, `git diff --check` clean.

## Why this matters to users

A user with a stale or wrong-format token gets a clear "Unsupported GitHub token format" message instead of a silent accept-then-fail — the exact footgun reported twice (#12650, #13970).

Closes #12650
Closes #13970

- [x] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature
- [ ] Breaking change

## Checklist

- [x] Code follows repo style
- [x] Self-review complete
- [x] Tests added (prefix acceptance/rejection)
- [x] Suite green: `17 passed` on current main
- [x] `git diff --check` clean
- [x] Repro receipt: baseline FAIL / fixed PASS (sha256 above)
- [x] Credit: implementation from #13538 by @zhao, cherry-picked with authorship preserved; same-class fix #13988 by @giwaov credited
