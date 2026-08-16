# FLEET-133: unguarded Python spawn-site triage

**Status:** DRAFT — analysis and inventory only; no behavior or boundary change. [VERIFIED: docs-only git diff scope]

**Analysis snapshot:** `bb1156bf6eef220708247698f154476f96adebe9` [VERIFIED: detached analysis worktree `git rev-parse HEAD`]

**Delivery base:** `7095e23eb2066fe9a2f93b99cdbfe0e2b5ece397` (`origin/main` when the branch was created). [VERIFIED: `git rev-parse origin/main` and `git merge-base --is-ancestor`; the named analysis snapshot is a side lineage, so the docs-only delivery branch was based on current main rather than importing its 30 commits]

**Inventory:** [`fleet-133-spawn-site-inventory.tsv`](fleet-133-spawn-site-inventory.tsv)

## Executive result

[VERIFIED: Python AST enumeration plus source/caller data-flow review at `bb1156bf6eef220708247698f154476f96adebe9`] The independent enumeration found 833 direct Python launch sites: 693 with no effective environment argument, 109 with an explicit environment expression, 11 using a boundary-builder call, and 20 whose environment may be hidden in `**kwargs`.

[VERIFIED: path taxonomy and per-site source/caller trace; blind spot: `production` excludes path segments named `tests`, `test`, `testing`, `benchmarks`, or `examples`, but deliberately retains shipped scripts and skill helpers] Of the 693 no-env sites, 383 are production under that stated taxonomy and were ranked: 64 Tier 1, 65 Tier 2, and 254 Tier 3.

| Tier | Definition | Count | Evidence |
|---|---|---:|---|
| Tier 1 | Model/runtime reachable and argv can be influenced by model/tool input, a skill/plugin input, configuration, or remote content. | 64 | [VERIFIED: source/caller data-flow trace for each TSV row] |
| Tier 2 | Model/runtime reachable with argv fixed by literals, internal constants, discovered runtime IDs, or Hermes-generated temporary paths. | 65 | [VERIFIED: source/caller data-flow trace for each TSV row] |
| Tier 3 | Operator-only CLI, setup, installer, dashboard/TUI RPC, or maintenance flow. | 254 | [VERIFIED: source/caller trace; `argv_trust` within Tier 3 is marked `[ASSUMED]` in the TSV because it does not affect ranking] |

[VERIFIED: TSV row count and unique `file:line:column` keys; blind spots: static import aliases are resolved, but callable rebinding, reflective dispatch, third-party wrappers, and subprocesses launched below non-spawn APIs are outside the enumeration] Every one of the 383 ranked sites has both axes, a tier, source evidence, and—when Tier 1 or Tier 2—a per-site minimum-environment note in the TSV.

## Enumeration method and limits

[VERIFIED: `Path.rglob("*.py")` followed by `ast.parse`] The walk parsed 2,941 Python files at the named snapshot and recorded zero parse failures. The zero is limited to files present beneath that checkout, UTF-8 decoding, and Python's parser; ignored/untracked files outside the checkout and runtime-generated code are blind spots.

[VERIFIED: AST callee resolution] The enumerated families were:

- [VERIFIED: enumerator call set] `subprocess.Popen`, `run`, `call`, `check_call`, `check_output`, `getoutput`, and `getstatusoutput`.
- [VERIFIED: enumerator call set] `asyncio.create_subprocess_exec` and `create_subprocess_shell`.
- [VERIFIED: enumerator call set] `os.system`, `popen`, `startfile`, `spawn*`, `posix_spawn*`, and `exec*`.
- [VERIFIED: enumerator call set] `pty.spawn` plus `ptyprocess`/`winpty` `PtyProcess.spawn` spellings.

[VERIFIED: AST import table] Direct module calls, imported function aliases, module aliases, and the three observed `PtyProcess.spawn` spellings are resolved. [VERIFIED: scope definition; blind spot stated] `multiprocessing.Process`/`Pool` are outside scope because the dispatch names the subprocess/os/asyncio/pty families; assignments such as `runner = subprocess.run` followed by `runner(...)`, wrapper functions, native extensions, and library-internal launches are not followed.

[VERIFIED: AST keyword and positional inspection] Environment buckets mean:

