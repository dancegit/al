"""The stand-in beyond a resume (standin.py): its scope and limits, the record of every decision, and
each decision kind with the mock agent. Git is real, the agent is the mock (one script of
tests/data/standin per action), nothing is spawned: decide.spawn_resume and launch._spawn either
record the command line or run it in this process."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smtp_sink  # noqa: E402
from test_decide import ADW, ADW_ID, MAIL, PROMPT, _layer, _mail_env, _mock, _state  # noqa: E402

from adw_modules import decide, events, ir, launch, standin, state as state_module  # noqa: E402
from adw_modules.errors import LayerError, StepFailed  # noqa: E402
from adw_modules.runner import main  # noqa: E402
from adw_modules.state import ADWState  # noqa: E402

DATA = Path(__file__).resolve().parent / "data" / "standin"
STEPS = ["plan", "build", "test", "review", "ship"]
ALL = list(standin.KINDS)


@pytest.fixture(autouse=True)
def no_settings_of_the_person(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_NOTIFY_TO", "AL_NOTIFY_FROM", "AL_NOTIFY_SMTP_URL", "AL_DASHBOARD_URL", "ADW_FANOUT_PARENT",
                "AL_DECIDER", "AL_DECIDE_IMAP_URL", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST"):
        monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------------------------------------ helpers

def declared(kind: str = "auto", **fields) -> dict:
    """The IR of a (stand-in KIND …) clause with the vocabulary's defaults."""
    return {"id": kind, "kind": kind, "status": "stable", "modelSet": "heavy", "may": ["resume"], "never": [],
            "maxActionsPerRun": 3, "maxPerHour": 12, "escalateTo": ["dashboard"], "systemd": False, **fields}


def set_ir(layer_dir: Path, **keys) -> None:
    path = layer_dir / "agentic-layer.json"
    data = json.loads(path.read_text())
    data.update(keys)
    path.write_text(json.dumps(data, indent=2))


def decider(layer_dir: Path, name: str) -> None:
    (layer_dir / "mock").mkdir(exist_ok=True)
    (layer_dir / "mock" / "decide.json").write_text((DATA / f"{name}.json").read_text(), encoding="utf-8")


def run(layer_dir: Path, *flags: str, adw_id: str = ADW_ID, adw: str = "demo") -> int:
    return main(adw, layer_dir, [PROMPT, "--adw-id", adw_id, *flags])


def stopped_run(make_layer, tmp_path, stand_in=None, adw_id: str = ADW_ID, **layer_kw) -> Path:
    """A layer whose run `adw_id` stopped at the check `test` (no `ready` file) and was handed over
    with PR.md; the decider was off while it ran."""
    layer_dir = _layer(make_layer, tmp_path, **layer_kw)
    environments = json.loads((layer_dir / "agentic-layer.json").read_text())["environments"]
    environments[0]["keep"] = "on-failure"                        # the vocabulary's default: a stopped run keeps its worktree
    set_ir(layer_dir, environments=environments)
    if stand_in is not None:
        set_ir(layer_dir, standIn=stand_in)
    os.environ["AL_DECIDER"] = "off"                              # off while it runs, without the run's own flag
    try:
        assert run(layer_dir, adw_id=adw_id) == 1
    finally:
        del os.environ["AL_DECIDER"]
    return layer_dir


def item_for(run_id: str = ADW_ID, **more) -> dict:
    return {"run": run_id, **more}


def log(layer_dir: Path) -> list[dict]:
    return standin.entries(ir.load(layer_dir))


