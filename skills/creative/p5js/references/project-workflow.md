# Project Workflow

The full production path for a p5.js deliverable: stack choices, the six-stage
pipeline in detail, technical design decisions, preview loop, export matrix, and
the agent-side execution steps.

## Stack

Single self-contained HTML file per project. No build step required.

| Layer | Tool | Purpose |
|-------|------|---------|
| Core | p5.js 1.11.3 (CDN) | Canvas rendering, math, transforms, event handling |
| 3D | p5.js WebGL mode | 3D geometry, camera, lighting, GLSL shaders |
| Audio | p5.sound.js (CDN) | FFT analysis, amplitude, mic input, oscillators |
| Export | Built-in `saveCanvas()` / `saveGif()` / `saveFrames()` | PNG, GIF, frame sequence output |
| Capture | CCapture.js (optional) | Deterministic framerate video capture (WebM, GIF) |
| Headless | Puppeteer + Node.js (optional) | Automated high-res rendering, MP4 via ffmpeg |
| SVG | p5.js-svg 1.6.0 (optional) | Vector output for print — requires p5.js 1.x |
| Natural media | p5.brush (optional) | Watercolor, charcoal, pen — requires p5.js 2.x + WEBGL |
| Texture | p5.grain (optional) | Film grain, texture overlays |
| Fonts | Google Fonts / `loadFont()` | Custom typography via OTF/TTF/WOFF2 |

### Version Note

**p5.js 1.x** (1.11.3) is the default — stable, well-documented, broadest library compatibility. Use this unless a project requires 2.x features.

**p5.js 2.x** (2.2+) adds: `async setup()` replacing `preload()`, OKLCH/OKLAB color modes, `splineVertex()`, shader `.modify()` API, variable fonts, `textToContours()`, pointer events. Required for p5.brush. See `references/core-api.md` § p5.js 2.0.

## Pipeline

Every project follows the same 6-stage path:

```
CONCEPT → DESIGN → CODE → PREVIEW → EXPORT → VERIFY
```

1. **CONCEPT** — Articulate the creative vision: mood, color world, motion vocabulary, what makes this unique
2. **DESIGN** — Choose mode, canvas size, interaction model, color system, export format. Map concept to technical decisions
3. **CODE** — Write single HTML file with inline p5.js. Structure: globals → `preload()` → `setup()` → `draw()` → helpers → classes → event handlers
4. **PREVIEW** — Open in browser, verify visual quality. Test at target resolution. Check performance
5. **EXPORT** — Capture output: `saveCanvas()` for PNG, `saveGif()` for GIF, `saveFrames()` + ffmpeg for MP4, Puppeteer for headless batch
6. **VERIFY** — Does the output match the concept? Is it visually striking at the intended display size? Would you frame it?

Stage 1 (CONCEPT) and stage 6 (VERIFY) are detailed in `references/creative-direction.md`.

## Step 2: Technical Design

- **Mode** — which of the 7 modes from the SKILL.md mode table
- **Canvas size** — landscape 1920x1080, portrait 1080x1920, square 1080x1080, or responsive `windowWidth/windowHeight`
- **Renderer** — `P2D` (default) or `WEBGL` (for 3D, shaders, advanced blend modes)
- **Frame rate** — 60fps (interactive), 30fps (ambient animation), or `noLoop()` (static generative)
- **Export target** — browser display, PNG still, GIF loop, MP4 video, SVG vector
- **Interaction model** — passive (no input), mouse-driven, keyboard-driven, audio-reactive, scroll-driven
- **Viewer UI** — for interactive generative art, start from `templates/viewer.html` which provides seed navigation, parameter sliders, and download. For simple sketches or video export, use bare HTML

## Step 3: Code the Sketch

For **interactive generative art** (seed exploration, parameter tuning): start from `templates/viewer.html`. Read the template first, keep the fixed sections (seed nav, actions), replace the algorithm and parameter controls. This gives the user seed prev/next/random/jump, parameter sliders with live update, and PNG download — all wired up.

For **animations, video export, or simple sketches**: use the bare single-file HTML skeleton in SKILL.md.

Optional CDN includes to uncomment as needed:

```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/addons/p5.sound.min.js"></script>
<script src="https://unpkg.com/p5.js-svg@1.6.0"></script>                                    <!-- SVG export -->
<script src="https://cdn.jsdelivr.net/npm/ccapture.js-npmfixed/build/CCapture.all.min.js"></script>  <!-- video capture -->
```

Key implementation patterns:
- **Seeded randomness**: Always `randomSeed()` + `noiseSeed()` for reproducibility
- **Color mode**: Use `colorMode(HSB, 360, 100, 100, 100)` for intuitive color control
- **State separation**: CONFIG for parameters, PALETTE for colors, globals for mutable state
- **Class-based entities**: Particles, agents, shapes as classes with `update()` + `display()` methods
- **Offscreen buffers**: `createGraphics()` for layered composition, trails, masks

## Step 4: Preview & Iterate

- Open HTML file directly in browser — no server needed for basic sketches
- For `loadImage()`/`loadFont()` from local files: use `scripts/serve.sh` or `python3 -m http.server`
- Chrome DevTools Performance tab to verify 60fps
- Test at target export resolution, not just the window size
- Adjust parameters until the visual matches the concept from the creative vision step

## Step 5: Export

| Format | Method | Command |
|--------|--------|---------|
| **PNG** | `saveCanvas('output', 'png')` in `keyPressed()` | Press 's' to save |
| **High-res PNG** | Puppeteer headless capture | `node scripts/export-frames.js sketch.html --width 3840 --height 2160 --frames 1` |
| **GIF** | `saveGif('output', 5)` — captures N seconds | Press 'g' to save |
| **Frame sequence** | `saveFrames('frame', 'png', 10, 30)` — 10s at 30fps | Then `ffmpeg -i frame-%04d.png -c:v libx264 output.mp4` |
| **MP4** | Puppeteer frame capture + ffmpeg | `bash scripts/render.sh sketch.html output.mp4 --duration 30 --fps 30` |
| **SVG** | `createCanvas(w, h, SVG)` with p5.js-svg | `save('output.svg')` |

Full detail, including deterministic headless capture and per-clip multi-scene
architecture, lives in `references/export-pipeline.md`.

## Agent Workflow

When building p5.js sketches:

1. **Write the HTML file** — single self-contained file, all code inline
2. **Open in browser** — `open sketch.html` (macOS) or `xdg-open sketch.html` (Linux)
3. **Local assets** (fonts, images) require a server: `python3 -m http.server 8080` in the project directory, then open `http://localhost:8080/sketch.html`
4. **Export PNG/GIF** — add `keyPressed()` shortcuts, tell the user which key to press
5. **Headless export** — `node scripts/export-frames.js sketch.html --frames 300` for automated frame capture (sketch must use `noLoop()` + `_p5Ready`)
6. **MP4 rendering** — `bash scripts/render.sh sketch.html output.mp4 --duration 30`
7. **Iterative refinement** — edit the HTML file, user refreshes browser to see changes
8. **Load references on demand** — use `skill_view(name="p5js", file_path="references/...")` to load specific reference files as needed during implementation
