"""The stand-in in the dashboard's model: the panel's view of a layer (asked from the layer's own
adws/standin.py, never by importing its runtime), the suggestion in a run's summary, the decisions,
the timeline marks and the cost split in its detail, which items the loop hands over, and the undo
of a settlement. No browser; the layers are start_layer's (the real runtime, the mock agent)."""
from __future__ import annotations

import json
from pathlib import Path

import dashboard as d
import standin_fixtures as sf
from conftest import start_server, stop_server

RUN, OLD = "5a0d1e01", "5a0d1e02"


def model_for(*layers: Path, idle: bool = False, **kw) -> d.Model:
    """A model that has read the layers once. `idle`: its stand-in worker ends at once, so what the
    loop queues stays in the queue and nothing is asked."""
    model = d.Model(list(layers), [], **kw)
    if idle:
        model.stop.set()
    model.discover()
    model.tick()
    return model


def view_of(model: d.Model, key: str) -> dict:
    return next(l for l in model.layer_list() if l["key"] == key)["standin"]


def test_layer_list_carries_the_standin_view(start_site, fx):
    notes = start_site["notes"]
    sf.write_run(notes, RUN)
    sf.record(notes, sf.entry(RUN, status="acted", auto=True, outcome="resuming from ready", at=sf.ago(5)),
              sf.entry(OLD, status="suggested"))
    model = model_for(notes, start_site["old"], fx["layer"])
    view = view_of(model, "notes")
    assert (view["available"], view["mode"], view["source"], view["writable"], view["loop"]) == (True, "off", "default", True, "on")
    assert view["may"] == ["resume"] and view["offered"] == ["resume"] and view["never"] == []
    assert view["suggests"] == [k for k in d.STANDIN_KINDS if k != "resume"]
    assert (view["per_run"], view["per_hour"], view["paused"]) == (2, 12, False)
    assert view["counts"] == {"today": 1, "hour": 1, "waiting": 1}
    assert [(e["run"], e["status"]) for e in view["log"]] == [(RUN, "acted"), (OLD, "suggested")]
    assert set(view["kinds"]) == set(d.STANDIN_KINDS)
    # a layer generated before the stand-in: the panel says so
    for key in ("old", "demo-shop"):
        missing = view_of(model, key)
        assert (missing["available"], missing["mode"], missing["writable"]) == (False, "off", False)
        assert missing["why"].startswith("generated before the stand-in: run `al gen ")
    # a declaration and a local setting are read from the layer, and the view follows the file
    sf.declare(notes, "auto", may=["resume", "settle"], never=["promote"], maxActionsPerRun=4)
    (notes / d.DECIDER_FILE).parent.mkdir(parents=True, exist_ok=True)
    (notes / d.DECIDER_FILE).write_text(json.dumps({"version": 1, "mode": "suggest", "may": ["resume"], "paused": True}))
    model.discover()
    model.layers["notes"] = d.load_layer(notes, set())
    view = view_of(model, "notes")
    assert (view["mode"], view["source"], view["paused"], view["per_run"]) == ("suggest", "local setting", True, 4)
    assert view["offered"] == ["resume", "settle"] and view["may"] == ["resume"] and view["never"] == ["promote"]
    assert view["declared"]["kind"] == "auto" and view["since"]
    # a broken setting: the view carries the error and the loop does nothing
    (notes / d.DECIDER_FILE).write_text("{oops")
    view = view_of(model, "notes")
    assert view["mode"] == "off" and "al decider LAYER --reset" in view["error"]
    model.standin_tick = 0
    model.tick_standin(model.started + 100)
    assert model.standin_queue.empty()


