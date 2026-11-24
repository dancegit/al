"""The voice helper's conversation without HTTP and without a browser: the dialogue state machine
(idle → clarify → confirm), what the server asks when a proposed action is incomplete, the read-back, the
explicit yes / no, cancel, change, the timeout and the two repeats of an unclear answer, and the three
action kinds that go through the buttons' own methods (approve & resume, give to an agent, settle).
The helper's agent is faked (helper_via_layer patched with scripted replies); a clock is injected for the
timeouts, nothing sleeps."""
from __future__ import annotations

import json
import re
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

import approvable_fixtures as ap
import assign_fixtures as af
import dashboard as d
import voice_fixtures as vf
from test_voice_helper import SESSION, flags, talk  # noqa: F401 — `talk` is a fixture

ENDED = "2026-11-17T10:00:00+00:00"
SEARCH = {"kind": "start", "target": {"kind": "workflow", "name": "plan-build"}, "prompt": "Add a search box",
          "class": "feature"}


def propose(action: dict[str, Any], say: str = "On it.") -> dict[str, Any]:
    return {"say": say, "tool": "propose", "args": {"action": action}}


def script(monkeypatch: pytest.MonkeyPatch, *replies: dict[str, Any]) -> list[str]:
    """The agent answers with `replies` in order; → the prompts it got (one per call)."""
    prompts: list[str] = []
    queue = list(replies)

    def fake(layer, prompt, timeout=d.HELPER_TIMEOUT):
        prompts.append(prompt)
        assert queue, "the agent was called more often than the test scripted"
        return {"status": "ok", "reply": json.dumps(queue.pop(0)), "cost": 0.0, "model": "mock"}

    monkeypatch.setattr(d, "helper_via_layer", fake)
    return prompts


def failed_run(layer: Path, run_id: str, prompt: str, status: str = "failed", **more: Any) -> None:
    vf.state_json(layer, run_id, adw="plan-build", prompt=prompt, status=status, host=socket.gethostname(),
                  **({} if status == "running" else {"ended": ENDED}), **more)


def helpers(turn: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in turn["entries"] if e["who"] == "helper"]


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------- the pure parts

def test_classify_reply():
    table = {
        "yes": ["yes", "yeah", "sure, go ahead", "yes please", "Yes.", "ok", "Yep!", "yes, do it", "okay do that"],
        "no": ["no", "nope", "no thanks", "not now", "No, don’t.", "don't", "no, cancel that"],
        "cancel": ["cancel", "stop", "Cancel that.", "stop it please", "abort", "never mind", "forget it",
                   "cancel everything"],
        "change": ["change the class to bug", "no, use plan-build", "yes but make it a bug", "make it a chore",
                   "use the heavy model", "actually the other workflow", "yes and also delete the tests"],
        "unclear": ["", "maybe", "what", "approve it", "start it now", "do it maybe", "hmm"],
    }
    for want, texts in table.items():
        for text in texts:
            assert d.classify_reply(text) == want, (text, want)
    # a yes with other content is never a yes
    for text in ("yes but make it a bug", "yes and also delete the tests", "ok then stop run 3fa2", "yes no"):
        assert d.classify_reply(text) != "yes", text


def test_match_option():
    names = (("plan-build", "plan-build"), ("ship-it", "ship-it"), ("plan_chore", "plan_chore"))
    assert d.match_option("plan build", names) == "plan-build"
    assert d.match_option("Plan-Build.", names) == "plan-build"
    assert d.match_option("ship it", names) == "ship-it"
    assert d.match_option("plan chore", names) == "plan_chore"
    assert d.match_option("the plan build one", names) == "plan-build"
    assert d.match_option("the second one", names) == "ship-it"
    assert d.match_option("first", names) == "plan-build"
    assert d.match_option("number 3", names) == "plan_chore"
    assert d.match_option("fourth", names) is None and d.match_option("plan", names) is None
    assert d.match_option("", names) is None and d.match_option("something else", names) is None
    runs = (("3fa2b1c9", "run 3 F A 2: Add a word count"), ("3fa9d000", "run 3 F A 9: Fix the footer"),
            ("c0ffee01", "run C 0 F F: Draw the map"))
    assert d.match_option("run 3 f a 2", runs) == "3fa2b1c9"
    assert d.match_option("3fa2", runs) == "3fa2b1c9"
    assert d.match_option("c0ffee01", runs) == "c0ffee01"
    assert d.match_option("run C 0 F F", runs) == "c0ffee01"
    assert d.match_option("the third", runs) == "c0ffee01"
    assert d.match_option("run 3 f a", runs) is None          # two runs start so
    assert d.match_option("3f", runs) is None and d.match_option("run 9 9 9 9", runs) is None
    same = (("a", "Alpha"), ("b", "alpha"))
    assert d.match_option("alpha", same) is None              # two matches: no guess


