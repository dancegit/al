"""A run that starts another again records `replaces`: the runner's --replaces, a same-source retry
after a failure, launch.start(replaces=…), and dispatch.py's read-only --pr-state."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adw_modules import ir, launch, runner, state as state_module

RUNTIME = Path(__file__).resolve().parents[1]
OLD = "a1d0a1d0"


def _layer(make_layer) -> Path:
    return make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                          "portBase": 26400, "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "adws": [{"name": "demo", "agent": "mock", "review": "auto", "environment": ["worktree"],
                  "input": ["cli"], "classes": ["chore"], "trigger": [],
                  "steps": [{"name": "plan", "kind": "run", "command": "plan"}]}],
    })


def _old_run(layer_dir: Path, adw_id: str = OLD, **fields) -> None:
    data = {"adw_id": adw_id, "adw": "demo", "status": "failed", "source": "cli",
            "started": "2026-11-01T10:00:00+00:00", "ended": "2026-11-01T10:05:00+00:00", **fields}
    run = layer_dir / "agents" / adw_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "state.json").write_text(json.dumps(data), encoding="utf-8")


def _state(layer_dir: Path, adw_id: str) -> dict:
    return json.loads((layer_dir / "agents" / adw_id / "state.json").read_text(encoding="utf-8"))


def _events(layer_dir: Path, adw_id: str) -> list[dict]:
    path = layer_dir / "agents" / adw_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_replaces_flag_is_recorded_in_state_and_run_start_event(make_layer):
    layer_dir = _layer(make_layer)
    _old_run(layer_dir)
    assert runner.main("demo", layer_dir, ["tidy the readme", "--adw-id", "b0b0b0b0", "--replaces", OLD]) == 0
    state = _state(layer_dir, "b0b0b0b0")
    assert state["replaces"] == OLD and state["status"] == "passed"
    start, = [e for e in _events(layer_dir, "b0b0b0b0") if e["type"] == "run.start"]
    assert start["replaces"] == OLD
    assert f"replaces:  {OLD}" in (layer_dir / "agents" / "b0b0b0b0" / "run.log").read_text()


@pytest.mark.parametrize("old", ["not-hex!", "abc", "c0ffee99"])
def test_replaces_rejects_bad_or_unknown_id(make_layer, capsys, old):
    layer_dir = _layer(make_layer)
    assert runner.main("demo", layer_dir, ["tidy", "--adw-id", "b0b0b0b1", "--replaces", old]) == 2
    assert not (layer_dir / "agents" / "b0b0b0b1").exists()
    assert "--replaces" in capsys.readouterr().err


def test_empty_replaces_is_left_out_of_state_json(tmp_path):
    state = state_module.ADWState(adw_id="b0b0b0b2", adw="demo")
    state.save(tmp_path)
    assert "replaces" not in _state(tmp_path, "b0b0b0b2")
    state.replaces = OLD
    state.save(tmp_path)
    assert _state(tmp_path, "b0b0b0b2")["replaces"] == OLD
    assert state_module.load(tmp_path, "b0b0b0b2").replaces == OLD


def test_same_source_after_a_failure_records_replaces(make_layer, monkeypatch):
    layer_dir = _layer(make_layer)
    source = "tasklist:3"
    _old_run(layer_dir, "f0000001", source=source, started="2026-11-01T09:00:00+00:00")
    _old_run(layer_dir, OLD, source=source)
    _old_run(layer_dir, "f0000002", source="tasklist:4")   # another task
    _old_run(layer_dir, "f0000003", source=source, status="passed", started="2026-11-02T09:00:00+00:00")
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _old_run(layer_dir, "f0000004", source="tasklist:9", status="running", heartbeat=stale)
    request = {"source": source}

    def resolve(layer, adw, args):
        return runner.Request(prompt="tidy the readme", source=request["source"], klass=None, model_set=None,
                              hooks=None)

    monkeypatch.setattr(runner, "resolve_request", resolve)
    layer = ir.load(layer_dir)
    assert runner.previous_attempt(layer, source, "b0b0b0b3") == OLD            # the newest failure
    assert runner.previous_attempt(layer, "tasklist:9", "b0b0b0b3") == "f0000004"   # died: its heartbeat stopped
    assert runner.previous_attempt(layer, "cli", "b0b0b0b3") == ""
    assert runner.main("demo", layer_dir, ["--adw-id", "b0b0b0b3"]) == 0
    assert _state(layer_dir, "b0b0b0b3")["replaces"] == OLD
    # OLD is replaced now; the earlier failure of the same task is next, never OLD twice
    assert runner.previous_attempt(ir.load(layer_dir), source, "b0b0b0b4") == "f0000001"
    assert runner.main("demo", layer_dir, ["--adw-id", "b0b0b0b4"]) == 0
    assert _state(layer_dir, "b0b0b0b4")["replaces"] == "f0000001"
    assert runner.main("demo", layer_dir, ["--adw-id", "b0b0b0b5"]) == 0
    assert "replaces" not in _state(layer_dir, "b0b0b0b5")   # every failure of the task is replaced
    _old_run(layer_dir, "f0000005", source="cli")
    request["source"] = "cli"
    assert runner.main("demo", layer_dir, ["--adw-id", "b0b0b0b6"]) == 0
    assert "replaces" not in _state(layer_dir, "b0b0b0b6")   # a cli source never links itself


class Spawn:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return type("Proc", (), {"pid": 1, "wait": lambda self: 0})()


def test_launch_start_passes_replaces_and_skips_duplicate_confirmation(make_layer):
    layer_dir = _layer(make_layer)
    layer = ir.load(layer_dir)
    spawn = Spawn()
    target = launch.Target("workflow", "demo")
    first = launch.start(layer, target, "tidy the readme", request_key="key-0001-abcdef", spawn=spawn)
    assert first["status"] == "started"
    old = first["run_id"]
    again = launch.start(layer, target, "tidy the readme", request_key="again-" + old, spawn=spawn, replaces=old)
    assert again["status"] == "started", again
    argv = spawn.calls[-1]
    assert argv[argv.index("--replaces") + 1] == old
    assert _state(layer_dir, again["run_id"])["replaces"] == old
    other = launch.start(layer, target, "tidy the readme", request_key="key-0003-abcdef", spawn=spawn)
    assert other["status"] == "confirm-duplicate"   # without replaces the duplicate still asks
    command = launch.start(layer, launch.Target("command", "plan"), "x", request_key="key-0004-abcdef",
                           spawn=spawn, replaces=old)
    assert command["status"] == "started" and spawn.calls[-1][-2:] == ["--replaces", old]
    for bad in ("XYZ", "c0ffee99"):
        refused = launch.start(layer, target, "y", request_key="key-0005-abcdef", spawn=spawn, replaces=bad)
        assert refused["status"] == "invalid" and "replaces" in refused["error"]


class FakeClient:
    def __init__(self, answer):
        self.answer = answer

    def pr_state(self, number):
        if isinstance(self.answer, Exception):
            raise self.answer
        return {**self.answer, "number_asked": number}


def test_dispatch_pr_state_prints_one_json_line(make_layer, monkeypatch):
    layer_dir = _layer(make_layer)
    spec = importlib.util.spec_from_file_location("dispatch_under_test", RUNTIME / "dispatch.py")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    monkeypatch.setattr(dispatch, "LAYER", layer_dir)
    from adw_modules import forge

    def run(answer) -> tuple[int, list[str]]:
        monkeypatch.setattr(forge, "get_client", lambda layer: FakeClient(answer))
        monkeypatch.setattr(sys, "argv", ["dispatch.py", "--pr-state", "7", "--json"])
        out = io.StringIO()
        with redirect_stdout(out):
            code = dispatch.main()
        return code, out.getvalue().splitlines()

    code, lines = run({"state": "merged", "commit": "abc1234", "at": "2026-11-01T10:00:00Z"})
    assert code == 0 and len(lines) == 1
    assert json.loads(lines[0]) == {"state": "merged", "commit": "abc1234", "at": "2026-11-01T10:00:00Z",
                                    "number_asked": 7}
    code, lines = run(forge.ForgeError("HTTP 500"))
    answer = json.loads(lines[0])
    assert code == 1 and len(lines) == 1 and answer["state"] == "unknown" and "HTTP 500" in answer["error"]
