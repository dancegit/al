#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#   "faster-whisper>=1.1,<2",
#   # faster-whisper 1.2.1 calls av.open(..., metadata_errors=...), which PyAV 19 rejects (see listen.py)
#   "av>=11,<16",
#   # 0.6 has Kokoro.from_session, which lets the service pin onnxruntime's threads
#   "kokoro-onnx>=0.6,<0.7",
#   "onnxruntime>=1.20",
#   "numpy",
#   "soundfile>=0.12",
# ]
# ///
"""The voice service of the dashboard's helper: local speech recognition and speech, over loopback HTTP.

    serve.py serve  --layer DIR [--port 0] [--threads 4] [--fake --fake-script FILE] [--record-dir DIR]
    serve.py setup  (--layer DIR | --stt-model NAME)
    serve.py doctor (--layer DIR | --stt-model NAME) [--json]

`setup` downloads the model files recorded in models.json (faster-whisper from Hugging Face at a pinned revision,
Kokoro int8 ONNX and its voices) into the user cache and checks every sha256; it is the only command that uses
the network. `doctor` checks the files, loads both engines, speaks and recognises one sentence and prints the
speed. `serve` (started by the dashboard, never by hand) loads the verified files only: it sets HF_HUB_OFFLINE,
refuses every non-loopback connection with an audit hook (logged to $AL_VOICE_NET_LOG when set), binds
127.0.0.1, wants `Authorization: Bearer $AL_VOICE_TOKEN` on every request and prints `{"port": N}` as its first
stdout line. Audio stays in memory unless --record-dir is given.

    GET  /health                                → {"ready", "loading", "device", "stt_model", "tts_model", "voice",
                                                   "rtf": {"stt", "tts"}, "missing": [...], "fake"}
    POST /transcribe   (audio/wav | audio/L16)  → {"text", "words": [{"w", "start", "end", "p"}], "seconds"}
    POST /listen?stream=ID&speaking=0|1 (L16)   → {"state": "silence"|"speech"|"end", "prob", "barge_in",
                                                   "partial"?, "text"?, "words"?}
    POST /speak  {"id", "text", "voice"?, "speed"?}  → application/x-al-voice-frames, one frame per sentence
    POST /cancel {"id"}                         → {"cancelled": bool}

A frame is b"ALV1" + u32 header length + header JSON + u32 audio length + WAV bytes (s16le mono); the u32s are
big-endian (network order). Header: {"seq", "text", "duration", "rate", "envelope", "last"}; a cancelled stream
ends with {"seq", "cancelled": true, "last": true} and no audio.

`--fake` uses standard-library engines (a tone for speech, transcripts from --fake-script
{"transcripts": [...]}), so the protocol is tested without the speech packages:
`python3 serve.py serve --fake --fake-script F` needs nothing but Python.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import hmac
import http.server
import io
import ipaddress
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import wave
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

RATE_IN = 16000                 # what the recogniser and the detector take: 16 kHz mono s16le
WINDOW = 512                    # Silero's window at 16 kHz (32 ms)
SPEAK_BODY = 8 * 1024
FRAME_MAGIC = b"ALV1"
FRAME_TYPE = "application/x-al-voice-frames"
STREAM_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
STREAM_IDLE = 60.0              # a /listen stream unused this long is dropped
LEAD = 1.5                      # a /speak frame is sent this many seconds before the audio sent so far ends
DOCTOR_SENTENCE = "The last run failed at the test step. Shall I start a new plan?"
WARMUP_SENTENCE = "Shall I start it?"
# about ten seconds of speech: whisper pads every clip to a 30 s window, so a real-time factor measured on
# one short sentence is mostly that fixed cost; the /health figures are measured on this instead
MEASURE_TEXT = ("The last run failed at the test step, because one assertion about the search box did not hold. "
                "I can prepare a new plan that fixes it, and you can approve it on the card. Shall I start it?")
VOICE_DEFAULTS: dict[str, Any] = {
    "ttsVoice": "af_heart", "ttsLang": "en-us", "ttsSpeed": 100, "sttModel": "base.en", "sttDevice": "auto",
    "recordSeconds": 8, "speakSummaries": True, "dashboardHelper": True, "listenMode": "hold", "helperAgent": "",
    "helperModelSet": "base", "interrupt": True, "maxUtteranceSeconds": 30,
}


def log(msg: str) -> None:
    print(f"voice: {msg}", file=sys.stderr, flush=True)


# ===== layer and manifest

def layer_voice(layer: str | Path | None) -> dict:
    """The layer's `voice` with every default filled (an IR compiled before a field existed too)."""
    voice: dict = {}
    if layer:
        try:
            voice = json.loads((Path(layer) / "agentic-layer.json").read_text()).get("voice") or {}
        except (OSError, ValueError):
            voice = {}
    return {**VOICE_DEFAULTS, **{k: v for k, v in voice.items() if v is not None}}


def layer_ir(layer: str | Path | None) -> dict:
    if not layer:
        return {}
    try:
        return json.loads((Path(layer) / "agentic-layer.json").read_text())
    except (OSError, ValueError):
        return {}


def setup_hint(layer: str | Path | None) -> str:
    return f"al voice {layer} setup" if layer else "al voice LAYER setup"


def load_manifest(path: str | Path | None = None) -> dict:
    """models.json beside this file ($AL_VOICE_MANIFEST wins)."""
    where = Path(path or os.environ.get("AL_VOICE_MANIFEST") or HERE / "models.json")
    return json.loads(where.read_text())


def voice_cache() -> Path:
    return Path(os.environ.get("AL_VOICE_CACHE") or Path.home() / ".cache" / "al" / "voice")


