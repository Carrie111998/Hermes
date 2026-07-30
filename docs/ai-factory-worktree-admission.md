# AI Factory worktree admission gate

HER-95 adds a machine-wide worktree admission gate to the existing `factory_lane.py` registry layout. It does not introduce a second registry: owners still live under `registry/locks/<KEY>/owner.json`, lane journals under `registry/lanes/<KEY>.jsonl`, and all worktree conflict decisions canonicalize `realpath(worktree)`.

## Commands

### Owner claim / pre-build hard gate

```bash
python scripts/factory_lane.py \
  --registry <registry-root> \
  admit HER-95 \
  --mode owner \
  --hard \
  --agent default \
  --profile default \
  --session <session-id> \
  --gateway-session-key <platform:chat:thread> \
  --owner-pid <long-lived-agent-pid> \
  --worktree <worktree>
```

Hard owner admission:
- serializes the final decision under `.worktree-admission.lock` to close preflight→claim TOCTOU races;
- refuses a second live owner for the same canonical worktree, even if the second owner uses another issue key;
- refreshes heartbeat for the same owner/session; a same-session re-claim onto a
  *different* worktree that is already owned by another lane is refused (never
  rewrites the owner, never creates two owners for one worktree);
- stores `profile` and `gateway_session_key` when supplied, but rejects a
  secret-like `gateway_session_key` (anything matching token/password/api_key/…)
  before writing owner.json — no tokens or chat secrets are ever persisted;
- records the transported **parent** process identity (`--owner-pid`, optional
  `--owner-start-time`) instead of the ephemeral `factory_lane.py` subprocess
  pid, so liveness/reclaim stays correct when the gate is driven as a subprocess;
  a dead `--owner-pid` (or a start-time that does not match it) is refused;
- refuses dirty ownerless git worktrees before a build, without resetting or deleting anything.

`--owner-pid` defaults to the running `factory_lane.py` process only when
omitted (standalone CLI use). When the gate is invoked from a launcher/gateway
subprocess, always transport the long-lived agent pid so a claim never persists a
pid that dies the instant the subprocess exits.

### Reviewer read-only admission

```bash
python scripts/factory_lane.py --registry <registry> admit HER-95 \
  --mode reviewer --agent opus-reviewer --session <session-id> \
  --worktree <worktree> --json
```

Reviewer mode is read-only: it may report the current owner, but it never creates or mutates `owner.json`.

### Advisory SessionStart

```bash
python scripts/factory_lane.py --registry <registry> hook-session-start \
  --repo <worktree> --agent hermes-immo --session <session-id>
```

If the same worktree is owned by another live session, the hook prints a bounded `STOP: worktree already owned ...` advisory. If the registry is absent or corrupt, the hook fails open with exit 0 and no output.

### Stale recovery

```bash
python scripts/factory_lane.py --registry <registry> claim HER-96 \
  --agent default --session <session-id> --worktree <worktree> \
  --reclaim-worktree --ttl-hours 2
```

Recovery only succeeds when the previous owner process is stale (`not_found`, `zombie`, or PID `reused`), heartbeat TTL is expired, and the worktree has been inactive for 24h. Otherwise the claim fails closed.

### Business-profile domain guard

For a métier profile such as `hermes-immo`, pass a bounded domain prefix set before hard owner admission:

```bash
python scripts/factory_lane.py --registry <registry> admit SCA-740 \
  --mode owner --hard --agent hermes-immo --profile hermes-immo \
  --domain-prefixes JYI,HER --session <session-id> --worktree <worktree>
```

A key outside the allowed prefixes is refused before any owner file is written. This is the canary path for generic `continue` prompts arriving in a business gateway.

### Bounded first-worktree bootstrap

The first Code worktree is created by the trusted controller, not by allowing a
model-issued `git worktree add`. A local, non-symlink, non-group/world-writable
JSON policy supplies every filesystem/Git choice:

```json
{"profiles":{"hermes-code-a":{"repo":"/ABS/repo","base_ref":"main","worktrees_parent":"/ABS/worktrees","branch_prefix":"fix"}}}
```

```bash
python scripts/factory_lane.py --registry <registry> bootstrap HER-96 \
  --policy /ABS/bootstrap-policy.json --profile hermes-code-a \
  --agent hermes-code-a --session <session-id> \
  --owner-pid <long-lived-gateway-pid> --owner-start-time '<ps lstart value>'
```