@pytest.fixture
def spawned(monkeypatch):
    """Resumes and starts are recorded, never started: [("resume", argv)] and [("start", argv)]."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(decide, "spawn_resume", lambda layer, state: calls.append(("resume", decide.resume_argv(layer, state))))
    monkeypatch.setattr(launch, "_spawn", lambda argv, **kw: calls.append(("start", list(argv))))
    monkeypatch.setattr(standin, "_spawn_detached", lambda argv, cwd, log: calls.append(("promote", list(argv))))
    return calls


def resume_in_process(monkeypatch, before=None) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake(layer, state):
        argv = decide.resume_argv(layer, state)
        seen.append(argv)
        if before is not None:
            before()
        main(state.adw, Path(layer.layer_dir), argv[2:])
    monkeypatch.setattr(decide, "spawn_resume", fake)
    return seen


# ------------------------------------------------------------------------------------ settings and mode

def test_a_plain_mode_writes_the_file_of_before(tmp_path):
    path = decide.write_setting(tmp_path, mode="suggest")
    assert path.read_bytes() == b'{\n  "mode": "suggest",\n  "version": 1\n}\n'
    assert path.read_text() == decide.render_setting("suggest")
    assert decide.read_setting(tmp_path) == {"mode": "suggest", "may": None, "paused": False}
    assert decide.write_mode(tmp_path, "auto").read_text() == decide.render_setting("auto")
    decide.write_setting(tmp_path, may=["restart", "resume"])
    assert json.loads(path.read_text()) == {"version": 1, "mode": "auto", "may": ["restart", "resume"]}
    decide.write_setting(tmp_path, paused=True)
    assert decide.read_setting(tmp_path) == {"mode": "auto", "may": ["restart", "resume"], "paused": True}
    assert decide.read_mode(tmp_path) == "auto"
    decide.write_setting(tmp_path, may=None, paused=False)
    assert path.read_text() == decide.render_setting("auto")
    decide.write_setting(tmp_path, mode="", paused=True)          # paused without a local mode
    assert json.loads(path.read_text()) == {"version": 1, "paused": True}
    assert decide.read_setting(tmp_path) == {"mode": "", "may": None, "paused": True} and decide.read_mode(tmp_path) == ""
    decide.write_setting(tmp_path, paused=False)                  # nothing left: the file goes
    assert not path.exists()
    assert decide.read_setting(tmp_path) == {"mode": "", "may": None, "paused": False}
    decide.write_setting(tmp_path, mode="suggest")
    decide.write_mode(tmp_path, "")                               # a reset removes the file
    assert not path.exists()
    with pytest.raises(ValueError, match="bless"):
        decide.write_setting(tmp_path, may=["bless"])
    with pytest.raises(ValueError, match="sometimes"):
        decide.write_setting(tmp_path, mode="sometimes")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1, "mode": "auto", "may": "resume"}')
    with pytest.raises(LayerError, match="al decider LAYER --reset"):
        decide.read_setting(tmp_path)


def test_mode_falls_back_to_the_declaration(tmp_path, monkeypatch):
    layer = SimpleNamespace(layer_dir=tmp_path, ir={"standIn": declared("auto")})
    assert decide.mode_for(layer, None) == ("auto", "layer")
    decide.write_mode(tmp_path, "suggest")
    assert decide.mode_for(layer, None) == ("suggest", "local setting")
    monkeypatch.setenv("AL_DECIDER", "off")
    assert decide.mode_for(layer, None) == ("off", "AL_DECIDER")
    assert decide.mode_for(layer, "auto") == ("auto", "--decider")
    monkeypatch.delenv("AL_DECIDER")
    decide.write_mode(tmp_path, "")
    layer.ir = {"standIn": declared("off")}
    assert decide.mode_for(layer, None) == ("off", "default")
    layer.ir = {"standIn": None}
    assert decide.mode_for(layer, None) == ("off", "default")
    assert decide.mode_for(SimpleNamespace(layer_dir=tmp_path), None) == ("off", "default")


def test_scope_without_a_declaration_is_resume_only(make_layer, tmp_path):
    layer = ir.load(_layer(make_layer, tmp_path))
    scope = standin.scope(layer)
    assert (scope["mode"], scope["source"], scope["declared"]) == ("off", "default", None)
    assert scope["offered"] == ["resume"] and scope["may"] == ["resume"] and scope["never"] == []
    assert scope["suggests"] == [k for k in ALL if k != "resume"]
    assert (scope["per_run"], scope["per_hour"]) == (decide.MAX_AUTO_ROUNDS, 12)
    assert scope["escalate"] == ["dashboard"] and scope["model_set"] == "heavy"
    assert scope["paused"] is False and scope["since"] == ""
    decide.write_mode(layer.layer_dir, "auto")
    scope = standin.scope(layer)
    assert (scope["mode"], scope["source"]) == ("auto", "local setting") and scope["since"]
    assert standin.scope(layer, "suggest")["mode"] == "suggest"


def test_scope_is_declared_may_minus_never_narrowed_locally(make_layer, tmp_path):
    layer_dir = _layer(make_layer, tmp_path)
    set_ir(layer_dir, standIn=declared("auto", may=["resume", "restart", "settle"], never=["land", "promote"],
                                       maxActionsPerRun=5, maxPerHour=7, escalateTo=["dashboard", "notify"]))
    layer = ir.load(layer_dir)
    scope = standin.scope(layer)
    assert (scope["mode"], scope["source"]) == ("auto", "layer")
    assert scope["offered"] == ["resume", "restart", "settle"] == scope["may"]
    assert scope["never"] == ["land", "promote"]
    assert scope["suggests"] == ["answer", "integrate", "retry-job"]
    assert (scope["per_run"], scope["per_hour"], scope["escalate"]) == (5, 7, ["dashboard", "notify"])
    decide.write_setting(layer_dir, may=["restart"])
    scope = standin.scope(layer)
    assert scope["may"] == ["restart"] and scope["offered"] == ["resume", "restart", "settle"]
    assert scope["suggests"] == ["resume", "answer", "integrate", "settle", "retry-job"]
    decide.write_setting(layer_dir, paused=True)
    assert standin.scope(layer)["paused"] is True


def test_a_local_kind_the_layer_does_not_offer_is_ignored(make_layer, tmp_path):
    layer_dir = _layer(make_layer, tmp_path)
    set_ir(layer_dir, standIn=declared("auto", may=["resume"], never=["promote"]))
    decide.write_setting(layer_dir, may=["resume", "land", "promote", "settle"])
    scope = standin.scope(ir.load(layer_dir))
    assert scope["may"] == ["resume"] and "land" in scope["suggests"] and "promote" not in scope["suggests"]


# ------------------------------------------------------------------------------------ the contract

def test_validate_accepts_each_new_action_and_checks_its_fields():
    good = {"summary": "Do it.", "explanation": "why", "instructions": "", "from_step": ""}
    extra = {"resume-with": {"instructions": "Add X."}, "settle": {"settle_as": "abandoned"},
             "promote": {"promote": "approve"}}
    assert set(decide.STOP_ACTIONS) < set(decide.ALL_ACTIONS) and len(decide.ALL_ACTIONS) == 11
    assert set(standin.KIND_OF) == set(decide.ALL_ACTIONS) - {"abandon", "ask-human"}
    assert set(standin.KIND_OF.values()) == set(standin.KINDS)
    for action in decide.ALL_ACTIONS:
        checked = decide.validate({**good, "action": action, **extra.get(action, {})}, STEPS, allowed=decide.ALL_ACTIONS)
        assert checked["action"] == action and checked["confidence"] is None
    checked = decide.validate({**good, "action": "settle", "settle_as": "landed", "settle_by": "1a2b3c4d", "confidence": 1},
                              STEPS, allowed=decide.ALL_ACTIONS)
    assert (checked["settle_as"], checked["settle_by"], checked["confidence"]) == ("landed", "1a2b3c4d", 1.0)
    assert isinstance(checked["confidence"], float)
    assert decide.validate({**good, "action": "promote", "promote": "refuse"}, STEPS, allowed=("promote",))["promote"] == "refuse"
    bad = [({**good, "action": "settle"}, "settle needs settle_as"),
           ({**good, "action": "settle", "settle_as": "gone"}, "settle needs settle_as"),
           ({**good, "action": "settle", "settle_as": "landed", "settle_by": "not a commit"}, "settle_by"),
           ({**good, "action": "promote"}, "promote needs promote"),
           ({**good, "action": "promote", "promote": "maybe"}, "promote needs promote"),
           ({**good, "action": "resume", "confidence": 1.5}, "confidence must be a number from 0 to 1"),
           ({**good, "action": "resume", "confidence": "high"}, "confidence must be a number from 0 to 1"),
           ({**good, "action": "resume", "confidence": True}, "confidence must be a number from 0 to 1")]
    for obj, why in bad:
        with pytest.raises(StepFailed, match=why):
            decide.validate(obj, STEPS, allowed=decide.ALL_ACTIONS)
    with pytest.raises(StepFailed, match="action must be one of resume, resume-with, abandon, ask-human"):
        decide.validate({**good, "action": "restart"}, STEPS)       # the in-run decider offers the four of before
    with pytest.raises(StepFailed, match="action must be one of land, ask-human"):
        decide.validate({**good, "action": "settle", "settle_as": "landed"}, STEPS, allowed=("land", "ask-human"))


# ------------------------------------------------------------------------------------ the record

def test_record_writes_the_run_file_the_layer_log_and_an_event(make_layer, tmp_path):
    layer_dir = stopped_run(make_layer, tmp_path)
    layer = ir.load(layer_dir)
    entry = standin.record(layer, {"type": "decision", "who": "stand-in", "via": "loop", "run": ADW_ID, "mode": "auto",
                                   "situation": "stopped", "kind": "resume", "action": "resume", "summary": "Again.",
                                   "why": "It looked flaky.", "confidence": 0.9, "read": ["why it stopped"],
                                   "status": "acted", "auto": True, "outcome": "resuming from test",
                                   "stop": "failed:x:test"})
    assert len(entry["id"]) == 8 and entry["at"]
    run_file = layer_dir / "agents" / ADW_ID / "decisions.jsonl"
    layer_log = layer_dir / "agents" / "stand-in" / "log.jsonl"
    assert json.loads(run_file.read_text()) == json.loads(layer_log.read_text()) == entry
    assert standin.entries(layer, run=ADW_ID) == standin.entries(layer) == [entry]
    types = [e for e in events.read(layer_dir / "agents" / ADW_ID / "events.jsonl") if e["type"].startswith("standin.")]
    assert [(e["type"], e["action"], e["status"], e["who"]) for e in types] == [("standin.action", "resume", "acted", "stand-in")]
    suggestion = standin.record(layer, {"type": "decision", "who": "stand-in", "via": "asked", "run": ADW_ID,
                                        "kind": "restart", "action": "restart", "status": "suggested"})
    types = [e["type"] for e in events.read(layer_dir / "agents" / ADW_ID / "events.jsonl") if e["type"].startswith("standin.")]
    assert types == ["standin.action", "standin.decision"]
    # a job has no run: the layer log only
    job = standin.record(layer, {"type": "decision", "who": "stand-in", "via": "loop", "job": "j1", "kind": "retry-job",
                                 "action": "retry-job", "status": "acted", "stop": "job:j1:0"})
    assert [e["id"] for e in standin.entries(layer)] == [entry["id"], suggestion["id"], job["id"]]
    assert [e["id"] for e in standin.entries(layer, run=ADW_ID)] == [entry["id"], suggestion["id"]]
    assert standin.entries(layer, since="2999-01-01T00:00:00+00:00") == []
    with layer_log.open("a") as handle:
        handle.write("not json\n[1, 2]\n")
    assert len(standin.entries(layer)) == 3                        # bad lines are skipped
    assert set(events.TYPES) >= {"standin.decision", "standin.action"}


def test_an_outcome_is_folded_into_its_entry(make_layer, tmp_path):
    layer_dir = stopped_run(make_layer, tmp_path)
    layer = ir.load(layer_dir)
    entry = standin.record(layer, {"type": "decision", "who": "stand-in", "run": ADW_ID, "action": "restart",
                                   "kind": "restart", "status": "acted", "outcome": "started 1a2b3c4d"})
    standin.outcome(layer, entry["id"], "stopped again for the same reason")
    for found in (standin.entries(layer), standin.entries(layer, run=ADW_ID)):
        assert [(e["id"], e["outcome"]) for e in found] == [(entry["id"], "stopped again for the same reason")]
    assert len((layer_dir / "agents" / ADW_ID / "decisions.jsonl").read_text().splitlines()) == 2


def test_why_holds_no_machine_path(make_layer, tmp_path):
    layer_dir = stopped_run(make_layer, tmp_path)
    layer = ir.load(layer_dir)
    state = state_module.load(layer_dir, ADW_ID)
    home = str(Path.home())
    entry = standin.record(layer, {"type": "decision", "who": "stand-in", "run": ADW_ID, "action": "resume", "kind": "resume",
                                   "status": "suggested", "summary": f"See {layer.app_root}/seed.py.",
                                   "why": f"The file {layer_dir}/specs/plan.md and {home}/notes say so. " + "x" * 3000,
                                   "outcome": f"wrote {layer.app_root}/out.txt"}, state=state)
    text = json.dumps(entry)
    assert str(tmp_path) not in text and home not in text
    assert "seed.py" in entry["summary"] and "~/notes" in entry["why"] and len(entry["why"]) <= 2000


# ------------------------------------------------------------------------------------ situations and eligibility

def test_situation_offers_the_actions_of_each_stop(make_layer, tmp_path):
    layer_dir = _layer(make_layer, tmp_path)
    set_ir(layer_dir, reviews=[{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""},
                               {"id": "auto", "kind": "auto", "status": "stable", "artefacts": [], "command": "review",
                                "reviewers": 1, "maxRetries": 0, "allowedClasses": ["chore"], "blockProtectedPaths": True,
                                "maxDiffLines": 100, "requireChecks": [], "ship": "merge", "deployCmd": ""}],
           adws=[ADW, {**ADW, "name": "zero", "review": "auto"}])
    layer = ir.load(layer_dir)

    def of(adw="demo", item=None, **fields):
        state = ADWState(adw_id=ADW_ID, adw=adw, prompt=PROMPT)
        for key, value in fields.items():
            setattr(state, key, value)
        return standin.situation(layer, state, item or {"run": ADW_ID})
    handoff = {"action": "handoff", "stopped_at": "test", "target": "agents/x/PR.md"}
    assert of(status="failed", ship=handoff) == ("stopped", ["resume", "resume-with", "restart", "settle", "ask-human"])
    assert of(status="failed") == ("failed", ["restart", "settle", "ask-human"])
    assert of(status="failed", ship={**handoff, "handoff_error": "boom"})[0] == "failed"
    assert of(status="failed", ship=handoff, item={"run": ADW_ID, "status": "died"})[0] == "failed"
    assert of(status="waiting") == ("waiting", ["answer", "ask-human"])
    assert of(status="waiting", promotion={"from": "dev", "to": "main"}) == ("promotion", ["promote", "ask-human"])
    over = ["integrate", "resume-with", "settle", "ask-human"]
    assert of(status="handed-over") == ("handed-over", over)
    passed = {"verdict": {"success": True, "summary": "fine", "issues": []}, "rounds": [{}]}
    assert of(status="handed-over", review=passed) == ("handed-over", over)             # a human review: no land
    assert of(adw="zero", status="handed-over", review=passed) == ("handed-over", ["land", *over])
    rejected = {"verdict": {"success": False, "summary": "no", "issues": []}, "rounds": [{}]}
    assert of(adw="zero", status="handed-over", review=rejected) == ("handed-over", over)
    job = {"job": {"id": "j1", "action": "add-layer", "failed_step": "gen", "reason": "timeout", "log": "…"}}
    assert of(item=job) == ("job", ["retry-job", "ask-human"])


def test_never_kinds_are_not_offered_and_an_empty_offer_makes_no_call(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["restart"], never=["resume", "settle"]))
    decider(layer_dir, "resume")
    layer = ir.load(layer_dir)
    state = state_module.load(layer_dir, ADW_ID)
    name, offered = standin.situation(layer, state, item_for())
    assert standin.offered_in_scope(offered, standin.scope(layer)) == ["restart", "ask-human"]
    result = standin.consider(layer, item_for())                  # the script answers `resume`: not offered
    assert result["status"] == "failed" and "action must be one of restart, ask-human" in result["entry"]["reason"]
    # every decision of the stop is in #:never: recorded as skipped, without a call
    set_ir(layer_dir, standIn=declared("auto", may=["answer"], never=["resume", "restart", "settle"]))
    calls = len(state_module.load(layer_dir, ADW_ID).agent_calls)
    result = standin.consider(ir.load(layer_dir), item_for(), force_suggest=True)
    assert result["status"] == "skipped" and result["entry"]["reason"] == "every decision here is in #:never"
    assert len(state_module.load(layer_dir, ADW_ID).agent_calls) == calls and spawned == []


def test_not_eligible_as_before(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=ALL))
    decider(layer_dir, "resume")
    path = layer_dir / "agents" / ADW_ID / "state.json"
    original = path.read_text()

    def with_state(**fields) -> dict:
        data = json.loads(original)
        data.update(fields)
        path.write_text(json.dumps(data))
        return standin.consider(ir.load(layer_dir), item_for(), force_suggest=True)
    reasons = []
    for fields in ({"fanout_parent": {"run": "aaaaaaaa", "step": "race", "index": 1}},
                   {"budget": {"stopped": {"limit": "run"}}},
                   {"usage": {"stopped": {"window": "five_hour"}}},
                   {"status": "running", "heartbeat": state_module.now(), "pid": os.getpid()}):
        result = with_state(**fields)
        assert result["status"] == "skipped", fields
        reasons.append((result["entry"] or result)["reason"])
    path.write_text(original)
    set_ir(layer_dir, adws=[{**ADW, "changes": "layer"}])
    result = standin.consider(ir.load(layer_dir), item_for(), force_suggest=True)
    assert result["status"] == "skipped"
    reasons.append(result["entry"]["reason"])
    assert len(set(reasons)) == 5 and all(reasons), reasons
    assert "fan-out" in reasons[0] and "spending limit" in reasons[1] and "usage limit" in reasons[2]
    assert "running" in reasons[3] and "changes the layer" in reasons[4]
    # a running run is not recorded: it will stop again, and that stop is looked at
    assert spawned == [] and [e["status"] for e in log(layer_dir)] == ["skipped"] * 4
    assert "decision" not in json.loads(original)


# ------------------------------------------------------------------------------------ every decision kind

AUTO_REVIEW = {"id": "auto", "kind": "auto", "status": "stable", "artefacts": [], "command": "review", "reviewers": 1,
               "maxRetries": 0, "allowedClasses": ["chore"], "blockProtectedPaths": True, "maxDiffLines": 100,
               "requireChecks": [], "ship": "merge", "deployCmd": ""}


def zero_touch(make_layer, tmp_path, app_repo, stand_in, keep: str = "always") -> Path:
    """The ADW `demo` with an automatic review that merges for class chore only; `ready` exists, so
    the check passes. The application's checkout is on a branch of its own, so main can be merged into."""
    layer_dir = _layer(make_layer, tmp_path, adw={**ADW, "review": "auto"})
    data = json.loads((layer_dir / "agentic-layer.json").read_text())
    data["environments"][0]["keep"] = keep
    set_ir(layer_dir, reviews=[AUTO_REVIEW], environments=data["environments"], standIn=stand_in)
    (tmp_path / "ready").touch()
    import subprocess
    subprocess.run(["git", "-C", str(app_repo), "checkout", "-q", "-b", "work"], check=True, capture_output=True)
    return layer_dir


