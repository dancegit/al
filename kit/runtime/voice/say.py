#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#   "kokoro-onnx>=0.4,<0.7",
#   "soundfile>=0.12",
# ]
# ///
"""Speak text with Kokoro TTS (local, ONNX, English).

    say.py "text" [--voice af_heart] [--lang en-us] [--speed 1.0]
           [--out file.wav] [--no-play] [--layer DIR]

Text is read from stdin when no argument is given. Model files are downloaded
once into ~/.cache/al/kokoro/. Defaults come from the layer's `voice`
declaration when --layer is given. Diagnostics go to stderr only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
CACHE_DIR = Path(os.environ.get("AL_KOKORO_CACHE", Path.home() / ".cache" / "al" / "kokoro"))
PLAYERS = (["pw-play"], ["paplay"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"])


def log(msg: str) -> None:
    print(f"say: {msg}", file=sys.stderr, flush=True)


def download(url: str, dest: Path, attempts: int = 20) -> None:
    """Resumable download: bytes accumulate in <dest>.part (HTTP Range), renamed when complete."""
    part = dest.with_name(dest.name + ".part")
    total = None
    for attempt in range(1, attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if have and resp.status != 206:  # server ignored Range: start over
                    have = 0
                    part.unlink(missing_ok=True)
                rng = resp.headers.get("Content-Range", "")
                if "/" in rng:
                    total = int(rng.rsplit("/", 1)[1])
                elif resp.headers.get("Content-Length"):
                    total = have + int(resp.headers["Content-Length"])
                with open(part, "ab") as fh:
                    shutil.copyfileobj(resp, fh, length=1 << 20)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and total is not None and have == total:
                pass  # already complete
            elif exc.code == 416:
                part.unlink(missing_ok=True)
                continue
            else:
                raise
        except (OSError, TimeoutError) as exc:
            log(f"download interrupted at {part.stat().st_size if part.exists() else 0} bytes "
                f"({exc}); retry {attempt}/{attempts}")
            time.sleep(min(30, 2 * attempt))
            continue
        size = part.stat().st_size if part.exists() else 0
        if total is None or size >= total:
            os.chmod(part, 0o644)
            os.replace(part, dest)
            return
    raise SystemExit(f"say: could not download {url} after {attempts} attempts")


def ensure_models(cache: Path = CACHE_DIR) -> tuple[Path, Path]:
    cache.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in MODEL_FILES:
        dest = cache / name
        if not dest.exists() or dest.stat().st_size == 0:
            url = f"{RELEASE}/{name}"
            log(f"downloading {url} into {cache}")
            download(url, dest)
        paths.append(dest)
    return paths[0], paths[1]


def layer_defaults(layer: str | None) -> dict:
    if not layer:
        return {}
    try:
        voice = json.loads((Path(layer) / "agentic-layer.json").read_text()).get("voice") or {}
    except (OSError, ValueError):
        return {}
    out = {}
    if voice.get("ttsVoice"):
        out["voice"] = voice["ttsVoice"]
    if voice.get("ttsLang"):
        out["lang"] = voice["ttsLang"]
    if voice.get("ttsSpeed"):
        out["speed"] = float(voice["ttsSpeed"]) / 100.0
    return out


def play(path: Path) -> bool:
    for cmd in PLAYERS:
        if shutil.which(cmd[0]):
            return subprocess.run([*cmd, str(path)], check=False).returncode == 0
    log("no audio player found (pw-play, paplay, aplay, ffplay)")
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kokoro text-to-speech")
    ap.add_argument("text", nargs="?")
    ap.add_argument("--voice")
    ap.add_argument("--lang")
    ap.add_argument("--speed", type=float)
    ap.add_argument("--out")
    ap.add_argument("--no-play", action="store_true")
    ap.add_argument("--layer")
    args = ap.parse_args(argv)

    defaults = {"voice": "af_heart", "lang": "en-us", "speed": 1.0, **layer_defaults(args.layer)}
    voice = args.voice or defaults["voice"]
    lang = args.lang or defaults["lang"]
    speed = args.speed if args.speed is not None else defaults["speed"]

    text = args.text if args.text is not None else sys.stdin.read()
    text = text.strip()
    if not text:
        log("nothing to say")
        return 2

    import soundfile as sf
    from kokoro_onnx import Kokoro

    model, voices = ensure_models()
    samples, rate = Kokoro(str(model), str(voices)).create(text, voice=voice, speed=speed, lang=lang)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), samples, rate)
        if not args.no_play:
            play(out)
    elif not args.no_play:
        with tempfile.TemporaryDirectory(prefix="al-say-") as tmp:
            out = Path(tmp) / "say.wav"
            sf.write(str(out), samples, rate)
            play(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