def test_dialogue_state_machine():
    clock = Clock()
    dialogue = d.Dialogue(clock=clock)
    assert dialogue.state == "idle" and not dialogue.pending() and dialogue.expires_at() is None
    assert dialogue.view() == {"state": "idle", "slot": "", "question": "", "options": [], "retries": 0,
                               "expires_at": None, "proposal": "", "decision": ""}
    gap = d.Gap("target", "Which workflow or command?", (("plan-build", "plan-build"),))
    dialogue.ask({"kind": "start"}, gap)
    assert dialogue.state == "clarify" and dialogue.pending() and dialogue.expires_at() == 1000.0 + d.DIALOGUE_WAIT
    assert dialogue.view()["options"] == [{"value": "plan-build", "label": "plan-build"}]
    assert dialogue.unclear() is True and dialogue.retries == 1
    dialogue.ask({"kind": "start"}, gap)                     # the same slot again keeps the count
    assert dialogue.retries == 1
    dialogue.ask({"kind": "start"}, d.Gap("prompt", "What should it do?"))
    assert dialogue.retries == 0 and dialogue.view()["options"] == []
    clock.now += d.DIALOGUE_WAIT + d.DIALOGUE_GRACE - 1
    assert not dialogue.expired()
    clock.now += 2
    assert dialogue.expired()
    dialogue.read_back("abcd1234")
    assert dialogue.state == "confirm" and dialogue.view()["question"] == d.READ_BACK_ASK
    assert dialogue.view("no")["decision"] == "no" and dialogue.view()["proposal"] == "abcd1234"
    assert [dialogue.unclear() for _ in range(3)] == [True, True, False] and d.DIALOGUE_RETRIES == 2
    dialogue.end()
    assert dialogue.state == "idle" and dialogue.draft is None and dialogue.gap is None and not dialogue.expired()


# ---------------------------------------------------------------- clarify

def test_clarify_asks_one_question_at_a_time(talk, monkeypatch, voice_site):
    model, conv = talk
    prompts = script(monkeypatch, propose({"kind": "start"}))
    first = d.helper_turn(model, conv, "start a run", "voice")
    assert first["dialogue"]["state"] == "clarify" and first["dialogue"]["slot"] == "target"
    assert first["reply"]["text"] == "Which workflow or command?" and first["card"] is None
    values = [o["value"] for o in first["dialogue"]["options"]]
    assert values[:2] == ["plan-build", "ship-it"] and "plan-chore" in values and len(values) <= 8
    assert len(helpers(first)) == 1 and helpers(first)[0]["kind"] == "question"
    second = d.helper_turn(model, conv, "plan build", "voice")
    assert second["dialogue"]["state"] == "clarify" and second["dialogue"]["slot"] == "prompt"
    assert second["reply"]["text"] == "What should it do?" and second["dialogue"]["options"] == []
    assert len(helpers(second)) == 1 and model.proposals.items == {}
    third = d.helper_turn(model, conv, "add a search box", "voice")
    assert third["dialogue"]["state"] == "confirm" and third["card"]["state"] == "proposed"
    assert third["card"]["workflow"] == "plan-build" and third["card"]["request"] == "add a search box"
    assert len(helpers(third)) == 1 and helpers(third)[0]["kind"] == "readback"
    assert third["dialogue"]["proposal"] == third["card"]["id"]
    assert len(prompts) == 1                                  # the answers never reached the agent
    assert not (voice_site["talk"] / "agents").exists() or not list((voice_site["talk"] / "agents").iterdir())


