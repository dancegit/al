"""Inputs for the voice helper's tests, standard library only.

  speech_like_wav(path, pattern)   a 16 kHz mono WAV of tone bursts and silences ("s0.3 t1.2 s0.6": seconds of
                                   silence / tone; `T` is a loud tone, `q` a quiet one), what a fake microphone
                                   or the fake recogniser's voice-activity detector hears as speech and pauses
  fake_script(path, transcripts)   the fake recogniser's script {"transcripts": [...]}: one text per utterance
  helper_mock(layer, rules, default)  the mock helper agent's mock/helper.json (rules: (regex, reply object))
  PROPOSE_SEARCH_BOX ...            replies the tests' mock helper gives
"""
from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

RATE = 16000
LEVELS = {"t": 0.5, "T": 0.8, "q": 0.05}


def tone(seconds: float, amplitude: float = 0.5, freq: float = 220.0, rate: int = RATE) -> bytes:
    n = int(seconds * rate)
    return b"".join(struct.pack("<h", int(32767 * amplitude * math.sin(2 * math.pi * freq * i / rate)))
                    for i in range(n))


def silence(seconds: float, rate: int = RATE) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def pcm_of(pattern: str, rate: int = RATE) -> bytes:
    """"s0.5 t1 s0.7" → 16-bit little-endian mono PCM."""
    out = b""
    for part in pattern.split():
        kind, seconds = part[0], float(part[1:])
        out += silence(seconds, rate) if kind == "s" else tone(seconds, LEVELS[kind], rate=rate)
    return out


def wav_of(pcm: bytes, rate: int = RATE) -> bytes:
    import io
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def speech_like_wav(path: Path, pattern: str = "s0.3 t1.2 s0.8") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_of(pcm_of(pattern)))
    return path


def fake_script(path: Path, transcripts: list[str]) -> Path:
    """The fake recogniser re-reads this at every utterance, so a test may rewrite it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"transcripts": list(transcripts)}))
    return path


def helper_mock(layer: Path, rules: list[tuple[str, Any]], default: Any = None) -> Path:
    """mock/helper.json of `layer`: the first rule whose regex matches the person's latest text wins."""
    path = Path(layer) / "mock" / "helper.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"rules": [{"when": when, "reply": reply} for when, reply in rules]}
    data["default"] = default if default is not None else {"say": "I can tell you about runs, or prepare one."}
    path.write_text(json.dumps(data, indent=1))
    return path


PROPOSE_SEARCH_BOX = {
    "say": "I can start plan-build for that.", "tool": "propose",
    "args": {"action": {"kind": "start", "target": {"kind": "workflow", "name": "plan-build"},
                        "prompt": "Add a search box to the notes page", "class": "feature"}}}
LIST_RUNS = {"say": "Let me look at the runs.", "tool": "list_runs", "args": {"limit": 3}}
SHELL = {"say": "Running that for you.", "tool": "shell", "args": {"cmd": "rm -rf /"}}
SCOUT = {"say": "I can scout it.", "tool": "propose", "args": {"action": {"kind": "job", "action": "scout"}}}

LONG_STORY = {"say": "The first run planned the notes page and it passed after a while, with every check green. "
                     "The second run built the search box, and a person still has to review it before it merges. "
                     "The third run is waiting for a slot, because two other runs hold the agent slots right now."}

DEFAULT_RULES: list[tuple[str, Any]] = [
    ("search box", PROPOSE_SEARCH_BOX),
    ("what is running|which runs", LIST_RUNS),
    ("shell|rm -rf", SHELL),
    ("scout", SCOUT),
    ("long story", LONG_STORY),
]


