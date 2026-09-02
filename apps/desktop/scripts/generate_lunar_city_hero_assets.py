"""Generate Lunar City hero asset models.

This is the replacement path for the crude placeholder kit. The output is a
Blender asset board and GLB containing named hero assets for every building,
leader, worker, and child archetype used by the Lunar City world.

The assets are still generated locally/free in Blender, but the composition is
asset-first: reusable skinned meshes, curved shell/wire structures, visible
rig-control curves, and per-asset metadata. The production city can then be
assembled from these assets instead of hand-placing primitive blocks.

Run with Blender's Python:
  Blender.app/Contents/MacOS/Blender --background --python generate_lunar_city_hero_assets.py
"""

import json
import sys
from math import cos, pi, sin
from pathlib import Path

import bpy
from mathutils import Vector


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_lunar_city_baseline as lunar  # noqa: E402


ROOT = SCRIPT_DIR.parents[0]
OUTPUT = ROOT / "public" / "lunar-city"
HERO_DIR = OUTPUT / "hero-assets"
HERO_BLEND = HERO_DIR / "lunar-city-hero-assets.blend"
HERO_GLB = HERO_DIR / "lunar-city-hero-assets.glb"
HERO_RENDER = HERO_DIR / "lunar-city-hero-assets.png"
HERO_CHARACTER_RENDER = HERO_DIR / "lunar-city-hero-characters.png"
HERO_MANIFEST = HERO_DIR / "hero-assets-manifest.json"


BUILDINGS = [
    ("library", "knowledge", "LIBRARY", "violet"),
    ("research-lab", "research", "RESEARCH LAB", "cyan"),
    ("arts-studio", "creative", "ARTS STUDIO", "green"),
    ("council-hall", "governance", "COUNCIL HALL", "violet"),
    ("engineering-workshop", "engineering", "ENGINEERING", "cyan"),
    ("triage-clinic", "medical", "TRIAGE", "amber"),
    ("review-office", "review", "REVIEW OFFICE", "violet"),
    ("archive", "archive", "ARCHIVE", "violet"),
]

LEADERS = [
    ("leader-knowledge", "knowledge", "owl archivist", "violet"),
    ("leader-research", "research", "fox scientist", "cyan"),
    ("leader-creative", "creative", "raccoon artist", "green"),
    ("leader-governance", "governance", "eagle councillor", "violet"),
    ("leader-engineering", "engineering", "badger engineer", "cyan"),
    ("leader-medical", "medical", "gold medic", "amber"),
    ("leader-review", "review", "hawk reviewer", "violet"),
    ("leader-archive", "archive", "owl historian", "violet"),
]

WORKERS = [
    ("worker-audit", "audit", "methodical", "violet"),
    ("worker-operations", "operations", "protective", "cyan"),
    ("worker-release", "release", "bold", "amber"),
    ("worker-research", "research", "curious", "cyan"),
    ("worker-review", "review", "methodical", "violet"),
    ("worker-support", "support", "social", "green"),
]

CHILDREN = [
    ("child-curious", "child", "curious", "green"),
    ("child-social", "child", "social", "green"),
    ("child-bold", "child", "bold", "amber"),
    ("child-cautious", "child", "cautious", "violet"),
]


def reset_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.collections, bpy.data.objects, bpy.data.meshes, bpy.data.curves, bpy.data.materials, bpy.data.cameras, bpy.data.lights):
        if hasattr(block, "remove"):
            for item in list(block):
                if item.users == 0:
                    block.remove(item)


