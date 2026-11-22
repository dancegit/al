"""The voices of the demo video's narration: which backend reads each caption's spoken line, the request to
the ElevenLabs service, the Kokoro voice of this machine, the cache of what they made and fitting each voiced
line into its caption's time on screen.

Three backends speak: `elevenlabs` (the service; ELEVENLABS_API_KEY), `kokoro` (kit/runtime/voice/say.py,
run on this machine by its command line only) and `local` (espeak-ng); `auto` picks the first of them
that can run, in that order, and Kokoro only when its model files are already in the user's cache: say.py
downloads them once into that cache (outside the repository) when it is chosen by name, never by `auto`.

Standard library only. This is the only file of tools/demovideo that names an outside address, and only
the spoken lines are sent there. The key (ELEVENLABS_API_KEY) goes only in the request's `xi-api-key`
header: it is never printed, never part of an error message (scrub), never in a file name or a file of the
cache, and never in the argv of a command.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

API_ROOT = "https://api.elevenlabs.io"
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
MODEL = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"
KEY_VAR = "ELEVENLABS_API_KEY"
VOICE_VAR = "ELEVENLABS_VOICE_ID"
BACKENDS = ("auto", "elevenlabs", "kokoro", "local", "none")
MAX_TEMPO = 1.35                 # a voiced line is played at most this much faster to fit its caption
ROOT = Path(__file__).resolve().parents[2]
SAY = ROOT / "kit" / "runtime" / "voice" / "say.py"
KOKORO_RUN: tuple[str, ...] = ("uv", "run", "--quiet")     # what runs SAY (uv brings kokoro-onnx from its inline metadata)
KOKORO_VOICE_VAR = "AL_DEMO_KOKORO_VOICE"
KOKORO_DEFAULT_VOICE = "af_heart"
KOKORO_SPEED = 1.0
KOKORO_MAX_TEMPO = 1.12          # a Kokoro line is played at most this much faster
KOKORO_TIMEOUT = 600             # seconds for one line (the first one may download the model on the host)
TIMEOUT = 60                     # seconds for one answer of the service
SILENCE = ("silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
           "silenceremove=start_periods=1:start_threshold=-50dB,areverse")
_SCRUBBED = f"[{KEY_VAR}]"


class TtsError(RuntimeError):
    """A voice that cannot be made; the message never holds the key."""


def scrub(text: str, key: str | None) -> str:
    """`text` with every occurrence of the key replaced by [ELEVENLABS_API_KEY]."""
    return text.replace(key, _SCRUBBED) if key else text


def kokoro_caches(env: Mapping[str, str]) -> list[Path]:
    """Where say.py keeps Kokoro's model files: AL_KOKORO_CACHE when set; else $XDG_CACHE_HOME/al/kokoro
    (when it is an absolute path) and ~/.cache/al/kokoro."""
    explicit = env.get("AL_KOKORO_CACHE", "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    out = []
    xdg = env.get("XDG_CACHE_HOME", "").strip()
    if xdg and os.path.isabs(xdg):
        out.append(Path(xdg) / "al" / "kokoro")
    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    out.append(home / ".cache" / "al" / "kokoro")
    return out


def kokoro_runnable(env: Mapping[str, str]) -> bool:
    """say.py is there and its runner (uv) is on PATH."""
    return SAY.is_file() and bool(shutil.which(KOKORO_RUN[0]))


def _non_empty(folder: Path, pattern: str) -> bool:
    try:
        return any(p.is_file() and p.stat().st_size > 0 for p in folder.glob(pattern))
    except OSError:
        return False


def kokoro_ready(env: Mapping[str, str]) -> bool:
    """Kokoro can run without downloading anything: say.py is runnable and a cache holds a model and its voices."""
    return kokoro_runnable(env) and any(_non_empty(c, "*.onnx") and _non_empty(c, "voices*.bin") for c in kokoro_caches(env))


def choose(requested: str, env: Mapping[str, str], espeak: bool, kokoro: bool = False) -> str:
    """The backend that reads the spoken lines: elevenlabs, kokoro, local (espeak-ng) or none. `kokoro` is
    kokoro_ready: auto takes Kokoro only when its model files are already cached."""
    has_key = bool(env.get(KEY_VAR, "").strip())
    if requested == "auto":
        return "elevenlabs" if has_key else "kokoro" if kokoro else "local" if espeak else "none"
    if requested == "elevenlabs" and not has_key:
        raise TtsError(f"--voice elevenlabs needs {KEY_VAR} in the environment")
    if requested == "kokoro" and not kokoro_runnable(env):
        raise TtsError("--voice kokoro needs uv on PATH and kit/runtime/voice/say.py")
    if requested == "local" and not espeak:
        raise TtsError("--voice local needs espeak-ng on PATH")
    if requested not in BACKENDS:
        raise TtsError(f"no voice {requested!r}; one of {', '.join(BACKENDS)}")
    return requested


@dataclass(frozen=True)
class Settings:
    voice_id: str
    model: str
    output_format: str
    key: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        return cls(voice_id=env.get(VOICE_VAR, "").strip() or DEFAULT_VOICE_ID, model=MODEL, output_format=OUTPUT_FORMAT,
                   key=env.get(KEY_VAR, "").strip())


def cache_name(text: str, settings: Settings) -> str:
    """The cache file of a line: the text, voice, model and format hashed; the key is not part of it."""
    digest = hashlib.sha256(json.dumps([text, settings.voice_id, settings.model, settings.output_format]).encode()).hexdigest()
    return digest + ".mp3"


@dataclass(frozen=True)
class KokoroSettings:
    voice: str
    speed: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "KokoroSettings":
        return cls(voice=env.get(KOKORO_VOICE_VAR, "").strip() or KOKORO_DEFAULT_VOICE, speed=KOKORO_SPEED)


def kokoro_cache_name(text: str, settings: KokoroSettings) -> str:
    """The cache file of a Kokoro line: the text, voice and speed hashed."""
    return hashlib.sha256(json.dumps(["kokoro", text, settings.voice, settings.speed]).encode()).hexdigest() + ".wav"


def kokoro_line(text: str, cache: Path, settings: KokoroSettings, env: Mapping[str, str] | None = None) -> tuple[Path, bool]:
    """The WAV of a line from the cache, or made by say.py into the cache; True when it was cached."""
    name = kokoro_cache_name(text, settings)
    path = cache / name
    if path.is_file() and path.stat().st_size > 0:
        return path, True
    cache.mkdir(parents=True, exist_ok=True)
    part = cache / (name[:-4] + ".part.wav")           # say.py picks the format from the suffix
    argv = [*KOKORO_RUN, str(SAY), "--voice", settings.voice, "--speed", f"{settings.speed:g}", "--out", str(part), "--no-play"]
    run_env = {k: v for k, v in (env if env is not None else os.environ).items() if k != "AL_APP_ROOT"}
    try:
        cp = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=KOKORO_TIMEOUT, env=run_env)
    except (OSError, subprocess.SubprocessError) as error:
        part.unlink(missing_ok=True)
        raise TtsError(f"Kokoro (say.py) could not run: {error}") from None
    if cp.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise TtsError(f"Kokoro (say.py) exited {cp.returncode} for voice {settings.voice}: {cp.stderr[-300:]}")
    os.replace(part, path)
    return path, False


def request_speech(text: str, settings: Settings, api_root: str | None = None, opener=urllib.request.urlopen) -> bytes:
    """The service's audio for `text`. Only the text and the model are in the body; the key is in a header."""
    root = api_root if api_root is not None else API_ROOT
    request = urllib.request.Request(
        f"{root}/v1/text-to-speech/{urllib.parse.quote(settings.voice_id)}?output_format={settings.output_format}",
        data=json.dumps({"text": text, "model_id": settings.model}).encode(), method="POST",
        headers={"xi-api-key": settings.key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    try:
        with opener(request, timeout=TIMEOUT) as answer:
            body = answer.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace") if error.fp is not None else ""
        raise TtsError(scrub(f"ElevenLabs answered {error.code} {error.reason} for voice {settings.voice_id}: {detail[:200]}",
                             settings.key)) from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise TtsError(scrub(f"ElevenLabs could not be reached: {reason}", settings.key)) from None
    if not body:
        raise TtsError(f"ElevenLabs answered with no audio for voice {settings.voice_id}")
    return body


def fetch_line(text: str, cache: Path, settings: Settings, api_root: str | None = None) -> tuple[Path, bool]:
    """The audio of a line from the cache, or from the service into the cache; True when it was cached."""
    path = cache / cache_name(text, settings)
    if path.is_file() and path.stat().st_size > 0:
        return path, True
    audio = request_speech(text, settings, api_root)
    cache.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    part.write_bytes(audio)
    os.replace(part, path)
    return path, False


def _seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


def _ffmpeg(audio: Path, target: Path, filters: str) -> None:
    if not shutil.which("ffmpeg"):
        raise TtsError("ffmpeg is not on PATH")
    cp = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(audio), "-af", filters, "-ar", "44100", "-ac", "1", str(target)],
                        capture_output=True, text=True)
    if cp.returncode != 0:
        raise TtsError(f"ffmpeg could not convert {audio.name}: {cp.stderr[:300]}")


def fit_line(audio: Path, target: Path, slot: float, max_tempo: float = MAX_TEMPO) -> float:
    """`audio` as 44.1 kHz mono WAV in `target`, its silence trimmed and, when it is longer than `slot`,
    played faster (at most `max_tempo`). Returns the tempo used."""
    _ffmpeg(audio, target, SILENCE)
    seconds = _seconds(target)
    if seconds <= slot:
        return 1.0
    tempo = seconds / slot * 1.01
    if tempo > max_tempo:
        raise TtsError(f"{seconds:.1f} s of speech for {slot:.1f} s on screen (over ×{max_tempo:g} faster)")
    _ffmpeg(audio, target, f"{SILENCE},atempo={tempo:.4f}")
    return tempo
