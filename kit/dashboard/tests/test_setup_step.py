"""The prepare step (`(environment worktree #:prepare (install))`, or a dev container's
postCreateCommand) is drawn as a leading `setup` node, comes first on the timeline, and its log is
served like a script step's. A layer and one failed run are written to tmp_path; the shapes are
those of kit/runtime/adw_modules/prepare.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import dashboard as d
from conftest import start_server, stop_server
from fixtures import iso, rec, state

NAME = "prep-shop"
RUN = "5e7a0001"
WORKTREE = {"id": "worktree", "kind": "worktree", "prepare": ["install"]}
ADW = {"name": "check", "agent": "mock", "classes": [], "environment": ["worktree"], "input": ["cli"],
       "review": "human", "trigger": ["manual"],
       "steps": [{"kind": "check", "name": "test", "cmd": "test", "resolve": "", "retries": 0, "collect": []}]}
IR = {"schemaVersion": 1, "name": NAME, "description": "A layer whose runs prepare first",
      "codebase": {"root": "../shop", "defaultBranch": "main",
                   "commands": [{"name": "install", "run": "uv sync --locked"}, {"name": "test", "run": "pytest"}]},
      "agents": [{"id": "mock", "kind": "mock"}], "reviews": [{"id": "human", "kind": "human"}],
      "environments": [WORKTREE], "adws": [ADW]}
T0 = 2_000.0
LOG = f"agents/{RUN}/scripts/prepare.1.log"


def write_layer(root: Path) -> Path:
    """The layer with one failed-prepare run, written under root; returns the layer path."""
    layer = root / f"{NAME}.al"
    run = layer / "agents" / RUN
    (run / "scripts").mkdir(parents=True)
    (layer / "agentic-layer.json").write_text(json.dumps(IR))
    (run / "scripts" / "prepare.1.log").write_text("Resolved 12 packages\nerror: no matching distribution for nope==9\n")
    records = [rec("prepare", "setup", "failed", T0 + 1, T0 + 8, f"prepare install exited 1; log {LOG}")]
    data = state(RUN, "check", "go", "failed", T0, T0 + 9, records,
                 scripts={"prepare": {"cmd": "install", "exit": 1, "log": LOG,
                                      "commands": [{"cmd": "install", "run": "uv sync --locked", "exit": 1, "log": LOG}]}})
    (run / "state.json").write_text(json.dumps(data))
    (run / "run.log").write_text("")
    return layer


@pytest.fixture
def layer(tmp_path: Path) -> Path:
    return write_layer(tmp_path)


def _detail(layer: Path) -> dict:
    model = d.Model([layer], [])
    model.discover()
    return model.run_detail(NAME, RUN)


def test_graph_starts_with_the_setup_node():
    graph = d.build_graph(ADW, IR)
    first = graph["nodes"][0]
    assert {k: first[k] for k in ("id", "type", "label", "sub")} == {"id": "prepare", "type": "setup", "label": "prepare",
                                                                     "sub": "install"}
    assert {"from": "prepare", "to": "test", "kind": "flow", "label": ""} in graph["edges"]
    plain = dict(IR, environments=[{"id": "worktree", "kind": "worktree", "prepare": []}])
    assert [n["id"] for n in d.build_graph(ADW, plain)["nodes"]] == ["test"]
    devcontainer = dict(IR, environments=[{"id": "worktree", "kind": "worktree", "prepare": []},
                                          {"id": "box", "kind": "podman", "devcontainer": ".devcontainer.json"}])
    node = d.build_graph(dict(ADW, environment=["worktree", "box"]), devcontainer)["nodes"][0]
    assert (node["type"], node["sub"]) == ("setup", "postCreateCommand")


def test_setup_step_comes_first_in_the_timeline(layer):
    detail = _detail(layer)
    steps = [b for b in detail["timeline"]["bars"] if b["id"].startswith("step:")]
    assert (steps[0]["step"], steps[0]["kind"], steps[0]["status"]) == ("prepare", "setup", "failed")
    node = detail["graph"]["nodes"][0]
    assert node["id"] == "prepare" and node["status"] == "failed"


def test_failed_setup_shows_its_log(layer):
    detail = _detail(layer)
    assert detail["scripts"]["prepare"]["log"] == LOG
    assert "exited 1" in detail["steps_detail"]["prepare"]["detail"]
    running = start_server(["--layer", str(layer)])
    try:
        status, _headers, body = running.get(f"/files/{NAME}/{LOG}")
        assert status == 200 and b"no matching distribution for nope==9" in body
    finally:
        stop_server(running)
