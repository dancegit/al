"""The voice helper's turn-by-turn conversation in a real Chromium with a fake microphone and the fake
speech engines (the harness of test_browser_voice.py; the helper is the layer's mock agent): a request
with missing parts is asked about one question at a time, the complete action is read back, and only a
yes — spoken or clicked — does it; cancel, no and silence do nothing; every step works by keyboard; the
transcript shows the read-back and the decision and can be cleared. Skipped without Playwright or Chrome;
nothing is downloaded.

With AL_DASHBOARD_SHOTS=<directory> it saves voice-talk-clarify-1280.png (the first question with its
options), voice-talk-readback-{1280,390}.png (waiting for yes or no), voice-talk-done-1280.png (the run
link and "decision: yes"), voice-talk-cancelled-1280.png and voice-talk-declined-1280.png."""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

import dashboard
import voice_fixtures
from test_browser_voice import (REQUEST, SPEECH, Voice, helper_rules, pw, run_dirs, shot, shots_dir, until,  # noqa: F401
                                voice, wait_finished)

pytestmark = pytest.mark.browser

PANEL = "document.querySelector('.voice-panel')"
CAPTION = "document.querySelector('.voice-caption-helper').textContent"


# the panel's parts that reach beyond the panel, or the panel beyond the window: none (no overflow at 390 px)
INSIDE = """(() => {
  const panel = document.querySelector('.voice-panel').getBoundingClientRect();
  const out = panel.left < 0 || panel.right > window.innerWidth + 1 ? ['panel'] : [];
  for (const el of document.querySelectorAll('.voice-panel *')) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && (r.right > panel.right + 1 || r.left < panel.left - 1)) out.push(el.className || el.tagName);
  }
  return out;
})()"""


def talk_rules(layer: Path) -> None:
    """mock/helper.json for these conversations (written after Voice.open wrote its own): a search box is a
    complete proposal; "start a run" and "resume" are proposals with parts missing; "change" proposes the
    same workflow with another request."""
    def start(prompt: str) -> dict:
        return {"say": "I can prepare that.", "tool": "propose", "args": {"action": {
            "kind": "start", "target": {"kind": "workflow", "name": "plan-build"}, "prompt": prompt}}}

    voice_fixtures.helper_mock(layer, [("search box", start("add a search box")),
                                       ("start a run", voice_fixtures.PROPOSE_BARE_START),
                                       ("^resume\\b", voice_fixtures.PROPOSE_RESUME_NO_RUN),
                                       ("change", start("add a filter"))],
                               {"say": "I can tell you about the runs, or prepare one for you."})


def dialogue(page, state: str, timeout: int = 20000) -> None:
    until(page, f"{PANEL}.dataset.dialogue === {json.dumps(state)} && {PANEL}.dataset.state !== 'thinking'", timeout=timeout)


def speak_line(v: Voice, heard: str) -> None:
    """One spoken line: hold the button while the fake microphone plays; the fake recogniser hears `heard`."""
    v.hold(1.0)
    until(v.page, f"document.querySelector('.voice-caption-person').textContent === {json.dumps(heard)}", timeout=20000)
    until(v.page, f"{PANEL}.dataset.state !== 'thinking'", timeout=20000)


def question(page) -> str:
    return page.inner_text(".voice-ask-question")


