---
name: p5js
description: "p5.js sketches: gen art, shaders, interactive, 3D."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative-coding, generative-art, p5js, canvas, interactive, visualization, webgl, shaders, animation]
    related_skills: [ascii-video, manim-video, excalidraw]
---

# p5.js Production Pipeline

## When to use

Use when users request: p5.js sketches, creative coding, generative art, interactive visualizations, canvas animations, browser-based visual art, data viz, shader effects, or any p5.js project.

## What's inside

Production pipeline for interactive and generative visual art using p5.js. Creates browser-based sketches, generative art, data visualizations, interactive experiences, 3D scenes, audio-reactive visuals, and motion graphics — exported as HTML, PNG, GIF, MP4, or SVG. Covers: 2D/3D rendering, noise and particle systems, flow fields, shaders (GLSL), pixel manipulation, kinetic typography, WebGL scenes, audio analysis, mouse/keyboard interaction, and headless high-res export.

## Red Lines

Non-negotiable. Violating any of these means the sketch is not shippable.

1. **First-render excellence.** If the output looks like a tutorial exercise, a default configuration, or "AI-generated creative coding," it is wrong — rethink before shipping. Articulate the creative concept before writing code.
2. **Never default configurations.** No raw `fill(255, 0, 0)`, no plain `background(0)`/`background(255)`. Always a designed 3-7 color palette and a treated background.
3. **Seeded randomness, always.** `randomSeed(CONFIG.seed)` + `noiseSeed(CONFIG.seed)` in `setup()`. Same seed must give same output. Never `Math.random()` for visual content.
4. **Disable the Friendly Error System.** `p5.disableFriendlyErrors = true;` before `setup()`, plus `pixelDensity(1)`. FES costs up to 10x. Never `console.log()` or DOM manipulation inside `draw()`.
5. **HSB color mode.** `colorMode(HSB, 360, 100, 100, 100)` — never hardcode raw RGB; derive variations procedurally.
6. **Layer, do not flatten.** Use `createGraphics()` offscreen buffers for background / trails / foreground. Single-pass flat rendering looks flat.
7. **Multi-octave noise.** Raw `noise(x, y)` is smooth blobs. Layer octaves (fBM) and consider domain warping.
8. **Headless capture requires `noLoop()`** in `setup()` plus `window._p5Ready = true`. Without it the draw loop races ahead of the screenshotter and frames are skipped or duplicated.
9. **Never claim a visual result you have not rendered.** Preview before reporting done.

## Modes

| Mode | Input | Output | Reference |
|------|-------|--------|-----------|
| **Generative art** | Seed / parameters | Procedural visual composition (still or animated) | `references/visual-effects.md` |
| **Data visualization** | Dataset / API | Interactive charts, graphs, custom data displays | `references/interaction.md` |
| **Interactive experience** | None (user drives) | Mouse/keyboard/touch-driven sketch | `references/interaction.md` |
| **Animation / motion graphics** | Timeline / storyboard | Timed sequences, kinetic typography, transitions | `references/animation.md` |
| **3D scene** | Concept description | WebGL geometry, lighting, camera, materials | `references/webgl-and-3d.md` |
| **Image processing** | Image file(s) | Pixel manipulation, filters, mosaic, pointillism | `references/visual-effects.md` § Pixel Manipulation |
| **Audio-reactive** | Audio file / mic | Sound-driven generative visuals | `references/interaction.md` § Audio Input |

## Stack

Single self-contained HTML file per project, p5.js 1.11.3 from CDN, no build step. Optional add-ons: p5.sound, p5.js-svg, CCapture.js, p5.brush (needs p5 2.x), Puppeteer + ffmpeg for headless video. Full stack table and the 1.x vs 2.x decision: `references/project-workflow.md`.

## Pipeline

```
CONCEPT → DESIGN → CODE → PREVIEW → EXPORT → VERIFY
```

