"""The voice helper panel in a real Chromium with a fake microphone and the fake speech engines
(`--voice-fake`: transcripts from a script, a 220 Hz tone for speech with a silence after each comma).
The helper is the layer's mock agent (mock/helper.json). Skipped without Playwright or Chrome.

Each test launches its own Chromium with `--use-fake-device-for-media-stream` and
`--use-file-for-fake-audio-capture=<a WAV this test writes>`, at 1280×900 and/or 390×844. With
AL_DASHBOARD_SHOTS=<directory> it saves voice-{idle,listening,thinking,speaking,interrupted,card,
approved,read-only}-{1280,390}.png, voice-reduced-motion-1280.png and a recording of a whole exchange,
voice-exchange-{1280,390}.webm (Playwright's video; when Playwright's own ffmpeg is not installed the
system ffmpeg is used, else the videos are left out)."""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import struct
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Iterator

import pytest

import voice_fixtures
from conftest import start_server, stop_server
from test_browser import CHROME, playwright_api, until  # noqa: F401 — skips without Playwright or Chrome

pytestmark = pytest.mark.browser

WIDTHS = {1280: (1280, 900), 390: (390, 844)}
REQUEST = "start a plan for adding a search box"
RUN_DIR = re.compile(r"^[0-9a-f]{8}$")

# The page records every data-state the panel takes (with performance.now()) and every microphone
# stream it opens, so a test can check the order of states and that a closed microphone's track ended.
WATCH = """
(() => {
  window.__voiceStates = [];
  window.__streams = [];
  window.__micOpened = [];
  const watch = () => {
    const panel = document.querySelector('.voice-panel');
    if (!panel) { setTimeout(watch, 20); return; }
    let last = '';
    const note = () => {
      const s = panel.dataset.state;
      if (s !== last) { last = s; window.__voiceStates.push([s, performance.now()]); }
    };
    note();
    new MutationObserver(note).observe(panel, { attributes: true, attributeFilter: ['data-state'] });
  };
  document.addEventListener('DOMContentLoaded', watch);
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      const stream = await original(constraints);
      window.__streams.push(stream);
      window.__micOpened.push(performance.now());
      return stream;
    };
  }
})();
"""


# ---------------------------------------------------------------- fake microphone, helper, artefacts

def mic_wav(path: Path, pattern: list[tuple[float, float]], rate: int = 48000) -> Path:
    """A 16-bit mono WAV of (seconds, amplitude) pieces: amplitude 0 is silence, else a speech-like
    sound (two tones, a syllable rhythm of 4 Hz) at that peak. Chrome loops it as the microphone."""
    frames = bytearray()
    t0 = 0
    for seconds, amplitude in pattern:
        n = int(seconds * rate)
        for i in range(n):
            t = (t0 + i) / rate
            if amplitude:
                syllable = 0.55 + 0.45 * abs(math.sin(math.pi * 4 * t))
                v = amplitude * syllable * (0.6 * math.sin(2 * math.pi * 180 * t) + 0.4 * math.sin(2 * math.pi * 440 * t))
            else:
                v = 0.0
            frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
        t0 += n
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    return path


# talking all the time (hold to talk; barge-in): syllables of 0.45 s with pauses shorter than the 600 ms that
# end an utterance; a steady tone would be taken for noise by the browser's noise suppression and fade
SPEECH = [(0.2, 0.0)] + [(0.45, 0.9), (0.3, 0.0)] * 10
TURNS = [(1.0, 0.0), (1.6, 0.8), (3.0, 0.0)]        # an utterance, then a pause long enough to end it


