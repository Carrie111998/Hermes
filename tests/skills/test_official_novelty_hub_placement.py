"""Novelty/leisure official skills belong in optional-skills, not the default seed.

Invariant (not a full-catalog snapshot): the named official novelty cluster
must not ship under skills/, must remain installable from optional-skills/
as official/<category>/<skill>, already-optional R2 names stay non-default,
and work-supporting media stay bundled.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Remaining bundled novelty moved in this change.
DEMOTED_NOVELTY = {
    "ascii-video": "creative/ascii-video",
    "songwriting-and-ai-music": "creative/songwriting-and-ai-music",
    "manim-video": "creative/manim-video",
    "gif-search": "media/gif-search",
    "songsee": "media/songsee",
}

# Already-optional R2 names that must stay out of the default seed.
ALREADY_OPTIONAL_NOVELTY = {
    "pokemon-player": "gaming/pokemon-player",
    "minecraft-modpack-server": "gaming/minecraft-modpack-server",
    "meme-generation": "creative/meme-generation",
    "hyperframes": "creative/hyperframes",
    "kanban-video-orchestrator": "creative/kanban-video-orchestrator",
    "audiocraft-audio-generation": "creative/audiocraft-audio-generation",
    "heartmula": "creative/heartmula",
    "pixel-art": "creative/pixel-art",
}

# Work-supporting media that must remain bundled (R4).
WORK_SUPPORTING_MEDIA = {
    "youtube-content": "media/youtube-content",
    "pdf": "productivity/pdf",
    "nano-pdf": "productivity/nano-pdf",
    "ocr-and-documents": "productivity/ocr-and-documents",
    "architecture-diagram": "creative/architecture-diagram",
    "excalidraw": "creative/excalidraw",
    "humanizer": "creative/humanizer",
    "inspecting-hermes-desktop-dom": "software-development/inspecting-hermes-desktop-dom",
}


def _top_level_skill_dirs(root: Path) -> dict[str, Path]:
    """Map skill directory name -> path for each SKILL.md that is a skill root.

    Nested support SKILL.md files under references/templates/scripts are ignored.
    """
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for skill_md in root.rglob("SKILL.md"):
        parts = skill_md.relative_to(root).parts
        if any(part in {"references", "templates", "scripts"} for part in parts[:-1]):
            continue
        found[skill_md.parent.name] = skill_md.parent
    return found


def _bundled() -> dict[str, Path]:
    return _top_level_skill_dirs(REPO / "skills")


def _optional() -> dict[str, Path]:
    return _top_level_skill_dirs(REPO / "optional-skills")


def test_demoted_novelty_is_absent_from_bundled_seed():
    bundled = _bundled()
    present = sorted(name for name in DEMOTED_NOVELTY if name in bundled)
    assert present == [], (
        "novelty skills must not remain in skills/ (default seed): " + ", ".join(present)
    )


def test_demoted_novelty_lives_under_optional_official_paths():
    optional = _optional()
    missing = []
    misplaced = []
    for name, rel in DEMOTED_NOVELTY.items():
        path = optional.get(name)
        if path is None:
            missing.append(name)
            continue
        got = str(path.relative_to(REPO / "optional-skills"))
        if got != rel:
            misplaced.append(f"{name}: expected {rel}, got {got}")
    assert missing == [], f"missing from optional-skills/: {missing}"
    assert misplaced == [], "optional path mismatch: " + "; ".join(misplaced)


def test_already_optional_r2_names_stay_non_default():
    bundled = _bundled()
    optional = _optional()
    leaked = sorted(name for name in ALREADY_OPTIONAL_NOVELTY if name in bundled)
    missing = []
    for name, rel in ALREADY_OPTIONAL_NOVELTY.items():
        path = optional.get(name)
        if path is None:
            missing.append(name)
            continue
        got = str(path.relative_to(REPO / "optional-skills"))
        assert got == rel, f"{name}: expected optional {rel}, got {got}"
    assert leaked == [], f"already-optional R2 leaked back into skills/: {leaked}"
    assert missing == [], f"already-optional R2 missing from optional-skills/: {missing}"


def test_work_supporting_media_remain_bundled():
    bundled = _bundled()
    missing = []
    for name, rel in WORK_SUPPORTING_MEDIA.items():
        path = bundled.get(name)
        if path is None:
            missing.append(name)
            continue
        got = str(path.relative_to(REPO / "skills"))
        assert got == rel, f"{name}: expected bundled {rel}, got {got}"
    assert missing == [], f"work-supporting media left the default seed: {missing}"


def test_official_identifier_for_one_r2_name_resolves():
    """hermes skills install official/<cat>/<skill> must still resolve locally."""
    from tools.skills_hub import OptionalSkillSource

    src = OptionalSkillSource()
    src._optional_dir = REPO / "optional-skills"
    src._remote_dirs = {}

    bundle = src.fetch("official/gaming/pokemon-player")
    assert bundle is not None, "official/gaming/pokemon-player did not resolve"
    assert bundle.identifier == "official/gaming/pokemon-player"
    assert bundle.source == "official"
    assert "SKILL.md" in bundle.files