def kokoro_cache() -> Path:
    """Kokoro's files live where say.py keeps them: ~/.cache/al/kokoro, or $AL_KOKORO_CACHE (read at each
    call, as say.CACHE_DIR is fixed when say.py is imported)."""
    return Path(os.environ.get("AL_KOKORO_CACHE") or Path.home() / ".cache" / "al" / "kokoro")


class NotRecorded(ValueError):
    """A speech model that models.json does not record (so it has no checksum)."""


def cache_paths(manifest: dict, stt_model: str) -> list[dict]:
    """Every model file the service needs: name, path, url, size, sha256, optional."""
    whisper = manifest.get("whisper") or {}
    if stt_model not in whisper:
        raise NotRecorded(f"speech model `{stt_model}` is not recorded in models.json (no checksum to verify it "
                          f"against); recorded: {', '.join(sorted(whisper))}")
    entry = whisper[stt_model]
    endpoint = (manifest.get("endpoint") or "https://huggingface.co").rstrip("/")
    out = []
    for name, f in entry["files"].items():
        out.append({"name": f"whisper {stt_model}/{name}", "path": voice_cache() / "whisper" / stt_model / name,
                    "url": f"{endpoint}/{entry['repo']}/resolve/{entry['revision']}/{name}",
                    "size": int(f["bytes"]), "sha256": f["sha256"], "optional": bool(f.get("optional"))})
    kokoro = manifest["kokoro"]
    for name, f in kokoro["files"].items():
        out.append({"name": f"kokoro {name}", "path": kokoro_cache() / name,
                    "url": f"{kokoro['release'].rstrip('/')}/{name}",
                    "size": int(f["bytes"]), "sha256": f["sha256"], "optional": bool(f.get("optional"))})
    return out


def model_files(manifest: dict, stt_model: str) -> dict:
    """{"whisper": dir, "kokoro": model path, "voices": path} of the files the service loads."""
    paths = {p["name"]: p["path"] for p in cache_paths(manifest, stt_model)}
    kokoro = manifest["kokoro"]
    voices = next(n for n in kokoro["files"] if n.startswith("voices"))
    return {"whisper": voice_cache() / "whisper" / stt_model, "kokoro": paths[f"kokoro {kokoro['model']}"],
            "voices": paths[f"kokoro {voices}"]}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(paths: list[dict], checksum: bool = True) -> list[dict]:
    """Per file: name, path, bytes (on disk, None when absent), expected (bytes), ok, why; with
    `checksum` the sha256 is compared too. An optional file that is absent is ok."""
    rows = []
    for p in paths:
        path = Path(p["path"])
        row = {"name": p["name"], "path": str(path), "bytes": None, "expected": p["size"], "ok": False, "why": "",
               "optional": p.get("optional", False), "sha256": p["sha256"]}
        if not path.is_file():
            row["ok"] = row["optional"]
            row["why"] = "optional, not present" if row["optional"] else "missing"
        else:
            row["bytes"] = path.stat().st_size
            if row["bytes"] != p["size"]:
                row["why"] = f"{row['bytes']} bytes, expected {p['size']}"
            elif checksum and (actual := sha256_file(path)) != p["sha256"]:
                row["why"] = f"sha256 {actual}, expected {p['sha256']}"
            else:
                row["ok"] = True
        rows.append(row)
    return rows


def missing_files(rows: list[dict]) -> list[str]:
    return [f"{r['name']} ({r['why']})" for r in rows if not r["ok"]]


def human_size(n: int | None) -> str:
    if n is None:
        return "-"
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def stt_model_of(layer: str | None, stt_model: str | None) -> str:
    return stt_model or str(layer_voice(layer)["sttModel"])


# ===== setup