def helper_rules(layer: Path, say_extra: str = "", prompt: str = "add a search box") -> None:
    """mock/helper.json: a request for a search box is proposed as plan-build; "read me something"
    gets a long reply (several sentences: one is playing while the rest wait); "count slowly" a reply
    with commas (loud and silent stretches of the fake voice); anything else a short answer."""
    propose = {"say": f"I can prepare that.{say_extra}", "tool": "propose",
               "args": {"action": {"kind": "start", "target": {"kind": "workflow", "name": "plan-build"},
                                   "prompt": prompt}}}
    long_say = ("Here is the state of things, the last run passed, the one before it failed at the test step. "
                "Nothing is waiting, so the queue is empty and the next run can start whenever you like. "
                "Tell me what you would like to do next, and I will prepare it.")
    count = "One two three four five, six seven eight nine ten, eleven twelve thirteen fourteen, fifteen sixteen."
    voice_fixtures.helper_mock(layer, [("search box", propose), ("read me something", {"say": long_say}),
                                       ("count slowly", {"say": count}),
                                       ("hostile", {"say": 'Here: <img src=x onerror="window.__pwned=1"> and <b>bold</b>.'})],
                               {"say": "I can tell you about the runs, or prepare one for you."})


def shots_dir() -> Path | None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if not directory:
        return None
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    return path


def shot(page, name: str) -> None:
    """The panel (the whole of it, however tall) as DIR/name."""
    directory = shots_dir()
    if directory:
        page.locator(".voice-panel").screenshot(path=str(directory / name), animations="allow")


def run_dirs(layer: Path) -> list[str]:
    agents = layer / "agents"
    return sorted(p.name for p in agents.iterdir() if p.is_dir() and RUN_DIR.match(p.name)) if agents.is_dir() else []


def wait_finished(layer: Path, run_id: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, ValueError):
            state = json.loads((layer / "agents" / run_id / "state.json").read_text())
            if state.get("status") in ("passed", "failed") and state.get("ended"):
                return state
        time.sleep(0.3)
    raise AssertionError(f"run {run_id} did not finish")


def _video_ffmpeg() -> Path | None:
    """A browsers directory where Playwright finds an ffmpeg for its videos: none needed when its own
    is installed; else a temporary one whose ffmpeg-<revision>/ffmpeg-linux is the system ffmpeg."""
    try:
        import playwright
        manifest = json.loads((Path(playwright.__file__).parent / "driver" / "package" / "browsers.json").read_text())
        revision = next(b["revision"] for b in manifest["browsers"] if b["name"] == "ffmpeg")
    except (OSError, ValueError, StopIteration, KeyError):
        return None
    default = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    if (default / f"ffmpeg-{revision}").is_dir():
        return default
    system = shutil.which("ffmpeg")
    if not system:
        return None
    root = Path(tempfile.mkdtemp(prefix="al-voice-pw-"))
    (root / f"ffmpeg-{revision}").mkdir()
    (root / f"ffmpeg-{revision}" / "ffmpeg-linux").symlink_to(system)
    return root


@pytest.fixture(scope="module")
def pw() -> Iterator[Any]:
    browsers = _video_ffmpeg() if shots_dir() else None
    previous = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    try:
        with playwright_api.sync_playwright() as p:
            if previous is None:
                os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            else:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous
            p.can_record = browsers is not None  # type: ignore[attr-defined]
            yield p
    finally:
        if previous is None:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous


