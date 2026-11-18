"""The voice service (voice/serve.py) with its standard-library fake engines: sentences, frames, the HTTP
protocol, utterance cutting, barge-in, the network guard and that no audio touches the disk by default."""
from __future__ import annotations

import http.client
import importlib.util
import json
import math
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

VOICE = Path(__file__).resolve().parents[1] / "voice"
SERVE = VOICE / "serve.py"
TOKEN = "test-token-" + "x" * 20


def _load():
    spec = importlib.util.spec_from_file_location("al_voice_serve", SERVE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


serve = _load()


# ---------------------------------------------------------------------------------------------- audio

def tone(seconds: float, amplitude: float = 0.5, rate: int = 16000, hz: float = 220.0) -> bytes:
    n = int(seconds * rate)
    return serve.floats_to_pcm([amplitude * math.sin(2 * math.pi * hz * i / rate) for i in range(n)])


def silence(seconds: float, rate: int = 16000) -> bytes:
    return b"\0\0" * int(seconds * rate)


def frames(pcm: bytes, ms: int = 100) -> list[bytes]:
    size = 16000 * ms // 1000 * 2
    return [pcm[i:i + size] for i in range(0, len(pcm), size)]


# ---------------------------------------------------------------------------------------------- service

class Service:
    def __init__(self, proc: subprocess.Popen, port: int, home: Path) -> None:
        self.proc, self.port, self.home = proc, port, home

    def request(self, method: str, path: str, body: bytes | dict | None = None, headers: dict | None = None,
                token: str | None = TOKEN, host: str | None = None, timeout: float = 30) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        for k, v in h.items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body or b"")))
        conn.endheaders(body or None)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def json(self, method: str, path: str, body=None, **kw) -> tuple[int, dict]:
        status, _h, data = self.request(method, path, body, **kw)
        return status, json.loads(data)

    def listen(self, stream: str, pcm: bytes, speaking: bool = False) -> list[dict]:
        out = []
        for frame in frames(pcm):
            status, data = self.json("POST", f"/listen?stream={stream}&speaking={int(speaking)}", frame,
                                     headers={"Content-Type": "audio/L16;rate=16000"})
            assert status == 200, data
            out.append(data)
        return out


def start(tmp_path: Path, transcripts: list[str] | None = None, *extra: str, layer: Path | None = None,
          env: dict | None = None) -> Service:
    script = tmp_path / "fake.json"
    script.write_text(json.dumps({"transcripts": transcripts or []}))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    full = {k: v for k, v in os.environ.items() if not k.startswith(("AL_VOICE", "AL_KOKORO"))}
    full.update({"AL_VOICE_TOKEN": TOKEN, "HOME": str(home), **(env or {})})
    argv = [sys.executable, str(SERVE), "serve", "--port", "0", "--fake", "--fake-script", str(script), *extra]
    if layer is not None:
        argv += ["--layer", str(layer)]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=full,
                            cwd=tmp_path, start_new_session=True)
    line = proc.stdout.readline()
    assert line, proc.stderr.read()
    return Service(proc, json.loads(line)["port"], home)


@pytest.fixture
def service(tmp_path: Path):
    started: list[Service] = []

    def factory(*args, **kw) -> Service:
        svc = start(tmp_path, *args, **kw)
        started.append(svc)
        return svc
    yield factory
    for svc in started:
        svc.proc.terminate()
        try:
            svc.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            svc.proc.kill()


def voice_layer(tmp_path: Path, **voice) -> Path:
    layer = tmp_path / "layer"
    layer.mkdir(exist_ok=True)
    (layer / "agentic-layer.json").write_text(json.dumps({"voice": {"sttModel": "base.en", **voice},
                                                          "agents": [{"id": "mock", "kind": "mock"}]}))
    return layer


# ---------------------------------------------------------------------------------------------- pure functions

