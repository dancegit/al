"""The five-minute demo video (tools/demovideo): the privacy rules find and blank what must never be on
a frame, the narration script has its six parts with short, gapless captions, the demo layer compiles
and every `al` command the video shows is one the tool accepts, and no video is kept in the tree. When
a recorded video is in the run's proof folder (.al-proof/demo, or AL_DEMO_VIDEO_DIR), it is verified:
its shape, its contact sheet, its timed script, OCR of a frame every five seconds and the caption band.
Two slow tests record a scene and check that the verifier refuses a bad video.

    uv run --with pytest --with pytest-xdist --with pyyaml --with jsonschema pytest tests/tools/test_demo_video.py -q -rA
    AL_SHOWCASE_RECORD=1 uv run --with pytest --with pyyaml --with jsonschema --with pillow pytest tests/tools/test_demo_video.py -q -m slow
"""
from __future__ import annotations

import functools
import http.server
import importlib.util
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path

import pytest

from conftest import AL, ROOT, SANDBOX_GIT, al

SHOWCASE = ROOT / "tools" / "showcase"
DEMO = ROOT / "tools" / "demovideo"
sys.path.insert(0, str(SHOWCASE))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(DEMO))

import rules  # noqa: E402
import scenes  # noqa: E402  (standard library only at import: Pillow and Playwright are loaded by the recorders)
import tts  # noqa: E402

assert Path(rules.__file__).parent == DEMO, rules.__file__
NARRATION = DEMO / "narration.md"
MEDIA_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wav", ".mp3", ".m4a", ".ogg", ".gif", ".png", ".jpg", ".jpeg")
VALUES = {"hosts": ["box-17.corp"], "repos": ["larder-internal"]}


# ------------------------------------------------------------------------------ the rules

def test_privacy_rules_find_each_kind():
    for text, kind, match in (("mail someone@corp.example.net today", "mail", "someone@corp.example.net"),
                              ("by ada@example.org", "mail", "ada@example.org"),
                              ("see /home/someone/x", "home", "/home/"),
                              ("in /Users/someone", "home", "/Users/"),
                              ("ssh BOX-17.corp now", "host", "BOX-17.corp"),
                              ("from 10.1.2.3 port 22", "ipv4", "10.1.2.3"),
                              ("http://127.0.0.1:8765/", "ipv4", "127.0.0.1"),
                              ("a token here", "token", "token"),
                              ("Tokens out", "token", "Token"),
                              ("cloned Larder-Internal beside it", "repo", "Larder-Internal")):
        hits = rules.find_private(text, VALUES)
        assert [(h.kind, h.match) for h in hits] == [(kind, match)], text
        assert match in hits[0].excerpt
    # as OCR gives it: neighbouring words joined, no space or boundary beside a match
    joined = rules.find_private("ada@example.org/home/ada/work10.1.2.3 Tokens 1234", VALUES)
    assert [(h.kind, h.match) for h in joined] == [("mail", "ada@example.org"), ("home", "/home/"), ("ipv4", "10.1.2.3"),
                                                   ("token", "Token")]
    assert set(rules.PATTERNS) | {"host", "repo"} == set(rules.KINDS_OF_HIT) and len(rules.KINDS_OF_HIT) == 6
    assert rules.find_private("nothing of this machine", {"hosts": [], "repos": []}) == []
    values = rules.machine_values(ROOT)
    assert set(values) == {"hosts", "repos"} and "al" not in values["repos"] and "localhost" not in values["hosts"]
    assert all(len(v) >= 3 for found in values.values() for v in found)


def test_privacy_rules_accept_what_the_demo_shows():
    for text in ("github.com/dancegit/al", "uv 0.13.8", "v0.2.0", "~/work/cart.al", "sped up ×3.5", "run 0123abcd",
                 "Python 3.12.3", "al dashboard cart.al --port 18765", "4 passed, 0 failed", "256.1.2.3", "1.2.3.4.5"):
        assert rules.find_private(text, VALUES) == [], text


