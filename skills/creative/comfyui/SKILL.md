---
name: comfyui
description: "Generate images, video, and audio with ComfyUI — install, launch, manage nodes/models, run workflows with parameter injection. Uses the official comfy-cli for lifecycle and direct REST/WebSocket API for execution."
version: 5.1.0
author: [kshitijk4poor, alt-glitch, purzbeats]
license: MIT
platforms: [macos, linux, windows]
compatibility: "Requires ComfyUI (local, Comfy Desktop, or Comfy Cloud) and comfy-cli (auto-installed via pipx/uvx by the setup script)."
prerequisites:
  commands: ["python3"]
setup:
  help: "Run scripts/hardware_check.py FIRST to decide local vs Comfy Cloud; then scripts/comfyui_setup.sh auto-installs locally (or use Cloud API key for platform.comfy.org)."
metadata:
  hermes:
    tags:
      - comfyui
      - image-generation
      - stable-diffusion
      - flux
      - sd3
      - wan-video
      - hunyuan-video
      - creative
      - generative-ai
      - video-generation
    related_skills: [stable-diffusion-image-generation, image_gen]
    category: creative
---

# ComfyUI

Generate images, video, audio, and 3D content through ComfyUI using the
official `comfy-cli` for setup/lifecycle and direct REST/WebSocket API
for workflow execution.

## When to use this skill

- User asks to generate images with Stable Diffusion, SDXL, Flux, SD3, etc.
- User wants to run a specific ComfyUI workflow file
- User wants to chain generative steps (txt2img → upscale → face restore)
- User needs ControlNet, inpainting, img2img, or other advanced pipelines
- User asks to manage ComfyUI queue, check models, or install custom nodes
- User wants video/audio/3D generation via AnimateDiff, Hunyuan, Wan, AudioCraft, etc.

## Reference map

| To do this | Read |
|---|---|
| Install ComfyUI, choose local vs Cloud, read hardware verdicts, download models/nodes | `references/setup-and-install.md` |
| Look up any `comfy ...` command and its flags | `references/official-cli.md` |
| Look up REST/WebSocket endpoints and payload schemas (local + cloud) | `references/rest-api.md` |
| Understand API-format JSON, node types, and parameter mapping | `references/workflow-format.md` |
| Convert an official `comfyui-workflow-templates` file to API format (Reroute bypass, dotted dynamic-input keys, ffmpeg stitch) | `references/template-integrity.md` |
| Run a workflow, inject params, batch/sweep, upload images for img2img/inpaint, manage the queue | `references/execution-recipes.md` |
| Work against Comfy Cloud (auth, endpoint renames, tier concurrency, signed-URL downloads) | `references/cloud-usage.md` |
| Pick the right script or command for a given user request; see what each script and bundled workflow does | `references/scripts-and-commands.md` |
| Diagnose a failure or run the full verification checklist | `references/pitfalls-and-verification.md` |

Load `template-integrity.md` whenever you're starting from an official template.
Load `setup-and-install.md` before running any install command.

## Red lines

- **Ask local vs Cloud FIRST.** When a user asks to set up ComfyUI, ask whether
  they want Comfy Cloud (hosted, API key) or Local before running any install
  command or hardware check.
- **Never force a local install past a `cloud` verdict silently.** If
  `hardware_check.py` returns `cloud`, show the `notes` array verbatim and ask
  whether to switch to Cloud or force local (which will OOM or be unusably slow).
- **Workflow JSON is arbitrary code.** Custom nodes run Python, so submitting an
  unknown workflow has the same trust profile as `eval`. Inspect workflows from
  untrusted sources before running them.
- **Keep path-traversal protection on.** Server-supplied output filenames go
  through `safe_path_join` so nothing escapes `--output-dir`. Custom save nodes
  can produce arbitrary paths.
- **Never leak the Cloud API key downstream.** `/api/view` 302-redirects to a
  signed storage URL; `X-API-Key` must be stripped before following it (the
  scripts do this).
- **API format only.** `/api/prompt` and every script require API-format JSON
  (each node has `class_type`). Editor format (top-level `nodes`/`links` arrays)
  is not executable — re-export via "Workflow → Export (API)".
- **A server must be running** for any execution. Verify with
  `curl http://127.0.0.1:8188/system_stats`.

## Minimal workflow

```bash
# 0. Is everything ready? (comfy-cli on PATH, server up, checkpoint present, smoke test)
python3 scripts/health_check.py

# 1. Get a workflow in API format — from workflows/, or exported from the web UI.
# 2. See what's controllable.
python3 scripts/extract_schema.py workflow_api.json --summary-only

# 3. Check the workflow's deps against the running server (auto_fix_deps.py installs them).
python3 scripts/check_deps.py workflow_api.json

# 4. Run it.
python3 scripts/run_workflow.py \
  --workflow workflow_api.json \
  --args '{"prompt": "a beautiful sunset over mountains", "seed": -1, "steps": 30}' \
  --output-dir ./outputs
```

Cloud is the same call plus `export COMFY_CLOUD_API_KEY=...` and
`--host https://cloud.comfy.org`.

Every script emits JSON to stdout describing each output file — present those
paths to the user:

```json
{
  "status": "success",
  "prompt_id": "abc-123",
  "outputs": [
    {"file": "./outputs/sdxl_00001_.png", "node_id": "9",
     "type": "image", "filename": "sdxl_00001_.png"}
  ]
}
```

Docs: https://docs.comfy.org/installation ·
https://docs.comfy.org/comfy-cli/getting-started ·
https://docs.comfy.org/get_started/cloud ·
https://docs.comfy.org/development/cloud/overview