def handed_over(layer_dir: Path, adw_id: str = ADW_ID) -> dict:
    """A run of class bug: the review passes, the gate refuses the class, the run is handed over."""
    os.environ["AL_DECIDER"] = "off"
    try:
        assert run(layer_dir, "--class", "bug", adw_id=adw_id) == 0
    finally:
        del os.environ["AL_DECIDER"]
    state = _state(layer_dir, adw_id)
    assert state["status"] == "handed-over" and "class-not-allowed:bug" in state["ship"]["handed_over"]
    return state


def start_in_process(monkeypatch, decider_off: bool = True) -> list[list[str]]:
    """launch._spawn runs the started run in this process (its own command line after the script)."""
    seen: list[list[str]] = []

    def fake(argv, **kw):
        seen.append(list(argv))
        assert argv[:3] == ["uv", "run", "--quiet"] and argv[3].endswith("adw_demo.py")
        if decider_off:
            os.environ["AL_DECIDER"] = "off"
        try:
            main("demo", Path(kw["cwd"]), argv[4:])
        finally:
            os.environ.pop("AL_DECIDER", None)
    monkeypatch.setattr(launch, "_spawn", fake)
    return seen


def test_resume_in_scope_resumes_and_records_an_automatic_action(make_layer, tmp_path, monkeypatch):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["resume"]))
    decider(layer_dir, "resume")
    resumed = resume_in_process(monkeypatch, before=lambda: (tmp_path / "ready").touch())
    result = standin.consider(ir.load(layer_dir), item_for())
    assert (result["status"], result["action"], result["kind"]) == ("acted", "resume", "resume")
    assert len(resumed) == 1 and resumed[0][2:5] == ["--resume", "--adw-id", ADW_ID]
    (entry,) = [e for e in log(layer_dir) if e["type"] == "decision"]
    assert (entry["who"], entry["via"], entry["status"], entry["auto"], entry["outcome"]) == (
        "stand-in", "loop", "acted", True, "resuming from test")
    assert entry["confidence"] == 0.9 and entry["situation"] == "stopped" and "why it stopped" in entry["read"]
    assert "EXPLANATION-MARKER" in entry["why"] and entry["stop"].startswith("failed:")
    final = _state(layer_dir)
    assert final["status"] == "passed" and final["decision"]["status"] == "resumed"
    assert final["decision"]["by"] == "stand-in" and final["decision"]["rounds"] == 1
    assert [c["tier_from"] for c in final["agent_calls"] if c["step"] == "decide"] == ["stand-in"]
    types = [e["type"] for e in events.read(layer_dir / "agents" / ADW_ID / "events.jsonl")]
    assert "standin.action" in types and "decision.resume" in types
    assert standin.status(ir.load(layer_dir))["counts"] == {"today": 1, "hour": 1, "waiting": 0}


