# brace-expansion 5.0.8 compatibility adapter

This directory contains the published `brace-expansion@5.0.8` `dist/` files
unchanged, plus `legacy.cjs`: a small CommonJS adapter that preserves the
callable export used by `brace-expansion` 1.x/2.x consumers while also exposing
the named 5.x API (`expand`, `EXPANSION_MAX`, and `EXPANSION_MAX_LENGTH`).
The adapter preserves the old export shape, but deliberately applies both
patched 5.0.8 safety caps: at most 100,000 results and 4,000,000 total output
characters. ES module consumers receive the upstream 5.0.8 module directly.

Hermes needs the adapter because CVE-2026-14257 / GHSA-mh99-v99m-4gvg affects
every release through 5.0.7, but several maintained Electron, ESLint, and
Docusaurus build dependencies still request the callable 1.x/2.x API. A global
major-version override without this adapter breaks those consumers.

Provenance:

- npm package: `brace-expansion@5.0.8`
- upstream: https://github.com/juliangruber/brace-expansion
- npm tarball SHA-1: `135ad0d8d808eb18eb5e0ec9a21f3a0b92ef18cf`
- npm integrity: `sha512-JZyDyq3D4AUifKTPOB7DELf6XsB3WdPuNxCtob1vFXPsSXhdAiHBWJ/tJ8HAc9aH84BK+5JFZLNkJKx3G9kzQg==`
- upstream license: MIT (retained in `LICENSE`)

Remove this adapter once every parent dependency in both npm graphs accepts a
patched native API line, or patched compatible 1.x/2.x releases are available.