def test_blank_replaces_only_the_match():
    text = "al dashboard on http://127.0.0.1:18765/\n  1 234 tokens out, by ada@example.org on box-17.corp\n"
    blanked, hits = rules.blank(text, VALUES)
    assert len(blanked) == len(text) and blanked.count("\n") == text.count("\n")
    assert blanked == ("al dashboard on http://█████████:18765/\n"
                       "  1 234 █████s out, by ███████████████ on ███████████\n")
    assert [h.kind for h in hits] == ["ipv4", "token", "mail", "host"]
    assert [(a, b) for a, b in zip(text, blanked) if a != b and b != rules.BLOCK] == []
    assert rules.find_private(blanked, VALUES) == []
    assert rules.blank("nothing private", VALUES) == ("nothing private", [])
    # the pattern is the marker alone; the recorder blanks the path behind it too, so no folder name is left
    assert rules.blank("see /home/ada/work now")[0] == "see ██████ada/work now"
    assert rules.blank("see /home/ada/work now", whole_paths=True)[0] == "see ██████████████ now"


def test_fit_speeds_up_or_holds():
    assert rules.fit(190, 95) == (2.0, 0.0)
    assert rules.fit(40, 45) == (1.0, 5.0)
    assert rules.fit(45, 45) == (1.0, 0.0)
    assert (rules.SIZE, rules.FPS, rules.MIN_SECONDS, rules.MAX_SECONDS) == ((1280, 720), 15, 270, 300)
    assert (rules.SHEET_EVERY, rules.OCR_EVERY, rules.STRIP_H) == (10, 5, 64)


# ------------------------------------------------------------------------------ the narration script

def _scene_ids_in_scenes_py() -> list[str]:
    text = (DEMO / "scenes.py").read_text(encoding="utf-8")
    block = text[text.index("\nSCENES = {"):]
    return re.findall(r'^\s+"([0-9a-z-]+)": scene_', block[:block.index("\n}")], re.M)


def test_narration_has_six_parts_in_order_and_the_right_length():
    script = rules.parse(NARRATION.read_text(encoding="utf-8"))
    assert [(p.number, p.title) for p in script.parts] == list(enumerate(
        ("What al is", "Writing a layer", "A real run", "Proof and guardrails", "Honest limits", "What shipped"), 1))
    ids = [s.id for s in script.scenes]
    known = _scene_ids_in_scenes_py()
    assert ids and len(set(ids)) == len(ids) and set(ids) <= set(known), sorted(set(ids) - set(known))
    assert ids == [i for i in known if i in ids], "the scenes are recorded in script order"
    assert all(s.kind in rules.KINDS and all(part.scenes for part in script.parts) for s in script.scenes)
    assert rules.MIN_SECONDS <= script.seconds <= rules.MAX_SECONDS, script.seconds
    captions = {s.id: [c.text for c in s.captions] for s in script.scenes}
    assert captions["03-run"][0] == "A demo app. A scripted stand-in plays the model."
    assert captions["04-review-no"][0] == "A test fixture: reviewers refuse three times."
    assert captions["04-own-plan"][0].startswith("This video's own run:")


def test_narration_captions_are_short_gapless_and_private():
    text = NARRATION.read_text(encoding="utf-8")
    script = rules.parse(text)
    assert rules.validate(script) == []
    for scene in script.scenes:
        assert scene.captions[0].offset == 0.0, scene.id
        for caption in scene.captions:
            assert len(caption.text) <= 64, caption.text
    first = script.scenes[0].captions
    line = f"- [+{first[1].offset:.1f}] {first[1].text}"
    assert text.count(line) == 1
    for changed, word in ((text.replace(line, f"- [+{first[1].offset:.1f}] " + "x" * 70), "70 characters"),
                          (text.replace(line, f"- [+1.0] {first[1].text}"), "under 2.5 s"),
                          (text.replace(line, f"- [+{first[1].offset:.1f}] see /home/x"), "private match (home)")):
        problems = rules.validate(rules.parse(changed))
        assert len(problems) == 1 and word in problems[0], problems
    # the timed copy `make.py assemble` writes parses to the same script, with its times
    timed = text.replace("### 01-what | card | 20 s", "### 01-what | card | 20 s | 0:00.0–0:20.0").replace(
        "- [+0.0] " + first[0].text, "- [+0.0 | 0:00.0–0:05.0] " + first[0].text, 1) + "\n## Scenes sped up\n\n- none\n"
    again = rules.parse(timed)
    assert rules.validate(again) == [] and (again.scenes[0].start, again.scenes[0].end) == (0.0, 20.0)
    assert (again.scenes[0].captions[0].start, again.scenes[0].captions[0].end) == (0.0, 5.0)
    assert rules.clock(125.04) == "2:05.0" and rules.unclock("2:05.0") == 125.0


