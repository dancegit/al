"""The voice helper's conversation over HTTP against a real dashboard (the harness of test_voice_server.py:
the fake speech service, the mock helper agent, the layer's own launcher): /api/voice/turn never writes, a
yes only confirms, and every write passes the guarded /api/voice/approve with its 403 and 409 refusals;
no, cancel, the timeout and a late yes leave nothing to approve; approve & resume, give to an agent and
settle go through the buttons' own methods and tokens; the transcript carries questions, read-backs and
decisions and can be cleared. Where the mock helper has no rule, the agent's reply is scripted
(dashboard.helper_via_layer patched; the dashboard runs in this process)."""
from __future__ import annotations

import contextlib
import json
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

import assign_fixtures as af
import dashboard as d
import start_layer
import voice_fixtures as vf
from conftest import start_server, stop_server
from test_approve_resume_http import PLAN, _approval, _passed
from test_start import key, post, run_dirs, wait_state
from test_voice_server import SEARCH_BOX, client, voice_server  # noqa: F401 — `voice_server` is a fixture

ENDED = "2026-11-17T10:00:00+00:00"


def scripted(monkeypatch: pytest.MonkeyPatch, *actions: dict[str, Any]) -> None:
    """The helper's agent proposes `actions`, one per call."""
    queue = list(actions)

    def fake(layer, prompt, timeout=d.HELPER_TIMEOUT):
        assert queue, "the agent was called more often than the test scripted"
        return {"status": "ok", "reply": json.dumps({"say": "On it.", "tool": "propose",
                                                     "args": {"action": queue.pop(0)}}), "cost": 0.0, "model": "mock"}

    monkeypatch.setattr(d, "helper_via_layer", fake)


def read_back(voice: vf.VoiceClient, text: str = SEARCH_BOX) -> dict:
    answer = voice.turn(text)
    assert answer["dialogue"]["state"] == "confirm" and answer["card"]["state"] == "proposed", answer
    return answer["card"]


def approve_body(voice: vf.VoiceClient, card: dict) -> dict:
    return {"layer": voice.layer, "proposal": card["id"], "digest": card["digest"]}


def failed_run(layer: Path, run_id: str, prompt: str) -> None:
    vf.state_json(layer, run_id, adw="plan-build", prompt=prompt, status="failed", host=socket.gethostname(),
                  ended=ENDED)


# ---------------------------------------------------------------- the turn route never writes

def test_turn_route_never_writes(voice_site, voice_server):
    running = voice_server()
    layer = voice_site["talk"]
    voice = client(running)
    first = voice.turn("start a run", "voice")
    assert first["dialogue"]["state"] == "clarify" and first["reply"]["text"] == "Which workflow or command?"
    second = voice.turn("plan build", "voice")
    assert second["dialogue"]["slot"] == "prompt" and second["reply"]["text"] == "What should it do?"
    third = voice.turn("add a search box", "voice")
    assert third["dialogue"]["state"] == "confirm" and third["card"]["state"] == "proposed"
    assert third["reply"]["text"].startswith("In layer talk: Start plan-build: add a search box.")
    assert run_dirs(layer) == []
    answer = voice.turn("yes", "voice")
    card = answer["card"]
    assert card["state"] == "confirmed" and card["digest"] and answer["dialogue"]["decision"] == "yes"
    time.sleep(1.0)
    assert run_dirs(layer) == []                              # the yes itself started nothing
    assert {p.state for p in running.server.model.proposals.items.values()} == {"confirmed"}
    code, body = voice.approve(card)                          # what the page sends after the yes
    assert code == 202 and body["card"]["state"] == "approved", body
    assert run_dirs(layer) == [body["run_id"]]
    assert body["reply"]["text"].startswith("Done. Started run ") and body["reply"]["speak"]
    assert wait_state(layer, body["run_id"])["prompt"] == "add a search box"


