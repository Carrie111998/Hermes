# minimatch CommonJS compatibility adapter

`@docusaurus/core@3.10.2` uses `serve-handler@6.1.7`, which still imports
`minimatch@3` as a callable CommonJS function. The secure minimatch release
exports an object instead. This package preserves only the legacy callable
entry shape while delegating all matching behavior to the exact
`minimatch@10.2.6` package installed as `minimatch-secure`.

This adapter owns the exact patched implementation dependency. The root
`package.json` installs the adapter and scopes its override to
`serve-handler@6.1.7`. Remove this adapter when Docusaurus adopts a
`serve-handler` release that consumes modern minimatch directly.
