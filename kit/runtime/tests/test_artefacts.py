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


def _files_under(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_a_later_collection_replaces_the_earlier_one(make_layer: object, tmp_path: Path) -> None:
    """After a patch round the e2e run no longer produces a screenshot: the reviewers must not be
    shown it any more. Manifest and artefacts/<id>/<artefact>/ reflect exactly the last collection."""
    layer = load(make_layer({"artefacts": [{"id": "shots", "kind": "image", "glob": "shots/*.png", "maxFiles": 12, "status": "stable"},
                                           {"id": "other", "kind": "file", "glob": "notes/*.txt", "status": "stable"}]}))
    worktree = tmp_path / "worktree"
    (worktree / "shots").mkdir(parents=True)
    (worktree / "notes").mkdir()
    (worktree / "shots" / "01-board.png").write_bytes(b"board v1")
    (worktree / "shots" / "02-old-dropdown.png").write_bytes(b"dropdown")
    (worktree / "notes" / "n.txt").write_text("kept\n")
    state = SimpleNamespace(adw_id="1234abcd", worktree=str(worktree), checks={}, artefacts=[])
    collect(layer, state, ["shots", "other"], "e2e")

    (worktree / "shots" / "02-old-dropdown.png").unlink()          # the new e2e run no longer creates it
    (worktree / "shots" / "01-board.png").write_bytes(b"board v2")   # and this one changed
    (worktree / "shots" / "03-new.png").write_bytes(b"new")
    second = collect(layer, state, ["shots"], "e2e")

    run_root = layer.layer_dir / "artefacts" / "1234abcd"
    assert _files_under(run_root / "shots") == {"shots/01-board.png", "shots/03-new.png"}
    manifest = json.loads((run_root / "manifest.json").read_text())
    assert manifest == state.artefacts
    shots = sorted((item for item in manifest if item["id"] == "shots"), key=lambda item: item["path"])
    assert [Path(item["path"]).name for item in shots] == ["01-board.png", "03-new.png"]
    assert shots[0]["sha256"] == hashlib.sha256(b"board v2").hexdigest()
    assert [item["path"] for item in second] == [item["path"] for item in shots]
    # an artefact id that was not collected again is left alone
    assert [Path(item["path"]).name for item in manifest if item["id"] == "other"] == ["n.txt"]
    assert (run_root / "other" / "notes" / "n.txt").is_file()


def test_a_replaced_video_loses_its_old_keyframes(make_layer: object, tmp_path: Path, monkeypatch) -> None:
    from adw_modules import artefacts

    def fake_keyframes(source: Path, target_dir: Path, count: int):
        target_dir.mkdir(parents=True, exist_ok=True)
        frames = [target_dir / f"{source.stem}-{index + 1:02d}.png" for index in range(count)]
        for frame in frames:
            frame.write_bytes(source.read_bytes() + frame.name.encode())
        return frames, None
    monkeypatch.setattr(artefacts, "extract_keyframes", fake_keyframes)
    layer = load(make_layer({"artefacts": [{"id": "rec", "kind": "video", "glob": "*.webm", "keyframes": 2, "status": "stable"}]}))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "old.webm").write_bytes(b"old video")
    state = SimpleNamespace(adw_id="1234abcd", worktree=str(worktree), checks={}, artefacts=[])
    collect(layer, state, ["rec"], "e2e")
    (worktree / "old.webm").unlink()
    (worktree / "session.webm").write_bytes(b"new video")
    collect(layer, state, ["rec"], "e2e")
    root = layer.layer_dir / "artefacts" / "1234abcd" / "rec"
    assert _files_under(root) == {"session.webm", "session.keyframes/session-01.png", "session.keyframes/session-02.png"}
    assert [Path(item["path"]).name for item in state.artefacts] == ["session.webm"]
    assert all("session.keyframes" in frame["path"] for frame in state.artefacts[0]["keyframes"])


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
