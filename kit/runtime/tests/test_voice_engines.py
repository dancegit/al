"""The speech engines of the voice helper with stubbed engines: the AL_VOICE_TTS setting, the choice of Kokoro,
espeak-ng or off with its reason, the espeak-ng call, the cache paths (XDG and the overrides), the checksummed
first-use fetch under a lock, say.py's flows and that run summaries never fetch. No network, no models."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parents[1]
VOICE = RUNTIME / "voice"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from adw_modules import runner  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load("al_voice_serve_engines", VOICE / "serve.py")
say = _load("al_voice_say_engines", VOICE / "say.py")

ESPEAK = "/usr/bin/espeak-ng"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("AL_VOICE_TTS", "AL_VOICE_CACHE", "AL_KOKORO_CACHE", "AL_VOICE_MANIFEST", "AL_VOICE_NO_FETCH",
                 "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)


def tone(seconds: float, rate: int, amplitude: float = 0.5) -> list[float]:
    return [amplitude * math.sin(2 * math.pi * 220 * i / rate) for i in range(int(seconds * rate))]


def never(*_a, **_k):
    raise AssertionError("must not be called")


# ---------------------------------------------------------------- setting and choice

def test_setting_defaults_to_kokoro_and_rejects_unknown(monkeypatch):
    assert serve.tts_setting() == "kokoro"
    monkeypatch.setenv("AL_VOICE_TTS", "")
    assert serve.tts_setting() == "kokoro"
    monkeypatch.setenv("AL_VOICE_TTS", " ESPEAK ")
    assert serve.tts_setting() == "espeak"
    monkeypatch.setenv("AL_VOICE_TTS", "off")
    assert serve.tts_setting() == "off"
    monkeypatch.setenv("AL_VOICE_TTS", "bogus")
    with pytest.raises(ValueError) as caught:
        serve.tts_setting()
    assert all(word in str(caught.value) for word in ("kokoro", "espeak", "off", "bogus", "AL_VOICE_TTS"))


def test_choose_kokoro_when_its_files_are_there():
    assert serve.choose_tts("kokoro", [], ESPEAK, "L") == ("kokoro", "")


def test_choose_falls_back_to_espeak_when_kokoro_is_missing():
    engine, why = serve.choose_tts("kokoro", ["kokoro voices-v1.0.bin (missing)"], ESPEAK, "L")
    assert engine == "espeak"
    assert "voices-v1.0.bin" in why and "al voice L setup" in why


def test_choose_off_when_nothing_can_speak():
    engine, why = serve.choose_tts("kokoro", ["kokoro kokoro-v1.0.int8.onnx (missing)"], None, "L")
    assert engine == "off"
    assert "kokoro-v1.0.int8.onnx" in why and "espeak-ng is not on PATH" in why
    assert serve.choose_tts("espeak", [], None, "L") == ("off", "espeak-ng is not on PATH")


def test_explicit_espeak_and_off_win_over_a_ready_kokoro():
    assert serve.choose_tts("espeak", [], ESPEAK, "L") == ("espeak", "")
    engine, why = serve.choose_tts("off", [], ESPEAK, "L")
    assert engine == "off" and "AL_VOICE_TTS=off" in why
    stub = object()
    assert serve.pick_synthesizer("espeak", "", never, lambda: stub) == (stub, "espeak", "")
    synthesizer, engine, why = serve.pick_synthesizer("off", "speech is off (AL_VOICE_TTS=off)", never, lambda: stub)
    assert synthesizer is None and engine == "off" and "off" in why


def test_kokoro_that_fails_to_load_falls_back():
    def broken():
        raise ImportError("No module named 'kokoro_onnx'")
    stub = object()
    synthesizer, engine, why = serve.pick_synthesizer("kokoro", "", broken, lambda: stub)
    assert synthesizer is stub and engine == "espeak" and "kokoro_onnx" in why and "ImportError" in why
    synthesizer, engine, why = serve.pick_synthesizer("kokoro", "", broken, None)
    assert synthesizer is None and engine == "off" and "kokoro_onnx" in why
    ready = object()
    assert serve.pick_synthesizer("kokoro", "", lambda: ready, never) == (ready, "kokoro", "")


# ---------------------------------------------------------------- espeak-ng

def test_espeak_synthesizer_passes_text_on_stdin(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        out = Path(argv[argv.index("-w") + 1])
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(serve.floats_to_pcm(tone(0.5, 22050)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(serve.subprocess, "run", fake_run)
    synth = serve.EspeakSynthesizer(ESPEAK)
    for text in ("The run passed.", "--help me -w /etc/passwd"):
        samples, rate = synth.synth(text, "af_heart", 1.5, "en-us")
        argv, kw = calls[-1]
        assert argv[0] == ESPEAK and "--stdin" in argv
        assert argv[argv.index("-v") + 1] == "en-us" and argv[argv.index("-s") + 1] == "263"
        assert text not in argv and kw["input"] == text
        assert kw.get("timeout") and kw.get("check") is True
        assert rate == 22050 and len(samples) == 11025 and max(samples) > 0.1
    assert serve.EspeakSynthesizer.model == "espeak-ng"


# ---------------------------------------------------------------- cache paths

def test_caches_follow_xdg_and_their_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    home = Path.home()
    assert serve.kokoro_cache() == home / ".cache" / "al" / "kokoro"
    assert serve.voice_cache() == home / ".cache" / "al" / "voice"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "x"))
    assert serve.kokoro_cache() == tmp_path / "x" / "al" / "kokoro"
    assert serve.voice_cache() == tmp_path / "x" / "al" / "voice"
    monkeypatch.setenv("AL_KOKORO_CACHE", str(tmp_path / "k"))
    monkeypatch.setenv("AL_VOICE_CACHE", str(tmp_path / "v"))
    assert serve.kokoro_cache() == tmp_path / "k" and serve.voice_cache() == tmp_path / "v"
    monkeypatch.delenv("AL_KOKORO_CACHE")
    monkeypatch.delenv("AL_VOICE_CACHE")
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    assert serve.kokoro_cache() == home / ".cache" / "al" / "kokoro"
    assert serve.voice_cache() == home / ".cache" / "al" / "voice"


# ---------------------------------------------------------------- fetch

FILES = {"tiny.int8.onnx": b"model bytes " * 10, "voices-tiny.bin": b"voices " * 7, "tiny.onnx": b"the big one"}


def tiny_manifest(tmp_path: Path) -> tuple[dict, Path]:
    kokoro = {"release": "https://example.invalid/kokoro", "model": "tiny.int8.onnx", "files": {}}
    for name, data in FILES.items():
        kokoro["files"][name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                 **({"optional": True} if name == "tiny.onnx" else {})}
    manifest = {"version": 1, "whisper": {}, "kokoro": kokoro}
    path = tmp_path / "models.json"
    path.write_text(json.dumps(manifest))
    return manifest, path


class Downloads:
    """A stub of say.download: writes the manifest's bytes (or `wrong`), counts each URL, may sleep."""

    def __init__(self, wrong: bool = False, sleep: float = 0.0) -> None:
        self.wrong, self.sleep = wrong, sleep
        self.urls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, url: str, dest: Path) -> None:
        with self.lock:
            self.urls.append(url)
        time.sleep(self.sleep)
        name = url.rsplit("/", 1)[1]
        dest.write_bytes(b"not the model" if self.wrong else FILES[name])


