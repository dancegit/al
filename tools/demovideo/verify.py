#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10", "rapidocr-onnxruntime==1.4.4"]
# ///
"""Check a recorded demo video and write its contact sheet and its privacy report.

  uv run tools/demovideo/verify.py FOLDER [--story {rota,testbed}] [--json]

FOLDER holds al-demo.mp4 and narration.md (what `make.py assemble` writes). With --story testbed it holds
al-demo-testbed.mp4 and narration-testbed.md instead, the sheet is al-demo-testbed-contact-sheet.png, the
report privacy-ocr-testbed.json, and the timed script must have the scenes of
tools/demovideo/narration-testbed.md (tools/demovideo/stories.py). Checked, in this order:

  shape      H.264, 1280×720 (or 1920×1080), yuv420p, 270–300 s, and it decodes without an error
  sheet      one frame every 10 s from 0, as FOLDER/al-demo-contact-sheet.png (tiles of 320×180, six a row)
  narration  narration.md parses and holds (rules.validate), every scene has a start and an end time, and
             the last end is the video's duration within a second
  ocr        one frame every 5 s is read with RapidOCR (ONNX models inside the package; nothing leaves this
             machine) and rules.find_private finds nothing in its text
  bands      each of those frames carries the caption band: the blue bar at its left edge and white text

It writes FOLDER/privacy-ocr.json (the report; this machine's own names are counted, never written)
and exits 0 only when all five hold. A report for the same video and script (size and sha256) is
reused, and a lock file keeps two verifiers apart. Needs ffmpeg and ffprobe.
"""
from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import rules  # noqa: E402
import stories  # noqa: E402

VIDEO, TIMED, SHEET, REPORT, LOCK = "al-demo.mp4", "narration.md", "al-demo-contact-sheet.png", "privacy-ocr.json", ".verify.lock"
SIZES = (("1280", "720"), ("1920", "1080"))
TILE, COLUMNS = (320, 180), 6
BAR = (0x58, 0xA6, 0xFF)
BAR_TOLERANCE, BRIGHT, MIN_BRIGHT_SHARE = 40, 200, 0.003
OCR_THREADS = 4
VERSION = 1


def _hash(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _write(path: Path, data: bytes) -> None:
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(handle, "wb") as out:
        out.write(data)
    os.chmod(name, 0o644)
    os.replace(name, path)


def shape(video: Path) -> dict:
    cp = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=codec_name,width,height,pix_fmt,avg_frame_rate:format=duration", "-of", "default=noprint_wrappers=1",
                         str(video)], capture_output=True, text=True)
    info = dict(line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line)
    out = {"codec": info.get("codec_name", ""), "width": info.get("width", ""), "height": info.get("height", ""),
           "pix_fmt": info.get("pix_fmt", ""), "duration": float(info.get("duration") or 0), "fps": info.get("avg_frame_rate", "")}
    decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"], capture_output=True, text=True)
    out["decodes"] = decode.returncode == 0 and not decode.stderr.strip()
    out["decode_errors"] = decode.stderr.strip()[:500]
    audio = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                            str(video)], capture_output=True, text=True)
    out["audio"] = audio.stdout.strip() or "none"
    problems = []
    if out["codec"] != "h264":
        problems.append(f"the codec is {out['codec'] or 'unknown'}, not h264")
    if (out["width"], out["height"]) not in SIZES:
        problems.append(f"the size is {out['width']}×{out['height']}, not 1280×720 or 1920×1080")
    if out["pix_fmt"] != "yuv420p":
        problems.append(f"the pixel format is {out['pix_fmt']}, not yuv420p")
    if not rules.MIN_SECONDS <= out["duration"] <= rules.MAX_SECONDS:
        problems.append(f"the duration is {out['duration']:.1f} s, outside {rules.MIN_SECONDS}–{rules.MAX_SECONDS} s")
    if not out["decodes"]:
        problems.append("it does not decode without an error")
    out["problems"] = problems
    out["ok"] = not problems
    return out


def frames(video: Path, folder: Path, fps: float) -> list[tuple[float, Path]]:
    """One full-size frame every OCR_EVERY seconds from 0, as (seconds, PNG)."""
    step = max(1, round(fps * rules.OCR_EVERY))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", f"select='not(mod(n\\,{step}))'", "-vsync", "0",
                    str(folder / "f-%04d.png")], check=True, capture_output=True)
    return [(i * rules.OCR_EVERY, path) for i, path in enumerate(sorted(folder.glob("f-*.png")))]