def setup(layer: str | None, out=None, stt_model: str | None = None) -> int:
    """Download every missing or mismatching model file, verify its sha256; 0 when all are in place."""
    out = out or sys.stdout
    say = lambda line: print(line, file=out, flush=True)  # noqa: E731
    manifest = load_manifest()
    model = stt_model_of(layer, stt_model)
    try:
        paths = cache_paths(manifest, model)
    except NotRecorded as exc:
        print(f"voice setup: {exc}", file=sys.stderr)
        return 2
    from say import download
    total = fetched = 0
    for p, row in zip(paths, verify(paths)):
        if row["ok"] and row["bytes"] is None:      # optional and absent: never downloaded
            continue
        if row["ok"]:
            say(f"  {p['name']:<40} {human_size(p['size']):>9}  already there, checksum ok")
            total += p["size"]
            continue
        path = Path(p["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()                            # wrong size or checksum: fetch again from the start
        say(f"  {p['name']:<40} {human_size(p['size']):>9}  downloading {p['url']}")
        download(p["url"], path)
        actual = sha256_file(path)
        if actual != p["sha256"]:
            path.unlink(missing_ok=True)
            print(f"voice setup: {p['name']}: checksum mismatch, the file was removed\n"
                  f"  expected sha256 {p['sha256']}\n  got      sha256 {actual}", file=sys.stderr)
            return 1
        size = path.stat().st_size
        say(f"  {p['name']:<40} {human_size(size):>9}  downloaded, checksum ok")
        total += size
        fetched += size
    say(f"voice setup: {human_size(total)} of speech models in {voice_cache()} and {kokoro_cache()} "
        f"({human_size(fetched)} downloaded now)")
    say("nothing will be downloaded again; the service runs offline")
    return 0


# ===== network guard

def _loopback(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    host = str(host).strip("[]")
    if host in ("localhost", "localhost.localdomain", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _deny_network(event: str, args: tuple) -> None:
    """Audit hook of `serve` and `doctor`: every connection or name lookup that is not loopback raises."""
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, (str, bytes)):           # AF_UNIX: a local socket
            return
        host = address[0] if isinstance(address, tuple) and address else address
    elif event == "socket.getaddrinfo":
        host = args[0]
    else:
        return
    if _loopback(host):
        return
    where = os.environ.get("AL_VOICE_NET_LOG")
    if where:
        try:
            with open(where, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "event": event, "address": repr(host)}) + "\n")
        except OSError:
            pass
    raise PermissionError(f"voice service: outbound network refused ({event} {host!r})")


def install_network_guard() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    sys.addaudithook(_deny_network)


# ===== audio helpers (standard library; numpy arrays accepted too)

def _floats(samples: Any) -> list[float]:
    return samples.tolist() if hasattr(samples, "tolist") else list(samples)


def pcm_to_floats(pcm: bytes) -> list[float]:
    ints = array.array("h")
    ints.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        ints.byteswap()
    return [v / 32768.0 for v in ints]


def floats_to_pcm(samples: Any) -> bytes:
    if hasattr(samples, "astype"):
        import numpy as np
        return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    ints = array.array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in samples))
    if sys.byteorder == "big":
        ints.byteswap()
    return ints.tobytes()


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def read_wav(data: bytes) -> tuple[bytes, int]:
    """(s16le mono pcm, rate) of a WAV; ValueError when it is not 16-bit mono PCM."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise ValueError("a WAV of 16-bit mono PCM is needed")
            return w.readframes(w.getnframes()), w.getframerate()
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"not a WAV: {exc}") from exc


def resample(pcm: bytes, rate: int, to: int = RATE_IN) -> bytes:
    """Linear resampling of s16le mono (enough for speech going to the recogniser)."""
    if rate == to or not pcm:
        return pcm
    src = pcm_to_floats(pcm)
    n = max(1, int(len(src) * to / rate))
    step = (len(src) - 1) / max(1, n - 1)
    out = []
    for i in range(n):
        x = i * step
        j = int(x)
        k = min(j + 1, len(src) - 1)
        out.append(src[j] + (src[k] - src[j]) * (x - j))
    return floats_to_pcm(out)


def envelope(samples: Any, rate: int, step_ms: int = 20) -> list[float]:
    """RMS per `step_ms`, normalised to 0–1, at most 600 values (the step grows for long audio)."""
    values = _floats(samples)
    step = max(1, int(rate * step_ms / 1000))
    if len(values) > 600 * step:
        step = math.ceil(len(values) / 600)
    rms = []
    for i in range(0, len(values), step):
        chunk = values[i:i + step]
        rms.append(math.sqrt(sum(v * v for v in chunk) / len(chunk)) if chunk else 0.0)
    top = max(rms, default=0.0)
    return [round(v / top, 3) if top > 0 else 0.0 for v in rms][:600]


# ===== sentences

_ABBREV = re.compile(r"(?:^|[^A-Za-z])(?:e\.g|i\.e|etc|vs|mr|mrs|ms|dr)$", re.IGNORECASE)
_INITIAL = re.compile(r"(?:^|[^A-Za-z])[A-Z]$")


def split_sentences(text: str, limit: int = 220) -> list[str]:
    """Sentences to speak one by one: split after . ! ? followed by a space or the end, not after e.g, i.e,
    etc, vs or a single capital letter (an initial), not inside a number; a piece over `limit` characters is
    split at its last comma (else space) before the limit."""
    text = " ".join((text or "").split())
    pieces: list[str] = []
    start = 0
    for m in re.finditer(r"[.!?]+(?=\s|$)", text):
        before = text[start:m.start()]
        if m.group().startswith(".") and len(m.group()) == 1:
            if _ABBREV.search(before) or _INITIAL.search(before):
                continue
            if before[-1:].isdigit() and text[m.end() + 1:m.end() + 2].isdigit():
                continue
        piece = text[start:m.end()].strip()
        if piece:
            pieces.append(piece)
        start = m.end()
    rest = text[start:].strip()
    if rest:
        pieces.append(rest)
    out: list[str] = []
    for piece in pieces:
        while len(piece) > limit:
            head = piece[:limit]
            cut = head.rfind(",")
            if cut <= 0:
                cut = head.rfind(" ")
            if cut <= 0:
                cut = limit - 1
            out.append(piece[:cut + 1].strip())
            piece = piece[cut + 1:].strip()
        if piece:
            out.append(piece)
    return out


# ===== framing

def encode_frame(header: dict, wav: bytes = b"") -> bytes:
    head = json.dumps(header, separators=(",", ":")).encode()
    return FRAME_MAGIC + struct.pack(">I", len(head)) + head + struct.pack(">I", len(wav)) + wav


def decode_frames(data: bytes) -> list[tuple[dict, bytes]]:
    """Every frame of a /speak response; ValueError on a bad magic or a truncated frame."""
    frames = []
    i = 0
    while i < len(data):
        if data[i:i + 4] != FRAME_MAGIC:
            raise ValueError(f"bad frame magic at byte {i}")
        if i + 8 > len(data):
            raise ValueError("truncated frame header")
        (hlen,) = struct.unpack(">I", data[i + 4:i + 8])
        if i + 8 + hlen + 4 > len(data):
            raise ValueError("truncated frame header")
        header = json.loads(data[i + 8:i + 8 + hlen])
        (alen,) = struct.unpack(">I", data[i + 8 + hlen:i + 12 + hlen])
        start = i + 12 + hlen
        if start + alen > len(data):
            raise ValueError("truncated frame audio")
        frames.append((header, data[start:start + alen]))
        i = start + alen
    return frames


# ===== engines

class FakeRecognizer:
    """Returns the transcripts of a fake script one per utterance ({"transcripts": [...]}; re-read at every
    call, so a test may rewrite it), the words spread evenly over the audio; "" when the list is used up."""

    device = "fake"
    sync_partial = True

    def __init__(self, script: str | Path | None) -> None:
        self.script = Path(script) if script else None
        self.used = 0
        self.lock = threading.Lock()

    def _texts(self) -> list[str]:
        if not self.script:
            return []
        try:
            return [str(t) for t in json.loads(self.script.read_text()).get("transcripts", [])]
        except (OSError, ValueError):
            return []

    def _result(self, text: str, seconds: float) -> dict:
        words = text.split()
        each = seconds / len(words) if words else 0.0
        return {"text": text, "seconds": round(seconds, 3),
                "words": [{"w": w, "start": round(i * each, 3), "end": round((i + 1) * each, 3), "p": 0.99}
                          for i, w in enumerate(words)]}

    def transcribe(self, pcm: bytes) -> dict:
        with self.lock:
            texts = self._texts()
            text = texts[self.used] if self.used < len(texts) else ""
            self.used += 1
        return self._result(text, len(pcm) / 2 / RATE_IN)

    def partial(self, pcm: bytes) -> str:
        """The first half of the next transcript, without using it up."""
        texts = self._texts()
        words = (texts[self.used] if self.used < len(texts) else "").split()
        return " ".join(words[:max(1, len(words) // 2)]) if words else ""


class FakeSynthesizer:
    """A 220 Hz tone, 60 ms per character, with 300 ms of silence after each comma (loud and quiet moments)."""

    rate = 24000
    model = "fake"

    def synth(self, text: str, voice: str = "", speed: float = 1.0, lang: str = "") -> tuple[list[float], int]:
        per = int(self.rate * 0.060 / max(0.25, speed))
        out: list[float] = []
        for ch in text:
            base = len(out)
            out.extend(0.5 * math.sin(2 * math.pi * 220 * (base + i) / self.rate) for i in range(per))
            if ch == ",":
                out.extend([0.0] * int(self.rate * 0.300))
        return out, self.rate


class FakeDetector:
    """A loudness detector: RMS over 0.1 → 0.95, over 0.02 → 0.6 (speech, but not loud enough to barge in
    over the helper's own voice), else 0.02."""

    def prob(self, frame: list[float]) -> float:
        rms = math.sqrt(sum(v * v for v in frame) / len(frame)) if frame else 0.0
        return 0.95 if rms > 0.1 else 0.6 if rms > 0.02 else 0.02


class WhisperRecognizer:
    """faster-whisper from a verified local directory (listen.load_model_dir)."""

    sync_partial = False

    def __init__(self, directory: Path, device: str = "auto", threads: int = 4) -> None:
        import listen
        self.model = listen.load_model_dir(directory, device, threads)
        self.device = "cuda" if getattr(self.model.model, "device", "cpu") == "cuda" else "cpu/int8"
        self.lock = threading.Lock()

    def _run(self, pcm: bytes, beam: int, words: bool) -> tuple[str, list[dict]]:
        import numpy as np
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        segments, _info = self.model.transcribe(audio, language="en", beam_size=beam, word_timestamps=words,
                                                vad_filter=False)
        text, out = [], []
        for seg in segments:
            text.append(seg.text.strip())
            for w in (seg.words or []) if words else []:
                out.append({"w": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3),
                            "p": round(w.probability, 3)})
        return " ".join(t for t in text if t).strip(), out

    def transcribe(self, pcm: bytes) -> dict:
        with self.lock:
            text, words = self._run(pcm, 5, True)
        return {"text": text, "words": words, "seconds": round(len(pcm) / 2 / RATE_IN, 3)}

    def partial(self, pcm: bytes) -> str:
        if not self.lock.acquire(blocking=False):
            return ""
        try:
            return self._run(pcm, 1, False)[0]
        finally:
            self.lock.release()