def test_clarify_never_guesses(talk, monkeypatch, voice_site):
    model, conv = talk
    layer = voice_site["talk"]
    failed_run(layer, "c0ffee01", "Add a word count")
    failed_run(layer, "c0ffee02", "Fix the footer")
    model.tick()
    script(monkeypatch, propose({"kind": "resume"}), propose({"kind": "resume", "run": "0badcafe"}),
           propose({"kind": "start", "target": {"kind": "workflow", "name": "plan-bild"}, "prompt": "x"}),
           propose({"kind": "resume"}))
    turn = d.helper_turn(model, conv, "resume")
    assert turn["reply"]["text"] == "Which run?" and turn["dialogue"]["slot"] == "run"
    assert sorted(o["value"] for o in turn["dialogue"]["options"]) == ["c0ffee01", "c0ffee02"]
    assert all(o["label"].startswith("run C 0 F F: ") for o in turn["dialogue"]["options"])
    assert model.proposals.items == {}
    chosen = d.helper_turn(model, conv, "the one about the footer")   # no option is called so: asked again
    assert chosen["dialogue"]["state"] == "clarify" and chosen["reply"]["text"] == f"{d.AGAIN_SAY} Which run?"
    read_back = d.helper_turn(model, conv, "c0ffee02")
    assert read_back["dialogue"]["state"] == "confirm" and read_back["card"]["run"] == "c0ffee02"
    assert d.helper_turn(model, conv, "no")["card"]["state"] == "declined"
    # a run the agent named that does not exist is asked about, never replaced
    turn = d.helper_turn(model, conv, "resume the bad one")
    assert turn["dialogue"]["slot"] == "run" and len(turn["dialogue"]["options"]) == 2
    d.helper_turn(model, conv, "cancel")
    # an unknown workflow name is a question, not a start of something else
    turn = d.helper_turn(model, conv, "start plan bild")
    assert turn["dialogue"]["slot"] == "target" and turn["card"] is None and model.proposals.open_for(conv.id) is None
    d.helper_turn(model, conv, "cancel")
    # one candidate: it is taken, and read back
    shutil.rmtree(layer / "agents" / "c0ffee01")
    model.tick()
    turn = d.helper_turn(model, conv, "resume")
    assert turn["dialogue"]["state"] == "confirm" and turn["card"]["run"] == "c0ffee02"


def test_which_layer_is_asked_when_unsure(voice_site, tmp_path, monkeypatch):
    import start_layer
    other = start_layer.build_layer(voice_site["talk"].parent, "chat", voice_site["app"], None, port_base=28790,
                                    voice=True)
    model = d.Model([voice_site["talk"], other], [], voice_dir=tmp_path / "voice", voice_fake=voice_site["fake"])
    model.discover()
    model.tick()
    conv = model.conversations.get(SESSION, "talk")
    conv.layer_sure = False
    script(monkeypatch, propose(dict(SEARCH)), propose(dict(SEARCH)))
    turn = d.helper_turn(model, conv, "plan a search box")
    assert turn["dialogue"]["slot"] == "layer" and turn["reply"]["text"] == "Which layer: talk or chat?"
    assert [o["value"] for o in turn["dialogue"]["options"]] == ["talk", "chat"] and turn["card"] is None
    turn = d.helper_turn(model, conv, "talk")                 # this layer: the conversation goes on
    assert turn["dialogue"]["state"] == "confirm" and turn["reply"]["text"].startswith("In layer talk: Start plan-build")
    assert conv.layer_sure is True and conv.layer_answered is True
    d.helper_turn(model, conv, "no")
    conv.layer_sure = False                                   # the other layer: nothing, and where to go
    turn = d.helper_turn(model, conv, "plan a search box")
    assert turn["dialogue"]["slot"] == "layer"
    turn = d.helper_turn(model, conv, "chat")
    assert turn["dialogue"]["state"] == "idle" and turn["dialogue"]["decision"] == "cancel"
    assert turn["reply"]["text"] == ("This conversation is with layer talk. Choose the other layer at the top of the "
                                     "voice panel and ask again. Nothing was done.")
    assert model.proposals.open_for(conv.id) is None