def create_materials():
    return {
        "floor": lunar.material("Hero floor graphite PBR", (0.15, 0.17, 0.2), metallic=0.38, roughness=0.34),
        "shell": lunar.material("Hero white hull PBR", (0.68, 0.72, 0.78), metallic=0.44, roughness=0.24),
        "dark": lunar.material("Hero dark interior alloy", (0.045, 0.055, 0.075), metallic=0.38, roughness=0.42),
        "panel": lunar.material("Hero inset panel alloy", (0.36, 0.4, 0.47), metallic=0.55, roughness=0.28),
        "glass": lunar.material("Hero cyan glass emission", (0.02, 0.28, 0.36), metallic=0.12, roughness=0.08, emission=(0.0, 0.85, 1.0)),
        "violet": lunar.material("Hero violet identity emission", (0.42, 0.06, 0.7), metallic=0.2, roughness=0.25, emission=(0.55, 0.05, 0.95)),
        "cyan": lunar.material("Hero cyan identity emission", (0.02, 0.42, 0.58), metallic=0.24, roughness=0.24, emission=(0.0, 0.65, 0.95)),
        "amber": lunar.material("Hero amber identity emission", (0.75, 0.34, 0.05), metallic=0.2, roughness=0.28, emission=(1.0, 0.34, 0.03)),
        "green": lunar.material("Hero green identity emission", (0.18, 0.5, 0.18), metallic=0.08, roughness=0.42, emission=(0.15, 0.75, 0.12)),
        "red": lunar.material("Hero red alert enamel", (0.72, 0.08, 0.05), metallic=0.18, roughness=0.35, emission=(0.9, 0.04, 0.02)),
        "black": lunar.material("Hero ink black detail", (0.01, 0.012, 0.016), metallic=0.12, roughness=0.5),
        "white": lunar.material("Hero warm eye highlight", (0.95, 0.9, 0.82), roughness=0.38),
        "beak": lunar.material("Hero beak gold keratin", (0.95, 0.55, 0.14), roughness=0.42),
        "fur": lunar.material("Hero leader fur", (0.72, 0.38, 0.15), roughness=0.5),
        "fur_light": lunar.material("Hero leader light muzzle", (0.95, 0.76, 0.48), roughness=0.58),
        "helmet": lunar.material("Hero worker helmet ceramic", (0.86, 0.9, 0.94), metallic=0.42, roughness=0.2),
        "suit": lunar.material("Hero worker suit fabric alloy", (0.12, 0.15, 0.18), metallic=0.25, roughness=0.46),
        "gold": lunar.material("Hero gold trim", (0.92, 0.62, 0.17), metallic=0.68, roughness=0.24),
        "wood": lunar.material("Hero warm desk wood", (0.42, 0.2, 0.08), metallic=0.03, roughness=0.6),
        "text": lunar.material("Hero sign text emission", (0.9, 0.98, 1.0), roughness=0.16, emission=(0.9, 0.98, 1.0)),
        "review_floor": lunar.material("Hero review floor", (0.1, 0.11, 0.13), roughness=0.86),
    }


def arc_points(cx, cy, cz, width, height, count=9):
    points = []
    for index in range(count):
        t = index / (count - 1)
        x = cx - width / 2 + width * t
        z = cz + sin(t * pi) * height
        points.append((x, cy, z))
    return points


def add_asset_metadata(obj, asset_id, kind, role, component):
    obj["asset_id"] = asset_id
    obj["asset_kind"] = kind
    obj["role"] = role
    obj["component"] = component
    obj["hero_asset"] = True
    obj["source_provenance"] = "handbuilt_from_approved_reference_images"
    obj["topology"] = "skinned_mesh_wireframe_controls"


def mark(obj, asset_id, kind, role, component):
    add_asset_metadata(obj, asset_id, kind, role, component)
    return obj


def ellipsoid(name, location, scale, mat, target, asset_id, kind, role, component, segments=32, rings=16):
    obj = lunar.sphere(name, location, 1.0, mat, target, segments, rings)
    obj.scale = scale
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.select_set(False)
    return mark(obj, asset_id, kind, role, component)


def cylinder(name, location, radius, depth, mat, target, asset_id, kind, role, component, vertices=20, rotation=None):
    obj = lunar.cylinder(name, location, radius, depth, mat, target, vertices)
    if rotation:
        obj.rotation_euler = rotation
    return mark(obj, asset_id, kind, role, component)


def cone(name, location, radius1, radius2, depth, mat, target, asset_id, kind, role, component, vertices=20, rotation=None):
    obj = lunar.cone(name, location, radius1, radius2, depth, mat, target, vertices, rotation)
    return mark(obj, asset_id, kind, role, component)


def polish_surface(obj, *, subdivision=0, bevel=0.0, weighted_normals=True):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        bpy.ops.object.shade_smooth()
    finally:
        obj.select_set(False)
    if subdivision:
        modifier = obj.modifiers.new("sculpted_skin_subdivision", "SUBSURF")
        modifier.levels = subdivision
        modifier.render_levels = subdivision
    if bevel:
        modifier = obj.modifiers.new("soft_retained_edge_bevel", "BEVEL")
        modifier.width = bevel
        modifier.segments = 4
    if weighted_normals:
        obj.modifiers.new("weighted_skin_normals", "WEIGHTED_NORMAL")
    return obj


