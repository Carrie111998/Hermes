# Session-Bridge Marker-Key Rotation

**Status:** implemented 2026-08-31 (keyring for the sidebar reservation ledger
and terminal-resolution lanes; extended the same day to origin detection,
hydration markers, Claude visibility bindings, lineage cursors, and durable
characterization records). **Audience:** whoever rotates
`~/.hermes/session-bridge/marker-key`.

## Why this exists

The origin-marker HMAC key signs durable artifacts whose validation outlives
the key:

- **Signed bridge markers** embedded in native Codex thread content at create
  time (`HERMES_SESSION_BRIDGE_V1:...`). The signature half depends on the
  key; the unsigned body does not.
- **Create-reservation recovery keys** in the sidebar ledger
  (`hermes-session-bridge-create-v1:<hmac>`), derived from the signed marker
  *and* the key, and stored both in `session_bridge_state` and as the native
  task's `thread_source`.
- **Reconciliation-proof marker digests** (`sha256(signed marker)`) bound
  into v2 attempt-zero reservations.

The 2026-08-27 10:57 EDT rotation (leak response, old key destroyed) proved
what happens without a rotation story, measured 2026-08-31 (loops claims
`sidebar-terminal-failures-cleared-20260831` →
`sidebar-marker-key-rotation-20260831`):

1. Every pre-rotation reservation failed the recovery-key equality in
   `validate_sidebar_create_reservation` — the acknowledge verbs and the
   executor retry lane recompute the expected key from the *current* secret,
   so all of them refused with `*_snapshot_mismatch` /
   `native_create_ambiguous`.
2. Marker probing (`find_by_marker_including_archived`, `reconcile_marker`)
   could no longer *authenticate* pre-rotation markers. The inventory search
   still finds them (the search term is the key-independent unsigned prefix),
   but authentication fails, so a pre-rotation task for the probed identity
   reads as an unauthenticated claim → `marker_conflict`, and
   `absence_proven` proofs claim more than they can know for pre-rotation
   creates.
3. Nothing could be repaired retroactively because no copy of the old key
   survived (correct for a leak — see below — but it must be a *choice*, not
   an accident of having no keyring).

## The keyring

Retired keys live in `~/.hermes/session-bridge/marker-key-retired/`, one file
per retired key, named with a sortable UTC retirement stamp:

```
~/.hermes/session-bridge/
  marker-key                                  # the live signing key
  marker-key-retired/
    20260827T145700Z-marker-key               # previous key, retired at that stamp
```

Resolution (`session_bridge.mcp_server.resolve_retired_marker_keys`):

- Missing directory ⇒ no retired keys (the pre-rotation-story default).
- Each file must satisfy the exact restricted-file discipline of the live key
  (owner+SYSTEM-only ACL, regular non-redirect file, 32–4096 bytes); any
  violation fails closed.
- Newest retirement first; duplicates of the live key or of each other are
  skipped; more than **4** retired keys is an error — retire old epochs (see
  *When to delete a retired key*) instead of hoarding.

**Signing never uses retired keys.** New markers, new recovery keys, new
proof digests always come from `marker-key`. Retired keys are used only to
*validate* what an earlier epoch minted:

| Surface | Behavior with retired keys |
| --- | --- |
| `validate_sidebar_create_reservation` (executor retry lane, unbound/v2 acknowledge verbs) | accepts a reservation whose recovery key re-derives under any keyring epoch |
| `SidebarThreadVerifier` / `_verified_sidebar_projection` (reconcile, terminal probes) | authenticates thread markers against current then retired keys; unauthenticated claims of the probed identity still block |
| Executor bind lane (`decode_sidebar_registration_identity` on the initial prompt) | tries current then retired keys |
| Store acknowledge resolutions (precreate / unbound / v2 attempt-zero) and the cutover replay validator | recovery-key equality is any-epoch; the v2 lane matches marker digest **and** recovery key pairwise per epoch, never mix-and-match |
| Origin detection (`_detect_origin` and `projection_has_marker_payload` in the Codex **and** Claude source adapters) | decodes embedded thread/transcript markers against current then retired keys, so a pre-rotation bridge-created thread never reclassifies as NATIVE origin (which would have made it sidebar-eligible) |
| Hydration lanes (`SidebarHydrationExecutor`, the `session_sidebar_hydration_pending` broker verb, the seed backfill classifier) | decodes stored `HERMES_SESSION_HYDRATION_V1` markers and classifies legacy placeholder prompts through the keyring; fresh hydration markers are always minted with the current key |
| Claude visibility identity bindings (`validate_claude_visibility_identity_binding`, the registrar claim/launch/reconcile lanes, `_insert_claude_visibility_job`) | a stored `signed_marker` authenticates under any keyring epoch; freshly derived identities still sign with the current key |
| Claude lineage reconciliation cursors | a cursor minted just before a rotation stays honored through retired epochs; new cursors sign with the current key |
| Durable characterization records (`_read_characterization_record` for the boot/lineage sync and completed evidence, Codex origin guards via `load_codex_characterization_origins`, `record_claude_visibility_characterization`) | signature verification is any-epoch (the stored signed marker binds pairwise per epoch, never mix-and-match); every write re-signs with the current key |

