"""The voice helper's HTTP routes against a real dashboard and its speech service in fake mode
(`--voice-fake FILE`: kit/runtime/voice/serve.py with stdlib engines, a scripted recogniser), the mock
helper agent (mock/helper.json of the `talk` layer, tests/start_layer.py) and the layer's own launcher.
Nothing here downloads or loads a model."""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

import dashboard as d
import voice_fixtures as vf
import workspace_fixtures as wf
from conftest import Running, start_server, stop_server
from test_start import held_run, run_dirs, wait_state

SERVE = Path(__file__).resolve().parents[2] / "runtime" / "voice" / "serve.py"
SEARCH_BOX = "start a plan for adding a search box"


def _serve_module():
    spec = importlib.util.spec_from_file_location("al_voice_serve_for_dashboard", SERVE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _serve_module()


@pytest.fixture()
def voice_server(voice_site: dict[str, Path]) -> Iterator[Any]:
    """A factory: start(*flags, layers=("talk",), fake=True) → a Running dashboard over those layers of the
    site, with the fake speech service (unless fake=False) and conversations in the site's voice dir."""
    started: list[Running] = []

    def start(*flags: str, layers: tuple[str, ...] = ("talk",), fake: bool = True) -> Running:
        args = [arg for name in layers for arg in ("--layer", str(voice_site[name]))]
        args += ["--voice-dir", str(voice_site["voice_dir"])]
        args += ["--voice-fake", str(voice_site["fake"])] if fake else []
        running = start_server([*args, *flags])
        started.append(running)
        return running

    try:
        yield start
    finally:
        for running in started:
            with contextlib.suppress(Exception):
                stop_server(running)
            running.server.model.voice.stop_all()


def client(running: Running, layer: str = "talk") -> vf.VoiceClient:
    voice = vf.VoiceClient(running, layer)
    voice.session()
    return voice


def layer_voice(running: Running, key: str) -> dict:
    return {layer["key"]: layer for layer in running.json("/api/snapshot")["layers"]}[key]["voice"]


def wav(pattern: str = "s0.2 t1.0 s0.2") -> bytes:
    return vf.wav_of(vf.pcm_of(pattern))


# ---------------------------------------------------------------- status

def test_status_without_voice_has_no_panel(voice_site, voice_server):
    running = voice_server(layers=("notes", "talk"))
    notes = layer_voice(running, "notes")
    assert notes["declared"] is False and notes["panel"] is False and notes["available"] is False
    assert notes["why"] == "" and notes["off"] == "this layer has no voice"   # the page mounts no panel
    talk = layer_voice(running, "talk")
    assert talk["declared"] and talk["panel"] and talk["available"], talk
    assert talk["helper"] == {"enabled": True, "why": ""} and talk["mode"] == "hold" and talk["max_utterance"] == 30
    assert talk["setup"] == "al voice talk setup" and talk["secure_context_note"].startswith("The microphone works only")
    status, body = vf.VoiceClient(running, "notes").get("/api/voice/status?layer=notes")
    assert (status, body) == (404, {"error": "this layer has no voice"})
    voice = client(running)
    voice.layer = "notes"
    assert voice.post("/api/voice/turn", {"layer": "notes", "text": "hello", "via": "text"}) == (
        404, {"error": "this layer has no voice"})
    assert voice.post("/api/voice/session") == (404, {"error": "this layer has no voice"})
    assert running.server.model.voice.services == {}   # nothing was started for it


def test_status_not_set_up_names_the_command(voice_site, voice_server):
    if not d.shutil.which("uv"):
        pytest.skip("uv is not on PATH")
    running = voice_server(fake=False)   # the real service, with an empty model cache (voice_site)
    status = layer_voice(running, "talk")
    assert status["panel"] and status["available"] is False and status["not_set_up"] is True
    assert status["why"] == "voice is not set up: run `al voice talk setup`", status
    assert "model.bin" in status["missing"] and "kokoro-v1.0.int8.onnx" in status["missing"]
    voice = client(running)
    code, body = voice.post("/api/voice/service", {"layer": "talk", "action": "start"})
    assert code == 409 and "al voice talk setup" in body["error"] and body["missing"], body
    assert running.server.model.voice.services == {}   # no service, and nothing downloaded
    assert not (voice_site["cache"]).exists()
    reply = voice.turn("what is running")   # typing still works: the helper does not need speech
    assert reply["reply"]["text"]


@pytest.mark.parametrize("missing", ["adws/voice/serve.py", "adws/adw_modules/helper.py"])
def test_status_old_layer_says_run_al_gen(voice_site, voice_server, missing):
    (voice_site["talk"] / missing).unlink()
    running = voice_server()
    status = layer_voice(running, "talk")
    assert status["panel"] is True and status["available"] is False
    assert status["why"] == "generated before the voice helper: run `al gen talk`", status
    assert status["helper"]["enabled"] is False
    voice = client(running)
    code, body = voice.post("/api/voice/turn", {"layer": "talk", "text": "hello", "via": "text"})
    assert code == 409 and "al gen talk" in body["error"]


@pytest.mark.parametrize("flag", ["--in-container", "--no-voice"])
def test_in_container_no_voice(voice_site, voice_server, flag):
    assert d.parse_args(["--layer", "x", "--in-container"]).no_voice is True
    running = voice_server(flag)
    status = layer_voice(running, "talk")
    assert status["declared"] and status["panel"] is False and status["available"] is False and status["why"] == ""
    assert ("container" if flag == "--in-container" else "--no-voice") in status["off"]
    voice = vf.VoiceClient(running)
    code, body = voice.post("/api/voice/session")
    assert code == 403 and body["error"] == status["off"]
    assert running.server.model.voice.services == {}


# ---------------------------------------------------------------- guards

def test_voice_routes_need_session_and_guard(voice_server):
    running = voice_server()
    voice = vf.VoiceClient(running)
    turn = {"layer": "talk", "text": "hello", "via": "text"}
    assert voice.post("/api/voice/turn", turn) == (403, {"error": "no voice session: reload the page"})
    assert voice.get("/api/voice/transcript?layer=talk") == (403, {"error": "no voice session: reload the page"})
    voice.session()
    assert len(voice.cookie) == 32
    _status, headers, _data = voice.raw("/api/voice/session", json.dumps({"layer": "talk"}).encode())
    assert "set-cookie" not in headers   # the cookie is set once
    fresh = vf.VoiceClient(running)
    _status, headers, _data = fresh.raw("/api/voice/session", json.dumps({"layer": "talk"}).encode())
    cookie = headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert voice.post("/api/voice/turn", turn, Origin="http://evil.example") == (
        403, {"error": "cross-origin request refused"})
    assert voice.post("/api/voice/turn", turn, Origin=None) == (403, {"error": "cross-origin request refused"})
    assert voice.post("/api/voice/turn", turn, X_AL_Dashboard=None) == (403, {"error": "missing X-AL-Dashboard header"})
    assert voice.post("/api/voice/turn", turn, content_type="text/plain")[0] == 415
    assert voice.post("/api/voice/turn", turn, Host="evil.example:80") == (403, {"error": "bad host"})
    code, body = voice.post("/api/voice/turn", {**turn, "text": "x" * (d.MAX_VOICE_BODY + 10)})
    assert code == 413 and "over" in body["error"]
    assert voice.post("/api/voice/utterance?layer=talk", wav(), "application/json")[0] == 415
    code, body = voice.utterance(b"RIFF" + b"\0" * (30 * 32000 + 2048))
    assert code == 413, body
    assert voice.post("/api/voice/turn", {**turn, "text": "x" * 2001})[0] == 400
    assert voice.post("/api/voice/turn", {**turn, "layer": "nope"}) == (404, {"error": "unknown layer"})
    assert voice.post("/api/voice/session", content_type="text/plain")[0] == 415   # the session route is guarded too
    assert running.server.model.voice.services == {}


def test_voice_routes_need_the_token(voice_site, voice_server):
    running = voice_server("--token", "dash-secret-token")
    voice = vf.VoiceClient(running, token="dash-secret-token")
    voice.session()
    assert voice.turn("hello")["reply"]["text"]
    voice.token = None
    assert voice.post("/api/voice/turn", {"layer": "talk", "text": "hello", "via": "text"})[0] == 401
    status, _h, _b = running.get("/api/voice/status?layer=talk")
    assert status == 401


def test_get_on_post_routes_is_405(voice_server):
    running = voice_server()
    for path in [*d.VOICE_POST, "/api/voice/approve"]:
        status, _headers, body = running.get(path)
        assert status == 405, (path, body)
    for method in ("PUT", "DELETE"):
        assert running.get("/api/voice/turn", method=method)[0] == 405
    assert running.get("/api/voice/nothing", method="POST", body=b"{}")[0] == 405


# ---------------------------------------------------------------- the conversation

def test_utterance_then_turn_creates_a_card(voice_site, voice_server):
    vf.fake_script(voice_site["fake"], [SEARCH_BOX])
    running = voice_server()
    voice = client(running)
    code, heard = voice.utterance(wav())
    assert code == 200 and heard["heard"] == SEARCH_BOX, heard
    assert [w["w"] for w in heard["words"]] == SEARCH_BOX.split()
    answer = voice.turn(heard["heard"], "voice")
    card = answer["card"]
    assert card["kind"] == "start" and card["workflow"] == "plan-build" and card["command"] == ""
    assert card["request"] == "Add a search box to the notes page" and card["class"] == "feature"
    assert card["tier"] == "" and card["repository"] == "app" and card["state"] == "proposed"
    assert card["approvable"] is True and card["why_not"] == "" and card["digest"] == "" and card["expires_at"] is None
    assert card["summary"] == "Start plan-build: Add a search box to the notes page (class feature)."
    assert "person reviews" in card["what_happens"] or card["what_happens"], card
    assert answer["reply"]["text"].endswith("Shall I prepare it for approval?")
    assert answer["reply"]["speak"] and answer["reply"]["id"]
    code, transcript = voice.get("/api/voice/transcript?layer=talk")
    assert code == 200
    people = [e for e in transcript["entries"] if e["who"] == "person"]
    assert [(e["text"], e["via"]) for e in people] == [(SEARCH_BOX, "voice")]   # heard once, not twice
    assert transcript["entries"][-1]["who"] == "helper" and transcript["entries"][-1]["card"] == card["id"]
    assert [c["id"] for c in transcript["cards"]] == [card["id"]]
    assert run_dirs(voice_site["talk"]) == []
    other = client(running)   # another browser sees none of it
    assert other.get("/api/voice/transcript?layer=talk")[1]["entries"] == []


def test_yes_confirms_and_nothing_starts(voice_site, voice_server):
    vf.fake_script(voice_site["fake"], ["approve it", "start it now"])
    running = voice_server()
    voice = client(running)
    card = voice.turn(SEARCH_BOX)["card"]
    answer = voice.turn("yes")
    assert answer["card"]["id"] == card["id"] and answer["card"]["state"] == "confirmed"
    assert answer["card"]["digest"] and answer["card"]["expires_at"]
    assert answer["reply"]["text"] == d.CONFIRMED_SAY
    for _ in range(2):   # "approve it", "start it now", spoken: words, never a click
        code, heard = voice.utterance(wav())
        assert code == 200
        voice.turn(heard["heard"], "voice")
    voice.turn("approve")
    time.sleep(1.0)
    assert run_dirs(voice_site["talk"]) == []
    states = {p.id: p.state for p in running.server.model.proposals.items.values()}
    assert states == {card["id"]: "confirmed"}


def test_no_declines(voice_server):
    running = voice_server()
    voice = client(running)
    card = voice.turn(SEARCH_BOX)["card"]
    answer = voice.turn("no, cancel that")
    assert answer["reply"]["text"] == d.DECLINED_SAY and answer["card"]["state"] == "declined"
    assert answer["card"]["approvable"] is False
    assert voice.approve(card)[0] == 409


def test_approve_starts_mock_run(voice_site, voice_server):
    running = voice_server()
    voice = client(running)
    card = voice.proposal()
    code, body = voice.approve(card)
    assert code == 202, body
    run_id = body["run_id"]
    assert body["status"] == "started" and body["url"] == f"#/run/talk/{run_id}"
    assert body["card"]["state"] == "approved" and body["card"]["approvable"] is False
    assert run_dirs(voice_site["talk"]) == [run_id]
    state = wait_state(voice_site["talk"], run_id)
    assert state["status"] == "passed" and state["prompt"] == "Add a search box to the notes page"
    assert state["klass"] == "feature"
    assert voice.approve(card) == (409, {"error": "already approved: it was started once", "card": body["card"]})
    assert run_dirs(voice_site["talk"]) == [run_id]
    entries = voice.get("/api/voice/transcript?layer=talk")[1]["entries"]
    started = entries[-1]
    assert started["who"] == "helper" and started["run"] == run_id and started["url"] == body["url"]
    assert started["text"].startswith(f"Started run {' '.join(run_id[:4].upper())}: Start plan-build")


def test_approve_refusals(voice_site, voice_server):
    running = voice_server()
    voice = client(running)
    model = running.server.model
    proposed = voice.turn(SEARCH_BOX)["card"]
    assert voice.approve(proposed, digest="0" * 64) == (409, {"error": "not confirmed: answer yes first",
                                                              "card": voice.get("/api/voice/transcript?layer=talk")[1]["cards"][0]})
    card = voice.turn("yes")["card"]
    other = client(running)
    code, body = other.approve(card)
    assert (code, body["error"]) == (403, "this proposal belongs to another browser session") and body["card"] is None
    code, body = voice.approve(card, digest="f" * 64)
    assert (code, body["error"]) == (409, "changed after confirmation: ask again")
    code, body = voice.post("/api/voice/approve", {"layer": "talk", "proposal": card["id"], "digest": card["digest"]},
                            Cookie=None)
    assert (code, body) == (403, {"error": "no voice session: reload the page"})
    code, body = voice.post("/api/voice/approve", {"layer": "talk", "proposal": card["id"], "digest": card["digest"]},
                            X_AL_Dashboard=None)
    assert code == 403
    model.proposals.items[card["id"]].action["prompt"] = "something else"   # edited after the yes
    assert voice.approve(card)[1]["error"] == "changed after confirmation: ask again"
    again = voice.proposal()
    model.proposals.items[again["id"]].confirmed_at -= d.PROPOSAL_TTL + 1
    code, body = voice.approve(again)
    assert (code, body["error"]) == (410, "expired after two minutes: ask again") and body["card"]["state"] == "expired"
    assert voice.post("/api/voice/approve", {"layer": "talk", "proposal": "nothing", "digest": "x"})[0] == 404
    assert run_dirs(voice_site["talk"]) == []


@pytest.mark.parametrize("flag,why", [("--read-only", "the dashboard was started with --read-only"),
                                      ("--no-start", "the dashboard was started with --no-start")])
def test_approve_refused_by_flags(voice_site, voice_server, flag, why):
    running = voice_server(flag)
    voice = client(running)
    card = voice.turn(SEARCH_BOX)["card"]   # the conversation keeps working
    assert card["approvable"] is False and card["why_not"] == why
    card = voice.turn("yes")["card"]
    assert card["state"] == "confirmed"
    code, body = voice.approve(card)
    if flag == "--read-only":   # the full write guard refuses it before anything else
        assert (code, body) == (403, {"error": why})
    else:
        assert (code, body["error"], body["card"]["state"]) == (403, why, "confirmed")
    assert run_dirs(voice_site["talk"]) == []


def test_approve_job_refused_by_no_actions(voice_site, voice_server):
    running = voice_server("--no-actions")
    voice = client(running)
    model = running.server.model
    conv = model.conversations.get(voice.cookie, "talk")
    proposal = model.proposals.create(conv, {"kind": "job", "repo": "app", "action": "scout"}, "Run al scout on app.",
                                      "al scout")
    model.proposals.confirm(proposal.id, voice.cookie, time.time())
    card = d.card_view(proposal, model)
    assert card["approvable"] is False and card["why_not"] == "actions are off (--no-actions)"
    code, body = voice.approve(card)
    assert (code, body["error"]) == (403, "actions are off (--no-actions)")
    start = voice.proposal()   # starting work stays on
    assert start["approvable"] is True


# ---------------------------------------------------------------- job, resume, answer, stop

def test_job_resume_stop_answer_cards(voice_site, voice_server, tmp_path):
    ws = tmp_path / "ws"
    wf.build(ws)
    al = wf.fake_al(tmp_path / "bin" / "al", out="# scouted")
    running = voice_server("--workspace", str(ws), "--jobs-dir", str(tmp_path / "jobs"), "--al", str(al))
    layer = voice_site["talk"]
    voice = vf.VoiceClient(running)
    voice.session(repo="fern")
    # a job: the mock helper proposes `scout` for the selected repository
    card = voice.turn("please scout this repository")["card"]
    assert card["kind"] == "job" and card["job"] == "scout" and card["repository"] == "fern", card
    card = voice.turn("yes")["card"]
    code, body = voice.approve(card)
    assert code == 202 and body["job"] and body["url"] == f"#/job/{body['job']}", body
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(c[:1] == ["scout"] for c in wf.calls(al)):
        time.sleep(0.2)
    assert any(c[:1] == ["scout"] for c in wf.calls(al)), wf.calls(al)
    # a waiting run, resumed with an answer
    waiting = "a1b2c3d4"
    vf.state_json(layer, waiting, adw="plan-build", prompt="Pick a colour", status="waiting",
                  host=socket.gethostname(), ended="2026-11-17T10:00:00+00:00")
    model = running.server.model
    model.tick()
    conv = model.conversations.get(voice.cookie, "talk")
    action, error = d.validate_action(model, model.layers["talk"], conv, {"kind": "answer", "run": waiting, "text": "Blue"})
    assert error == "", error
    proposal = model.proposals.create(conv, action, *d.proposal_text(model, model.layers["talk"], action))
    card = voice.turn("yes")["card"]
    assert card["id"] == proposal.id and card["kind"] == "answer" and card["run"] == waiting
    code, body = voice.approve(card)
    assert (code, body["status"], body["run_id"]) == (202, "resumed", waiting), body
    answer = layer / "agents" / waiting / "answer.md"
    assert answer.read_text() == "Blue\n" and stat.S_IMODE(answer.stat().st_mode) == 0o600
    assert (layer / "agents" / waiting / "launch.log").exists()
    # a running run (its process alive on this host, in its own session), stopped
    sleeper = subprocess.Popen(["sleep", "120"], start_new_session=True)
    try:
        running_id = "b2c3d4e5"
        vf.state_json(layer, running_id, adw="plan-build", prompt="Long job", status="running", pid=sleeper.pid,
                      host=socket.gethostname())
        model.tick()
        action, error = d.validate_action(model, model.layers["talk"], conv, {"kind": "stop", "run": running_id})
        assert error == "", error
        proposal = model.proposals.create(conv, action, *d.proposal_text(model, model.layers["talk"], action))
        assert proposal.summary == f"Stop run {' '.join(running_id[:4].upper())}."
        card = voice.turn("yes")["card"]
        code, body = voice.approve(card)
        assert (code, body["status"], body["run_id"]) == (202, "stopping", running_id), body
        assert sleeper.wait(10) == -signal.SIGTERM
    finally:
        with contextlib.suppress(OSError):
            sleeper.kill()
    assert d.validate_action(model, model.layers["talk"], conv, {"kind": "answer", "run": running_id, "text": "x"})[1] \
        .endswith("only a waiting run takes an answer")


def test_resume_proposal_refuses_a_run_whose_process_is_alive(voice_site, voice_server):
    running = voice_server()
    layer = voice_site["talk"]
    voice = vf.VoiceClient(running)
    voice.session()
    failed = "c3d4e5f6"
    vf.state_json(layer, failed, adw="plan-build", prompt="Fix the footer", status="failed",
                  host=socket.gethostname(), ended="2026-11-17T10:00:00+00:00")
    model = running.server.model
    model.tick()
    conv = model.conversations.get(voice.cookie, "talk")
    with held_run(layer, failed) as child:
        action, error = d.validate_action(model, model.layers["talk"], conv, {"kind": "resume", "run": failed})
        assert action is None and str(child.pid) in error and "left to that process" in error, error
        child.kill()
        child.wait(timeout=30)
        action, error = d.validate_action(model, model.layers["talk"], conv, {"kind": "resume", "run": failed})
    assert error == "" and action == {"kind": "resume", "run": failed}, error


# ---------------------------------------------------------------- speech

def read_frames(response: http.client.HTTPResponse, want: int | None = None) -> list[tuple[dict, bytes]]:
    """Frames of a /api/voice/speak answer as they arrive; stops after `want` frames (or at the end)."""
    data = b""
    frames: list[tuple[dict, bytes]] = []
    while want is None or len(frames) < want:
        chunk = response.read1(65536)
        if not chunk:
            break
        data += chunk
        try:
            frames = serve.decode_frames(data)
        except ValueError:
            continue   # a frame is still arriving
    return frames


def speak(running: Running, voice: vf.VoiceClient, reply: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=30)
    connection.request("POST", "/api/voice/speak", body=json.dumps({"layer": "talk", "reply": reply}).encode(),
                       headers=voice.headers())
    return connection, connection.getresponse()


def test_speak_proxies_frames_and_cancel(voice_server):
    running = voice_server()
    voice = client(running)
    reply = voice.turn("hello there")["reply"]
    connection, response = speak(running, voice, reply["id"])
    try:
        assert response.status == 200 and response.getheader("Content-Type") == serve.FRAME_TYPE
        assert response.getheader("Transfer-Encoding") == "chunked"
        frames = read_frames(response)
    finally:
        connection.close()
    sentences = serve.split_sentences(reply["speak"])
    assert [h["text"] for h, _w in frames] == sentences and frames[-1][0]["last"] is True
    assert all(w[:4] == b"RIFF" and h["envelope"] for h, w in frames)
    # a long reply, cancelled after its first sentence: at most one more sentence, then `cancelled`
    long = voice.turn("tell me a long story")["reply"]
    assert len(serve.split_sentences(long["speak"])) == 3
    connection, response = speak(running, voice, long["id"])
    try:
        first = read_frames(response, want=1)
        assert first[0][0]["seq"] == 0 and first[0][1]
        assert voice.post("/api/voice/cancel", {"layer": "talk", "reply": long["id"]}) == (200, {"cancelled": True})
        rest = read_frames(response)
    finally:
        connection.close()
    audio = [h for h, w in rest if w]
    assert len(audio) <= 2 and rest[-1][0].get("cancelled") is True and rest[-1][0]["last"] is True, rest
    assert voice.post("/api/voice/speak", {"layer": "talk", "reply": "00000000"}) == (
        404, {"error": "no such reply in this conversation"})
    other = client(running)   # another browser cannot have this conversation's replies spoken
    assert other.post("/api/voice/speak", {"layer": "talk", "reply": reply["id"]})[0] == 404


def test_listen_echo_is_ignored(voice_site, voice_server):
    running = voice_server()
    voice = client(running)
    reply = voice.turn("hello there")["reply"]
    connection, response = speak(running, voice, reply["id"])
    read_frames(response)
    connection.close()
    vf.fake_script(voice_site["fake"], [reply["speak"], "what is running right now"])
    utterance = vf.pcm_of("s0.3 T1.0 s0.8")
    code, answer = voice.listen(utterance, stream="mic1", speaking=0)
    assert code == 200 and answer["state"] == "end" and answer["echo"] is True, answer
    code, answer = voice.listen(utterance, stream="mic1")
    assert code == 200 and answer["state"] == "end" and "echo" not in answer
    assert answer["text"] == "what is running right now"
    entries = voice.get("/api/voice/transcript?layer=talk")[1]["entries"]
    echoes = [e for e in entries if e.get("echo")]
    assert len(echoes) == 1 and echoes[0]["who"] == "system" and echoes[0]["text"].startswith("ignored: my own voice")
    assert entries[-1] == {**entries[-1], "who": "person", "text": "what is running right now", "via": "voice"}
    code, answer = voice.listen(vf.pcm_of("s0.3"), stream="mic1")
    assert answer["state"] == "silence"
    assert voice.listen(b"", stream="bad%20stream!")[0] == 400


def test_recordings_only_when_switched_on(voice_site, voice_server):
    vf.fake_script(voice_site["fake"], ["one", "two", "three"])
    running = voice_server()
    voice = client(running)
    conversation = voice.session()["conversation"]
    assert voice.utterance(wav())[0] == 200
    assert not (voice_site["talk"] / "agents" / "voice").exists()
    code, body = voice.post("/api/voice/recordings", {"layer": "talk", "keep": True})
    assert (code, body) == (200, {"keep_recordings": True, "record_dir": f"agents/voice/{conversation}/"})
    assert voice.session()["keep_recordings"] is True
    assert voice.utterance(wav())[0] == 200
    kept = sorted((voice_site["talk"] / "agents" / "voice" / conversation).glob("*.wav"))
    assert [p.name for p in kept] == ["1.wav"] and stat.S_IMODE(kept[0].stat().st_mode) == 0o600
    voice.post("/api/voice/recordings", {"layer": "talk", "keep": False})
    assert voice.utterance(wav())[0] == 200
    assert len(list((voice_site["talk"] / "agents" / "voice" / conversation).glob("*.wav"))) == 1
    assert not list(voice_site["app"].rglob("*.wav"))   # never in the repository
    stop_server(running)
    read_only = voice_server("--read-only")
    voice = client(read_only)
    assert voice.post("/api/voice/recordings", {"layer": "talk", "keep": True}) == (
        403, {"error": "the dashboard was started with --read-only"})
    assert voice.turn("hello")["reply"]["text"]


def test_service_started_on_demand_and_reaped(voice_site, voice_server):
    vf.fake_script(voice_site["fake"], ["one", "two"])
    running = voice_server()
    model = running.server.model
    voice = client(running)
    assert model.voice.services == {} and layer_voice(running, "talk")["service"] == {"running": False}
    assert voice.get("/api/voice/health?layer=talk") == (200, {"running": False, "ready": False, "loading": False,
                                                                 "missing": []})
    code, body = voice.post("/api/voice/service", {"layer": "talk", "action": "start"})
    assert code == 200 and body["running"] and body["health"]["ready"] and body["health"]["fake"], body
    code, health = voice.get("/api/voice/health?layer=talk")
    assert health["running"] and health["ready"] and health["stt_model"] == "fake"
    first = model.voice.services["talk"].process
    os.killpg(first.pid, signal.SIGKILL)   # the service dies: the next request starts it again
    first.wait(10)
    code, heard = voice.utterance(wav())
    assert (code, heard["heard"]) == (200, "one"), heard
    second = model.voice.services["talk"]
    assert second.process.pid != first.pid and second.process.poll() is None
    second.last_used -= d.VOICE_IDLE + 1
    assert model.voice.reap(time.time()) == ["talk"]
    assert second.process.wait(10) is not None and model.voice.services == {}
    assert voice.post("/api/voice/service", {"layer": "talk", "action": "start"})[0] == 200
    assert voice.post("/api/voice/service", {"layer": "talk", "action": "stop"}) == (200, {"running": False,
                                                                                          "stopped": True})
    assert voice.post("/api/voice/service", {"layer": "talk", "action": "jump"})[0] == 400


def test_reap_leaves_a_service_that_replaced_the_dead_one():
    voice = d.VoiceServices()
    now = time.time()
    new = d.VoiceService(process=subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                                  start_new_session=True),
                         port=2, token="new", record_dir="rec", started=now, last_used=now)

    class DeadProcess:   # the old service: dead, and `ensure` swaps in the new one between reap's check and its stop
        pid = -1
        swapped = False

        def poll(self) -> int:
            if not self.swapped:
                self.swapped = True
                voice.services["talk"] = new
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    old = d.VoiceService(process=DeadProcess(), port=1, token="old", record_dir="", started=now, last_used=now)
    try:
        voice.services["talk"] = old
        assert voice.reap(time.time()) == []
        assert voice.services["talk"] is new
        assert new.process.poll() is None
    finally:
        d._stop_group(new.process)