class KokoroSynthesizer:
    """Kokoro int8 ONNX with onnxruntime pinned to `threads` threads (faster than default threads on a busy host)."""

    rate = 24000

    def __init__(self, model: Path, voices: Path, threads: int = 4) -> None:
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        session = ort.InferenceSession(str(model), sess_options=opts, providers=["CPUExecutionProvider"])
        self.kokoro = Kokoro.from_session(session, str(voices))
        self.model = Path(model).name
        self.lock = threading.Lock()

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0, lang: str = "en-us"):
        with self.lock:
            samples, rate = self.kokoro.create(text, voice=voice, speed=speed, lang=lang)
        return samples, int(rate)


class SileroDetector:
    """faster-whisper's bundled Silero VAD (v6). Its __call__ scores each 512-sample window with the 64 samples
    before it as context, so the previous window is passed along and the last score taken."""

    HISTORY = 2

    def __init__(self) -> None:
        from faster_whisper.vad import get_vad_model
        self.model = get_vad_model()
        self.history: list[list[float]] = []

    def prob(self, frame: list[float]) -> float:
        import numpy as np
        self.history = (self.history + [frame])[-self.HISTORY:]
        audio = np.asarray([v for f in self.history for v in f], dtype=np.float32)
        out = self.model(audio, num_samples=WINDOW, context_size_samples=64)
        return float(np.asarray(out).reshape(-1)[-1])


class Engines:
    """The three engines of a service, loaded once."""

    def __init__(self, recognizer, synthesizer, new_detector: Callable[[], Any], fake: bool) -> None:
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.new_detector = new_detector
        self.fake = fake


def fake_engines(script: str | Path | None) -> Engines:
    return Engines(FakeRecognizer(script), FakeSynthesizer(), FakeDetector, fake=True)


def real_engines(files: dict, device: str, threads: int) -> Engines:
    recognizer = WhisperRecognizer(files["whisper"], device, threads)
    synthesizer = KokoroSynthesizer(files["kokoro"], files["voices"], threads)
    SileroDetector()                                 # loads the model now, not at the first /listen
    return Engines(recognizer, synthesizer, SileroDetector, fake=False)