def test_summary_carries_the_suggestion_and_detail_the_decisions(start_site):
    notes = start_site["notes"]
    directory = sf.write_run(notes, RUN, decider="suggest")
    (directory / "decide").mkdir()
    (directory / "decide" / "prompt.md").write_text("# Stand in for a person\n")
    (directory / "decide" / "raw.jsonl").write_text(json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.08, "num_turns": 1, "result": "x"}) + "\n")
    decision = sf.with_suggestion(notes, RUN)
    sf.record(notes, {"id": "aa000001", "at": sf.ago(20), "type": "answer", "who": "person", "via": "dashboard", "run": RUN,
                      "answer": "explain", "kind": "resume", "action": "resume", "status": "suggested"})
    sf.write_run(notes, OLD)                                       # a run the stand-in never saw
    model = model_for(notes)
    row = model.summaries[f"notes/{RUN}"]
    s = row["suggestion"]
    assert (s["kind"], s["action"], s["summary"], s["from_step"], s["confidence"]) == (
        "resume", "resume", decision["summary"], "ready", 0.9)
    assert s["read"] == decision["read"] and s["situation"] == "stopped" and "code" not in s
    assert row["decider"] == "suggest" and row["cost_standin"] == 0.08
    assert row["standin_last"]["status"] == "suggested" and row["standin_last"]["auto"] is False
    assert row["standin_stop"] == row["fingerprint"] == f"failed:{sf.state_of(notes, RUN)['ended']}:ready"
    plain = model.summaries[f"notes/{OLD}"]
    assert (plain["suggestion"], plain["standin_last"], plain["standin_stop"], plain["cost_standin"], plain["decider"]) == (
        None, None, "", None, "")
    assert plain["fingerprint"].startswith("failed:")
    detail = model.run_detail("notes", RUN)
    dd = detail["decision"]
    assert (dd["kind"], dd["situation"], dd["confidence"], dd["read"]) == ("resume", "stopped", 0.9, decision["read"])
    assert dd["explanation"] == decision["explanation"] and "code" not in dd
    assert [(e["type"], e["who"], e["status"]) for e in detail["decisions"]] == [
        ("decision", "stand-in", "suggested"), ("answer", "person", "suggested")]
    assert detail["decisions"][0]["why"] == decision["explanation"] and detail["decisions"][1]["answer"] == "explain"
    marks = detail["timeline"]["marks"]
    assert [(m["who"], m["action"], m["auto"]) for m in marks] == [("stand-in", "resume", False), ("person", "resume", False)]
    assert all(isinstance(m["at"], float) for m in marks)
    assert detail["standin"] == {"mode": "suggest", "from": "--decider"}
    assert model.run_detail("notes", OLD)["standin"] == {"mode": "off", "from": "default"}
    assert model.run_detail("notes", OLD)["decisions"] == [] and model.run_detail("notes", OLD)["timeline"]["marks"] == []
    # a new entry republishes the run: the signature counts decisions.jsonl
    before = d.RunFiles(model.layers["notes"], RUN).signature(deep=False)
    sf.record(notes, sf.entry(RUN, id="aa000002", status="acted", auto=True, outcome="resuming from ready"))
    assert d.RunFiles(model.layers["notes"], RUN).signature(deep=False) != before


def test_hostile_decision_text_is_data_not_markup(start_site):
    notes = start_site["notes"]
    sf.write_run(notes, RUN)
    sf.with_suggestion(notes, RUN, summary=sf.HOSTILE, explanation=sf.HOSTILE, instructions=sf.HOSTILE)
    sf.record(notes, sf.entry(RUN, id="bb000001", status="acted", auto=True, outcome=sf.HOSTILE, summary=sf.HOSTILE))
    running = start_server(["--layer", str(notes), "--no-stand-in"])
    try:
        for path in (f"/api/run?layer=notes&run={RUN}", "/api/snapshot"):
            status, _headers, body = running.get(path)
            assert status == 200 and b"<img" not in body and b"<b>" not in body, path   # JSON escapes markup
        detail = running.json(f"/api/run?layer=notes&run={RUN}")
        assert detail["decision"]["summary"] == sf.HOSTILE and detail["decisions"][-1]["outcome"] == sf.HOSTILE
        snapshot = running.json("/api/snapshot")
        row = next(r for r in snapshot["runs"] if r["id"] == RUN)
        assert row["suggestion"]["summary"] == sf.HOSTILE and row["standin_last"]["outcome"] == sf.HOSTILE
        assert snapshot["layers"][0]["standin"]["log"][-1]["outcome"] == sf.HOSTILE
        assert snapshot["layers"][0]["standin"]["loop"] == "off: --no-stand-in"
    finally:
        stop_server(running)