def start_a_run(v: Voice, width: int, yes: str = "spoken", shots: bool = False) -> str:
    """The whole conversation: "start a run" → which workflow → what should it do → the read-back → yes.
    → the id of the run the yes started."""
    page = v.open(width, transcripts=["start a run", "plan build", "add a search box", "yes"], mic=SPEECH)
    talk_rules(v.layer)
    speak_line(v, "start a run")
    dialogue(page, "clarify")
    assert question(page) == "Which workflow or command?"
    options = page.locator(".voice-ask-options .voice-option")
    assert options.count() >= 2 and options.nth(0).inner_text() == "plan-build"
    assert page.locator(".voice-yes").is_hidden() and page.locator(".voice-cancel").is_visible()
    until(page, f"{PANEL}.dataset.state === 'asking'")
    assert "Waiting for your answer" in page.inner_text(".voice-state")
    if shots and width == 1280:
        shot(page, "voice-talk-clarify-1280.png")
    speak_line(v, "plan build")
    until(page, "document.querySelector('.voice-ask-question').textContent === 'What should it do?'")
    assert page.locator(".voice-ask-options .voice-option").count() == 0 and page.locator(".voice-card").count() == 0
    speak_line(v, "add a search box")
    dialogue(page, "confirm")
    v.card("proposed")
    read_back = page.evaluate(CAPTION)
    assert read_back.startswith("In layer talk: Start plan-build: add a search box.") and read_back.endswith("Shall I do it?")
    assert question(page) == "Shall I do it?" and page.locator(".voice-yes").is_visible()
    assert page.locator(".voice-card .voice-approve").count() == 0
    v.wait_state("waiting")
    assert page.inner_text(".voice-state").startswith("Waiting for yes or no")
    assert "answer within" in page.inner_text(".voice-wait")
    if shots:
        assert page.evaluate(INSIDE) == [], "something of the panel is wider than the panel"
        shot(page, f"voice-talk-readback-{width}.png")
    time.sleep(1.0)
    assert run_dirs(v.layer) == []                            # nothing before the yes
    if yes == "spoken":
        speak_line(v, "yes")
    else:
        page.click(".voice-ask .voice-yes")
    page.wait_for_selector(".voice-card[data-state='approved'] a.voice-run-link", timeout=30000)
    dialogue(page, "idle")
    assert page.locator(".voice-ask").is_hidden()
    run_id = page.get_attribute(".voice-card a.voice-run-link", "href").rsplit("/", 1)[1]
    until(page, "document.querySelector('.voice-transcript').textContent.includes('decision: yes')")
    until(page, f"{CAPTION}.startsWith('Done. Started run')")
    return run_id


@pytest.mark.parametrize("width,yes", [(1280, "spoken"), (390, "clicked")])
def test_spoken_conversation_starts_a_run(voice, width, yes):
    run_id = start_a_run(voice, width, yes, shots=True)
    page = voice.page
    marks = page.evaluate("[...document.querySelectorAll('.voice-mark')].map(e => e.textContent)")
    assert marks == ["question", "question", "read-back", "decision: yes", "result"], marks
    vias = page.evaluate("[...document.querySelectorAll('.voice-entry.voice-person .voice-via')].map(e => e.textContent.trim())")
    assert vias == ["(spoken)"] * 3 + ["(spoken)" if yes == "spoken" else "(clicked)"], vias
    if width == 1280:
        shot(page, "voice-talk-done-1280.png")
    state = wait_finished(voice.layer, run_id)
    assert state["status"] == "passed" and state["adw"] == "plan-build" and state["prompt"] == "add a search box"
    assert run_dirs(voice.layer) == [run_id]


def test_states_are_visible(voice):
    start_a_run(voice, 1280)
    order = voice.states()
    for state in ("listening", "thinking", "speaking", "asking", "waiting"):
        assert state in order, (state, order)
    assert order.index("listening") < order.index("thinking") < order.index("speaking") < order.index("waiting"), order
    assert voice.page.evaluate("document.querySelector('.voice-state').getAttribute('aria-live')") == "polite"


@pytest.mark.parametrize("how", ["spoken", "clicked"])
def test_cancel_midway_does_nothing(voice, how):
    page = voice.open(1280, transcripts=["start a run", "cancel"], mic=SPEECH)
    talk_rules(voice.layer)
    speak_line(voice, "start a run")
    dialogue(page, "clarify")
    if how == "spoken":
        speak_line(voice, "cancel")
    else:
        page.click(".voice-ask .voice-cancel")
    dialogue(page, "idle")
    until(page, f"{CAPTION} === 'Cancelled. Nothing was done.'")
    assert page.locator(".voice-ask").is_hidden() and page.locator(".voice-card").count() == 0
    until(page, "document.querySelector('.voice-transcript').textContent.includes('decision: cancel')")
    if how == "spoken":
        shot(page, "voice-talk-cancelled-1280.png")
    time.sleep(1.0)
    assert run_dirs(voice.layer) == []


def test_no_declines(voice):
    page = voice.open(1280, transcripts=[REQUEST, "no"], mic=SPEECH)
    talk_rules(voice.layer)
    speak_line(voice, REQUEST)
    dialogue(page, "confirm")
    voice.card("proposed")
    speak_line(voice, "no")
    voice.card("declined")
    dialogue(page, "idle")
    until(page, f"{CAPTION} === \"All right, I won't do it. Nothing was done.\"")
    until(page, "document.querySelector('.voice-transcript').textContent.includes('decision: no')")
    assert page.locator(".voice-card .voice-approve").count() == 0 and page.locator(".voice-ask").is_hidden()
    shot(page, "voice-talk-declined-1280.png")
    time.sleep(1.0)
    assert run_dirs(voice.layer) == []