1. **CONCEPT** — mood, color world, motion vocabulary, what makes this unique
2. **DESIGN** — mode, canvas size, renderer, frame rate, interaction, export format
3. **CODE** — one HTML file (skeleton below), or start from `templates/viewer.html` for explorable seed/parameter art
4. **PREVIEW** — open in browser, check visual quality and 60fps at target resolution
5. **EXPORT** — `saveCanvas()` / `saveGif()` / `saveFrames()` + ffmpeg / Puppeteer
6. **VERIFY** — does it match the concept and hold up at display size?

Stage detail, technical-design checklist, export matrix, and agent execution steps: `references/project-workflow.md`. Creative vision and verification questions: `references/creative-direction.md`.

## Minimal Skeleton

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Project Name</title>
  <script>p5.disableFriendlyErrors = true;</script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/p5.min.js"></script>
  <style>
    html, body { margin: 0; padding: 0; overflow: hidden; }
    canvas { display: block; }
  </style>
</head>
<body>
<script>
const CONFIG = { seed: 42 };                      // project parameters
const PALETTE = { bg: '#0a0a0f', primary: '#e8d5b7' };  // designed palette
let particles = [];                               // mutable state

function preload() { /* loadFont / loadImage */ }

function setup() {
  pixelDensity(1);
  createCanvas(1920, 1080);
  randomSeed(CONFIG.seed);
  noiseSeed(CONFIG.seed);
  colorMode(HSB, 360, 100, 100, 100);
}

function draw() { /* render one frame */ }

// helpers → classes → event handlers
class Particle { /* update() + display() */ }

function keyPressed() {
  if (key === 's') saveCanvas('output', 'png');
  if (key === 'g') saveGif('output', 5);
}
function windowResized() { resizeCanvas(windowWidth, windowHeight); }
</script>
</body>
</html>
```

## References

Load on demand with `skill_view(name="p5js", file_path="references/...")`.

| To do this | Read |
|------------|------|
| Choose an aesthetic direction, design parameters, or handle an "experimental / surprise me" brief | `references/creative-direction.md` |
| Run the full 6-stage production path, pick the stack/version, or drive the export step | `references/project-workflow.md` |
| Set up the canvas, coordinate system, draw loop, transforms, offscreen buffers, composition layouts, instance mode, or p5.js 2.0 APIs | `references/core-api.md` |
| Draw primitives, custom vertex shapes, Bezier/Catmull-Rom curves, vectors, SDFs, or SVG paths | `references/shapes-and-geometry.md` |
| Build noise fields, flow fields, particle systems, pixel manipulation, textures, feedback loops, L-systems, or reaction-diffusion | `references/visual-effects.md` |
| Animate: easing, spring physics, state machines, timelines, `millis()` timing, transitions | `references/animation.md` |
| Render text, load fonts, do `textToPoints()` particle text, kinetic typography, or text masks | `references/typography.md` |
| Work with color: HSB/HSL/RGB, `lerpColor()`, procedural palettes, harmony, blend modes, gradients, curated palettes | `references/color-systems.md` |
| Go 3D or write shaders: WEBGL renderer, camera, lighting, materials, custom geometry, GLSL, framebuffers, post-processing | `references/webgl-and-3d.md` |
| Handle input: mouse, keyboard, touch, DOM controls, audio (p5.sound FFT/amplitude), scroll-driven animation | `references/interaction.md` |
| Export anything: PNG/GIF/frames, deterministic headless capture, ffmpeg, CCapture.js, SVG, per-clip video, fxhash/Art Blocks | `references/export-pipeline.md` |
| Fix a problem: performance profiling, per-pixel budgets, performance targets, common mistakes, browser quirks, WebGL debugging, font loading, memory leaks, CORS | `references/troubleshooting.md` |
| Ship explorable generative art with seed navigation and parameter sliders | `templates/viewer.html` |

---

> The canvas is the medium; the algorithm is the brush.