def test_fetch_kokoro_checks_sums_and_keeps_one_copy(monkeypatch, tmp_path):
    cache = tmp_path / "kokoro"
    monkeypatch.setenv("AL_KOKORO_CACHE", str(cache))
    manifest, _path = tiny_manifest(tmp_path)
    download = Downloads()
    serve.fetch_kokoro(manifest, download, out=sys.stderr)
    assert sorted(u.rsplit("/", 1)[1] for u in download.urls) == ["tiny.int8.onnx", "voices-tiny.bin"]
    serve.fetch_kokoro(manifest, download, out=sys.stderr)
    assert len(download.urls) == 2                                  # the second fetch downloads nothing
    assert sorted(p.name for p in cache.iterdir()) == [".lock", "tiny.int8.onnx", "voices-tiny.bin"]
    assert (cache / "tiny.int8.onnx").read_bytes() == FILES["tiny.int8.onnx"]


def test_fetch_kokoro_refuses_a_bad_checksum(monkeypatch, tmp_path):
    cache = tmp_path / "kokoro"
    monkeypatch.setenv("AL_KOKORO_CACHE", str(cache))
    manifest, _path = tiny_manifest(tmp_path)
    with pytest.raises(serve.FetchError) as caught:
        serve.fetch_kokoro(manifest, Downloads(wrong=True), out=sys.stderr)
    message = str(caught.value)
    name = next(n for n in ("tiny.int8.onnx", "voices-tiny.bin") if n in message)
    assert manifest["kokoro"]["files"][name]["sha256"] in message
    assert hashlib.sha256(b"not the model").hexdigest() in message
    assert str(cache) in message and "AL_KOKORO_CACHE" in message
    assert not (cache / name).exists()


