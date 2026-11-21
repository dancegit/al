#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10", "playwright>=1.45", "pytest>=8"]
# ///
"""Record the five-minute demo video of al, one scene at a time, and assemble it.

  uv run tools/demovideo/make.py scene ID… --work DIR [--own-plan PATH] [--rehearse]
  uv run tools/demovideo/make.py assemble --work DIR [--out OUT] [--no-voice]
  uv run tools/demovideo/make.py clean --work DIR

`scene` records the scenes named (ids of tools/demovideo/narration.md) into DIR/clips/ID.mp4 (H.264,
1280×720, 15 fps, no audio, exactly the scene's seconds) with DIR/clips/ID.json beside it: the real
seconds, the speed, the held seconds, the caption times as rendered, what was blanked and which
stand-ins were used. --rehearse runs the same steps without video. --own-plan is the plan of the run
that records the video (scene 04-own-plan shows its Proof plan rows).

`assemble` joins the clips in script order, adds a local espeak-ng voice when there is one (--no-voice:
never), checks the result and writes OUT/al-demo.mp4 and OUT/narration.md (the script with the times in
the final video). OUT is .al-proof/demo when the repository has a .al-proof folder (a run's proof
folder), else media/demo-video; both are ignored by git. `clean` removes DIR.

Every scene runs in a neutral environment of its own (tools/showcase/make.py::neutral_env) on invented
names. Before a frame is encoded, every terminal transcript and the dashboard's page text at each
caption is checked with rules.find_private; a private match is blanked with █ and listed, and one that
is still there stops the scene. A command that exits other than the scene expects, or a page that
reports an error, records nothing. Nothing is uploaded or sent anywhere: a person reviews the video.

Needs uv, ffmpeg and ffprobe, Chromium or Chrome, DejaVu Sans Mono and racket; espeak-ng is optional.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SHOWCASE = ROOT / "tools" / "showcase"
for _folder in (str(ROOT / "tools"), str(SHOWCASE), str(HERE)):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

import rules  # noqa: E402
import scenes  # noqa: E402

NARRATION = HERE / "narration.md"
VIDEO, TIMED = "al-demo.mp4", "narration.md"
PROOF_OUT, MEDIA_OUT = ".al-proof/demo", "media/demo-video"
MAX_WORK_BYTES = 2_500_000_000
W, H = rules.SIZE
LEAD = 0.2                       # seconds of a browser recording kept before its first caption
BADGE_FROM = 1.05                # a part played faster than this gets the badge
VOICE_RATES = (165, 190, 215)    # espeak-ng words a minute: the next one when a caption is too short for the last
MUTE = f"ffmpeg -i {VIDEO} -c:v copy -an al-demo-silent.mp4"


def say(text: str) -> None:
    print(text, flush=True)


def _showcase():
    """tools/showcase/make.py under a name of its own (this file is called make.py too)."""
    name = "showcase_make"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SHOWCASE / "make.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def default_out() -> Path:
    return ROOT / PROOF_OUT if (ROOT / ".al-proof").is_dir() else ROOT / MEDIA_OUT


def work_bytes(work: Path) -> int:
    return sum(p.stat().st_size for p in work.rglob("*") if p.is_file() and not p.is_symlink()) if work.is_dir() else 0


def too_big(work: Path) -> str:
    size = work_bytes(work)
    if size > MAX_WORK_BYTES:
        return (f"make.py: {work} holds {size / 1e9:.1f} GB, over the limit of {MAX_WORK_BYTES / 1e9:.1f} GB: "
                f"run `make.py clean --work {work}` first")
    return ""


def script() -> rules.Script:
    parsed = rules.parse(NARRATION.read_text(encoding="utf-8"))
    problems = rules.validate(parsed)
    if problems:
        raise SystemExit("make.py: tools/demovideo/narration.md: " + "; ".join(problems))
    return parsed


# ------------------------------------------------------------------------------------------ one part

X264_BODY = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-pix_fmt", "yuv420p", "-an"]


def encode_frames(part: scenes.Part, ctx: scenes.Ctx, slot: float, target: Path) -> dict:
    """A terminal or card part: each segment is held until the script says its next caption starts (never
    cut), and the whole is played faster only when it is longer than its time."""
    base = ctx.offset(part.first)
    plan, cues, at, real, held = [], {}, 0, 0, 0
    for k, segment in enumerate(part.segments):
        if segment.cue is not None:
            cues[segment.cue] = at / rules.FPS
        at += len(segment.frames)
        real += len(segment.frames)
        following = next((s.cue for s in part.segments[k + 1:] if s.cue is not None), None)
        due = round(((ctx.offset(following) - base) if following is not None else slot) * rules.FPS)
        pad = max(0, due - at)
        at, held = at + pad, held + pad
        plan.append((segment.frames, pad))
    total = at / rules.FPS
    speed, _hold = rules.fit(total, slot)
    filters = (f"setpts=PTS/{speed:.6f}," if speed > 1 else "") + f"fps={rules.FPS},tpad=stop_mode=clone:stop_duration=1"
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                             "-r", str(rules.FPS), "-i", "-", "-vf", filters, "-t", f"{slot:.3f}", *X264_BODY, str(target)],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    for frames, pad in plan:
        raw = b""
        for frame in frames:
            raw = frame.tobytes()
            proc.stdin.write(raw)
        for _ in range(pad):
            proc.stdin.write(raw)
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg (frames) failed: {proc.stderr.read().decode() if proc.stderr else ''}")
    return {"kind": "frames", "seconds": slot, "real_seconds": round(real / rules.FPS, 2), "speed": round(speed, 3),
            "hold": round(held / rules.FPS / speed, 2), "cues": {k: v / speed for k, v in cues.items()}}


def encode_video(part: scenes.Part, ctx: scenes.Ctx, slot: float, target: Path) -> dict:
    """A browser part: from just before its first caption to its end, faster when longer than its time,
    the last frame held when shorter."""
    showcase = _showcase()
    if part.video is None or not part.cues:
        raise RuntimeError(f"{ctx.scene.id}: no recording")
    # the recorder's file goes on for some seconds after the page was last looked at (the browser closing):
    # the part ends where the clock says the scene function ended
    start = max(0.0, part.cues[0][0] - LEAD)
    real = min(part.wall, showcase.duration(part.video)) - start
    speed, hold = rules.fit(real, slot)
    filters = (f"trim=duration={real:.3f},setpts=(PTS-STARTPTS)/{speed:.6f},fps={rules.FPS},scale={W}:{H}:flags=lanczos,setsar=1,"
               f"tpad=stop_mode=clone:stop_duration={hold + 1:.3f}")
    showcase.ffmpeg("-ss", f"{start:.3f}", "-i", str(part.video), "-vf", filters, "-t", f"{slot:.3f}", *X264_BODY, str(target))
    return {"kind": "video", "seconds": slot, "real_seconds": round(real, 2), "speed": round(speed, 3), "hold": round(hold, 2),
            "cues": {caption: (at - start) / speed for at, caption in part.cues}}


# ------------------------------------------------------------------------------------------ one scene

def badge_png(speed: float, path: Path) -> Path:
    from PIL import Image, ImageDraw
    import term
    text = f"sped up ×{speed:.1f}"
    face = term.font(18, True)
    width = int(face.getlength(text)) + 28
    img = Image.new("RGBA", (width, 34), (13, 17, 23, 230))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width - 1, 33], outline=term.YELLOW, width=2)
    d.text((width // 2, 17), text, font=face, fill=term.YELLOW, anchor="mm")
    img.save(path)
    return path


def caption_times(scene: rules.Scene, fitted: dict[int, float]) -> list[dict]:
    """Where each caption is in the clip: the first at 0, each later one at the later of its script
    offset and the fitted time of its cue; each stays until the next, the last until the scene ends."""
    starts = []
    for i, caption in enumerate(scene.captions):
        at = 0.0 if i == 0 else max(caption.offset, fitted.get(i, caption.offset))
        if starts:
            at = max(at, starts[-1])
        starts.append(round(min(at, scene.seconds), 2))
    out = []
    for i, caption in enumerate(scene.captions):
        end = starts[i + 1] if i + 1 < len(starts) else scene.seconds
        out.append({"offset": caption.offset, "start": starts[i], "end": end, "text": caption.text,
                    "moved": round(starts[i] - caption.offset, 2)})
    return out


def record_scene(scene: rules.Scene, work: Path, values: dict, own_plan: Path | None, rehearse: bool) -> int:
    showcase = _showcase()
    env_root = work / f"env-{scene.id}"
    shutil.rmtree(env_root, ignore_errors=True)
    env_root.mkdir(parents=True)
    showcase.neutral_env(env_root)
    scratch = env_root / "scratch"
    scratch.mkdir()
    ctx = scenes.Ctx(scene, work, env_root / "home" / "work", values, own_plan, rehearse)
    parts = scenes.SCENES[scene.id](ctx)
    firsts = [part.first for part in parts]
    if firsts != sorted(firsts) or firsts[0] != 0:
        raise scenes.SceneStopped(f"{scene.id}: the parts do not start with captions in script order: {firsts}")
    cued = [c for part in parts for c in ([s.cue for s in part.segments if s.cue is not None] if part.kind == "frames"
                                          else [caption for _at, caption in part.cues])]
    if cued != sorted(set(cued)) or (cued and cued[-1] >= len(scene.captions)):
        raise scenes.SceneStopped(f"{scene.id}: the cues {cued} are not in the order of the script's captions")
    if rehearse:
        say(f"    rehearsed: {len(parts)} part(s), cues {cued}; blanked {ctx.blanked or 'nothing'}")
        return 0
    import term
    encoded, fitted, files = [], {}, []
    for n, part in enumerate(parts):
        begin = ctx.offset(part.first)
        slot = (ctx.offset(parts[n + 1].first) if n + 1 < len(parts) else scene.seconds) - begin
        target = scratch / f"part-{n}.mp4"
        info = (encode_frames if part.kind == "frames" else encode_video)(part, ctx, slot, target)
        fitted.update({caption: begin + at for caption, at in info.pop("cues").items()})
        encoded.append(info)
        files.append(target)
    captions = caption_times(scene, fitted)
    for i, caption in enumerate(captions):
        if abs(caption["moved"]) > 1.0:
            say(f"    MOVED: caption {i} is at +{caption['start']:g}, the script says +{caption['offset']:g}: "
                f"edit tools/demovideo/narration.md to what was recorded")
        if caption["end"] - caption["start"] < rules.MIN_CAPTION_SECONDS:
            raise scenes.SceneStopped(f"{scene.id}: caption {i} would be on screen for {caption['end'] - caption['start']:.1f} s; "
                                      f"give it more time in tools/demovideo/narration.md")
    inputs: list[str] = []
    for path in files:
        inputs += ["-i", str(path)]
    chain = ["".join(f"[{i}:v]" for i in range(len(files))) + f"concat=n={len(files)}:v=1:a=0,fps={rules.FPS},setsar=1[v0]"]
    label, index = "v0", len(files)
    for i, caption in enumerate(captions):
        strip = scratch / f"caption-{i:02d}.png"
        term.caption_strip(caption["text"]).save(strip)
        inputs += ["-i", str(strip)]
        until = caption["end"] if i + 1 < len(captions) else scene.seconds + 5
        chain.append(f"[{label}][{index}:v]overlay=0:H-h:enable='gte(t,{caption['start']:.3f})*lt(t,{until:.3f})'[v{index}]")
        label, index = f"v{index}", index + 1
    fastest = max(info["speed"] for info in encoded)
    if fastest > BADGE_FROM:
        inputs += ["-i", str(badge_png(fastest, scratch / "badge.png"))]
        chain.append(f"[{label}][{index}:v]overlay=W-w-16:44[v{index}]")
        label = f"v{index}"
    clips = work / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    clip = clips / f"{scene.id}.mp4"
    showcase.ffmpeg(*inputs, "-filter_complex", ";".join(chain), "-map", f"[{label}]", "-t", f"{scene.seconds:.3f}",
                    "-r", str(rules.FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "14", "-pix_fmt", "yuv420p",
                    "-an", "-map_metadata", "-1", str(clip))
    seconds = showcase.duration(clip)
    if abs(seconds - scene.seconds) > 0.11:
        raise RuntimeError(f"{clip.name} is {seconds:.2f} s long, not {scene.seconds:g} s")
    record = {"scene": scene.id, "kind": scene.kind, "seconds": scene.seconds,
              "real_seconds": round(sum(info["real_seconds"] for info in encoded), 2), "speed": fastest,
              "hold": round(sum(info["hold"] for info in encoded), 2), "parts": encoded, "captions": captions,
              "blanked": ctx.blanked, "shortened": sorted(set(ctx.shortened)), "stand_ins": ctx.stand_ins}
    (clips / f"{scene.id}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    say(f"    {clip.name}: {seconds:.1f} s, real {record['real_seconds']:g} s, speed ×{fastest:g}, held {record['hold']:g} s, "
        f"{len(ctx.blanked)} kind(s) of blank")
    return 0


def cmd_scene(a: argparse.Namespace) -> int:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"make.py: {tool} is not on PATH; install ffmpeg (it brings ffprobe)", file=sys.stderr)
            return 2
    parsed = script()
    unknown = [name for name in a.ids if name not in scenes.SCENES or name not in [s.id for s in parsed.scenes]]
    if unknown:
        print(f"make.py: no scene {unknown}; scenes: {', '.join(s.id for s in parsed.scenes)}", file=sys.stderr)
        return 2
    work = a.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    stop = too_big(work)
    if stop:
        print(stop, file=sys.stderr)
        return 1
    own_plan = a.own_plan.resolve() if a.own_plan else None
    import privacy
    forbidden = privacy.forbidden_values(Path.home())          # before HOME changes
    values = rules.machine_values(ROOT)
    original = dict(os.environ)
    import browser_clips
    import terminal_clips
    terminal_clips.FORBIDDEN[:] = forbidden
    browser_clips.FORBIDDEN[:] = forbidden
    for name in a.ids:
        scene = parsed.scene(name)
        say(f"{scene.id} ({scene.kind}, {scene.seconds:g} s){' — rehearsal' if a.rehearse else ''}")
        try:
            record_scene(scene, work, values, own_plan, a.rehearse)
        except (scenes.SceneStopped, privacy.PrivateText, terminal_clips.UnexpectedExit) as error:
            print(f"make.py: {scene.id} is not recorded: {error}", file=sys.stderr)
            return 1
        finally:
            os.environ.clear()
            os.environ.update(original)
    return 0


# ------------------------------------------------------------------------------------------ the whole video

def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


def speak(text: str, target: Path, seconds: float) -> int:
    """The caption spoken by espeak-ng (local) into `target`, at the first rate that fits `seconds`."""
    for rate in VOICE_RATES:
        subprocess.run(["espeak-ng", "-v", "en", "-s", str(rate), "-w", str(target), text], check=True, capture_output=True)
        if _wav_seconds(target) <= seconds:
            return rate
    raise SystemExit(f"make.py assemble: the caption {text!r} takes {_wav_seconds(target):.1f} s to say even at "
                     f"{VOICE_RATES[-1]} words a minute, and is on screen for {seconds:.1f} s: shorten it or give it more time")


def timed_narration(parsed: rules.Script, records: dict[str, dict], total: float, voice: str) -> str:
    """The committed script with the times of the final video, then what a viewer should know."""
    lines, starts, at = [], {}, 0.0
    for scene in parsed.scenes:
        starts[scene.id] = at
        at += scene.seconds
    current, nth = None, 0
    for line in NARRATION.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            current, nth = parsed.scene(line[4:].split(" | ")[0]), 0
            begin = starts[current.id]
            line = f"### {current.id} | {current.kind} | {current.seconds:g} s | {rules.clock(begin)}–{rules.clock(begin + current.seconds)}"
        elif line.startswith("- [") and current is not None:
            caption = records[current.id]["captions"][nth]
            begin = starts[current.id]
            line = (f"- [+{caption['start']:.1f} | {rules.clock(begin + caption['start'])}–{rules.clock(begin + caption['end'])}] "
                    f"{caption['text']}")
            nth += 1
        elif line.startswith("# "):
            line += f" (timed: {rules.clock(total)} in all)"
        lines.append(line)
    lines += ["", "## Scenes sped up", ""]
    fast = [(sid, r) for sid, r in records.items() if r["speed"] > BADGE_FROM]
    lines += [f"- `{sid}`: ×{r['speed']:.1f} ({r['real_seconds']:g} s of real time in {r['seconds']:g} s; the badge says so on screen)"
              for sid, r in fast] or ["- none: every scene plays at its real speed; a scene shorter than its time holds its last frame"]
    lines += ["", "## Blanked on screen", "",
              "Each private match is replaced by █ before a frame is made (`tools/demovideo/rules.py`).", ""]
    counted: dict[tuple, int] = {}
    for sid, r in records.items():
        for b in r["blanked"]:
            key = (sid, b["where"], b["kind"], str(b["what"]).lower())
            counted[key] = max(counted.get(key, 0), b["count"])
    blanks = [f"- `{sid}`, {where}: {kind}, {count} time(s) ({what})" for (sid, where, kind, what), count in counted.items()]
    lines += blanks or ["- nothing"]
    short = sorted({s for r in records.values() for s in r.get("shortened", [])})
    lines += ["", "Paths are shown short: the demo's work folder as `~/work`, this repository as `.`, its layer as `agentic`."]
    lines += [f"- {s}" for s in short]
    lines += ["", "## Stand-ins and fixtures", ""]
    lines += [f"- {s}" for r in records.values() for s in r["stand_ins"]] or ["- none"]
    lines += ["", "## The voice", "", voice, "", f"mute: {MUTE}", ""]
    return "\n".join(lines)


def cmd_assemble(a: argparse.Namespace) -> int:
    showcase = _showcase()
    work = a.work.resolve()
    stop = too_big(work)
    if stop:
        print(stop, file=sys.stderr)
        return 1
    parsed = script()
    clips = work / "clips"
    missing = [s.id for s in parsed.scenes if not (clips / f"{s.id}.mp4").is_file() or not (clips / f"{s.id}.json").is_file()]
    if missing:
        print(f"make.py assemble: no clip for {', '.join(missing)}: record with `make.py scene ID --work {a.work}`", file=sys.stderr)
        return 1
    records = {s.id: json.loads((clips / f"{s.id}.json").read_text()) for s in parsed.scenes}
    stale = [s.id for s in parsed.scenes if records[s.id]["seconds"] != s.seconds
             or [c["text"] for c in records[s.id]["captions"]] != [c.text for c in s.captions]]
    if stale:
        print(f"make.py assemble: the script changed since {', '.join(stale)} was recorded: record again", file=sys.stderr)
        return 1
    total = parsed.seconds
    out = (a.out or default_out()).resolve()
    out.mkdir(parents=True, exist_ok=True)
    build = work / "assemble"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True)
    inputs: list[str] = []
    for scene in parsed.scenes:
        inputs += ["-i", str(clips / f"{scene.id}.mp4")]
    count = len(parsed.scenes)
    chain = ["".join(f"[{i}:v]" for i in range(count)) + f"concat=n={count}:v=1:a=0,fps={rules.FPS},format=yuv420p[v]"]
    maps = ["-map", "[v]"]
    audio: list[str] = ["-an"]
    voice = "No voice: the captions carry the whole narration."
    if shutil.which("espeak-ng") and not a.no_voice:
        delays, at, rates = [], 0.0, []
        for scene in parsed.scenes:
            for i, caption in enumerate(records[scene.id]["captions"]):
                wav = build / f"{scene.id}-{i:02d}.wav"
                rates.append(speak(caption["text"], wav, caption["end"] - caption["start"]))
                delays.append((wav, at + caption["start"]))
            at += scene.seconds
        mix_inputs: list[str] = []
        mix = []
        for n, (wav, start) in enumerate(delays):
            mix_inputs += ["-i", str(wav)]
            mix.append(f"[{n}:a]aresample=44100,adelay={int(round(start * 1000))}:all=1[a{n}]")
        mix.append("".join(f"[a{n}]" for n in range(len(delays)))
                   + f"amix=inputs={len(delays)}:normalize=0:dropout_transition=0,apad=whole_dur={total:.3f}[a]")
        voice_wav = build / "voice.wav"
        showcase.ffmpeg(*mix_inputs, "-filter_complex", ";".join(mix), "-map", "[a]", "-t", f"{total:.3f}", "-ac", "1", str(voice_wav))
        inputs += ["-i", str(voice_wav)]
        maps += ["-map", f"{count}:a"]
        audio = ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]
        voice = (f"One mono audio stream: each caption read by espeak-ng on this machine (a synthetic voice; nothing was sent "
                 f"anywhere), at {min(rates)} to {max(rates)} words a minute. The captions carry the whole narration without it.")
    target = build / VIDEO
    showcase.ffmpeg(*inputs, "-filter_complex", ";".join(chain), *maps, "-c:v", "libx264", "-preset", "slow", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-r", str(rules.FPS), *audio, "-t", f"{total:.3f}", "-movflags", "+faststart",
                    "-map_metadata", "-1", str(target))
    info = showcase.probe(target)
    seconds = float(info["duration"])
    ok = ((info["codec_name"], info["width"], info["height"], info["pix_fmt"]) == ("h264", str(W), str(H), "yuv420p")
          and rules.MIN_SECONDS <= seconds <= rules.MAX_SECONDS)
    if not ok:
        print(f"make.py assemble: the result is not H.264 {W}×{H} yuv420p of {rules.MIN_SECONDS}–{rules.MAX_SECONDS} s: {info}",
              file=sys.stderr)
        return 1
    timed = build / TIMED
    timed.write_text(timed_narration(parsed, records, seconds, voice), encoding="utf-8")
    problems = rules.validate(rules.parse(timed.read_text(encoding="utf-8")))
    if problems:
        print("make.py assemble: the timed script does not hold: " + "; ".join(problems), file=sys.stderr)
        return 1
    for made, name in ((target, VIDEO), (timed, TIMED)):       # the work folder may be on another disk: copy, then rename
        landing = out / f".{name}.part"
        shutil.copyfile(made, landing)
        os.replace(landing, out / name)
    say(f"{out / VIDEO}: {seconds:.1f} s, {(out / VIDEO).stat().st_size / 1e6:.1f} MB; {out / TIMED}")
    return 0


def cmd_clean(a: argparse.Namespace) -> int:
    work = a.work.resolve()
    if work == ROOT or ROOT.is_relative_to(work):
        print(f"make.py clean: {work} holds this repository; nothing is removed", file=sys.stderr)
        return 2
    shutil.rmtree(work, ignore_errors=True)
    say(f"removed {work}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record and assemble the five-minute demo video of al.")
    sub = ap.add_subparsers(dest="verb", required=True)
    sc = sub.add_parser("scene", help="record scenes into DIR/clips")
    sc.add_argument("ids", nargs="+", metavar="ID")
    sc.add_argument("--work", type=Path, required=True, metavar="DIR")
    sc.add_argument("--own-plan", type=Path, default=None, metavar="PATH")
    sc.add_argument("--rehearse", action="store_true", help="run the same steps without video")
    sc.set_defaults(run=cmd_scene)
    ass = sub.add_parser("assemble", help="join the clips into OUT/al-demo.mp4 and write OUT/narration.md")
    ass.add_argument("--work", type=Path, required=True, metavar="DIR")
    ass.add_argument("--out", type=Path, default=None, metavar="OUT", help=f"default: {PROOF_OUT} in a run, else {MEDIA_OUT}")
    ass.add_argument("--no-voice", action="store_true")
    ass.set_defaults(run=cmd_assemble)
    cl = sub.add_parser("clean", help="remove DIR")
    cl.add_argument("--work", type=Path, required=True, metavar="DIR")
    cl.set_defaults(run=cmd_clean)
    a = ap.parse_args(argv)
    return a.run(a)


if __name__ == "__main__":
    sys.exit(main())