class Voice:
    """One test's dashboard (the `talk` layer, fake engines) and Chromium with a fake microphone."""

    def __init__(self, pw, site: dict[str, Path], tmp: Path) -> None:
        self.pw = pw
        self.site = site
        self.layer = site["talk"]
        self.tmp = tmp
        self.running = None
        self.browser = None
        self.context = None
        self.page = None
        self.video_name = ""
        self.errors: list[str] = []

    def open(self, width: int = 1280, *, transcripts: list[str] | None = None, mic: list[tuple[float, float]] | None = None,
             args: tuple[str, ...] = (), fake: bool = True, reduced_motion: str | None = None, video: str = "",
             init: str = "", route: str = "", mic_file: Path | None = None) -> Any:
        helper_rules(self.layer)
        script = voice_fixtures.fake_script(self.site["fake"], transcripts or [])
        server_args = ["--layer", str(self.layer), "--voice-dir", str(self.site["voice_dir"]), *args]
        if fake:
            server_args += ["--voice-fake", str(script)]
        self.running = start_server(server_args)
        wav = mic_file or mic_wav(self.tmp / f"mic-{width}.wav", mic or [(1.0, 0.0)])
        self.browser = self.pw.chromium.launch(executable_path=CHROME, headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={wav}", "--autoplay-policy=no-user-gesture-required"])
        options: dict[str, Any] = {"viewport": dict(zip(("width", "height"), WIDTHS[width]))}
        if reduced_motion:
            options["reduced_motion"] = reduced_motion
        if video and shots_dir() and getattr(self.pw, "can_record", False):
            options["record_video_dir"] = str(self.tmp / "video")
            options["record_video_size"] = options["viewport"]
            self.video_name = video
        self.context = self.browser.new_context(**options)
        with contextlib.suppress(Exception):
            self.context.grant_permissions(["microphone"], origin=self.running.base)
        self.context.add_init_script(WATCH + init)
        page = self.context.new_page()
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        page.on("console", lambda msg: self.errors.append(msg.text)
                if msg.type == "error" and "Failed to load resource" not in msg.text else None)
        self.page = page
        page.goto(f"{self.running.base}/#{route or '/layer/talk'}")
        page.wait_for_selector("#conn.conn-live", timeout=15000)
        page.wait_for_selector(".voice-panel[data-session='ready']", timeout=15000)
        return page

    def close(self) -> None:
        video = self.page.video if (self.page is not None and self.video_name) else None
        with contextlib.suppress(Exception):
            if self.context is not None:
                self.context.close()
        if video is not None:
            with contextlib.suppress(Exception):
                video.save_as(str(shots_dir() / self.video_name))
        with contextlib.suppress(Exception):
            if self.browser is not None:
                self.browser.close()
        if self.running is not None:
            stop_server(self.running)

    # -- page helpers
    def state(self) -> str:
        return self.page.evaluate("document.querySelector('.voice-panel').dataset.state")

    def states(self) -> list[str]:
        return [s for s, _t in self.page.evaluate("window.__voiceStates")]

    def wait_state(self, state: str, timeout: float = 20) -> None:
        until(self.page, f"document.querySelector('.voice-panel').dataset.state === {json.dumps(state)}",
              timeout=int(timeout * 1000))

    def seen(self, state: str, timeout: float = 20) -> None:
        until(self.page, f"window.__voiceStates.some(([s]) => s === {json.dumps(state)})", timeout=int(timeout * 1000))

    def level(self) -> float:
        return float(self.page.evaluate(
            "(() => { const el = document.querySelector('.voice-synth, .voice-level'); return el ? el.dataset.level || '0' : '0'; })()"))

    def type_(self, text: str) -> None:
        self.page.fill(".voice-text", text)
        self.page.click(".voice-send")

    def card(self, state: str = "", timeout: float = 30) -> Any:
        selector = f".voice-card[data-state='{state}']" if state else ".voice-card"
        self.page.wait_for_selector(selector, timeout=int(timeout * 1000))
        return self.page.locator(selector).first

    def card_fields(self) -> dict[str, str]:
        return self.page.evaluate("""(() => {
          const out = {};
          const card = document.querySelector('.voice-card');
          const dts = card.querySelectorAll('dt');
          for (const dt of dts) out[dt.textContent] = dt.nextElementSibling.textContent;
          return out;
        })()""")

    def hold(self, seconds: float = 1.5, key: bool = False, during=None) -> None:
        """Hold the talk button (or Space with the focus in the panel) for `seconds`."""
        page = self.page
        if key:
            page.focus(".voice-talk")
            page.keyboard.down(" ")
        else:
            box = page.locator(".voice-talk").bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
        end = time.monotonic() + seconds
        if during is not None:
            during()
        while time.monotonic() < end:
            time.sleep(0.05)
        if key:
            page.keyboard.up(" ")
        else:
            page.mouse.up()


@pytest.fixture()
def voice(pw, voice_site, tmp_path: Path) -> Iterator[Voice]:
    v = Voice(pw, voice_site, tmp_path)
    try:
        yield v
    finally:
        v.close()
    problems = [e for e in v.errors if "favicon" not in e]
    assert not problems, problems


# ---------------------------------------------------------------- tests

@pytest.mark.parametrize("width", [1280, 390])
def test_panel_states_and_labels(voice, width):
    page = voice.open(width)
    assert voice.state() == "idle"
    assert page.locator(".voice-mic-open").is_hidden()
    assert page.locator(".voice-panel[data-mic='closed']").count() == 1
    names = page.evaluate("""(() => {
      const out = [];
      for (const el of document.querySelectorAll('.voice-panel button, .voice-panel input')) {
        const label = el.closest('label');
        const name = (el.getAttribute('aria-label') || (label ? label.textContent : '') || el.textContent || '').trim();
        out.push([el.className || el.type, name]);
      }
      return out;
    })()""")
    assert names and all(name for _cls, name in names), names
    labels = {name for _cls, name in names}
    assert {"Hold to talk", "Always listen", "Keep recordings for this conversation", "Send", "Message to the helper",
            "Stop speaking", "Close microphone"} <= labels, labels
    assert page.locator(".voice-panel [aria-live='polite']").count() >= 1
    synth = page.locator("canvas.voice-synth")
    assert synth.count() == 1 and synth.get_attribute("role") == "img" and "idle" in synth.get_attribute("aria-label")
    assert page.locator(".voice-talk").get_attribute("aria-pressed") == "false"
    assert not page.locator(".voice-talk").is_disabled()
    shot(page, f"voice-idle-{width}.png")
    if width == 390:   # the panel fits the narrow screen: nothing scrolls sideways
        assert page.evaluate("(() => { const p = document.querySelector('.voice-panel'); return p.scrollWidth <= p.clientWidth + 1; })()")
        box = page.locator(".voice-panel").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 391


def test_panel_without_a_secure_context_offers_typing(voice):
    page = voice.open(1280, init="Object.defineProperty(window, 'isSecureContext', { get: () => false });")
    until(page, "document.querySelector('.voice-note').textContent.includes('only on loopback or https')")
    assert page.locator(".voice-talk").is_disabled()
    assert not page.locator(".voice-text").is_disabled()
    voice.type_("what can you do")
    page.wait_for_selector(".voice-entry.voice-helper")
    assert "about the runs" in page.inner_text(".voice-caption-helper")


def test_panel_not_set_up_names_the_command(voice):
    """Real engines, an empty model cache (voice_site's): the panel names the setup command."""
    if not shutil.which("uv"):
        pytest.skip("uv is not on PATH")
    page = voice.open(1280, fake=False)
    page.wait_for_selector(".voice-note code.voice-setup")
    assert page.inner_text(".voice-note code.voice-setup") == "al voice talk setup"
    assert "voice is not set up" in page.inner_text(".voice-note")
    assert page.locator(".voice-talk").is_disabled()
    assert not page.locator(".voice-text").is_disabled()   # typing still works
    assert not any(p.is_file() for p in voice.site["cache"].rglob("*"))   # nothing was downloaded


@pytest.mark.parametrize("width,key", [(1280, False), (390, False), (1280, True)])
def test_hold_to_talk_caption_and_card(voice, width, key):
    page = voice.open(width, transcripts=[REQUEST], mic=SPEECH)
    levels: list[float] = []

    def during() -> None:
        voice.wait_state("listening", timeout=10)
        for _ in range(12):
            levels.append(voice.level())
            time.sleep(0.07)
        if not key:
            shot(page, f"voice-listening-{width}.png")

    voice.hold(2.0, key=key, during=during)
    assert max(levels) > 0.05 and len({round(x, 2) for x in levels}) > 1, levels   # the mic level moves
    assert page.locator(".voice-talk").get_attribute("aria-pressed") in ("true", "false")
    voice.seen("thinking", timeout=10)
    if not key and voice.state() == "thinking":
        shot(page, f"voice-thinking-{width}.png")
    until(page, f"document.querySelector('.voice-caption-person').textContent === {json.dumps(REQUEST)}", timeout=20000)
    card = voice.card("proposed")
    fields = voice.card_fields()
    assert fields.get("Workflow") == "plan-build", fields
    assert fields.get("Request") == "add a search box", fields
    assert fields.get("Repository"), fields
    assert "What will happen" in fields and fields["What will happen"], fields
    assert card.locator(".voice-approve").is_disabled()   # not before "yes"
    voice.seen("speaking", timeout=20)
    if not key:
        if voice.state() == "speaking":
            shot(page, f"voice-speaking-{width}.png")
    order = voice.states()
    assert order.index("listening") < order.index("thinking") < order.index("speaking"), order
    assert "Shall I prepare it for approval?" in page.inner_text(".voice-caption-helper")
    assert run_dirs(voice.layer) == []


@pytest.mark.parametrize("width", [1280, 390])
def test_yes_then_approve_starts_mock_run(voice, width):
    page = voice.open(width, transcripts=[REQUEST], mic=SPEECH, video=f"voice-exchange-{width}.webm")
    voice.hold(1.6)
    voice.card("proposed")
    shot(page, f"voice-card-{width}.png")
    voice.type_("yes")
    card = voice.card("confirmed")
    until(page, "!document.querySelector('.voice-card .voice-approve').disabled")
    assert "Confirmed. Click Approve" in page.inner_text(".voice-caption-helper")
    assert card.locator(".voice-countdown").count() == 1
    time.sleep(1.0)
    assert run_dirs(voice.layer) == []                    # yes alone starts nothing
    page.click(".voice-card .voice-approve")
    page.wait_for_selector(".voice-card[data-state='approved'] a.voice-run-link", timeout=30000)
    link = page.get_attribute(".voice-card a.voice-run-link", "href")
    run_id = link.rsplit("/", 1)[1]
    assert link == f"#/run/talk/{run_id}" and RUN_DIR.match(run_id)
    assert "Started" in page.inner_text(".voice-card[data-state='approved']")
    shot(page, f"voice-approved-{width}.png")
    state = wait_finished(voice.layer, run_id)
    assert state["status"] == "passed" and state["adw"] == "plan-build" and state["prompt"] == "add a search box"
    assert run_dirs(voice.layer) == [run_id]
    time.sleep(1.0)    # the recording shows the end of the exchange


def test_speech_alone_never_starts(voice):
    page = voice.open(1280, transcripts=[REQUEST, "yes", "approve", "start it"], mic=SPEECH)
    for heard in (REQUEST, "yes", "approve", "start it"):
        voice.hold(1.0)
        until(page, f"document.querySelector('.voice-caption-person').textContent === {json.dumps(heard)}", timeout=20000)
        until(page, "document.querySelector('.voice-panel').dataset.state !== 'thinking'", timeout=20000)
        if heard == REQUEST:
            voice.card("proposed")
        if heard == "yes":
            voice.card("confirmed")
    until(page, "document.querySelectorAll('.voice-entry.voice-person').length >= 4", timeout=20000)
    time.sleep(5)
    assert run_dirs(voice.layer) == []
    said = page.evaluate("[...document.querySelectorAll('.voice-entry.voice-person .voice-said')].map(e => e.textContent)")
    assert said[-4:] == [REQUEST, "yes", "approve", "start it"]


def test_always_listen_cuts_an_utterance(voice):
    page = voice.open(1280, transcripts=["what is running right now"], mic=TURNS)
    page.check(".voice-mode-always")
    assert page.evaluate("localStorage.getItem('al.voice.mode')") == "always"
    page.wait_for_selector(".voice-mic-open:not([hidden])", timeout=15000)
    until(page, "document.querySelector('.voice-caption-person').textContent === 'what is running right now'", timeout=30000)
    page.wait_for_selector(".voice-entry.voice-helper", timeout=20000)
    assert page.evaluate("[...document.querySelectorAll('.voice-entry.voice-person .voice-said')].map(e => e.textContent)") \
        == ["what is running right now"]
    page.click(".voice-close-mic")
    page.wait_for_selector(".voice-mic-open", state="hidden")
    assert page.evaluate("document.querySelector('.voice-panel').dataset.mic") == "closed"
    assert page.evaluate("window.__streams.length >= 1 && window.__streams.every(s => s.getTracks().every(t => t.readyState === 'ended'))")
    page.reload()
    page.wait_for_selector(".voice-panel[data-session='ready']", timeout=15000)
    assert page.is_checked(".voice-mode-always") and not page.is_checked(".voice-mode-hold")
    assert page.inner_text(".voice-talk") == "Start listening"
    assert page.locator(".voice-mic-open").is_hidden()   # a reload does not open the microphone by itself


@pytest.mark.parametrize("width", [1280, 390])
def test_speaking_during_playback_stops_it(voice, width):
    page = voice.open(width, mic=SPEECH)
    page.check(".voice-mode-always")
    page.wait_for_selector(".voice-mic-open:not([hidden])", timeout=15000)
    voice.type_("read me something")
    voice.seen("interrupted", timeout=30)
    shot(page, f"voice-interrupted-{width}.png")
    marks = page.evaluate("""(() => {
      const at = (name) => performance.getEntriesByName(name).map(e => e.startTime);
      return {barge: at('al-voice-barge-in'), interrupted: at('al-voice-interrupted')};
    })()""")
    assert marks["barge"] and marks["interrupted"], marks
    assert 0 <= marks["interrupted"][0] - marks["barge"][0] <= 300, marks
    assert page.evaluate("document.querySelector('.voice-panel').dataset.queued") == "0"
    dropped = page.inner_text(".voice-caption-helper .voice-dropped")
    assert "not spoken" in dropped
    # nothing more is played: no sentence is scheduled after the interruption and the output falls silent
    traced = page.evaluate("window.alVoice.trace.length")
    page.click(".voice-close-mic")
    time.sleep(1.5)
    assert page.evaluate("window.alVoice.trace.length") == traced
    assert voice.state() == "idle" and voice.level() < 0.05
    order = voice.states()
    assert order.index("speaking") < order.index("interrupted"), order


def test_synthesizer_follows_the_audio(voice):
    page = voice.open(1280)
    page.evaluate("""(() => {
      window.__samples = [];
      const canvas = document.querySelector('canvas.voice-synth');
      const g = canvas.getContext('2d');
      const tick = () => {
        const data = g.getImageData(0, 0, canvas.width, canvas.height).data;
        let sum = 0;
        for (let i = 3; i < data.length; i += 4) sum += data[i];
        window.__samples.push([window.alVoice.audioTime(), parseFloat(canvas.dataset.level || '0'), sum]);
        if (window.__samples.length < 4000) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    })()""")
    voice.type_("count slowly")
    voice.seen("speaking", timeout=20)
    until(page, "window.__voiceStates.filter(([s]) => s === 'speaking').length >= 1 && "
                "document.querySelector('.voice-panel').dataset.state === 'idle'", timeout=30000)
    trace = page.evaluate("window.alVoice.trace")
    samples = page.evaluate("window.__samples")
    loud: list[tuple[float, float]] = []
    quiet: list[tuple[float, float]] = []
    for item in trace:
        env = item["envelope"]
        if not env:
            continue
        step = item["duration"] / len(env)          # seconds of the playback clock per envelope value
        for i, value in enumerate(env):
            start = item["start"] + i * step
            # inside a long run of the same kind only (the analyser and the level's decay lag a little)
            before, after = env[max(0, i - 12):i], env[i + 1:i + 3]
            if value > 0.8 and all(v > 0.8 for v in before[-5:] + after):
                loud.append((start, start + step))
            elif value < 0.02 and len(before) >= 8 and all(v < 0.02 for v in before[-8:] + after):
                quiet.append((start, start + step))
    assert loud and quiet, (len(loud), len(quiet))

    def inside(spans, t):
        return any(a <= t < b for a, b in spans)

    loud_s = [(lv, px) for t, lv, px in samples if inside(loud, t)]
    quiet_s = [(lv, px) for t, lv, px in samples if inside(quiet, t)]
    assert loud_s and quiet_s, (len(loud_s), len(quiet_s))
    loud_level = sorted(lv for lv, _ in loud_s)[len(loud_s) // 2]
    quiet_level = sorted(lv for lv, _ in quiet_s)[len(quiet_s) // 2]
    assert loud_level > 0.3 and quiet_level < 0.05, (loud_level, quiet_level)
    loud_px = sorted(px for _, px in loud_s)[len(loud_s) // 2]
    quiet_px = sorted(px for _, px in quiet_s)[len(quiet_s) // 2]
    assert loud_px >= 3 * max(1, quiet_px), (loud_px, quiet_px)


def test_panel_says_which_engine_spoke(voice):
    page = voice.open(1280)
    voice.type_("hello there")
    voice.seen("speaking", timeout=20)
    until(page, "document.querySelector('.voice-panel').dataset.state === 'idle'", timeout=30000)
    assert page.locator(".voice-engine").inner_text() == "Spoken by the test engine"
    assert page.locator(".voice-panel[data-engine='fake']").count() == 1
    shot(page, "voice-engine-1280.png")


def test_reduced_motion_uses_a_level_bar(voice):
    page = voice.open(1280, reduced_motion="reduce")
    assert page.locator("canvas.voice-synth").count() == 0
    meter = page.locator(".voice-level[role='meter']")
    assert meter.count() == 1
    assert page.evaluate("document.querySelector('.voice-level-bar').style.width") in ("", "0%")
    page.evaluate("""(() => {
      window.__widths = [];
      const bar = document.querySelector('.voice-level-bar');
      const meter = document.querySelector('.voice-level');
      const tick = () => {
        window.__widths.push([parseFloat(bar.style.width || '0'), parseFloat(meter.dataset.level || '0'),
                              parseInt(meter.getAttribute('aria-valuenow') || '0', 10)]);
        if (window.__widths.length < 3000) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    })()""")
    voice.type_("count slowly")
    voice.seen("speaking", timeout=20)
    time.sleep(1.0)
    shot(page, "voice-reduced-motion-1280.png")
    until(page, "document.querySelector('.voice-panel').dataset.state === 'idle'", timeout=30000)
    widths = page.evaluate("window.__widths")
    assert max(w for w, _l, _v in widths) > 30
    assert all(abs(w - round(level * 100)) <= 1 and abs(v - w) <= 1 for w, level, v in widths)   # the width is the level
    assert page.evaluate("getComputedStyle(document.querySelector('.voice-mic-dot')).animationName") == "none"


@pytest.mark.parametrize("width", [1280, 390])
def test_read_only_card_has_no_approve(voice, width):
    page = voice.open(width, args=("--read-only",))
    voice.type_("please start a plan for a search box")
    card = voice.card("proposed")
    assert card.locator(".voice-approve").count() == 0
    assert "--read-only" in card.locator(".voice-why-not").inner_text()
    voice.type_("yes")
    voice.card("confirmed")
    assert page.locator(".voice-card .voice-approve").count() == 0
    assert "Confirmed" in page.inner_text(".voice-caption-helper")   # the conversation keeps working
    shot(page, f"voice-read-only-{width}.png")
    assert run_dirs(voice.layer) == []


def test_hostile_text_stays_text(voice):
    hostile = '<img src=x onerror="window.__pwned=1"> start a plan for a search box'
    page = voice.open(1280, transcripts=[hostile], mic=SPEECH)
    helper_rules(voice.layer, say_extra=' <img src=y onerror="window.__pwned=2">',
                 prompt='<script>window.__pwned=3</script><img src=z onerror="window.__pwned=4">')
    voice.hold(1.2)
    voice.card("proposed")
    until(page, f"document.querySelector('.voice-caption-person').textContent === {json.dumps(hostile)}", timeout=20000)
    voice.type_("say something hostile")
    until(page, "document.querySelector('.voice-caption-helper').textContent.includes('<img src=x')", timeout=20000)
    assert page.evaluate("document.querySelectorAll('.voice-panel img, .voice-panel script').length") == 0
    assert not page.locator('.voice-caption-helper b').count()
    assert page.evaluate("window.__pwned === undefined")
    assert "<script>window.__pwned=3</script>" in voice.card_fields()["Request"]
    entries = page.evaluate("[...document.querySelectorAll('.voice-entry .voice-said')].map(e => e.textContent)")
    assert hostile in entries
