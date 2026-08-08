# Windows venv replacement recovery

## Symptom and reproduction

On Windows, an Hermes update can leave the existing `venv` partially removed
when a running Hermes Python process still has a native extension loaded. A
subsequent install then reports access denied, and the console launchers may be
missing even though a sibling `.venv` contains them.

The failure is reproducible by running an Hermes process from `venv\Scripts`,
then starting the installer's `venv` stage while the process is supervised or
while a native extension remains open.

## Root cause

The old installer tried to rename the existing venv, but if that rename failed
it fell back to recursive in-place deletion. Windows refuses deletion of a
loaded `.pyd`/DLL, so the fallback could destroy part of the environment while
leaving the install stage failed. A later dependency sync could also target a
sibling `.venv` unless the project environment was pinned explicitly.

## Safety behavior

The venv stage now:

1. disables only gateway tasks that were enabled before the stage;
2. stops processes whose executable path is under the target venv;
3. performs a final CIM process check and fails with structured stage output if
   any target-venv process remains;
4. requires `Rename-Item venv -> venv.stale.<timestamp>` to succeed;
5. refuses all in-place recursive deletion and retains the quarantined tree;
6. restores the original scheduled-task state in `finally`, including failure.

The dependency stage continues to pin `UV_PROJECT_ENVIRONMENT` to `venv`,
checks baseline imports, and verifies all expected console entry points before
PATH finalization.

## Rollback and operator recovery

The stale tree is retained for forensics and rollback. If replacement
validation fails, keep the failed replacement and stale tree intact, stop any
new processes using the failed replacement, and restore the prior tree only
after confirming that no process executes from either tree. Cleanup of
`venv.stale.*` is an explicit operator action after acceptance; it is not part
of the replacement stage.

## Validation

`scripts/tests/test-install-ps1-venv-safety.ps1` runs the real stage on native
Windows against disposable fixtures. It covers a continuously respawned venv
process, a failed quarantine rename caused by an open file handle, successful
quarantine with stale-tree retention, fresh `venv\Scripts\python.exe`
creation, and scheduled-task state preservation. The stage-protocol and Git
Bash compatibility smoke tests also pass. The existing long-path smoke suite
has unrelated failures on hosts where its synthetic short TEMP alias does not
exist.