def tab_to(page, selector: str, key: str = "Shift+Tab") -> None:
    """Move the keyboard focus (never the mouse) until it is on `selector`."""
    for _ in range(40):
        if page.evaluate(f"!!document.activeElement && document.activeElement.matches({json.dumps(selector)})"):
            return
        page.keyboard.press(key)
    raise AssertionError(f"the keyboard never reached {selector}")


def test_ambiguous_then_clarified_by_keyboard(voice):
    runs = {"c0ffee01": "Add a word count", "beef0002": "Fix the footer"}
    for run_id, prompt in runs.items():
        voice_fixtures.state_json(voice.layer, run_id, adw="plan-build", prompt=prompt, status="failed",
                                  host=socket.gethostname(), ended="2026-11-17T10:00:00+00:00")
    page = voice.open(1280)
    talk_rules(voice.layer)
    page.focus(".voice-text")
    page.keyboard.type("resume")
    page.keyboard.press("Enter")
    dialogue(page, "clarify")
    assert question(page) == "Which run?"
    options = page.locator(".voice-ask-options .voice-option")
    assert options.count() == 2
    second = options.nth(1).get_attribute("data-value")
    assert second in runs and runs[second] in options.nth(1).inner_text()
    tab_to(page, f".voice-option[data-value='{second}']")
    page.keyboard.press("Enter")
    dialogue(page, "confirm")
    spoken = "run " + " ".join(second[:4].upper())
    assert page.evaluate(CAPTION) == (f"In layer talk: Resume {spoken}. The run continues from the step where it "
                                      "stopped, in its kept worktree. Shall I do it?")
    assert page.inner_text(".voice-card .voice-card-summary") == f"Resume {spoken}."
    tab_to(page, ".voice-no")
    page.keyboard.press("Enter")
    voice.card("declined")
    dialogue(page, "idle")
    until(page, "document.querySelector('.voice-transcript').textContent.includes('decision: no')")
    vias = page.evaluate("[...document.querySelectorAll('.voice-entry.voice-person .voice-via')].map(e => e.textContent.trim())")
    assert vias == ["(clicked)", "(clicked)"]                 # the typed line has no mark
    time.sleep(1.0)
    assert run_dirs(voice.layer) == sorted(runs)
    for run_id in runs:
        assert json.loads((voice.layer / "agents" / run_id / "state.json").read_text())["status"] == "failed"
        assert not (voice.layer / "agents" / run_id / "launch.log").exists()


def test_silence_counts_as_no(voice, monkeypatch):
    monkeypatch.setattr(dashboard, "DIALOGUE_WAIT", 3.0)
    page = voice.open(1280)
    talk_rules(voice.layer)
    voice.type_(REQUEST)
    dialogue(page, "confirm")
    voice.card("proposed")
    voice.wait_state("waiting")
    dialogue(page, "idle", timeout=30000)                     # nobody answered
    voice.card("declined")
    until(page, "document.querySelector('.voice-transcript').textContent.includes('decision: timeout')")
    until(page, f"{CAPTION} === 'I heard no answer, so I cancelled it. Nothing was done.'")
    assert "waiting" in voice.states() and voice.state() != "waiting"
    assert page.locator(".voice-ask").is_hidden()
    time.sleep(1.0)
    assert run_dirs(voice.layer) == []


def test_clear_transcript_button(voice):
    page = voice.open(1280)
    talk_rules(voice.layer)
    voice.type_(REQUEST)
    dialogue(page, "confirm")
    voice.type_("change it")                                  # read back again, with the other request
    until(page, f"{CAPTION}.startsWith('In layer talk: Start plan-build: add a filter.')")
    dialogue(page, "confirm")
    until(page, "document.querySelectorAll(\".voice-card[data-state='superseded']\").length === 1")
    assert page.locator(".voice-entry").count() >= 4 and page.locator(".voice-card").count() == 2
    page.focus(".voice-clear")
    page.keyboard.press(" ")                                  # Space presses the button, it does not open the microphone
    until(page, "document.querySelectorAll('.voice-entry').length === 0 && document.querySelectorAll('.voice-card').length === 0")
    dialogue(page, "idle")
    assert page.inner_text(".voice-note") == "The transcript was cleared."
    assert page.locator(".voice-ask").is_hidden() and page.evaluate(CAPTION) == ""
    assert page.evaluate(f"{PANEL}.dataset.mic") == "closed"
    assert not list(voice.site["voice_dir"].glob("*.jsonl"))
    voice.type_("yes")                                        # nothing waits for a yes any more
    until(page, "document.querySelectorAll('.voice-entry.voice-helper').length === 1")
    time.sleep(1.0)
    assert run_dirs(voice.layer) == [] and page.locator(".voice-card").count() == 0
