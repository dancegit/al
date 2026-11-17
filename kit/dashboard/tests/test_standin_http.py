"""The stand-in's three writes (POST /api/decider, /api/decision, /api/standin), the undo of a
settlement and the loop, on real layers (start_layer: the real runtime copied in, the mock agent,
real `uv run` of the layer's scripts). The layer is start_layer's `bare` (no forge: a stopped run is
handed over with PR.md); `old` is one generated before the stand-in. No browser."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import dashboard as d
import standin_fixtures as sf
from conftest import start_server, stop_server
from standin_fixtures import post
from test_start import key, wait_state

RUN, OTHER = "5a0d1e01", "5a0d1e02"
SUGGEST_FILE = b'{\n  "mode": "suggest",\n  "version": 1\n}\n'   # what `al decider LAYER --set suggest` writes


def serve(site: dict[str, Path], *flags: str):
    return start_server(["--layer", str(site["bare"]), "--layer", str(site["old"]), *flags])


@pytest.fixture()
def notes(start_site, monkeypatch) -> dict[str, Path]:
    monkeypatch.delenv("AL_DECIDER", raising=False)
    sf.stops(start_site["bare"])
    return start_site


def setting(site: dict[str, Path]) -> Path:
    return site["bare"] / d.DECIDER_FILE


def start_stops(running, prompt: str = "Add a note about the orchard", **extra) -> str:
    status, data = post(running, {"layer": "bare", "target": {"kind": "workflow", "name": "stops"}, "prompt": prompt,
                                  "request_key": key(), **extra}, path="/api/start")
    assert status == 202, data
    return data["run_id"]


def log_of(layer: Path) -> list[dict]:
    return d.read_decisions(layer / d.STANDIN_LOG, limit=None)


# ---------------------------------------------------------------- the guard

def test_decider_endpoint_needs_every_guard(notes):
    running = serve(notes)
    body = {"layer": "bare", "mode": "suggest"}
    try:
        assert post(running, body, path="/api/decider", X_AL_Dashboard=None)[0] == 403        # no custom header
        assert post(running, body, path="/api/decider", Origin="http://evil.example")[0] == 403
        assert post(running, body, path="/api/decider", Origin=None)[0] == 403
        assert post(running, body, path="/api/decider", Host="evil.example")[0] == 403
        assert post(running, body, path="/api/decider", Content_Type="application/x-www-form-urlencoded")[0] == 415
        assert post(running, {"layer": "bare", "mode": "suggest", "pad": "x" * 5000}, path="/api/decider")[0] == 413
        assert running.get("/api/decider")[0] == 405 and running.get("/api/decision")[0] == 405
        assert running.get("/api/standin")[0] == 405
        for path in ("/api/decision", "/api/standin"):
            assert post(running, {"layer": "bare", "run": RUN, "answer": "go"}, path=path, X_AL_Dashboard=None)[0] == 403
        # the body: unknown keys, an unknown layer, a layer generated before the stand-in, a bad mode
        assert post(running, {"layer": "bare", "mode": "suggest", "extra": 1}, path="/api/decider")[0] == 400
        assert post(running, {"layer": "bare"}, path="/api/decider")[0] == 400
        assert post(running, {"layer": "bare", "mode": "auto", "paused": True}, path="/api/decider")[0] == 400
        assert post(running, {"layer": "nope", "mode": "suggest"}, path="/api/decider")[0] == 404
        status, data = post(running, {"layer": "old", "mode": "suggest"}, path="/api/decider")
        assert status == 409 and data["error"].startswith("generated before the stand-in: run `al gen ")
        status, data = post(running, {"layer": "bare", "mode": "always"}, path="/api/decider")
        assert status == 400 and data["error"] == "mode: one of off, suggest, auto"
        assert not setting(notes).exists()
    finally:
        stop_server(running)
    running = serve(notes, "--token", "t0k")
    try:   # a token server: the token in the URL is not accepted for a write
        assert post(running, body, path="/api/decider?token=t0k")[0] == 401
        assert not setting(notes).exists()
        assert post(running, body, path="/api/decider", Cookie="al_dashboard_token=t0k")[0] == 200
        assert setting(notes).read_bytes() == SUGGEST_FILE
    finally:
        stop_server(running)


def test_decider_endpoint_is_off_under_read_only(notes):
    sf.write_run(notes["bare"], RUN)
    sf.with_suggestion(notes["bare"], RUN)
    running = serve(notes, "--read-only")
    try:
        for path, body in (("/api/decider", {"layer": "bare", "mode": "auto"}),
                           ("/api/decision", {"layer": "bare", "run": RUN, "answer": "go"}),
                           ("/api/standin", {"layer": "bare", "run": RUN}),
                           ("/api/settle", {"layer": "bare", "run": RUN, "undo": True})):
            status, data = post(running, body, path=path)
            assert status == 403 and data["error"] == "the dashboard was started with --read-only", path
        assert not setting(notes).exists()
        view = sf.standin_of(running, "bare")
        assert (view["writable"], view["why"], view["loop"]) == (False, "the dashboard was started with --read-only",
                                                                 "off: --read-only")
        assert sf.state_of(notes["bare"], RUN)["decision"]["status"] == "suggested"
    finally:
        stop_server(running)


def test_switching_writes_the_file_al_decider_writes(notes):
    sf.declare(notes["bare"], "off", may=["resume", "restart", "settle"], never=["promote"])
    running = serve(notes)
    try:
        status, data = post(running, {"layer": "bare", "mode": "suggest"}, path="/api/decider")
        assert status == 200 and data["ok"] is True
        assert setting(notes).read_bytes() == SUGGEST_FILE
        assert (data["standin"]["mode"], data["standin"]["source"], data["standin"]["since"] != "") == ("suggest", "local setting", True)
        assert sf.standin_of(running, "bare")["mode"] == "suggest"
        status, data = post(running, {"layer": "bare", "may": ["resume", "settle"]}, path="/api/decider")
        assert status == 200 and data["standin"]["may"] == ["resume", "settle"]
        assert json.loads(setting(notes).read_text()) == {"version": 1, "mode": "suggest", "may": ["resume", "settle"]}
        for kinds, why in ((["land"], "land is not in the layer's (stand-in … #:may)"),
                           (["promote"], "promote is not in the layer's (stand-in … #:may)"),
                           (["bless"], "may: 'bless' is not a decision kind")):
            status, data = post(running, {"layer": "bare", "may": kinds}, path="/api/decider")
            assert status == 400 and data["error"].startswith(why), data
        status, data = post(running, {"layer": "bare", "paused": True}, path="/api/decider")
        assert status == 200 and data["standin"]["paused"] is True
        assert json.loads(setting(notes).read_text())["paused"] is True
        assert post(running, {"layer": "bare", "paused": False}, path="/api/decider")[1]["standin"]["paused"] is False
        assert post(running, {"layer": "bare", "may": ["resume", "restart", "settle"]}, path="/api/decider")[0] == 200
        status, data = post(running, {"layer": "bare", "reset": True}, path="/api/decider")
        assert status == 200 and (data["standin"]["mode"], data["standin"]["source"]) == ("off", "default")
        assert json.loads(setting(notes).read_text()) == {"version": 1, "may": ["restart", "resume", "settle"]}
        entries = [e for e in d.read_text(notes["bare"] / d.STANDIN_LOG).splitlines()]
        assert len(entries) == 6 and all(json.loads(e)["type"] == "mode" and json.loads(e)["via"] == "dashboard" for e in entries)
    finally:
        stop_server(running)


# ---------------------------------------------------------------- starting with a mode

def test_start_form_decider_reaches_the_run(notes):
    running = serve(notes)
    try:
        catalog = running.json("/api/startable?layer=bare")
        assert catalog["decider"] == {"modes": ["off", "suggest", "auto"], "layer": "off"}
        status, data = post(running, {"layer": "bare", "target": {"kind": "workflow", "name": "plan-build"},
                                      "prompt": "Add a word count", "request_key": key(), "decider": "always"},
                            path="/api/start")
        assert status == 400 and data["error"] == "decider: one of off, suggest, auto"
        status, data = post(running, {"layer": "bare", "target": {"kind": "workflow", "name": "plan-build"},
                                      "prompt": "Add a word count", "request_key": key(), "decider": "suggest"},
                            path="/api/start")
        assert status == 202, data
        state = wait_state(notes["bare"], data["run_id"])
        assert state["decider"] == "suggest" and state["status"] == "passed"
        detail = sf.wait_for(lambda: running.get(f"/api/run?layer=bare&run={data['run_id']}")[0] == 200
                             and running.json(f"/api/run?layer=bare&run={data['run_id']}"), why="the run's page")
        assert detail["standin"] == {"mode": "suggest", "from": "--decider"} and detail["summary"]["decider"] == "suggest"
        plain = post(running, {"layer": "bare", "target": {"kind": "workflow", "name": "plan-build"},
                               "prompt": "Add a footer", "request_key": key()}, path="/api/start")[1]["run_id"]
        assert "decider" not in wait_state(notes["bare"], plain)
    finally:
        stop_server(running)


# ---------------------------------------------------------------- the acceptance flow

def test_suggest_then_go_ahead_resumes(notes):
    layer = notes["bare"]
    ready = layer.parent / "ready"
    sf.mock_decision(layer, "resume", summary="The check waits for the site; run it again once it is ready.")
    running = serve(notes)
    try:
        assert post(running, {"layer": "bare", "mode": "suggest"}, path="/api/decider")[0] == 200
        run = start_stops(running)
        row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", run)) and r.get("suggestion") and r, why="a suggestion")
        s = row["suggestion"]
        assert (s["kind"], s["action"], s["from_step"], s["confidence"]) == ("resume", "resume", "ready", 0.8)
        assert s["summary"] == "The check waits for the site; run it again once it is ready."
        assert row["status"] == "failed" and row["bucket"] == "needs-you"
        assert sf.standin_of(running, "bare")["counts"]["waiting"] == 1
        ready.touch()
        status, data = post(running, {"layer": "bare", "run": run, "answer": "go"}, path="/api/decision")
        assert status == 200 and data["status"] == "resuming" and "ready" in data["message"], data
        state = sf.wait_for(lambda: (st := sf.state_of(layer, run)).get("status") == "passed" and st.get("ended") and st,
                            timeout=180, why="the resumed run passes")
        assert state["decision"]["status"] == "resumed" and state["decision"]["answers"][0]["via"] == "dashboard"
        row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", run)) and r["status"] == "passed" and r)
        assert row["suggestion"] is None and sf.standin_of(running, "bare")["counts"]["waiting"] == 0
        detail = running.json(f"/api/run?layer=bare&run={run}")
        assert [(e["type"], e["who"], e["via"]) for e in detail["decisions"]] == [
            ("decision", "stand-in", "run"), ("answer", "person", "dashboard")]
        assert detail["decisions"][1]["outcome"].startswith("resuming")
    finally:
        stop_server(running)


def test_each_answer(notes):
    layer = notes["bare"]
    for run in (RUN, OTHER, "5a0d1e03"):
        sf.write_run(layer, run)
        sf.with_suggestion(layer, run, action="settle", kind="settle", settle_as="abandoned", from_step="",
                           summary="Nobody needs this any more.")
    running = serve(notes)
    try:
        sf.wait_for(lambda: sf.run_row(running, "bare", RUN))
        answer = lambda run, kind, **more: post(running, {"layer": "bare", "run": run, "answer": kind, **more},  # noqa: E731
                                                path="/api/decision")
        assert answer(RUN, "alter")[0] == 400 and answer(RUN, "alter", text="  ")[0] == 400
        assert answer(RUN, "maybe")[0] == 400 and answer("0badc0de", "go")[0] == 404
        assert answer(RUN, "go", extra=1)[0] == 400
        status, data = answer(RUN, "explain")
        assert status == 200 and data["status"] == "explained"
        status, data = answer(RUN, "alter", text="Dropped: the customer left.")
        assert status == 200 and data == {"status": "applied", "message": "settled as abandoned"}
        record = json.loads((layer / "agents" / RUN / "settlement.json").read_text())
        assert (record["as"], record["note"], record["via"], record["who"]) == (
            "abandoned", "Dropped: the customer left.", "dashboard", "person")
        status, data = answer(RUN, "go")
        assert status == 409 and "no suggestion waiting" in data["error"]
        status, data = answer(OTHER, "reject")
        assert status == 200 and data["status"] == "rejected"
        row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", OTHER)) and r["suggestion"] is None and r)
        assert row["bucket"] == "needs-you" and not (layer / "agents" / OTHER / "settlement.json").exists()
        assert answer(OTHER, "go")[0] == 409
        # an alter on a resume puts the text in the run's prompt
        state = sf.state_of(layer, "5a0d1e03")
        state["decision"].update(action="resume", kind="resume", from_step="ready")
        (layer / "agents" / "5a0d1e03" / "state.json").write_text(json.dumps(state))
        status, data = answer("5a0d1e03", "alter", text="Wait for the orchard first.")
        assert status == 200 and data["status"] == "resuming"
        assert sf.wait_for(lambda: "The person adds: Wait for the orchard first." in sf.state_of(layer, "5a0d1e03")["prompt"])
        assert [e["answer"] for e in log_of(layer) if e["type"] == "answer"] == ["explain", "alter", "reject", "alter"]
    finally:
        stop_server(running)
    running = serve(notes, "--no-start")
    try:   # a go-ahead starts or resumes work: off where starting work is
        sf.write_run(layer, "5a0d1e04")
        sf.with_suggestion(layer, "5a0d1e04")
        sf.wait_for(lambda: sf.run_row(running, "bare", "5a0d1e04"))
        status, data = post(running, {"layer": "bare", "run": "5a0d1e04", "answer": "go"}, path="/api/decision")
        assert status == 403 and "--no-start" in data["error"]
        assert post(running, {"layer": "bare", "apply": ["5a0d1e04"]}, path="/api/standin")[0] == 403
        assert post(running, {"layer": "bare", "run": "5a0d1e04", "answer": "reject"}, path="/api/decision")[0] == 200
    finally:
        stop_server(running)


# ---------------------------------------------------------------- ask the stand-in

def test_ask_the_stand_in_on_an_old_hand_over_changes_nothing_else(notes):
    layer = notes["bare"]
    sf.write_run(layer, RUN, ended=sf.ago(86400))
    sf.write_run(layer, OTHER, status="passed", ship={})
    sf.mock_decision(layer, "resume")
    before = sf.state_of(layer, RUN)
    head = subprocess.run(["git", "-C", str(notes["app"]), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    running = serve(notes)
    try:
        sf.wait_for(lambda: sf.run_row(running, "bare", RUN) and sf.run_row(running, "bare", OTHER))
        assert sf.standin_of(running, "bare")["mode"] == "off"
        for body in ({"layer": "bare"}, {"layer": "bare", "run": RUN, "runs": [RUN]}, {"layer": "bare", "runs": []},
                     {"layer": "bare", "runs": ["nope"]}, {"layer": "bare", "runs": [RUN] * 201},
                     {"layer": "bare", "run": OTHER}):                # a passed run: nothing to ask
            assert post(running, body, path="/api/standin")[0] == 400, body
        assert post(running, {"layer": "bare", "run": "0badc0de"}, path="/api/standin")[0] == 404
        status, data = post(running, {"layer": "bare", "run": RUN}, path="/api/standin")
        assert status == 202 and data == {"queued": 1}
        row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", RUN)) and r.get("suggestion") and r, why="a suggestion")
        assert (row["suggestion"]["action"], row["suggestion"]["situation"]) == ("resume", "stopped")
        after = sf.state_of(layer, RUN)
        for name in ("status", "steps", "ship", "prompt", "branch", "ended", "checks"):
            assert after[name] == before[name], name
        assert after["decision"]["status"] == "suggested" and after["decision"]["mode"] == "suggest"
        assert not (layer / "agents" / RUN / "settlement.json").exists()
        assert subprocess.run(["git", "-C", str(notes["app"]), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head
        assert not setting(notes).exists() and sf.standin_of(running, "bare")["mode"] == "off"
        (entry,) = [e for e in log_of(layer) if e["type"] == "decision"]
        assert (entry["via"], entry["status"], entry["who"]) == ("asked", "suggested", "stand-in")
    finally:
        stop_server(running)


def test_ask_for_all(notes):
    layer = notes["bare"]
    for run in (RUN, OTHER):
        sf.write_run(layer, run, ended=sf.ago(7200))
    sf.mock_decision(layer, "settle", settle_as="abandoned")
    running = serve(notes)
    try:
        sf.wait_for(lambda: sf.run_row(running, "bare", RUN) and sf.run_row(running, "bare", OTHER))
        status, data = post(running, {"layer": "bare", "runs": [RUN, OTHER]}, path="/api/standin")
        assert status == 202 and data == {"queued": 2}
        for run in (RUN, OTHER):
            row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", run)) and r.get("suggestion") and r)
            assert (row["suggestion"]["action"], row["suggestion"]["settle_as"]) == ("settle", "abandoned")
            assert not (layer / "agents" / run / "settlement.json").exists()
        assert sf.standin_of(running, "bare")["counts"] == {"today": 0, "hour": 0, "waiting": 2}
    finally:
        stop_server(running)


# ---------------------------------------------------------------- the loop

def test_auto_loop_handles_a_new_stop_and_leaves_older_items(notes):
    layer = notes["bare"]
    sf.declare(layer, "off", may=["resume", "settle"])
    sf.mock_decision(layer, "settle", settle_as="abandoned", summary="Nobody waits for this; drop it.")
    sf.write_run(layer, OTHER, ended=sf.ago(3600))                 # stopped before the stand-in was switched on
    running = serve(notes)
    try:
        sf.wait_for(lambda: sf.run_row(running, "bare", OTHER))
        assert post(running, {"layer": "bare", "mode": "auto"}, path="/api/decider")[0] == 200
        run = start_stops(running)
        off = start_stops(running, prompt="Add a second note", decider="off")
        row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", run)) and r.get("bucket") == "settled" and r,
                          timeout=180, why="the stand-in settles the new stop")
        record = json.loads((layer / "agents" / run / "settlement.json").read_text())
        assert (record["as"], record["who"], record["via"]) == ("abandoned", "stand-in", "stand-in")
        assert row["standin_last"]["status"] == "acted" and row["standin_last"]["auto"] is True
        assert row["standin_last"]["outcome"] == "settled as abandoned"
        view = sf.wait_for(lambda: (v := sf.standin_of(running, "bare"))["counts"]["today"] == 1 and v)
        assert view["log"][-1]["run"] == run and view["log"][-1]["status"] == "acted"
        # the older item and the run started with its own `decider: off` stay with a person
        wait_state(layer, off)
        time.sleep(d.STANDIN_EVERY + 3)
        for untouched in (OTHER, off):
            row = sf.run_row(running, "bare", untouched)
            assert row["bucket"] == "needs-you" and row["standin_last"] is None and row["suggestion"] is None, untouched
            assert not (layer / "agents" / untouched / "settlement.json").exists()
        assert sf.standin_of(running, "bare")["counts"]["today"] == 1
    finally:
        stop_server(running)


def test_handle_existing_items_first_suggests_then_applies(notes):
    layer = notes["bare"]
    sf.declare(layer, "off", may=["settle"])
    sf.mock_decision(layer, "settle", settle_as="abandoned")
    for run in (RUN, OTHER):
        sf.write_run(layer, run, ended=sf.ago(3600))
    running = serve(notes)
    try:
        sf.wait_for(lambda: sf.run_row(running, "bare", RUN) and sf.run_row(running, "bare", OTHER))
        assert post(running, {"layer": "bare", "mode": "auto"}, path="/api/decider")[0] == 200
        time.sleep(1.0)
        assert all(sf.run_row(running, "bare", run)["standin_last"] is None for run in (RUN, OTHER))   # older: left alone
        assert post(running, {"layer": "bare", "runs": [RUN, OTHER]}, path="/api/standin")[1] == {"queued": 2}
        for run in (RUN, OTHER):   # first what it would do, and nothing done
            row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", run)) and r.get("suggestion") and r)
            assert row["suggestion"]["kind"] == "settle" and row["bucket"] == "needs-you"
            assert not (layer / "agents" / run / "settlement.json").exists()
        status, data = post(running, {"layer": "bare", "apply": [RUN, OTHER]}, path="/api/standin")
        assert status == 202 and data == {"queued": 2}
        for run in (RUN, OTHER):
            row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", run)) and r.get("bucket") == "settled" and r)
            assert row["standin_last"]["status"] == "acted" and row["suggestion"] is None
        assert sf.standin_of(running, "bare")["counts"]["today"] == 2
    finally:
        stop_server(running)


# ---------------------------------------------------------------- undo and a person's actions

def test_undo_a_settlement_by_the_stand_in(notes):
    layer = notes["bare"]
    sf.write_run(layer, RUN)
    d.write_settlement(layer, RUN, as_="abandoned", note="Nobody waits for this.", who="stand-in", via="stand-in",
                       status="failed")
    running = serve(notes)
    try:
        row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", RUN)) and r.get("bucket") == "settled" and r)
        assert row["settlement"]["via"] == "stand-in"
        assert post(running, {"layer": "bare", "run": RUN, "undo": False}, path="/api/settle")[0] == 400
        assert post(running, {"layer": "bare", "run": RUN, "undo": True, "as": "landed"}, path="/api/settle")[0] == 400
        status, data = post(running, {"layer": "bare", "run": RUN, "undo": True}, path="/api/settle")
        assert status == 200 and data == {"status": "undone", "record": None}
        assert not (layer / "agents" / RUN / "settlement.json").exists()
        row = sf.wait_for(lambda: (r := sf.run_row(running, "bare", RUN)) and r.get("bucket") == "needs-you" and r)
        assert row["settlement"] is None
        assert post(running, {"layer": "bare", "run": RUN, "undo": True}, path="/api/settle")[0] == 404
        assert post(running, {"layer": "bare", "run": "0badc0de", "undo": True}, path="/api/settle")[0] == 404
    finally:
        stop_server(running)


def test_person_actions_are_recorded(notes):
    layer = notes["bare"]
    sf.write_run(layer, RUN, adw="plan-build")
    sf.write_run(layer, OTHER, adw="plan-build")
    (layer / "agents" / OTHER / "request.md").write_text("Add a note about the orchard.\n")
    running = serve(notes)
    try:
        sf.wait_for(lambda: sf.run_row(running, "bare", RUN) and sf.run_row(running, "bare", OTHER))
        # a layer that does not use the stand-in: nothing is written for it
        assert post(running, {"layer": "bare", "run": RUN, "as": "abandoned", "who": "Ada"}, path="/api/settle")[0] == 200
        assert not (layer / "agents" / "stand-in").exists()
        assert post(running, {"layer": "bare", "run": RUN, "undo": True}, path="/api/settle")[0] == 200
        assert post(running, {"layer": "bare", "mode": "suggest"}, path="/api/decider")[0] == 200
        assert post(running, {"layer": "bare", "run": RUN, "as": "abandoned", "who": "Ada"}, path="/api/settle")[0] == 200
        assert post(running, {"layer": "bare", "run": RUN, "undo": True}, path="/api/settle")[0] == 200
        status, data = post(running, {"layer": "bare", "again": OTHER, "request_key": key()}, path="/api/start")
        assert status == 202, data
        found = [e for e in log_of(layer) if e["type"] == "decision"]
        assert [(e["who"], e["via"], e["run"], e["kind"], e["action"], e["status"]) for e in found] == [
            ("person", "dashboard", RUN, "settle", "settle", "acted"),
            ("person", "dashboard", RUN, "settle", "undo", "acted"),
            ("person", "dashboard", OTHER, "restart", "restart", "acted")]
        assert found[0]["outcome"] == "settled as abandoned" and found[2]["outcome"] == f"started {data['run_id']}"
        detail = running.json(f"/api/run?layer=bare&run={RUN}")
        assert [e["action"] for e in detail["decisions"]] == ["settle", "undo"]
        wait_state(layer, data["run_id"])
    finally:
        stop_server(running)


# ---------------------------------------------------------------- the loop without the page

def test_stand_in_only_runs_the_loop_without_a_server(notes):
    layer = notes["bare"]
    sf.declare(layer, "auto", may=["settle"])
    sf.mock_decision(layer, "settle", settle_as="abandoned")
    sf.record(layer, {"id": "dd000001", "at": sf.ago(60), "type": "mode", "who": "person", "via": "cli", "mode": "auto",
                      "set": ["mode"]})
    sf.write_run(layer, RUN, ended=sf.ago(5))
    refused = subprocess.run([sys.executable, str(Path(d.__file__)), "--stand-in-only", "--read-only", "--layer", str(layer)],
                             capture_output=True, text=True, timeout=60)
    assert refused.returncode == 2 and "--stand-in-only" in refused.stderr
    env = {k: v for k, v in os.environ.items() if k not in ("AL_DECIDER", "AL_APP_ROOT")}
    process = subprocess.Popen([sys.executable, str(Path(d.__file__)), "--stand-in-only", "--layer", str(layer),
                                "--poll", "0.2", "--no-podman", "--no-workspace"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        record = sf.wait_for(lambda: (p := layer / "agents" / RUN / "settlement.json").exists() and json.loads(p.read_text()),
                             timeout=120, why="the loop settles the stop")
        assert (record["as"], record["via"]) == ("abandoned", "stand-in")
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0, err
        assert out.splitlines()[0] == "stand-in loop for 1 layer(s)" and "al dashboard" not in out
    finally:
        if process.poll() is None:
            process.kill()
