"""envsource: the image and the flake a layer reuses from the repository are made ready before the
first step, without envs.py knowing. `podman` and `nix` are fake executables on PATH that record
their arguments, so these tests need neither."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import envs, envsource, ir, sandbox
from adw_modules.errors import LayerError, StepFailed

FAKE = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$FAKE_LOG"
case "$1 $2" in
  "image inspect") if [ -n "$FAKE_FP" ]; then echo "$FAKE_FP"; exit 0; fi; exit 1 ;;
  "build "*|"build") if [ -n "$FAKE_BUILD_FAIL" ]; then echo "step 3/5: something broke" >&2; exit 1; fi; exit 0 ;;
  "flake lock") if [ -n "$FAKE_NIX_FAIL" ]; then echo "error: cannot fetch app" >&2; exit 1; fi; exit 0 ;;
esac
exit 0
"""

PODMAN = {"id": "sandbox", "kind": "podman", "status": "stable", "baseImage": "docker.io/library/debian:stable-slim",
          "packages": [], "setup": [], "network": "slirp", "memory": "2g", "cpus": "1", "agentInside": False,
          "scope": "run", "caches": [], "credentials": [], "publish": [], "services": [], "pidsLimit": 512,
          "readOnlyRoot": False, "containerfile": "Containerfile", "target": "dev", "context": ".", "devcontainer": "",
          "browser": False}
WORKTREE = {"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 19400, "portsPerRun": 2,
            "keep": "never", "status": "stable", "prepare": []}
NIX = {"id": "nix-devshell", "kind": "nix-devshell", "status": "stable", "packages": [], "nixpkgs": "x", "shellHook": "",
       "pure": False, "flake": ".", "shell": "default", "browser": False}