def test_yes_still_needs_the_guarded_approve(voice_site, voice_server):
    running = voice_server()
    voice = client(running)
    card = voice.proposal()
    body = approve_body(voice, card)
    assert voice.post("/api/voice/approve", body, Origin=None) == (403, {"error": "cross-origin request refused"})
    assert voice.post("/api/voice/approve", body, X_AL_Dashboard=None) == (403, {"error": "missing X-AL-Dashboard header"})
    assert voice.post("/api/voice/approve", body, Cookie=None) == (403, {"error": "no voice session: reload the page"})
    code, refused = client(running).post("/api/voice/approve", body)
    assert (code, refused) == (403, {"error": "this proposal belongs to another browser session", "card": None})
    code, refused = voice.approve(card, digest="f" * 64)
    assert (code, refused["error"]) == (409, "changed after confirmation: ask again")
    assert refused["reply"]["text"] == "I could not do it: changed after confirmation: ask again."
    assert run_dirs(voice_site["talk"]) == []
    code, done = voice.approve(card)
    assert code == 202, done
    code, refused = voice.approve(card)                       # single use
    assert (code, refused["error"]) == (409, "already approved: it was started once")
    assert run_dirs(voice_site["talk"]) == [done["run_id"]]


def test_no_cancel_timeout_leave_nothing_to_approve(voice_site, voice_server, monkeypatch):
    running = voice_server()
    voice = client(running)
    for words, decision, said in [("no", "no", d.DECLINED_SAY), ("cancel", "cancel", d.CANCELLED_SAY),
                                  (None, "timeout", d.TIMEOUT_SAY)]:
        card = read_back(voice)
        answer = voice.event("timeout") if words is None else voice.turn(words, "voice")
        assert answer["dialogue"] == {**d.Dialogue().view(decision)} and answer["reply"]["text"] == said
        assert answer["card"]["id"] == card["id"] and answer["card"]["state"] == "declined"
        code, body = voice.approve(answer["card"])
        assert (code, body["error"]) == (409, "this proposal was declined: ask again"), (words, body)
    assert voice.event("timeout") == {"entries": [], "reply": None, "card": None, "dialogue": d.Dialogue().view()}
    monkeypatch.setattr(d, "DIALOGUE_WAIT", 0.0)              # a yes that comes too late is a no
    monkeypatch.setattr(d, "DIALOGUE_GRACE", 0.0)
    card = read_back(voice)
    time.sleep(0.05)
    answer = voice.turn("yes")
    assert answer["card"]["state"] == "declined" and answer["dialogue"]["decision"] == "timeout"
    assert voice.approve(answer["card"])[0] == 409
    time.sleep(1.0)
    assert run_dirs(voice_site["talk"]) == []


@pytest.mark.parametrize("flag,why", [("--read-only", "the dashboard was started with --read-only"),
                                      ("--no-start", "the dashboard was started with --no-start")])
def test_flags_refuse_the_voice_write(voice_site, voice_server, monkeypatch, flag, why):
    layer = voice_site["talk"]
    failed_run(layer, "c0ffee01", "Add a word count")
    running = voice_server(flag)
    voice = client(running)
    card = read_back(voice)                                   # the conversation keeps working
    assert card["approvable"] is False and card["why_not"] == why
    card = voice.turn("yes")["card"]
    code, body = voice.approve(card)
    assert code == 403 and body["error"] == why, body
    assert run_dirs(layer) == ["c0ffee01"]
    # a settlement starts nothing: --no-start allows it, --read-only does not
    scripted(monkeypatch, {"kind": "settle", "run": "c0ffee01", "as": "abandoned", "who": "Ada"})
    card = read_back(voice, "settle the failed run as abandoned")
    assert card["kind"] == "settle" and card["approvable"] is (flag == "--no-start")
    code, body = voice.approve(voice.turn("yes")["card"])
    record = layer / "agents" / "c0ffee01" / d.SETTLEMENT_FILE
    if flag == "--no-start":
        assert code == 202 and body["status"] == "settled" and record.is_file(), body
    else:
        assert (code, body) == (403, {"error": why}) and not record.exists()


# ---------------------------------------------------------------- the three kinds of the buttons