def test_one_voice_layer_asks_no_layer_question(talk, monkeypatch):
    model, conv = talk                                        # notes has no voice: one voice layer
    conv.layer_sure = False
    script(monkeypatch, propose(dict(SEARCH)))
    assert d.helper_turn(model, conv, "plan a search box")["dialogue"]["state"] == "confirm"


# ---------------------------------------------------------------- the read-back

def test_read_back_names_layer_run_action_and_request(talk, monkeypatch, voice_site):
    model, conv = talk
    failed_run(voice_site["talk"], "3fa2b1c9", "Add a word count")
    model.tick()
    script(monkeypatch, propose({"kind": "start", "target": {"kind": "workflow", "name": "plan-build"},
                                 "prompt": "Add a search box to the notes page"}),
           propose({"kind": "resume", "run": "3fa2"}))
    start = d.helper_turn(model, conv, "plan a search box")["reply"]["text"]
    assert start.startswith("In layer talk: Start plan-build: Add a search box to the notes page.")
    assert start.endswith("Shall I do it?") and "On it." not in start        # the agent's words are not used
    d.helper_turn(model, conv, "no")
    resume = d.helper_turn(model, conv, "resume run 3 f a 2")
    assert resume["card"]["run"] == "3fa2b1c9"                # a spoken prefix of one run is that run
    text = resume["reply"]["text"]
    assert text == ("In layer talk: Resume run 3 F A 2. The run continues from the step where it stopped, in its kept "
                    "worktree. Shall I do it?")
    proposal = model.proposals.get(resume["card"]["id"])
    assert d.read_back_text(model.layers["talk"], proposal) == text


def test_spoken_text_never_says_yes(talk, monkeypatch):
    model, conv = talk
    spoken = [d.DOING_SAY, d.DECLINED_SAY, d.CANCELLED_SAY, d.TIMEOUT_SAY, d.UNCLEAR_SAY, d.AGAIN_SAY,
              d.NOTHING_PENDING_SAY, d.READ_BACK_ASK, d.LOST_THREAD]
    script(monkeypatch, propose({"kind": "start", "target": {"kind": "workflow", "name": "plan-build"},
                                 "prompt": "Add a search box. Keep it small. Use the old index. Show ten hits."}))
    turn = d.helper_turn(model, conv, "plan a search box")
    spoken += [turn["reply"]["text"], turn["reply"]["speak"]]
    again = d.helper_turn(model, conv, "hmm")
    spoken += [again["reply"]["text"], again["reply"]["speak"]]
    for text in spoken:
        assert "yes" not in re.findall(r"[a-z']+", text.lower()), text
    # the read-back is spoken whole: the closing question is not cut at three sentences
    assert turn["reply"]["speak"].endswith("Shall I do it?") and "Show ten hits." in turn["reply"]["speak"]
    assert again["reply"]["speak"].endswith("Shall I do it?")


# ---------------------------------------------------------------- yes, no, silence

def test_yes_confirms_and_no_declines(talk, monkeypatch):
    model, conv = talk
    script(monkeypatch, propose(dict(SEARCH)), propose(dict(SEARCH)))
    card = d.helper_turn(model, conv, "plan a search box")["card"]
    assert card["state"] == "proposed" and card["digest"] == ""
    turn = d.helper_turn(model, conv, "yes", "voice")
    assert turn["card"]["id"] == card["id"] and turn["card"]["state"] == "confirmed" and turn["card"]["digest"]
    assert turn["dialogue"] == {**d.Dialogue().view("yes")}
    assert turn["reply"]["text"] == d.DOING_SAY
    assert helpers(turn)[0]["kind"] == "decision" and helpers(turn)[0]["decision"] == "yes"
    card = d.helper_turn(model, conv, "plan a search box again")["card"]
    turn = d.helper_turn(model, conv, "no thanks", "click")
    assert turn["card"]["state"] == "declined" and turn["dialogue"]["decision"] == "no"
    assert turn["dialogue"]["state"] == "idle" and turn["reply"]["text"] == d.DECLINED_SAY
    assert turn["entries"][0]["via"] == "click" and helpers(turn)[0]["decision"] == "no"
    assert model.proposals.approve(card["id"], SESSION, "", 1.0, flags())[0] == 409