def measure(engines: Engines, voice: dict, sentence: str = WARMUP_SENTENCE) -> dict:
    """Speak `sentence`, recognise it: {"tts", "stt"} real-time factors, the transcript, the WAV."""
    t0 = time.monotonic()
    samples, rate = engines.synthesizer.synth(sentence, voice["ttsVoice"], float(voice["ttsSpeed"]) / 100,
                                              voice["ttsLang"])
    tts_s = time.monotonic() - t0
    pcm = floats_to_pcm(samples)
    seconds = len(pcm) / 2 / rate
    pcm16 = resample(pcm, rate)
    t1 = time.monotonic()
    heard = engines.recognizer.transcribe(pcm16)
    stt_s = time.monotonic() - t1
    if engines.fake:                                  # the warm-up must not use up a scripted transcript
        engines.recognizer.used = max(0, engines.recognizer.used - 1)
    return {"tts": round(tts_s / seconds, 3) if seconds else None,
            "stt": round(stt_s / seconds, 3) if seconds else None,
            "text": heard["text"], "seconds": round(seconds, 3), "wav": wav_bytes(pcm, rate)}


# ===== listening

class Listener:
    """One /listen stream: 100 ms frames in, Silero on 512-sample windows, utterances cut by silence."""

    START_WINDOWS = 3                  # ~100 ms over the threshold starts an utterance
    END_SAMPLES = int(0.6 * RATE_IN)   # 600 ms under it ends one
    BARGE_SAMPLES = int(0.2 * RATE_IN) # 200 ms of speech while the helper speaks interrupts it
    PREROLL = 10                       # windows kept before the start (~320 ms)

    def __init__(self, detector, recognizer, max_seconds: float, record: Callable[[bytes], None] | None = None) -> None:
        self.detector = detector
        self.recognizer = recognizer
        self.max_samples = int(max_seconds * RATE_IN)
        self.record = record
        self.rest = b""
        self.preroll: list[bytes] = []
        self.utterance: list[bytes] = []
        self.samples = 0
        self.in_speech = False
        self.over = 0
        self.below = 0
        self.barge = 0
        self.barge_in = False
        self.partial_at = 0
        self.partial_text = ""
        self.partial_running = False
        self.used = time.monotonic()
        self.lock = threading.Lock()

    def _reset(self) -> None:
        self.utterance, self.samples, self.in_speech = [], 0, False
        self.over = self.below = self.barge = 0
        self.barge_in = False
        self.partial_at, self.partial_text = 0, ""

    def _end(self) -> dict:
        pcm = b"".join(self.utterance)
        self._reset()
        if self.record:
            self.record(pcm)
        heard = self.recognizer.transcribe(pcm)
        return {"state": "end", "text": heard["text"], "words": heard["words"], "seconds": heard.get("seconds")}

    def _partial(self) -> None:
        pcm = b"".join(self.utterance)
        self.partial_at = self.samples
        if getattr(self.recognizer, "sync_partial", False):
            self.partial_text = self.recognizer.partial(pcm) or self.partial_text
            return
        if self.partial_running:
            return
        self.partial_running = True

        def work() -> None:
            try:
                text = self.recognizer.partial(pcm)
                if text and self.in_speech:
                    self.partial_text = text
            except Exception as exc:  # noqa: BLE001 - a partial is a nicety
                log(f"partial transcription failed: {exc}")
            finally:
                self.partial_running = False
        threading.Thread(target=work, daemon=True).start()

    def feed(self, pcm: bytes, speaking: bool) -> dict:
        with self.lock:
            self.used = time.monotonic()
            data = self.rest + pcm
            whole = len(data) - len(data) % (WINDOW * 2)
            self.rest = data[whole:]
            threshold = 0.85 if speaking else 0.5
            top = 0.0
            ended: dict | None = None
            for i in range(0, whole, WINDOW * 2):
                window = data[i:i + WINDOW * 2]
                prob = float(self.detector.prob(pcm_to_floats(window)))
                top = max(top, prob)
                loud = prob >= threshold
                if not self.in_speech:
                    self.preroll = (self.preroll + [window])[-self.PREROLL:]
                    self.over = self.over + 1 if loud else 0
                    if loud and speaking:
                        self.barge += WINDOW
                    elif not loud:
                        self.barge = 0
                    if self.over >= self.START_WINDOWS:
                        self.in_speech = True
                        self.utterance = list(self.preroll)
                        self.samples = len(self.utterance) * WINDOW
                        self.preroll = []
                        self.below = 0
                    continue
                self.utterance.append(window)
                self.samples += WINDOW
                if loud:
                    self.below = 0
                    if speaking:
                        self.barge += WINDOW
                else:
                    self.below += WINDOW
                if speaking and self.barge >= self.BARGE_SAMPLES:
                    self.barge_in = True
                if self.below >= self.END_SAMPLES or self.samples >= self.max_samples:
                    barge = self.barge_in
                    cut = self.samples >= self.max_samples
                    ended = self._end()
                    ended["barge_in"] = barge
                    ended["cut"] = cut
            if ended is None and speaking and self.barge >= self.BARGE_SAMPLES:
                self.barge_in = True
            if ended is not None:
                ended["prob"] = round(top, 3)
                return ended
            out: dict = {"state": "speech" if self.in_speech else "silence", "prob": round(top, 3),
                         "barge_in": self.barge_in}
            if self.in_speech:
                if self.samples - self.partial_at >= RATE_IN:
                    self._partial()
                if self.partial_text:
                    out["partial"] = self.partial_text
            return out


# ===== HTTP

