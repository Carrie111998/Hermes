# hermes-ngit

Hermes skill for decentralized Git over Nostr via [ngit](https://gitworkshop.dev/ngit).

## What is this?

A skill file for Hermes that provides instructions and workflows for working with
Nostr-based Git repositories. ngit is a sovereign, decentralized Git hosting protocol
built on Nostr — no central forge, no daemon required.

## Links

- **GitHub:** https://github.com/rinchen/hermes-ngit
- **Nostr:** published via `ngit init`; view on [gitworkshop.dev](https://gitworkshop.dev/npub1wwaq5gyk7yljly3cwl3wleuk79nz63ukpp2a6lq5x4q9s9r4nrgqjk3dlv/relay.ngit.dev/hermes-ngit)
- **Radicle:** [rad:z2fGx8T85zCue2efEmSHL8NxKCvqd/z6MkncGT6ghwtmF9utKMvuqxtK93WHj56dB3GmrDyFQoFWcj](https://app.radicle.xyz/nodes/seed.radicle.xyz/rad:z2fGx8T85zCue2efEmSHL8NxKCvqd/z6MkncGT6ghwtmF9utKMvuqxtK93WHj56dB3GmrDyFQoFWcj)

## Features

- Announce repos to Nostr (`ngit init`) and keep gitworkshop current via `git push`
- Native `git clone` / `git push` against `nostr://` remotes (no daemon)
- Session repo detection for Nostr-enabled projects
- Full PR and issue lifecycle: open, view, comment, checkout, merge, close, reopen, label
- Account management (bunker/NIP-46 login, inline `--nsec` for CI) and key-flags reference
- Triple-push configuration (GitHub + Radicle + Nostr)

## Key Lesson

`ngit sync` only pushes git refs — it does **not** update the NIP-34 announcement that
gitworkshop.dev reads. To keep gitworkshop current, add the `nostr://` URL as an `origin`
pushurl so `git push` drives `git-remote-nostr` and re-announces HEAD automatically.

## Usage

Copy `SKILL.md` (plus `LICENSE` and `NOTICE`) into your Hermes skills directory to enable
ngit workflows, or install via the upstream PR under `optional-skills/devops/hermes-ngit`.

## Credit / License

This skill builds on **Dan Conway's** ngit command reference
(`DanConwayDev/ngit-cli`, `skills/ngit/SKILL.md`), incorporated under the
**Creative Commons Attribution-ShareAlike 4.0 International (CC-BY-SA-4.0)** license in
accordance with the source material's share-alike terms. Original framing and multi-mirror
packaging by Joey Stanford.

The full license text is in the `LICENSE` file; attribution is also recorded in `NOTICE`.
