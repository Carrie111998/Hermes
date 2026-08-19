"""Regression coverage for the Docker image browser-install policy."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"


def test_docker_image_does_not_download_playwright_browser_during_build() -> None:
    """Browser automation is optional and must not block image publication."""
    text = DOCKERFILE.read_text()

    assert "npx playwright install" not in text
    assert "PLAYWRIGHT_BROWSERS_PATH" not in text
