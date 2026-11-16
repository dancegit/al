"""The stand-in decider (decide.py): when a run stops and is handed to a person, one more agent call
on the heavy tier chooses resume, resume-with, abandon or ask-human. In `auto` mode a resume starts
at once (at most MAX_AUTO_ROUNDS per run); in `suggest` mode the choice goes to the person, who
answers go, alter, explain or reject. Git is real, the agent is the mock, the SMTP server is
smtp_sink.py on 127.0.0.1."""
from __future__ import annotations

import email
import email.policy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smtp_sink  # noqa: E402

from adw_modules import agents, events, ir, state as state_module  # noqa: E402
from adw_modules.errors import LayerError, StepFailed  # noqa: E402
from adw_modules.state import ADWState  # noqa: E402

DATA = Path(__file__).resolve().parent / "data" / "decide"
ADW_ID = "dec1de01"
PROMPT = "Add a pay band to each worker."
MOCK = {"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}
TEXT = {"template": "build", "output": "text", "description": "", "file": "", "focus": [], "fabric": [],
        "relevant": [], "validation": []}
MAIL = {"id": "mail", "kind": "email", "status": "stable", "events": ["needs-human", "failed"],
        "toEnv": "AL_NOTIFY_TO", "fromEnv": "AL_NOTIFY_FROM", "smtpUrlEnv": "AL_NOTIFY_SMTP_URL", "timeout": 5}
TO, FROM = "ops@example.org", "al@example.org"


@pytest.fixture(autouse=True)
def no_settings_of_the_person(tmp_path, monkeypatch):
    """The person's own settings never reach a test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_NOTIFY_TO", "AL_NOTIFY_FROM", "AL_NOTIFY_SMTP_URL", "AL_DASHBOARD_URL", "ADW_FANOUT_PARENT",
                "AL_DECIDER", "AL_DECIDE_IMAP_URL", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST"):
        monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------------------------------------ state and agent call

def test_state_json_has_no_decision_key_when_unused(tmp_path):
    state = ADWState(adw_id=ADW_ID, adw="demo", prompt="p")
    path = state.save(tmp_path)
    assert "decision" not in json.loads(path.read_text())
    state.decision = {"mode": "suggest", "action": "resume", "status": "suggested", "answers": []}
    state.save(tmp_path)
    assert json.loads(path.read_text())["decision"]["status"] == "suggested"
    assert state_module.load(tmp_path, ADW_ID).decision == state.decision


def test_run_prompt_records_a_call_like_run_agent(make_layer):
    layer_dir = make_layer({"agents": [MOCK], "adws": [{"name": "demo", "agent": "mock", "review": "auto",
                                                         "environment": [], "steps": []}]})
    layer = ir.load(layer_dir)
    state = ADWState(adw_id=ADW_ID, adw="demo", prompt="p", dry_run=True)
    command = {"name": "decide", "template": "decide", "output": "json", "modelSet": "heavy", "isHop": False}
    stack = SimpleNamespace(env={}, read_only=False)
    with events.recording(layer_dir, ADW_ID):
        output = agents.run_prompt(layer, state, "decide", command, "Decide.", "heavy", stack)
    assert json.loads(output)["action"] == "ask-human"
    directory = layer_dir / "agents" / ADW_ID / "decide"
    assert (directory / "prompt.md").read_text() == "Decide." and (directory / "raw.jsonl").is_file()
    (call,) = state.agent_calls
    assert (call["step"], call["tier"], call["status"], call["agent_kind"]) == ("decide", "heavy", "passed", "mock")
    types = [e["type"] for e in events.read(layer_dir / "agents" / ADW_ID / "events.jsonl")]
    assert types == ["agent.start", "agent.end"]


# ------------------------------------------------------------------------------------ helpers

from adw_modules import decide  # noqa: E402
from adw_modules.runner import main  # noqa: E402

STEPS = ["plan", "build", "test", "review", "ship"]
ADW = {"name": "demo", "agent": "mock", "review": "human", "environment": ["wt"], "input": ["cli"],
       "trigger": ["manual"], "classes": [],
       "steps": [{"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
                 {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
                 {"name": "test", "kind": "check", "cmd": "test", "retries": 0, "collect": []},
                 {"name": "review", "kind": "review"},
                 {"name": "ship", "kind": "ship"}]}


@pytest.fixture
def sink():
    server = smtp_sink.Sink()
    yield server
    server.close()


def _layer(make_layer, tmp_path: Path, notifiers=(MAIL,), adw=None) -> Path:
    """The ADW `demo` on the mock agent: plan, build (commits seed.py), a check `test` that passes
    only once <tmp>/ready exists, a human review and a ship step. Without `ready` the run stops at
    `test` and is handed over with PR.md."""
    layer = make_layer({
        "agents": [MOCK],
        "commands": [{"name": "plan", "modelSet": "base", **TEXT}, {"name": "build", "modelSet": "base", **TEXT},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json", "description": "",
                      "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}],
        "codebase": {"commands": [{"name": "test", "run": f"test -f {tmp_path / 'ready'}", "timeout": 20}]},
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 28300,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "adws": [adw or ADW],
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "budget": None,
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "notifiers": list(notifiers),
    })
    _mock(layer, "build", files={"seed.py": "PAY_BANDS = {'hca': 'Band 3'}\n"}, commit=True, output="built")
    return layer


def _mock(layer: Path, step: str, **script) -> None:
    path = layer / "mock" / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(script), encoding="utf-8")


def _decider(layer: Path, name: str) -> None:
    (layer / "mock").mkdir(exist_ok=True)
    (layer / "mock" / "decide.json").write_text((DATA / f"{name}.json").read_text(), encoding="utf-8")


def _mail_env(layer: Path, server: smtp_sink.Sink) -> None:
    (layer / "local.env").write_text(f"AL_NOTIFY_TO={TO}\nAL_NOTIFY_FROM={FROM}\n"
                                     f"AL_NOTIFY_SMTP_URL=smtp://127.0.0.1:{server.port}\n", encoding="utf-8")


def _state(layer: Path, adw_id: str = ADW_ID) -> dict:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text())


def _run(layer: Path, *flags: str, adw_id: str = ADW_ID) -> int:
    return main("demo", layer, [PROMPT, "--adw-id", adw_id, *flags])


def _parsed(message: dict):
    return email.message_from_bytes(message["data"], policy=email.policy.default)


def _body(message: dict) -> str:
    return _parsed(message).get_content().replace("\r\n", "\n")


@pytest.fixture
def spawned(monkeypatch):
    """decide.spawn_resume records the command line it would start, and starts nothing."""
    calls: list[list[str]] = []
    monkeypatch.setattr(decide, "spawn_resume", lambda layer, state: calls.append(decide.resume_argv(layer, state)))
    return calls


def _resume_in_process(monkeypatch, before=None) -> list[dict]:
    """decide.spawn_resume runs the resume in this process (its own command line, minus the
    interpreter and the script); returns the state.json of each run as it was when it spawned."""
    snapshots: list[dict] = []

    def fake(layer, state):
        argv = decide.resume_argv(layer, state)
        assert argv[0] == sys.executable and argv[1].endswith("adw_demo.py") and argv[2:5] == ["--resume", "--adw-id", state.adw_id]
        snapshots.append(_state(Path(layer.layer_dir), state.adw_id))
        if before is not None:
            before()
        main("demo", Path(layer.layer_dir), argv[2:])
    monkeypatch.setattr(decide, "spawn_resume", fake)
    return snapshots


# ------------------------------------------------------------------------------------ settings and the contract

def test_mode_precedence_flag_env_setting_default(tmp_path, monkeypatch):
    layer = SimpleNamespace(layer_dir=tmp_path)
    assert decide.mode_for(layer, None) == ("off", "default")
    path = decide.write_mode(tmp_path, "suggest")
    assert path == tmp_path / decide.SETTINGS_FILE
    assert path.read_text() == decide.render_setting("suggest") == '{\n  "mode": "suggest",\n  "version": 1\n}\n'
    assert decide.read_mode(tmp_path) == "suggest"
    assert decide.mode_for(layer, None) == ("suggest", "local setting")
    monkeypatch.setenv("AL_DECIDER", "auto")
    assert decide.mode_for(layer, None) == ("auto", "AL_DECIDER")
    assert decide.mode_for(layer, "off") == ("off", "--decider")
    monkeypatch.setenv("AL_DECIDER", "sometimes")
    with pytest.raises(LayerError, match="AL_DECIDER"):
        decide.mode_for(layer, None)
    monkeypatch.delenv("AL_DECIDER")
    path.write_text('{"version": 1, "mode": "sometimes"}')
    with pytest.raises(LayerError) as caught:
        decide.mode_for(layer, None)
    assert str(path) in str(caught.value) and "al decider LAYER --reset" in str(caught.value)
    decide.write_mode(tmp_path, "")
    assert not path.exists() and decide.mode_for(layer, None) == ("off", "default")


def test_validate_accepts_each_action_and_rejects_bad_ones():
    good = {"summary": " Do it. ", "explanation": "why", "instructions": "", "from_step": ""}
    for action in decide.ACTIONS:
        obj = {**good, "action": action, "instructions": "Add X." if action == "resume-with" else ""}
        checked = decide.validate(obj, STEPS)
        assert checked["action"] == action and checked["summary"] == "Do it."
    assert decide.validate({**good, "action": "resume", "from_step": "build"}, STEPS)["from_step"] == "build"
    bad = [{**good, "action": "merge"},
           {**good, "action": "resume-with"},
           {**good, "action": "resume-with", "instructions": "   "},
           {**good, "action": "resume", "from_step": "deploy"},
           {**good, "action": "abandon", "summary": 42},
           {**good, "action": "abandon", "summary": "x" * 301},
           {**good, "action": "abandon", "explanation": ["no"]},
           ["not", "an", "object"]]
    for obj in bad:
        with pytest.raises(StepFailed) as caught:
            decide.validate(obj, STEPS)
        assert caught.value.step == "decide"


def test_not_eligible_for_layer_changes_limits_waiting_fanout_and_off(make_layer, tmp_path):
    layer = ir.load(_layer(make_layer, tmp_path))

    def stopped(**fields) -> ADWState:
        state = ADWState(adw_id=ADW_ID, adw="demo", prompt=PROMPT, status="failed",
                         ship={"action": "handoff", "stopped_at": "test", "target": "agents/x/PR.md"})
        for key, value in fields.items():
            setattr(state, key, value)
        return state
    assert decide.eligible(layer, stopped(), ADW, mode="auto") == ""
    assert decide.eligible(layer, stopped(), ADW, mode="suggest") == ""
    reasons = [decide.eligible(layer, stopped(), ADW, mode="off"),
               decide.eligible(layer, stopped(), {**ADW, "changes": "layer"}, mode="auto"),
               decide.eligible(layer, stopped(budget={"stopped": {"limit": "run"}}), ADW, mode="auto"),
               decide.eligible(layer, stopped(usage={"stopped": {"window": "five_hour"}}), ADW, mode="auto"),
               decide.eligible(layer, stopped(status="waiting"), ADW, mode="auto"),
               decide.eligible(layer, stopped(fanout_parent={"run": "aaaaaaaa", "step": "race", "index": 1}), ADW, mode="auto"),
               decide.eligible(layer, stopped(ship={"action": "handoff", "handoff_error": "boom"}), ADW, mode="auto"),
               decide.eligible(layer, stopped(ship={"handoff_skipped": "nothing changed"}), ADW, mode="suggest"),
               decide.eligible(layer, stopped(decision={"rounds": decide.MAX_AUTO_ROUNDS}), ADW, mode="auto")]
    assert all(reasons), reasons
    assert len(set(reasons)) == len(reasons)
    assert reasons[-1] == "auto rounds used up"
    assert decide.eligible(layer, stopped(decision={"rounds": decide.MAX_AUTO_ROUNDS}), ADW, mode="suggest") == ""


def test_prepare_resume_resets_steps_from_the_chosen_step_and_appends_instructions(tmp_path):
    state = ADWState(adw_id=ADW_ID, adw="demo", prompt=PROMPT, status="failed")
    for name, status in (("plan", "passed"), ("build", "passed"), ("deliver", "passed"), ("test", "failed"),
                         ("decide", "passed")):
        state.step_record(name, "run").update(status=status, attempts=1)
    decision = {"mode": "auto", "action": "resume-with", "from_step": "build", "instructions": "Do X.", "rounds": 1}
    decide.prepare_resume(state, ADW, decision, layer_dir=tmp_path)
    statuses = {r["name"]: r["status"] for r in state.steps}
    assert statuses == {"plan": "passed", "build": "pending", "deliver": "passed", "test": "pending", "decide": "passed"}
    assert state.prompt == PROMPT + "\n\n## Decision (round 1, auto)\nDo X."
    assert state.decision["status"] == "resuming" and _state(tmp_path)["decision"]["status"] == "resuming"
    later = {"mode": "suggest", "action": "resume", "from_step": "test", "instructions": "", "rounds": 0}
    decide.prepare_resume(state, ADW, later, extra="Also Y.")
    assert state.prompt.endswith("\n\n## Decision (round 2, suggest, answered by a person)\n"
                                 "Run again from step `test`.\n\nThe person adds: Also Y.")


# ------------------------------------------------------------------------------------ through a run

def test_auto_mode_resumes_from_the_chosen_step_with_instructions(make_layer, tmp_path, sink, monkeypatch):
    layer = _layer(make_layer, tmp_path)
    _mail_env(layer, sink)
    _decider(layer, "decide-resume-with")
    snapshots = _resume_in_process(monkeypatch, before=lambda: (tmp_path / "ready").touch())
    assert _run(layer, "--decider", "auto") == 1                 # the first run stopped; the decider resumed it
    (first,) = snapshots
    assert first["status"] == "failed" and first["ship"]["action"] == "handoff"
    d = first["decision"]
    assert (d["mode"], d["action"], d["from_step"], d["status"], d["rounds"]) == ("auto", "resume-with", "build", "resuming", 1)
    assert len(d["code"]) >= 12 and d["answers"] == []
    assert (layer / "agents" / ADW_ID / "decision.md").is_file()
    record = (layer / "agents" / ADW_ID / "decision.md").read_text()
    assert "resume-with" in record and "INSTRUCTIONS-MARKER" in record and str(tmp_path) not in record
    decide_calls = [c for c in first["agent_calls"] if c["step"] == "decide"]
    assert len(decide_calls) == 1 and decide_calls[0]["tier"] == "heavy"
    final = _state(layer)
    assert final["status"] == "passed"
    assert final["decision"]["status"] == "resumed"
    records = {r["name"]: r for r in final["steps"]}
    assert records["build"]["attempts"] == 2 and records["plan"]["attempts"] == 1
    assert records["decide"]["kind"] == "decide" and records["decide"]["status"] == "passed"
    assert "## Decision (round 1, auto)\nINSTRUCTIONS-MARKER" in final["prompt"]
    assert sink.messages == []                                    # no needs-human for the round that resumed
    types = [e["type"] for e in events.read(layer / "agents" / ADW_ID / "events.jsonl")]
    assert "decision" in types and "decision.resume" in types


def test_auto_mode_stops_after_max_rounds_and_notifies(make_layer, tmp_path, sink, monkeypatch):
    layer = _layer(make_layer, tmp_path)
    _mail_env(layer, sink)
    _decider(layer, "decide-resume-with")
    snapshots = _resume_in_process(monkeypatch)                    # `ready` never appears: every round stops again
    assert _run(layer, "--decider", "auto") == 1
    assert len(snapshots) == decide.MAX_AUTO_ROUNDS
    assert [s["decision"]["rounds"] for s in snapshots] == list(range(1, decide.MAX_AUTO_ROUNDS + 1))
    final = _state(layer)
    assert final["status"] == "failed" and final["decision"]["skipped"] == "auto rounds used up"
    (message,) = sink.messages
    parsed = _parsed(message)
    assert "needs a person" in parsed["Subject"] and "[decide " not in parsed["Subject"]
    assert final["notified"] == {"mail": ["needs-human"]}
    assert "decision:  not used (auto rounds used up)" in (layer / "agents" / ADW_ID / "run.log").read_text()


def test_auto_abandon_and_ask_human_leave_the_handoff(make_layer, tmp_path, sink, spawned):
    layer = _layer(make_layer, tmp_path)
    _mail_env(layer, sink)
    _decider(layer, "decide-abandon")
    assert _run(layer, "--decider", "auto") == 1
    d = _state(layer)["decision"]
    assert (d["action"], d["status"], d["rounds"]) == ("abandon", "decided", 1)
    (message,) = sink.messages
    body = _body(message)
    assert "Decision (stand-in for a person):" in body and "- action: abandon" in body
    assert "protected path" in body and "[decide " not in _parsed(message)["Subject"]
    (layer / "mock" / "decide.json").unlink()                     # the mock agent's own answer: ask-human
    assert _run(layer, "--decider", "auto", adw_id="dec1de02") == 1
    d = _state(layer, "dec1de02")["decision"]
    assert (d["action"], d["status"], d["from_step"]) == ("ask-human", "decided", "test")
    assert len(sink.messages) == 2 and spawned == []
    assert not Path(_state(layer, "dec1de02")["worktree"]).exists()   # nothing to resume: the worktree goes


def test_a_failing_decider_never_changes_the_run(make_layer, tmp_path, sink, spawned):
    layer = _layer(make_layer, tmp_path)
    _mail_env(layer, sink)
    assert _run(layer, "--decider", "off", adw_id="0ff0ff01") == 1
    _decider(layer, "decide-bad-step")
    assert _run(layer, "--decider", "auto") == 1
    state = _state(layer)
    assert state["status"] == "failed" and state["ship"]["action"] == "handoff"
    assert state["decision"]["status"] == "failed" and "deploy-to-moon" in state["decision"]["reason"]
    assert "action" not in state["decision"] and spawned == []
    plain = (layer / "agents" / "0ff0ff01" / "PR.md").read_text().replace("0ff0ff01", ADW_ID)
    assert (layer / "agents" / ADW_ID / "PR.md").read_text() == plain
    assert "decision" not in _state(layer, "0ff0ff01")
    assert len(sink.messages) == 2
    assert "Decision (stand-in" not in _body(sink.messages[1])


def test_suggest_mode_go_resumes_and_reject_does_not(make_layer, tmp_path, sink, spawned):
    layer = _layer(make_layer, tmp_path)
    _mail_env(layer, sink)
    _decider(layer, "decide-resume-with")
    assert _run(layer, "--decider", "suggest") == 1
    state = _state(layer)
    d = state["decision"]
    assert (d["mode"], d["action"], d["status"], d["rounds"]) == ("suggest", "resume-with", "suggested", 0)
    assert Path(state["worktree"]).is_dir()                       # kept for a later `go`
    (message,) = sink.messages
    subject, body = _parsed(message)["Subject"], _body(message)
    assert subject.endswith(f"[decide {ADW_ID}-{d['code']}]")
    for needle in ("- mode: suggest", "- action: resume-with", "- from step: build", "the ready marker",
                   "go", "alter: ", "explain", "reject", f"al answer LAYER {ADW_ID} go"):
        assert needle in body, needle
    assert spawned == []
    loaded = ir.load(layer)
    result = decide.answer(loaded, ADW_ID, "go", "", via="cli")
    assert result["status"] == "resuming" and "build" in result["message"]
    assert len(spawned) == 1 and spawned[0][2:] == ["--resume", "--adw-id", ADW_ID, "--decider", "suggest"]
    state = _state(layer)
    assert state["decision"]["status"] == "resuming"
    assert "## Decision (round 1, suggest, answered by a person)\nINSTRUCTIONS-MARKER" in state["prompt"]
    assert [(a["kind"], a["via"]) for a in state["decision"]["answers"]] == [("go", "cli")]
    answers = [e for e in events.read(layer / "agents" / ADW_ID / "events.jsonl") if e["type"] == "decision.answer"]
    assert [(e["kind"], e["via"], e["status"]) for e in answers] == [("go", "cli", "resuming")]

    assert _run(layer, "--decider", "suggest", adw_id="dec1de02") == 1
    result = decide.answer(loaded, "dec1de02", "reject", "", via="cli")
    assert result["status"] == "rejected" and len(spawned) == 1
    d = _state(layer, "dec1de02")["decision"]
    assert d["status"] == "rejected" and d["answers"][-1]["via"] == "cli"
    assert "## Decision" not in _state(layer, "dec1de02")["prompt"]


def test_suggest_mode_alter_turns_ask_human_into_resume_with(make_layer, tmp_path, sink, spawned):
    layer = _layer(make_layer, tmp_path)
    _mail_env(layer, sink)
    assert _run(layer, "--decider", "suggest") == 1               # the mock agent asks a human
    assert _state(layer)["decision"]["action"] == "ask-human"
    result = decide.answer(ir.load(layer), ADW_ID, "alter", "Use the v2 API for the seed data.", via="cli")
    assert result["status"] == "resuming"
    state = _state(layer)
    d = state["decision"]
    assert (d["action"], d["from_step"], d["instructions"]) == ("resume-with", "test", "Use the v2 API for the seed data.")
    assert state["prompt"].endswith("## Decision (round 1, suggest, answered by a person)\nUse the v2 API for the seed data.")
    assert {r["name"]: r["status"] for r in state["steps"]}["test"] == "pending"
    assert len(spawned) == 1


def test_explain_sends_the_full_explanation_once(make_layer, tmp_path, sink, spawned):
    layer = _layer(make_layer, tmp_path)
    _mail_env(layer, sink)
    _decider(layer, "decide-resume-with")
    assert _run(layer, "--decider", "suggest") == 1
    assert len(sink.messages) == 1 and "EXPLANATION-MARKER" not in _body(sink.messages[0])
    loaded = ir.load(layer)
    result = decide.answer(loaded, ADW_ID, "explain", "", via="cli")
    assert result["status"] == "explained"
    assert len(sink.messages) == 2
    code = _state(layer)["decision"]["code"]
    explained = sink.messages[1]
    assert "EXPLANATION-MARKER" in _body(explained) and str(tmp_path) not in _body(explained)
    assert _parsed(explained)["Subject"].endswith(f"[decide {ADW_ID}-{code}]")
    assert [s["notifier"] for s in _state(layer)["decision"]["sent"]] == ["mail"]
    assert decide.answer(loaded, ADW_ID, "go", "", via="cli")["status"] == "resuming"
    assert len(spawned) == 1 and len(sink.messages) == 2


def test_summary_line_names_the_decision(make_layer, tmp_path, sink, spawned):
    layer = _layer(make_layer, tmp_path)
    _mail_env(layer, sink)
    _decider(layer, "decide-resume-with")
    assert _run(layer, "--decider", "auto") == 1
    log = (layer / "agents" / ADW_ID / "run.log").read_text()
    assert "decision:  auto resume-with from build (resuming)" in log
    assert len(spawned) == 1 and Path(_state(layer)["worktree"]).is_dir()
