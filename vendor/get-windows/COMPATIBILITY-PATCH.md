# Hermes build-tool compatibility patch

This directory is the published `get-windows@9.3.0` package with its runtime
source, native artifacts, license, and package version preserved. The published
tarball SHA-256 is
`f9d447c776522924bbffb9e58215f48aa7b2b3746e5cf5f90554f662051ef753`.
Only package metadata is changed:

- `@mapbox/node-pre-gyp` is pinned to `2.0.3`;
- `node-gyp` and its optional peer range are updated to `11.5.0`;
- upstream development-only dependencies are omitted from the runtime fork;
- this document and the `hermesPatch` marker are added.

The original dependency ranges resolve through `tar@6`, for which npm reports
known vulnerabilities and no maintained fixed 6.x release. A previous root npm
override crossed both build-tool major versions without changing the package's
declared compatibility contract. This local package makes that contract explicit
and keeps `node-gyp` on version 11, whose Node engine (`^18.17 || >=20.5`) covers
the repository's declared Node floor and odd-numbered Node releases.

The package version remains `9.3.0` intentionally: `node-pre-gyp` derives the
prebuilt-binary release URL from it, and the publisher's binaries live under the
`v9.3.0` GitHub release. Changing the version would force every Windows install
to compile from source.

## Verification

Desktop tests assert the local resolution and dependency metadata. The Windows
installer CI performs a real `npm ci`, verifies Electron extraction, loads the
published get-windows binding, then rebuilds get-windows from source to exercise
the fallback path with these build-tool versions.

## Removal

Replace this local package with an upstream release once `get-windows` publishes
compatible maintained build-tool dependencies. Compare that release against the
published 9.3.0 tarball before deleting this patch.
