# Hermes Mobile Working State

**Canonical active worktree:** `/home/argos/Argos/Projects/hermes-android-salvage` on `salvage/mobile-49834`.

## Coordination rule

Before editing, inspect `git status`, this file, and the Notion Android MVP record. Do not copy changes from the separate reference worktree `/home/argos/Argos/Projects/hermes-android-client` or overwrite current Desktop source wholesale. Preserve upstream Desktop renderer reuse; mobile-specific changes stay narrow and Android-focused.

## Current verified baseline

- Desktop and mobile TypeScript checks pass.
- Mobile test suite passes (42 tests at the last full gate).
- Capacitor sync and Android debug APK build pass.
- Owner reports the v3 physical-device QA pass complete; detailed screenshots and notes are not stored in this repository.
- Current private device-test APK: `Hermes-Mobile-Android-debug-v11-fold-layout-repair.apk` (debug-signed only; versionCode 7 / versionName 1.0.6) in the owner’s Drive. It supersedes v10 after real Fold screenshots exposed a squeezed desktop layout: docked rails remained at the Fold’s ~928 CSS-pixel width, overlay rails/search/actions layered over content, and the composer exposed too many voice controls. v11 collapses rails below 1024px, synchronizes drawer state with overlay close actions, makes overlays one-column/touch-sized below 1024px, removes the floating desktop search pill from the compact overlay, and folds phone voice controls into one menu. Verified: 52 mobile tests, mobile typecheck, targeted Desktop UI tests, renderer build, Capacitor sync, Android debug compilation, APK signature, and packaged layout contracts. **Current physical-device blocker:** install v11 as an in-place update and return Fold screenshots for the chat drawer, Settings, composer, portrait, and landscape before any release claim.
- HTTPS/WSS-only gateway policy, Android Keystore storage, resume/network reconnect, touch/landscape behavior, and permission hardening are implemented and verified by automated gates.
- **2026-08-22 emulator evidence:** Android API 36 successfully installed the rebuilt debug APK, reached the real remote Desktop shell over the Tailnet URL, loaded the session sidebar, and completed a no-tools message/reply round trip. The first-launch target was persisted through the Android Keystore path.
- **2026-08-22 connection repair:** the initial `/api/sessions?limit=0` preflight replaced the interactive-login-only `/api/auth/providers` endpoint but the sessions endpoint ignores its limit and returns private metadata. v10 uses authenticated `/api/dashboard/font` instead; the live route returns 200 for the valid token and 401 for an invalid token. The Tailnet proxy regenerates the upstream WebSocket handshake instead of forwarding browser handshake headers; the live authenticated WSS preflight succeeds.
- **2026-08-23 touch QA:** a real Android touch pass found two phone blockers: closing the session drawer left its store open (hiding any re-entry control), and an opened Desktop preview pane consumed the phone-width split and produced unreadable one-word columns. The v1.0.4 candidate adds a persistent 44px session toggle, forces drawer state closed on session selection/dismissal, and suppresses split previews at phone width. Its policy regression tests pass (3), full mobile tests pass (51), typecheck/build/Capacitor sync/Android debug assembly pass. The temporary Android emulator repeatedly hit host-level System UI/OOM instability after extended runs, so this exact post-fix APK still needs one final stable-device touch pass before a release claim.
- The verified artifact is still a **debug-signed test APK**. A controlled release/AAB remains blocked on the approved release-signing vault lane; do not relabel the debug artifact as a release.
- Current local notifications are foreground-only callbacks from the renderer. Background completion notifications require a separately verified server-to-device push design; no such proof exists yet.

## Do not claim complete

- No controlled signed release/AAB has been generated yet; upstream signing-secret access is required.
- OAuth-only gateway authorization is not implemented.
- The final two-pass device QA is owner-confirmed, but independent detailed artifacts must be reconciled before an upstream PR.

- This project changes only the Desktop renderer/mobile shell. It must use the existing remote Hermes backend protocol unchanged; no Hermes Agent backend/core changes are in scope.
- A future separate project may evaluate a containerized on-device Hermes Agent with explicitly approved Android elevated capabilities. Accessibility, screen control, and phone-local agent execution are expressly excluded from this work.

## Next owner-safe sequence

1. Reconcile owner-provided device QA evidence into Notion.
2. Run the manual signed-AAB workflow only after upstream signing secrets are configured.
3. Independently review the exact final diff, run full gates, then open a draft upstream PR.
