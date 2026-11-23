"""`al improve` and the disk: its pristine copy of the application is sparse when the layer is and
copies no git objects, a dry run's worktree is born sparse and removed when the run ends, the check
refuses when the disk has no room, and what a stopped run left behind is swept at the next start."""
from __future__ import annotations

import argparse
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import AL, LAYER_TEMPLATE, ROOT, al, commit, git, mock_scripts

ADW = "abcd0001"
BIG = 256 * 1024
SPARSE = [{"id": "worktree", "kind": "worktree", "sparse": ["src"]}]


def _tool():
    """tools/al loaded as a module (it has no .py suffix)."""
    name = "al_tool_for_improve_disk"
    if name in sys.modules:
        return sys.modules[name]
    loader = importlib.machinery.SourceFileLoader(name, str(AL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def _improve():
    if str(ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(ROOT / "tools"))
    import al_improve
    return al_improve


def grow(app: Path, test_sh: str | None = None, gitignore: bool = True) -> Path:
    """The `app` fixture plus a source directory, a large directory no agent needs and (unless told
    otherwise) a .gitignore; committed."""
    (app / "src").mkdir()
    (app / "src" / "m.py").write_text("M = 1\n")
    (app / "data").mkdir()
    (app / "data" / "big.bin").write_bytes(b"\0" * BIG)
    if gitignore:
        (app / ".gitignore").write_text("__pycache__/\n")
    if test_sh is not None:
        (app / "test.sh").write_text(test_sh)
    commit(app, "a source directory and a large one")
    return app


def check_worktree(tmp_path: Path, environments: list[dict], adws: list[dict], codebase: dict | None = None) -> Path:
    """A fake worktree of an `al improve` run, <tmp>/layer/.al-improve/trees/<run id>/, holding only
    the changed layer's agentic-layer.json."""
    worktree = tmp_path / "layer" / ".al-improve" / "trees" / ADW
    worktree.mkdir(parents=True)
    ir = {"codebase": {"defaultBranch": "main", **(codebase or {})}, "environments": environments, "adws": adws}
    (worktree / "agentic-layer.json").write_text(json.dumps(ir))
    return worktree


def namespace(app: Path) -> argparse.Namespace:
    return argparse.Namespace(app=app, sub=".", default_branch="main")


def own_objects(clone: Path) -> list[str]:
    """The packs and loose objects in the clone's own object store."""
    objects = clone / ".git" / "objects"
    return sorted(str(p.relative_to(objects)) for p in objects.rglob("*")
                  if p.is_file() and (p.parent.name == "pack" or len(p.parent.name) == 2))


def test_a_sparse_layer_gets_a_sparse_pristine_clone(app, tmp_path) -> None:
    tool, improve = _tool(), _improve()
    grow(app)
    worktree = check_worktree(tmp_path, SPARSE, [{"name": "change", "environment": ["worktree"]}],
                              {"readme": "README.md", "conditionalDocs": [{"path": "docs/rules/style.md", "when": "always"}]})
    a = namespace(app)
    paths = improve.sparse_paths(tool, worktree, a)
    assert paths == ["docs/rules", "src"]          # the sparse list, and where the prompts' documents are
    small, full = improve.checkout_bytes(app, "main", paths), improve.checkout_bytes(app, "main", [])
    assert full - small == BIG and small > 0       # the estimate leaves out exactly what the checkout leaves out

    clone = improve.pristine_app(tool, worktree, a)
    assert clone == tmp_path / "layer" / ".al-improve" / "agents" / ADW / "app"
    assert (clone / "src" / "m.py").is_file() and (clone / "app.py").is_file()
    assert not (clone / "data").exists()
    assert (clone / ".git" / "objects" / "info" / "alternates").is_file()
    assert own_objects(clone) == []                # none of the application's objects was copied
    assert git(clone, "status", "--porcelain") == ""

    tree = tmp_path / "wt"                         # the runtime's own first command for a run's worktree
    git(clone, "worktree", "add", str(tree), "-b", "x", "main")
    assert (tree / "src" / "m.py").is_file() and (tree / "app.py").is_file()
    assert not (tree / "data").exists()
    assert improve.pristine_app(tool, worktree, a) == clone    # made once per run


def test_a_layer_without_sparse_gets_a_full_clone_that_copies_no_objects(app, tmp_path) -> None:
    tool, improve = _tool(), _improve()
    grow(app)
    a = namespace(app)
    worktree = check_worktree(tmp_path, [{"id": "worktree", "kind": "worktree", "sparse": []}],
                              [{"name": "change", "environment": ["worktree"]}])
    assert improve.sparse_paths(tool, worktree, a) == []
    clone = improve.pristine_app(tool, worktree, a)
    assert (clone / "data" / "big.bin").stat().st_size == BIG and (clone / "src" / "m.py").is_file()
    assert (clone / ".git" / "objects" / "info" / "alternates").is_file()
    assert own_objects(clone) == []

    # one workflow in a sparse worktree, one on the host: the second needs every file
    shutil.rmtree(tmp_path / "layer")
    worktree = check_worktree(tmp_path, SPARSE + [{"id": "host", "kind": "host"}],
                              [{"name": "change", "environment": ["worktree"]}, {"name": "tidy", "environment": ["host"]}])
    assert improve.sparse_paths(tool, worktree, a) == []
    clone = improve.pristine_app(tool, worktree, a)
    assert (clone / "data" / "big.bin").is_file()
    assert (clone / ".git" / "objects" / "info" / "alternates").is_file()


def test_the_free_space_guard_refuses_before_anything_is_copied(app, tmp_path, monkeypatch) -> None:
    tool, improve = _tool(), _improve()
    grow(app)
    worktree = check_worktree(tmp_path, SPARSE, [{"name": "change", "environment": ["worktree"]}])
    monkeypatch.setattr(improve.shutil, "disk_usage", lambda p: SimpleNamespace(total=100, used=99, free=1))
    said: list[str] = []
    with pytest.raises(improve.CheckFailed, match="not enough free disk space") as caught:
        improve.check_dry_runs(tool, worktree, worktree, namespace(app), said.append)
    assert "#:sparse" in str(caught.value) and "the pristine copy of the application was not made" in str(caught.value)
    assert not (tmp_path / "layer" / ".al-improve" / "agents" / ADW / "app").exists()
    assert not (worktree / "trees").exists()
    monkeypatch.undo()

    room = tool.diskuse_module().room_problem
    gib, mib = 1 << 30, 1 << 20
    disk = lambda free: (lambda p: SimpleNamespace(total=100 * gib, used=100 * gib - free, free=free))  # noqa: E731
    missing = tmp_path / "not" / "there" / "yet"                       # asked at its nearest existing ancestor
    assert room(missing, gib, disk_usage=disk(50 * gib)) == ""
    refused = room(missing, 2 * gib, disk_usage=disk(gib))            # does not fit
    assert refused.startswith(f"not enough free disk space on {tmp_path}: this needs about 2.0G, 1.0G of 100G is free")
    assert "al doctor" in refused and refused.endswith("narrow the layer's worktree with #:sparse")
    assert "not enough free disk space" in room(tmp_path, 2 * gib, disk_usage=disk(11 * gib))   # would leave under 10%
    assert room(tmp_path, 2 * gib, disk_usage=disk(12 * gib)) == ""
    assert room(tmp_path, mib, disk_usage=disk(5 * gib)) == ""        # a small checkout only has to fit
    assert "not enough free disk space" in room(tmp_path, mib, disk_usage=disk(mib - 1))


def test_a_dead_runs_leftovers_are_swept_and_a_live_runs_are_not(tmp_path) -> None:
    improve = _improve()
    self_dir = tmp_path / "layer" / ".al-improve"
    for run in ("aaaa0001", "bbbb0002"):
        (self_dir / "agents" / run / "app").mkdir(parents=True)
        (self_dir / "agents" / run / "app" / "x").write_text("x")
        (self_dir / "agents" / run / "state.json").write_text("{}")
        (self_dir / "trees" / run / "trees" / "11112222").mkdir(parents=True)
        (self_dir / "trees" / run / "trees" / "11112222" / "big").write_text("big")
        (self_dir / "trees" / run / "agentic.rkt").write_text("")
    (self_dir / "agents" / "aaaa0001" / "run.lock").write_text("")     # a lock nobody holds: the run is dead
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (self_dir / "trees" / "aaaa0001" / "trees" / "33334444").symlink_to(outside, target_is_directory=True)
    (self_dir / "agents" / "notarun" / "app").mkdir(parents=True)
    lock = self_dir / "agents" / "bbbb0002" / "run.lock"
    lock.write_text("")
    held = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        said: list[str] = []
        improve.sweep_stale(self_dir, ".", said.append)
        assert said == ["removed what run aaaa0001 left behind: 1 dry-run worktree(s) and its copy of the application"]
        assert not (self_dir / "agents" / "aaaa0001" / "app").exists()
        assert not (self_dir / "trees" / "aaaa0001" / "trees" / "11112222").exists()
        assert (self_dir / "agents" / "aaaa0001" / "state.json").is_file()        # the run's record stays
        assert (self_dir / "trees" / "aaaa0001" / "agentic.rkt").is_file()        # and so does its kept worktree
        assert (outside / "keep").is_file()                                       # nothing outside .al-improve
        assert (self_dir / "agents" / "notarun" / "app").is_dir()                 # not a run id
        assert (self_dir / "agents" / "bbbb0002" / "app" / "x").is_file()         # the live run keeps everything
        assert (self_dir / "trees" / "bbbb0002" / "trees" / "11112222" / "big").is_file()
        improve.sweep_stale(self_dir, ".", said.append)
        assert len(said) == 1                                                     # nothing left to say
    finally:
        os.close(held)
    improve.sweep_stale(self_dir, ".", said.append)                               # its lock released: now it is dead
    assert said[1:] == ["removed what run bbbb0002 left behind: 1 dry-run worktree(s) and its copy of the application"]
    assert not (self_dir / "agents" / "bbbb0002" / "app").exists()


# ---------------------------------------------------------------- end to end, with a git that logs what it left

WRAPPER = """#!{python}
# runs the real git, then logs what is in the destination of every clone, worktree add and sparse-checkout
import json, os, subprocess, sys
args = sys.argv[1:]
code = subprocess.call([{real!r}, *args])
at, i = os.getcwd(), 0
while i < len(args) and args[i].startswith("-"):
    if args[i] == "-C":
        at = os.path.join(at, args[i + 1])
    i += 2 if args[i] in ("-C", "-c") else 1
rest = args[i:]
dest = None
if rest[:1] == ["clone"]:
    dest = rest[-1]
elif rest[:2] == ["worktree", "add"]:
    dest = rest[2]
elif rest[:1] == ["sparse-checkout"]:
    dest = "."
if dest is not None:
    dest = os.path.normpath(os.path.join(at, dest))
    listing = sorted(n for n in os.listdir(dest) if n != ".git") if os.path.isdir(dest) else None
    with open({log!r}, "a") as log:
        log.write(json.dumps({{"cmd": " ".join(rest[:2] if rest[0] == "worktree" else rest[:1]), "dest": dest,
                              "ls": listing, "exit": code}}) + "\\n")
sys.exit(code)
"""


def logging_git(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """(the environment that puts the logging git first on PATH and in AL_GIT, its log file)."""
    real = shutil.which("git")
    assert real
    directory, log = tmp_path / "git-bin", tmp_path / "git.log"
    directory.mkdir()
    wrapper = directory / "git"
    wrapper.write_text(WRAPPER.format(python=sys.executable, real=real, log=str(log)))
    wrapper.chmod(0o755)
    return {"PATH": f"{directory}{os.pathsep}{os.environ.get('PATH', '')}", "AL_GIT": str(wrapper)}, log


def logged(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines() if line] if log.is_file() else []


def sparse_sibling(app: Path) -> Path:
    """<app>.al: a sibling layer whose only workflow runs in a worktree that checks out src/ only."""
    layer = app.parent / "notes.al"
    layer.mkdir()
    text = LAYER_TEMPLATE.format(root="../notes", placement='(layer sibling)\n  (environment worktree #:sparse ("src"))')
    assert '(adw change #:description "Plan, build, test; a person reviews"' in text
    text = text.replace('(adw change #:description "Plan, build, test; a person reviews"',
                        '(adw change #:description "Plan, build, test; a person reviews" #:environment (worktree)')
    (layer / "agentic.rkt").write_text(text)
    al("gen", layer)
    return layer


def scripted_change(layer: Path, tmp_path: Path) -> Path:
    plan = ("# Layer change: focus line\n\n## Request\nfocus line\n\n## Summary\nfocus line (scripted plan).\n\n"
            "## What changes for you\n- plans mention the notes list\n\n## How\nThe smallest change.\n\n"
            "## The change, step by step\n### Change the layer\n- as scripted\n\n## How it is proven\n- definition\n\n"
            "## What is left out\nnone\n")
    before = "(command plan #:template plan-chore #:model-set heavy #:output path #:validation (test))"
    source = (layer / "agentic.rkt").read_text()
    assert before in source
    return mock_scripts(tmp_path, {
        "plan": {"layer_files": {"specs/layer-change.md": plan}, "output": "specs/layer-change.md"},
        "implement": {"files": {"agentic.rkt": source.replace(
            before, before[:-1] + '\n           #:focus ("Name the notes list NOTES in every plan."))')}}})


def dry_run_trees(layer: Path, log: Path) -> list[dict]:
    """The logged `git worktree add` calls that made a dry run's worktree."""
    trees = (layer / ".al-improve" / "trees" / ADW / "trees").resolve()
    return [row for row in logged(log) if row["cmd"] == "worktree add" and Path(row["dest"]).resolve().parent == trees]


def test_dry_runs_of_a_sparse_sibling_layer_never_check_out_the_rest(app, tmp_path) -> None:
    grow(app)
    layer = sparse_sibling(app)
    env, log = logging_git(tmp_path)
    cp = al("improve", layer, "Plans must name the notes list", "--dry-run", "--adw-id", ADW,
            "--mock-scripts", scripted_change(layer, tmp_path), env=env, timeout=1800)
    assert "dry-runs=0" in cp.stdout + cp.stderr, cp.stdout + cp.stderr
    clone = (layer / ".al-improve" / "agents" / ADW / "app").resolve()
    clones = [row for row in logged(log) if row["cmd"] == "clone" and Path(row["dest"]).resolve() == clone]
    assert len(clones) == 1 and clones[0]["exit"] == 0
    assert clones[0]["ls"] == []                       # nothing is checked out before the clone is sparse
    narrowed = [row for row in logged(log) if row["cmd"] == "sparse-checkout" and Path(row["dest"]).resolve() == clone]
    assert narrowed and all(row["exit"] == 0 for row in narrowed)
    trees = dry_run_trees(layer, log)
    assert trees, logged(log)
    for row in trees:                                  # born sparse: no moment with the rest of the application
        assert row["exit"] == 0 and "src" in row["ls"] and "app.py" in row["ls"] and "data" not in row["ls"], row
        assert not Path(row["dest"]).exists()          # and removed when its dry run ended
    assert not clone.exists()
    assert (layer / ".al-improve" / "agents" / ADW / "dry-runs" / "change.log").is_file()
    assert (app / "data" / "big.bin").is_file() and git(app, "status", "--porcelain") == ""
    assert git(app, "branch", "--list") == "* main"    # dry runs wrote into the clone, never into the application


def test_a_failed_dry_run_leaves_no_worktree_behind(app, tmp_path) -> None:
    grow(app, test_sh="#!/bin/sh\nexit 1\n")           # the workflow's test check fails in every dry run
    layer = sparse_sibling(app)
    env, log = logging_git(tmp_path)
    cp = al("improve", layer, "Plans must name the notes list", "--dry-run", "--adw-id", ADW,
            "--mock-scripts", scripted_change(layer, tmp_path), env=env, ok=False, timeout=1800)
    said = cp.stdout + cp.stderr
    assert cp.returncode != 0 and "dry-runs=0" not in said, said
    trees = dry_run_trees(layer, log)
    assert trees, said
    assert all(row["exit"] == 0 and "data" not in row["ls"] for row in trees), trees
    assert [row["dest"] for row in trees if Path(row["dest"]).exists()] == []
    kept = layer / ".al-improve" / "trees" / ADW / "trees"
    assert not kept.is_dir() or list(kept.iterdir()) == []
    assert not (layer / ".al-improve" / "agents" / ADW / "app").exists()
    log_file = layer / ".al-improve" / "agents" / ADW / "dry-runs" / "change.log"
    assert log_file.is_file()                          # what a person reads instead of the worktree
