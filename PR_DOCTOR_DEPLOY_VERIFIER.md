# Upstream PR Description: Add 'hermes doctor deploy' Process Verifier

**Branch**: `contrib/doctor-deploy-verifier`  
**Base**: `main` (`b39d76d902b0891457bc73a6eb43aa136f26d7a8`)  
**Type**: Feature / Diagnostics

---

## Summary
Adds `hermes doctor deploy` to list every long-lived hermes process (gateway, backend server, dashboard) alongside its HEAD commit at startup versus current repository HEAD.

If any process is running stale code (or repository HEAD cannot be verified), it flags `STALE` and exits non-zero to prevent silent production drift across updates.

## Tests Added
- `tests/hermes_cli/test_doctor_deploy.py`: 5 tests covering zero stale processes, stale detection, unknown HEAD handling, unresolvable HEAD fail-closed behavior, and empty process table.
