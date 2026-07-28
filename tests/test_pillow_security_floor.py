import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


REPO_ROOT = Path(__file__).resolve().parents[1]


def _requirement_for(package: str, specs: list[str] | tuple[str, ...]) -> Requirement:
    package_name = canonicalize_name(package)
    requirements = []
    for spec in specs:
        requirement = Requirement(spec)
        if canonicalize_name(requirement.name) == package_name:
            requirements.append(requirement)
    assert len(requirements) == 1, (
        f"expected exactly one {package} requirement, found {requirements}"
    )
    return requirements[0]


def test_pillow_requirement_is_synchronized() -> None:
    from tools.lazy_deps import LAZY_DEPS

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core_requirement = _requirement_for("pillow", project["project"]["dependencies"])
    vision_requirement = _requirement_for("pillow", LAZY_DEPS["tool.vision"])

    assert core_requirement.specifier
    assert all(specifier.operator == "==" for specifier in core_requirement.specifier)
    assert vision_requirement.specifier == core_requirement.specifier

    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [
        Version(package["version"])
        for package in lock["package"]
        if package["name"].lower() == "pillow"
    ]
    assert versions, "pillow not found in uv.lock"
    assert all(version in core_requirement.specifier for version in versions)
