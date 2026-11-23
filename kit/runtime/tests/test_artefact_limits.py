"""The limit of an image artefact (`#:max-files`): the files of the change are handed over first,
and the reviewers' manifest says how many matching files were left out."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from adw_modules import artefacts, evidence
from adw_modules.ir import load

RUN = "1234abcd"
SHOTS = {"id": "shots", "kind": "image", "glob": "shots/*.png", "maxFiles": 12, "status": "stable"}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _worktree(app: Path, tmp_path: Path, old: int) -> Path:
    """A worktree of the application on a run's branch; `old` screenshots are on main already."""
    (app / "shots").mkdir()
    for index in range(old):
        (app / "shots" / f"{index:02d}-old.png").write_bytes(f"old {index}".encode())
    _git(app, "add", "-A")
    _git(app, "commit", "-q", "-m", "the screenshots of earlier changes")
    worktree = tmp_path / "worktree"
    _git(app, "worktree", "add", "-q", "-b", "chore/1234abcd-shelf", str(worktree), "main")
    return worktree


def _state(worktree: Path) -> SimpleNamespace:
    return SimpleNamespace(adw_id=RUN, worktree=str(worktree), checks={}, artefacts=[])


def _names(records: list[dict]) -> list[str]:
    return [Path(item["path"]).name for item in records]


def test_files_of_the_change_come_first(make_layer, app_repo: Path, tmp_path: Path, capsys) -> None:
    layer = load(make_layer({"artefacts": [SHOTS]}))
    worktree = _worktree(app_repo, tmp_path, old=11)
    (worktree / "shots" / "zz-new-1.png").write_bytes(b"new 1")               # untracked
    (worktree / "shots" / "zz-new-2.png").write_bytes(b"new 2")
    _git(worktree, "add", "shots/zz-new-2.png")                              # staged
    (worktree / "shots" / "zz-new-3.png").write_bytes(b"new 3")
    _git(worktree, "add", "shots/zz-new-3.png")
    _git(worktree, "commit", "-q", "-m", "a screenshot the run committed")   # committed by the run
    assert len(list((worktree / "shots").glob("*.png"))) == 14
    collected = artefacts.collect(layer, _state(worktree), ["shots"], "review")
    names = _names(collected)
    assert len(names) == 12
    assert names[:3] == ["zz-new-1.png", "zz-new-2.png", "zz-new-3.png"], names
    assert names[3:] == [f"{index:02d}-old.png" for index in range(9)]
    out = capsys.readouterr().out
    assert (f"[adw {RUN}] artefact shots: 12 of 14 files handed over, 2 left out (limit 12); "
            "files the proof plan names and of this change first") in out


def test_files_a_proof_row_writes_come_first(make_layer, app_repo: Path, tmp_path: Path) -> None:
    layer = load(make_layer({"artefacts": [SHOTS]}))
    worktree = _worktree(app_repo, tmp_path, old=14)                         # none changed by the run
    state = _state(worktree)
    writes = [{"glob": "shots/1[23]-old.png", "count": 2}]
    state.proof_plan = {"rows": [{"id": "C1", "writes": writes}]}
    collected = artefacts.collect(layer, state, ["shots"], "review")
    names = _names(collected)
    assert names[:2] == ["12-old.png", "13-old.png"], names
    assert names[2:] == [f"{index:02d}-old.png" for index in range(10)]
    assert len(names) == 12
    # the evidence pack finds the row's files: no `files-missing`
    assert evidence._files(state, {"writes": writes})[1] == []


def test_files_the_proof_table_names_come_first(make_layer, app_repo: Path, tmp_path: Path) -> None:
    layer = load(make_layer({"artefacts": [SHOTS]}))
    worktree = _worktree(app_repo, tmp_path, old=14)
    (worktree / "shots" / "zz-new.png").write_bytes(b"new")                  # untracked
    (worktree / "shots" / "voice-engine-1280.png").write_bytes(b"voice")     # untracked, named by C6
    state = _state(worktree)
    state.proof_plan = {"rows": [{"id": "C6", "writes": [{"glob": "shots/voice-engine-1280.png", "count": 1}]}]}
    collected = artefacts.collect(layer, state, ["shots"], "evidence")
    names = _names(collected)
    assert len(names) == 12
    assert names[0] == "voice-engine-1280.png", names
    assert names[1] == "zz-new.png", names
    assert names[2:] == [f"{index:02d}-old.png" for index in range(10)]
    assert all(item["left_out"] == 4 and item["matched"] == 16 for item in collected)


def test_manifest_says_how_many_were_left_out(make_layer, app_repo: Path, tmp_path: Path) -> None:
    layer = load(make_layer({"artefacts": [SHOTS, {**SHOTS, "id": "few", "glob": "few/*.png"}]}))
    worktree = _worktree(app_repo, tmp_path, old=14)
    (worktree / "few").mkdir()
    (worktree / "few" / "one.png").write_bytes(b"one")
    state = _state(worktree)
    artefacts.collect(layer, state, ["shots", "few"], "review")
    manifest = json.loads(artefacts.manifest_path(layer, RUN).read_text())
    left = [item for item in manifest if item["type"] == "left-out"]
    assert left == [{"id": "shots", "type": "left-out", "step": "review", "path": "", "left_out": 2, "matched": 14,
                     "limit": 12}]
    shots = [item for item in state.artefacts if item["id"] == "shots"]
    assert len(shots) == 12 and all(item["left_out"] == 2 and item["matched"] == 14 for item in shots)
    few = [item for item in state.artefacts if item["id"] == "few"]
    assert len(few) == 1 and "left_out" not in few[0] and "matched" not in few[0]
    assert all(item["type"] != "left-out" for item in state.artefacts), "the entry is the manifest's, not the run's"
    assert artefacts.left_out_entries(state) == left
    # nothing is left out any more: the entry goes
    for path in sorted((worktree / "shots").glob("*.png"))[:2]:
        path.unlink()
    artefacts.collect(layer, state, ["shots"], "review")
    manifest = json.loads(artefacts.manifest_path(layer, RUN).read_text())
    assert not [item for item in manifest if item["type"] == "left-out"]
    assert artefacts.left_out_entries(state) == []


def test_order_is_by_name_when_git_cannot_say(make_layer, tmp_path: Path) -> None:
    layer = load(make_layer({"artefacts": [{**SHOTS, "maxFiles": 2}]}))
    worktree = tmp_path / "plain"                 # no git repository
    (worktree / "shots").mkdir(parents=True)
    for name in ("c.png", "a.png", "d.png", "b.png"):
        (worktree / "shots" / name).write_bytes(name.encode())
    state = _state(worktree)
    assert artefacts._changed_by_run(layer, state, worktree) == set()
    collected = artefacts.collect(layer, state, ["shots"], "review")
    assert _names(collected) == ["a.png", "b.png"]
    assert [item["left_out"] for item in collected] == [2, 2]