@pytest.fixture
def fakes(tmp_path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("podman", "nix"):
        (bin_dir / name).write_text(FAKE)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    for name in ("FAKE_FP", "FAKE_BUILD_FAIL", "FAKE_NIX_FAIL"):
        monkeypatch.delenv(name, raising=False)
    return log


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def setup_layer(make_layer, app_repo: Path, source: dict | None, environments: list[dict], publish: list[int] | None = None):
    (app_repo / "Containerfile").write_text("FROM docker.io/library/debian:stable-slim AS dev\n")
    subprocess.run(["git", "-C", str(app_repo), "add", "Containerfile"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(app_repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "cf"],
                   check=True, capture_output=True)
    envs_ir = [dict(e, publish=publish if publish is not None else e.get("publish", [])) if e["kind"] == "podman" else e
               for e in environments]
    layer_dir = make_layer({"environments": envs_ir})
    (layer_dir / "env").mkdir(exist_ok=True)
    (layer_dir / "env/Containerfile").write_text("FROM localhost/al-test-layer-base:latest\nENTRYPOINT []\n")
    if source is not None:
        (layer_dir / "env/source.json").write_text(json.dumps(source))
    layer = ir.load(layer_dir)
    state = SimpleNamespace(adw_id="0badc0de", worktree=str(app_repo), ports=[19400, 19401], klass="chore",
                            prompt="p", branch="", plan="")
    stack = envs.build(layer, state, [e["id"] for e in environments])
    return layer, state, stack


BUILD_SOURCE = {"version": 1, "nix": None,
                "podman": {"option": "sandbox", "build": {"containerfile": "Containerfile", "target": "dev", "context": "."},
                           "image": "", "publish": [], "prepare": "", "notHonoured": []}}


def test_no_source_does_nothing(make_layer, app_repo, fakes):
    layer, state, stack = setup_layer(make_layer, app_repo, None, [WORKTREE, PODMAN])
    envsource.apply_publish(layer, stack)
    envsource.ensure_ready(layer, state, stack)
    assert envsource.load(layer) == {} and envsource.prepare_commands(layer, stack) == []
    assert calls(fakes) == []


@pytest.fixture
def recipe_images(monkeypatch) -> list[str]:
    """Records sandbox.ensure_image calls: the recipe image on top of the base is the sandbox's job."""
    ensured: list[str] = []
    monkeypatch.setattr(sandbox, "ensure_image", lambda layer, say=print: ensured.append(layer.name) or {})
    return ensured


def test_base_then_final_image_with_fingerprint(make_layer, app_repo, fakes, recipe_images):
    layer, state, stack = setup_layer(make_layer, app_repo, BUILD_SOURCE, [WORKTREE, PODMAN])
    envsource.ensure_ready(layer, state, stack)
    fp = envsource.fingerprint(layer, BUILD_SOURCE["podman"])
    assert calls(fakes) == [
        'podman image inspect localhost/al-test-layer-base:latest --format {{ index .Labels "al.env-fingerprint" }}',
        f"podman build -f {app_repo}/Containerfile --target dev --label al.env-fingerprint={fp} "
        f"-t localhost/al-test-layer-base:latest {app_repo}/.",
    ]
    assert recipe_images == ["test-layer"]
    assert (Path(layer.layer_dir) / "agents/.env-source.lock").exists()


def test_matching_fingerprint_skips_the_build(make_layer, app_repo, fakes, monkeypatch, recipe_images):
    layer, state, stack = setup_layer(make_layer, app_repo, BUILD_SOURCE, [WORKTREE, PODMAN])
    monkeypatch.setenv("FAKE_FP", envsource.fingerprint(layer, BUILD_SOURCE["podman"]))
    envsource.ensure_ready(layer, state, stack)
    assert [c for c in calls(fakes) if " build " in f" {c} "] == []
    assert recipe_images == ["test-layer"]  # the recipe image is still ensured on top


def test_fingerprint_changes_with_the_repository_file(make_layer, app_repo, fakes):
    layer, _state, _stack = setup_layer(make_layer, app_repo, BUILD_SOURCE, [WORKTREE, PODMAN])
    before = envsource.fingerprint(layer, BUILD_SOURCE["podman"])
    (app_repo / "Containerfile").write_text("FROM docker.io/library/debian:stable-slim AS dev\nRUN true\n")
    assert envsource.fingerprint(layer, BUILD_SOURCE["podman"]) == before  # the working tree does not count
    subprocess.run(["git", "-C", str(app_repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "change"],
                   check=True, capture_output=True)
    assert envsource.fingerprint(layer, BUILD_SOURCE["podman"]) != before


def test_build_failure_is_a_step_failure(make_layer, app_repo, fakes, monkeypatch):
    layer, state, stack = setup_layer(make_layer, app_repo, BUILD_SOURCE, [WORKTREE, PODMAN])
    monkeypatch.setenv("FAKE_BUILD_FAIL", "1")
    with pytest.raises(StepFailed) as failure:
        envsource.ensure_ready(layer, state, stack)
    assert failure.value.step == "environment setup"
    assert "something broke" in str(failure.value) and "Containerfile" in str(failure.value)


def test_forwarded_ports_are_merged(make_layer, app_repo, fakes):
    source = json.loads(json.dumps(BUILD_SOURCE))
    source["podman"]["publish"] = [8000]
    layer, _state, stack = setup_layer(make_layer, app_repo, source, [WORKTREE, PODMAN])
    envsource.apply_publish(layer, stack)
    assert stack.podman_option["publish"] == [8000]
    assert layer.option("environments", "sandbox")["publish"] == []  # the loaded definition is not changed
    source["podman"]["publish"] = [8000, 9000]
    layer, _state, stack = setup_layer(make_layer, app_repo, source, [WORKTREE, PODMAN], publish=[5000, 8000])
    envsource.apply_publish(layer, stack)
    assert stack.podman_option["publish"] == [5000, 8000, 9000]
    with pytest.raises(LayerError, match="portsPerRun"):  # envs.py's own check still applies
        stack.setup()


def test_flake_input_is_refreshed(make_layer, app_repo, fakes, monkeypatch):
    source = {"version": 1, "podman": None, "nix": {"option": "nix-devshell", "flake": ".", "shell": "default"}}
    layer, state, stack = setup_layer(make_layer, app_repo, source, [WORKTREE, NIX])
    envsource.ensure_ready(layer, state, stack)
    assert calls(fakes) == [f"nix flake lock --update-input app path:{Path(layer.layer_dir) / 'env'}"]
    assert (Path(layer.layer_dir) / "agents/.env-source.lock").exists()
    monkeypatch.setenv("FAKE_NIX_FAIL", "1")
    with pytest.raises(StepFailed, match="cannot fetch app"):
        envsource.ensure_ready(layer, state, stack)


def test_a_stack_without_the_source_option_is_left_alone(make_layer, app_repo, fakes):
    other = dict(PODMAN, id="other", containerfile="")
    layer, state, stack = setup_layer(make_layer, app_repo, BUILD_SOURCE, [WORKTREE, other])
    envsource.ensure_ready(layer, state, stack)
    assert calls(fakes) == []
