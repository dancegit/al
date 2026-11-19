"""Runs placed on hosts (`(placement auto #:hosts …)`): the host of a run in its summary, the new
status `lost`, liveness of a run that executes elsewhere, and the Hosts panel's data (the declared
hosts, the scheduler's last survey in agents/hosts.json, the layer's live runs per host). A layer
without a placement reads as before (fixtures.py: notes.al has one, demo-shop.al has none)."""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

import dashboard
import fixtures
from conftest import SSEReader, start_server, stop_server

REMOTE, LOST, AT_HOME = fixtures.REMOTE, fixtures.LOST, fixtures.AT_HOME


@pytest.fixture()
def hx(tmp_path: Path) -> dict[str, Path]:
    return fixtures.build_hosts(tmp_path)


def model_for(*layers: Path, **kwargs) -> dashboard.Model:
    model = dashboard.Model(list(layers), [], **kwargs)
    model.discover()
    model.tick()
    return model


def entry(model: dashboard.Model, name: str) -> dict:
    return next(layer for layer in model.layer_list() if layer["name"] == name)


def test_summary_names_the_host(hx):
    model = model_for(hx["layer"])
    remote, lost, home = (model.summaries[f"notes/{rid}"] for rid in (REMOTE, LOST, AT_HOME))
    assert (remote["host"], remote["status"], remote["remote"]["phase"]) == ("build-1", "running", "remote")
    assert abs(remote["remote"]["last_seen"] - (time.time() - 2)) < 30          # epoch seconds, like every time
    assert (lost["host"], lost["remote"]["phase"]) == ("build-2", "lost")
    assert lost["remote"]["last_seen"] == fixtures.at(270).timestamp()
    assert (home["host"], home["remote"]) == ("home", None)                    # the layer places runs; this one stayed
    state = json.loads((hx["agents"] / REMOTE / "state.json").read_text())      # anything but a host name is not one
    state["remote"] = {"host": ["<b>"], "phase": 7}
    (hx["agents"] / REMOTE / "state.json").write_text(json.dumps(state))
    model.tick()
    assert (model.summaries[f"notes/{REMOTE}"]["host"], model.summaries[f"notes/{REMOTE}"]["remote"]) == (
        "home", {"phase": "", "last_seen": None})


def test_a_layer_without_placement_has_no_host_and_no_hosts_panel(fx):
    model = model_for(fx["layer"])
    assert model.summaries and all(s["host"] == "" and s["remote"] is None for s in model.summaries.values())
    assert entry(model, "demo-shop")["hosts"] == []
    assert dashboard.Model.layer_hosts(model.layers["demo-shop"], list(model.summaries.values())) == []
    (fx["agents"] / "hosts.json").write_text(json.dumps(fixtures.hosts_survey(time.time())))   # a stray survey shows nothing
    assert entry(model, "demo-shop")["hosts"] == []


def test_lost_is_a_finished_status(hx):
    model = model_for(hx["layer"])
    lost = model.summaries[f"notes/{LOST}"]
    assert lost["status"] == "lost" and lost["ended"] == fixtures.at(400).timestamp() and lost["duration"] == 300
    stats = dashboard.aggregate(list(model.summaries.values()))
    assert (stats["count"], stats["finished"], stats["passed"], stats["failed"], stats["lost"], stats["running"]) == (3, 2, 1, 0, 1, 1)
    assert stats["pass_rate"] == 0.5
    assert dashboard.aggregate([{"status": "passed"}])["lost"] == 0
    # the 4-second rule: a finished run is not read again at once, a running one is
    state = hx["agents"] / LOST / "state.json"
    data = json.loads(state.read_text())
    data["prompt"] = "Changed"
    state.write_text(json.dumps(data))
    model.tick()
    assert model.summaries[f"notes/{LOST}"]["title"] == "List the note tags"
    model.checked.clear()
    model.tick()
    assert model.summaries[f"notes/{LOST}"]["title"] == "Changed"
    assert model.run_detail("notes", LOST)["summary"]["status"] == "lost"


def test_a_remote_run_is_alive_by_heartbeat_only(hx, monkeypatch):
    now = 10_000.0
    gone, here = fixtures.dead_pid(), socket.gethostname()
    stale = {"status": "running", "heartbeat": "1970-01-01T02:45:00+00:00", "pid": gone, "host": here}   # 100 s ago
    remote = dict(stale, remote={"host": "build-1", "phase": "remote"})
    rule = dashboard.liveness
    assert rule(stale, now)["died"] is True                                # a local run: its process is gone
    assert rule(remote, now) == {"died": False, "last_seen": 9900.0}       # the proxy's pid says nothing about the run
    assert rule(dict(remote, heartbeat="1970-01-01T02:30:00+00:00"), now)["died"] is True      # 1000 s without a pull
    gate = dict(stale, remote={"host": "build-1", "phase": "gate"})
    assert rule(gate, now)["died"] is True                                 # back home for the gate: a local run again
    assert rule(dict(remote, remote="build-1"), now)["died"] is True       # not a record: judged like any run
    # the fixture's run: its proxy is gone, its heartbeat is fresh, then 100 s old
    monkeypatch.setattr(dashboard, "HEARTBEAT_STALE", 0.5)
    model = model_for(hx["layer"])
    assert model.summaries[f"notes/{REMOTE}"]["status"] == "running"
    state = hx["agents"] / REMOTE / "state.json"
    data = json.loads(state.read_text())
    data["heartbeat"] = dashboard.datetime.fromtimestamp(time.time() - 100, dashboard.timezone.utc).isoformat()
    state.write_text(json.dumps(data))
    model.tick()
    assert model.summaries[f"notes/{REMOTE}"]["status"] == "running"
    data["remote"]["phase"] = "gate"
    state.write_text(json.dumps(data))
    model.tick()
    assert model.summaries[f"notes/{REMOTE}"]["status"] == "died"