def test_timeout_is_no(talk, monkeypatch):
    model, conv = talk
    clock = Clock()
    conv.dialogue = d.Dialogue(clock=clock)
    script(monkeypatch, propose(dict(SEARCH)), propose(dict(SEARCH)), propose({"kind": "start"}))
    before = len(conv.entries)
    idle = d.helper_turn(model, conv, "", event="timeout")    # nothing pending: nothing is said or kept
    assert idle["entries"] == [] and idle["reply"] is None and idle["card"] is None and len(conv.entries) == before
    card = d.helper_turn(model, conv, "plan a search box")["card"]
    assert conv.dialogue.expires_at() == clock.now + d.DIALOGUE_WAIT
    turn = d.helper_turn(model, conv, "", event="timeout")
    assert turn["card"]["state"] == "declined" and turn["dialogue"]["decision"] == "timeout"
    assert turn["reply"]["text"] == d.TIMEOUT_SAY and [e["who"] for e in turn["entries"]] == ["helper"]
    # a yes that comes too late is not a yes
    late = d.helper_turn(model, conv, "plan a search box again")["card"]
    clock.now += d.DIALOGUE_WAIT + d.DIALOGUE_GRACE + 1
    turn = d.helper_turn(model, conv, "yes")
    assert turn["card"]["id"] == late["id"] and turn["card"]["state"] == "declined"
    assert turn["dialogue"]["decision"] == "timeout" and turn["reply"]["text"] == d.TIMEOUT_SAY
    # a question times out the same way
    assert d.helper_turn(model, conv, "start a run")["dialogue"]["state"] == "clarify"
    turn = d.helper_turn(model, conv, "", event="timeout")
    assert turn["dialogue"]["state"] == "idle" and turn["dialogue"]["decision"] == "timeout" and turn["card"] is None
    assert {p.state for p in model.proposals.items.values()} == {"declined"} and card["id"] != late["id"]


def test_cancel_at_any_point(talk, monkeypatch):
    model, conv = talk
    prompts = script(monkeypatch, propose({"kind": "start"}), propose(dict(SEARCH)), propose({"kind": "start"}))
    idle = d.helper_turn(model, conv, "cancel")
    assert idle["reply"]["text"] == "There is nothing to cancel." and prompts == []
    assert d.helper_turn(model, conv, "start a run")["dialogue"]["state"] == "clarify"
    turn = d.helper_turn(model, conv, "cancel")
    assert turn["dialogue"]["state"] == "idle" and turn["dialogue"]["decision"] == "cancel"
    assert turn["reply"]["text"] == "Cancelled. Nothing was done." and model.proposals.items == {}
    card = d.helper_turn(model, conv, "plan a search box")["card"]
    turn = d.helper_turn(model, conv, "stop", "voice")
    assert turn["card"]["id"] == card["id"] and turn["card"]["state"] == "declined"
    assert turn["dialogue"]["decision"] == "cancel" and turn["reply"]["text"] == d.CANCELLED_SAY
    assert d.helper_turn(model, conv, "start a run")["dialogue"]["slot"] == "target"
    d.helper_turn(model, conv, "plan build")
    assert d.helper_turn(model, conv, "never mind")["dialogue"]["decision"] == "cancel"   # at the second question too
    assert model.proposals.open_for(conv.id) is None and len(prompts) == 3
    assert not [p for p in model.proposals.items.values() if p.state in ("proposed", "confirmed")]