def test_restart_starts_the_request_again_and_records_replaces(make_layer, tmp_path, monkeypatch):
    layer_dir = _layer(make_layer, tmp_path)
    set_ir(layer_dir, standIn=declared("auto", may=["restart"]))
    os.environ["AL_DECIDER"] = "off"
    try:
        assert run(layer_dir, "--dry-run") == 1                    # a dry run: the restart is one too
    finally:
        del os.environ["AL_DECIDER"]
    decider(layer_dir, "restart")
    started = start_in_process(monkeypatch)
    (tmp_path / "ready").touch()
    result = standin.consider(ir.load(layer_dir), item_for())
    assert result["status"] == "acted" and result["action"] == "restart"
    (argv,) = started
    new = argv[argv.index("--adw-id") + 1]
    assert "--dry-run" in argv and argv[argv.index("--replaces") + 1] == ADW_ID and "--prompt-file" in argv
    assert result["entry"]["outcome"] == f"started {new}" and result["entry"]["auto"] is True
    fresh = _state(layer_dir, new)
    assert fresh["replaces"] == ADW_ID and fresh["dry_run"] is True and fresh["status"] == "passed"
    assert fresh["prompt"] == PROMPT
    old = _state(layer_dir)
    assert old["status"] == "failed" and old["decision"]["status"] == "applied"
    assert old["decision"]["outcome"] == f"started {new}"
    assert standin.consider(ir.load(layer_dir), item_for())["status"] == "seen"


