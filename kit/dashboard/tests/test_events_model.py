"""The dashboard on runs that carry events.jsonl, liveness and fan-out parents (fixtures_events.py).
Old runs without these keep their mtime-based reading (test_model.py)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import dashboard
import fixtures
import fixtures_events
from conftest import SSEReader, start_server, stop_server

T0 = fixtures.T0.timestamp()


@pytest.fixture()
def ev(tmp_path: Path) -> dict[str, Path]:
    return fixtures_events.build(tmp_path)


def model_for(layer: Path, **kwargs) -> dashboard.Model:
    model = dashboard.Model([layer], [], **kwargs)
    model.discover()
    model.tick()
    return model


def detail(model: dashboard.Model, run: str) -> dict:
    return model.run_detail("events-shop", run)


def bar(data: dict, bar_id: str) -> dict:
    return next(b for b in data["timeline"]["bars"] if b["id"] == bar_id)


# ---------------------------------------------------------------- exact spans from events

def test_agent_and_check_spans_come_from_events_not_mtimes(ev):
    data = detail(model_for(ev["layer"]), "e1000001")
    assert data["summary"]["timing"] == "events"
    build = bar(data, "agent:build")
    assert (build["start"], build["end"]) == (T0 + 41.5, T0 + 89.5)
    lint1, lint2 = bar(data, "check:lint.1.log"), bar(data, "check:lint.2.log")
    assert (lint1["start"], lint1["end"], lint1["exit"], lint1["status"]) == (T0 + 91, T0 + 95, 1, "failed")
    assert (lint2["start"], lint2["end"], lint2["exit"], lint2["status"]) == (T0 + 113, T0 + 116, 0, "passed")
    resolve = bar(data, "agent:lint-resolve-1")
    assert resolve["step"] == "lint" and resolve["start"] == T0 + 95.5
    gate = [b for b in data["timeline"]["bars"] if b["kind"] == "gate"]
    assert [(b["label"], b["start"], b["end"], b["step"]) for b in gate] == [
        ("gate lint", T0 + 151, T0 + 153, "ship"), ("gate test", T0 + 153.5, T0 + 157, "ship")]
    assert data["checks"]["lint"][0]["exit"] == 1 and data["checks"]["lint"][1]["exit"] == 0


def test_slot_waits_are_exact_bars(ev):
    data = detail(model_for(ev["layer"]), "e1000001")
    runs_wait = bar(data, "wait:runs")
    assert (runs_wait["start"], runs_wait["end"], runs_wait["level"]) == (T0, T0 + 12, 0)
    check_wait = next(b for b in data["timeline"]["bars"] if b["kind"] == "wait" and b["level"] == 1)
    assert (check_wait["start"], check_wait["end"], check_wait["step"]) == (T0 + 110.2, T0 + 113, "lint")
    assert data["timeline"]["totals"]["wait"] == pytest.approx(12 + 2.8)


def test_agents_carry_kind_and_model_into_graph_and_cost_table(ev):
    data = detail(model_for(ev["layer"]), "e1000001")
    build = next(a for a in data["agents"] if a["dir"] == "build")
    assert (build["agent_id"], build["agent_kind"], build["model"]) == ("claude", "claude-code", "claude-opus-5-5")
    node = next(n for n in data["graph"]["nodes"] if n["id"] == "build")
    assert node["agent"] == {"id": "claude", "kind": "claude-code", "model": "claude-opus-5-5"}
    lint = next(n for n in data["graph"]["nodes"] if n["id"] == "lint")
    assert lint["agent"]["model"] == "claude-opus-5-5"   # its resolve agent


def test_container_comes_from_events(ev):
    summary = model_for(ev["layer"]).summaries["events-shop/e1000001"]
    assert summary["container"] == {"name": "al-events-shop-e1000001", "state": "removed", "source": "events"}


# ---------------------------------------------------------------- a running check

def test_a_running_check_is_a_live_attempt_with_its_growing_log(ev):
    model = model_for(ev["layer"])
    summary = model.summaries["events-shop/e1000002"]
    assert summary["status"] == "running"
    assert summary["current"]["step"] == "test" and summary["current"]["action"] == "running check test"
    data = detail(model, "e1000002")
    live = bar(data, "check:test.1.log")
    assert live["status"] == "running" and live["end"] is None
    attempt = data["checks"]["test"][0]
    assert attempt["file"] == "test.1.log" and attempt["running"] is True and attempt["exit"] is None


def test_the_tail_endpoint_follows_a_running_check_log(ev):
    running = start_server(["--layer", str(ev["layer"])])
    try:
        reader = SSEReader(running.port, "/api/tail?layer=events-shop&run=e1000002&what=check&file=test.1.log")
        first, _ = reader.wait_for("text")
        assert "collecting" in "".join(seg[0] for seg in json.loads(first)["segments"])
        log = ev["agents"] / "e1000002" / "checks" / "test.1.log"
        with log.open("a") as handle:
            handle.write(". [100%]\n3 passed in 0.12s\n")
        later, _ = reader.wait_for("text", lambda d: "3 passed" in d)
        assert "3 passed" in "".join(seg[0] for seg in json.loads(later)["segments"])
        reader.close()
    finally:
        stop_server(running)


# ---------------------------------------------------------------- liveness

def test_a_run_whose_heartbeat_is_stale_and_pid_gone_has_died(ev):
    model = model_for(ev["layer"])
    died = model.summaries["events-shop/e1000003"]
    assert died["status"] == "died" and died["current"] is None
    assert died["last_seen"] == pytest.approx(time.time() - 300, abs=30)
    assert "events-shop/e1000003" not in [r["key"] for r in model.snapshot()["runs"] if r["status"] == "running"]
    alive = model.summaries["events-shop/e1000002"]
    assert alive["status"] == "running" and alive["last_seen"] == pytest.approx(time.time() - 1, abs=30)
    agent = next(a for a in detail(model, "e1000003")["agents"] if a["dir"] == "build")
    assert agent["status"] != "running"


def test_liveness_rules():
    now = 10_000.0
    here = dashboard.socket.gethostname()
    gone = fixtures_events.dead_pid()
    alive = dashboard.os.getpid()
    rule = dashboard.liveness
    assert rule({"status": "passed"}, now) is None                       # finished: nothing to say
    assert rule({"status": "running"}, now) is None                      # an old run without a heartbeat
    fresh = {"status": "running", "heartbeat": "1970-01-01T02:46:35+00:00", "pid": gone, "host": here}  # 5 s ago
    assert rule(fresh, now) == {"died": False, "last_seen": 9995.0}
    stale = dict(fresh, heartbeat="1970-01-01T02:45:00+00:00")           # 100 s ago, pid gone
    assert rule(stale, now) == {"died": True, "last_seen": 9900.0}
    assert rule(dict(stale, pid=alive), now)["died"] is False             # pid alive: just slow
    elsewhere = dict(stale, host="another-host")                          # cannot see its pid
    assert rule(elsewhere, now)["died"] is False
    assert rule(dict(elsewhere, heartbeat="1970-01-01T02:30:00+00:00"), now)["died"] is True   # 1000 s
    assert rule(stale, now, can_see_pids=False)["died"] is False          # in a container: time only


def test_a_run_that_stops_beating_is_republished_as_died(ev, monkeypatch):
    run = ev["agents"] / "e1000002" / "state.json"
    data = json.loads(run.read_text())
    data["pid"] = fixtures_events.dead_pid()
    run.write_text(json.dumps(data))
    monkeypatch.setattr(dashboard, "HEARTBEAT_STALE", 0.5)
    model = model_for(ev["layer"])
    assert model.summaries["events-shop/e1000002"]["status"] == "died"   # its heartbeat is 1 s old > 0.5
    data["heartbeat"] = dashboard.datetime.now(dashboard.timezone.utc).isoformat()
    run.write_text(json.dumps(data))
    model.checked.clear()
    model.tick()
    assert model.summaries["events-shop/e1000002"]["status"] == "running"
    queue = model.hub.subscribe()
    time.sleep(0.7)
    model.checked.clear()
    model.tick()
    published = []
    while not queue.empty():
        kind, payload, _serial = queue.get_nowait()
        if kind == "run":
            published.append(json.loads(payload))
    assert any(p["key"] == "events-shop/e1000002" and p["status"] == "died" for p in published)


# ---------------------------------------------------------------- fan-out parent

def test_a_candidate_names_its_parent_from_its_own_state(ev):
    model = model_for(ev["layer"])
    summary = model.snapshot()["runs"]
    candidate = next(r for r in summary if r["id"] == "e1000004")
    assert candidate["fanout_parent"] == "e1000099"
    assert candidate["fanout"] == {"run": "e1000099", "step": "race", "index": 2}


# ---------------------------------------------------------------- fallback

def test_runs_without_events_still_use_file_times(fx):
    model = dashboard.Model([fx["layer"]], [])
    model.discover()
    model.tick()
    data = model.run_detail("demo-shop", "a1000003")
    assert data["summary"]["timing"] == "files"
    assert data["summary"]["last_seen"] is None


# ---------------------------------------------------------------- paths a person reads

@pytest.mark.parametrize("in_repo", [False, True])
def test_paths_the_runtime_recorded_are_shown_relative_not_absolute(tmp_path: Path, in_repo: bool):
    """The plan path is recorded as an absolute path of the machine; the page shows it relative to
    the layer, or to the repository when the layer is checked in with it."""
    repo = tmp_path / "repo"
    layer = repo / "agentic" if in_repo else tmp_path / "shop.al"
    ir = dict(fixtures.IR, name="shop", layer={"kind": "in-repo", "dir": "agentic"} if in_repo else {"kind": "sibling"},
              codebase={**fixtures.IR["codebase"], "root": ".." if in_repo else str(repo)})
    fixtures.write(layer / "agentic-layer.json", json.dumps(ir))
    st = fixtures.state("b1000001", "zte", "Fix it", "passed", 0, 10, [fixtures.rec("classify", "run", "passed", 0, 10)],
                        plan=str(layer.resolve() / "specs" / "plan-b1000001.md"),
                        worktree=str(layer.resolve() / "trees" / "b1000001"))
    fixtures.write(layer / "agents" / "b1000001" / "state.json", json.dumps(st))
    model = dashboard.Model([layer], [])
    model.discover()
    model.tick()
    plan = model.run_detail("shop", "b1000001")["state"]["plan"]
    assert str(tmp_path) not in plan
    assert plan == ("agentic/specs/plan-b1000001.md" if in_repo else "layer:specs/plan-b1000001.md")