def state_json(layer: Path, run_id: str, **fields: Any) -> Path:
    """agents/<run>/state.json written by the layer's own state module (its ADWState and fields)."""
    import subprocess
    import sys
    code = ("import json, sys; sys.path.insert(0, sys.argv[1] + '/adws'); from adw_modules import state; "
            "s = state.ADWState(**json.loads(sys.argv[2])); s.save(sys.argv[1])")
    subprocess.run([sys.executable, "-c", code, str(layer), json.dumps({"adw_id": run_id, **fields})],
                   check=True, capture_output=True, timeout=30)
    return Path(layer) / "agents" / run_id / "state.json"


class VoiceClient:
    """One browser of the voice panel against a running dashboard (tests/conftest.py Running): its
    al_voice_session cookie and the headers the page sends."""

    def __init__(self, running: Any, layer: str = "talk", token: str | None = None) -> None:
        self.running, self.layer, self.token = running, layer, token
        self.cookie = ""

    def headers(self, content_type: str = "application/json", **changes: str | None) -> dict[str, str]:
        out = {"Content-Type": content_type, "Origin": f"http://127.0.0.1:{self.running.port}",
               "Host": f"127.0.0.1:{self.running.port}", "X-AL-Dashboard": "1"}
        if self.cookie:
            out["Cookie"] = f"al_voice_session={self.cookie}"
        if self.token:
            out["Authorization"] = f"Bearer {self.token}"
        for name, value in changes.items():
            name = name.replace("_", "-")
            if value is None:
                out.pop(name, None)
            else:
                out[name] = value
        return out

    def raw(self, path: str, body: bytes | None = None, content_type: str = "application/json",
            method: str = "POST", **changes: str | None) -> tuple[int, dict[str, str], bytes]:
        return self.running.get(path, self.headers(content_type, **changes), method=method, body=body)

    def post(self, path: str, body: Any = None, content_type: str = "application/json",
             **changes: str | None) -> tuple[int, dict]:
        if body is None:
            body = {"layer": self.layer}
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        status, _headers, data = self.raw(path, raw, content_type, **changes)
        try:
            return status, json.loads(data) if data else {}
        except ValueError:
            return status, {"raw": data[:200]}

    def get(self, path: str) -> tuple[int, dict]:
        status, _headers, data = self.raw(path, None, method="GET", Content_Type=None, Origin=None,
                                          X_AL_Dashboard=None)
        return status, json.loads(data) if data else {}

    def session(self, repo: str = "") -> dict:
        status, headers, data = self.raw("/api/voice/session", json.dumps(
            {"layer": self.layer, **({"repo": repo} if repo else {})}).encode())
        assert status == 200, (status, data)
        cookie = headers.get("set-cookie", "")
        if cookie:
            self.cookie = cookie.split(";", 1)[0].split("=", 1)[1]
        return json.loads(data)

    def turn(self, text: str, via: str = "text") -> dict:
        status, body = self.post("/api/voice/turn", {"layer": self.layer, "text": text, "via": via})
        assert status == 200, (status, body)
        return body

    def utterance(self, wav: bytes) -> tuple[int, dict]:
        return self.post(f"/api/voice/utterance?layer={self.layer}", wav, "audio/wav")

    def listen(self, pcm: bytes, stream: str = "s1", speaking: int = 0) -> tuple[int, dict]:
        return self.post(f"/api/voice/listen?layer={self.layer}&stream={stream}&speaking={speaking}", pcm,
                         "audio/L16;rate=16000")

    def approve(self, card: dict, digest: str | None = None) -> tuple[int, dict]:
        return self.post("/api/voice/approve", {"layer": self.layer, "proposal": card["id"],
                                                "digest": card.get("digest", "") if digest is None else digest})

    def proposal(self, text: str = "start a plan for adding a search box") -> dict:
        """A card the mock helper proposes, confirmed by a typed yes → the confirmed card."""
        card = self.turn(text)["card"]
        assert card and card["state"] == "proposed", card
        confirmed = self.turn("yes")["card"]
        assert confirmed["state"] == "confirmed" and confirmed["digest"], confirmed
        return confirmed