# ------------------------------------------------------------------------------ the demo layer and the commands shown

def test_the_demo_layer_compiles_and_holds_what_the_excerpt_shows(tmp_path):
    site = scenes.demo_site(tmp_path)
    layer = site["layer"]
    assert (site["app"] / "cart.py").is_file() and (site["app"] / "tools" / "lint.py").is_file()
    assert not (layer / "agentic-layer.json").exists(), "al gen is run on screen, not by the setup"
    text = (layer / "agentic.rkt").read_text(encoding="utf-8")
    for needed in ("(adw change ", "(adw tidy ", "(cmd lint ", "(cmd test ", '#:protected ("tools")', "(review agent #:reviewers 2)",
                   "(agent mock)", "#:agent replay"):
        assert needed in text, needed
    assert "@REPLAY@" not in text
    lines = text.splitlines()
    (a, b), (c, d) = scenes.excerpts(text)
    first, second = lines[a - 1:b], lines[c - 1:d]
    for excerpt in (first, second):
        assert len(excerpt) <= 18 and all(len(line) <= 100 for line in excerpt), excerpt
        assert not any("replay_agent.py" in line for line in excerpt), "the stand-in's path on this machine is not shown"
    shown = "\n".join(first)
    assert "(cmd lint " in shown and "(cmd test " in shown and '#:protected ("tools")' in shown and "(review agent #:reviewers 2)" in shown
    assert second[0].lstrip().startswith("(adw change ") and second[-1].strip() == "(ship)))"
    assert [step for step in ("classify", "plan", "build", "lint", "test", "commit") if f" {step} " in "\n".join(second)] == [
        "classify", "plan", "build", "lint", "test", "commit"]
    assert json.loads((layer / "mock" / "build.json").read_text())["files"].keys() == {"tools/lint.py"}
    gen = al("gen", layer)
    assert "generated " in gen.stdout, gen.stdout
    assert al("check", layer).stdout.strip().endswith("check: ok")
    assert scenes.demo_site(tmp_path, generated=True)["layer"] == layer      # set up once, generated once


def test_every_al_command_the_demo_shows_is_accepted():
    shown = [scenes.fill(argv, scenes.SAMPLES) for argv in scenes.COMMANDS.values()]
    assert len(shown) >= 4 and all(argv[0] == "al" for argv in shown)
    assert not [a for argv in shown for a in argv if "{" in a], "every placeholder has a sample"
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    for argv in shown:
        cp = subprocess.run([str(AL), *argv[1:], "--help"], capture_output=True, text=True, env=env, timeout=120)
        assert cp.returncode == 0, (argv, cp.stdout, cp.stderr)
        for flag in (a for a in argv if a.startswith("--")):
            assert flag in cp.stdout, (argv, flag)


def test_recorder_keeps_videos_out_of_the_tree():
    present = [p for p in DEMO.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
    assert present == [], "tools/demovideo holds no video, sound or picture"
    cp = subprocess.run(["git", "-C", str(ROOT), "ls-files", "tools/demovideo", ".al-proof", "media"], capture_output=True, text=True,
                        env={**os.environ, **SANDBOX_GIT})
    if cp.returncode == 0:
        assert [p for p in cp.stdout.splitlines() if p.lower().endswith(MEDIA_SUFFIXES) or p.startswith((".al-proof/demo", "media/"))] == []
    make = (DEMO / "make.py").read_text(encoding="utf-8")
    assert '".al-proof/demo"' in make and '"media/demo-video"' in make
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/media/" in ignore, "the fallback folder is ignored by git"
    for script in ("make.py", "verify.py", "scenes.py", "rules.py"):
        text = (DEMO / script).read_text(encoding="utf-8")
        assert not re.search(r"https?://(?!127\.0\.0\.1)", text), f"{script} names no address to send anything to"
    tts_text = (DEMO / "tts.py").read_text(encoding="utf-8")
    assert set(re.findall(r"https?://[^\s\"'/]+", tts_text)) == {"https://api.elevenlabs.io"}, "tts.py names only ElevenLabs"



# ------------------------------------------------------------------------------ the voice (tts.py, against a fake server)

KEY = "sk-test-0123456789abcdef-not-real"


def _make():
    """tools/demovideo/make.py under a name of its own (tools/showcase/make.py is called make.py too)."""
    name = "demovideo_make"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, DEMO / "make.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _tone_wav(lead: float, tone: float, tail: float) -> bytes:
    """44.1 kHz 16-bit mono: `lead` s of silence, `tone` s of 440 Hz at half scale, `tail` s of silence."""
    rate = 44100
    samples = [0] * int(lead * rate)
    samples += [int(16383 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(int(tone * rate))]
    samples += [0] * int(tail * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


def _files_holding(root: Path, needle: str) -> list[Path]:
    return [p for p in Path(root).rglob("*") if p.is_file() and needle.encode() in p.read_bytes()]


def _wav_info(path: Path) -> tuple[float, int, int]:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate()), wav.getframerate(), wav.getnchannels()


class _FakeEleven:
    def __init__(self):
        self.requests: list[dict] = []
        self.answer = (200, "audio/mpeg", _tone_wav(0.2, 1.0, 0.2))
        self.root = ""


@pytest.fixture
def fake_eleven():
    fake = _FakeEleven()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            fake.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers.items()), "body": body})
            status, content_type, answer = fake.answer
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.root = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def _settings(voice: str = tts.DEFAULT_VOICE_ID, key: str = KEY) -> tts.Settings:
    return tts.Settings(voice_id=voice, model=tts.MODEL, output_format=tts.OUTPUT_FORMAT, key=key)


