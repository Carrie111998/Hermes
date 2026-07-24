---
name: concept-diagrams
description: Educational SVG concept diagrams as standalone light/dark HTML — physics, chemistry, math curves, physical objects, anatomy, floor plans, journeys, hub-spoke systems, exploded layers. 9 semantic color ramps, auto dark mode. For non-software visuals; prefer a specialized skill for software/cloud architecture or hand-drawn sketches.
version: 0.1.0
author: v1k22 (original PR), ported into hermes-agent
license: MIT
dependencies: []
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [diagrams, svg, visualization, education, physics, chemistry, engineering]
    related_skills: [architecture-diagram, excalidraw, generative-widgets]
---

# Concept Diagrams

Generate production-quality SVG diagrams with a unified flat, minimal design system. Output is a single self-contained HTML file that renders identically in any modern browser, with automatic light/dark mode.

## Scope

**Best suited for:**
- Physics setups, chemistry mechanisms, math curves, biology
- Physical objects (aircraft, turbines, smartphones, mechanical watches, cells)
- Anatomy, cross-sections, exploded layer views
- Floor plans, architectural conversions
- Narrative journeys (lifecycle of X, process of Y)
- Hub-spoke system integrations (smart city, IoT networks, electricity grids)
- Educational / textbook-style visuals in any domain
- Quantitative charts (grouped bars, energy profiles)

**Look elsewhere first for:**
- Dedicated software / cloud infrastructure architecture with a dark tech aesthetic (consider `architecture-diagram` if available)
- Hand-drawn whiteboard sketches (consider `excalidraw` if available)
- Animated explainers or video output (consider an animation skill)

If a more specialized skill is available for the subject, prefer that. If none fits, this skill can serve as a general-purpose SVG diagram fallback — the output will carry the clean educational aesthetic described below, which is a reasonable default for almost any subject.

## Workflow

1. Decide on the diagram type (see Diagram Types below).
2. Lay out components using the Design System rules.
3. Write the full HTML page using `templates/template.html` as the wrapper — paste your SVG where the template says `<!-- PASTE SVG HERE -->`.
4. Save as a standalone `.html` file (for example `~/my-diagram.html` or `./my-diagram.html`).
5. User opens it directly in a browser — no server, no dependencies.

Optional: if the user wants a browsable gallery of multiple diagrams, see "Local preview server" in `references/output-and-preview.md`.

Load the HTML template:
```
skill_view(name="concept-diagrams", file_path="templates/template.html")
```

The template embeds the full CSS design system (`c-*` color classes, text classes, light/dark variables, arrow marker styles). The SVG you generate relies on these classes being present on the hosting page.

---

## Routing table — read the reference you need

| Intent | Do this |
|---|---|
| Any diagram at all — colors, the 9 `c-*` ramps and their 7 stops, color assignment rules, typography (`t`/`ts`/`th`), spacing/layout numbers, stroke & rect rounding, the arrow `<defs>` marker, template CSS classes, and reusable node/connector/container patterns | read `references/design-system.md` |
| Physical objects — vehicles, buildings, hardware, anatomy, cross-sections, exploded views; shape selection, layering, semantic CSS classes | read `references/physical-shape-cookbook.md` |
| Infrastructure / systems integration — smart cities, IoT, grids; hub-spoke layout, semantic line styles, power/water/transport shape primitives | read `references/infrastructure-patterns.md` |
| UI / dashboard mockups — screen frames, mini charts, gauges, status indicators | read `references/dashboard-patterns.md` |
| Writing the file out, telling the user how to open it, or an opt-in multi-diagram local preview server | read `references/output-and-preview.md` |
| The HTML wrapper that carries the full CSS design system | load `templates/template.html` |
| A worked, tested diagram of a similar type | load one of `examples/*.md` — see the Examples Reference table below |

---

## Skeleton — the one SVG structure to start from

Every SVG inside the template page starts with this exact structure:

```xml
<svg width="100%" viewBox="0 0 680 {HEIGHT}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
            markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke"
            stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
  </defs>

  <!-- Diagram content here -->

</svg>
```

Replace `{HEIGHT}` with the actual computed height (last element bottom + 40px).

Wrap it in `templates/template.html` (paste at `<!-- PASTE SVG HERE -->`), then read `references/design-system.md` for the colors, typography, spacing, and node patterns that fill the diagram content in.

---

## Diagram Types

Choose the layout that fits the subject:

