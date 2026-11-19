"""What a layer can run, on the dashboard (model and HTTP, no browser): the Layers view's data and
the start form's data come from the runtime's registry (adws/adw_modules/registry.py), so the
workflows the kit gives every layer (`integrate`, promotions, `improve`, the stand-in decider) are
listed beside the declared ones, marked `kit`, with the input each takes and whether and how a
person starts it. On the scenario of integrate_fixtures (`pantry.al`: adws/integrate.py and
handed-over runs) and the promotion site of promotion_fixtures."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

import dashboard as d
import integrate_fixtures as fx
import settle_fixtures as sf
from conftest import start_server, stop_server
from promotion_fixtures import build_promotion_site
from test_start import post

KIT = ["integrate", "assign", "promote", "improve", "decider"]
APPLICABLE = [fx.ACCEPTANCE, fx.CLEAN, fx.PROTECTED, fx.CHANGED]   # offered; the preview refuses the last two
VIEW_KEYS = {"name", "kind", "purpose", "steps", "input", "start", "ships", "setting", "off"}


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("registry-views") / "s"
    fx.build_scenario(root)
    return root


@pytest.fixture()
def layer(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("AL_APP_ROOT", raising=False)
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


def model_for(layer: Path, ticks: int = 3) -> d.Model:
    model = d.Model([layer], [])
    model.discover()
    for _ in range(ticks):
        model.tick()
    return model


def named(items: list[dict], name: str) -> dict:
    return next(item for item in items if item["name"] == name)


def startable(server, key: str = "pantry") -> dict:
    return server.json(f"/api/startable?layer={key}")


def startable_when(server, predicate, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    data: dict = {}
    while time.monotonic() < deadline:
        data = startable(server)
        if predicate(data):
            return data
        time.sleep(0.2)
    raise AssertionError(f"/api/startable never matched: {named(data['kit'], 'integrate')}")


def wait_record(layer: Path, run: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    record: dict = {}
    while time.monotonic() < deadline:
        path = layer / "agents" / run / "integration.json"
        if path.is_file():
            record = json.loads(path.read_text())
            if record.get("status") not in ("queued", "running"):
                return record
        time.sleep(0.3)
    raise AssertionError(f"the integration of {run} did not end: {record}")


# ------------------------------------------------------------------------------ both views

def test_kit_workflows_are_in_both_views(layer):
    """The regression: `integrate` (and every other kit entry) is in what the start form and the
    Layers view get, marked as coming from the kit; the declared lists are what they were."""
    model = model_for(layer)
    catalog = d.startables(model.layers["pantry"])
    detail = model.layer_detail("pantry")
    assert [k["name"] for k in catalog["kit"]] == KIT
    assert [k["name"] for k in detail["kit"]] == KIT
    assert [w["name"] for w in catalog["workflows"]] == ["plan-build", "ship-it"]
    assert [a["name"] for a in detail["adws"]] == ["plan-build", "ship-it"]
    assert [c["name"] for c in catalog["commands"]] == ["classify", "plan-chore", "plan-bug", "plan-feature",
                                                        "review", "implement"]
    for view in (catalog["kit"], detail["kit"]):
        for entry in view:
            assert VIEW_KEYS <= set(entry), entry["name"]
        integrate = named(view, "integrate")
        assert integrate["kind"] == "kit-workflow" and integrate["off"] is False
        assert integrate["input"] == {"kind": "run", "what": "a handed-over run"}
        assert integrate["start"] == {"by_hand": True, "how": "form", "command": "al integrate LAYER RUN", "why": ""}
        assert integrate["steps"] == ["prepare", "resolve", "checks", "review", "ship"]
        assert integrate["setting"] == {"where": "templates/integrate.json", "key": "enabled", "on": True,
                                        "turn_on": "al integrate LAYER --set enabled=true"}
        assert "ship-it" in integrate["purpose"], "the workflow whose runs it serves"
        promote = named(view, "promote")                      # the layer declares no stages
        assert promote["off"] is True and promote["start"]["by_hand"] is False
        assert "(delivery sprint)" in promote["start"]["why"] and promote["setting"]["on"] is False
        improve = named(view, "improve")
        assert improve["start"]["by_hand"] is False and improve["start"]["command"] == 'al improve LAYER "…"'
        decider = named(view, "decider")
        assert decider["kind"] == "decider" and decider["off"] is True
        assert decider["setting"]["turn_on"] == "al decider LAYER --set suggest|auto"
    # the Layers view: a graph of the steps, and the runs of each entry
    integrate = named(detail["kit"], "integrate")
    assert [n["step"] for n in integrate["graph"]["nodes"]] == ["prepare", "resolve", "checks", "review", "ship"]
    assert integrate["stats"]["count"] == 0 and integrate["recent"] == []
    assert named(detail["kit"], "improve")["graph"] is None and named(detail["kit"], "improve")["steps"][0] == "plan"
    assert [c["name"] for c in detail["commands"]] == [c["name"] for c in catalog["commands"]]
    assert named(detail["commands"], "implement")["kind"] == "hop"
    assert named(detail["commands"], "classify")["adw"] == "command:classify"
    assert detail["suite"] == [] and detail["problems"] == [] and catalog["problems"] == []
    assert named(detail["adws"], "ship-it")["stats"]["count"] == 9
    assert model.layers["pantry"].entry("integrate")["source"] == "kit"
    assert model.layers["pantry"].entry("ship-it")["kind"] == "workflow"
    assert model.layers["pantry"].entry("command:classify")["name"] == "classify"
    assert model.layers["pantry"].entry("nope") is None and model.layers["pantry"].adw("integrate") is None


def test_layers_view_counts_the_runs_of_a_kit_workflow_and_a_command(layer):
    now = d.utc_stamp().replace("Z", "+00:00")
    definition = {"name": "integrate", "integration": {"run": fx.ACCEPTANCE, "target": "main"},
                  "steps": [{"kind": "integrate", "name": "prepare", "action": "prepare"}]}
    sf.write_run(layer, "c0000009", adw="integrate", status="failed", started=now, ended=now, definition=definition,
                 prompt="Integrate run f0000001 onto main", integration={"run": fx.ACCEPTANCE, "target": "main"})
    sf.write_run(layer, "c000000a", adw="command:classify", status="passed", started=now, ended=now,
                 prompt="what is this", definition={"name": "command:classify", "steps": [
                     {"kind": "run", "name": "classify", "command": "classify"}]})
    detail = model_for(layer).layer_detail("pantry")
    integrate = named(detail["kit"], "integrate")
    assert integrate["stats"]["count"] == 1 and [r["id"] for r in integrate["recent"]] == ["c0000009"]
    classify = named(detail["commands"], "classify")
    assert classify["stats"]["count"] == 1 and [r["id"] for r in classify["recent"]] == ["c000000a"]
    assert named(detail["adws"], "ship-it")["stats"]["count"] == 9, "a kit run is not counted as a declared one"


# ------------------------------------------------------------------------------ the start form's data

def test_integrate_picker_lists_only_applicable_runs(layer, running):
    """GET /api/startable: `integrate` comes with the runs it applies to (the handed-over runs the
    page offers it for; the runtime's preview may still refuse one), newest first."""
    data = startable_when(running, lambda data: len(named(data["kit"], "integrate").get("candidates") or []) == 4)
    integrate = named(data["kit"], "integrate")
    assert sorted(c["id"] for c in integrate["candidates"]) == sorted(APPLICABLE)
    assert [c["id"] for c in integrate["candidates"]] == sorted(APPLICABLE, reverse=True), "newest first"
    first = named([{**c, "name": c["id"]} for c in integrate["candidates"]], fx.ACCEPTANCE)
    assert first["title"] == "Give stock a discount" and first["target"] == "main"
    assert first["reasons"] == [fx.CONFLICT]
    assert integrate["none_why"] == "" and integrate["page_start"] == {"enabled": True, "why": ""}
    assert {fx.REJECTED, fx.LANDED, fx.REPLACED, fx.MOVER} & {c["id"] for c in integrate["candidates"]} == set()
    for run in (fx.ACCEPTANCE, fx.CLEAN):                      # the preview accepts these on first sight
        assert running.json(f"/api/integrate/preview?layer=pantry&run={run}")["eligible"] is True
    for run in (fx.PROTECTED, fx.CHANGED):                     # offered, and the preview says why not
        assert running.json(f"/api/integrate/preview?layer=pantry&run={run}")["eligible"] is False
    assert str(layer.parent) not in json.dumps(data), "no path of this machine"
    # the other kit entries say why they are not started here
    assert named(data["kit"], "improve")["page_start"]["enabled"] is False
    assert "al improve" in named(data["kit"], "improve")["page_start"]["why"]
    assert "candidates" not in named(data["kit"], "promote")
    assert data["start"] == {"enabled": True, "why": ""} and [w["name"] for w in data["workflows"]] == ["plan-build", "ship-it"]
    # switched off: the entry is off, nothing is listed, and the reason names the file
    (layer / d.INTEGRATE_SETTINGS).write_text(json.dumps({"version": 1, "enabled": False}))
    off = named(startable_when(running, lambda data: named(data["kit"], "integrate")["off"])["kit"], "integrate")
    assert off["candidates"] == [] and off["setting"]["on"] is False
    assert off["none_why"] == "switched off in templates/integrate.json"
    # a file the runtime would refuse: off, with the problem, and the page still answers
    (layer / d.INTEGRATE_SETTINGS).write_text("{broken")
    broken = startable_when(running, lambda data: bool(data["problems"]))
    assert broken["problems"] == ["templates/integrate.json: not JSON"]
    assert named(broken["kit"], "integrate")["none_why"] == "templates/integrate.json: not JSON"
    # on again, and the script missing (a layer generated before the kit workflow)
    (layer / d.INTEGRATE_SETTINGS).unlink()
    (layer / d.INTEGRATE_SCRIPT).unlink()
    old = named(startable_when(running, lambda data: not named(data["kit"], "integrate")["available"])["kit"], "integrate")
    assert old["off"] is True and old["candidates"] == [] and old["none_why"] == "run `al gen` to add adws/integrate.py"


def test_picker_says_why_when_no_run_is_handed_over(layer, running):
    for run in APPLICABLE:
        d.write_settlement(layer, run, as_="landed", who="Ada Example", via="dashboard", status="handed-over")
    data = startable_when(running, lambda data: named(data["kit"], "integrate").get("candidates") == [])
    integrate = named(data["kit"], "integrate")
    assert integrate["off"] is False
    assert integrate["none_why"] == "no run of this layer is handed over because of a merge conflict or a moved target"


@pytest.mark.parametrize("flag", ["--read-only", "--no-start"])
def test_picker_is_listed_but_not_startable_on_a_read_only_page(layer, flag):
    server = start_server(["--layer", str(layer), flag])
    try:
        integrate = named(startable(server)["kit"], "integrate")
        assert integrate["page_start"]["enabled"] is False and flag in integrate["page_start"]["why"]
    finally:
        stop_server(server)


def test_integrate_starts_from_the_form(layer, running):
    """What the form does: the first candidate, its preview, then the start with the preview's nonce."""
    data = startable_when(running, lambda data: len(named(data["kit"], "integrate").get("candidates") or []) == 4)
    candidates = named(data["kit"], "integrate")["candidates"]
    chosen = next(c for c in candidates if c["id"] == fx.ACCEPTANCE)
    preview = running.json(f"/api/integrate/preview?layer=pantry&run={chosen['id']}")
    assert preview["eligible"] is True and preview["steps"] == ["prepare", "resolve", "test", "review", "ship"]
    status, answer = post(running, {"layer": "pantry", "run": chosen["id"], "action": "start", "confirm": preview["nonce"]},
                          "/api/integrate")
    assert status == 202, answer
    new = answer["run_id"]
    summary = next(r for r in running.json("/api/snapshot")["runs"] if r["id"] == new)
    assert summary["kit"] is True and summary["acts_on"] == {"kind": "run", "id": fx.ACCEPTANCE}
    assert summary["adw"] == "integrate"
    record = wait_record(layer, fx.ACCEPTANCE)
    assert record["status"] == "landed", (layer / "agents" / new / "launch.log").read_text()[-3000:]
    detail = running.json(f"/api/run?layer=pantry&run={new}")
    assert detail["summary"]["kit"] is True and detail["summary"]["acts_on"]["id"] == fx.ACCEPTANCE
    assert [n["step"] for n in detail["graph"]["nodes"]] == ["prepare", "resolve", "test", "review", "ship"]
    # it is not listed any more, and the Layers view counts the run under `integrate`
    after = startable_when(running, lambda data: fx.ACCEPTANCE not in
                           [c["id"] for c in named(data["kit"], "integrate")["candidates"]])
    assert len(named(after["kit"], "integrate")["candidates"]) == 3
    kit = named(running.json("/api/layer?layer=pantry")["kit"], "integrate")
    assert kit["stats"]["count"] == 1 and kit["recent"][0]["id"] == new


def test_kit_name_is_not_a_free_text_start(layer, running):
    """A kit workflow takes a run or two stages: the free-text start says where it is started,
    never "no workflow named"; nothing starts."""
    before = sorted(p.name for p in (layer / "agents").iterdir())
    sentence = "integrate takes a handed-over run: choose it in the form"
    body = {"layer": "pantry", "request_key": "key-0001-abcdef"}
    status, answer = post(running, {**body, "text": "/integrate x"})
    assert status == 400 and answer["error"] == sentence, answer
    status, answer = post(running, {**body, "target": {"kind": "workflow", "name": "integrate"}, "prompt": "x"})
    assert status == 400 and answer["error"] == sentence, answer
    status, answer = post(running, {**body, "text": "/improve make it faster"})
    assert status == 400 and answer["error"] == 'improve is a kit workflow that takes a request: al improve LAYER "…"'
    status, answer = post(running, {**body, "text": "/decider"})
    assert status == 400 and answer["error"].startswith("decider is the stand-in for a person")
    status, answer = post(running, {**body, "text": "/nope x"})
    assert status == 400 and answer["error"].startswith("no workflow or command named '/nope'; known: /plan-build, /ship-it, ")
    assert answer["error"].endswith("/integrate (kit), /assign (kit), /promote (kit), /improve (kit), /decider (kit)")
    assert sorted(p.name for p in (layer / "agents").iterdir()) == before
    model = model_for(layer)
    assert d.split_slash("/integrate x", d.startables(model.layers["pantry"])) is None
    assert d.kit_sentence(model.layers["pantry"], "integrate") == sentence
    assert d.kit_sentence(model.layers["pantry"], "ship-it") == "" and d.kit_sentence(model.layers["pantry"], "nope") == ""


# ------------------------------------------------------------------------------ the run page

def test_kit_run_page_draws_its_steps(layer):
    now = d.utc_stamp().replace("Z", "+00:00")
    recorded = {"name": "integrate", "integration": {"run": fx.ACCEPTANCE, "target": "main"}, "review": "gate",
                "agent": "mock", "environment": ["wt"],
                "steps": [{"kind": "integrate", "name": "prepare", "action": "prepare"},
                          {"kind": "integrate", "name": "resolve", "action": "resolve"},
                          {"kind": "check", "name": "test", "cmd": "test", "whenAffected": True},
                          {"kind": "review", "name": "review"},
                          {"kind": "integrate", "name": "approve", "action": "approve"},
                          {"kind": "ship", "name": "ship"}]}
    sf.write_run(layer, "c0000009", adw="integrate", status="failed", started=now, ended=now, definition=recorded,
                 prompt="Integrate run f0000001 onto main", integration={"run": fx.ACCEPTANCE, "target": "main"})
    sf.write_run(layer, "c000000a", adw="integrate", status="failed", started=now, ended=now,
                 prompt="an integration run of an earlier kit that recorded no definition")
    sf.write_run(layer, "c000000b", adw="command:classify", status="passed", started=now, ended=now,
                 prompt="what is this", definition={"name": "command:classify", "steps": [
                     {"kind": "run", "name": "classify", "command": "classify"}]})
    model = model_for(layer)
    with_definition = model.run_detail("pantry", "c0000009")
    # the definition the run recorded is drawn, whatever the registry says today
    assert [n["step"] for n in with_definition["graph"]["nodes"]] == ["prepare", "resolve", "test", "review", "approve", "ship"]
    assert [s["name"] for s in with_definition["summary"]["steps"]] == ["prepare", "resolve", "test", "review", "approve", "ship"]
    assert with_definition["summary"]["kit"] is True
    assert with_definition["summary"]["acts_on"] == {"kind": "run", "id": fx.ACCEPTANCE}
    # without one, the registry entry's steps (before the registry: no step at all)
    without = model.run_detail("pantry", "c000000a")
    assert [n["step"] for n in without["graph"]["nodes"]] == ["prepare", "resolve", "checks", "review", "ship"]
    assert [n["type"] for n in without["graph"]["nodes"]][:2] == ["integrate", "integrate"]
    assert without["summary"]["kit"] is True and without["summary"]["acts_on"] is None
    assert without["summary"]["adw"] == "integrate"
    # a single-command run and a run of a declared workflow
    command = model.run_detail("pantry", "c000000b")["summary"]
    assert command["kit"] is False and command["acts_on"] == {"kind": "command", "name": "classify"}
    declared = model.run_detail("pantry", fx.MOVER)["summary"]
    assert declared["kit"] is False and declared["acts_on"] is None
    assert [s["name"] for s in declared["steps"]] == ["plan", "test", "review", "ship"]
    # a failed kit run is not started again as free text: the entry's sentence, not "no longer declares"
    request, code, why = model.again_request(model.layers["pantry"], "c000000a")
    assert request is None and code == 400
    assert why == "run c000000a ran a kit workflow: integrate takes a handed-over run: choose it in the form"


# ------------------------------------------------------------------------------ older layers

def test_older_layer_without_registry_module(layer, monkeypatch):
    """A layer generated before the registry: the copy beside the dashboard is used; without any
    copy (the dashboard's container, an older layer) the declared lists and one problem line."""
    (layer / d.REGISTRY_MODULE).unlink()
    model = model_for(layer)
    catalog = d.startables(model.layers["pantry"])
    assert [w["name"] for w in catalog["workflows"]] == ["plan-build", "ship-it"]
    assert [k["name"] for k in catalog["kit"]] == KIT and catalog["problems"] == []
    monkeypatch.setattr(d, "REGISTRY_BESIDE", ())
    monkeypatch.setattr(d, "_REGISTRY_BUILT", {})
    bare = d.startables(model.layers["pantry"])
    assert bare["workflows"] == catalog["workflows"] and bare["commands"] == catalog["commands"]
    assert bare["kit"] == [] and bare["problems"] == ["generated before the registry: run al gen pantry"]
    detail = model.layer_detail("pantry")
    assert [a["name"] for a in detail["adws"]] == ["plan-build", "ship-it"] and detail["kit"] == []
    assert detail["problems"] == ["generated before the registry: run al gen pantry"]
    assert d.kit_sentence(model.layers["pantry"], "integrate") == ""


def test_a_layer_copy_that_does_not_load_falls_back(layer, capsys):
    (layer / d.REGISTRY_MODULE).write_text("raise RuntimeError('broken copy')\n")
    model = model_for(layer)
    catalog = d.startables(model.layers["pantry"])
    assert [k["name"] for k in catalog["kit"]] == KIT and catalog["problems"] == []
    assert "the registry module did not load" in capsys.readouterr().err


def test_only_the_compiled_layer_is_needed(tmp_path):
    """A directory with nothing but agentic-layer.json: its declared workflows are listed, and the kit
    entries whose script is not there are off with the way to add them."""
    bare = tmp_path / "bare.al"
    bare.mkdir()
    import start_layer

    (bare / "agentic-layer.json").write_text(json.dumps(start_layer.ir("bare", tmp_path / "app", None, 28990)))
    model = model_for(bare)
    catalog = d.startables(model.layers["bare"])
    assert [w["name"] for w in catalog["workflows"]] == ["plan-build", "ship-it"]
    integrate = named(catalog["kit"], "integrate")
    assert integrate["off"] is True and integrate["start"]["why"] == "run `al gen` to add adws/integrate.py"
    assert [a["name"] for a in model.layer_detail("bare")["adws"]] == ["plan-build", "ship-it"]


# ------------------------------------------------------------------------------ promotions

@pytest.fixture(scope="module")
def promotion_site(tmp_path_factory):
    return build_promotion_site(tmp_path_factory.mktemp("registry-promotion"))


def test_promotion_entry_takes_two_stages(promotion_site, monkeypatch):
    monkeypatch.delenv("AL_APP_ROOT", raising=False)
    layer = promotion_site["layer"]
    server = start_server(["--layer", str(layer)])
    try:
        data = startable(server, "shop")
        assert [k["name"] for k in data["kit"]] == ["integrate", "assign", "promote-sprint-main", "improve", "decider"]
        promote = named(data["kit"], "promote-sprint-main")
        assert promote["kind"] == "kit-workflow" and promote["off"] is False
        assert promote["input"] == {"kind": "stages", "what": "two stages", "from": "sprint", "to": "main"}
        assert promote["start"]["by_hand"] is True and promote["start"]["command"] == "al promote LAYER sprint main"
        assert promote["page_start"] == {"enabled": True, "why": ""}
        assert promote["open"]["id"] == promotion_site["run"] and promote["approval"] == "human" and promote["method"]
        assert promote["steps"][0] == "open" and promote["steps"][-1] == "close" and "approval" in promote["steps"]
        # the layer has no adws/integrate.py (generated before it): listed, off, with the way to add it
        integrate = named(data["kit"], "integrate")
        assert integrate["off"] is True and integrate["none_why"] == "run `al gen` to add adws/integrate.py"
        detail = server.json("/api/layer?layer=shop")
        kit = named(detail["kit"], "promote-sprint-main")
        assert [n["step"] for n in kit["graph"]["nodes"]] == promote["steps"]
        assert kit["stats"]["count"] == 1 and kit["recent"][0]["id"] == promotion_site["run"]
        assert [a["name"] for a in detail["adws"]] == ["demo"] and detail["promotions"][0]["from"] == "sprint"
        summary = server.json(f"/api/run?layer=shop&run={promotion_site['run']}")["summary"]
        assert summary["kit"] is True and summary["acts_on"] == {"kind": "promotion", "from": "sprint", "to": "main"}
        status, answer = post(server, {"layer": "shop", "request_key": "key-0001-abcdef", "text": "/promote-sprint-main"})
        assert status == 400
        assert answer["error"] == "promote-sprint-main takes two stages: choose it in the form or on the layer's page"
    finally:
        stop_server(server)