def test_change_reads_back_again(talk, monkeypatch):
    model, conv = talk
    prompts = script(monkeypatch, propose(dict(SEARCH)), propose({**SEARCH, "class": "bug"}),
                     {"say": "I am not sure what to change."})
    first = d.helper_turn(model, conv, "plan a search box")["card"]
    turn = d.helper_turn(model, conv, "change the class to bug")
    second = turn["card"]
    assert second["id"] != first["id"] and second["class"] == "bug" and second["state"] == "proposed"
    assert model.proposals.get(first["id"]).state == "superseded"
    assert turn["dialogue"]["state"] == "confirm" and turn["dialogue"]["proposal"] == second["id"]
    assert turn["reply"]["text"].startswith("In layer talk: Start plan-build: Add a search box (class bug).")
    assert "the person wants a change to the proposal" in prompts[1] and "change the class to bug" in prompts[1]
    # a change the agent does not turn into a proposal ends the action
    turn = d.helper_turn(model, conv, "no, use something nicer")
    assert turn["reply"]["text"] == "I am not sure what to change. Nothing was done."
    assert turn["dialogue"]["state"] == "idle" and turn["dialogue"]["decision"] == "cancel" and turn["card"] is None
    assert model.proposals.get(second["id"]).state == "superseded"
    assert model.proposals.approve(second["id"], SESSION, second["digest"], 1.0, flags())[0] == 409


def test_unclear_is_asked_twice_then_cancelled(talk, monkeypatch):
    model, conv = talk
    prompts = script(monkeypatch, propose(dict(SEARCH)), propose({"kind": "start"}))
    card = d.helper_turn(model, conv, "plan a search box")["card"]
    for attempt in (1, 2):
        turn = d.helper_turn(model, conv, "maybe")
        assert turn["dialogue"]["state"] == "confirm" and turn["dialogue"]["retries"] == attempt
        assert turn["reply"]["text"] == f"{d.AGAIN_SAY} {card['summary']} {d.READ_BACK_ASK}"
        assert helpers(turn)[0]["kind"] == "readback" and turn["card"]["state"] == "proposed"
    turn = d.helper_turn(model, conv, "approve it")
    assert turn["dialogue"]["state"] == "idle" and turn["dialogue"]["decision"] == "unclear"
    assert turn["reply"]["text"] == d.UNCLEAR_SAY and turn["card"]["state"] == "declined"
    assert d.helper_turn(model, conv, "start a run")["dialogue"]["slot"] == "target"
    for attempt in (1, 2):
        turn = d.helper_turn(model, conv, "the nice one")
        assert turn["dialogue"]["state"] == "clarify" and turn["dialogue"]["retries"] == attempt
        assert turn["reply"]["text"] == f"{d.AGAIN_SAY} Which workflow or command?"
    turn = d.helper_turn(model, conv, "the nice one")
    assert turn["dialogue"]["state"] == "idle" and turn["dialogue"]["decision"] == "unclear"
    assert turn["reply"]["text"] == d.UNCLEAR_SAY and len(prompts) == 2 and model.proposals.open_for(conv.id) is None


# ---------------------------------------------------------------- read-only questions

def test_read_only_questions_need_no_confirmation(talk, monkeypatch, voice_site):
    model, conv = talk
    failed_run(voice_site["talk"], "c0ffee01", "Add a word count")
    failed_run(voice_site["notes"], "feed0001", "Elsewhere")
    failed_run(voice_site["talk"], "c0ffee03", "Draw the map", status="passed")
    model.tick()
    model.refresh_attention(model.layers["talk"])
    assert list(d.HELPER_TOOLS).index("needs_you") == list(d.HELPER_TOOLS).index("propose") - 1
    needs = d.run_helper_tool(model, conv, "needs_you", {})
    assert needs["total"] == 1 and [r["run"] for r in needs["runs"]] == ["c0ffee01"]
    assert set(needs["runs"][0]) == {"run", "status", "workflow", "request", "reason", "next"}
    assert needs["runs"][0]["status"] == "failed" and needs["runs"][0]["request"] == "Add a word count"
    assert "takes no arguments" in d.run_helper_tool(model, conv, "needs_you", {"layer": "notes"})["error"]
    for tool, args, question in [("list_runs", {"limit": 3}, "what is running"),
                                 ("run_summary", {"run": "c0ffee01"}, "what did run c0ff do"),
                                 ("needs_you", {}, "what needs me")]:
        script(monkeypatch, {"say": "Let me look.", "tool": tool, "args": args}, {"say": "One run failed."})
        turn = d.helper_turn(model, conv, question)
        assert turn["dialogue"]["state"] == "idle" and turn["dialogue"]["decision"] == "" and turn["card"] is None
        assert turn["reply"]["text"] == "One run failed." and "kind" not in helpers(turn)[0]
    assert model.proposals.items == {}