def contact_sheet(sampled: list[tuple[float, Path]], target: Path) -> dict:
    from PIL import Image, ImageDraw, ImageFont
    chosen = [(at, path) for at, path in sampled if at % rules.SHEET_EVERY == 0]
    if not chosen:
        return {"ok": False, "frames": 0, "problems": ["no frame to put on the sheet"]}
    try:
        face = ImageFont.load_default(size=15)
    except TypeError:
        face = ImageFont.load_default()
    rows = -(-len(chosen) // COLUMNS)
    sheet = Image.new("RGB", (TILE[0] * COLUMNS, TILE[1] * rows), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    for i, (at, path) in enumerate(chosen):
        x, y = (i % COLUMNS) * TILE[0], (i // COLUMNS) * TILE[1]
        with Image.open(path) as frame:
            sheet.paste(frame.convert("RGB").resize(TILE, Image.LANCZOS), (x, y))
        label = rules.clock(at)
        draw.rectangle([x, y, x + 62, y + 20], fill=(0, 0, 0))
        draw.text((x + 5, y + 2), label, font=face, fill=(255, 255, 255))
    handle, name = tempfile.mkstemp(prefix=".sheet.", suffix=".png", dir=target.parent)
    os.close(handle)
    sheet.save(name, format="PNG")
    os.chmod(name, 0o644)
    os.replace(name, target)
    return {"ok": True, "frames": len(chosen), "every": rules.SHEET_EVERY, "file": target.name, "problems": []}


def narration(path: Path, duration: float, committed: Path | None = None) -> tuple[dict, rules.Script | None]:
    """`committed`: the story's own script; the timed one must have its scenes (given for a story other than the default)."""
    if not path.is_file():
        return {"ok": False, "problems": [f"no {path.name} beside the video"]}, None
    script = rules.parse(path.read_text(encoding="utf-8"))
    problems = rules.validate(script)
    if committed is not None:
        expected = [s.id for s in rules.parse(committed.read_text(encoding="utf-8")).scenes]
        if [s.id for s in script.scenes] != expected:
            problems.append(f"the scenes are not those of {committed.name}")
    untimed = [s.id for s in script.scenes if s.start is None or s.end is None
               or any(c.start is None or c.end is None for c in s.captions)]
    if untimed:
        problems.append("no start and end time for " + ", ".join(untimed))
    ends = [s.end for s in script.scenes if s.end is not None]
    if ends and abs(ends[-1] - duration) > 1.0:
        problems.append(f"the last scene ends at {rules.clock(ends[-1])}, the video at {rules.clock(duration)}")
    starts = [s.start for s in script.scenes if s.start is not None]
    if starts != sorted(starts):
        problems.append("the scenes' times are not in order")
    out = {"ok": not problems, "problems": problems, "parts": [[p.number, p.title] for p in script.parts],
           "scenes": [{"id": s.id, "start": s.start, "end": s.end, "captions": len(s.captions)} for s in script.scenes],
           "seconds": script.seconds}
    return out, script


def caption_at(script: rules.Script | None, at: float) -> str:
    for scene in script.scenes if script else []:
        for caption in scene.captions:
            if caption.start is not None and caption.end is not None and caption.start <= at < caption.end:
                return caption.text
    return ""


def band(path: Path) -> dict:
    """Does the bottom band of the frame hold the caption strip: its blue bar and white text?"""
    from PIL import Image, ImageChops, ImageStat
    with Image.open(path) as frame:
        frame = frame.convert("RGB")
        scale = frame.size[1] / rules.SIZE[1]
        height, bar = round(rules.STRIP_H * scale), max(1, round(6 * scale))
        strip = frame.crop((0, frame.size[1] - height, frame.size[0], frame.size[1]))
    mean = tuple(ImageStat.Stat(strip.crop((0, 0, bar, height))).mean)
    red, green, blue_channel = strip.split()
    darkest = ImageChops.darker(ImageChops.darker(red, green), blue_channel)       # a pixel is bright when all three are
    bright = sum(darkest.histogram()[BRIGHT + 1:]) / (strip.size[0] * strip.size[1])
    blue = all(abs(mean[c] - BAR[c]) <= BAR_TOLERANCE for c in range(3))
    return {"bar": [round(v) for v in mean], "bright_share": round(bright, 4), "ok": blue and bright >= MIN_BRIGHT_SHARE}


def read_frames(sampled: list[tuple[float, Path]], script: rules.Script | None, values: dict) -> tuple[dict, dict]:
    from rapidocr_onnxruntime import RapidOCR
    # a few threads, not one per core of the host: in a container with a CPU quota the default is many times slower
    engine = RapidOCR(intra_op_num_threads=max(1, min(OCR_THREADS, os.cpu_count() or 1)), inter_op_num_threads=1)
    hits, samples, failures = [], [], []
    for at, path in sampled:
        result, _elapsed = engine(str(path))
        lines = result or []
        from PIL import Image
        with Image.open(path) as frame:
            height = frame.size[1]
        cut = height - round(rules.STRIP_H * height / rules.SIZE[1])
        text = "\n".join(str(line[1]) for line in lines)
        low = " ".join(str(line[1]) for line in lines if min(point[1] for point in line[0]) >= cut - 4)
        for hit in rules.find_private(text, values):
            shown = hit.excerpt if hit.kind in rules.PATTERNS else f"({len(hit.match)} characters)"
            hits.append({"at": at, "time": rules.clock(at), "kind": hit.kind, "excerpt": shown})
        expected = caption_at(script, at)
        samples.append({"at": at, "time": rules.clock(at), "read": low, "caption": expected,
                        "ratio": round(difflib.SequenceMatcher(None, low.lower(), expected.lower()).ratio(), 3)})
        seen = band(path)
        if not seen["ok"]:
            failures.append({"at": at, "time": rules.clock(at), **seen})
    ocr = {"ok": not hits, "engine": "rapidocr-onnxruntime 1.4.4 (local)", "every": rules.OCR_EVERY, "frames": len(sampled),
           "hits": hits, "captions": samples}
    bands = {"ok": not failures and bool(sampled), "frames": len(sampled), "failures": failures}
    return ocr, bands


def verify(folder: Path, story: str = stories.DEFAULT) -> dict:
    chosen = stories.get(story)
    default = chosen.name == stories.DEFAULT
    video = folder / chosen.video
    report: dict = {"version": VERSION, "folder": folder.name, "ok": False}
    if not default:
        report["story"] = chosen.name
    if not video.is_file():
        report["problems"] = [f"no {chosen.video} in {folder}"]
        return report
    hashes = {chosen.video: _hash(video)}
    if (folder / chosen.timed).is_file():
        hashes[chosen.timed] = _hash(folder / chosen.timed)
    previous = folder / chosen.report
    if previous.is_file() and (folder / chosen.sheet).is_file():
        try:
            old = json.loads(previous.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
        if old.get("hashes") == hashes and old.get("version") == VERSION:
            old["reused"] = True
            return old
    values = rules.machine_values(ROOT)
    report["hashes"] = hashes
    report["rules"] = {"patterns": {kind: pattern.pattern for kind, pattern in rules.PATTERNS.items()},
                       "machine_values": {key: len(found) for key, found in values.items()},
                       "kinds": list(rules.KINDS_OF_HIT)}
    report["shape"] = shape(video)
    report["narration"], script = narration(folder / chosen.timed, report["shape"]["duration"],
                                            None if default else chosen.narration)
    with tempfile.TemporaryDirectory(prefix="al-demo-verify-") as scratch:
        try:
            rate = report["shape"]["fps"].split("/")
            fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else float(rate[0])
            sampled = frames(video, Path(scratch), fps)
        except (subprocess.CalledProcessError, ValueError, ZeroDivisionError) as error:
            sampled = []
            report["frames_error"] = str(error)[:300]
        report["sheet"] = contact_sheet(sampled, folder / chosen.sheet)
        report["ocr"], report["bands"] = read_frames(sampled, script, values)
    report["ok"] = all(report[part]["ok"] for part in ("shape", "narration", "sheet", "ocr", "bands"))
    _write(previous, (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return report


def summary(report: dict) -> str:
    if "shape" not in report:
        return "verify: " + "; ".join(report.get("problems", ["nothing to check"]))
    s, lines = report["shape"], []
    lines.append(f"shape:     {'ok' if s['ok'] else 'NOT OK'}: {s['codec']} {s['width']}×{s['height']} {s['pix_fmt']}, "
                 f"{s['duration']:.1f} s, audio {s['audio']}" + "".join(f"\n           {p}" for p in s["problems"]))
    sheet, chosen = report["sheet"], stories.get(report.get("story") or stories.DEFAULT)
    lines.append(f"sheet:     {'ok' if sheet['ok'] else 'NOT OK'}: {sheet['frames']} frame(s), one every {rules.SHEET_EVERY} s, {chosen.sheet}")
    n = report["narration"]
    lines.append(f"narration: {'ok' if n['ok'] else 'NOT OK'}: {len(n.get('parts', []))} parts, {len(n.get('scenes', []))} scenes"
                 + "".join(f"\n           {p}" for p in n["problems"]))
    o = report["ocr"]
    lines.append(f"ocr:       {'ok' if o['ok'] else 'NOT OK'}: {o['frames']} frame(s) read, one every {o['every']} s, {len(o['hits'])} private match(es)"
                 + "".join(f"\n           {h['time']} {h['kind']}: {h['excerpt']}" for h in o["hits"]))
    b = report["bands"]
    lines.append(f"bands:     {'ok' if b['ok'] else 'NOT OK'}: {b['frames'] - len(b['failures'])} of {b['frames']} frame(s) carry the caption band"
                 + "".join(f"\n           {f['time']}: bar {f['bar']}, bright {f['bright_share']}" for f in b["failures"]))
    lines.append(f"verify:    {'ok' if report['ok'] else 'NOT OK'}" + (" (report reused: same video and script)" if report.get("reused") else "")
                 + f"; report in {chosen.report}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check a recorded demo video; write its contact sheet and privacy report.")
    ap.add_argument("folder", type=Path, metavar="FOLDER")
    ap.add_argument("--story", choices=tuple(stories.STORIES), default=stories.DEFAULT,
                    help="rota (the default): al-demo.mp4 and narration.md; testbed: al-demo-testbed.mp4 and narration-testbed.md")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    a = ap.parse_args(argv)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"verify.py: {tool} is not on PATH; install ffmpeg (it brings ffprobe)", file=sys.stderr)
            return 2
    folder = a.folder.resolve()
    if not folder.is_dir():
        print(f"verify.py: no folder {a.folder}", file=sys.stderr)
        return 2
    with (folder / LOCK).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = verify(folder, a.story)
    print(json.dumps(report, indent=2, ensure_ascii=False) if a.json else summary(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
