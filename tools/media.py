"""Where the videos are. No video file is tracked in this repository: each one is an asset of a
release (`docs/media.json`: the release tag, the repository, the base URL, and per file its asset
name, the path it once had, its size and sha256). Locally the files live in an ignored folder,
`media/<release>/`, under their asset names; the recorders write there and a person uploads them
with `tools/release_media.sh`.

An asset name is the old path under `docs/` with `/` replaced by `-`
(`docs/demo/01-layer.mp4` -> `demo-01-layer.mp4`), so names are unique in the flat release.

Standard library only: the recorders and the tests import this module by path.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "media.json"
VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".ogv")


def load() -> dict:
    """The manifest as a dict."""
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def media_dir() -> Path:
    """The local folder of the release's files: $AL_MEDIA_DIR, else media/<release>/ (git-ignored)."""
    override = os.environ.get("AL_MEDIA_DIR")
    return Path(override) if override else ROOT / "media" / load()["release"]


def asset_name(old_path: str) -> str:
    """`docs/demo/dashboard/live.mp4` -> `demo-dashboard-live.mp4`."""
    path = old_path.replace("\\", "/").lstrip("./")
    if path.startswith("docs/"):
        path = path[len("docs/"):]
    return path.replace("/", "-")


def url(name: str) -> str:
    """The download URL of one asset of the release."""
    return load()["base"] + name


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record(path: Path, was: str | None = None) -> dict:
    """Write (or replace) the manifest entry of a file in the media folder; returns the entry.
    `was` is the path the file once had in the tree; by default it is derived from an existing entry."""
    data = load()
    name = path.name
    old = next((f for f in data["files"] if f["name"] == name), None)
    entry = {"name": name, "was": was or (old or {}).get("was", ""), "bytes": path.stat().st_size, "sha256": sha256(path)}
    data["files"] = sorted([f for f in data["files"] if f["name"] != name] + [entry], key=lambda f: f["name"])
    MANIFEST.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return entry