1. **Flowchart** — CI/CD pipelines, request lifecycles, approval workflows, data processing. Single-direction flow (top-down or left-right). Max 4-5 nodes per row.
2. **Structural / Containment** — Cloud infrastructure nesting, system architecture with layers. Large outer containers with inner regions. Dashed rects for logical groupings.
3. **API / Endpoint Map** — REST routes, GraphQL schemas. Tree from root, branching to resource groups, each containing endpoint nodes.
4. **Microservice Topology** — Service mesh, event-driven systems. Services as nodes, arrows for communication patterns, message queues between.
5. **Data Flow** — ETL pipelines, streaming architectures. Left-to-right flow from sources through processing to sinks.
6. **Physical / Structural** — Vehicles, buildings, hardware, anatomy. Use shapes that match the physical form — `<path>` for curved bodies, `<polygon>` for tapered shapes, `<ellipse>`/`<circle>` for cylindrical parts, nested `<rect>` for compartments. See `references/physical-shape-cookbook.md`.
7. **Infrastructure / Systems Integration** — Smart cities, IoT networks, multi-domain systems. Hub-spoke layout with central platform connecting subsystems. Semantic line styles (`.data-line`, `.power-line`, `.water-pipe`, `.road`). See `references/infrastructure-patterns.md`.
8. **UI / Dashboard Mockups** — Admin panels, monitoring dashboards. Screen frame with nested chart/gauge/indicator elements. See `references/dashboard-patterns.md`.

For physical, infrastructure, and dashboard diagrams, load the matching reference file before generating — each one provides ready-made CSS classes and shape primitives.

---

## Validation Checklist

Before finalizing any SVG, verify ALL of the following:

1. Every `<text>` has class `t`, `ts`, or `th`.
2. Every `<text>` inside a box has `dominant-baseline="central"`.
3. Every connector `<path>` or `<line>` used as arrow has `fill="none"`.
4. No arrow line crosses through an unrelated box.
5. `box_width >= (longest_label_chars × 8) + 48` for 14px text.
6. `box_width >= (longest_label_chars × 6.5) + 48` for 12px text.
7. ViewBox height = bottom-most element + 40px.
8. All content stays within x=40 to x=640.
9. Color classes (`c-*`) are on `<g>` or shape elements, never on `<path>` connectors.
10. Arrow `<defs>` block is present.
11. No gradients, shadows, blur, or glow effects.
12. Stroke width is 0.5px on all node borders.

---

## Examples Reference

The `examples/` directory ships 15 complete, tested diagrams. Browse them for working patterns before writing a new diagram of a similar type:

| File | Type | Demonstrates |
|------|------|--------------|
| `hospital-emergency-department-flow.md` | Flowchart | Priority routing with semantic colors |
| `feature-film-production-pipeline.md` | Flowchart | Phased workflow, horizontal sub-flows |
| `automated-password-reset-flow.md` | Flowchart | Auth flow with error branches |
| `autonomous-llm-research-agent-flow.md` | Flowchart | Loop-back arrows, decision branches |
| `place-order-uml-sequence.md` | Sequence | UML sequence diagram style |
| `commercial-aircraft-structure.md` | Physical | Paths, polygons, ellipses for realistic shapes |
| `wind-turbine-structure.md` | Physical cross-section | Underground/above-ground separation, color coding |
| `smartphone-layer-anatomy.md` | Exploded view | Alternating left/right labels, layered components |
| `apartment-floor-plan-conversion.md` | Floor plan | Walls, doors, proposed changes in dotted red |
| `banana-journey-tree-to-smoothie.md` | Narrative journey | Winding path, progressive state changes |
| `cpu-ooo-microarchitecture.md` | Hardware pipeline | Fan-out, memory hierarchy sidebar |
| `sn2-reaction-mechanism.md` | Chemistry | Molecules, curved arrows, energy profile |
| `smart-city-infrastructure.md` | Hub-spoke | Semantic line styles per system |
| `electricity-grid-flow.md` | Multi-stage flow | Voltage hierarchy, flow markers |
| `ml-benchmark-grouped-bar-chart.md` | Chart | Grouped bars, dual axis |

Load any example with:
```
skill_view(name="concept-diagrams", file_path="examples/<filename>")
```

---

## Quick Reference: What to Use When

| User says | Diagram type | Suggested colors |
|-----------|--------------|------------------|
| "show the pipeline" | Flowchart | gray start/end, purple steps, red errors, teal deploy |
| "draw the data flow" | Data pipeline (left-right) | gray sources, purple processing, teal sinks |
| "visualize the system" | Structural (containment) | purple container, teal services, coral data |
| "map the endpoints" | API tree | purple root, one ramp per resource group |
| "show the services" | Microservice topology | gray ingress, teal services, purple bus, coral workers |
| "draw the aircraft/vehicle" | Physical | paths, polygons, ellipses for realistic shapes |
| "smart city / IoT" | Hub-spoke integration | semantic line styles per subsystem |
| "show the dashboard" | UI mockup | dark screen, chart colors: teal, purple, coral for alerts |
| "power grid / electricity" | Multi-stage flow | voltage hierarchy (HV/MV/LV line weights) |
| "wind turbine / turbine" | Physical cross-section | foundation + tower cutaway + nacelle color-coded |
| "journey of X / lifecycle" | Narrative journey | winding path, progressive state changes |
| "layers of X / exploded" | Exploded layer view | vertical stack, alternating labels |
| "CPU / pipeline" | Hardware pipeline | vertical stages, fan-out to execution ports |
| "floor plan / apartment" | Floor plan | walls, doors, proposed changes in dotted red |
| "reaction mechanism" | Chemistry | atoms, bonds, curved arrows, transition state, energy profile |
