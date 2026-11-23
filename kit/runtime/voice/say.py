#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#   # the same Kokoro packages as serve.py, so uv keeps one set of wheels
#   "kokoro-onnx>=0.6,<0.7",
#   "onnxruntime>=1.20",
#   "numpy",
# ]
# ///
"""Speak text with Kokoro TTS (local, int8 ONNX, English), falling back to espeak-ng.

    say.py "text" [--voice af_heart] [--lang en-us] [--speed 1.0]
           [--out file.wav] [--no-play] [--no-fetch] [--layer DIR]

Text is read from stdin when no argument is given. Defaults come from the layer's `voice` declaration when
--layer is given. Diagnostics go to stderr only.

$AL_VOICE_TTS chooses the engine: kokoro (the default), espeak or off. Kokoro's files are the ones models.json
records, in the cache the voice service uses ($AL_KOKORO_CACHE, else $XDG_CACHE_HOME/al/kokoro, default
~/.cache/al/kokoro). On first use they are fetched (about 120 MB, every sha256 checked, under a lock so
concurrent callers fetch once); --no-fetch (or AL_VOICE_NO_FETCH=1) never fetches, which is how a run speaks
its summary. Without Kokoro's files, or when Kokoro fails to load, espeak-ng speaks when it is on PATH, else
nothing is spoken.
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

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import serve  # noqa: E402 - beside this file; standard library at module level, nothing loaded or fetched

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


def synthesize(text: str, voice: str, lang: str, speed: float, fetch: bool,
               layer: str | None) -> tuple[list[float], int, str, str]:
    """(samples, rate, engine, why): Kokoro (fetched first when allowed and missing), else espeak-ng, else
    ([], 0, "off", why)."""
    setting = serve.tts_setting()
    manifest = serve.load_manifest()
    rows = serve.kokoro_paths(manifest)
    missing = serve.missing_files(serve.verify(rows, checksum=False))
    if setting == "kokoro" and missing and fetch:
        log(f"downloading the Kokoro model (about 120 MB) into {serve.kokoro_cache()}")
        try:
            serve.fetch_kokoro(manifest, download)
        except serve.FetchError as exc:
            log(f"could not fetch the Kokoro model: {exc}; speaking with espeak-ng instead")
        missing = serve.missing_files(serve.verify(rows, checksum=False))
    prog = serve.espeak_program()
    engine, why = serve.choose_tts(setting, missing, prog, layer)
    kokoro = manifest["kokoro"]
    voices = next(n for n in kokoro["files"] if n.startswith("voices"))
    model_path, voices_path = serve.kokoro_cache() / kokoro["model"], serve.kokoro_cache() / voices
    synthesizer, engine, why = serve.pick_synthesizer(
        engine, why, lambda: serve.KokoroSynthesizer(model_path, voices_path, threads=4),
        (lambda: serve.EspeakSynthesizer(prog)) if prog else None)
    if synthesizer is None:
        return [], 0, "off", why
    samples, rate = synthesizer.synth(text, voice, speed, lang)
    return samples, int(rate), engine, why


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kokoro text-to-speech, with espeak-ng as the fallback")
    ap.add_argument("text", nargs="?")
    ap.add_argument("--voice")
    ap.add_argument("--lang")
    ap.add_argument("--speed", type=float)
    ap.add_argument("--out")
    ap.add_argument("--no-play", action="store_true")
    ap.add_argument("--no-fetch", action="store_true", help="never download Kokoro's model (run summaries)")
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

    fetch = not (args.no_fetch or os.environ.get("AL_VOICE_NO_FETCH") == "1")
    try:
        samples, rate, engine, why = synthesize(text, voice, lang, speed, fetch, args.layer)
    except ValueError as exc:                        # an unknown AL_VOICE_TTS
        log(str(exc))
        return 2
    if engine == "off":
        log(f"nothing spoken: {why}")
        return 0
    log(f"spoken by {engine}" + (f" ({why})" if why else ""))
    wav = serve.wav_bytes(serve.floats_to_pcm(samples), rate)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(wav)
        if not args.no_play:
            play(out)
    elif not args.no_play:
        with tempfile.TemporaryDirectory(prefix="al-say-") as tmp:
            out = Path(tmp) / "say.wav"
            out.write_bytes(wav)
            play(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
