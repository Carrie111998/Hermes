---
title: Local Hermes Base
sidebar_position: 14
---

# Local Hermes Base

When you run Hermes on a personal machine, the most stable setup is a small
reviewable layer of intent plus the noisy runtime state left out of git.

This guide captures the split we recommend for a long-lived local deployment.

## Keep in version control

These are the files and notes that should move slowly and deserve review:

- `SOUL.md` or other persona / operating notes
- a sanitized `config.yaml` base with secrets removed
- docs that explain how the local setup is meant to evolve
- helper scripts that are stable enough to reuse across machines

## Keep out of version control

These change too often or contain sensitive material:

- session history and transcript databases
- caches, locks, temp files, checkpoints, downloads
- auth material and API keys
- generated artifacts that can be recreated on demand
- machine-specific runtime state

## Update cadence

A Hermes update should not automatically force a local config commit.
Only commit a change when the distilled intent changed:

- a new stable config key is worth adopting
- a model/provider choice changed on purpose
- the operating guidance in `SOUL.md` changed materially
- the layout of the local source tree changed

If the update only touched transient state or internal defaults that you do
not want to adopt yet, leave the repo alone.

## Practical pattern

A clean local Hermes base usually ends up with:

- one small sanitized config file
- one persona / operating note file
- a short README or policy note explaining what gets bumped and why

That keeps the review surface small and makes future iterations faster to
reason about.