def test_tts_backend_follows_the_environment(monkeypatch):
    monkeypatch.setenv(tts.KEY_VAR, KEY)
    assert tts.choose("auto", os.environ, espeak=True) == "elevenlabs"
    assert tts.choose("auto", {tts.KEY_VAR: KEY}, espeak=False) == "elevenlabs"
    for blank in ("", "  "):
        monkeypatch.setenv(tts.KEY_VAR, blank)
        assert tts.choose("auto", os.environ, espeak=True) == "local"
        assert tts.choose("auto", os.environ, espeak=False) == "none"
    monkeypatch.delenv(tts.KEY_VAR)
    assert tts.choose("auto", os.environ, espeak=False) == "none"
    with pytest.raises(tts.TtsError, match="ELEVENLABS_API_KEY"):
        tts.choose("elevenlabs", os.environ, espeak=True)
    with pytest.raises(tts.TtsError, match="espeak-ng"):
        tts.choose("local", os.environ, espeak=False)
    assert tts.choose("local", os.environ, espeak=True) == "local"
    assert tts.choose("none", {tts.KEY_VAR: KEY}, espeak=True) == "none"
    monkeypatch.setenv(tts.KEY_VAR, f" {KEY} ")
    monkeypatch.delenv(tts.VOICE_VAR, raising=False)
    settings = tts.Settings.from_env(os.environ)
    assert (settings.voice_id, settings.model, settings.output_format, settings.key) == (
        "JBFqnCBsd6RMkjVDRZzb", "eleven_multilingual_v2", "mp3_44100_128", KEY)
    assert KEY not in repr(settings)
    monkeypatch.setenv(tts.VOICE_VAR, " someVoice42 ")
    assert tts.Settings.from_env(os.environ).voice_id == "someVoice42"


def test_tts_sends_the_key_only_in_the_header(fake_eleven):
    served = fake_eleven.answer[2]
    got = tts.request_speech("Hello there.", _settings("voiceA"), api_root=fake_eleven.root)
    assert got == served
    assert len(fake_eleven.requests) == 1
    request = fake_eleven.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/text-to-speech/voiceA?output_format=mp3_44100_128"
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["xi-api-key"] == KEY
    assert json.loads(request["body"]) == {"text": "Hello there.", "model_id": "eleven_multilingual_v2"}
    assert KEY not in request["path"] and KEY.encode() not in request["body"]


