---
name: touchdesigner-mcp
description: "Control a running TouchDesigner instance via twozero MCP — create operators, set parameters, wire connections, execute Python, build real-time visuals. 36 native tools."
version: 1.1.0
author: kshitijk4poor
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [TouchDesigner, MCP, twozero, creative-coding, real-time-visuals, generative-art, audio-reactive, VJ, installation, GLSL]
    related_skills: [native-mcp, ascii-video, manim-video, hermes-video]

---

# TouchDesigner Integration (twozero MCP)

## When to use

Use when the user wants to build, inspect, or debug something inside a running
TouchDesigner instance: real-time visuals, generative networks, audio-reactive
patches, GLSL shaders, VJ/installation setups, projection mapping, or video
capture out of TD. Also use for TD Python scripting, operator/parameter
questions, and twozero MCP connection troubleshooting.

Do not use for offline/rendered video (`manim-video`, `hermes-video`) or for
browser-based creative coding (`p5js`).

## CRITICAL RULES

1. **NEVER guess parameter names.** Call `td_get_par_info` for the op type FIRST. Your training data is wrong for TD 2025.32.
2. **If `tdAttributeError` fires, STOP.** Call `td_get_operator_info` on the failing node before continuing.
3. **NEVER hardcode absolute paths** in script callbacks. Use `me.parent()` / `scriptOp.parent()`.
4. **Prefer native MCP tools over td_execute_python.** Use `td_create_operator`, `td_set_operator_pars`, `td_get_errors` etc. Only fall back to `td_execute_python` for complex multi-step logic.
5. **Call `td_get_hints` before building.** It returns patterns specific to the op type you're working with.
6. **Split cleanup and creation into SEPARATE MCP calls.** Destroy-and-recreate of same-named nodes inside one script causes "Invalid OP object" errors.
7. **Verify before claiming a visual result.** `td_get_errors` + `td_get_screenshot`; FPS must be > 0 before any recording.

## Security boundary

`td_execute_python` has unrestricted access to the TD Python environment and the
filesystem as the TD process user. MCP listens on localhost:40404 with **no
authentication** — any local process can drive TD. Never expose the port beyond
localhost. Full notes: `references/setup-and-environment.md`.

## Architecture

```
Hermes Agent -> MCP (Streamable HTTP) -> twozero.tox (port 40404) -> TD Python
```

36 native tools, free plugin, context-aware (knows selected OP and current network).

## Setup

```bash
bash "${HERMES_HOME:-$HOME/.hermes}/skills/creative/touchdesigner-mcp/scripts/setup.sh"
nc -z 127.0.0.1 40404 && echo "twozero MCP: READY"
```

Three manual one-time steps remain (drag the .tox in, enable the MCP toggle,
restart the Hermes session) — see `references/setup-and-environment.md`.

## Minimal end-to-end build

```
# 0. Discover — never skip
td_get_par_info(op_type="noiseTOP");  td_get_hints(topic="feedback")
td_get_focus();  td_get_network(path="/project1")

# 1. Create
td_create_operator(type="noiseTOP", parent="/project1", name="bg",
                   parameters={"resolutionw": 1280, "resolutionh": 720})
td_create_operator(type="levelTOP", parent="/project1", name="fx")
td_create_operator(type="nullTOP",  parent="/project1", name="out")

# 2. Parameters
td_set_operator_pars(path="/project1/bg", parameters={"roughness": 0.6})

# 3. Wire (no native wire tool — use Python)
td_execute_python: op('/project1/bg').outputConnectors[0].connect(op('/project1/fx').inputConnectors[0])

# 4. Verify
td_get_errors(path="/project1", recursive=true);  td_get_perf()

# 5. Look at it
td_get_screenshot(path="/project1/out")
```

Full step detail, GLSL-time / feedback / extension / point-access rules:
`references/build-workflow.md`.

## References

Load on demand with `skill_view(name="touchdesigner-mcp", file_path="references/...")`.

| To do this | Read |
|------------|------|
| Install, connect, check the hub, or review the security/licensing boundary | `references/setup-and-environment.md` |
| Run the discover → create → param → wire → verify loop, or hit a GLSL-time / feedback / extension / point-access rule | `references/build-workflow.md` |
| Record or export video, pick a codec, or run the pre-record checklist | `references/video-recording.md` |
| Avoid a known trap from real sessions | `references/pitfalls.md` |
| Look up an operator family, its params, and use cases | `references/operators.md` |
| Build a recipe: audio-reactive, generative, GLSL, instancing | `references/network-patterns.md` |
| Get the full twozero MCP tool index and parameter schemas | `references/mcp-tools.md` |
| Script TD Python: `op()`, extensions, classes | `references/python-api.md` |
| Diagnose a connection failure or debug a broken network | `references/troubleshooting.md` |
| Write GLSL: uniforms, built-ins, shader templates | `references/glsl.md` |
| Add post-FX: bloom, CRT, chromatic aberration, feedback glow | `references/postfx.md` |
| Lay out a HUD, panel grid, or BSP-style composition | `references/layout-compositor.md` |
| Render wireframes or set up a Feedback TOP | `references/operator-tips.md` |
| Use Geometry COMP: instancing, POP vs SOP, morphing | `references/geometry-comp.md` |
| Extract audio bands, detect beats, follow envelopes, or feed an FFT spectrum into GLSL | `references/audio-reactive.md` |
| Animate: LFOs, timers, keyframes, easing, expression-driven motion | `references/animation.md` |
| Wire MIDI/OSC controllers, TouchOSC, or multi-machine sync | `references/midi-osc.md` |
| Build particles with POPs or legacy particleSOP — emission, forces, collisions | `references/particles.md` |
| Do projection mapping: multi-window output, corner pin, mesh warp, edge blending | `references/projection-mapping.md` |
| Pull external data: HTTP, WebSocket, MQTT, Serial, TCP, webserverDAT | `references/external-data.md` |
| Build panel UI: custom params, panel COMPs, buttons/sliders/fields, panelExecuteDAT | `references/panel-ui.md` |
| Clone data-driven layouts with replicatorCOMP and its callbacks | `references/replicator.md` |
| Use the Execute DAT family: chop/dat/parameter/panel/op/executeDAT | `references/dat-scripting.md` |
| Build a 3D scene: lighting rigs, shadows, IBL/cubemaps, multi-camera, PBR | `references/3d-scene.md` |
| Re-run automated setup | `scripts/setup.sh` |

---

> You're not writing code. You're conducting light.
