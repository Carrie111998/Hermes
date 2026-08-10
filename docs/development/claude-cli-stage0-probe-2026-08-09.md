# Claude CLI Stage-0 subprocess probe

Status: **tracked harness available; live managed-wrapper oracle not rerun in this change**

The reproducible harness is `scripts/claude_cli_stage0_probe.py`. It launches
the official Claude CLI through an approved transparent managed-policy wrapper,
uses the same fixed argv and environment shaping as the runtime, and emits one
sanitized JSON object. It never prints prompt/response content, environment
values, session identifiers, PIDs, credential paths, or raw stderr.

## Exact command

From the repository root on Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\claude_cli_stage0_probe.py `
  --managed-wrapper C:\approved\managed-policy-wrapper.exe `
  --claude-executable claude
```

The wrapper must accept the official executable and its argv as separate
arguments and remain attached for the subprocess lifetime. If it needs fixed,
non-secret arguments, append one `--wrapper-arg <value>` per argument. Do not
place credentials, tokens, or environment values in wrapper arguments.

The harness first runs `claude --version` and `claude --help` through that same
wrapper boundary. It then sends two static user frames over one persistent
stdin/stdout stream and closes stdin after the second result. `shell=False` is
used for every child process.

## Environment contract

The child environment enforcement is exact:

- remove every inherited `ANTHROPIC_*` request-shaping variable;
- remove the named CLI backend selectors `CLAUDE_CODE_USE_BEDROCK` and
  `CLAUDE_CODE_USE_VERTEX`;
- deliberately preserve `CLAUDE_CODE_OAUTH_TOKEN` without reading or logging
  its value, so the official CLI owns authentication;
- preserve `HOME` and `USERPROFILE`, so the official CLI can resolve its own
  login/keychain and managed policy.

No wildcard removal of `CLAUDE_CODE_*` variables is performed.

## Evidence semantics

A zero exit status and `"status":"PASS"` require all of these observations in
the same run:

- `same_child_pid=true`: both turns were handled by the same wrapper/CLI process;
- `session_consistent=true`: the opaque session identifier was present and
  stable, but its value was not retained in output;
- `process_generations=1`;
- `replay_acknowledgements=2`;
- `results=2`;
- `init_tools_empty=true`: the runtime accepted an init event only with
  `tools=[]`;
- `tool_events=0`;
- `forbidden_flags=0` for `--bare`, `--dangerously-skip-permissions`, and
  `--allow-dangerously-skip-permissions`;
- `clean_exit=true`: stdin close produced exit code 0;
- `bounded_exit=true`: cleanup completed within the harness's eight-second
  upper bound.

Any mismatch returns a nonzero exit status. Capability and protocol failures
are classified with sanitized messages; raw stderr is never emitted.

## Current evidence

The harness parser/help, syntax, and deterministic evaluator tests are local
verification only. This change does **not** claim a current live oracle PASS,
because the tracked harness was not executed through an approved managed-policy
wrapper in this worktree. A release operator may replace this paragraph with
the emitted sanitized JSON only after the exact managed-wrapper command above
returns all required counters.