def test_tts_caches_by_text_voice_and_model(fake_eleven, tmp_path):
    cache = tmp_path / "voice-cache"
    first, cached = tts.fetch_line("One line.", cache, _settings(), api_root=fake_eleven.root)
    assert not cached and first.read_bytes() == fake_eleven.answer[2]
    again, cached = tts.fetch_line("One line.", cache, _settings(), api_root=fake_eleven.root)
    assert cached and again == first and len(fake_eleven.requests) == 1
    tts.fetch_line("One line.", cache, _settings("otherVoice"), api_root=fake_eleven.root)
    assert len(fake_eleven.requests) == 2
    tts.fetch_line("Another line.", cache, _settings(), api_root=fake_eleven.root)
    assert len(fake_eleven.requests) == 3
    _path, cached = tts.fetch_line("One line.", cache, _settings(key="sk-test-another-key-not-real"), api_root=fake_eleven.root)
    assert cached and len(fake_eleven.requests) == 3, "the key is not part of the cache name"
    names = sorted(p.name for p in cache.iterdir())
    assert len(names) == 3 and all(re.fullmatch(r"[0-9a-f]{64}\.mp3", name) for name in names), names
    assert _files_holding(tmp_path, KEY) == []


def _fake_clips(work: Path) -> None:
    parsed = rules.parse(NARRATION.read_text(encoding="utf-8"))
    clips = work / "clips"
    clips.mkdir(parents=True)
    for scene in parsed.scenes:
        offsets = [c.offset for c in scene.captions] + [scene.seconds]
        captions = [{"offset": c.offset, "start": c.offset, "end": offsets[i + 1], "text": c.text, "moved": 0.0}
                    for i, c in enumerate(scene.captions)]
        (clips / f"{scene.id}.mp4").write_bytes(b"")
        (clips / f"{scene.id}.json").write_text(json.dumps({"scene": scene.id, "seconds": scene.seconds, "speed": 1.0,
                                                             "real_seconds": scene.seconds, "hold": 0.0, "captions": captions,
                                                             "blanked": [], "shortened": [], "stand_ins": []}))


def test_tts_failure_does_not_leak_the_key(fake_eleven, tmp_path, monkeypatch, capsys):
    fake_eleven.answer = (401, "application/json", json.dumps({"detail": f"invalid api key {KEY}"}).encode())
    cache = tmp_path / "voice-cache"
    with pytest.raises(tts.TtsError) as caught:
        tts.fetch_line("One line.", cache, _settings(), api_root=fake_eleven.root)
    assert "401" in str(caught.value) and KEY not in str(caught.value)
    assert not cache.exists() or list(cache.iterdir()) == []
    # the whole command: assemble stops before any video is made
    work, out = tmp_path / "work", tmp_path / "out"
    _fake_clips(work)
    monkeypatch.setattr(tts, "API_ROOT", fake_eleven.root)
    monkeypatch.setenv(tts.KEY_VAR, KEY)
    capsys.readouterr()
    code = _make().main(["assemble", "--work", str(work), "--out", str(out), "--voice", "elevenlabs"])
    printed = capsys.readouterr()
    assert code == 1, printed
    assert "401" in printed.out + printed.err and KEY not in printed.out + printed.err
    assert _files_holding(tmp_path, KEY) == []
    assert not (out / "al-demo.mp4").exists() and not (out / "narration.md").exists()


def _needs_ffmpeg():
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg on PATH")


def test_tts_fits_a_voiced_line_to_its_caption(tmp_path, monkeypatch):
    _needs_ffmpeg()
    monkeypatch.setenv(tts.KEY_VAR, KEY)
    argvs: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(argv, *args, **kwargs):
        argvs.append([str(a) for a in argv])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    audio = tmp_path / "line.mp3"
    audio.write_bytes(_tone_wav(0.5, 2.0, 0.5))
    roomy = tmp_path / "roomy.wav"
    assert tts.fit_line(audio, roomy, slot=3.0) == 1.0
    seconds, rate, channels = _wav_info(roomy)
    assert abs(seconds - 2.0) <= 0.15 and (rate, channels) == (44100, 1), seconds
    tight = tmp_path / "tight.wav"
    tempo = tts.fit_line(audio, tight, slot=1.8)
    assert 1.0 < tempo <= 1.35
    assert _wav_info(tight)[0] <= 1.8
    with pytest.raises(tts.TtsError, match="×1.35"):
        tts.fit_line(audio, tmp_path / "short.wav", slot=1.0)
    assert argvs and all(argv[0] == "ffmpeg" for argv in argvs)
    assert not [argv for argv in argvs if any(KEY in a for a in argv)]


