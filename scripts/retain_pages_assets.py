#!/usr/bin/env python3
"""Retain hashed assets from the previous GitHub Pages deployment.

Why: deploy-site.yml deploys on every push to main touching website/** —
often several times an hour. GitHub Pages keeps ONLY the newest deploy's
files, while the CDN chain in front (Vercel proxy -> Fastly -> Pages)
serves cached HTML for up to ~1 hour (max-age=300 +
stale-while-revalidate=3600). Stale HTML references the PREVIOUS deploy's
content-hashed bundles (main.<hash>.js, lazy chunks), which the new deploy
just deleted -> sitewide 404s on JS/CSS, dead search, broken lazy routes,
for a large fraction of the day at our deploy cadence.

Fix (class fix, not a debounce): before uploading the new Pages artifact,
download the previous successful deployment's artifact and union-merge its
hashed asset files into the new tree. Hashed filenames are
content-addressed, so a name collision means identical content — new build
always wins, old files are only ADDED when absent.

Growth is bounded by a retention manifest (docs/assets-retention.json in
the deployed tree): every carried-forward file records when it was first
retained and is dropped after RETENTION_DAYS. Files present in the current
build never need entries.

Best-effort by design: any failure prints a warning and exits 0 — asset
retention must never block a deploy.

Usage: python3 scripts/retain_pages_assets.py <site_dir>
Requires: gh CLI authenticated (GH_TOKEN), tar. Run from repo root in CI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

RETENTION_DAYS = 7
ARTIFACT_NAME = "github-pages"
WORKFLOW = "deploy-site.yml"
MANIFEST_REL = "docs/assets-retention.json"
# Only content-hashed, immutable output is retained. HTML and data files
# must always come from the current build.
RETAIN_DIRS = ("docs/assets/", "docs/zh-Hans/assets/")


def log(msg: str) -> None:
    print(f"[retain-pages-assets] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"::warning::retain_pages_assets: {msg}", flush=True)


def gh_json(args: list[str]):
    out = subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True, timeout=120
    ).stdout
    return json.loads(out)


def find_previous_artifact() -> dict | None:
    current_run = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "NousResearch/hermes-agent")
    runs = gh_json([
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        WORKFLOW,
        "--status",
        "success",
        "--limit",
        "10",
        "--json",
        "databaseId",
    ])
    for run in runs:
        run_id = str(run["databaseId"])
        if run_id == current_run:
            continue
        artifacts = gh_json([
            "api",
            f"repos/{repo}/actions/runs/{run_id}/artifacts",
            "--jq",
            "{artifacts: [.artifacts[] | {id, name, expired}]}",
        ])["artifacts"]
        for artifact in artifacts:
            if artifact["name"] == ARTIFACT_NAME and not artifact["expired"]:
                log(f"using artifact {artifact['id']} from run {run_id}")
                return {"repo": repo, "id": artifact["id"]}
    return None


def download_and_extract(repo: str, artifact_id: int, dest: Path) -> Path:
    zip_path = dest / "artifact.zip"
    with zip_path.open("wb") as fh:
        subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/artifacts/{artifact_id}/zip"],
            check=True,
            stdout=fh,
            timeout=600,
        )
    extract_dir = dest / "prev"
    extract_dir.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    # Pages artifacts wrap the site in a single tar.
    tars = list(extract_dir.glob("*.tar"))
    if tars:
        tree = dest / "prev_tree"
        tree.mkdir()
        with tarfile.open(tars[0]) as tf:
            tf.extractall(tree, filter="data")
        return tree
    return extract_dir


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    site_dir = Path(sys.argv[1]).resolve()
    if not site_dir.is_dir():
        warn(f"site dir {site_dir} does not exist; skipping retention")
        return 0

    try:
        ref = find_previous_artifact()
        if ref is None:
            log("no previous github-pages artifact found; nothing to retain")
            return 0
        with tempfile.TemporaryDirectory() as td:
            prev_tree = download_and_extract(ref["repo"], ref["id"], Path(td))

            old_manifest: dict[str, float] = {}
            manifest_path = prev_tree / MANIFEST_REL
            if manifest_path.is_file():
                try:
                    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    old_manifest = {}

            now = time.time()
            cutoff = now - RETENTION_DAYS * 86400
            new_manifest: dict[str, float] = {}
            copied = expired = 0

            for retain_root in RETAIN_DIRS:
                root = prev_tree / retain_root
                if not root.is_dir():
                    continue
                for src in root.rglob("*"):
                    if not src.is_file():
                        continue
                    rel = src.relative_to(prev_tree).as_posix()
                    target = site_dir / rel
                    if target.exists():
                        continue  # present in current build — no entry needed
                    first_seen = old_manifest.get(rel, now)
                    if first_seen < cutoff:
                        expired += 1
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
                    new_manifest[rel] = first_seen
                    copied += 1

            (site_dir / MANIFEST_REL).write_text(
                json.dumps(new_manifest, indent=0, sort_keys=True),
                encoding="utf-8",
            )
            log(
                f"retained {copied} previous asset file(s), "
                f"expired {expired}, manifest entries {len(new_manifest)}"
            )
        return 0
    except Exception as exc:  # noqa: BLE001 — retention is best-effort
        warn(f"asset retention failed ({exc!r}); deploying without retention")
        return 0


if __name__ == "__main__":
    sys.exit(main())
