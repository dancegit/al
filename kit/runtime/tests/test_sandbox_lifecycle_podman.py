"""The sandbox lifecycle against REAL rootless podman: a changed recipe is built at the next run with
no manual step, an unchanged one is not, and the doctor / preflight name what the image lacks.

Skipped cleanly when podman or a local base image is missing; nothing is pulled: every layer's
env/Containerfile is `FROM <the sandbox_image of test_sandbox_podman.py>` plus one RUN line.
With AL_SANDBOX_PROOF=<dir> the doctor's table and the rebuild's events are written there."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path

import pytest

from adw_modules import events, sandbox
from adw_modules.ir import load
from adw_modules.runner import main
from test_sandbox_podman import _podman, sandbox_image  # noqa: F401 — the fixture is reused

pytestmark = pytest.mark.podman


def _options() -> list[dict]:
    return [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 25100, "portsPerRun": 0,
             "keep": "never", "status": "stable"},
            {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "local", "cpus": "1", "memory": "1g",
             "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
             "credentials": []}]


def _layer(make_layer, test_cmd: str = "git --version") -> Path:
    return make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"commands": [{"name": "test", "timeout": 120, "run": test_cmd}]},
        "environments": _options(),
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False}],
        "adws": [{"name": "demo", "agent": "mock", "review": "human", "environment": ["wt", "box"],
                  "classes": ["chore"], "steps": [{"name": "test", "kind": "check", "cmd": "test"}]}],
    })


def _recipe(layer_dir: Path, base: str, line: str) -> None:
    (layer_dir / "env").mkdir(exist_ok=True)
    (layer_dir / "env" / "Containerfile").write_text(f"FROM {base}\nRUN {line}\n", encoding="utf-8")


@pytest.fixture
def cleanup():
    """Every image of the layers a test built is removed afterwards (and :latest, which they share)."""
    layers: list = []
    yield layers.append
    for layer in layers:
        for row in sandbox.layer_images(layer):
            for ref in row["refs"]:
                _podman("rmi", ref)
    _podman("rmi", "al-test-layer:latest")


def _proof(name: str, text: str) -> None:
    target = os.environ.get("AL_SANDBOX_PROOF")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / name).write_text(text, encoding="utf-8")


def test_changed_recipe_rebuilds_and_unchanged_does_not(make_layer, sandbox_image, cleanup) -> None:
    layer_dir = _layer(make_layer)
    token = secrets.token_hex(4)
    _recipe(layer_dir, sandbox_image, f"echo one-{token} > /tmp/recipe")
    layer = load(layer_dir)
    cleanup(layer)
    first = sandbox.ensure_image(layer, say=lambda line: None)
    assert first["built"] and sandbox.image_exists(first["image"])
    assert sandbox.ensure_image(layer, say=lambda line: None)["built"] is False
    _recipe(layer_dir, sandbox_image, f"echo two-{token} > /tmp/recipe")
    second = sandbox.ensure_image(layer, say=lambda line: None)
    assert second["built"] and second["image"] != first["image"]
    assert sandbox.image_exists(first["image"])                       # kept: one of the KEEP_OLD newest
    assert sandbox.image_info("al-test-layer:latest")["Id"] == sandbox.image_info(second["image"])["Id"]
    assert sandbox.prune_images(layer) == [first["image"]]
    assert not sandbox.image_exists(first["image"]) and sandbox.image_exists(second["image"])


def test_a_run_picks_up_a_changed_recipe_without_a_manual_step(make_layer, sandbox_image, cleanup) -> None:
    layer_dir = _layer(make_layer)
    token = secrets.token_hex(4)
    _recipe(layer_dir, sandbox_image, f"echo one-{token} > /tmp/recipe")
    cleanup(load(layer_dir))

    def run(adw_id: str) -> list[dict]:
        try:
            assert main("demo", layer_dir, ["Check the sandbox", "--adw-id", adw_id]) == 0, \
                (layer_dir / f"agents/{adw_id}/run.log").read_text()
        finally:
            _podman("rm", "-f", f"al-test-layer-{adw_id}")
        return events.read(layer_dir / "agents" / adw_id / "events.jsonl")

    run("6a6a0001")
    _recipe(layer_dir, sandbox_image, f"echo two-{token} > /tmp/recipe")
    second = run("6a6a0002")
    builds = [e for e in second if e["type"] == "env.build.end"]
    assert [(e["status"], e["recipe"]) for e in builds] == [("passed", sandbox.recipe_hash(load(layer_dir)))]
    state = json.loads((layer_dir / "agents/6a6a0002/state.json").read_text())
    assert state["image"] == builds[0]["image"]
    _proof("rebuild-events.jsonl", "".join(json.dumps(e) + "\n" for e in second
                                           if e["type"].startswith(("env.", "run.", "step."))))
    third = run("6a6a0003")
    assert not [e for e in third if e["type"].startswith("env.build")]


def test_doctor_names_a_missing_command_and_fails(make_layer, sandbox_image, cleanup) -> None:
    layer_dir = _layer(make_layer, "definitely-missing-tool --x")
    _recipe(layer_dir, sandbox_image, f"echo doctor-{secrets.token_hex(4)} > /tmp/recipe")
    layer = load(layer_dir)
    cleanup(layer)
    sandbox.ensure_image(layer, say=lambda line: None)
    adw = layer.adw("demo")
    with sandbox.throwaway(layer, adw["environment"], "doctor") as stack:
        checks = sandbox.doctor(layer, stack, adw)
    table = sandbox.format_table(checks)
    _proof("doctor-missing-command.txt", table + "\n" + sandbox.summary(checks) + "\n")
    rows = {check.name: check for check in checks}
    assert rows["command:test"].status == "miss" and "definitely-missing-tool" in rows["command:test"].detail
    assert "(cmd test" in rows["command:test"].fix
    assert all(rows[name].status == "ok" for name in ("git", "init", "disk", "image")), table
    assert not list((layer_dir / "trees").iterdir())                     # the throwaway worktree is gone
    assert subprocess.run(["podman", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True
                          ).stdout.count("al-test-layer-") == 0


def test_preflight_stops_a_run_whose_image_lacks_a_command(make_layer, sandbox_image, cleanup) -> None:
    layer_dir = _layer(make_layer, "definitely-missing-tool --x")
    _recipe(layer_dir, sandbox_image, f"echo preflight-{secrets.token_hex(4)} > /tmp/recipe")
    cleanup(load(layer_dir))
    adw_id = "6b6b0001"
    try:
        assert main("demo", layer_dir, ["Check the sandbox", "--adw-id", adw_id]) == 1
    finally:
        _podman("rm", "-f", f"al-test-layer-{adw_id}")
    found = events.read(layer_dir / "agents" / adw_id / "events.jsonl")
    assert not [e for e in found if e["type"] == "step.start"]
    (pre,) = [e for e in found if e["type"] == "env.preflight"]
    assert pre["status"] == "failed"
    log = (layer_dir / "agents" / adw_id / "run.log").read_text()
    assert "the sandbox is missing what this workflow needs" in log and "definitely-missing-tool" in log