def test_answer_resumes_a_waiting_run(make_layer, app_repo, monkeypatch):
    import test_script_waiting_journal as waiting

    layer_dir = waiting._layer(make_layer, app_repo)
    set_ir(layer_dir, standIn=declared("auto", may=["answer"]))
    run_id = waiting.ADW_ID
    os.environ["AL_DECIDER"] = "off"
    try:
        assert main("watch", layer_dir, ["wait", "--adw-id", run_id]) == 75
    finally:
        del os.environ["AL_DECIDER"]
    before = _state(layer_dir, run_id)
    assert before["status"] == "waiting"
    (Path(before["worktree"]) / "ready").touch()                  # what it waited for is there
    decider(layer_dir, "answer")
    resumed = resume_in_process(monkeypatch)
    result = standin.consider(ir.load(layer_dir), item_for(run_id))
    assert (result["status"], result["action"]) == ("acted", "answer")
    assert result["entry"]["outcome"] == "resumed at run" and result["entry"]["situation"] == "waiting"
    assert len(resumed) == 1 and resumed[0][1].endswith("adw_watch.py")
    final = _state(layer_dir, run_id)
    assert final["status"] == "handed-over" or final["status"] == "passed"
    assert next(r for r in final["steps"] if r["name"] == "run")["status"] == "passed"
    assert "## Decision (the stand-in)\nANSWER-MARKER the file is in place." in final["prompt"]


def test_integrate_starts_a_run_that_names_the_branch(make_layer, tmp_path, app_repo, spawned):
    layer_dir = zero_touch(make_layer, tmp_path, app_repo, declared("auto", may=["integrate"]))
    state = handed_over(layer_dir)
    decider(layer_dir, "integrate")
    result = standin.consider(ir.load(layer_dir), item_for(landing={"state": "partly", "differ": 2, "target": "main"}))
    assert (result["status"], result["action"]) == ("acted", "integrate")
    (call,) = spawned
    assert call[0] == "start" and "--replaces" not in call[1] and "--class" in call[1]
    new = call[1][call[1].index("--adw-id") + 1]
    assert result["entry"]["outcome"] == f"integration run {new} started" and "landing" in result["entry"]["read"]
    request = (layer_dir / "agents" / new / "request.md").read_text()
    assert request.startswith(f"Integrate the work of run {ADW_ID} (branch {state['branch']}) onto the current main "
                              "and make every check pass.\n\nThe original request:\n\n" + PROMPT)
    assert _state(layer_dir)["status"] == "handed-over"


def test_land_asks_the_gate_again(make_layer, tmp_path, app_repo, monkeypatch):
    import subprocess

    layer_dir = zero_touch(make_layer, tmp_path, app_repo, declared("auto", may=["land"]))
    head = lambda: subprocess.run(["git", "-C", str(app_repo), "rev-parse", "main"], capture_output=True,  # noqa: E731
                                  text=True, check=True).stdout.strip()
    main_before = head()
    handed_over(layer_dir)
    handed_over(layer_dir, adw_id="dec1de02")
    assert head() == main_before
    decider(layer_dir, "land")
    resumed = resume_in_process(monkeypatch)
    # the class is still refused: the gate says no again, nothing is merged
    result = standin.consider(ir.load(layer_dir), item_for("dec1de02"))
    assert (result["status"], result["action"], result["entry"]["outcome"]) == ("acted", "land", "asked the gate again")
    again = _state(layer_dir, "dec1de02")
    assert again["status"] == "handed-over" and "class-not-allowed:bug" in again["ship"]["handed_over"]
    assert again["ship"]["shipped"] is False and head() == main_before
    assert next(r for r in again["steps"] if r["name"] == "ship")["attempts"] == 2
    # a person allows the class in the layer: now the same gate lets it through
    set_ir(layer_dir, reviews=[{**AUTO_REVIEW, "allowedClasses": ["chore", "bug"]}])
    result = standin.consider(ir.load(layer_dir), item_for())
    assert (result["status"], result["action"]) == ("acted", "land")
    final = _state(layer_dir)
    assert final["status"] == "passed" and final["ship"]["shipped"] is True and final["ship"]["action"] == "merge"
    assert head() != main_before and len(resumed) == 2
    assert {r["name"]: r["attempts"] for r in final["steps"]}["build"] == 1     # only the ship step ran again


def test_settle_writes_a_settlement_as_the_stand_in(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["settle"]))
    decider(layer_dir, "settle")
    path = layer_dir / "agents" / ADW_ID / "settlement.json"
    result = standin.consider(ir.load(layer_dir), item_for())
    assert (result["status"], result["action"], result["entry"]["outcome"]) == ("acted", "settle", "settled as abandoned")
    record = json.loads(path.read_text())
    assert set(record) == {"as", "by", "note", "who", "at", "via"}
    assert (record["as"], record["by"], record["who"], record["via"]) == ("abandoned", "", "stand-in", "stand-in")
    assert record["note"] == "Nothing more to do: the request was dropped." and record["at"].endswith("Z")
    assert oct(path.stat().st_mode & 0o777) == "0o600" and spawned == []
    # a second settlement keeps the first under `previous`
    state = state_module.load(layer_dir, ADW_ID)
    again = standin.settle(ir.load(layer_dir), state, "superseded", by="ffffffff", note="x\ny " + "z" * 600)
    assert again["by"] == "" and "\n" not in again["note"] and len(again["note"]) == 500
    assert json.loads(path.read_text())["previous"] == record