class ServiceServer(http.server.ThreadingHTTPServer):
    """The service: loopback only, one bearer token, engines loaded once."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, token: str, voice: dict, *, fake: bool = False, record_dir: Path | None = None,
                 stt_model: str = "base.en", tts_model: str = "", missing: list[str] | None = None) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.token = token
        self.voice = voice
        self.fake = fake
        self.record_dir = record_dir
        self.stt_model = stt_model
        self.tts_model = tts_model
        self.engines: Engines | None = None
        self.loading = True
        self.error = ""
        self.device = ""
        self.rtf: dict = {"stt": None, "tts": None}
        self.missing: list[str] = list(missing or [])
        self.listeners: dict[str, Listener] = {}
        self.speaking: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.recorded = 0

    def server_bind(self) -> None:
        # HTTPServer.server_bind asks getfqdn(), a name lookup: not needed on loopback
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    @property
    def max_utterance(self) -> int:
        return int(self.voice.get("maxUtteranceSeconds") or 30)

    def set_engines(self, engines: Engines) -> None:
        if engines.fake:
            measured = measure(engines, self.voice)
        else:
            measure(engines, self.voice)   # the first call pays for graph set-up: warm up, then measure
            measured = measure(engines, self.voice, MEASURE_TEXT)
        self.rtf = {"stt": measured["stt"], "tts": measured["tts"]}
        self.device = getattr(engines.recognizer, "device", "cpu")
        self.tts_model = getattr(engines.synthesizer, "model", self.tts_model)
        self.engines = engines
        self.loading = False

    def load(self, loader: Callable[[], Engines]) -> None:
        try:
            self.set_engines(loader())
        except Exception as exc:  # noqa: BLE001 - reported by /health, the service keeps answering
            self.error = f"{type(exc).__name__}: {exc}"
            self.loading = False
            log(f"loading the speech models failed: {self.error}")

    def record(self, pcm: bytes) -> None:
        if not self.record_dir:
            return
        with self.lock:
            self.record_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.recorded += 1
            n = self.recorded
            while (self.record_dir / f"{n}.wav").exists():
                n += 1
            self.recorded = n
            path = self.record_dir / f"{n}.wav"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(wav_bytes(pcm, RATE_IN))

    def listener(self, stream: str) -> Listener:
        now = time.monotonic()
        with self.lock:
            for key in [k for k, v in self.listeners.items() if now - v.used > STREAM_IDLE]:
                del self.listeners[key]
            found = self.listeners.get(stream)
            if found is None:
                assert self.engines is not None
                found = Listener(self.engines.new_detector(), self.engines.recognizer, self.max_utterance,
                                 self.record if self.record_dir else None)
                self.listeners[stream] = found
            return found

    def health(self) -> dict:
        out = {"ready": self.engines is not None and not self.error, "loading": self.loading,
               "device": self.device, "stt_model": self.stt_model, "tts_model": self.tts_model,
               "voice": self.voice.get("ttsVoice"), "rtf": dict(self.rtf), "missing": list(self.missing),
               "fake": self.fake}
        if self.error:
            out["error"] = self.error
        return out


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ServiceServer
    server_version = "al-voice"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        if os.environ.get("AL_VOICE_DEBUG"):
            log(format % args)

    # --- responses

    def _json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status >= 400:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    # --- checks

    def _allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if not host or not _loopback(name):
            self._json(403, {"error": "not a loopback Host"})
            return False
        auth = self.headers.get("Authorization") or ""
        given = auth[7:] if auth.startswith("Bearer ") else ""
        if not given or not hmac.compare_digest(given.encode(), self.server.token.encode()):
            self._json(401, {"error": "a bearer token is needed"})
            return False
        return True

    def _body(self, cap: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "bad Content-Length"})
            return None
        if self.headers.get("Transfer-Encoding"):
            self._json(411, {"error": "a Content-Length is needed"})
            return None
        if length > cap:
            self._json(413, {"error": f"body over {cap} bytes"})
            return None
        return self.rfile.read(length) if length > 0 else b""

    def _ready(self) -> bool:
        if self.server.engines is None:
            data = {"loading": self.server.loading}
            if self.server.error:
                data["error"] = self.server.error
            self._json(503, data)
            return False
        return True

    def _audio_cap(self) -> int:
        return self.server.max_utterance * 32000 + 44

    def _json_body(self) -> dict | None:
        body = self._body(SPEAK_BODY)
        if body is None:
            return None
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self._json(400, {"error": "a JSON object is needed"})
            return None
        return data

    # --- routes

    def do_GET(self) -> None:  # noqa: N802 - the base class's name
        if not self._allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self._json(200, self.server.health())
        elif path in ("/transcribe", "/listen", "/speak", "/cancel"):
            self._json(405, {"error": "use POST"})
        else:
            self._json(404, {"error": "no such path"})

    def do_POST(self) -> None:  # noqa: N802 - the base class's name
        if not self._allowed():
            return
        url = urllib.parse.urlsplit(self.path)
        routes = {"/transcribe": self._transcribe, "/listen": self._listen, "/speak": self._speak,
                  "/cancel": self._cancel}
        if url.path == "/health":
            self._json(405, {"error": "use GET"})
        elif url.path not in routes:
            self._json(404, {"error": "no such path"})
        else:
            routes[url.path](urllib.parse.parse_qs(url.query))

    def _transcribe(self, query: dict) -> None:
        body = self._body(self._audio_cap())
        if body is None or not self._ready():
            return
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind in ("audio/wav", "audio/x-wav", "audio/wave"):
            try:
                pcm, rate = read_wav(body)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            pcm = resample(pcm, rate)
        elif kind == "audio/l16":
            pcm = body
        else:
            self._json(415, {"error": "audio/wav or audio/L16;rate=16000"})
            return
        pcm = pcm[: self.server.max_utterance * RATE_IN * 2]
        self.server.record(pcm)
        assert self.server.engines is not None
        self._json(200, self.server.engines.recognizer.transcribe(pcm))

    def _listen(self, query: dict) -> None:
        body = self._body(self._audio_cap())
        if body is None or not self._ready():
            return
        stream = (query.get("stream") or [""])[0]
        if not STREAM_ID.match(stream):
            self._json(400, {"error": "stream=ID (letters, digits, _ . -; at most 64) is needed"})
            return
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind != "audio/l16":
            self._json(415, {"error": "audio/L16;rate=16000"})
            return
        speaking = (query.get("speaking") or ["0"])[0] == "1"
        self._json(200, self.server.listener(stream).feed(body, speaking))

    def _cancel(self, query: dict) -> None:
        data = self._json_body()
        if data is None:
            return
        event = self.server.speaking.get(str(data.get("id") or ""))
        if event is not None:
            event.set()
        self._json(200, {"cancelled": event is not None})

    def _speak(self, query: dict) -> None:
        data = self._json_body()
        if data is None or not self._ready():
            return
        sid, text = str(data.get("id") or ""), data.get("text")
        if not STREAM_ID.match(sid) or not isinstance(text, str):
            self._json(400, {"error": '{"id": ID, "text": TEXT} is needed'})
            return
        sentences = split_sentences(text)
        if not sentences:
            self._json(400, {"error": "nothing to say"})
            return
        voice = self.server.voice
        name = str(data.get("voice") or voice["ttsVoice"])
        try:
            speed = float(data["speed"]) if data.get("speed") is not None else float(voice["ttsSpeed"]) / 100
        except (TypeError, ValueError):
            speed = float(voice["ttsSpeed"]) / 100
        speed = min(2.0, max(0.5, speed))
        cancel = threading.Event()
        with self.server.lock:
            self.server.speaking[sid] = cancel
        ahead: queue.Queue = queue.Queue(maxsize=1)    # synthesis runs one sentence ahead of what was sent
        engines = self.server.engines
        assert engines is not None

        def produce() -> None:
            for seq, sentence in enumerate(sentences):
                if cancel.is_set():
                    break
                try:
                    samples, rate = engines.synthesizer.synth(sentence, name, speed, voice["ttsLang"])
                    item: Any = (seq, sentence, samples, rate)
                except Exception as exc:  # noqa: BLE001 - the stream ends with the error
                    item = (seq, sentence, exc, 0)
                while not cancel.is_set():
                    try:
                        ahead.put(item, timeout=0.05)
                        break
                    except queue.Full:
                        continue
                if isinstance(item[2], Exception):
                    break

        threading.Thread(target=produce, daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", FRAME_TYPE)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        started = None
        played = 0.0
        seq = 0
        try:
            while seq < len(sentences):
                item = None
                while not cancel.is_set():
                    if started is not None and time.monotonic() < started + played - LEAD:
                        cancel.wait(0.05)        # paced like playback, so a cancel stops what is not heard
                        continue
                    try:
                        item = ahead.get(timeout=0.05)
                        break
                    except queue.Empty:
                        continue
                if cancel.is_set() or item is None:
                    break
                seq, sentence, samples, rate = item
                if isinstance(samples, Exception):
                    self._chunk(encode_frame({"seq": seq, "error": str(samples), "last": True}))
                    seq = len(sentences)
                    break
                pcm = floats_to_pcm(samples)
                duration = len(pcm) / 2 / rate
                header = {"seq": seq, "text": sentence, "duration": round(duration, 3), "rate": rate,
                          "envelope": envelope(samples, rate), "last": seq == len(sentences) - 1}
                self._chunk(encode_frame(header, wav_bytes(pcm, rate)))
                started = time.monotonic() if started is None else started
                played += duration
                seq += 1
            if seq < len(sentences):
                self._chunk(encode_frame({"seq": seq, "cancelled": True, "last": True}))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            cancel.set()
            with self.server.lock:
                if self.server.speaking.get(sid) is cancel:
                    del self.server.speaking[sid]


# ===== serve / doctor

def serve(args: argparse.Namespace) -> int:
    token = os.environ.pop("AL_VOICE_TOKEN", "")
    if not token:
        print("voice serve: AL_VOICE_TOKEN is not set (the dashboard starts this service with one)", file=sys.stderr)
        return 2
    voice = layer_voice(args.layer)
    record_dir = Path(args.record_dir).resolve() if args.record_dir else None
    if args.fake:
        server = ServiceServer(args.port, token, voice, fake=True, record_dir=record_dir,
                               stt_model="fake", tts_model="fake")
        server.load(lambda: fake_engines(args.fake_script))
    else:
        install_network_guard()
        manifest = load_manifest()
        model = str(voice["sttModel"])
        try:
            paths = cache_paths(manifest, model)
        except NotRecorded as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
            print(f"voice serve: {exc}", file=sys.stderr)
            return 1
        missing = missing_files(verify(paths, checksum=False))
        if missing:
            sentence = f"voice is not set up: {setup_hint(args.layer)}"
            print(json.dumps({"error": sentence, "missing": missing}), flush=True)
            print(f"voice serve: {sentence}\n  missing: " + "\n  missing: ".join(missing), file=sys.stderr)
            return 1
        files = model_files(manifest, model)
        server = ServiceServer(args.port, token, voice, record_dir=record_dir, stt_model=model,
                               tts_model=files["kokoro"].name)

        def loader() -> Engines:
            bad = missing_files(verify(paths, checksum=True))
            if bad:
                server.missing = bad
                raise RuntimeError(f"voice is not set up: {setup_hint(args.layer)}")
            return real_engines(files, str(voice["sttDevice"]), args.threads)

        threading.Thread(target=server.load, args=(loader,), daemon=True).start()
    print(json.dumps({"port": server.server_address[1]}), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def helper_check(layer: str | None) -> dict:
    """Whether the helper's agent can run without tools: claude-code must offer `--tools`."""
    ir = layer_ir(layer)
    if not ir:
        return {"ok": True, "text": "helper: no layer given, not checked"}
    voice = layer_voice(layer)
    agents = ir.get("agents") or []
    agent = next((a for a in agents if a.get("id") == voice["helperAgent"]), None) if voice["helperAgent"] else \
        (agents[0] if agents else None)
    if agent is None:
        return {"ok": False, "text": "helper: the layer declares no agent for the helper"}
    kind = agent.get("kind")
    if kind == "mock":
        return {"ok": True, "text": f"helper: agent `{agent['id']}` (mock) answers from mock/helper.json"}
    if kind != "claude-code":
        return {"ok": True, "text": f"helper: agent `{agent['id']}` is {kind}, so the panel will speak and listen "
                                    "but cannot answer (set #:helper-agent to a claude-code or mock agent)"}
    claude = os.environ.get("AL_CLAUDE") or shutil.which("claude")
    if not claude:
        return {"ok": False, "text": "helper: Claude Code (claude) was not found, so the helper cannot answer"}
    try:
        cp = subprocess.run([claude, "--help"], capture_output=True, text=True, timeout=30)
        offered = "--tools" in (cp.stdout + cp.stderr)
    except (OSError, subprocess.TimeoutExpired):
        offered = False
    if not offered:
        return {"ok": False, "text": f"helper: {claude} does not offer --tools, so the helper cannot run without "
                                     "tools and stays off; update Claude Code"}
    return {"ok": True, "text": f"helper: agent `{agent['id']}` (claude-code) runs with every tool off (--tools \"\")"}