def test_loop_takes_only_items_that_stopped_after_since_and_were_not_seen(start_site):
    notes = start_site["notes"]
    sf.write_run(notes, OLD, ended=sf.ago(3600))                   # stopped an hour ago: before the switch
    sf.write_run(notes, RUN, ended=sf.ago(5))                      # stopped after it
    sf.write_run(notes, "5a0d1e03", ended=sf.ago(4))               # stopped after it, and the stand-in looked already
    sf.with_suggestion(notes, "5a0d1e03")
    sf.write_run(notes, "5a0d1e04", ended=sf.ago(3), decider="off")   # its own --decider off
    sf.write_run(notes, "5a0d1e05", ended=sf.ago(2), status="passed", ship={})   # needs nobody
    sf.record(notes, {"id": "cc000001", "at": sf.ago(600), "type": "mode", "who": "person", "via": "dashboard",
                      "mode": "suggest", "set": ["mode"]})
    (notes / d.DECIDER_FILE).parent.mkdir(parents=True, exist_ok=True)
    (notes / d.DECIDER_FILE).write_text(json.dumps({"version": 1, "mode": "suggest"}))
    model = model_for(notes, no_standin=True)
    view = view_of(model, "notes")
    assert view["mode"] == "suggest" and view["since"]
    items = model.standin_items(model.layers["notes"], view)
    assert [item["run"] for item in items] == [RUN]
    assert items[0]["status"] == "failed" and items[0]["reason"].startswith("ready:") and items[0]["next"] == "Start again, or Give to an agent"
    assert set(items[0]) == {"run", "status", "reason", "next", "landing", "target_branch", "replaced_by"}
    # the same stop is handed over once per process, and a new entry for it ends it for every process
    model.standin_done[f"notes/{RUN}"] = model.summaries[f"notes/{RUN}"]["fingerprint"]
    assert model.standin_items(model.layers["notes"], view) == []
    # the loop itself: on, it queues the first item; a second look does not queue it twice
    live = model_for(notes, idle=True)                             # its first tick looked already
    assert live.standin_queue.qsize() == 1 and live.standin_pending == {f"notes/{RUN}"}
    assert live.standin_queue.queue[0][1] == "consider" and live.standin_queue.queue[0][2]["run"] == RUN
    live.standin_tick = 0
    live.tick_standin(live.started + 200)
    assert live.standin_queue.qsize() == 1


def test_loop_is_off_when_read_only_or_no_stand_in(start_site):
    notes = start_site["notes"]
    (notes / d.DECIDER_FILE).parent.mkdir(parents=True, exist_ok=True)
    (notes / d.DECIDER_FILE).write_text(json.dumps({"version": 1, "mode": "auto"}))
    sf.write_run(notes, RUN, ended=sf.ago(-2))                     # it stopped after the stand-in was switched on
    for kw, loop in (({"read_only": True}, "off: --read-only"), ({"no_standin": True}, "off: --no-stand-in"),
                     ({"in_container": True}, "off: in a container"), ({}, "on")):
        model = model_for(notes, idle=True, **kw)
        view = view_of(model, "notes")
        assert (view["loop"], model.standin_on) == (loop, loop == "on"), kw
        assert view["mode"] == "auto" and view["writable"] is (not kw.get("read_only"))
        model.standin_tick = 0
        model.tick_standin(model.started + 100)
        assert model.standin_queue.qsize() == (1 if loop == "on" else 0), kw
    args = d.parse_args(["--layer", str(notes), "--no-stand-in"])
    assert args.no_stand_in is True and d.parse_args(["--layer", str(notes), "--stand-in-only"]).stand_in_only is True


def test_remove_settlement_restores_the_previous_record(start_site):
    notes = start_site["notes"]
    sf.write_run(notes, RUN)
    first = d.write_settlement(notes, RUN, as_="abandoned", note="nobody needs it", who="Ada", via="dashboard", status="failed")
    second = d.write_settlement(notes, RUN, as_="superseded", note="", who="stand-in", via="stand-in", status="failed")
    assert second["previous"]["as"] == "abandoned"
    left = d.remove_settlement(notes, RUN)
    assert left == {k: v for k, v in first.items()} and d.read_settlement(notes, RUN) == left
    assert d.remove_settlement(notes, RUN) is None and d.read_settlement(notes, RUN) is None
    assert not (notes / "agents" / RUN / "settlement.json").exists()
    try:
        d.remove_settlement(notes, RUN)
    except d.SettleError as exc:
        assert exc.kind == "missing"
    else:
        raise AssertionError("a run without a settlement has nothing to undo")