def _worktrees(layer_dir: Path) -> str:
    app = ir.load(layer_dir).app_root
    return subprocess.run(["git", "-C", str(app), "worktree", "list", "--porcelain"], capture_output=True, text=True,
                          check=True, timeout=30).stdout


def test_settling_removes_the_worktree_and_keeps_the_record(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["settle"]))
    tree = layer_dir / "trees" / ADW_ID
    assert tree.is_dir() and ADW_ID in _worktrees(layer_dir)
    decider(layer_dir, "settle")
    result = standin.consider(ir.load(layer_dir), item_for())
    assert (result["status"], result["entry"]["outcome"]) == ("acted", "settled as abandoned")
    assert not tree.exists() and f"/{ADW_ID}\n" not in _worktrees(layer_dir)
    run_dir = layer_dir / "agents" / ADW_ID
    assert (run_dir / "state.json").is_file()
    record = json.loads((run_dir / "settlement.json").read_text())
    assert set(record) == {"as", "by", "note", "who", "at", "via"}


def test_an_unsettled_run_keeps_its_worktree(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path)
    tree = layer_dir / "trees" / ADW_ID
    assert tree.is_dir()
    result = standin.consider(ir.load(layer_dir), item_for())
    assert result["status"] == "off" and result["action"] == ""
    assert tree.is_dir() and not (layer_dir / "agents" / ADW_ID / "settlement.json").exists()


def test_promote_records_the_stand_in_and_continues(make_layer, app_repo, monkeypatch):
    import promotion_fixtures as pf

    def waiting_promotion(approvers: int) -> tuple[Path, str]:
        layer_dir = pf.sprint_layer(make_layer, app_repo, promotion={"approvers": approvers})
        set_ir(layer_dir, standIn=declared("auto", may=["promote"]))
        pf.deliver(layer_dir, app_repo, "9a000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
        assert pf.promote(layer_dir, "sprint", "main")[0] == 75
        decider(layer_dir, "promote")
        return layer_dir, pf.open_id(layer_dir)

    layer_dir, pid = waiting_promotion(2)
    continued: list[tuple[list[str], int]] = []

    def fake(argv, cwd, log_path):
        assert argv[0] == sys.executable and argv[1:4] == ["adws/promote.py", "run", "sprint"] and "--resume" in argv
        continued.append((argv, pf.promote(Path(cwd), "sprint", "main")[0]))
    monkeypatch.setattr(standin, "_spawn_detached", fake)
    result = standin.consider(ir.load(layer_dir), item_for(pid))
    assert (result["status"], result["action"]) == ("acted", "promote")
    assert result["entry"]["outcome"] == "recorded approve as stand-in; the promotion continues"
    assert result["entry"]["situation"] == "promotion"
    lines = [json.loads(line) for line in (layer_dir / "promotions" / pid / "acceptance.jsonl").read_text().splitlines()]
    assert [(line["by"], line["decision"]) for line in lines] == [("stand-in", "approve")]
    # two approvers are declared: the stand-in's approval is one, the promotion still waits
    assert [code for _argv, code in continued] == [75]
    assert pf.state_of(layer_dir, pid)["status"] == "waiting"


def test_retry_job_is_returned_for_the_dashboard_to_perform(make_layer, tmp_path, spawned):
    layer_dir = _layer(make_layer, tmp_path)
    set_ir(layer_dir, standIn=declared("auto", may=["retry-job"]))
    decider(layer_dir, "retry-job")
    before = sorted(p.name for p in (layer_dir / "agents").iterdir()) if (layer_dir / "agents").is_dir() else []
    item = {"job": {"id": "job-20261117-1", "action": "add-layer", "failed_step": "generate", "reason": "timed out",
                    "log": "generate: timed out after 600 s", "retries": 0}, "dry_run": True}
    result = standin.consider(ir.load(layer_dir), item)
    assert (result["status"], result["action"]) == ("acted", "retry-job")
    assert result["perform"] == {"kind": "retry-job", "job": "job-20261117-1", "from": "generate"}
    assert result["entry"]["job"] == "job-20261117-1" and result["entry"]["outcome"] == "retry requested"
    assert result["entry"]["stop"] == "job:job-20261117-1:0" and "job log" in result["entry"]["read"]
    after = sorted(p.name for p in (layer_dir / "agents").iterdir())
    assert [name for name in after if name not in before] == ["stand-in"]          # no run directory
    call = layer_dir / "agents" / "stand-in" / "jobs" / "job-20261117-1" / "decide"
    assert (call / "prompt.md").is_file() and (call / "raw.jsonl").is_file() and (call / "output.txt").is_file()
    assert standin.consider(ir.load(layer_dir), item)["status"] == "seen"
    assert standin.consider(ir.load(layer_dir), {**item, "job": {**item["job"], "retries": 1}})["status"] == "acted"
    assert spawned == []


# ------------------------------------------------------------------------------------ scope and suggestions

def test_a_kind_outside_may_is_only_suggested(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["resume"]))
    decider(layer_dir, "restart")
    result = standin.consider(ir.load(layer_dir), item_for())
    assert (result["status"], result["action"], result["kind"]) == ("suggested", "restart", "restart")
    d = _state(layer_dir)["decision"]
    assert (d["status"], d["action"], d["kind"], d["by"], d["mode"], d["situation"]) == (
        "suggested", "restart", "restart", "stand-in", "auto", "stopped")
    assert d["confidence"] == 0.7 and d["read"] and len(d["code"]) >= 12
    assert spawned == [] and result["entry"]["auto"] is False
    assert "- action: restart" in (layer_dir / "agents" / ADW_ID / "decision.md").read_text()
    # the same kind, narrowed away on this machine, is a suggestion too
    set_ir(layer_dir, standIn=declared("auto", may=["resume", "restart"]))
    decide.write_setting(layer_dir, may=["resume"])
    assert standin.consider(ir.load(layer_dir), item_for(), force_suggest=True)["status"] == "suggested"
    assert standin.status(ir.load(layer_dir))["counts"]["waiting"] == 1


def test_suggest_mode_files_a_suggestion_for_every_kind(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("suggest", may=ALL))
    for name, action in (("resume", "resume"), ("resume-with", "resume-with"), ("restart", "restart"), ("settle", "settle")):
        decider(layer_dir, name)
        result = standin.consider(ir.load(layer_dir), item_for(), force_suggest=name != "resume")
        assert (result["status"], result["action"]) == ("suggested", action), name
        assert _state(layer_dir)["decision"]["status"] == "suggested"
    assert _state(layer_dir)["decision"]["settle_as"] == "abandoned"
    # a call that answers nonsense changes nothing but the decision's status: the item stays with a person
    steps_before = _state(layer_dir)["steps"]
    decider(layer_dir, "bad")
    result = standin.consider(ir.load(layer_dir), item_for(), force_suggest=True)
    assert result["status"] == "failed" and "merge-it-now" in result["entry"]["reason"]
    state = _state(layer_dir)
    assert state["decision"]["status"] == "failed" and "action" not in state["decision"]
    assert state["status"] == "failed" and state["steps"] == steps_before and spawned == []
    assert not (layer_dir / "agents" / ADW_ID / "settlement.json").exists()