The destination is exactly `<worktrees_parent>/<profile>-<lowercase-key>` and
the branch exactly `<branch_prefix>/<lowercase-key>`. Bootstrap reserves an
exact `bootstrap_pending` owner under the machine-wide lock before invoking
`git worktree add` with an argv and `shell=False`. Pending never authorizes a
mutation. Activation occurs only after canonical top-level and branch checks.
Failure removes only a clean worktree proven to have been created by this call;
otherwise pending evidence is preserved for reconciliation. Bootstrap never
uses force, clean, reset, or stash.

## Runtime wiring — real pre-mutation gate (`pre_tool_call` hook)

The `admit` / `claim` CLI takes *ownership*; the runtime gate that actually runs
**before every build-capable tool** is `scripts/factory_admission_hook.py`, wired
through the generic shell-hook bridge Hermes already loads at startup
(`agent.shell_hooks.register_from_config(load_config(), …)`, called from
`cli.py`, `hermes_cli/main.py`, and `gateway/run.py`). No core file changes: the
gate is a declarative, opt-in `hooks:` entry in `config.yaml` (profile-aware).

```yaml
# ~/.hermes/config.yaml (or a profile's config.yaml)
hooks:
  pre_tool_call:
    - matcher: ".*"
      fail_closed: true
      command: >-
        python3 /ABS/scripts/factory_admission_hook.py
        --registry /ABS/registry --agent hermes-code-a --profile hermes-code-a
        --only-mutating --require-owned-git
```

For Code A/B this exact shape is a single security contract: `matcher: ".*"`
ensures that every exposed tool reaches the gate; `fail_closed: true` makes a
hook timeout/error/invalid response a veto; and
`--only-mutating --require-owned-git` enables the HER-96 fail-closed classifier.
Do not replace `.*` with a hand-maintained mutator list. New or unknown tools,
actions, and malformed/ambiguous payloads classify as unbounded mutations by
default.

As defense in depth, Code profiles should also disable toolsets they do not need
(computer-use, cronjob, mutating browser/UI tools, messaging, project creation,
skill mutation, and delegation). Toolset configuration is not the security
boundary: the all-tools hook still blocks these operations if they are exposed
accidentally. This document does not require or perform any live config change.

At tool time the plugin manager calls the hook with the standard shell-hook stdin
payload (`hook_event_name`, `tool_name`, `tool_input`, `session_id`, `cwd`, …).
The hook is **read-only**: it resolves every effective mutation target — explicit
terminal `workdir`, all Git global `-C PATH` / `-CPATH` targets (including
chained `-C` bases), file-tool `path`/`file_path`, every source and destination
in the real V4A `patch(mode="patch", patch=...)` envelope, Codex `apply_patch`
`changes[*].path`, and path arguments in terminal commands (absolute or relative
to the session cwd). V4A headers and relative paths are parsed fail-closed;
absolute, incomplete, unknown, or ambiguous operation headers are refused. Shell
punctuation is tokenized even when adjacent to a path
(`repo;`, `repo&&`), closing no-whitespace command-chain bypasses. The session
`cwd` is used only when no effective target exists before each candidate is resolved to its
git top-level and passed to `factory_lane.evaluate_admission_guard(...)`. This
prevents a session launched outside a worktree from bypassing admission by
targeting it through tool arguments. Only when the guard denies, the hook prints
`{"decision": "block", "reason": "..."}`. `agent/shell_hooks.py`
translates that into the canonical `{"action": "block", "message": …}` that
`hermes_cli.plugins.get_pre_tool_call_block_message()` (the exact call site in
`model_tools.handle_function_call`) uses to veto the tool before it executes.

Guard semantics:
- the strict Code classifier has three outcomes. A schema-bounded observation is
  allowed without a claim; a file/patch/terminal mutation with extractable
  targets proceeds to exact worktree ownership checks; an unbounded mutation is
  blocked even after a valid claim because ownership cannot make an unrelated
  external target safe;
- the explicit observation table covers strict `read_file`, `read_terminal`,
  `search_files`, `session_search`, `skill_view`, `skills_list`, `web_search`,
  `web_extract`, `project_list`, browser `snapshot`/`get_images`, read-only
  `todo`, and `clarify` payloads. Unknown fields, invalid types/bounds, and new
  actions are mutations by default. Browser navigation/click/type/press/scroll,
  browser CDP/console, messaging, media generation, memory, project/skill
  mutation, delegation, kanban, Home Assistant, Spotify, and every other tool
  without a bounded worktree target are blocked;
- `cronjob(action="list")` alone is observational. `create`, `update`, `remove`,
  `run`, `pause`, `resume`, unknown actions, and ambiguous payloads are blocked;