# ---------------------------------------------------------------- approve & resume, give to an agent, settle

def test_new_kinds_validate_and_read_back(talk, voice_site, tmp_path):
    model, conv = talk
    layer_dir = voice_site["talk"]
    records = ap.add_approvable_runs(layer_dir)
    failed_run(layer_dir, "c0ffee01", "Add a word count")
    failed_run(layer_dir, "c0ffee09", "Long job", status="running")
    model.tick()
    layer = model.layers["talk"]
    model.refresh_attention(layer)

    def check(action):
        return d.validate_action(model, layer, conv, action)

    # approve & resume: the items and the token are the run's own approval
    action, error = check({"kind": "approve-resume", "run": ap.PROOF, "who": "Robin Example", "note": "agreed"})
    assert error == "", error
    assert action == {"kind": "approve-resume", "run": ap.PROOF, "who": "Robin Example", "note": "agreed",
                      "items": ["vocabulary"], "item_texts": [ap.PROOF_ITEM["text"]],
                      "token": records[ap.PROOF]["token"]}
    summary, what = d.proposal_text(model, layer, action)
    assert summary == (f"Approve 1 item of run C 2 0 0 as Robin Example ({ap.PROOF_ITEM['text']}) and resume it.")
    assert what == "the approvals are recorded with your name and the run continues"
    assert check({"kind": "approve-resume", "run": "c0ffee01", "who": "Robin"})[1].endswith("does not wait for an approval")
    assert check({"kind": "approve-resume", "run": ap.STALE, "who": "Robin"})[0] is None   # a record of another state
    assert "who" in check({"kind": "approve-resume", "run": ap.PROOF, "who": ""})[1]
    assert "note" in check({"kind": "approve-resume", "run": ap.PROOF, "who": "R", "note": "x" * 501})[1]
    assert "not token" in check({"kind": "approve-resume", "run": ap.PROOF, "who": "R", "token": "x"})[1]
    card = d.card_view(model.proposals.create(conv, action, summary, what), model)
    assert card["kind"] == "approve-resume" and card["run"] == ap.PROOF and card["request"] == "agreed"
    assert "token" not in json.dumps(card) and records[ap.PROOF]["token"] not in json.dumps(card)
    # settle
    action, error = check({"kind": "settle", "run": "c0ffee01", "as": "abandoned", "who": " Ada ", "note": "no  longer\nneeded"})
    assert error == "" and action == {"kind": "settle", "run": "c0ffee01", "as": "abandoned", "by": "", "who": "Ada",
                                      "note": "no longer needed"}
    assert d.proposal_text(model, layer, action) == (
        "Settle run C 0 F F as abandoned, recorded for Ada.",
        "a settlement record is written beside the run; the run's own records do not change")
    by, error = check({"kind": "settle", "run": "c0ffee01", "as": "superseded", "who": "Ada", "by": ap.PROOF})
    assert error == "" and d.proposal_text(model, layer, by)[0] == f"Settle run C 0 F F as superseded by {ap.PROOF}, recorded for Ada."
    assert check({"kind": "settle", "run": "c0ffee01", "as": "superseded", "who": "Ada"})[1] == \
        "superseded needs by: the run or commit that replaced it"
    assert check({"kind": "settle", "run": "c0ffee01", "as": "done", "who": "Ada"})[1] == \
        "as is one of landed, superseded, abandoned"
    assert check({"kind": "settle", "run": "c0ffee01", "as": "landed", "who": "Ada", "by": "not a run"})[1].startswith("by names")
    assert check({"kind": "settle", "run": "c0ffee09", "as": "abandoned", "who": "Ada"})[1] == \
        "only a finished run can be settled"
    # what the flags allow
    for kind, read_only, no_start in [("settle", False, True), ("approve-resume", False, False), ("assign", False, False)]:
        assert d.approvable({"kind": kind}, flags(read_only=True)) == (False, "the dashboard was started with --read-only")
        assert d.approvable({"kind": kind}, flags(no_start=True))[0] is no_start, kind
        assert d.approvable({"kind": kind}, flags())[0] is True
    assert d.approvable({"kind": "settle"}, flags(no_actions=True, in_container=True))[0] is True