def test_ask_runs_in_suggest_mode_whatever_the_mode_and_changes_nothing_else(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path)                 # no declaration, no local setting: off
    decider(layer_dir, "resume")
    before = _state(layer_dir)
    assert standin.consider(ir.load(layer_dir), item_for())["status"] == "off"
    assert _state(layer_dir) == before and log(layer_dir) == []
    result = standin.consider(ir.load(layer_dir), item_for(), force_suggest=True, via="asked")
    assert (result["status"], result["action"], result["entry"]["via"]) == ("suggested", "resume", "asked")
    after = _state(layer_dir)
    assert after["decision"]["status"] == "suggested" and after["decision"]["mode"] == "suggest"
    for key in ("status", "steps", "ship", "prompt", "branch", "ended", "checks"):
        assert after[key] == before[key], key
    assert spawned == [] and not (layer_dir / "agents" / ADW_ID / "settlement.json").exists()
    assert decide.mode_for(ir.load(layer_dir), None) == ("off", "default")


def test_go_applies_a_non_resume_suggestion_through_decide_answer(make_layer, tmp_path, spawned):
    sink = smtp_sink.Sink()
    try:
        layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("suggest", may=ALL))
        _mail_env(layer_dir, sink)
        for index, via in enumerate(("cli", "dashboard", "mail")):
            run_id = f"dec1de1{index}"
            os.environ["AL_DECIDER"] = "off"
            try:
                assert run(layer_dir, adw_id=run_id) == 1
            finally:
                del os.environ["AL_DECIDER"]
            decider(layer_dir, "restart")
            layer = ir.load(layer_dir)
            assert standin.consider(layer, item_for(run_id))["status"] == "suggested"
            code = _state(layer_dir, run_id)["decision"]["code"]
            if via == "mail":
                assert decide.answer(layer, run_id, "go", "", via="mail", code="wrong", sender="ops@example.org")["reason"] == "bad-code"
                assert decide.answer(layer, run_id, "go", "", via="mail", code=code, sender="x@example.org")["reason"] == "bad-sender"
                result = decide.answer(layer, run_id, "go", "", via="mail", code=code, sender="ops@example.org")
            else:
                result = decide.answer(layer, run_id, "go", "", via=via)
            assert result["status"] == "applied" and result["message"].startswith("started "), result
            kind, argv = spawned[-1]
            assert kind == "start" and argv[argv.index("--replaces") + 1] == run_id
            found = standin.entries(layer, run=run_id)
            assert [(e["type"], e["who"], e["via"], e["status"]) for e in found] == [
                ("decision", "stand-in", "loop", "suggested"), ("answer", "person", via, "acted"),
                ("decision", "person", via, "acted")]
            assert found[-1]["auto"] is False and found[1]["answer"] == "go"
            assert decide.answer(layer, run_id, "go", "", via="cli")["reason"] == "not-waiting"
        assert len(spawned) == 3
        assert decide.answer(ir.load(layer_dir), ADW_ID, "go", "", via="stand-in")["reason"] == "own-work"
    finally:
        sink.close()


def test_alter_adds_the_text(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("suggest", may=ALL))
    layer = ir.load(layer_dir)
    decider(layer_dir, "restart")
    assert standin.consider(layer, item_for())["status"] == "suggested"
    result = decide.answer(layer, ADW_ID, "alter", "Use the v2 API.", via="dashboard")
    assert result["status"] == "applied"
    new = spawned[-1][1][spawned[-1][1].index("--adw-id") + 1]
    request = (layer_dir / "agents" / new / "request.md").read_text()
    assert request == PROMPT + "\n\n## Decision (answered by a person)\nThe person adds: Use the v2 API.\n"
    # settle: the text is the note
    os.environ["AL_DECIDER"] = "off"
    try:
        assert run(layer_dir, adw_id="dec1de02") == 1
    finally:
        del os.environ["AL_DECIDER"]
    decider(layer_dir, "settle")
    assert standin.consider(layer, item_for("dec1de02"))["status"] == "suggested"
    assert decide.answer(layer, "dec1de02", "alter", "Dropped: the customer left.", via="cli")["status"] == "applied"
    record = json.loads((layer_dir / "agents" / "dec1de02" / "settlement.json").read_text())
    assert (record["as"], record["note"], record["who"], record["via"]) == ("abandoned", "Dropped: the customer left.", "person", "cli")


def test_alter_is_refused_for_land_and_promote(make_layer, tmp_path, app_repo, spawned):
    layer_dir = zero_touch(make_layer, tmp_path, app_repo, declared("suggest", may=ALL))
    handed_over(layer_dir)
    decider(layer_dir, "land")
    layer = ir.load(layer_dir)
    assert standin.consider(layer, item_for())["status"] == "suggested"
    before = _state(layer_dir)
    result = decide.answer(layer, ADW_ID, "alter", "but squash it", via="dashboard")
    assert (result["status"], result["reason"]) == ("refused", "bad-answer")
    assert result["message"] == "alter does not apply to land; answer go or reject"
    assert _state(layer_dir) == before and spawned == []
    state = state_module.load(layer_dir, ADW_ID)
    state.decision.update(action="promote", promote="approve")
    state.save(layer_dir)
    assert decide.answer(layer, ADW_ID, "alter", "x", via="cli")["message"] == "alter does not apply to promote; answer go or reject"


def test_reject_leaves_the_run_with_the_person(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["resume"]))
    layer = ir.load(layer_dir)
    decider(layer_dir, "settle")
    assert standin.consider(layer, item_for())["status"] == "suggested"
    result = decide.answer(layer, ADW_ID, "reject", "", via="dashboard")
    assert result["status"] == "rejected"
    state = _state(layer_dir)
    assert state["decision"]["status"] == "rejected" and state["status"] == "failed"
    assert not (layer_dir / "agents" / ADW_ID / "settlement.json").exists() and spawned == []
    assert [(e["type"], e["status"]) for e in standin.entries(layer, run=ADW_ID)] == [
        ("decision", "suggested"), ("answer", "refused")]
    assert standin.status(layer)["counts"]["waiting"] == 0
    # the person said no: the stand-in does not look at this stop again, and never acts on it
    assert standin.consider(layer, item_for())["status"] == "seen"
    decider(layer_dir, "resume")
    result = standin.consider(layer, item_for(), force_suggest=True)
    assert (result["status"], result["entry"]["reason"]) == ("refused", standin.TOUCHED)