def test_new_kinds_use_the_buttons_checks(voice_site, voice_server, monkeypatch, tmp_path):
    layer = voice_site["talk"]
    (layer / "mock" / "plan.json").write_text(json.dumps({"layer_files": {"specs/plan-word.md": PLAN},
                                                          "output": "specs/plan-word.md"}))
    running = voice_server()
    model = running.server.model
    voice = client(running)

    def stopped_run(prompt: str) -> tuple[str, dict]:
        status, data = post(running, {"layer": "talk", "target": {"kind": "workflow", "name": "plan-build"},
                                      "prompt": prompt, "request_key": key()})
        assert status == 202, data
        state = wait_state(layer, data["run_id"])
        assert state["status"] == "failed" and state["proof_plan"]["stopped"]["party"] == "approver"
        return data["run_id"], _approval(running, data["run_id"])

    # approve & resume: the run's own items and token, recorded with the person's name
    run, approval = stopped_run("Add a word count")
    scripted(monkeypatch, {"kind": "approve-resume", "who": "Robin Example", "note": "agreed in review"})
    card = read_back(voice, "approve the run that waits")
    assert card["kind"] == "approve-resume" and card["run"] == run and card["request"] == "agreed in review"
    assert "token" not in card and "nonce" not in card and approval["token"] not in json.dumps(card)
    confirmed = voice.turn("yes")["card"]
    assert approval["token"] not in json.dumps(confirmed)
    code, body = voice.approve(confirmed)
    assert (code, body["status"], body["run_id"]) == (202, "resumed", run), body
    (given,) = _passed(layer, run)["approvals"]
    assert (given["what"], given["who"], given["via"], given["note"]) == ("vocabulary", "Robin Example", "dashboard",
                                                                         "agreed in review")
    # … and a token that went stale after the read-back (the button approved it meanwhile) is 409
    run, approval = stopped_run("Add a line count")
    scripted(monkeypatch, {"kind": "approve-resume", "run": run, "who": "Robin Example"})
    confirmed = voice.turn("approve that run too") and voice.turn("yes")["card"]
    status, data = post(running, {"layer": "talk", "run": run, "items": [i["id"] for i in approval["items"]],
                                  "token": approval["token"], "who": "Ada", "note": ""}, path="/api/approve-resume")
    assert status == 202, data
    _passed(layer, run)
    code, body = voice.approve(confirmed)
    assert (code, body["reason"]) == (409, "stale") and body["card"]["state"] == "failed", body
    assert body["reply"]["text"] == "I could not do it: what was shown is stale: open it again. Nothing was done."
    assert [a["who"] for a in json.loads((layer / "agents" / run / "state.json").read_text())["approvals"]] == ["Ada"]
    # settle: the settlement file; and a run that is running again is 409
    failed_run(layer, "c0ffee01", "Draw the map")
    failed_run(layer, "c0ffee02", "Fix the footer")
    model.tick()
    scripted(monkeypatch, {"kind": "settle", "run": "c0ffee01", "as": "abandoned", "who": "Ada", "note": "not needed"},
             {"kind": "settle", "run": "c0ffee02", "as": "abandoned", "who": "Ada"})
    read_back(voice, "settle run c0ffee01 as abandoned")
    code, body = voice.approve(voice.turn("yes")["card"])
    assert code == 202 and body["status"] == "settled" and body["url"] == "#/run/talk/c0ffee01", body
    record = json.loads((layer / "agents" / "c0ffee01" / d.SETTLEMENT_FILE).read_text())
    assert (record["as"], record["who"], record["note"], record["via"]) == ("abandoned", "Ada", "not needed", "dashboard")
    read_back(voice, "settle run c0ffee02 as abandoned")
    confirmed = voice.turn("yes")["card"]
    sleeper = subprocess.Popen(["sleep", "120"], start_new_session=True)
    try:
        vf.state_json(layer, "c0ffee02", adw="plan-build", prompt="Fix the footer", status="running", pid=sleeper.pid,
                      host=socket.gethostname())
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and model.summaries["talk/c0ffee02"]["status"] != "running":
            model.tick()
            time.sleep(0.3)
        assert model.summaries["talk/c0ffee02"]["status"] == "running"
        code, body = voice.approve(confirmed)
        assert code == 409 and body["card"]["state"] == "failed", body
        assert body["reply"]["text"].startswith("I could not do it: ") and body["reply"]["text"].endswith("Nothing was done.")
        assert not (layer / "agents" / "c0ffee02" / d.SETTLEMENT_FILE).exists()
    finally:
        with contextlib.suppress(OSError):
            sleeper.kill()


