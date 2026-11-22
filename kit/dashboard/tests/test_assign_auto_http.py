"""POST /api/assign {action: "auto"}: the Automatic switch of "Needs you", on the scenario of
assign_fixtures (real dashboard server, the layer's own adws/assign.py with the mock agent, no
browser): what the request must look like, where it is refused, the setting it writes and the view
the layer publishes, and end to end what the stand-in's loop gives to an agent, what it leaves with
a person and that it gives nothing twice."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

import assign_fixtures as af
import dashboard as d
import integrate_fixtures as fx
from conftest import start_server, stop_server
from test_assign_http import counts, records as _records, run_of, snapshot_when, wait_ended
from test_start import post, run_dirs as _dirs

SETTING = "agents/settings/assign.json"
SWITCH = {"layer": "pantry", "action": "auto"}


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("assign-auto-http") / "s"
    af.build_scenario(root)
    return root


@pytest.fixture()
def layer(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN", "AL_DECIDER"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return root / "pantry.al"


@pytest.fixture()
def running(layer: Path):
    server = start_server(["--layer", str(layer)])
    try:
        yield server
    finally:
        stop_server(server)


def run_dirs(layer: Path) -> list[str]:
    """The run directories under agents/ (agents/settings, which the switch creates, is not one)."""
    return [name for name in _dirs(layer) if name != "settings"]


def records(layer: Path) -> list[str]:
    """The runs with an assign.json (the switch's own setting has the same file name, in agents/settings)."""
    return [name for name in _records(layer) if name != "settings"]


def layer_of(data: dict) -> dict:
    return next(entry for entry in data["layers"] if entry["key"] == "pantry")


def auto_of(server) -> dict:
    return layer_of(server.json("/api/snapshot"))["assign_auto"]


def switch(server, on: bool) -> tuple[int, dict]:
    return post(server, {**SWITCH, "on": on}, "/api/assign")


def test_switch_needs_a_boolean_and_nothing_else(layer, running):
    for body in (SWITCH, {**SWITCH, "on": "yes"}, {**SWITCH, "on": 1}, {**SWITCH, "on": None},
                 {**SWITCH, "on": True, "run": af.REJECTED}, {**SWITCH, "on": True, "note": "x"},
                 {**SWITCH, "on": True, "force": True}, {**SWITCH, "on": True, "confirm": "x" * 22},
                 {"action": "auto", "on": True}):
        status, answer = post(running, body, "/api/assign")
        assert status == 400, (body, answer)
    status, answer = post(running, {**SWITCH, "on": "yes"}, "/api/assign")
    assert answer["error"] == 'expected {"layer", "action": "auto", "on": true|false}; nothing else'
    # `on` belongs to this action only
    for action in ("start", "approve", "bulk"):
        assert post(running, {"layer": "pantry", "action": action, "run": af.REJECTED, "confirm": "x" * 22,
                              "on": True}, "/api/assign")[0] == 400, action
    status, answer = post(running, {**SWITCH, "layer": "nowhere", "on": True}, "/api/assign")
    assert status == 404 and answer["error"] == "unknown layer"
    assert post(running, {**SWITCH, "on": True}, "/api/assign", X_AL_Dashboard=None)[0] == 403
    assert post(running, {**SWITCH, "on": True}, "/api/assign", Origin="http://evil.example")[0] == 403
    assert not (layer / SETTING).exists() and records(layer) == []
    assert auto_of(running)["on"] is False


@pytest.mark.parametrize("flag", ["--read-only", "--no-start"])
def test_switch_is_refused_when_nothing_may_start(layer, flag):
    server = start_server(["--layer", str(layer), flag])
    try:
        view = auto_of(server)
        assert (view["available"], view["on"], view["writable"], view["loop"]) == (True, False, False, f"off: {flag}")
        assert flag in view["why"]
        for on in (True, False):
            status, answer = switch(server, on)
            assert status == (403 if flag == "--read-only" else 409) and flag in answer["error"], answer
        if flag == "--no-start":
            assert answer["error"] == "the dashboard was started with --no-start"
        assert not (layer / SETTING).exists() and records(layer) == []
    finally:
        stop_server(server)


def test_switch_in_a_layer_without_the_script_is_409(tmp_path, monkeypatch):
    monkeypatch.delenv("AL_APP_ROOT", raising=False)
    root = tmp_path / "old"
    af.build_scenario(root, with_assign=False)
    old = root / "pantry.al"
    server = start_server(["--layer", str(old)])
    try:
        view = auto_of(server)
        assert (view["available"], view["writable"]) == (False, False)
        status, answer = switch(server, True)
        assert status == 409 and answer["error"] == "generated before `al assign`: run `al gen pantry`"
        assert not (old / SETTING).exists()
    finally:
        stop_server(server)


def test_switch_writes_the_setting_and_the_layer_says_so(layer):
    server = start_server(["--layer", str(layer), "--no-stand-in"])
    try:
        snapshot_when(server, lambda data: counts(data).get("needs-you") == 9)
        before = run_dirs(layer)
        status, answer = switch(server, True)
        assert status == 200, answer
        assert answer == {"ok": True, "assign_auto": {"available": True, "on": True, "writable": True, "why": "",
                                                      "loop": "off: --no-stand-in", "error": "", "log": []}}
        assert json.loads((layer / SETTING).read_text()) == {"version": 1, "auto": True}
        assert sorted(p.name for p in (layer / SETTING).parent.iterdir() if p.name.startswith(".")) == []
        assert auto_of(server)["on"] is True
        assert switch(server, True)[0] == 200 and auto_of(server)["on"] is True, "on twice is on"
        # a broken file is said, and the switch repairs it
        (layer / SETTING).write_text("{oops")
        broken = snapshot_when(server, lambda data: layer_of(data)["assign_auto"]["error"])
        assert layer_of(broken)["assign_auto"]["on"] is False
        assert switch(server, True)[0] == 200 and auto_of(server) == answer["assign_auto"]
        time.sleep(1.5)                                    # several ticks: this dashboard gives nothing
        status, off = switch(server, False)
        assert status == 200 and off["assign_auto"]["on"] is False
        assert not (layer / SETTING).exists() and auto_of(server)["on"] is False
        assert switch(server, False)[0] == 200, "off twice is off"
        assert run_dirs(layer) == before and records(layer) == [], "the switch itself starts nothing"
    finally:
        stop_server(server)


def test_automatic_gives_what_can_be_given_and_leaves_the_rest(layer, running, monkeypatch):
    monkeypatch.setattr(d, "STANDIN_EVERY", 1.0)           # the server runs in this process: a look a second
    snapshot_when(running, lambda data: counts(data).get("needs-you") == 9)
    for run in (fx.ACCEPTANCE, fx.CLEAN, fx.REJECTED, fx.PROTECTED, fx.CHANGED, af.REJECTED):
        status, answer = post(running, {"layer": "pantry", "run": run, "as": "abandoned", "by": "", "note": "",
                                        "who": "Ada Example"}, "/api/settle")
        assert status == 200, answer
    left = snapshot_when(running, lambda data: counts(data).get("needs-you") == 3)
    waiting = sorted((r for r in left["runs"] if r["layer"] == "pantry" and r.get("bucket") == "needs-you"),
                     key=lambda r: (r.get("ended") or r.get("started") or 0, r["id"]))
    order = [r["id"] for r in waiting]                     # oldest stop first: the order the loop takes them in
    assert sorted(order) == sorted([af.FAILED_CHECK, af.NOTHING, af.PERSON])
    before = run_dirs(layer)
    status, answer = switch(running, True)
    assert status == 200 and answer["assign_auto"]["on"] is True and answer["assign_auto"]["loop"] == "on", answer
    data = snapshot_when(running, lambda data: len(layer_of(data)["assign_auto"]["log"]) >= 3, timeout=120)
    log = layer_of(data)["assign_auto"]["log"]
    assert [entry["run"] for entry in log[:3]] == order, log
    by_run = {entry["run"]: entry for entry in log[:3]}
    # a failed check: resumed in place
    check = by_run[af.FAILED_CHECK]
    assert (check["status"], check["kind"], check["run_id"], check["why"]) == ("started", "check", af.FAILED_CHECK, "")
    assert [(a["by"], a["kind"], a["run"]) for a in af.record_of(layer, af.FAILED_CHECK)["attempts"]] == [
        ("decider", "check", af.FAILED_CHECK)]
    # a run that changed nothing: the request again, in a new run that replaces it
    again = by_run[af.NOTHING]
    new = again["run_id"]
    assert (again["status"], again["kind"]) == ("started", "start-again") and new not in before
    assert [(a["by"], a["kind"], a["run"]) for a in af.record_of(layer, af.NOTHING)["attempts"]] == [
        ("decider", "start-again", new)]
    assert json.loads((layer / "agents" / new / "state.json").read_text())["replaces"] == af.NOTHING
    # what only a person may decide stays, with the runtime's reason
    person = by_run[af.PERSON]
    assert (person["status"], person["kind"], person["run_id"]) == ("skipped", "", "")
    assert person["why"].startswith("needs-a-person: ")
    assert not (layer / "agents" / af.PERSON / "assign.json").exists()
    assert all(isinstance(entry["at"], float) and set(entry) == {"at", "run", "status", "kind", "run_id", "why"}
               for entry in log)
    assert str(layer.parent) not in json.dumps(layer_of(data)), "no path of this machine"
    # nothing is given a second time, whatever the two runs end as
    wait_ended(layer, [af.FAILED_CHECK, new])
    time.sleep(6)                                          # several looks of the loop
    after = running.json("/api/snapshot")
    assert run_dirs(layer) == sorted([*before, new]), "no run besides the one that replaces the unchanged one"
    assert run_of(after, af.PERSON)["bucket"] == "needs-you"
    assert not (layer / "agents" / af.PERSON / "assign.json").exists()
    assert records(layer) == sorted([af.FAILED_CHECK, af.NOTHING])
    for run in records(layer):
        attempts = af.record_of(layer, run)["attempts"]
        assert [a["by"] for a in attempts] == ["decider"], (run, attempts)
    later = layer_of(after)["assign_auto"]["log"]
    assert later[:3] == log[:3] and all(entry["status"] == "skipped" for entry in later[3:]), later
    assert all(entry["run"] == new and entry["why"].startswith("limit: ") and af.NOTHING in entry["why"]
               for entry in later[3:]), later
    status, answer = switch(running, False)
    assert status == 200 and answer["assign_auto"]["on"] is False
    assert not (layer / SETTING).exists()