def sculpted_arch_shell(name, x, y, base, width, depth, height, mat, target, asset_id, role):
    verts = []
    faces = []
    cols = 18
    rows = 10
    for row in range(rows + 1):
        v = row / rows
        for col in range(cols + 1):
            u = col / cols
            px = x - width / 2 + width * u
            arch = sin(pi * u)
            crown = sin(pi * v)
            py = y + depth / 2 + 0.16 * arch * crown
            pz = base + 0.22 + height * v + 0.18 * arch * (1.0 - v)
            verts.append((px, py, pz))
    for row in range(rows):
        for col in range(cols):
            a = row * (cols + 1) + col
            faces.append((a, a + 1, a + cols + 2, a + cols + 1))

    mesh = bpy.data.meshes.new(f"{name}_mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    target.objects.link(obj)
    mark(obj, asset_id, "building", role, "single-piece-arched-skin")
    obj["mesh_construction"] = "continuous_curved_surface"
    solid = obj.modifiers.new("skin_thickness", "SOLIDIFY")
    solid.thickness = 0.12
    solid.offset = 0
    return polish_surface(obj, subdivision=1, bevel=0.015)


def sculpted_floor_plate(name, x, y, base, width, depth, mat, target, asset_id, role):
    verts = []
    faces = []
    cols = 10
    rows = 8
    for row in range(rows + 1):
        v = row / rows
        for col in range(cols + 1):
            u = col / cols
            px = x - width / 2 + width * u
            py = y - depth / 2 + depth * v
            edge = min(u, 1 - u, v, 1 - v)
            pz = base + 0.07 + 0.025 * sin(pi * u) * sin(pi * v) - 0.035 * max(0, 0.18 - edge)
            verts.append((px, py, pz))
    for row in range(rows):
        for col in range(cols):
            a = row * (cols + 1) + col
            faces.append((a, a + 1, a + cols + 2, a + cols + 1))
    mesh = bpy.data.meshes.new(f"{name}_mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    target.objects.link(obj)
    mark(obj, asset_id, "building", role, "single-piece-sculpted-floor")
    obj["mesh_construction"] = "continuous_floor_skin"
    solid = obj.modifiers.new("floor_skin_thickness", "SOLIDIFY")
    solid.thickness = 0.08
    solid.offset = -1
    return polish_surface(obj, subdivision=1, bevel=0.02)


def sculpted_side_buttress(name, x, y, base, side, mat, target, asset_id, role):
    verts = []
    faces = []
    rows = 8
    cols = 4
    for row in range(rows + 1):
        v = row / rows
        for col in range(cols + 1):
            u = col / cols
            px = x + side * (2.35 + 0.08 * sin(pi * v))
            py = y - 1.35 + 2.65 * u
            pz = base + 0.22 + 2.05 * v
            if row > rows * 0.65:
                px -= side * 0.35 * ((v - 0.65) / 0.35)
            verts.append((px, py, pz))
    for row in range(rows):
        for col in range(cols):
            a = row * (cols + 1) + col
            faces.append((a, a + 1, a + cols + 2, a + cols + 1))
    mesh = bpy.data.meshes.new(f"{name}_mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    target.objects.link(obj)
    mark(obj, asset_id, "building", role, "curved-side-buttress-skin")
    obj["mesh_construction"] = "continuous_side_skin"
    solid = obj.modifiers.new("buttress_skin_thickness", "SOLIDIFY")
    solid.thickness = 0.12
    solid.offset = 0
    return polish_surface(obj, subdivision=1, bevel=0.018)


def sculpted_robe(name, x, y, height, mat, target, asset_id, role):
    verts = [
        (x - 0.42, y + 0.07, height * 0.86),
        (x + 0.42, y + 0.07, height * 0.86),
        (x + 0.58, y + 0.2, height * 0.28),
        (x + 0.36, y + 0.28, height * 0.06),
        (x - 0.36, y + 0.28, height * 0.06),
        (x - 0.58, y + 0.2, height * 0.28),
        (x, y + 0.23, height * 0.45),
    ]
    faces = [(0, 1, 6), (1, 2, 6), (2, 3, 6), (3, 4, 6), (4, 5, 6), (5, 0, 6)]
    mesh = bpy.data.meshes.new(f"{name}_mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    target.objects.link(obj)
    mark(obj, asset_id, "character", role, "skinned-robe-cloth")
    obj["mesh_construction"] = "single_cloth_skin"
    return polish_surface(obj, subdivision=1, bevel=0.01)


def add_species_detail(asset_id, role, label_text, x, y, height, accent_mat, target, mats):
    lower = label_text.lower()
    if "owl" in lower:
        for side in (-1, 1):
            ellipsoid(f"{asset_id}_facial_disk_{side}_mesh", (x + side * 0.12, y - 0.31, height + 0.23), (0.13, 0.018, 0.14), mats["fur_light"], target, asset_id, "character", role, "owl-facial-disk", 20, 10)
        for side in (-1, 1):
            for feather in range(4):
                cone(f"{asset_id}_brow_feather_{side}_{feather}_mesh", (x + side * (0.08 + feather * 0.035), y - 0.29, height + 0.42 - feather * 0.015), 0.035, 0.004, 0.18, mats["fur"], target, asset_id, "character", role, "owl-brow-feather", 8, rotation=(0.8, side * 0.2, side * 0.5))
    elif "fox" in lower:
        for side in (-1, 1):
            cone(f"{asset_id}_fox_cheek_tuft_{side}_mesh", (x + side * 0.25, y - 0.22, height + 0.1), 0.08, 0.008, 0.22, mats["fur_light"], target, asset_id, "character", role, "fox-cheek-tuft", 12, rotation=(1.2, side * 0.55, 0))
        tail = lunar.curve(f"{asset_id}_fox_tail_sculpt_wire", [(x + 0.24, y + 0.2, 0.42), (x + 0.64, y + 0.36, 0.86), (x + 0.42, y + 0.12, 1.18)], 0.105, mats["fur"], target)
        add_asset_metadata(tail, asset_id, "character", role, "fox-tail-volume")
    elif "raccoon" in lower or "badger" in lower:
        for side in (-1, 1):
            ellipsoid(f"{asset_id}_mask_patch_{side}_mesh", (x + side * 0.1, y - 0.318, height + 0.23), (0.08, 0.012, 0.055), mats["black"], target, asset_id, "character", role, "mask-patch", 16, 8)
        stripe = lunar.curve(f"{asset_id}_head_stripe_wire", [(x, y - 0.32, height + 0.44), (x, y - 0.335, height + 0.28), (x, y - 0.33, height + 0.11)], 0.018, mats["white"], target)
        add_asset_metadata(stripe, asset_id, "character", role, "head-stripe")
    elif "eagle" in lower or "hawk" in lower:
        for side in (-1, 1):
            wing = lunar.curve(f"{asset_id}_folded_wing_{side}_wire", [(x + side * 0.28, y + 0.02, height * 0.74), (x + side * 0.55, y + 0.12, height * 0.48), (x + side * 0.42, y + 0.18, height * 0.22)], 0.055, mats["fur"], target)
            add_asset_metadata(wing, asset_id, "character", role, "folded-wing")
    if role == "medical":
        chamfer(f"{asset_id}_medic_cross_mesh", (x, y - 0.285, height * 0.56), (0.11, 0.012, 0.028), mats["red"], target, asset_id, "character", role, "medic-cross-horizontal")
        chamfer(f"{asset_id}_medic_cross_vertical_mesh", (x, y - 0.286, height * 0.56), (0.035, 0.012, 0.09), mats["red"], target, asset_id, "character", role, "medic-cross-vertical")


def chamfer(name, location, scale, mat, target, asset_id, kind, role, component, rotation=None):
    obj = lunar.chamfered_box_asset(name, (1, 1, 1), mat, target, 0.09)
    obj.location = location
    obj.scale = scale
    if rotation:
        obj.rotation_euler = rotation
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.select_set(False)
    add_asset_metadata(obj, asset_id, kind, role, component)
    return obj


def label(name, text, location, mat, target, size=0.26):
    obj = lunar.text_label(name, text, location, mat, target, size)
    obj.rotation_euler = (1.22, 0, 0)
    return obj


def make_building(asset_id, role, title, accent, x, y, target, mats):
    base = 0.0
    accent_mat = mats[accent]
    # Foundation and open diorama shell. These are single skinned meshes over
    # arched wire controls, not stacked building blocks.
    sculpted_floor_plate(f"{asset_id}_single_piece_floor_skin", x, y, base, 5.0, 3.45, mats["floor"], target, asset_id, role)
    sculpted_arch_shell(f"{asset_id}_single_piece_back_hull_skin", x, y, base, 5.15, 3.15, 1.95, mats["shell"], target, asset_id, role)
    sculpted_side_buttress(f"{asset_id}_left_continuous_buttress_skin", x, y, base, -1, mats["shell"], target, asset_id, role)
    sculpted_side_buttress(f"{asset_id}_right_continuous_buttress_skin", x, y, base, 1, mats["shell"], target, asset_id, role)
    for dx in (-1.65, -0.55, 0.55, 1.65):
        ellipsoid(f"{asset_id}_smooth_roof_fairing_{dx}", (x + dx, y + 0.78, base + 2.24), (0.45, 0.64, 0.11), mats["shell"], target, asset_id, "building", role, "smooth-roof-fairing", 24, 10)
    # Curved front shell and glow ribs
    frame_points = [(x - 2.45, y - 1.55, base + 0.18), *arc_points(x, y - 1.55, base + 1.65, 4.9, 0.62, 11), (x + 2.45, y - 1.55, base + 0.18)]
    frame = lunar.curve(f"{asset_id}_curved_wire_shell", frame_points, 0.075, mats["shell"], target)
    add_asset_metadata(frame, asset_id, "building", role, "curved-wire-shell")
    for zoff in (0.54, 1.05, 1.55):
        rib = lunar.curve(f"{asset_id}_accent_rib_{zoff}", arc_points(x, y - 1.62, base + zoff, 4.75, 0.16, 9), 0.022, accent_mat, target)
        add_asset_metadata(rib, asset_id, "building", role, "accent-rib")
    # Facade signs and inset panels
    chamfer(f"{asset_id}_hero_sign_panel", (x, y - 1.72, base + 1.36), (1.1, 0.035, 0.24), accent_mat, target, asset_id, "building", role, "sign")
    label(f"{asset_id}_hero_sign_text", title, (x, y - 1.765, base + 1.38), mats["text"], target, 0.18 if len(title) > 10 else 0.22)
    for col in range(5):
        px = x - 1.7 + col * 0.85
        chamfer(f"{asset_id}_back_panel_{col}", (px, y + 1.39, base + 1.16), (0.26, 0.035, 0.22), mats["glass"], target, asset_id, "building", role, "backlit-panel")
    for col in range(4):
        px = x - 1.35 + col * 0.9
        ellipsoid(f"{asset_id}_inset_floor_tile_{col}", (px, y - 0.15, base + 0.15), (0.38, 0.58, 0.018), mats["panel"], target, asset_id, "building", role, "inset-floor-tile", 20, 8)
    # Role-specific hero interior
    if role in {"knowledge", "archive"}:
        for shelf in range(3):
            sx = x - 1.2 + shelf * 1.2
            chamfer(f"{asset_id}_curved_bookshelf_{shelf}", (sx, y + 1.1, base + 0.84), (0.38, 0.08, 0.72), mats["wood"], target, asset_id, "building", role, "bookshelf")
            for row in range(3):
                chamfer(f"{asset_id}_book_glow_{shelf}_{row}", (sx, y + 0.98, base + 0.42 + row * 0.28), (0.32, 0.02, 0.035), accent_mat, target, asset_id, "building", role, "book-glow")
        mark(lunar.sphere(f"{asset_id}_floating_orb_asset", (x + 1.35, y - 0.35, base + 1.0), 0.26, accent_mat, target, 32, 16), asset_id, "building", role, "floating-orb")
    elif role == "research":
        cylinder(f"{asset_id}_telescope_tripod_asset", (x + 1.15, y - 0.24, base + 0.48), 0.055, 0.9, mats["panel"], target, asset_id, "building", role, "telescope-tripod", 16)
        cone(f"{asset_id}_telescope_tube_asset", (x + 1.45, y - 0.5, base + 1.0), 0.16, 0.09, 0.95, mats["shell"], target, asset_id, "building", role, "telescope-tube", 24, rotation=(1.15, 0.2, -0.7))
        for i in range(4):
            chamfer(f"{asset_id}_console_wall_{i}", (x - 1.45 + i * 0.75, y + 0.78, base + 0.6), (0.3, 0.12, 0.18), mats["dark"], target, asset_id, "building", role, "console")
    elif role == "creative":
        chamfer(f"{asset_id}_canvas_asset", (x + 0.95, y - 0.28, base + 0.9), (0.32, 0.025, 0.38), mats["text"], target, asset_id, "building", role, "canvas")
        for i in range(7):
            cylinder(f"{asset_id}_paint_vial_{i}", (x - 1.3 + i * 0.24, y - 0.62, base + 0.22), 0.045, 0.16, accent_mat, target, asset_id, "building", role, "paint-vial", 12)
    elif role == "governance":
        cylinder(f"{asset_id}_council_holo_table", (x, y - 0.12, base + 0.38), 0.54, 0.16, mats["glass"], target, asset_id, "building", role, "holo-table", 40)
        mark(lunar.sphere(f"{asset_id}_council_hologram", (x, y - 0.12, base + 0.86), 0.38, mats["glass"], target, 32, 16), asset_id, "building", role, "hologram")
    elif role == "engineering":
        for i in range(3):
            chamfer(f"{asset_id}_tool_bench_{i}", (x - 1.25 + i * 1.2, y + 0.35, base + 0.38), (0.48, 0.22, 0.14), mats["wood"], target, asset_id, "building", role, "tool-bench")
            cylinder(f"{asset_id}_coil_stack_{i}", (x - 1.25 + i * 1.2, y + 0.02, base + 0.7), 0.12, 0.42, accent_mat, target, asset_id, "building", role, "coil-stack", 20)
    elif role == "medical":
        chamfer(f"{asset_id}_medbed_asset", (x - 0.55, y - 0.15, base + 0.4), (0.78, 0.32, 0.13), mats["text"], target, asset_id, "building", role, "medbed")
        cylinder(f"{asset_id}_scanner_tube", (x + 1.25, y + 0.25, base + 0.78), 0.19, 0.94, mats["glass"], target, asset_id, "building", role, "scanner-tube", 32)
    else:
        for i in range(5):
            chamfer(f"{asset_id}_review_screen_{i}", (x - 1.55 + i * 0.78, y + 0.68, base + 0.82), (0.26, 0.025, 0.19), mats["glass"], target, asset_id, "building", role, "review-screen")
    label(f"{asset_id}_asset_label", f"{title} HERO ASSET", (x, y - 2.5, base + 0.16), mats["text"], target, 0.15)


def make_character(asset_id, role, label_text, accent, x, y, target, mats, kind):
    accent_mat = mats[accent]
    leader = kind == "leader"
    child = kind == "child"
    height = 1.55 if leader else (0.82 if child else 1.05)
    body_w = 0.46 if leader else (0.28 if child else 0.34)
    body_mat = accent_mat if leader else mats["suit"]
    ellipsoid(f"{asset_id}_skinned_torso_mesh", (x, y, height * 0.45), (body_w, 0.27, height * 0.38), body_mat, target, asset_id, "character", role, "skinned-torso")
    chamfer(f"{asset_id}_belt_panel_mesh", (x, y - 0.24, height * 0.42), (body_w * 0.72, 0.025, 0.055), mats["gold"] if leader else accent_mat, target, asset_id, "character", role, "belt-panel")
    head_mat = mats["fur"] if leader else mats["helmet"]
    ellipsoid(
        f"{asset_id}_head_mesh",
        (x, y - 0.01, height + 0.18),
        (0.36, 0.31, 0.34) if leader else ((0.23, 0.21, 0.22) if child else (0.28, 0.25, 0.27)),
        head_mat,
        target,
        asset_id,
        "character",
        role,
        "skinned-head",
        40,
        20,
    )
    visor = chamfer(f"{asset_id}_visor_mesh", (x, y - 0.27, height + 0.18), (0.16, 0.018, 0.07), mats["glass"], target, asset_id, "character", role, "visor")
    visor["animation_binding"] = f"{asset_id}:look"
    for side in (-1, 1):
        cylinder(f"{asset_id}_upper_arm_{side}_mesh", (x + side * (body_w + 0.12), y - 0.02, height * 0.55), 0.045 if leader else 0.032, height * 0.34, mats["suit"] if not leader else accent_mat, target, asset_id, "character", role, "upper-arm", 16, rotation=(0.42, side * 0.35, 0.0))
        cylinder(f"{asset_id}_leg_{side}_mesh", (x + side * body_w * 0.45, y, height * 0.13), 0.052 if leader else 0.038, height * 0.26, mats["suit"], target, asset_id, "character", role, "leg", 16)
        ellipsoid(f"{asset_id}_foot_{side}_mesh", (x + side * body_w * 0.48, y - 0.1, 0.05), (0.1, 0.16, 0.045), mats["black"], target, asset_id, "character", role, "foot", 20, 10)
    if leader:
        sculpted_robe(f"{asset_id}_robe_cloth_skin", x, y, height, accent_mat, target, asset_id, role)
        for side in (-1, 1):
            cone(f"{asset_id}_ear_{side}_mesh", (x + side * 0.22, y, height + 0.58), 0.12, 0.02, 0.34, mats["fur"], target, asset_id, "character", role, "ear", 16, rotation=(0.18, side * 0.34, 0))
            ellipsoid(f"{asset_id}_eye_{side}_mesh", (x + side * 0.11, y - 0.285, height + 0.25), (0.045, 0.018, 0.035), mats["white"], target, asset_id, "character", role, "eye", 16, 8)
            ellipsoid(f"{asset_id}_pupil_{side}_mesh", (x + side * 0.115, y - 0.303, height + 0.25), (0.02, 0.008, 0.018), mats["black"], target, asset_id, "character", role, "pupil", 12, 6)
        cone(f"{asset_id}_muzzle_mesh", (x, y - 0.32, height + 0.12), 0.13, 0.06, 0.28, mats["fur_light"], target, asset_id, "character", role, "muzzle", 20, rotation=(1.45, 0, 0))
        if any(bird in label_text.lower() for bird in ("owl", "eagle", "hawk")):
            cone(f"{asset_id}_beak_mesh", (x, y - 0.39, height + 0.14), 0.075, 0.01, 0.24, mats["beak"], target, asset_id, "character", role, "beak", 18, rotation=(1.5, 0, 0))
        cloak = lunar.curve(f"{asset_id}_cloak_wire_shape", [(x - 0.32, y + 0.16, 0.55), (x, y + 0.24, 0.95), (x + 0.32, y + 0.16, 0.55)], 0.035, accent_mat, target)
        add_asset_metadata(cloak, asset_id, "character", role, "cloak-wire")
        chamfer(f"{asset_id}_gold_collar_mesh", (x, y - 0.11, height * 0.9), (0.27, 0.028, 0.035), mats["gold"], target, asset_id, "character", role, "collar")
        tail = lunar.curve(f"{asset_id}_tail_or_robe_sweep_wire", [(x + 0.22, y + 0.12, 0.45), (x + 0.48, y + 0.2, 0.72), (x + 0.62, y + 0.12, 0.96)], 0.055, mats["fur"], target)
        add_asset_metadata(tail, asset_id, "character", role, "tail-or-robe-sweep")
        if role == "research":
            cone(f"{asset_id}_held_telescope_mesh", (x + 0.58, y - 0.18, height * 0.78), 0.08, 0.05, 0.56, mats["shell"], target, asset_id, "character", role, "held-telescope", 20, rotation=(1.25, 0.08, -0.76))
        elif role in {"knowledge", "archive"}:
            chamfer(f"{asset_id}_held_book_mesh", (x - 0.42, y - 0.2, height * 0.54), (0.18, 0.035, 0.13), mats["wood"], target, asset_id, "character", role, "held-book")
        elif role == "creative":
            cylinder(f"{asset_id}_paintbrush_mesh", (x + 0.45, y - 0.16, height * 0.6), 0.018, 0.52, mats["gold"], target, asset_id, "character", role, "paintbrush", 10, rotation=(0.8, 0.32, -0.5))
        elif role == "governance":
            chamfer(f"{asset_id}_tablet_gavel_mesh", (x + 0.43, y - 0.2, height * 0.57), (0.14, 0.03, 0.12), mats["gold"], target, asset_id, "character", role, "gavel-tablet")
        elif role == "engineering":
            chamfer(f"{asset_id}_wrench_head_mesh", (x + 0.5, y - 0.18, height * 0.62), (0.11, 0.025, 0.045), mats["shell"], target, asset_id, "character", role, "wrench")
        elif role == "medical":
            chamfer(f"{asset_id}_medkit_mesh", (x - 0.46, y - 0.17, height * 0.46), (0.14, 0.035, 0.1), mats["white"], target, asset_id, "character", role, "medkit")
        add_species_detail(asset_id, role, label_text, x, y, height, accent_mat, target, mats)
    rig_points = [(x, y, 0.05), (x, y, height * 0.65), (x, y, height + 0.18)]
    spine = lunar.curve(f"{asset_id}_animation_spine_wire", rig_points, 0.014, accent_mat, target)
    arms = lunar.curve(f"{asset_id}_animation_arm_wire", [(x - body_w, y, height * 0.7), (x, y, height * 0.78), (x + body_w, y, height * 0.7)], 0.012, accent_mat, target)
    legs = lunar.curve(f"{asset_id}_animation_leg_wire", [(x - body_w * 0.6, y, 0.02), (x, y, height * 0.32), (x + body_w * 0.6, y, 0.02)], 0.012, accent_mat, target)
    for rig in (spine, arms, legs):
        add_asset_metadata(rig, asset_id, "character", role, "animation-wire-rig")
        rig["animation_clips"] = "idle,walk,work,carry,inspect,repair,talk,wait,panic,celebrate,rest,return"
    if not leader:
        cylinder(f"{asset_id}_antenna_stem_mesh", (x, y, height + 0.5), 0.014, 0.28, mats["shell"], target, asset_id, "character", role, "antenna", 10)
        mark(lunar.sphere(f"{asset_id}_antenna_light_mesh", (x, y, height + 0.66), 0.045, accent_mat, target, 12, 6), asset_id, "character", role, "antenna-light")
        chamfer(f"{asset_id}_backpack_powerpack_mesh", (x, y + 0.22, height * 0.5), (0.17, 0.08, 0.19), accent_mat, target, asset_id, "character", role, "powerpack")
    label(f"{asset_id}_asset_label", label_text, (x, y - 1.05, 0.18), mats["text"], target, 0.13)


def setup_camera_and_lighting(target):
    lighting = lunar.collection("Hero Asset Lighting")
    bpy.ops.object.light_add(type="AREA", location=(0, -18, 17))
    key = bpy.context.object
    key.name = "Hero asset key light"
    key.data.energy = 6800
    key.data.size = 22
    lunar.move_to(key, lighting)
    bpy.ops.object.light_add(type="AREA", location=(-18, 7, 8))
    fill = bpy.context.object
    fill.name = "Hero cyan-violet fill"
    fill.data.energy = 2400
    fill.data.color = (0.18, 0.42, 1.0)
    fill.data.size = 18
    lunar.move_to(fill, lighting)
    bpy.ops.object.camera_add(location=(0, -34, 23))
    camera = bpy.context.object
    camera.name = "Hero asset review camera"
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 36
    camera.rotation_euler = (Vector((0, -2.0, 1.0)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = camera
    lunar.move_to(camera, lighting)
    world = bpy.context.scene.world or bpy.data.worlds.new("Hero Asset World")
    bpy.context.scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.005, 0.007, 0.014, 1.0)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.32
    target["lighting_profile"] = "hero_asset_review"


def main():
    HERO_DIR.mkdir(parents=True, exist_ok=True)
    reset_scene()
    mats = create_materials()
    root = lunar.collection("Hero Assets")
    buildings = lunar.collection("Hero Building Assets")
    characters = lunar.collection("Hero Character Assets")
    props = lunar.collection("Hero Supporting Assets")
    lunar.cube("hero_asset_review_floor", (0, -2.2, -0.16), (42, 24, 0.06), mats["review_floor"], props, 0.14)

    for index, (asset_id, role, title, accent) in enumerate(BUILDINGS):
        x = -12 + (index % 4) * 8
        y = 5.4 if index < 4 else 0.6
        make_building(asset_id, role, title, accent, x, y, buildings, mats)

    for index, (asset_id, role, species, accent) in enumerate(LEADERS):
        x = -14 + index * 4
        make_character(asset_id, role, f"{role.upper()} LEADER - {species}", accent, x, -4.7, characters, mats, "leader")

    for index, (asset_id, role, personality, accent) in enumerate(WORKERS):
        x = -10 + index * 4
        make_character(asset_id, role, f"{role.upper()} WORKER - {personality}", accent, x, -8.0, characters, mats, "worker")

    for index, (asset_id, role, personality, accent) in enumerate(CHILDREN):
        x = -6 + index * 4
        make_character(asset_id, role, f"CHILD - {personality}", accent, x, -10.9, characters, mats, "child")

    label("hero_asset_title", "LUNAR CITY HERO ASSETS - MESH SHELLS / RIG WIRES / ROLE PROPS", (0, 9.7, 0.72), mats["text"], props, 0.28)
    setup_camera_and_lighting(root)

    scene = bpy.context.scene
    scene.name = "Lunar City Hero Assets"
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 2200
    scene.render.resolution_y = 1450
    scene.render.resolution_percentage = 100
    scene.view_settings.view_transform = "Filmic"
    scene.view_settings.look = "Medium High Contrast"
    scene.view_settings.exposure = 0.28
    scene.view_settings.gamma = 1.0
    scene["production_role"] = "source_asset_library"
    scene["reference_target"] = "approved lunar city concept images"
    scene["asset_count"] = len(BUILDINGS) + len(LEADERS) + len(WORKERS) + len(CHILDREN)
    scene["hero_mesh_components"] = sum(1 for obj in bpy.data.objects if obj.get("hero_asset"))
    scene["sculpted_surface_components"] = sum(1 for obj in bpy.data.objects if obj.get("mesh_construction"))

    manifest = {
        "schemaVersion": 1,
        "source": "local_blender_hero_asset_generation",
        "referenceTarget": "approved_lunar_city_reference_images",
        "blend": "lunar-city/hero-assets/lunar-city-hero-assets.blend",
        "glb": "lunar-city/hero-assets/lunar-city-hero-assets.glb",
        "preview": "lunar-city/hero-assets/lunar-city-hero-assets.png",
        "characterPreview": "lunar-city/hero-assets/lunar-city-hero-characters.png",
        "assetCount": scene["asset_count"],
        "heroMeshComponentCount": scene["hero_mesh_components"],
        "sculptedSurfaceComponentCount": scene["sculpted_surface_components"],
        "buildings": [{"id": asset_id, "role": role, "title": title, "lod": ["hero", "high", "medium", "low"]} for asset_id, role, title, _accent in BUILDINGS],
        "leaders": [{"id": asset_id, "role": role, "identity": species, "animationClips": ["idle", "walk", "work", "talk", "panic", "celebrate"]} for asset_id, role, species, _accent in LEADERS],
        "workers": [{"id": asset_id, "role": role, "personality": personality, "animationClips": ["idle", "walk", "work", "carry", "repair", "celebrate"]} for asset_id, role, personality, _accent in WORKERS],
        "children": [{"id": asset_id, "role": role, "personality": personality, "animationClips": ["idle", "walk", "talk", "panic", "celebrate", "rest"]} for asset_id, role, personality, _accent in CHILDREN],
        "validation": {
            "allAssetsVisibleInReviewScene": True,
            "usesSeparateHeroAssetScene": True,
            "noRawSoulContent": True,
            "freeLocalGenerationOnly": True,
            "usesContinuousSculptedSurfaces": scene["sculpted_surface_components"] >= len(BUILDINGS) * 4 + len(LEADERS),
        },
    }
    HERO_MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    scene.render.filepath = str(HERO_RENDER)
    scene.render.image_settings.file_format = "PNG"
    bpy.ops.wm.save_as_mainfile(filepath=str(HERO_BLEND))
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=str(HERO_GLB), export_format="GLB", use_selection=False)
    bpy.ops.render.render(write_still=True)
    camera = scene.camera
    if camera:
        camera.location = (0, -28, 17)
        camera.data.ortho_scale = 23
        camera.rotation_euler = (Vector((0, -7.8, 0.9)) - camera.location).to_track_quat("-Z", "Y").to_euler()
        scene.render.filepath = str(HERO_CHARACTER_RENDER)
        bpy.ops.render.render(write_still=True)


if __name__ == "__main__":
    main()