def test_hosts_panel_merges_the_survey_with_live_runs(hx):
    model = model_for(hx["layer"])
    hosts = entry(model, "notes")["hosts"]
    assert [h["host"] for h in hosts] == ["home", "build-1", "build-2"]     # as declared
    home, one, two = hosts
    assert (home["state"], home["runs"], home["max_runs"], home["reported_runs"], home["pending"]) == ("ok", 0, 2, 0, 0)
    assert (home["cpus"], home["load1"], home["mem_total_kb"], home["mem_available_kb"], home["disk_free_kb"]) == (
        8, 0.42, 16_000_000, 9_000_000, 120_000_000)
    assert home["image"] is True and home["logins"] == {"claude-code": "own login", "mock": "not needed"}
    assert (one["state"], one["runs"], one["reported_runs"], one["pending"], one["image"]) == ("ok", 1, 1, 1, False)
    assert one["logins"]["claude-code"] == "token ANTHROPIC_API_KEY in its env file"
    assert (two["state"], two["silent"], two["runs"], two["max_runs"]) == ("silent", "no answer within 10 s", 0, 2)
    assert two["load1"] is None and two["logins"] == {} and two["image"] is None and two["reported_runs"] is None
    survey = dashboard.parse_time(json.loads(hx["hosts"].read_text())["at"])
    assert {h["at"] for h in hosts} == {survey}                             # the load facts are as old as the survey
    # a run that came home for the gate counts at home; a lost or finished one nowhere
    state = hx["agents"] / REMOTE / "state.json"
    data = json.loads(state.read_text())
    data["remote"]["phase"] = "gate"
    data["pid"] = 0
    state.write_text(json.dumps(data))
    model.tick()
    assert [(h["host"], h["runs"]) for h in entry(model, "notes")["hosts"]] == [("home", 1), ("build-1", 0), ("build-2", 0)]
    assert model.summaries[f"notes/{REMOTE}"]["host"] == "build-1"          # the badge still says where it ran
    # no survey yet, and a file that is not a survey: the declared hosts, nothing known about them
    for content in (None, "[1, 2", json.dumps(["x"]), json.dumps({"at": 5, "hosts": {"home": 7, "build-1": {"runs": "<b>", "logins": [1]}},
                                                                  "pending": "soon"})):
        hx["hosts"].unlink(missing_ok=True)
        if content is not None:
            hx["hosts"].write_text(content)
        listed = entry(model, "notes")["hosts"]
        assert [(h["host"], h["state"], h["at"], h["load1"], h["reported_runs"], h["max_runs"], h["logins"], h["pending"])
                for h in listed] == [(name, "unknown", None, None, None, 2, {}, 0) for name in fixtures.HOSTS], content


def test_a_changed_hosts_file_publishes_layers(hx):
    running = start_server(["--layer", str(hx["layer"])])
    try:
        reader = SSEReader(running.port, "/api/events")
        try:
            snapshot, _id = reader.wait_for("snapshot")
            listed = next(layer for layer in json.loads(snapshot)["layers"] if layer["name"] == "notes")
            assert [h["state"] for h in listed["hosts"]] == ["ok", "ok", "silent"]
            time.sleep(0.5)                                  # the watcher has seen the file as it is
            survey = fixtures.hosts_survey(time.time(), age=0)
            survey["hosts"]["build-2"] = fixtures.host_load(1, 3.5, 2_000_000)
            temporary = hx["hosts"].with_suffix(".tmp")     # as the scheduler writes it: a temporary file, then replace
            temporary.write_text(json.dumps(survey))
            temporary.replace(hx["hosts"])

            def answered(payload: str) -> bool:
                hosts = next(layer for layer in json.loads(payload) if layer["name"] == "notes")["hosts"]
                return hosts[2]["state"] == "ok" and hosts[2]["load1"] == 3.5
            reader.wait_for("layers", answered)
            hx["hosts"].unlink()                             # and when it goes away

            def unknown(payload: str) -> bool:
                hosts = next(layer for layer in json.loads(payload) if layer["name"] == "notes")["hosts"]
                return [h["state"] for h in hosts] == ["unknown"] * 3
            reader.wait_for("layers", unknown)
        finally:
            reader.close()
    finally:
        stop_server(running)


def test_run_detail_exposes_remote(hx, fx):
    model = model_for(hx["layer"], fx["layer"])
    remote = model.run_detail("notes", REMOTE)["state"]["remote"]
    assert (remote["host"], remote["phase"], remote["status"], remote["pushed"]["step"]) == ("build-1", "remote", "running", "answer")
    lost = model.run_detail("notes", LOST)
    assert lost["state"]["remote"]["lost"] == {"at": fixtures.iso(400), "reason": "no answer from build-2 for 120 s", "host": "build-2"}
    assert (lost["summary"]["host"], lost["summary"]["remote"]["phase"]) == ("build-2", "lost")
    assert model.run_detail("notes", AT_HOME)["state"]["remote"] is None
    assert model.run_detail("demo-shop", "a1000001")["state"]["remote"] is None
