"""The prepare step: `(environment worktree #:prepare (install …))` runs its cmds once per run, after
the worktree exists and before the first step, recorded as the step `prepare` of kind `setup`; a
failure ends the run at once with the command's output. Real git, host worktree, mock agent."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from adw_modules import envsource, events, ir
from adw_modules.runner import main

ADW_ID = "7e57ab1e"


def _layer(make_layer, prepare: list[str], cmds: dict[str, str], steps: list[dict] | None = None,
           keep: str = "never") -> Path:
    return make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "codebase": {"commands": [{"name": name, "run": run, "timeout": 30, "description": ""}
                                  for name, run in {"test": "./test.sh", **cmds}.items()]},
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27990,
                          "portsPerRun": 0, "keep": keep, "status": "stable", "prepare": prepare}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "adws": [{"name": "check", "agent": "mock", "review": "human", "environment": ["wt"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [],
                  "steps": steps or [{"name": "test", "kind": "check", "cmd": "test", "resolve": "", "retries": 0,
                                      "collect": []}]}],
    })


def _state(layer: Path) -> dict:
    return json.loads((layer / "agents" / ADW_ID / "state.json").read_text())


def _events(layer: Path) -> list[dict]:
    return events.read(layer / "agents" / ADW_ID / "events.jsonl")


def test_prepare_runs_once_before_the_first_step(make_layer, tmp_path):
    count = tmp_path / "layer" / "count"
    layer = _layer(make_layer, ["install"], {"install": 'echo installing; echo x >> "$ADW_LAYER_DIR/count"'})
    assert main("check", layer, ["go", "--adw-id", ADW_ID]) == 0
    assert count.read_text().splitlines() == ["x"]
    starts = [(e["step"], e.get("kind")) for e in _events(layer) if e["type"] == "step.start"]
    assert starts == [("prepare", "setup"), ("test", "check")]
    data = _state(layer)
    first = data["steps"][0]
    assert {k: first[k] for k in ("name", "kind", "status")} == {"name": "prepare", "kind": "setup", "status": "passed"}
    record = data["scripts"]["prepare"]
    assert record["exit"] == 0 and "installing" in (layer / record["log"]).read_text()
    kinds = [(e["type"], e.get("step")) for e in _events(layer) if e["type"].startswith("script.")]
    assert kinds == [("script.start", "prepare"), ("script.end", "prepare")]


def test_several_prepare_cmds_run_in_order(make_layer, tmp_path):
    order = tmp_path / "layer" / "order"
    layer = _layer(make_layer, ["first", "second"], {"first": 'echo a >> "$ADW_LAYER_DIR/order"',
                                                     "second": 'echo b >> "$ADW_LAYER_DIR/order"'})
    assert main("check", layer, ["go", "--adw-id", ADW_ID]) == 0
    assert order.read_text().splitlines() == ["a", "b"]
    assert [c["cmd"] for c in _state(layer)["scripts"]["prepare"]["commands"]] == ["first", "second"]


def test_resume_does_not_prepare_again(make_layer, tmp_path):
    layer_dir = tmp_path / "layer"
    gate = {"name": "gate", "kind": "check", "cmd": "gate", "resolve": "", "retries": 0, "collect": []}
    layer = _layer(make_layer, ["install"], {"install": 'echo x >> "$ADW_LAYER_DIR/count"',
                                             "gate": f"test -f {layer_dir}/ok"}, steps=[gate], keep="on-failure")
    assert main("check", layer, ["go", "--adw-id", ADW_ID]) == 1
    assert _state(layer)["steps"][0]["status"] == "passed"
    (layer_dir / "ok").write_text("")
    assert main("check", layer, ["--resume", "--adw-id", ADW_ID]) == 0
    assert (layer_dir / "count").read_text().splitlines() == ["x"]


def test_a_failing_prepare_ends_the_run_at_once(make_layer):
    layer = _layer(make_layer, ["install"], {"install": "echo boom; exit 3"})
    assert main("check", layer, ["go", "--adw-id", ADW_ID]) != 0
    types = [(e["type"], e.get("step")) for e in _events(layer)]
    assert ("step.start", "prepare") in types
    assert not [t for t in types if t[0] == "agent.start" or (t[0] == "step.start" and t[1] != "prepare")]
    data = _state(layer)
    assert data["status"] == "failed"
    record = data["steps"][0]
    assert record["name"] == "prepare" and record["status"] == "failed"
    assert "exited 3" in record["detail"] and "boom" in record["detail"]
    assert "test" not in data["checks"]
    assert "boom" in (layer / "agents" / ADW_ID / "run.log").read_text()


def test_devcontainer_post_create_is_the_prepare_step(make_layer):
    layer_dir = _layer(make_layer, [], {})
    (layer_dir / "env").mkdir()
    (layer_dir / "env/source.json").write_text(json.dumps({"version": 1, "nix": None, "podman": {
        "option": "sandbox", "build": None, "image": "x", "publish": [], "prepare": "echo prepared > .prepared",
        "notHonoured": []}}))
    layer = ir.load(layer_dir)
    stack = SimpleNamespace(worktree_option={"prepare": []}, podman_option={"id": "sandbox"})
    assert envsource.prepare_commands(layer, stack) == [("postCreateCommand", "echo prepared > .prepared")]
    other = SimpleNamespace(worktree_option={"prepare": []}, podman_option={"id": "elsewhere"})
    assert envsource.prepare_commands(layer, other) == []
    preparing = SimpleNamespace(worktree_option={"prepare": ["test"]}, podman_option={"id": "sandbox"})
    assert envsource.prepare_commands(layer, preparing) == [("test", "./test.sh")]


def test_no_prepare_no_setup_step(make_layer):
    layer = _layer(make_layer, [], {})
    assert main("check", layer, ["go", "--adw-id", ADW_ID]) == 0
    data = _state(layer)
    assert [s["name"] for s in data["steps"]] == ["test"] and "prepare" not in data["scripts"]
    assert not [e for e in _events(layer) if e.get("step") == "prepare"]