- `computer_use` permits schema-bounded `capture`, `list_apps`, and
  `list_windows`. Mouse, keyboard, type, focus, set-value, drag, scroll, wait,
  unknown actions, and extra/invalid fields are blocked;
- `process` permits only strict instantaneous `list`, `poll`, and bounded `log`
  payloads. `wait`, `write`, `submit`, `kill`, `close`, and unknown actions are
  blocked because target-process ownership is not present in the payload;
- `execute_code`, `project_create`, `skill_manage`, and delegation are unbounded
  mutations and remain blocked even in the owned worktree. File/patch tools use
  their explicit paths; `terminal` uses its positive shell/target grammar;
- `terminal` is read-only only for the positive literal grammar (`pwd`, bounded
  Git worktree/branch/rev-parse, bounded `gh pr list/view`, and
  `claude --version` / `claude auth status`). After allowed Git global options
  are parsed, the subcommand must belong to a literal builtin allowlist needed
  by the Code workflow. Unknown names are rejected both before and after claim,
  so neither `alias.<name>=!helper` nor a PATH-provided `git-<name>` can execute.
  The decision never consults `git help`, Git config, or dynamic PATH. Git
  `status`, `diff`, `log`, and `show` are never pre-claim reads because
  repository config, fsmonitor, diff drivers, textconv, or pagers can execute
  code;
- wrappers, shell operators, glob/variable/command expansion, write
  redirection, malformed syntax, unsupported options, and generic `curl` remain
  mutation-capable and require admission;
- under `--require-owned-git`, every effective target must be a Git worktree
  with the exact active agent/profile/session and the hook's parent PID/start
  identity; absent registry, ownerless worktree, dead/reused PID, and pending
  bootstrap all block;
- worktree owned by a **different live session** → block (one winner per worktree);
- same owning session → allowed (the hook never rewrites owner.json, never
  persists the hook subprocess pid);
- **business profile out of domain** → block automatically — the
  `--profile` / `--domain-prefixes` live in the hook command line (the profile's
  `config.yaml`), so the denial no longer depends on a caller remembering to
  pass flags;
- outside `--require-owned-git`, ownerless worktree or absent registry keeps the
  historical advisory behavior; corrupt registry still blocks because an owner
  scan cannot be proven complete;
- an unexpected error in the advisory hook fails open (a gate bug must not freeze
  every tool), while a *detected* conflict fails closed.

For a business profile such as `hermes-immo`, the same block is produced with the
profile's own hook line:

```yaml
hooks:
  pre_tool_call:
    - matcher: ".*"
      fail_closed: true
      command: >-
        python3 /ABS/scripts/factory_admission_hook.py
        --registry /ABS/registry --agent hermes-immo
        --profile hermes-immo --domain-prefixes JYI,HER --only-mutating
```

The hook is opt-in and consent-gated exactly like any other shell hook
(allowlist + `--accept-hooks` / `HERMES_ACCEPT_HOOKS` / `hooks_auto_accept`), and
it is skipped entirely under `--safe-mode`.

## AppSec hardening (exact-head review fixes)

The two exact-head reviews' blockers are closed and covered by
`tests/test_factory_lane_appsec.py` and `tests/test_factory_lane_integration.py`:

- **Ancestor symlink swap (`registry/locks`, `registry/lanes`).** Every
  claim/admit write descends the path with `openat(O_NOFOLLOW|O_DIRECTORY)` from a
  registry-root fd, re-validated per write, so a swapped ancestor fails the
  `openat` (`ELOOP`/`ENOTDIR`) and the write can never land outside the registry.
  On platforms without `renameat(dir_fd)` (macOS), the atomic replace re-checks
  `(st_dev, st_ino)` of the open fd against the textual path before renaming, and
  the temp file lives in the real directory so a late swap fails the rename.
- **Same-session rebind** onto an already-owned worktree is refused (guarded
  before any owner rewrite).
- **Secret-like `gateway_session_key`** is validated and rejected before write.
- **`process_start_time=None`** is classified `alive`, never `reused`, so a live
  owner without a recorded start baseline never becomes reclaimable.
- **Process identity** is transported from the long-lived parent (`--owner-pid`),
  never the ephemeral gate subprocess pid.

## Strong Code A/B terminal boundary

Admission decides *which* worktree a Code session may mutate. It does not make a
host shell safe by itself. Code A/B must also use Docker's fail-closed
workspace-only mode so the admitted Git worktree is the sole writable host
mount:

