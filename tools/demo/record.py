#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]
# ///
"""Record real command runs as a terminal video (MP4) and GIFs. Nothing is faked: every scene
runs its commands and renders the output they produced; long waits are compressed, and scenes
marked `replay` print a saved log of a real run (labelled as a replay on screen).

  tools/demo/record.py tools/demo/scenes.toml --out docs/demo [--only SCENE …] [--no-run]

Per scene: <out>/<id>.gif, <out>/<id>.mp4 and <out>/logs/<id>.txt (the captured output).
All scenes: <out>/tour.mp4 with title cards. `--no-run` re-renders from the captured logs.
Needs ffmpeg and a monospace TTF (AL_DEMO_FONT, else fontconfig's default monospace).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent.parent
COLS, ROWS, FPS = 110, 30, 10
BG, FG, DIM, GREEN, RED, YELLOW, BLUE = "#0d1117", "#c9d1d9", "#6e7681", "#3fb950", "#f85149", "#d29922", "#58a6ff"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


def font_path(bold: bool = False) -> str:
    if os.environ.get("AL_DEMO_FONT"):
        return os.environ["AL_DEMO_FONT"]
    query = "monospace:bold" if bold else "monospace"
    return subprocess.run(["fc-match", "-f", "%{file}", query], capture_output=True, text=True, check=True).stdout


FONT = ImageFont.truetype(font_path(), 15)
BOLD = ImageFont.truetype(font_path(True), 15)
BIG = ImageFont.truetype(font_path(True), 34)
MID = ImageFont.truetype(font_path(), 19)
CW = int(FONT.getlength("M"))
LH = 21
PAD = 18
W, H = (PAD * 2 + CW * COLS) // 2 * 2, (PAD * 2 + 34 + LH * ROWS) // 2 * 2


def colour(line: str) -> str:
    if line.startswith("$ "):
        return BLUE
    if re.search(r"\b(FAIL|FAILED|failed|error|Error|rejected)\b|^al: ", line):
        return RED
    if re.search(r"\b(ok|passed|PASSED|merge|shipped|pass)\b", line):
        return GREEN
    if line.startswith("#") or "skipped" in line or line.startswith("[slots]"):
        return YELLOW if line.startswith("[slots]") else DIM
    return FG


def frame(title: str, lines: list[str], cursor: bool = False) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 30], fill="#161b22")
    for i, c in enumerate(("#f85149", "#d29922", "#3fb950")):
        d.ellipse([12 + i * 20, 9, 24 + i * 20, 21], fill=c)
    d.text((84, 6), title, font=BOLD, fill=DIM)
    shown = lines[-ROWS:]
    for row, line in enumerate(shown):
        text = line[:COLS]
        d.text((PAD, 34 + PAD // 2 + row * LH), text, font=BOLD if line.startswith("$ ") else FONT, fill=colour(line))
    if cursor and shown:
        x = PAD + CW * min(len(shown[-1]), COLS)
        y = 34 + PAD // 2 + (len(shown) - 1) * LH
        d.rectangle([x, y + 2, x + CW - 2, y + LH - 3], fill=FG)
    return img


def card(title: str, subtitle: str, note: str = "") -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W // 2, H // 2 - 50), title, font=BIG, fill=FG, anchor="mm")
    y = H // 2 + 8
    for part in wrap(subtitle, 78):
        d.text((W // 2, y), part, font=MID, fill=BLUE, anchor="mm")
        y += 28
    if note:
        d.text((W // 2, H - 44), note, font=FONT, fill=DIM, anchor="mm")
    return img


def wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return out + [line] if line else out


def run_step(step: dict, env: dict) -> list[tuple[float, str]]:
    """Run one command; return (seconds since start, line) for every output line."""
    cwd = (ROOT / step.get("cwd", ".")).resolve()
    start = time.monotonic()
    proc = subprocess.Popen(["bash", "-c", step["run"]], cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    events: list[tuple[float, str]] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = ANSI.sub("", raw.rstrip("\n")).replace(str(ROOT) + "/", "").replace(str(Path.home()), "~").expandtabs(4)
        for piece in ([line[i:i + COLS] for i in range(0, len(line), COLS)] or [""]):
            events.append((time.monotonic() - start, piece))
    proc.wait()
    expected = step.get("exit", 0)
    if proc.returncode != expected:
        print(f"    !! `{step['run'][:70]}` exited {proc.returncode}, expected {expected}", file=sys.stderr)
        events.append((time.monotonic() - start, f"[exit {proc.returncode}]"))
    return events


def capture(scene: dict) -> list[dict]:
    env = dict(os.environ, PYTHONUNBUFFERED="1", TERM="dumb", NO_COLOR="1", PLTCOLLECTS=f"{ROOT / 'racket'}:",
               PATH=f"{ROOT / 'tools'}:{os.environ['PATH']}")
    recorded = []
    for step in scene["steps"]:
        if step.get("hidden"):
            subprocess.run(["bash", "-c", step["run"]], cwd=(ROOT / step.get("cwd", ".")).resolve(), env=env,
                           capture_output=True, text=True)
            continue
        shown = step.get("show", step["run"])
        print(f"    $ {shown[:90]}")
        recorded.append({"show": shown, "note": step.get("note", ""), "events": run_step(step, env),
                         "max_lines": step.get("max_lines", 400)})
    return recorded


def render(scene: dict, recorded: list[dict], frames_dir: Path) -> int:
    """Write PNG frames; returns the count. Waits are compressed: no gap longer than `gap` seconds,
    and bursts of output are paced so they can be read."""
    title = f"al — {scene['title']}"
    lines: list[str] = []
    n = 0

    def emit(count: int = 1, cursor: bool = False) -> None:
        nonlocal n
        img = frame(title, lines, cursor)
        for _ in range(count):
            img.save(frames_dir / f"{n:05d}.png")
            n += 1

    gap = float(scene.get("gap", 0.6))
    for step in recorded:
        if step["note"]:
            lines.append(f"# {step['note']}")
        lines.append("$ ")
        emit(3, cursor=True)
        typed = step["show"]
        for i in range(0, len(typed), 3):          # typing animation, 3 chars per frame
            lines[-1] = "$ " + typed[: i + 3]
            emit(1, cursor=True)
        emit(4)
        events = step["events"]
        if len(events) > step["max_lines"]:
            keep = step["max_lines"] // 2
            events = events[:keep] + [(events[keep][0], f"… ({len(events) - 2 * keep} lines) …")] + events[-keep:]
        previous = 0.0
        burst = 0
        for at, line in events:
            wait = min(at - previous, gap)
            previous = at
            if wait > 0.08:
                emit(max(1, round(wait * FPS)))
                burst = 0
            lines.append(line)
            burst += 1
            if len(events) > 45:                   # long output: one line per frame
                emit(1)
            else:                                  # short output arriving in a burst: readable pace
                emit(2)
        emit(int(FPS * float(scene.get("hold", 1.6))))
        lines.append("")
    emit(int(FPS * 1.2))
    return n


def encode(frames_dir: Path, out_mp4: Path, out_gif: Path | None) -> None:
    quiet = ["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i", str(frames_dir / "%05d.png")]
    subprocess.run([*quiet, "-c:v", "libx264", "-preset", "slow", "-crf", "30", "-tune", "animation",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_mp4)], check=True)
    if out_gif:
        subprocess.run([*quiet, "-vf", "fps=5,scale=820:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=32[p];[b][p]paletteuse=dither=none",
                        str(out_gif)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenes", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "docs/demo")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--no-run", action="store_true", help="re-render from <out>/logs/*.json without running commands")
    a = ap.parse_args()
    if not shutil.which("ffmpeg"):
        print("ffmpeg is required", file=sys.stderr)
        return 2
    spec = tomllib.loads(a.scenes.read_text())
    out = a.out.resolve()
    (out / "logs").mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)

        def clip(name: str, images: list[tuple[Image.Image, float]]) -> Path:
            d = work / name
            d.mkdir()
            k = 0
            for img, seconds in images:
                for _ in range(int(seconds * FPS)):
                    img.save(d / f"{k:05d}.png")
                    k += 1
            path = work / f"{name}.mp4"
            encode(d, path, None)
            return path

        parts.append(clip("intro", [(card(spec["title"], spec["subtitle"], spec.get("note", "")), 3.5)]))
        for scene in spec["scene"]:
            sid = scene["id"]
            raw = out / "logs" / f"{sid}.json"
            if a.only and sid not in a.only:
                if (out / f"{sid}.mp4").exists():
                    parts.append(clip(f"card-{sid}", [(card(scene["title"], scene["subtitle"]), 2.6)]))
                    parts.append(out / f"{sid}.mp4")
                continue
            print(f"scene {sid}: {scene['title']}")
            if a.no_run and raw.exists():
                recorded = json.loads(raw.read_text())
            else:
                recorded = capture(scene)
                raw.write_text(json.dumps(recorded))
            (out / "logs" / f"{sid}.txt").write_text("\n".join(
                f"$ {s['show']}\n" + "\n".join(line for _, line in s["events"]) for s in recorded) + "\n")
            frames = work / f"frames-{sid}"
            frames.mkdir()
            count = render(scene, recorded, frames)
            encode(frames, out / f"{sid}.mp4", out / f"{sid}.gif")
            print(f"    {count / FPS:.0f}s  mp4 {(out / f'{sid}.mp4').stat().st_size // 1024} KB  gif {(out / f'{sid}.gif').stat().st_size // 1024} KB")
            parts.append(clip(f"card-{sid}", [(card(scene["title"], scene["subtitle"]), 2.6)]))
            parts.append(out / f"{sid}.mp4")
        listing = work / "concat.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                        "-c:v", "libx264", "-preset", "slow", "-crf", "30", "-tune", "animation", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(out / "tour.mp4")], check=True)
    tour = out / "tour.mp4"
    seconds = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(tour)],
                             capture_output=True, text=True).stdout.strip()
    print(f"tour.mp4: {float(seconds):.0f}s, {tour.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
