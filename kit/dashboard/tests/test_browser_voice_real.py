"""The voice helper's acceptance flow with the real speech engines (faster-whisper, Kokoro, Silero) and
the mock agent as the helper, in Chromium with a fake microphone whose sound Kokoro made.

Runs only with AL_VOICE_REAL=1, uv, Chrome and the models in the cache (`al voice LAYER setup`, or
`uv run --script kit/runtime/voice/serve.py setup --stt-model base.en`); it never downloads. With
AL_DASHBOARD_SHOTS=DIR it saves voice-real-card-1280.png, voice-real-approved-1280.png and
voice-real-exchange-1280.webm there.

The flow: hold to talk over "Start a plan for adding a search box." → real transcription → a card;
then a typed question gets a long spoken answer (no word of it in the request, so the request is not
taken for an echo), and always listen is switched on while it plays: the request spoken over it
interrupts the real Kokoro reply and is read back, and the spoken "Yes." does it: the mock run
passes with no click. The synthesizer's level is high while the microphone carries speech and low in
the pauses."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import voice_fixtures
from test_browser_voice import (RUN_DIR, Voice, pw, run_dirs, shot, until, wait_finished)  # noqa: F401 — pw: fixture

pytestmark = [pytest.mark.browser, pytest.mark.voice, pytest.mark.slow]

SERVE = Path(__file__).resolve().parents[2] / "runtime" / "voice" / "serve.py"
STT_MODEL = "base.en"
SENTENCE = "Start a plan for adding a search box."
GAP = 4.0          # seconds of silence between the request and "Yes.": the helper's reply plays in it
# a long answer that shares no word with SENTENCE (is_echo would take the request for the helper's own
# voice otherwise): it is still playing when the microphone's loop comes round to the request
NEWS = {"say": "Two runs passed this morning, and their checks were all green from the first step to the last one. "
               "One more run waits until one of the agent slots frees up, which should happen within the next "
               "few minutes, once the other two runs have handed over their work. Nothing has failed today, so "
               "there is nothing that needs your attention right now, and you can ask me again whenever you like."}


def _serve_module():
    spec = importlib.util.spec_from_file_location("al_voice_serve_browser", SERVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


serve = _serve_module()
# the real caches, read before voice_site points AL_VOICE_CACHE / AL_KOKORO_CACHE at empty directories
REAL_CACHE = {name: os.environ.get(name) for name in ("AL_VOICE_CACHE", "AL_KOKORO_CACHE")}


def _skip_reason() -> str | None:
    if os.environ.get("AL_VOICE_REAL") != "1":
        return "the real speech engines run only with AL_VOICE_REAL=1"
    if not shutil.which("uv"):
        return "uv not found"
    missing = serve.missing_files(serve.verify(serve.cache_paths(serve.load_manifest(), STT_MODEL), checksum=False))
    if missing:
        return f"voice models not set up ({', '.join(missing)}): serve.py setup --stt-model {STT_MODEL}"
    return None


def kokoro_mic(layer: Path, path: Path) -> tuple[Path, dict[str, float]]:
    """The fake microphone's WAV, spoken by the real Kokoro through the layer's own voice service:
    0.6 s silence, SENTENCE, GAP s silence, "Yes.", 2.5 s silence (16 kHz). → (path, segment times)."""
    token = secrets.token_urlsafe(24)
    env = {**os.environ, "AL_VOICE_TOKEN": token}
    proc = subprocess.Popen(["uv", "run", "--quiet", "--script", str(layer / "adws" / "voice" / "serve.py"), "serve",
                             "--layer", str(layer), "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, start_new_session=True)
    try:
        line = proc.stdout.readline()
        assert line, proc.stderr.read()
        port = json.loads(line)["port"]

        def call(method: str, route: str, body: bytes | None = None) -> tuple[int, bytes]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
            conn.request(method, route, body, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            response = conn.getresponse()
            data = response.read()
            conn.close()
            return response.status, data

        deadline = time.monotonic() + 600
        while True:
            status, data = call("GET", "/health")
            health = json.loads(data)
            if health.get("ready") or health.get("error") or time.monotonic() > deadline:
                break
            time.sleep(1)
        assert health.get("ready"), health

        def speech(text: str) -> bytes:
            status, data = call("POST", "/speak", json.dumps({"id": secrets.token_hex(4), "text": text}).encode())
            assert status == 200, data
            pcm = b""
            for _head, wav in serve.decode_frames(data):
                if wav:
                    part, rate = serve.read_wav(wav)
                    pcm += serve.resample(part, rate, 16000)
            return pcm

        def quiet(seconds: float) -> bytes:
            return b"\x00\x00" * int(seconds * 16000)

        request, yes = speech(SENTENCE), speech("Yes.")
    finally:
        os.killpg(proc.pid, 15)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
    pcm = quiet(0.6) + request + quiet(GAP) + yes + quiet(2.5)
    path.write_bytes(serve.wav_bytes(pcm, 16000))
    req_s, yes_s = len(request) / 32000, len(yes) / 32000
    return path, {"request": req_s, "yes": yes_s, "loop": len(pcm) / 32000}


def test_spoken_request_becomes_a_card_and_approve_starts(pw, voice_site, tmp_path, monkeypatch):  # noqa: F811
    for name, value in REAL_CACHE.items():          # voice_site emptied the cache; the real one is wanted here
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)
    layer = voice_site["talk"]
    wav, times = kokoro_mic(layer, tmp_path / "kokoro-mic.wav")
    voice = Voice(pw, voice_site, tmp_path)
    try:
        page = voice.open(1280, fake=False, mic_file=wav, video="voice-real-exchange-1280.webm")
        # after open(), which writes the browser tests' own mock/helper.json: the long answer is this test's
        voice_fixtures.helper_mock(layer, [("tell me everything", NEWS), *voice_fixtures.DEFAULT_RULES])
        page.evaluate("""(() => {
          window.__levels = [];
          const tick = () => {
            const panel = document.querySelector('.voice-panel');
            const el = document.querySelector('.voice-synth, .voice-level');
            window.__levels.push([performance.now(), panel.dataset.state, parseFloat(el.dataset.level || '0')]);
            if (window.__levels.length < 20000) requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
        })()""")
        # 1. hold to talk for a whole loop of the microphone: the request is in it, whatever the phase
        voice.hold(times["loop"] + 0.5)
        until(page, "document.querySelector('.voice-caption-person').textContent.toLowerCase().includes('search box')",
              timeout=240000)
        voice.card("proposed", timeout=120)
        shot(page, "voice-real-card-1280.png")
        assert run_dirs(layer) == []
        # 2. a long spoken answer; once its audio is scheduled, always listen: the microphone's loop has speech
        # every few seconds and the answer lasts far longer, so whatever is said next interrupts the real
        # Kokoro playback (on a loaded host the first sentence can take seconds to synthesise, so this waits
        # for the audio itself rather than guessing the loop's phase). The request makes a new card (the
        # first one was declined), and the spoken "Yes." does it.
        voice.type_("no")
        voice.card("declined", timeout=60)
        spoken = len(voice.states())
        voice.type_("tell me everything")
        until(page, "(window.alVoice.trace || []).some((t) => t.text.startsWith('Two runs passed'))", timeout=120000)
        page.check(".voice-mode-always")
        page.wait_for_selector(".voice-mic-open:not([hidden])", timeout=30000)
        page.wait_for_selector(".voice-card[data-state='approved'] a.voice-run-link", timeout=240000)
        page.click(".voice-close-mic")                  # before the microphone's loop says the request again
        page.wait_for_selector(".voice-mic-open", state="hidden")
        assert page.locator(".voice-entry.voice-kind-decision", has_text="decision: yes").count() >= 1
        said = page.evaluate("[...document.querySelectorAll('.voice-entry.voice-person .voice-said')].map(e => e.textContent)")
        assert any(s.strip().lower().strip(".!") == "yes" for s in said), said
        # from the question on (typing it may first cut the previous reply): the long answer was cut by the request
        order = voice.states()[spoken:]
        assert "speaking" in order and "interrupted" in order[order.index("speaking"):], order
        # 3. the spoken yes sent the guarded approval: one run
        run_id = page.get_attribute(".voice-card[data-state='approved'] a.voice-run-link", "href").rsplit("/", 1)[1]
        assert RUN_DIR.match(run_id)
        shot(page, "voice-real-approved-1280.png")
        state = wait_finished(layer, run_id)
        assert state["status"] == "passed" and state["adw"] == "plan-build"
        # 4. the synthesizer followed the microphone: high while speech came in, low in the pauses
        levels = page.evaluate("window.__levels")
        heard = [lv for _t, st, lv in levels if st == "listening"]
        assert heard and max(heard) > 0.3 and min(heard) < 0.05, (max(heard or [0]), min(heard or [0]))
        time.sleep(1.0)
    finally:
        voice.close()
    problems = [e for e in voice.errors if "favicon" not in e]
    assert not problems, problems