def test_assemble_voices_through_elevenlabs_with_the_fake_server(fake_eleven, tmp_path, monkeypatch, capsys):
    _needs_ffmpeg()
    monkeypatch.setattr(tts, "API_ROOT", fake_eleven.root)
    monkeypatch.setenv(tts.KEY_VAR, KEY)
    monkeypatch.delenv(tts.VOICE_VAR, raising=False)
    make = _make()
    build, cache = tmp_path / "build", tmp_path / "voice-cache"
    build.mkdir()
    lines = [("Hello there.", 0.0, 3.0), ("Second line.", 3.0, 3.0)]
    delays, voice = make.voice_lines(lines, build, cache, "elevenlabs")
    assert [start for _wav, start in delays] == [0.0, 3.0]
    assert all(_wav_info(wav)[0] <= 3.0 for wav, _start in delays)
    assert "ElevenLabs" in voice and "JBFqnCBsd6RMkjVDRZzb" in voice and "eleven_multilingual_v2" in voice and KEY not in voice
    assert len(fake_eleven.requests) == 2
    make.voice_lines(lines, build, cache, "elevenlabs")
    assert len(fake_eleven.requests) == 2, "the second assemble takes every line from the cache"
    printed = capsys.readouterr()
    assert KEY not in printed.out + printed.err
    assert _files_holding(tmp_path, KEY) == []