def test_assign_nonce_is_single_use(voice_site, tmp_path, monkeypatch):
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN", "AL_DECIDER"):
        monkeypatch.delenv(name, raising=False)
    layer = af.build_scenario(tmp_path / "s")["layer"]
    data = json.loads((layer / "agentic-layer.json").read_text())        # the pantry gets a voice
    data["voice"] = dict(start_layer.VOICE_IR)
    (layer / "agentic-layer.json").write_text(json.dumps(data, indent=1))
    shutil.copytree(start_layer.RUNTIME / "voice", layer / "adws" / "voice", ignore=shutil.ignore_patterns("__pycache__"))
    running = start_server(["--layer", str(layer), "--voice-dir", str(voice_site["voice_dir"]),
                            "--voice-fake", str(voice_site["fake"])])
    try:
        model = running.server.model
        voice = client(running, "pantry")
        scripted(monkeypatch, {"kind": "assign", "run": af.FAILED_CHECK, "note": "mind the order"})
        card = read_back(voice, "give the failed check to an agent")
        assert card["kind"] == "assign" and card["run"] == af.FAILED_CHECK and card["request"] == "mind the order"
        assert card["summary"] == "Give run F 0 0 0 to an agent (check)."
        (nonce,) = list(model.assign_nonces)
        assert "nonce" not in card and nonce not in json.dumps(card)
        confirmed = voice.turn("yes")["card"]
        assert nonce not in json.dumps(confirmed)
        assert model._assign_take(model.layers["pantry"], af.FAILED_CHECK, nonce) is not None   # used up elsewhere
        before = run_dirs(layer)
        code, body = voice.approve(confirmed)
        assert (code, body["error"]) == (409, "the preview is stale: open it again"), body
        assert body["card"]["state"] == "failed" and run_dirs(layer) == before
        assert not (layer / "agents" / af.FAILED_CHECK / "assign.json").exists()
    finally:
        stop_server(running)
        running.server.model.voice.stop_all()


# ---------------------------------------------------------------- the routes

def test_turn_route_input(voice_server):
    running = voice_server()
    voice = client(running)
    read_back(voice)
    answer = voice.turn("no", "click")
    assert answer["entries"][0]["via"] == "click" and answer["dialogue"]["decision"] == "no"
    people = [e for e in voice.get("/api/voice/transcript?layer=talk")[1]["entries"] if e["who"] == "person"]
    assert [(e["text"], e["via"]) for e in people] == [(SEARCH_BOX, "text"), ("no", "click")]
    assert voice.turn("hello", "text", sure=True)["reply"]["text"]
    for body in ({"layer": "talk", "event": "hurry"}, {"layer": "talk", "event": "timeout", "text": "yes"},
                 {"layer": "talk", "event": "timeout", "via": "click"}, {"layer": "talk", "text": "hello", "via": "mind"},
                 {"layer": "talk", "text": "hello", "sure": "yes"}, {"layer": "talk"}, {"layer": "talk", "event": None}):
        code, refused = voice.post("/api/voice/turn", body)
        assert code == 400 and refused["error"].startswith('expected {"layer", "text"'), (body, refused)
    assert vf.VoiceClient(running).post("/api/voice/turn", {"layer": "talk", "event": "timeout"}) == (
        403, {"error": "no voice session: reload the page"})
    assert voice.post("/api/voice/turn", {"layer": "talk", "event": "timeout"}, Origin=None)[0] == 403


def test_clear_transcript(voice_site, voice_server):
    running = voice_server()
    voice = client(running)
    card = voice.proposal()
    conversation = voice.get("/api/voice/transcript?layer=talk")[1]["conversation"]
    path = voice_site["voice_dir"] / f"{conversation}.jsonl"
    assert path.is_file() and path.read_text().count("\n") >= 4
    assert vf.VoiceClient(running).post("/api/voice/clear", {"layer": "talk"}) == (
        403, {"error": "no voice session: reload the page"})
    assert voice.post("/api/voice/clear", {"layer": "talk"}, X_AL_Dashboard=None)[0] == 403
    assert running.get("/api/voice/clear")[0] == 405 and path.is_file()
    assert voice.clear() == (200, {"cleared": True, "entries": [], "cards": [], "dialogue": d.Dialogue().view()})
    assert not path.exists()
    code, transcript = voice.get("/api/voice/transcript?layer=talk")
    assert code == 200 and transcript["entries"] == [] and transcript["dialogue"]["state"] == "idle"
    assert transcript["cards"] == []
    assert [p.state for p in running.server.model.proposals.items.values()] == ["superseded"]
    code, body = voice.approve(card)                          # what was confirmed can no longer be approved
    assert (code, body["error"]) == (409, "this proposal was superseded: ask again")
    assert run_dirs(voice_site["talk"]) == []
    read_back(voice)                                          # a question that waits is forgotten too
    assert voice.clear()[1]["dialogue"]["state"] == "idle"
    assert voice.turn("yes")["card"] is None                  # nothing waits for a yes
    assert voice.clear()[0] == 200 and other_is_untouched(running)


