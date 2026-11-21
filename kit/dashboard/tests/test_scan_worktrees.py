"""--scan lists a repository's layer once, however many git worktrees of it lie below the scanned directory."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import dashboard as d

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main"]


def git(cwd: Path, *args: str) -> None:
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(path: Path, name: str = "al") -> Path:
    """A repository with agentic/agentic-layer.json {"name": name} and one commit."""
    (path / "agentic").mkdir(parents=True)
    (path / "agentic" / d.LAYER_FILE).write_text(json.dumps({"name": name}))
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def test_scan_lists_one_layer_for_two_worktrees_of_one_repository(tmp_path: Path) -> None:
    app = make_repo(tmp_path / "app")
    git(app, "worktree", "add", "-q", "--detach", "../app-land")
    model = d.Model([], [tmp_path])
    model.discover()
    assert sorted(model.layers) == ["al"]
    assert model.layers["al"].path == real(app / "agentic")
    git(app, "worktree", "add", "-q", "--detach", "../a-land")   # sorts before app: the main checkout still wins
    model = d.Model([], [tmp_path])
    model.discover()
    assert sorted(model.layers) == ["al"]
    assert model.layers["al"].path == real(app / "agentic")


def test_explicit_layer_of_a_worktree_still_shows(tmp_path: Path) -> None:
    app = make_repo(tmp_path / "app")
    git(app, "worktree", "add", "-q", "--detach", "../app-land")
    land = tmp_path / "app-land" / "agentic"
    model = d.Model([land], [])
    model.discover()
    assert {k: layer.path for k, layer in model.layers.items()} == {"al": real(land)}
    model = d.Model([app / "agentic", land], [])
    model.discover()
    assert sorted(model.layers) == ["al", "al-2"]
    assert {layer.path for layer in model.layers.values()} == {real(app / "agentic"), real(land)}
    model = d.Model([land], [tmp_path])
    model.discover()
    assert real(land) in {layer.path for layer in model.layers.values()}


def test_two_repositories_with_the_same_layer_name_both_show(tmp_path: Path) -> None:
    make_repo(tmp_path / "one")
    make_repo(tmp_path / "two")
    model = d.Model([], [tmp_path])
    model.discover()
    assert sorted(model.layers) == ["al", "al-2"]