def test_assign_takes_the_previews_nonce(tmp_path, monkeypatch):
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN", "AL_DECIDER"):
        monkeypatch.delenv(name, raising=False)
    scenario = af.build_scenario(tmp_path / "s")
    model = d.Model([scenario["layer"]], [], voice_dir=tmp_path / "voice")
    model.discover()
    model.tick()
    layer = model.layers["pantry"]
    model.refresh_attention(layer)
    conv = model.conversations.get(SESSION, "pantry")
    action, error = d.validate_action(model, layer, conv, {"kind": "assign", "run": af.FAILED_CHECK, "note": "mind the order"})
    assert error == "", error
    assert action["kind"] == "assign" and action["run"] == af.FAILED_CHECK and action["note"] == "mind the order"
    assert action["plan_kind"] == af.KINDS[af.FAILED_CHECK] == "check"
    assert action["nonce"] in model.assign_nonces             # the preview's own confirmation
    summary, what = d.proposal_text(model, layer, action)
    assert summary == "Give run F 0 0 0 to an agent (check)."
    assert what == "the layer's assign script starts the plan of the preview, detached"
    card = d.card_view(model.proposals.create(conv, action, summary, what), model)
    assert card["request"] == "mind the order" and action["nonce"] not in json.dumps(card) and "nonce" not in card
    # a plan that only a person can carry out is no proposal
    refused, error = d.validate_action(model, layer, conv, {"kind": "assign", "run": af.PERSON})
    assert refused is None and error, error
    assert d.validate_action(model, layer, conv, {"kind": "assign", "run": af.REJECTED, "note": "x" * 501})[0] is None
    # the candidates of "give a run to an agent" are the runs under Needs you
    ids = [s["id"] for s in d._run_candidates(model, layer, "assign")]
    assert ids and len(ids) <= 5 and set(ids) <= set(af.KINDS)


def test_who_and_as_are_asked(talk, voice_site, monkeypatch):
    model, conv = talk
    failed_run(voice_site["talk"], "c0ffee01", "Add a word count")
    model.tick()
    model.refresh_attention(model.layers["talk"])
    script(monkeypatch, propose({"kind": "settle"}), propose({"kind": "settle", "as": "abandoned"}))
    turn = d.helper_turn(model, conv, "settle the failed run")   # one candidate: the run is not asked
    assert turn["dialogue"]["slot"] == "as"
    assert turn["reply"]["text"] == "Settle it as landed, superseded or abandoned?"
    assert [o["value"] for o in turn["dialogue"]["options"]] == ["landed", "superseded", "abandoned"]
    turn = d.helper_turn(model, conv, "superseded")
    assert turn["dialogue"]["slot"] == "by" and turn["reply"]["text"] == "Superseded by which run?"
    turn = d.helper_turn(model, conv, "abc1234")
    assert turn["dialogue"]["slot"] == "who" and turn["reply"]["text"] == "Who settles it? Say your name."
    turn = d.helper_turn(model, conv, "Ada")
    assert turn["dialogue"]["state"] == "confirm"
    assert turn["reply"]["text"].startswith("In layer talk: Settle run C 0 F F as superseded by abc1234, recorded for Ada.")
    d.helper_turn(model, conv, "no")
    turn = d.helper_turn(model, conv, "settle it as abandoned")   # the name is remembered in this conversation
    assert turn["dialogue"]["state"] == "confirm" and conv.who == "Ada"
    assert "as abandoned, recorded for Ada." in turn["reply"]["text"]


def test_conversation_clear(talk, monkeypatch):
    model, conv = talk
    script(monkeypatch, propose({"kind": "start"}))
    d.helper_turn(model, conv, "start a run")
    assert conv.path.is_file() and conv.entries and conv.replies and conv.dialogue.pending()
    conv.clear()
    assert conv.entries == [] and conv.replies == {} and not conv.path.exists() and conv.dialogue.state == "idle"
    conv.clear()                                              # a missing file is fine
    conv.add("person", "hello")
    assert conv.path.is_file() and len(conv.entries) == 1
