# Hermes security patch for `image-size`

This directory vendors the published `image-size@2.0.2` package under its MIT license.
The published tarball SHA-256 is
`7a47b434cf3c1d3f50dd23f6de2587c5cb0bd55bf044c2546cbb60d979f00d36`.
Runtime implementation changes are limited to parser loop progress validation; package
metadata reports version `2.0.3-hermes.1`, marks the fork private, omits upstream-only
build scripts/development dependencies, and includes this provenance document.

The upstream package has no patched release as of 2026-08-08. The local patch rejects
zero-length/non-advancing records in the ICNS, HEIF, and JXL parsers, addressing:

- GHSA-w3rx-r6r6-pgpr
- GHSA-5p2g-fcmc-qvqq

Remove this fork and the `package.json` override after upstream publishes a fixed release.
The regression command exercises ESM and CommonJS parser modules plus the public buffer and
file APIs in killable child processes, and runs in the docs-site CI workflow.