```yaml
terminal:
  backend: docker
  cwd: /ABS/exact-admitted-worktree
  docker_mount_cwd_to_workspace: true
  docker_workspace_only: true
  docker_volumes: []
  docker_forward_env: []
  docker_env: {}
  docker_extra_args: []
```

`docker_workspace_only` is opt-in and changes the Docker contract, not just its
defaults. Before contacting Docker it requires an existing exact Git worktree
at `terminal.cwd`, the `/workspace` bind, and empty custom volumes, forwarded
environment, explicit environment, and extra arguments. Invalid or incomplete
configuration raises an error; it never falls back to the local backend.

For a valid launch Hermes then enforces:

- the canonical worktree is mounted read-write at `/workspace`;
- only the worktree's Git common directory is additionally mounted, at its
  original absolute path and read-only, so status/diff/format-patch can read
  objects without allowing refs, indexes, branches, or other worktree metadata
  to be mutated;
- the container root filesystem is read-only, networking is `none`, the
  container runs as the host uid/gid, and cross-process container reuse is
  disabled;
- the selected image must already be locally inspectable and must not declare
  Docker `VOLUME` paths, which would otherwise create implicit writable
  anonymous volumes despite the read-only root filesystem;
- `/tmp`, `/var/tmp`, `/home`, `/root`, and `/cache` are container tmpfs roots;
  package caches are directed to `/cache` and never to a host cache;
- Hermes credentials, skill directories, media caches, and user-configured
  Docker mounts are not exposed to the container.

The same setting is propagated through terminal, file, patch/write, and
execute-code environment creation. Code A and Code B therefore need distinct
process/session configurations whose `terminal.cwd` values are their two
separately admitted worktrees. The admission hook remains mandatory defense in
depth: workspace-only confinement does not grant ownership and must not replace
`--require-owned-git`.

Operational prerequisites are deliberately strict: a ready Docker daemon, an
image with the required build/test toolchain, POSIX uid/gid support, and valid
Git worktree metadata. If any prerequisite is missing, Code A/B stays blocked;
do not switch `terminal.backend` to `local` as a fallback.

## Canary for Hermes Immo (no live restart in this task)

1. Use a temporary registry and two temporary git worktrees.
2. Claim one worktree as `default` on a product lane (transport a live
   `--owner-pid` so the owner is not immediately reclaimable).
3. Run `hook-session-start --agent hermes-immo --session continue` against the same worktree and verify the STOP advisory.
4. Run `admit --mode owner --hard --profile hermes-immo --domain-prefixes JYI,HER` for `SCA-740` and verify it refuses before owner creation.
5. Run `admit --mode reviewer` and verify it exits 0 without changing the owner JSON.
6. Wire `factory_admission_hook.py` into a *temporary* `config.yaml` and
   confirm `hermes hooks test pre_tool_call` (or a scripted
   `get_pre_tool_call_block_message`) blocks a `terminal`/`patch` tool in the
   foreign-owned worktree and allows the owning session — this is what
   `tests/test_factory_lane_integration.py` automates.
7. Only after review/merge should a real gateway config enable this hook before
   launching build-capable agents. Do not restart `hermes-immo` from this canary.

## Rollback

This change is additive in the repo. The tracked files are
`scripts/factory_lane.py`, `scripts/factory_admission_hook.py`, this doc, and the
tests `tests/test_factory_lane_admission.py`,
`tests/test_factory_lane_appsec.py`, `tests/test_factory_lane_integration.py`.

The admission-hook runtime wiring is **config-only and opt-in**: the gate is disabled until a
`hooks: pre_tool_call:` entry is added to `config.yaml`. To roll the wiring
back, delete that `hooks` entry (and, optionally, revoke the hook from the
shell-hook allowlist with `hermes hooks revoke <command>`) — no core file is
touched, so nothing else changes.

To roll the code back before installation, remove the files above from the
branch. If a deployed copy has already been installed into
`~/.hermes/scripts/factory_lane.py`, restore the timestamped backup of that file
and remove the `hooks` entry. The registry remains reconstructible evidence; do
do not delete `registry/` as part of rollback unless Jean explicitly asks for a
separate cleanup gate.

The strong terminal mode is independently opt-in. Before deployment, rollback
is simply removal of the branch change. After deployment, stopping Code A/B and
setting `terminal.docker_workspace_only: false` restores the previous Docker
behavior, but those profiles must remain stopped until another proven strong
confinement path is in place; local-terminal fallback is not an acceptable
rollback state.