def test_service_token_not_in_argv_or_page(voice_site, voice_server, monkeypatch):
    monkeypatch.setenv("AL_DASHBOARD_TOKEN", "dash-secret-token")
    running = voice_server()   # --token from AL_DASHBOARD_TOKEN
    assert running.server.token == "dash-secret-token"
    voice = vf.VoiceClient(running, token="dash-secret-token")
    voice.session()
    assert voice.post("/api/voice/service", {"layer": "talk", "action": "start"})[0] == 200
    service = running.server.model.voice.services["talk"]
    assert len(service.token) >= 32
    cmdline = Path(f"/proc/{service.process.pid}/cmdline").read_bytes()
    assert service.token.encode() not in cmdline and b"dash-secret-token" not in cmdline
    environ = Path(f"/proc/{service.process.pid}/environ").read_bytes()
    assert b"dash-secret-token" not in environ and b"AL_DASHBOARD_TOKEN" not in environ
    seen = b""
    for path in ("/", "/static/voice.js", "/api/snapshot", "/api/voice/status?layer=talk", "/api/voice/health?layer=talk",
                 "/api/voice/transcript?layer=talk"):
        status, _h, body = voice.raw(path, None, method="GET")
        assert status == 200, (path, status)
        seen += body
    assert service.token.encode() not in seen
    with socket.create_connection(("127.0.0.1", service.port), timeout=5) as sock:   # loopback only, token needed
        sock.sendall(f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{service.port}\r\n\r\n".encode())
        assert b" 401 " in sock.recv(4096).split(b"\r\n", 1)[0] + b" "


def test_skips_print_their_reason(pytestconfig) -> None:
    assert "s" in pytestconfig.option.reportchars