def doctor(layer: str | None, out=None, as_json: bool = False, stt_model: str | None = None,
           threads: int = 4) -> int:
    """Files, device, both engines on a fixed sentence, the helper's agent; 1 when something is missing."""
    out = out or sys.stdout
    say = (lambda line: None) if as_json else (lambda line: print(line, file=out, flush=True))  # noqa: E731
    report: dict = {"ok": False, "files": [], "setup": setup_hint(layer)}
    model = stt_model_of(layer, stt_model)
    try:
        paths = cache_paths(load_manifest(), model)
    except NotRecorded as exc:
        print(f"voice doctor: {exc}", file=sys.stderr)
        return 2
    rows = verify(paths)
    report["files"] = [{k: r[k] for k in ("name", "path", "bytes", "expected", "ok", "why")} for r in rows]
    for r in rows:
        state = "ok" if r["ok"] and r["bytes"] is not None else r["why"]
        say(f"  {r['name']:<40} {human_size(r['bytes']):>9}  {state}")
    missing = missing_files(rows)
    if missing:
        report["missing"] = missing
        say(f"voice is not set up: {setup_hint(layer)}")
        if as_json:
            print(json.dumps(report, indent=2), file=out)
        return 1
    install_network_guard()
    import listen
    voice = layer_voice(layer)
    report["device"] = "cuda" if (voice["sttDevice"] == "cuda" or (voice["sttDevice"] == "auto" and listen.cuda_usable())) \
        else "cpu/int8"
    say(f"device: {report['device']}")
    files = model_files(load_manifest(), model)
    t0 = time.monotonic()
    recognizer = WhisperRecognizer(files["whisper"], str(voice["sttDevice"]), threads)
    t1 = time.monotonic()
    synthesizer = KokoroSynthesizer(files["kokoro"], files["voices"], threads)
    t2 = time.monotonic()
    SileroDetector()
    t3 = time.monotonic()
    report["load_seconds"] = {"stt": round(t1 - t0, 2), "tts": round(t2 - t1, 2), "vad": round(t3 - t2, 2)}
    say(f"loaded: recognition ({model}) {t1 - t0:.1f} s, speech ({files['kokoro'].name}, {threads} threads) "
        f"{t2 - t1:.1f} s, voice activity (Silero) {t3 - t2:.2f} s")
    engines = Engines(recognizer, synthesizer, SileroDetector, fake=False)
    measure(engines, voice)                # warm up first: the first call pays for graph set-up, not speech
    measured = measure(engines, voice, DOCTOR_SENTENCE)
    report["rtf"] = {"tts": measured["tts"], "stt": measured["stt"]}
    report["sentence"], report["transcript"] = DOCTOR_SENTENCE, measured["text"]
    say(f"speech: {measured['seconds']:.2f} s of audio, real-time factor {measured['tts']}")
    say(f"recognition: real-time factor {measured['stt']}, heard: {measured['text']}")
    helper = helper_check(layer)
    report["helper"] = helper
    say(helper["text"])
    report["ok"] = helper["ok"]
    say("doctor: " + ("ok" if helper["ok"] else "the helper cannot answer"))
    if as_json:
        print(json.dumps(report, indent=2), file=out)
    return 0 if helper["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the dashboard's local voice service (recognition, speech, VAD)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="serve recognition and speech on loopback (the dashboard starts it)")
    p.add_argument("--layer")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--fake", action="store_true", help="standard-library engines (tests)")
    p.add_argument("--fake-script", help='{"transcripts": [...]} for the fake recogniser')
    p.add_argument("--record-dir", help="write each utterance as <dir>/<n>.wav (off: no audio touches the disk)")
    for name, text in (("setup", "download and verify the model files (the only command that uses the network)"),
                       ("doctor", "check the files, load the engines, measure their speed")):
        p = sub.add_parser(name, help=text)
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--layer")
        group.add_argument("--stt-model")
        if name == "doctor":
            p.add_argument("--json", action="store_true")
            p.add_argument("--threads", type=int, default=4)
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return serve(args)
    if args.cmd == "setup":
        return setup(args.layer, sys.stdout, args.stt_model)
    return doctor(args.layer, sys.stdout, args.json, args.stt_model, args.threads)


if __name__ == "__main__":
    sys.exit(main())
