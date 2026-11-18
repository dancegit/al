"""The real speech engines, end to end: Kokoro speaks five sentences, faster-whisper recognises them.

Runs only with AL_VOICE_REAL=1 and the models in the cache (`al voice LAYER setup`, or
`uv run --script kit/runtime/voice/serve.py setup --stt-model base.en`); it never downloads. With
AL_VOICE_ARTEFACTS=DIR it keeps roundtrip-1.wav … roundtrip-5.wav and roundtrip.json there.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from test_voice_service import SERVE, TOKEN, serve

pytestmark = [pytest.mark.voice, pytest.mark.slow]

SENTENCES = ["What is running right now?", "Why did the last run fail?", "Start a plan for adding a search box.",
             "Yes, prepare it for approval.", "Stop the run that is waiting for review."]
STT_MODEL = "base.en"


def _models_missing() -> list[str]:
    return serve.missing_files(serve.verify(serve.cache_paths(serve.load_manifest(), STT_MODEL), checksum=False))


def _skip_reason() -> str | None:
    if os.environ.get("AL_VOICE_REAL") != "1":
        return "the real speech engines run only with AL_VOICE_REAL=1"
    if not shutil.which("uv"):
        return "uv not found"
    missing = _models_missing()
    if missing:
        return f"voice models not set up ({', '.join(missing)}): serve.py setup --stt-model {STT_MODEL}"
    return None


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def edits(ref: list[str], hyp: list[str]) -> int:
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1]


def call(port: int, method: str, path: str, body: bytes | None = None, kind: str = "application/json",
         timeout: float = 120) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body, {"Authorization": f"Bearer {TOKEN}", "Content-Type": kind})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_round_trip_wer(tmp_path: Path) -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)
    layer = tmp_path / "layer"
    layer.mkdir()
    (layer / "agentic-layer.json").write_text(json.dumps({"voice": {"sttModel": STT_MODEL, "sttDevice": "cpu"},
                                                          "agents": [{"id": "mock", "kind": "mock"}]}))
    net_log = tmp_path / "net.jsonl"
    env = {**os.environ, "AL_VOICE_TOKEN": TOKEN, "AL_VOICE_NET_LOG": str(net_log)}
    proc = subprocess.Popen(["uv", "run", "--quiet", "--script", str(SERVE), "serve", "--layer", str(layer),
                             "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            start_new_session=True)
    try:
        line = proc.stdout.readline()
        assert line, proc.stderr.read()
        port = json.loads(line)["port"]
        deadline = time.monotonic() + 600
        while True:
            status, data = call(port, "GET", "/health")
            health = json.loads(data)
            if health["ready"] or health.get("error") or time.monotonic() > deadline:
                break
            time.sleep(1)
        assert health["ready"], health
        assert health["fake"] is False and health["missing"] == []
        transcripts, ref_words, errors = [], 0, 0
        artefacts = Path(os.environ["AL_VOICE_ARTEFACTS"]) if os.environ.get("AL_VOICE_ARTEFACTS") else None
        if artefacts:
            artefacts.mkdir(parents=True, exist_ok=True)
        for n, sentence in enumerate(SENTENCES, 1):
            status, data = call(port, "POST", "/speak", json.dumps({"id": f"rt{n}", "text": sentence}).encode())
            assert status == 200, data
            frames = serve.decode_frames(data)
            assert frames[-1][0]["last"] is True and not frames[-1][0].get("cancelled")
            pcm, rate = b"", 0
            for _head, wav in frames:
                part, rate = serve.read_wav(wav)
                pcm += part
            wav = serve.wav_bytes(pcm, rate)
            if artefacts:
                (artefacts / f"roundtrip-{n}.wav").write_bytes(wav)
            status, data = call(port, "POST", "/transcribe", serve.wav_bytes(serve.resample(pcm, rate), 16000),
                                kind="audio/wav")
            assert status == 200, data
            heard = json.loads(data)["text"]
            transcripts.append(heard)
            ref_words += len(words(sentence))
            errors += edits(words(sentence), words(heard))
        wer = errors / ref_words
        rtf = health["rtf"]
        if artefacts:
            (artefacts / "roundtrip.json").write_text(json.dumps(
                {"sentences": SENTENCES, "transcripts": transcripts, "wer": round(wer, 4), "rtf": rtf,
                 "device": health["device"], "stt_model": health["stt_model"], "tts_model": health["tts_model"]},
                indent=2))
        assert wer <= 0.10, (wer, transcripts)
        assert rtf["stt"] is not None and rtf["stt"] < 1.0 and rtf["tts"] is not None and rtf["tts"] < 1.0, rtf
        assert not net_log.exists() or net_log.read_text() == "", net_log.read_text()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