def test_concurrent_fetches_download_once(monkeypatch, tmp_path):
    monkeypatch.setenv("AL_KOKORO_CACHE", str(tmp_path / "kokoro"))
    manifest, _path = tiny_manifest(tmp_path)
    download = Downloads(sleep=0.2)
    errors: list[BaseException] = []

    def fetch() -> None:
        try:
            serve.fetch_kokoro(manifest, download, out=sys.stderr)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=fetch) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert sorted(download.urls) == sorted(set(download.urls)) and len(download.urls) == 2


# ---------------------------------------------------------------- say.py

class StubSynth:
    def __init__(self, *args, **kw) -> None:
        self.args = args

    def synth(self, text, voice="", speed=1.0, lang=""):
        return tone(0.3, 22050), 22050


def test_say_falls_back_to_espeak_without_fetching_when_told_not_to(monkeypatch, tmp_path, capsys):
    cache = tmp_path / "kokoro"
    cache.mkdir()
    monkeypatch.setenv("AL_KOKORO_CACHE", str(cache))
    monkeypatch.setattr(say, "download", never)
    monkeypatch.setattr(say.serve, "espeak_program", lambda: ESPEAK)
    monkeypatch.setattr(say.serve, "EspeakSynthesizer", StubSynth)
    out = tmp_path / "o.wav"
    assert say.main(["hi there", "--no-fetch", "--no-play", "--out", str(out)]) == 0
    assert out.read_bytes()[:4] == b"RIFF"
    assert "spoken by espeak" in capsys.readouterr().err
    assert [p.name for p in cache.iterdir() if p.name != ".lock"] == []


def test_say_fetches_kokoro_on_first_use(monkeypatch, tmp_path):
    cache = tmp_path / "kokoro"
    monkeypatch.setenv("AL_KOKORO_CACHE", str(cache))
    _manifest, path = tiny_manifest(tmp_path)
    monkeypatch.setenv("AL_VOICE_MANIFEST", str(path))
    download = Downloads()
    monkeypatch.setattr(say, "download", download)
    loaded: list[tuple] = []

    class StubKokoro(StubSynth):
        model = "tiny.int8.onnx"

        def __init__(self, *args, **kw) -> None:
            loaded.append(args)

        def synth(self, text, voice="", speed=1.0, lang=""):
            return tone(0.3, 24000), 24000

    monkeypatch.setattr(say.serve, "KokoroSynthesizer", StubKokoro)
    monkeypatch.setattr(say.serve, "EspeakSynthesizer", never)
    samples, rate, engine, why = say.synthesize("Hello there.", "af_heart", "en-us", 1.0, True, None)
    assert (engine, why, rate) == ("kokoro", "", 24000) and samples
    assert loaded and loaded[0][0] == cache / "tiny.int8.onnx" and loaded[0][1] == cache / "voices-tiny.bin"
    assert sorted(p.name for p in cache.iterdir() if p.name != ".lock") == ["tiny.int8.onnx", "voices-tiny.bin"]
    assert len(download.urls) == 2
    assert say.synthesize("Again.", "af_heart", "en-us", 1.0, True, None)[2] == "kokoro"
    assert len(download.urls) == 2                                  # nothing downloaded the second time


def test_run_summaries_never_fetch(monkeypatch, tmp_path):
    script = tmp_path / "adws" / "voice" / "say.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    started: list[list[str]] = []

    def fake_popen(argv, **kw):
        started.append(list(argv))
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    layer = SimpleNamespace(ir={"voice": {"speakSummaries": True}}, layer_dir=str(tmp_path))
    runner._speak(layer, "ADW done")
    assert started and started[0][1:] == [str(script), "--no-fetch", "ADW done"]


def _dependencies(path: Path) -> list[str]:
    block = re.search(r"^# /// script\n(.*?)^# ///$", path.read_text(), re.S | re.M)
    assert block, path
    return re.findall(r'^#\s+"([A-Za-z0-9_.-]+)', block.group(1), re.M)


def test_speech_scripts_pull_no_torch_and_fetch_nothing_at_import(monkeypatch, tmp_path):
    for script in ("serve.py", "say.py"):
        names = _dependencies(VOICE / script)
        assert "kokoro-onnx" in names and "onnxruntime" in names, (script, names)
        assert not [n for n in names if n.lower().startswith(("torch", "nvidia", "cuda"))], (script, names)
    cache = tmp_path / "kokoro"
    monkeypatch.setenv("AL_KOKORO_CACHE", str(cache))
    monkeypatch.setattr("urllib.request.urlopen", never)
    for name in ("serve", "say"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    _load("al_voice_serve_import_check", VOICE / "serve.py")
    _load("al_voice_say_import_check", VOICE / "say.py")
    assert not cache.exists()
