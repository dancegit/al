"""The Automatic switch of "Needs you" in the dashboard's model, on the scenario of assign_fixtures
(nine runs under Needs you in the layer `pantry`): the local setting agents/settings/assign.json and
its one format, the `assign_auto` view of a layer, which runs the loop gives to an agent and which it
leaves, that it gives one run per look and nothing where the stand-in's loop or starting is off, and
that the model stand-in waits for the rules. No browser, and nothing is started: the worker is idle."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import assign_fixtures as af
import dashboard as d
import standin_fixtures as sf

SETTING = "agents/settings/assign.json"
OFF = {"available": True, "on": False, "writable": True, "why": "", "loop": "on", "error": "", "log": []}


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("assign-auto") / "s"
    af.build_scenario(root)
    return root


@pytest.fixture()
def layer(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN", "AL_DECIDER"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return root / "pantry.al"


def model_for(layer: Path, idle: bool = True, needs: int = 9, **kw) -> d.Model:
    """A model that has read the layer until `needs` runs are under Needs you. `idle`: its stand-in
    worker ends at once, so what the loop queues stays in the queue and nothing is given."""
    model = d.Model([layer], [], **kw)
    if idle:
        model.stop.set()
    model.discover()
    for _ in range(20):
        model.tick()
        if model.needs_you("pantry") == needs:
            break
    assert model.needs_you("pantry") == needs
    return model


def view_of(model: d.Model) -> dict:
    return next(l for l in model.layer_list() if l["key"] == "pantry")["assign_auto"]


def taken(model: d.Model) -> list[str]:
    return [item["run"] for item in model.assign_auto_items(model.layers["pantry"])]


def oldest_first(model: d.Model) -> list[str]:
    rows = [model.summaries[f"pantry/{run}"] for run in af.KINDS]
    return [row["id"] for row in sorted(rows, key=lambda r: (r.get("ended") or r.get("started") or 0, r["id"]))]


def change_state(layer: Path, run: str, **fields) -> None:
    path = layer / "agents" / run / "state.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}, indent=2) + "\n")


def write_attempt(layer: Path, run: str, by: str) -> None:
    attempt = {"n": 1, "at": d.utc_stamp(), "by": by, "who": "", "kind": af.KINDS[run], "note": "", "run": "c00000a1",
               "reason": "", "outcome": "stopped", "after": "", "why": "it stopped again"}
    (layer / "agents" / run / d.ASSIGN_FILE).write_text(json.dumps({"version": 1, "attempts": [attempt], "approvals": []}))


def test_setting_is_read_and_written(layer):
    path = layer / SETTING
    assert d.ASSIGN_AUTO_FILE == SETTING and d.read_assign_auto(layer) is False and not path.exists()
    assert d.write_assign_auto(layer, True) == path
    assert path.read_text() == '{\n  "auto": true,\n  "version": 1\n}\n'
    assert json.loads(path.read_text()) == {"version": 1, "auto": True}
    assert d.read_assign_auto(layer) is True
    assert sorted(p.name for p in path.parent.iterdir()) == ["assign.json"], "no temporary file is left"
    assert d.write_assign_auto(layer, True) == path and d.read_assign_auto(layer) is True
    d.write_assign_auto(layer, False)
    assert not path.exists() and d.read_assign_auto(layer) is False
    d.write_assign_auto(layer, False)                              # off twice is off
    path.write_text('{"version": 1, "auto": false}')
    assert d.read_assign_auto(layer) is False
    for broken in ("not json", '{"auto": "yes"}', '{"version": 1, "auto": true, "since": "now"}', '{"auto": true}',
                   '{"version": 2, "auto": true}', '{"version": 1, "auto": 1}', '["auto"]', ""):
        path.write_text(broken)
        with pytest.raises(ValueError) as caught:
            d.read_assign_auto(layer)
        assert SETTING in str(caught.value) and "fix or remove it" in str(caught.value), broken
        assert str(layer) not in str(caught.value)


def test_layer_list_carries_the_automatic_view(layer, tmp_path):
    model = model_for(layer)
    assert view_of(model) == OFF
    d.write_assign_auto(layer, True)
    model.tick()
    assert view_of(model) == {**OFF, "on": True}
    # a broken setting: off, with the reader's sentence, and the loop takes nothing
    (layer / SETTING).write_text("{oops")
    broken = view_of(model)
    assert broken["on"] is False and broken["error"].startswith(f"{SETTING}: expected ") and broken["writable"] is True
    assert taken(model) == []
    d.write_assign_auto(layer, True)
    for kw, loop, writable in (({"read_only": True}, "off: --read-only", False),
                               ({"no_start": True}, "off: --no-start", False),
                               ({"no_standin": True}, "off: --no-stand-in", True),
                               ({"in_container": True}, "off: in a container", False)):
        view = view_of(model_for(layer, **kw))
        assert (view["available"], view["on"], view["loop"], view["writable"]) == (True, True, loop, writable), kw
        assert bool(view["why"]) is (not writable), kw
    assert "--read-only" in view_of(model_for(layer, read_only=True))["why"]
    assert "--no-start" in view_of(model_for(layer, no_start=True))["why"]
    # a layer generated before `al assign`
    root = tmp_path / "old"
    af.build_scenario(root, with_assign=False)
    old = view_of(model_for(root / "pantry.al"))
    assert (old["available"], old["on"], old["writable"]) == (False, False, False)
    assert old["why"].startswith("generated before `al assign`: run `al gen pantry`")


def test_loop_takes_every_run_under_needs_you_once(layer):
    model = model_for(layer, no_standin=True)
    assert taken(model) == [], "the switch is off"
    d.write_assign_auto(layer, True)
    order = oldest_first(model)
    assert sorted(order) == sorted(af.KINDS) and len(order) == 9
    assert taken(model) == order, "every run that waits already, oldest stop first: there is no since"
    assert all(set(item) == {"run"} for item in model.assign_auto_items(model.layers["pantry"]))
    # a stop this process tried already
    first = order[0]
    model.assign_auto_done[f"pantry/{first}"] = model.summaries[f"pantry/{first}"]["fingerprint"]
    assert taken(model) == order[1:]
    model.assign_auto_done[f"pantry/{first}"] = "failed:another stop:"
    assert taken(model) == order, "a new stop of the run is looked at again"
    # the stand-in gave it already; a person gave it; it was started with the stand-in off
    write_attempt(layer, af.REJECTED, "decider")
    write_attempt(layer, af.FAILED_CHECK, "dashboard")
    change_state(layer, af.NOTHING, decider="off")
    model._reread(model.layers["pantry"], [af.REJECTED, af.FAILED_CHECK, af.NOTHING])   # as the watcher's next scan does
    assert model.summaries[f"pantry/{af.FAILED_CHECK}"]["assign"]["clicks"] == 1
    assert model.summaries[f"pantry/{af.NOTHING}"]["decider"] == "off"
    assert taken(model) == [run for run in order if run not in (af.REJECTED, af.FAILED_CHECK, af.NOTHING)]
    d.write_assign_auto(layer, False)
    assert taken(model) == []


def test_loop_queues_one_give_per_look_and_only_when_it_may(layer):
    d.write_assign_auto(layer, True)
    model = model_for(layer)
    model.standin_tick = 0
    model.tick_standin(model.started + 100)
    oldest = oldest_first(model)[0]
    assert list(model.standin_queue.queue) == [("pantry", "give", {"run": oldest})]
    assert model.standin_pending == {f"pantry/{oldest}"}
    model.standin_tick = 0
    model.tick_standin(model.started + 200)
    assert model.standin_queue.qsize() == 1, "a second look queues nothing more while the first waits"
    for kw in ({"read_only": True}, {"no_standin": True}, {"in_container": True}, {"no_start": True}):
        quiet = model_for(layer, **kw)
        quiet.standin_tick = 0
        quiet.tick_standin(quiet.started + 100)
        assert quiet.standin_queue.empty() and quiet.assign_auto_loop() != "on", kw
    # switched off: the loop that may run queues nothing
    d.write_assign_auto(layer, False)
    off = model_for(layer)
    off.standin_tick = 0
    off.tick_standin(off.started + 100)
    assert off.standin_queue.empty() and off.assign_auto_loop() == "on"


def test_model_stand_in_waits_for_the_rules(layer):
    (layer / d.DECIDER_FILE).parent.mkdir(parents=True, exist_ok=True)
    (layer / d.DECIDER_FILE).write_text(json.dumps({"version": 1, "mode": "suggest"}))
    change_state(layer, af.FAILED_CHECK, ended=sf.ago(-2))         # it stopped after the stand-in was switched on
    model = model_for(layer)
    pantry = model.layers["pantry"]
    view = model.standin_view(pantry)
    asked = lambda: [item.get("run") for item in model.standin_items(pantry, view)]   # noqa: E731
    assert asked() == [af.FAILED_CHECK], "the Automatic switch is off: the model stand-in is asked"
    d.write_assign_auto(layer, True)
    assert asked() == [], "the rules go first"
    model.assign_auto_done[f"pantry/{af.FAILED_CHECK}"] = model.summaries[f"pantry/{af.FAILED_CHECK}"]["fingerprint"]
    assert asked() == [af.FAILED_CHECK], "the rules tried it and it stayed: now the model stand-in"
    # where the Automatic loop does not run, the model stand-in does not wait for it
    del model.assign_auto_done[f"pantry/{af.FAILED_CHECK}"]
    elsewhere = model_for(layer, no_start=True)
    assert [item.get("run") for item in elsewhere.standin_items(elsewhere.layers["pantry"], view)] == [af.FAILED_CHECK]
