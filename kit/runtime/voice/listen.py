#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#   "faster-whisper>=1.1,<2",
#   # faster-whisper 1.2.1 calls av.open(..., metadata_errors=...), which PyAV 19 rejects
#   # (TypeError); 15.1 is verified to work.
#   "av>=11,<16",
# ]
# ///
"""Record from the microphone (or read a file) and print the English transcript.

    listen.py [--file audio.wav] [--seconds N] [--model base.en]
              [--device auto|cpu|cuda] [--layer DIR]

Only the transcript goes to stdout; everything else goes to stderr.
Recording uses the first available of pw-record, arecord, ffmpeg -f pulse
(16 kHz mono wav in a temp file). `auto` uses CUDA when the CUDA runtime
libraries (cuBLAS 12 + cuDNN 9) can be loaded, otherwise CPU with int8.
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def log(msg: str) -> None:
    print(f"listen: {msg}", file=sys.stderr, flush=True)


def layer_defaults(layer: str | None) -> dict:
    if not layer:
        return {}
    try:
        voice = json.loads((Path(layer) / "agentic-layer.json").read_text()).get("voice") or {}
    except (OSError, ValueError):
        return {}
    out = {}
    if voice.get("sttModel"):
        out["model"] = voice["sttModel"]
    if voice.get("sttDevice"):
        out["device"] = voice["sttDevice"]
    if voice.get("recordSeconds"):
        out["seconds"] = int(voice["recordSeconds"])
    return out


def record_command(out: Path, seconds: int) -> tuple[list[str], bool] | None:
    """Return (argv, needs_kill_after_seconds) for the first available recorder."""
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", str(out)], True
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", str(seconds), str(out)], False
    if shutil.which("ffmpeg"):
        return ["ffmpeg", "-loglevel", "error", "-y", "-f", "pulse", "-i", "default",
                "-ac", "1", "-ar", "16000", "-t", str(seconds), str(out)], False
    return None


def record(out: Path, seconds: int) -> None:
    found = record_command(out, seconds)
    if not found:
        raise SystemExit("listen: no recorder found (pw-record, arecord, ffmpeg)")
    argv, kill_after = found
    log(f"recording {seconds}s with {argv[0]} ...")
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL)
    try:
        proc.wait(timeout=seconds if kill_after else seconds + 10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=5)
    log("recording done")


def _preload_cuda_libs() -> bool:
    """Make cuBLAS/cuDNN loadable. Uses pip `nvidia-*` wheels when installed."""
    try:
        import nvidia  # type: ignore[import-not-found]

        for base in nvidia.__path__:
            for lib in sorted(glob.glob(os.path.join(base, "*", "lib", "lib*.so*"))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass
    except ImportError:
        pass
    try:
        ctypes.CDLL("libcublas.so.12")
        ctypes.CDLL("libcudnn_ops.so.9")
        return True
    except OSError:
        return False


def cuda_usable() -> bool:
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() < 1:
            return False
    except Exception:
        return False
    return _preload_cuda_libs()


def load_model(name: str, device: str):
    from faster_whisper import WhisperModel

    if device == "cuda":
        _preload_cuda_libs()
        return WhisperModel(name, device="cuda", compute_type="float16")
    if device == "cpu":
        return WhisperModel(name, device="cpu", compute_type="int8")
    if cuda_usable():
        try:
            return WhisperModel(name, device="cuda", compute_type="float16")
        except Exception as exc:  # noqa: BLE001 - fall back on any CUDA failure
            log(f"cuda unavailable ({exc}); falling back to cpu/int8")
    else:
        log("cuda runtime (cuBLAS 12 / cuDNN 9) not loadable; using cpu/int8")
    return WhisperModel(name, device="cpu", compute_type="int8")


def load_model_dir(path: str | Path, device: str = "auto", threads: int = 4):
    """A model from a local, already verified directory (the voice service's cache): never downloads.
    `device` auto|cpu|cuda as in load_model; on the CPU int8 with `threads` threads."""
    from faster_whisper import WhisperModel

    where = str(path)
    use_cuda = device == "cuda" or (device == "auto" and cuda_usable())
    if use_cuda:
        _preload_cuda_libs()
        try:
            return WhisperModel(where, device="cuda", compute_type="float16", local_files_only=True)
        except Exception as exc:  # noqa: BLE001 - fall back on any CUDA failure
            if device == "cuda":
                raise
            log(f"cuda unavailable ({exc}); falling back to cpu/int8")
    return WhisperModel(where, device="cpu", compute_type="int8", cpu_threads=threads, local_files_only=True)


def transcribe(path: Path, model_name: str, device: str) -> str:
    model = load_model(model_name, device)
    segments, _info = model.transcribe(str(path), language="en", beam_size=5, vad_filter=False)
    return " ".join(s.text.strip() for s in segments).strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="faster-whisper speech recognition")
    ap.add_argument("--file")
    ap.add_argument("--seconds", type=int)
    ap.add_argument("--model")
    ap.add_argument("--device", choices=("auto", "cpu", "cuda"))
    ap.add_argument("--layer")
    args = ap.parse_args(argv)

    d = {"model": "base.en", "device": "auto", "seconds": 8, **layer_defaults(args.layer)}
    model = args.model or d["model"]
    device = args.device or d["device"]
    seconds = args.seconds or d["seconds"]

    if args.file:
        print(transcribe(Path(args.file), model, device))
        return 0
    with tempfile.TemporaryDirectory(prefix="al-listen-") as tmp:
        wav = Path(tmp) / "rec.wav"
        record(wav, seconds)
        if not wav.exists() or wav.stat().st_size <= 44:
            log("nothing recorded")
            return 1
        print(transcribe(wav, model, device))
    return 0


if __name__ == "__main__":
    sys.exit(main())
