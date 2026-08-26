# Hermes Mobile

> **Status: active upstream contribution — not an official release and not yet a supported install path.**

Hermes Mobile brings the **real Hermes Desktop experience** to Android. It packages the Desktop renderer in a Capacitor shell and connects it to the same remote Hermes gateway that powers Desktop's Remote Gateway mode.

## 📱 What this is

- A native Android shell for the existing Hermes Desktop renderer.
- A remote client for the same Hermes sessions, profiles, skills, and tool activity you use elsewhere.
- An upstream-focused effort: changes should be narrow, testable, secure, and suitable for review in `NousResearch/hermes-agent`.

## 🚫 What this is not

- Not a Termux/local-agent replacement.
- Not a separate mobile chat UI or a copy of the Desktop source tree.
- Not an official Hermes app, signed release, or APK download source yet.
- Not a way to expose a Hermes gateway publicly or to copy desktop credentials to a phone.

## 🧭 Architecture

```mermaid
flowchart LR
  U[Phone user] --> A[Capacitor Android shell]
  A --> R[Bundled Hermes Desktop renderer]
  R --> B[Typed mobile bridge]
  B -->|HTTPS + WSS| G[Remote Hermes gateway]
  G --> H[Hermes sessions, tools, memory, and agent runtime]
  A --> K[Platform permissions and secure local storage]

  classDef boundary fill:#fff4e5,stroke:#d97706,color:#111827;
  classDef trusted fill:#ecfdf5,stroke:#059669,color:#111827;
  class A,R,B,K boundary;
  class G,H trusted;
```

The bridge is deliberately explicit. Desktop-only capabilities must either map to a meaningful phone capability (for example, clipboard, file picker, haptics, or microphone permission) or be disabled cleanly. A missing bridge capability must never silently pretend to work.

## 🔐 Security model

- Connect only to a user-controlled remote Hermes gateway over **HTTPS/WSS**.
- Keep gateway tokens and session material out of source control, logs, screenshots, and CI output.
- Store mobile connection secrets through the platform-backed secure-storage path; do not use browser `localStorage` for secrets.
- Open external links only through an allowlisted system-browser path; gateway content cannot invoke arbitrary device schemes.
- A release will require its own reviewed package identifier, signing identity, update channel, and real-device verification.

## 🧪 Contributor path

This workspace is intentionally set up as a normal Hermes monorepo workspace. Start from the repository root:

```bash
npm ci
npm run typecheck --workspace apps/mobile
npx vitest run --config apps/mobile/vitest.config.ts
npm run build --workspace apps/mobile
npm run cap:sync --workspace apps/mobile
cd apps/mobile/android && ./gradlew assembleDebug
```

> These are the intended quality gates. They must all be run successfully on the exact branch and Android toolchain before this README can describe an APK as buildable or installable.

### Prerequisites

- Node.js version required by the root `package.json`.
- JDK 17.
- Android SDK platform/build tools accepted by the Capacitor/Gradle project.
- A private, reachable Hermes gateway for an integration test. Do **not** use a public or credential-bearing example URL.

### Controlled release candidates

`Mobile Release Candidate` is a manual GitHub Actions workflow that builds a signed AAB only when all four release-signing secrets are configured in the canonical repository. It never falls back to the Android debug certificate. The workflow uploads the resulting AAB as a short-lived private Actions artifact; publishing to Play or another channel remains a separate, reviewed decision.

## ✅ First release bar

Before any signed Android build is shared, it must prove:

1. Remote connection configuration and authentication work for both REST and WebSocket paths.
2. Reverse-proxy path prefixes remain intact for WebSocket URLs.
3. A session loads, one message streams, and reconnect works after foreground/background.
4. Mobile bridge tests cover token, password-capable gateway, profile, reconnection, and error paths. OAuth-only gateway authorization is explicitly out of scope for this MVP.
5. Phone controls are touch-accessible; keyboard, back navigation, safe areas, and file selection behave correctly.
6. The app has passed real-device testing and a review of secrets, permissions, WebView navigation, and update behavior.

## 🤝 Upstream relationship

This work builds on—and preserves credit for—existing Hermes mobile contributions. Current open mobile PRs are research and implementation references, not install sources:

- [#49834 — Capacitor Android thin client](https://github.com/NousResearch/hermes-agent/pull/49834)
- [#52673 — native mobile shell for Hermes Desktop](https://github.com/NousResearch/hermes-agent/pull/52673)
- [#53772 — Expo mobile shell integration branch](https://github.com/NousResearch/hermes-agent/pull/53772)
- [#64962 — Capacitor iOS renderer client](https://github.com/NousResearch/hermes-agent/pull/64962)

When upstream Desktop changes, this project should rebase onto a pinned current `main`, run the full mobile test/build gates, and produce a reviewable candidate. It must never silently ship a new upstream renderer into an installed mobile app.

## 📚 Related documentation

- [Hermes contribution guide](../../CONTRIBUTING.md)
- [Desktop architecture rules](../desktop/AGENTS.md)
- [Android / Termux guide](../../website/docs/getting-started/termux.md) — a separate local-CLI path, not this project