def test_split_sentences() -> None:
    assert serve.split_sentences("") == [] and serve.split_sentences("   \n ") == []
    assert serve.split_sentences("The run failed. Shall I start it? Yes!") == \
        ["The run failed.", "Shall I start it?", "Yes!"]
    assert serve.split_sentences("Use a tier, e.g. heavy, i.e. the big one. Then stop.") == \
        ["Use a tier, e.g. heavy, i.e. the big one.", "Then stop."]
    assert serve.split_sentences("Tests, docs etc. are fine. Ruby vs. Python is old.") == \
        ["Tests, docs etc. are fine.", "Ruby vs. Python is old."]
    assert serve.split_sentences("J. Smith asked. It cost 3.50 dollars. Version 2. 5 runs.")[:2] == \
        ["J. Smith asked.", "It cost 3.50 dollars."]
    assert serve.split_sentences("no end mark") == ["no end mark"]
    long = "This sentence keeps going, " * 12 + "and ends here."
    parts = serve.split_sentences(long, limit=100)
    assert all(len(p) <= 100 for p in parts) and len(parts) > 2
    assert parts[0].endswith(",")                                      # cut at a comma
    assert " ".join(parts) == " ".join(long.split())                   # nothing lost
    spaces = serve.split_sentences("word " * 60, limit=50)
    assert all(len(p) <= 50 for p in spaces) and " ".join(spaces) == " ".join(("word " * 60).split())


