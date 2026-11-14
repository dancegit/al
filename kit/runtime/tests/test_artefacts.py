from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from adw_modules.artefacts import collect
from adw_modules.ir import load


def test_image_cap_hash_and_manifest(make_layer: object, tmp_path: Path) -> None:
    layer = load(make_layer({"artefacts": [{"id": "shots", "kind": "image", "glob": "shots/**/*.png", "maxFiles": 2, "status": "stable"}]}))
    worktree = tmp_path / "worktree"
    (worktree / "shots" / "nested").mkdir(parents=True)
    for index in range(4):
        (worktree / "shots" / "nested" / f"{index}.png").write_bytes(f"image{index}".encode())
    state = SimpleNamespace(adw_id="1234abcd", worktree=str(worktree), checks={}, artefacts=[])
    result = collect(layer, state, ["shots"], "test")
    assert len(result) == 2
    assert result[0]["sha256"] == hashlib.sha256((worktree / "shots/nested/0.png").read_bytes()).hexdigest()
    manifest = json.loads((layer.layer_dir / "artefacts/1234abcd/manifest.json").read_text())
    assert len(manifest) == 2


def test_video_generates_keyframes_when_ffmpeg_available(make_layer: object, tmp_path: Path) -> None:
    import shutil
    import subprocess
    import pytest

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe unavailable")
    layer = load(make_layer({"artefacts": [{"id": "clips", "kind": "video", "glob": "*.mp4", "keyframes": 2, "status": "stable"}]}))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    clip = worktree / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10", "-pix_fmt", "yuv420p", str(clip)], capture_output=True, text=True, timeout=120, check=True)
    state = SimpleNamespace(adw_id="1234abcd", worktree=str(worktree), checks={}, artefacts=[])
    result = collect(layer, state, ["clips"], "test")
    assert len(result) == 1
    assert len(result[0]["keyframes"]) == 2