def other_is_untouched(running) -> bool:
    other = client(running)
    other.turn("hello")
    first = client(running)
    first.clear()
    return len(other.get("/api/voice/transcript?layer=talk")[1]["entries"]) == 2


def test_transcript_has_readback_and_decision(voice_site, voice_server):
    running = voice_server()
    voice = client(running)
    voice.turn("start a run", "voice")
    voice.turn("plan build", "click")
    voice.turn("add a search box", "voice")
    code, waiting = voice.get("/api/voice/transcript?layer=talk")
    assert waiting["dialogue"]["state"] == "confirm" and waiting["dialogue"]["expires_at"] > time.time()
    card = voice.turn("yes", "voice")["card"]
    assert voice.approve(card)[0] == 202
    code, transcript = voice.get("/api/voice/transcript?layer=talk")
    helper = [e for e in transcript["entries"] if e["who"] == "helper"]
    assert [e.get("kind") for e in helper] == ["question", "question", "readback", "decision", "outcome"]
    assert helper[2]["text"].endswith(d.READ_BACK_ASK) and helper[2]["card"] == card["id"]
    assert helper[3]["decision"] == "yes" and helper[3]["text"] == d.DOING_SAY and helper[4]["run"]
    assert [e["via"] for e in transcript["entries"] if e["who"] == "person"] == ["voice", "click", "voice", "voice"]
    assert transcript["dialogue"]["state"] == "idle"
    read_back(voice)
    voice.turn("no")
    last = voice.get("/api/voice/transcript?layer=talk")[1]["entries"][-1]
    assert (last["kind"], last["decision"], last["text"]) == ("decision", "no", d.DECLINED_SAY)
    # kept outside every repository, readable by the owner only
    path = voice_site["voice_dir"] / f"{transcript['conversation']}.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e.get("decision") for e in lines if e.get("kind") == "decision"] == ["yes", "no"]
    for layer in ("talk", "notes", "app"):
        assert voice_site[layer].resolve() not in path.resolve().parents
        assert not list(voice_site[layer].rglob("*.jsonl")) or all(
            transcript["conversation"] not in p.name for p in voice_site[layer].rglob("*.jsonl"))


def test_transcript_times_a_waiting_question_out(voice_server, monkeypatch):
    running = voice_server()
    voice = client(running)
    card = read_back(voice)
    monkeypatch.setattr(d, "DIALOGUE_WAIT", 0.0)
    monkeypatch.setattr(d, "DIALOGUE_GRACE", 0.0)
    time.sleep(0.05)
    transcript = voice.get("/api/voice/transcript?layer=talk")[1]
    assert transcript["dialogue"]["state"] == "idle" and transcript["entries"][-1]["decision"] == "timeout"
    assert [c["state"] for c in transcript["cards"]] == ["declined"] and transcript["cards"][0]["id"] == card["id"]


def test_layer_question_over_http(voice_site, voice_server):
    voice_site["chat"] = start_layer.build_layer(voice_site["talk"].parent, "chat", voice_site["app"], None,
                                                 port_base=28790, voice=True)
    running = voice_server(layers=("talk", "chat"))
    voice = client(running)
    answer = voice.turn(SEARCH_BOX, "voice", sure=False)      # the dock picked the layer, not the person
    assert answer["dialogue"]["slot"] == "layer" and answer["reply"]["text"] == "Which layer: talk or chat?"
    assert [o["value"] for o in answer["dialogue"]["options"]] == ["talk", "chat"] and answer["card"] is None
    answer = voice.turn("talk", "click", sure=False)
    assert answer["dialogue"]["state"] == "confirm" and answer["card"]["workflow"] == "plan-build"
    voice.turn("no", sure=False)
    assert voice.turn(SEARCH_BOX, sure=False)["dialogue"]["state"] == "confirm"   # answered once: not asked again
    voice.turn("no")
    sure = client(running)                                    # the person chose the layer: no question
    assert sure.turn(SEARCH_BOX, sure=True)["dialogue"]["state"] == "confirm"
    alone = voice_server()                                    # one voice layer: no question either
    assert client(alone).turn(SEARCH_BOX, sure=False)["dialogue"]["state"] == "confirm"