def test_envelope_bounds() -> None:
    samples, rate = serve.FakeSynthesizer().synth("ab, cd")
    env = serve.envelope(samples, rate)
    assert env and all(0.0 <= v <= 1.0 for v in env) and max(env) == 1.0
    assert min(env) < 0.05                                             # the comma's silence
    assert len(env) == math.ceil(len(samples) / (rate * 20 // 1000))
    long = serve.envelope([0.3] * (24000 * 60), 24000)                 # one minute: at most 600 values
    assert 0 < len(long) <= 600
    assert serve.envelope([0.0] * 1000, 24000) == [0.0] * 3
    assert serve.envelope([], 24000) == []


def test_frame_round_trip() -> None:
    wav = serve.wav_bytes(tone(0.1), 16000)
    one = serve.encode_frame({"seq": 0, "text": "Hi.", "duration": 0.1, "rate": 16000, "envelope": [1.0],
                              "last": False}, wav)
    two = serve.encode_frame({"seq": 1, "cancelled": True, "last": True})
    assert one[:4] == b"ALV1"
    (hlen,) = struct.unpack(">I", one[4:8])
    assert json.loads(one[8:8 + hlen])["text"] == "Hi."
    decoded = serve.decode_frames(one + two)
    assert [h["seq"] for h, _ in decoded] == [0, 1]
    assert decoded[0][1] == wav and decoded[1][1] == b"" and decoded[1][0]["cancelled"] is True
    for broken in (one[:-5], one[:6], one + two[:10]):
        with pytest.raises(ValueError):
            serve.decode_frames(broken)
    with pytest.raises(ValueError):
        serve.decode_frames(b"XXXX" + one[4:])


# ---------------------------------------------------------------------------------------------- HTTP

def test_health_reports_fake_ready(service) -> None:
    svc = service()
    status, health = svc.json("GET", "/health")
    assert status == 200
    assert health["ready"] is True and health["loading"] is False and health["fake"] is True
    assert health["missing"] == [] and health["voice"] == "af_heart"
    assert set(health) >= {"device", "stt_model", "tts_model", "rtf"} and set(health["rtf"]) == {"stt", "tts"}


def test_requests_need_the_token_and_loopback_host(service) -> None:
    svc = service()
    assert svc.request("GET", "/health", token=None)[0] == 401
    assert svc.request("GET", "/health", token="wrong")[0] == 401
    assert svc.request("GET", "/health", host="evil.example")[0] == 403
    assert svc.request("GET", "/health", host="evil.example:80", token=None)[0] == 403
    assert svc.request("GET", "/health", host="localhost")[0] == 200
    assert svc.request("POST", "/health", b"")[0] == 405
    assert svc.request("GET", "/speak")[0] == 405
    assert svc.request("GET", "/nothing")[0] == 404
    assert svc.request("POST", "/transcribe", tone(0.2), headers={"Content-Type": "text/plain"})[0] == 415


def test_body_caps(service, tmp_path: Path) -> None:
    svc = service(layer=voice_layer(tmp_path, maxUtteranceSeconds=5))
    cap = 5 * 32000 + 44
    assert svc.request("POST", "/speak", b"{" + b" " * (8 * 1024) + b"}",
                       headers={"Content-Type": "application/json"})[0] == 413
    big = serve.wav_bytes(tone(5.1), 16000)
    assert len(big) > cap
    assert svc.request("POST", "/transcribe", big, headers={"Content-Type": "audio/wav"})[0] == 413
    assert svc.request("POST", "/listen?stream=a&speaking=0", b"\0" * (cap + 2),
                       headers={"Content-Type": "audio/L16;rate=16000"})[0] == 413
    ok = serve.wav_bytes(tone(4.5), 16000)
    assert svc.request("POST", "/transcribe", ok, headers={"Content-Type": "audio/wav"})[0] == 200


def test_transcribe_returns_text_and_word_timings(service) -> None:
    svc = service(["start a plan for adding a search box", "yes"])
    status, out = svc.json("POST", "/transcribe", serve.wav_bytes(tone(2.0), 16000),
                           headers={"Content-Type": "audio/wav"})
    assert status == 200 and out["text"] == "start a plan for adding a search box"
    assert [w["w"] for w in out["words"]] == out["text"].split()
    assert all(set(w) == {"w", "start", "end", "p"} for w in out["words"])
    assert out["words"][0]["start"] == 0 and abs(out["words"][-1]["end"] - 2.0) < 0.01
    assert all(a["end"] <= b["start"] + 1e-9 for a, b in zip(out["words"], out["words"][1:]))
    # a 48 kHz WAV is resampled; raw L16 is taken as it is
    status, out = svc.json("POST", "/transcribe", serve.wav_bytes(tone(1.0, rate=48000), 48000),
                           headers={"Content-Type": "audio/wav"})
    assert status == 200 and out["text"] == "yes" and abs(out["seconds"] - 1.0) < 0.01
    status, out = svc.json("POST", "/transcribe", tone(0.5), headers={"Content-Type": "audio/L16;rate=16000"})
    assert status == 200 and out["text"] == ""                         # the script is used up
    assert svc.request("POST", "/transcribe", b"RIFFnonsense", headers={"Content-Type": "audio/wav"})[0] == 400


def read_frames(svc: Service, body: dict, cancel_after: int | None = None) -> list[tuple[dict, bytes]]:
    conn = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=30)
    data = json.dumps(body).encode()
    conn.request("POST", "/speak", data, {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type") == "application/x-al-voice-frames"
    assert resp.getheader("Transfer-Encoding") == "chunked"
    out: list[tuple[dict, bytes]] = []
    buf = b""
    while True:
        chunk = resp.read1(65536)
        if not chunk:
            break
        buf += chunk
        try:
            out = serve.decode_frames(buf)
        except ValueError:
            continue
        if cancel_after is not None and len(out) == cancel_after:
            status, res = svc.json("POST", "/cancel", {"id": body["id"]})
            assert status == 200 and res == {"cancelled": True}
            cancel_after = None
    conn.close()
    return serve.decode_frames(buf)


def test_speak_streams_one_frame_per_sentence(service) -> None:
    svc = service()
    got = read_frames(svc, {"id": "r1", "text": "The run failed. Shall I, maybe, start it? Yes."})
    heads = [h for h, _ in got]
    assert [h["seq"] for h in heads] == [0, 1, 2]
    assert [h["text"] for h in heads] == ["The run failed.", "Shall I, maybe, start it?", "Yes."]
    assert [h["last"] for h in heads] == [False, False, True]
    for head, wav in got:
        pcm, rate = serve.read_wav(wav)
        assert rate == head["rate"] == 24000
        assert abs(len(pcm) / 2 / rate - head["duration"]) < 0.01
        assert head["envelope"] and max(head["envelope"]) == 1.0
    assert min(heads[1]["envelope"]) < 0.05                             # the comma pauses are in the envelope
    assert svc.json("POST", "/speak", {"id": "r2", "text": "  "})[0] == 400
    assert svc.json("POST", "/speak", {"id": "../x", "text": "Hi."})[0] == 400
    assert svc.json("POST", "/cancel", {"id": "r1"}) == (200, {"cancelled": False})   # already done


def test_cancel_stops_before_the_next_sentence(service) -> None:
    svc = service()
    sentence = "This sentence is long enough to take a few seconds of fake speech to say."
    got = read_frames(svc, {"id": "r9", "text": " ".join([sentence] * 6)}, cancel_after=1)
    audio = [h for h, wav in got if wav]
    assert 1 <= len(audio) <= 2                                        # at most one more after the cancel
    assert got[-1][0] == {"seq": len(audio), "cancelled": True, "last": True} and got[-1][1] == b""


def test_listen_cuts_an_utterance(service) -> None:
    svc = service(["what is running right now"])
    replies = svc.listen("s1", silence(0.5) + tone(1.0) + silence(1.0))
    states = [r["state"] for r in replies]
    assert states[:5] == ["silence"] * 5
    assert "speech" in states and states.count("end") == 1
    assert states.index("speech") < states.index("end")
    end = replies[states.index("end")]
    assert end["text"] == "what is running right now" and [w["w"] for w in end["words"]][0] == "what"
    assert end["barge_in"] is False
    assert all(r["state"] == "silence" for r in replies[states.index("end") + 1:])
    assert any(r.get("partial") for r in replies if r["state"] == "speech")   # a live caption while speaking
    assert svc.json("POST", "/listen?stream=bad/id", silence(0.1),
                    headers={"Content-Type": "audio/L16;rate=16000"})[0] == 400


def test_listen_barge_in_needs_the_higher_threshold(service) -> None:
    svc = service(["quiet", "loud"])
    quiet = svc.listen("q", silence(0.3) + tone(1.0, amplitude=0.06) + silence(1.0), speaking=True)
    assert not any(r["barge_in"] for r in quiet)
    assert all(r["state"] == "silence" for r in quiet)                 # under 0.85: the helper's own echo
    heard = svc.listen("q2", silence(0.3) + tone(1.0, amplitude=0.06) + silence(1.0), speaking=False)
    assert any(r["state"] == "end" for r in heard)                     # the same level is speech otherwise
    loud = svc.listen("l", silence(0.3) + tone(1.0, amplitude=0.5) + silence(1.0), speaking=True)
    first = next(i for i, r in enumerate(loud) if r["barge_in"])
    onset = 3                                                           # the tone starts with frame 3
    assert (first - onset + 1) * 100 <= 400                             # 100 ms to start + 200 ms of speech


def test_max_utterance_cut(service, tmp_path: Path) -> None:
    svc = service(["one long sentence"], layer=voice_layer(tmp_path, maxUtteranceSeconds=5))
    replies = svc.listen("m", tone(7.0))
    ends = [i for i, r in enumerate(replies) if r["state"] == "end"]
    assert ends and ends[0] <= 52                                        # cut at five seconds, not at silence
    assert replies[ends[0]]["text"] == "one long sentence" and replies[ends[0]]["cut"] is True


def test_no_audio_written_unless_record_dir(service, tmp_path: Path) -> None:
    def wavs() -> set[Path]:
        return {p for p in tmp_path.rglob("*.wav")}
    before = wavs()
    svc = service(["a", "b"])
    svc.json("POST", "/transcribe", serve.wav_bytes(tone(1.0), 16000), headers={"Content-Type": "audio/wav"})
    svc.listen("x", tone(0.6) + silence(0.8))
    assert wavs() == before
    svc.proc.terminate()
    record = tmp_path / "rec"
    svc2 = service(["c", "d"], "--record-dir", str(record))
    svc2.json("POST", "/transcribe", serve.wav_bytes(tone(1.0), 16000), headers={"Content-Type": "audio/wav"})
    svc2.listen("x", tone(0.6) + silence(0.8))
    written = sorted(p.name for p in record.glob("*.wav"))
    assert written == ["1.wav", "2.wav"]
    assert all((record / n).stat().st_mode & 0o777 == 0o600 for n in written)
    assert wavs() - before == {record / n for n in written}


def test_network_audit_hook_blocks_outbound(tmp_path: Path) -> None:
    log = tmp_path / "net.jsonl"
    code = (
        "import socket, sys\n"
        f"sys.path.insert(0, {str(VOICE)!r})\n"
        "import serve\n"
        "serve.install_network_guard()\n"
        "try:\n"
        "    socket.create_connection(('192.0.2.1', 80), timeout=2)\n"
        "    print('connected')\n"
        "except PermissionError as e:\n"
        "    print('refused', e)\n"
        "try:\n"
        "    s = socket.socket(); s.settimeout(2); s.connect(('192.0.2.1', 80))\n"
        "except PermissionError:\n"
        "    print('refused-connect')\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=2)\n"
        "except PermissionError:\n"
        "    print('loopback refused')\n"
        "except OSError:\n"
        "    print('loopback allowed')\n"
    )
    env = {**os.environ, "AL_VOICE_NET_LOG": str(log)}
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert cp.returncode == 0, cp.stderr
    lines = cp.stdout.splitlines()
    assert lines[0].startswith("refused") and "refused-connect" in lines and "loopback allowed" in lines
    logged = [json.loads(line) for line in log.read_text().splitlines()]
    assert logged and all("192.0.2.1" in e["address"] for e in logged)
    assert {e["event"] for e in logged} <= {"socket.getaddrinfo", "socket.connect"}


def test_serve_refuses_without_models(tmp_path: Path) -> None:
    log = tmp_path / "net.jsonl"
    cache = tmp_path / "cache"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AL_VOICE", "AL_KOKORO"))}
    env.update({"AL_VOICE_TOKEN": TOKEN, "AL_VOICE_CACHE": str(cache), "AL_KOKORO_CACHE": str(cache / "kokoro"),
                "AL_VOICE_NET_LOG": str(log),
                "HOME": str(tmp_path)})
    layer = voice_layer(tmp_path)
    cp = subprocess.run([sys.executable, str(SERVE), "serve", "--layer", str(layer), "--port", "0"],
                        capture_output=True, text=True, env=env, timeout=60)
    assert cp.returncode == 1
    assert f"voice is not set up: al voice {layer} setup" in cp.stderr
    first = json.loads(cp.stdout.splitlines()[0])
    assert "al voice" in first["error"] and any("model.bin" in m for m in first["missing"])
    assert not log.exists() or log.read_text() == ""                   # nothing tried to download
    assert not cache.exists() or not any(cache.rglob("*"))
    no_token = {k: v for k, v in env.items() if k != "AL_VOICE_TOKEN"}
    cp = subprocess.run([sys.executable, str(SERVE), "serve", "--fake"], capture_output=True, text=True,
                        env=no_token, timeout=60)
    assert cp.returncode == 2 and "AL_VOICE_TOKEN" in cp.stderr


def test_manifest_records_the_spike_files() -> None:
    manifest = serve.load_manifest(VOICE / "models.json")
    assert set(manifest["whisper"]) == {"base.en", "small.en"}
    base = manifest["whisper"]["base.en"]
    assert base["revision"] == "3d3d5dee26484f91867d81cb899cfcf72b96be6c"
    assert base["files"]["model.bin"]["bytes"] == 145216508
    assert manifest["kokoro"]["model"] == "kokoro-v1.0.int8.onnx"
    assert manifest["kokoro"]["files"]["kokoro-v1.0.onnx"]["optional"] is True
    paths = serve.cache_paths(manifest, "base.en")
    assert {p["name"] for p in paths} >= {"whisper base.en/model.bin", "kokoro voices-v1.0.bin"}
    assert next(p for p in paths if p["name"] == "whisper base.en/model.bin")["url"] == \
        "https://huggingface.co/Systran/faster-whisper-base.en/resolve/3d3d5dee26484f91867d81cb899cfcf72b96be6c/model.bin"
    with pytest.raises(serve.NotRecorded, match="base.en, small.en"):
        serve.cache_paths(manifest, "tiny.en")


def test_service_answers_while_another_request_streams(service) -> None:
    svc = service(["hello"])
    results: list[int] = []
    t = threading.Thread(target=lambda: results.append(len(read_frames(svc, {"id": "s", "text": "One. Two. Three."}))))
    t.start()
    time.sleep(0.05)
    assert svc.json("GET", "/health")[0] == 200
    t.join(timeout=30)
    assert results == [3]


def test_skips_print_their_reason(pytestconfig) -> None:
    assert "s" in pytestconfig.option.reportchars