The recovery-key *string* stored in the reservation is what probes native
inventory (`thread_source` equality), so once validation accepts the old
epoch, recovery of the pre-rotation native task works unchanged.

## Runbook: hygiene rotation (key NOT compromised)

From PowerShell, with the bridge stopped or between deliveries:

```powershell
$root = "$env:USERPROFILE\.hermes\session-bridge"
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
New-Item -ItemType Directory -Force "$root\marker-key-retired" | Out-Null
Move-Item "$root\marker-key" "$root\marker-key-retired\$stamp-marker-key"
# Mint the replacement (32 random bytes, no trailing newline):
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
[System.IO.File]::WriteAllBytes("$root\marker-key", $bytes)
```

Then apply the restricted ACL to **both** the new key and the retired file
(same recipe the installer uses):

```powershell
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($f in @("$root\marker-key", "$root\marker-key-retired\$stamp-marker-key")) {
  icacls $f /inheritance:r /grant:r "*${sid}:(F)" "*S-1-5-18:(F)"
  icacls $f /remove:g "*S-1-3-4" "*S-1-5-32-544"
}
```

Restart the bridge. In-flight reservations, terminal-resolution lanes, and
pre-rotation native-task probing keep working through the keyring. The
`marker-key-retired` directory rides the nightly `Hermes-OOBStateBackup`
alongside the live key.

## Runbook: leak rotation (key compromised)

**Do NOT move a leaked key into `marker-key-retired/`.** A retired key
authenticates thread-content markers during probing, and thread content is
attacker-influenceable — keeping a leaked key trusted preserves exactly the
forgery the rotation is answering. A leak rotation is: destroy the old key,
mint the new one (steps above minus the `Move-Item`), and accept that
pre-rotation ledger records break as they did on 2026-08-27. The operator
fallbacks are the ones proven that day: `retry_failed_sidebar_job` (fresh
create authority after operator audit) and the bound-lane
`sidebar-retry-bound` verbs, which never depend on key epochs.

If a future incident needs validation-without-probing (the reservation ledger
lives behind the store's own trust boundary, unlike thread content), that is
a *ledger-only trust tier* for keyring entries — deliberately not implemented
yet; do not simulate it by retiring a leaked key.

## When to delete a retired key

A retired key is only needed while records minted under it are still in
flight. Delete `marker-key-retired/<stamp>-marker-key` when **all** of:

- no `sidebar_pending` / `sidebar_retry` / blocking `sidebar_failed` job
  predates the rotation stamp (`hermes-session-bridge sidebar-status`),
- no live create-reservation in `session_bridge_state` has `reserved_at`
  before the rotation stamp, and
- no terminal-resolution acknowledgement of a pre-rotation record is still
  expected.

Pre-rotation *bound* threads need no key at all for delivery; only marker
re-probing of them does, which ends once their jobs are terminal.

## Deliberately epoch-pinned surfaces (do NOT extend the keyring here)

The 2026-08-31 extension closed the previously listed residual surfaces
(origin detection, hydration markers, visibility bindings, lineage cursors,
durable characterization records — see the table above). What remains
current-key-only is *pinned on purpose*:

- **In-flight characterization operations.** A live Claude-visibility
  characterization (the active operation record's resume/abort/cleanup
  machinery in `characterize.py`, including cleanup capability tokens and the
  abort claim/retire lanes) characterizes the *current* configuration; a
  rotation mid-operation invalidates the probe by design. Do not rotate while
  `.claude-visibility-operation.json` or `.abort-claims/` entries exist —
  finish or abort the operation first. A pre-rotation record wedged there is
  cleaned up manually (quarantine the file), not by retiring keys into its
  validators. The *evidence* sync of such records (boot/lineage sync,
  `.cleanup-completed/`) does accept retired keys, so provenance survives.
- **The Codex characterization probe key.** `_characterize_codex` mints an
  ephemeral per-run key (`secrets.token_bytes(32)`); it never touches the
  production keyring. The durable provenance for those threads is the
  origin guard, which is production-signed and keyring-aware.
- **Every signing path.** Fresh markers, recovery keys, hydration markers,
  registration prompts, cursors, and record writes always use the live
  `marker-key`; retired keys validate only.