- [VERIFIED: `env` keyword/positional inspection] `none`: no environment argument, or `env=None`; normal API semantics inherit the parent environment.
- [VERIFIED: `env` expression inspection] `explicit_dict`: an explicit environment expression, including named variables and `{**os.environ, ...}`; the label does not claim the expression is sanitized.
- [VERIFIED: callee-name and source inspection] `boundary_builder`: the environment is returned by the shared or specialized child-environment builder used at that site.
- [VERIFIED: `ast.keyword(arg=None)` inspection; blind spot: the expansion's runtime contents are intentionally unknown] `hidden_kwargs`: one or more `**kwargs` expansions can contain `env`.

## Enumeration totals

[VERIFIED: grouping all 833 TSV rows by `env_bucket`] Bucket totals are 693 none/full inheritance, 109 explicit environment, 11 boundary builder, and 20 hidden `**kwargs`; they sum to 833.

| Package/root | None / full inheritance | Explicit env | Boundary builder | Hidden `**kwargs` | Total | Evidence |
|---|---:|---:|---:|---:|---:|---|
| `agent` | 7 | 5 | 4 | 3 | 19 | [VERIFIED: TSV group-by] |
| `batch_runner` | 2 | 0 | 0 | 0 | 2 | [VERIFIED: TSV group-by] |
| `cli` | 21 | 1 | 0 | 0 | 22 | [VERIFIED: TSV group-by] |
| `cron` | 0 | 0 | 1 | 0 | 1 | [VERIFIED: TSV group-by] |
| `gateway` | 15 | 4 | 0 | 1 | 20 | [VERIFIED: TSV group-by] |
| `hermes_cli` | 203 | 40 | 0 | 10 | 253 | [VERIFIED: TSV group-by] |
| `hermes_constants` | 0 | 3 | 0 | 0 | 3 | [VERIFIED: TSV group-by] |
| `optional-skills` | 9 | 0 | 0 | 1 | 10 | [VERIFIED: TSV group-by] |
| `plugins` | 43 | 6 | 0 | 0 | 49 | [VERIFIED: TSV group-by] |
| `scripts` | 12 | 2 | 0 | 0 | 14 | [VERIFIED: TSV group-by] |
| `skills` | 4 | 2 | 0 | 0 | 6 | [VERIFIED: TSV group-by] |
| `tests` | 310 | 32 | 0 | 1 | 343 | [VERIFIED: TSV group-by] |
| `tools` | 63 | 12 | 5 | 3 | 83 | [VERIFIED: TSV group-by] |
| `tui_gateway` | 4 | 2 | 1 | 1 | 8 | [VERIFIED: TSV group-by] |

### Reconciliation with the dispatch context

[INHERITED: FLEET-133 dispatch] The comparison point is 886 total sites and approximately 350 full-inheritance production sites, including `hermes_cli` approximately 204, `tools` approximately 64, `plugins` approximately 37, `gateway` approximately 16, and `agent` approximately 6, plus `tui_gateway` and `cli.py`.

[VERIFIED: independent TSV group-by] This enumeration is 53 below the inherited 886 total. It does not reconstruct or import the earlier enumerator, so the unresolved 53 are reported as a method/taxonomy difference, not silently normalized. The most important blind spots are callable rebinding, wrappers, library-internal launches, and spawn families outside the dispatch.

| Slice | Independent | Inherited | Difference | Evidence |
|---|---:|---:|---:|---|
| Total direct AST-resolved sites | 833 | 886 | -53 | [VERIFIED: TSV count; inherited value from dispatch] |
| `hermes_cli` no-env production | 203 | ~204 | -1 versus the stated center | [VERIFIED: TSV filter/group-by; inherited approximation] |
| `tools` no-env production | 63 | ~64 | -1 versus the stated center | [VERIFIED: TSV filter/group-by; inherited approximation] |
| `plugins` no-env production | 43 | ~37 | +6 versus the stated center | [VERIFIED: TSV filter/group-by; inherited approximation] |
| `gateway` no-env production | 15 | ~16 | -1 versus the stated center | [VERIFIED: TSV filter/group-by; inherited approximation] |
| `agent` no-env production | 7 | ~6 | +1 versus the stated center | [VERIFIED: TSV filter/group-by; inherited approximation] |
| `cli.py` no-env production | 21 | not numerically stated | not computed | [VERIFIED: TSV filter/group-by; inherited context omits a number] |
| `tui_gateway` no-env production | 4 | not numerically stated | not computed | [VERIFIED: TSV filter/group-by; inherited context omits a number] |

[VERIFIED: TSV filter/group-by; blind spot: this is a taxonomy subtotal, not a claim that omitted roots are non-production] The seven roots explicitly emphasized by the dispatch (`agent`, `cli.py`, `gateway`, `hermes_cli`, `plugins`, `tools`, and `tui_gateway`) contain 356 no-env production sites. The remaining 27 ranked sites are `batch_runner` (2), `optional-skills` (9), `scripts` (12), and `skills` (4).

## Tier 1 map

[VERIFIED: TSV filter `tier == TIER 1`; blind spots are the static-analysis limits above] The following table lists every Tier 1 site found by this method. “Spawns” describes the executable and the trust-bearing argv elements; exact source expressions and per-site child-environment needs are in the TSV.

| Site | Spawns / trust-bearing argv | Evidence |
|---|---|---|
| `gateway/platforms/qqbot/adapter.py:2107` | `ffmpeg` converting remotely supplied audio paths to 16 kHz mono WAV. | [VERIFIED: AST + caller/data-flow trace] |
| `gateway/platforms/signal.py:169` | `ffmpeg` remuxing inbound AAC bytes through generated source/destination paths. | [VERIFIED: AST + caller/data-flow trace] |
| `gateway/platforms/webhook.py:1135` | `gh pr comment`; repository, PR number, and generated comment body enter argv. | [VERIFIED: AST + caller/data-flow trace] |
| `gateway/platforms/whatsapp_cloud.py:1231` | configured/discovered `ffmpeg` converting generated MP3 path to Opus output. | [VERIFIED: AST + caller/data-flow trace] |
| `gateway/run.py:2127` | `ffprobe` over the inbound/generated audio path. | [VERIFIED: AST + caller/data-flow trace] |
| `hermes_cli/kanban_db.py:4765` | `tmux list-panes` for a task-assignee-derived session name. | [VERIFIED: AST + caller/data-flow trace] |
| `hermes_cli/kanban_db.py:4770` | `tmux kill-session` for a task-assignee-derived session name. | [VERIFIED: AST + caller/data-flow trace] |
| `hermes_cli/kanban_db.py:5708` | `git -C <workspace> rev-parse --show-toplevel`. | [VERIFIED: AST + caller/data-flow trace] |
| `hermes_cli/kanban_db.py:5730` | `git show-ref` for a task/config-derived repository and branch. | [VERIFIED: AST + caller/data-flow trace] |
| `hermes_cli/kanban_db.py:5744` | `git -C <workspace> rev-parse --git-common-dir`. | [VERIFIED: AST + caller/data-flow trace] |
| `hermes_cli/kanban_db.py:5763` | `git -C <workspace> rev-parse --git-dir`. | [VERIFIED: AST + caller/data-flow trace] |
| `hermes_cli/kanban_db.py:5782` | `git -C <workspace> branch --show-current`. | [VERIFIED: AST + caller/data-flow trace] |
| `hermes_cli/kanban_db.py:5839` | `git worktree add`; repository, target, and branch derive from Kanban task/config state. | [VERIFIED: AST + caller/data-flow trace] |
| `optional-skills/creative/kanban-video-orchestrator/scripts/monitor.py:37` | `hermes kanban list --tenant <tenant> --json`. | [VERIFIED: AST + caller/data-flow trace] |
| `optional-skills/creative/kanban-video-orchestrator/scripts/monitor.py:46` | fallback `hermes kanban list --tenant <tenant>`. | [VERIFIED: AST + caller/data-flow trace] |
| `optional-skills/creative/kanban-video-orchestrator/scripts/monitor.py:70` | `hermes kanban show <task-id> --json`. | [VERIFIED: AST + caller/data-flow trace] |
| `optional-skills/creative/pixel-art/scripts/pixel_art_video.py:297` | `ffmpeg` video encoding with skill-supplied FPS, frame directory, and output path. | [VERIFIED: AST + caller/data-flow trace] |
| `optional-skills/creative/pixel-art/scripts/pixel_art_video.py:309` | `ffmpeg` GIF encoding with skill-supplied FPS, frame directory, and output path. | [VERIFIED: AST + caller/data-flow trace] |
| `optional-skills/finance/excel-author/scripts/recalc.py:45` | LibreOffice headless recalculation of a skill-supplied workbook path. | [VERIFIED: AST + caller/data-flow trace] |
| `optional-skills/security/unbroker/scripts/crypto.py:56` | `age-keygen -o <configured identity path>`. | [VERIFIED: AST + caller/data-flow trace] |
| `optional-skills/security/unbroker/scripts/crypto.py:82` | `age -r <configured/generated recipient>`. | [VERIFIED: AST + caller/data-flow trace] |
| `optional-skills/security/unbroker/scripts/crypto.py:87` | `age -d -i <configured identity path>`. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/memory/openviking/__init__.py:1170` | `openviking-server --host <configured host> --port <configured port>`. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/platforms/discord/adapter.py:695` | `ffmpeg` over inbound PCM/output paths and source audio metadata. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/platforms/discord/voice_mixer.py:312` | `ffmpeg` decoding a remote/plugin-supplied media path. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/platforms/photon/adapter.py:829` | `lsof` with configured Photon sidecar port. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/platforms/simplex/adapter.py:898` | ImageMagick `convert` over an inbound attachment path. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/platforms/simplex/adapter.py:906` | ImageMagick `convert` thumbnail generation over an inbound attachment path. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/platforms/telegram/adapter.py:5713` | configured Gmail-triage script with callback-supplied action argument. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/platforms/whatsapp/adapter.py:55` | `lsof` with configured WhatsApp bridge port. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/platforms/whatsapp/adapter.py:70` | `ss` with configured WhatsApp bridge port. | [VERIFIED: AST + caller/data-flow trace] |
| `plugins/teams_pipeline/pipeline.py:498` | `ffmpeg` extracting audio from a downloaded remote meeting recording. | [VERIFIED: AST + caller/data-flow trace] |
| `skills/creative/comfyui/scripts/auto_fix_deps.py:54` | Comfy CLI node/model installation commands assembled from workflow/package/model inputs. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/code_execution_tool.py:1692` | a project/config-selected Python interpreter with a fixed version probe. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/docker.py:393` | `docker image inspect <configured image>`. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/docker.py:475` | constrained `docker run <configured image> sleep 0` capability probe. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/docker.py:983` | `docker run` assembled from configured image, mounts, limits, and task context. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/docker.py:1151` | replacement `docker run` assembled from configured image, mounts, limits, and task context. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/singularity.py:223` | `singularity`/`apptainer instance start` with configured image, limits, mounts, and overlay. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:104` | `ssh` ControlMaster establishment using configured host/user/port/options. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:122` | `ssh` remote-home probe using configured target/options. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:149` | `ssh` remote-directory creation using configured target/path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:164` | `ssh mkdir` for a model/tool-selected upload destination. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:178` | `scp` upload using configured target and model/tool-selected paths. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:207` | `ssh` bulk-upload command using configured target/path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:255` | local `tar` producer over model/tool-selected upload files. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:262` | `ssh` bulk-upload consumer using configured target/path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:311` | `ssh` bulk download using configured target and model/tool-selected paths. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:325` | `ssh` delete command for a model/tool-selected remote path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/environments/ssh.py:364` | `ssh` ControlMaster cleanup using configured target/options. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/mcp_stdio_watchdog.py:148` | the configured MCP server's complete `real_argv`. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/tirith_security.py:781` | `tirith check ... -- <model-authored shell command>`. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/transcription_tools.py:1191` | `ffmpeg` over a model/tool/remote-selected audio path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/transcription_tools.py:1237` | configured local STT shell command template expanded with input/output/language/model. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/transcription_tools.py:1239` | configured/auto-detected local STT argv expanded with input/output/language/model. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/tts_tool.py:914` | `ffmpeg` converting a tool-selected MP3/output path to Opus. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/tts_tool.py:1801` | `ffmpeg` converting Gemini TTS output to a tool-selected output path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/tts_tool.py:1884` | Hermes' NeuTTS helper Python with model text and configured reference/model/device argv. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/tts_tool.py:1896` | `ffmpeg` converting NeuTTS output to the tool-selected output path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/tts_tool.py:1963` | `python -m piper.download_voices` with configured voice and download directory. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/tts_tool.py:2075` | `ffmpeg` converting Piper output to the tool-selected output path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/tts_tool.py:2141` | `ffmpeg` converting KittenTTS output to the tool-selected output path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/vision_tools.py:292` | `rsvg-convert` or `inkscape` over a model/tool-selected SVG path. | [VERIFIED: AST + caller/data-flow trace] |
| `tools/voice_mode.py:1099` | platform audio player (`afplay`, `ffplay`, or `aplay`) over a generated/tool-selected audio path. | [VERIFIED: AST + caller/data-flow trace] |

## Child environment needed by Tier 1 and Tier 2

[VERIFIED: per-site source inspection] `environment_needs_if_bounded` in the TSV records a minimum for every Tier 1 and Tier 2 row. The repeated patterns are:

- [VERIFIED: command and library behavior visible at each site] Media converters/probes need runtime lookup, dynamic-loader/platform bootstrap variables, locale, and temporary-directory variables; they do not need provider, gateway, AWS, or Claude credentials.
- [VERIFIED: SSH command construction] SSH/SCP sites need `HOME`/`USERPROFILE` for config and `known_hosts`, plus the selected key or `SSH_AUTH_SOCK` and only the transport settings actually configured.
- [VERIFIED: Docker command construction] Docker sites need the Docker config/context and only configured `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CERT_PATH`, or `DOCKER_TLS_VERIFY` values; private-registry credentials belong in Docker's credential store or an exact binding.
- [VERIFIED: package-manager command construction] NPM/pip/uv/model-download sites need cache/temp/home, configured proxy/CA variables, and exact private-registry credentials only when their selected source requires them.
- [VERIFIED: git/gh command construction] Local git probes need runtime plus git config; network git/`gh` sites additionally need the chosen credential helper/SSH agent or exact `GH_TOKEN` only when the deployment intentionally relies on ambient-token auth.
- [VERIFIED: MCP config flow; blind spot: arbitrary MCP servers have arbitrary documented requirements] The MCP watchdog's real child needs the selected server's explicitly configured environment, not Hermes' ambient process environment.
- [VERIFIED: PulseAudio call sites] `pactl`/`paplay` paths can require `XDG_RUNTIME_DIR` and configured `PULSE_SERVER`/`DBUS_SESSION_BUS_ADDRESS`; those are operational runtime variables rather than provider credentials.

## `CLAUDE_CODE_OAUTH_TOKEN` standing-policy answer

[VERIFIED: `tools/environments/local.py:241-266,316-322,553-590`] `CLAUDE_CODE_OAUTH_TOKEN` is in `_AMBIENT_OPERATOR_ENV_ALLOWLIST`; `_is_ambient_env_allowed` therefore admits it into terminal builders and `hermes_subprocess_env`, while `_AWS_OPERATOR_ENV_VARS` are explicitly removed inside `hermes_subprocess_env`. The result is that the Claude token reaches shared non-terminal children while the AWS operator chain is terminal-boundary-only.

[VERIFIED: direct-call search for `hermes_subprocess_env` plus caller inspection] Non-terminal recipients include the TUI slash worker and `cli.exec`, dependency installers, lazy dependency bootstrap, browser children, CUA/permissions/doctor children, shell hooks, inline skill shell, Codex app-server, and the generic ACP child. These recipients do not consume a Claude token merely because the shared builder supplies it.

[VERIFIED: `agent/anthropic_adapter.py:354-371,1331-1368`] The `claude --version` probe does not need authentication, and `claude setup-token` is an operator-interactive credential-creation flow that uses Claude's credential store; neither justifies universal non-terminal propagation.

[VERIFIED: `agent/copilot_acp_client.py:102-110,417-419,508-516`; `tools/delegate_tool.py:1218-1250,2503`; `hermes_cli/tips.py:291`] The genuine non-terminal consumer is the generic ACP launch when its configured/overridden command is `claude`/`claude-code`. Copilot itself authenticates through its own store, and Codex app-server uses Codex auth/config, so those command choices do not need the Claude token.

[VERIFIED: `tools/mcp_tool.py:427-446,2021-2079`; blind spot: user MCP configuration is intentionally open-ended] A configured MCP server that launches `claude mcp serve` is also a genuine consumer only when the user explicitly binds the token in that server's `env`; the MCP safe-env path does not rely on the shared ambient allowlist.

[INHERITED: commit `14639ded7737a3feafc8ed3ba0f30fcfa8f21b04` and its #55878 rationale] Removing the token from an agent-spawned Claude CLI without a replacement caused fallback to shared Keychain/`~/.claude/.credentials.json` state and could clear that state after authentication failure, logging the operator out.

[ASSUMED] Recommendation: scope `CLAUDE_CODE_OAUTH_TOKEN` like the AWS operator chain—retain it at terminal boundaries 1–2, remove it from the shared non-terminal allowlist, and explicitly bind it only when the resolved child is a Claude CLI/ACP consumer. The assumption is policy intent: this document does not change or test boundary behavior.

[VERIFIED: caller inventory and #55878 failure mode] If it were scoped without the explicit Claude-ACP binding, delegated/selected Claude ACP sessions that rely solely on ambient `CLAUDE_CODE_OAUTH_TOKEN` would lose authentication and could repeat the Keychain/credential-file fallback regression. [ASSUMED] Intentionally configured shell hooks or inline skill commands that invoke `claude` and rely only on the ambient token would also stop authenticating; preserving those requires an exact per-consumer binding rather than continued disclosure to every hook/skill child. [VERIFIED: parent-process credential reads in `agent/anthropic_adapter.py`] Hermes' in-process Anthropic OAuth/provider use would not break merely because child propagation is narrowed—the parent still reads its configured token.

## Handoff

[VERIFIED: document and TSV contents] This is a ranked map and machine-readable inventory. [VERIFIED: diff-scope inspection before commit; blind spot: repository behavior can still change independently on `main`] The deliverable contains documentation data only and proposes no code, boundary, or behavior edit.