def test_assemble_offers_the_voice_option(monkeypatch, capsys):
    make = _make()
    with pytest.raises(SystemExit) as done:
        make.main(["assemble", "--help"])
    assert done.value.code == 0
    shown = capsys.readouterr().out
    assert "--voice" in shown and "{auto,elevenlabs,local,none}" in shown
    with pytest.raises(SystemExit) as done:
        make.main(["assemble", "--work", "/nonexistent", "--voice", "local", "--no-voice"])
    assert done.value.code == 2
    capsys.readouterr()
    monkeypatch.delenv(tts.KEY_VAR, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/espeak-ng" if name == "espeak-ng" else None)
    assert tts.choose("auto", os.environ, bool(shutil.which("espeak-ng"))) == "local"


def test_showcase_readme_documents_the_voice():
    text = (ROOT / "docs" / "showcase" / "README.md").read_text(encoding="utf-8")
    for needed in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb", "--voice", "voice-cache"):
        assert needed in text, needed
    assert not re.search(r"sk_[A-Za-z0-9]{20,}", text), "no real-looking key in the docs"


# ------------------------------------------------------------------------------ the recorded video

@functools.lru_cache(maxsize=None)
def _verify(folder: str) -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    cp = subprocess.run(["uv", "run", str(DEMO / "verify.py"), folder, "--json"], capture_output=True, text=True, env=env, timeout=1500)
    assert cp.stdout.strip().startswith("{"), cp.stdout + cp.stderr
    return cp.returncode, json.loads(cp.stdout)


def _verified() -> tuple[int, dict, Path]:
    folder = Path(os.environ.get("AL_DEMO_VIDEO_DIR") or ROOT / ".al-proof" / "demo")
    if not (folder / "al-demo.mp4").is_file():
        pytest.skip("no recorded demo in .al-proof/demo: record it with tools/demovideo/make.py")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            pytest.skip(f"no {tool} on PATH: the recorded demo is not verified")
    code, report = _verify(str(folder))
    return code, report, folder


def test_recorded_demo_has_the_right_shape():
    _code, report, _folder = _verified()
    shape = report["shape"]
    assert shape["ok"], shape["problems"]
    assert shape["codec"] == "h264" and (shape["width"], shape["height"]) in (("1280", "720"), ("1920", "1080"))
    assert shape["pix_fmt"] == "yuv420p" and 270 <= shape["duration"] <= 300 and shape["decodes"]


def test_recorded_demo_has_a_contact_sheet():
    _code, report, folder = _verified()
    sheet = folder / "al-demo-contact-sheet.png"
    assert report["sheet"]["ok"] and sheet.is_file()
    assert sheet.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    expected = int(report["shape"]["duration"] // 10) + 1
    assert abs(report["sheet"]["frames"] - expected) <= 1, (report["sheet"]["frames"], expected)


def test_recorded_demo_narration_has_times_for_every_scene():
    _code, report, folder = _verified()
    assert report["narration"]["ok"], report["narration"]["problems"]
    script = rules.parse((folder / "narration.md").read_text(encoding="utf-8"))
    assert [p.title for p in script.parts] == list(rules.TITLES) and rules.validate(script) == []
    at = 0.0
    for scene in script.scenes:
        assert scene.start is not None and scene.end is not None and abs(scene.start - at) < 0.11, scene.id
        assert all(c.start is not None and c.end is not None and scene.start <= c.start < c.end <= scene.end + 0.11
                   for c in scene.captions), scene.id
        at = scene.end
    assert abs(at - report["shape"]["duration"]) <= 1.0
    text = (folder / "narration.md").read_text(encoding="utf-8")
    for section in ("## Scenes sped up", "## Blanked on screen", "## Stand-ins and fixtures", "mute: ffmpeg -i al-demo.mp4"):
        assert section in text, section


def test_recorded_demo_privacy_ocr_finds_nothing():
    _code, report, folder = _verified()
    ocr = report["ocr"]
    assert ocr["frames"] >= report["shape"]["duration"] / 5, ocr["frames"]
    assert ocr["hits"] == [] and ocr["ok"]
    assert set(report["rules"]["kinds"]) == set(rules.KINDS_OF_HIT)
    assert (folder / "privacy-ocr.json").is_file()
    assert len(ocr["captions"]) == ocr["frames"] and any(sample["read"] for sample in ocr["captions"])


def test_recorded_demo_has_a_caption_band_on_every_sampled_frame():
    code, report, _folder = _verified()
    assert report["bands"]["failures"] == [] and report["bands"]["ok"] and report["bands"]["frames"] == report["ocr"]["frames"]
    assert report["ok"] and code == 0


# ------------------------------------------------------------------------------ recording (slow)

def _recording() -> dict[str, str]:
    if os.environ.get("AL_SHOWCASE_RECORD") != "1":
        pytest.skip("records with ffmpeg: set AL_SHOWCASE_RECORD=1")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("no ffmpeg")
    return {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}


@pytest.mark.slow
def test_recording_one_scene(tmp_path):
    env = _recording()
    pytest.importorskip("PIL")
    work = tmp_path / "work"
    cp = subprocess.run(["uv", "run", str(DEMO / "make.py"), "scene", "02-layer", "--work", str(work)],
                        capture_output=True, text=True, env=env, timeout=1800)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    clip = work / "clips" / "02-layer.mp4"
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,width,height:format=duration", "-of", "default=noprint_wrappers=1", str(clip)],
                           capture_output=True, text=True, check=True)
    info = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
    assert (info["codec_name"], info["width"], info["height"]) == ("h264", "1280", "720")
    assert abs(float(info["duration"]) - 45) <= 0.11
    record = json.loads((work / "clips" / "02-layer.json").read_text())
    assert record["seconds"] == 45 and record["speed"] == 1.0 and len(record["captions"]) == 5
    assert not [p for p in DEMO.rglob("*") if p.suffix.lower() in MEDIA_SUFFIXES]
    assert subprocess.run(["uv", "run", str(DEMO / "make.py"), "clean", "--work", str(work)], capture_output=True, env=env).returncode == 0
    assert not work.exists()


@pytest.mark.slow
def test_verify_refuses_a_private_frame_and_a_wrong_length(tmp_path):
    env = _recording()
    font = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans Mono"], capture_output=True, text=True).stdout.strip()
    if not font or not Path(font).is_file():
        pytest.skip("no font for the test video")
    (tmp_path / "a.txt").write_text("mail ada@example.org")
    (tmp_path / "b.txt").write_text("see /home/ada/work")
    draw = (f"drawtext=fontfile={font}:textfile={tmp_path / 'a.txt'}:fontcolor=white:fontsize=40:x=80:y=200,"
            f"drawtext=fontfile={font}:textfile={tmp_path / 'b.txt'}:fontcolor=white:fontsize=40:x=80:y=320")
    made = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x0d1117:s=1280x720:r=15:d=12", "-vf", draw,
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "al-demo.mp4")], capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    cp = subprocess.run(["uv", "run", str(DEMO / "verify.py"), str(tmp_path), "--json"], capture_output=True, text=True, env=env, timeout=1500)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    report = json.loads(cp.stdout)
    assert not report["ok"] and not report["shape"]["ok"] and any("duration" in p for p in report["shape"]["problems"])
    assert {"mail", "home"} <= {hit["kind"] for hit in report["ocr"]["hits"]}
    assert len(report["bands"]["failures"]) == report["bands"]["frames"] >= 2
    assert not report["narration"]["ok"]