# ------------------------------------------------------------------------------------ limits, pause, once

def test_limit_per_run_counts_over_replacements_then_escalates_with_the_history(make_layer, tmp_path, monkeypatch):
    sink = smtp_sink.Sink()
    try:
        layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared(
            "auto", may=["restart"], maxActionsPerRun=2, escalateTo=["dashboard", "notify"]))
        _mail_env(layer_dir, sink)
        decider(layer_dir, "restart")
        started = start_in_process(monkeypatch)                    # `ready` never appears: each restart stops again
        current = ADW_ID
        for _round in range(2):
            result = standin.consider(ir.load(layer_dir), item_for(current))
            assert result["status"] == "acted", result
            current = started[-1][started[-1].index("--adw-id") + 1]
            assert _state(layer_dir, current)["status"] == "failed"
        result = standin.consider(ir.load(layer_dir), item_for(current))
        assert result["status"] == "escalated"
        assert result["entry"]["reason"] == "the stand-in acted 2 times on this run (limit 2)"
        assert len(started) == 2
        d = _state(layer_dir, current)["decision"]
        assert d["status"] == "suggested" and d["held"] == result["entry"]["reason"]   # a person may still say go
        body = sink.messages[-1]["data"].decode("utf-8", errors="replace")
        assert "limit 2" in body and body.count("restart (acted)") == 2
        assert str(tmp_path) not in body
    finally:
        sink.close()


def test_limit_per_hour(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["settle"], maxPerHour=1))
    os.environ["AL_DECIDER"] = "off"
    try:
        assert run(layer_dir, adw_id="dec1de02") == 1
    finally:
        del os.environ["AL_DECIDER"]
    decider(layer_dir, "settle")
    layer = ir.load(layer_dir)
    assert standin.consider(layer, item_for())["status"] == "acted"
    result = standin.consider(layer, item_for("dec1de02"))
    assert (result["status"], result["entry"]["reason"]) == ("escalated", "the stand-in acted 1 times this hour (limit 1)")
    assert not (layer_dir / "agents" / "dec1de02" / "settlement.json").exists()
    assert standin.limit_reached(layer, state_module.load(layer_dir, "dec1de02")) == result["entry"]["reason"]


def test_pause_stops_actions_and_suggestions(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["resume"]))
    decider(layer_dir, "resume")
    decide.write_setting(layer_dir, paused=True)
    layer = ir.load(layer_dir)
    before = _state(layer_dir)
    result = standin.consider(layer, item_for())
    assert (result["status"], result["reason"]) == ("skipped", "paused")
    assert _state(layer_dir) == before and log(layer_dir) == [] and spawned == []
    assert standin.limit_reached(layer, state_module.load(layer_dir, ADW_ID)) == "paused"
    # the decider inside a run is paused too
    assert run(layer_dir, adw_id="dec1de02") == 1
    assert _state(layer_dir, "dec1de02")["decision"] == {"skipped": "the stand-in is paused"}
    decide.write_setting(layer_dir, paused=False)
    assert standin.consider(layer, item_for())["status"] == "acted" and len(spawned) == 1


def test_a_stop_is_considered_once(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("suggest"))
    decider(layer_dir, "resume")
    results: list[dict] = []

    def work() -> None:
        results.append(standin.consider(ir.load(layer_dir), item_for()))
    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(r["status"] for r in results) == ["seen", "suggested"]
    assert len([e for e in log(layer_dir) if e["type"] == "decision"]) == 1
    assert len([c for c in _state(layer_dir)["agent_calls"] if c["step"] == "decide"]) == 1
    # the decider inside a run counts as the one look at that stop
    assert run(layer_dir, adw_id="dec1de02") == 1
    assert _state(layer_dir, "dec1de02")["decision"]["status"] == "suggested"
    assert "entry_pending" not in _state(layer_dir, "dec1de02")["decision"]
    (entry,) = standin.entries(ir.load(layer_dir), run="dec1de02")
    assert entry["via"] == "run" and entry["stop"] == standin.fingerprint(state_module.load(layer_dir, "dec1de02"))
    assert standin.consider(ir.load(layer_dir), item_for("dec1de02"))["status"] == "seen"


def test_the_same_reason_again_is_marked_on_the_earlier_action(make_layer, tmp_path, monkeypatch):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["resume"]))
    decider(layer_dir, "resume")

    def fake(layer, state):                                       # the resume stops again: `ready` never appears
        os.environ["AL_DECIDER"] = "off"
        try:
            main("demo", Path(layer.layer_dir), decide.resume_argv(layer, state)[2:-2])
        finally:
            del os.environ["AL_DECIDER"]
    monkeypatch.setattr(decide, "spawn_resume", fake)
    layer = ir.load(layer_dir)
    first = standin.consider(layer, item_for())
    assert first["status"] == "acted" and first["entry"]["outcome"] == "resuming from test"
    time.sleep(0.01)
    second = standin.consider(layer, item_for())
    assert second["status"] == "acted"                            # a new stop: it counts like every action
    found = [e for e in standin.entries(layer, run=ADW_ID) if e["status"] == "acted"]
    assert [e["outcome"] for e in found][0] == standin.SAME_REASON and len(found) == 2
    assert standin.consider(layer, item_for())["status"] == "acted"
    fourth = standin.consider(layer, item_for())                  # the declared limit of three actions on one run
    assert (fourth["status"], fourth["entry"]["reason"]) == ("escalated", "the stand-in acted 3 times on this run (limit 3)")
    assert [e["outcome"] for e in standin.entries(layer, run=ADW_ID) if e["status"] == "acted"] == [standin.SAME_REASON] * 3


def test_escalation_notifies_when_declared(make_layer, tmp_path, spawned):
    sink = smtp_sink.Sink()
    try:
        layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("auto", may=["resume"], escalateTo=["dashboard", "notify"]))
        _mail_env(layer_dir, sink)
        decider(layer_dir, "ask-human")
        result = standin.consider(ir.load(layer_dir), item_for())
        assert (result["status"], result["action"]) == ("escalated", "ask-human")
        (message,) = sink.messages
        assert "The stand-in hands run dec1de01 to a person" in message["data"].decode("utf-8", errors="replace")
        assert _state(layer_dir)["decision"]["status"] == "decided"
        # without `notify` in #:escalate-to nothing is sent
        set_ir(layer_dir, standIn=declared("auto", may=["resume"]))
        assert standin.consider(ir.load(layer_dir), item_for(), force_suggest=True)["status"] == "escalated"
        assert len(sink.messages) == 1
    finally:
        sink.close()
