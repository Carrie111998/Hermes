# Compresr plugin (out-of-tree, SDK-backed)

Query-aware **context compaction** and **large tool-output** compression via
[Compresr](https://compresr.ai) (YC W26). Fail-open and inert without an API key.

This is the **new-style** integration: none of the compression logic lives in
this repo. It ships in the `compresr` PyPI package under
`compresr.integrations.hermes`, and this plugin is a thin shim that:

1. registers Compresr's cache subdir (`cache/compresr/tool-output`) with the
   generic out-of-tree cache surface
   (`tools.credential_files.register_cache_dir`), so recovery paths resolve on
   **Docker / Modal / SSH** backends — not only Local; and
2. delegates to the SDK's `register(ctx)`.

## Why out-of-tree

Upstream Hermes declines in-tree vendor SaaS integrations under `plugins/`
(they must ship as a standalone package + pip entry point). So the compression
code lives in the Compresr SDK, and Hermes only needs a small, **vendor-neutral**
plugin surface — provided here — for any cache-backed out-of-tree plugin. This
plugin is the reference consumer of that surface.

## Install

```bash
# 1. Install the SDK into the interpreter that runs Hermes:
pip install 'compresr>=2.8.4'          # or: compresr-sdk login   (writes the key)

# 2. Set the key (if not using `compresr-sdk login`):
echo 'COMPRESR_API_KEY=cmp_...' >> ~/.hermes/.env

# 3. Enable the plugin + (optionally) select the context engine in ~/.hermes/config.yaml:
#    plugins:
#      enabled: [compresr]
#    compresr:
#      tool_output_enabled: true
#    context:
#      engine: compresr        # optional: also route compaction through Compresr
```

Restart Hermes. `/compresr` shows live stats. With no key the plugin loads but
stays inactive (built-in compaction is used).

> The SDK package (`compresr`) is *not* vendored here and is not auto-installed
> by Hermes; see `requirements.txt`.
