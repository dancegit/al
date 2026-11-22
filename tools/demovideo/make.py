#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10", "playwright>=1.45", "pytest>=8"]
# ///
"""Record the five-minute demo video of al, one scene at a time, and assemble it.

  uv run tools/demovideo/make.py scene ID… --work DIR [--story {rota,testbed}] [--rehearse]
  uv run tools/demovideo/make.py assemble --work DIR [--story {rota,testbed}] [--out OUT] [--voice {auto,elevenlabs,kokoro,local,none}]
  uv run tools/demovideo/make.py forgejo {up,down} --work DIR [--port N]
  uv run tools/demovideo/make.py clean --work DIR

There are two stories (tools/demovideo/stories.py). `rota`, the default, is the hospital rota app and the
`cart` demo library: narration.md, scenes.py, DIR/clips, OUT/al-demo.mp4. `testbed` (--story testbed) is
the same seven parts on the pinned public codebase flask, delivered to a local Forgejo:
narration-testbed.md, scenes_testbed.py, DIR/clips-testbed, OUT/al-demo-testbed.mp4 and
OUT/narration-testbed.md. What follows describes `rota`; `testbed` differs only in those names. `forgejo up`
starts the throwaway Forgejo of the testbed story in rootless podman on the loopback interface (port 3910,
or --port) with an empty repository demo/flask, and records it in DIR; `forgejo down` removes the container
and its data (tools/demovideo/forgejo.py). Without a reachable instance the testbed scenes that need one
stop: the story never fakes its pull request. `clean` refuses while an instance is recorded in DIR.

`scene` records the scenes named (ids of tools/demovideo/narration.md) into DIR/clips/ID.mp4 (H.264,
1280×720, 15 fps, no audio, exactly the scene's seconds) with DIR/clips/ID.json beside it: the real
seconds, the speed, the held seconds, the caption times as rendered, what was blanked and which
stand-ins were used. --rehearse runs the same steps without video.

`assemble` joins the clips in script order, adds a voice that says each caption's spoken line (the
`  > ` lines of narration.md), checks the result and writes OUT/al-demo.mp4 and OUT/narration.md (the
script with the times in the final video). --voice auto (the default) uses ElevenLabs when
ELEVENLABS_API_KEY is set, else Kokoro when its model files are already cached, else espeak-ng when it is
installed, else no voice. --voice elevenlabs, --voice kokoro, --voice local (espeak-ng) and --voice none
(or --no-voice) choose one. Kokoro runs on this machine through kit/runtime/voice/say.py, which downloads
its model once into the user's cache outside the repository (never when `auto` chose it). Each line starts
VOICE_LEAD (0.3 s) after its caption; voiced lines are cached in DIR/voice-cache and fitted to their
captions (tools/demovideo/tts.py); the mix is loudness-normalised to about -16 LUFS, AAC 128k, and the
closing card names the synthetic voice. The scene lengths never change. OUT is .al-proof/demo when the repository has a .al-proof folder (a run's proof
folder), else media/demo-video; both are ignored by git. `clean` removes DIR.

Every scene runs in a neutral environment of its own (tools/showcase/make.py::neutral_env) on invented
names. Before a frame is encoded, every terminal transcript and the dashboard's page text at each
caption is checked with rules.find_private; a private match is blanked with █ and listed, and one that
is still there stops the scene. A command that exits other than the scene expects, or a page that
reports an error, records nothing. Nothing is uploaded or sent anywhere, except each spoken line (not the
captions) to ElevenLabs when that voice is used (the key goes only in the request header): a person
reviews the video.

Needs uv, ffmpeg and ffprobe, Chromium or Chrome, DejaVu Sans and DejaVu Sans Mono, and racket; espeak-ng,
Kokoro (say.py) and ELEVENLABS_API_KEY are optional.
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
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SHOWCASE = ROOT / "tools" / "showcase"
for _folder in (str(ROOT / "tools"), str(SHOWCASE), str(HERE)):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

import forgejo  # noqa: E402
import rules  # noqa: E402
import scenes  # noqa: E402
import stories  # noqa: E402
import tts  # noqa: E402

NARRATION = HERE / "narration.md"
VIDEO, TIMED = "al-demo.mp4", "narration.md"
PROOF_OUT, MEDIA_OUT = ".al-proof/demo", "media/demo-video"
MAX_WORK_BYTES = 2_500_000_000
W, H = rules.SIZE
LEAD = 0.2                       # seconds of a browser recording kept before its first caption
BADGE_FROM = 1.05                # a part played faster than this gets the badge
VOICE_RATES = (165, 190, 215)    # espeak-ng words a minute: the next one when a caption is too short for the last
MUTE = f"ffmpeg -i {VIDEO} -c:v copy -an al-demo-silent.mp4"
VOICE_LEAD = 0.3                 # seconds between a caption's start and its spoken line
AUDIO = ["-c:a", "aac", "-b:a", "128k", "-ac", "1"]
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
FADE = 0.35                      # seconds of fade at a part's first and last frame
CAPTION_BG = (13, 17, 23, 215)


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


def script(story: str = stories.DEFAULT) -> rules.Script:
    narration = stories.get(story).narration
    parsed = rules.parse(narration.read_text(encoding="utf-8"))
    problems = rules.validate(parsed)
    if problems:
        raise SystemExit(f"make.py: tools/demovideo/{narration.name}: " + "; ".join(problems))
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


@lru_cache(maxsize=None)
def sans(size: int, bold: bool = True):
    """DejaVu Sans (bold), or the terminal's mono face when fontconfig finds no such file."""
    from PIL import ImageFont
    import term
    try:
        found = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold" if bold else "DejaVu Sans"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        found = ""
    if found and Path(found).is_file():
        return ImageFont.truetype(found, size)
    return term.font(size, bold)


def chapter_of(parsed: rules.Script, scene_id: str) -> str:
    part = next(p for p in parsed.parts if any(s.id == scene_id for s in p.scenes))
    return f"{part.number} · {part.title}"


def caption_png(text: str, chapter: str):
    """The lower third: an RGBA 1280×64 band with the blue bar at its left (as term.caption_strip, so that
    verify.py's band check holds), the chapter tag and the caption in white. ValueError when it does not fit."""
    from PIL import Image, ImageDraw
    import term
    img = Image.new("RGBA", (W, rules.STRIP_H), CAPTION_BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 6, rules.STRIP_H], fill=term.BLUE)
    tag, face = sans(16, False), sans(26, True)
    x = 22
    if chapter:
        d.text((x, rules.STRIP_H // 2), chapter, font=tag, fill=term.DIM, anchor="lm")
        x += int(tag.getlength(chapter)) + 24
    if "\n" in text or x + face.getlength(text) > 1240:
        raise ValueError(f"caption does not fit one line of 1240 px after its chapter tag: {text!r}")
    d.text((x, rules.STRIP_H // 2), text, font=face, fill="#ffffff", anchor="lm")
    return img


def fade_filter(parsed: rules.Script, scene_id: str, seconds: float) -> str:
    """A fade in at the first scene of a part and a fade out at its last; the captions are laid over it."""
    part = next(p for p in parsed.parts if any(s.id == scene_id for s in p.scenes))
    out = ""
    if part.scenes[0].id == scene_id:
        out += f",fade=t=in:st=0:d={FADE:g}"
    if part.scenes[-1].id == scene_id:
        out += f",fade=t=out:st={seconds - FADE:.3f}:d={FADE:g}"
    return out


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


def record_scene(scene: rules.Scene, parsed: rules.Script, work: Path, values: dict, rehearse: bool,
                 story: str = stories.DEFAULT) -> int:
    showcase = _showcase()
    chosen = stories.get(story)
    env_root = work / f"env-{scene.id}"
    shutil.rmtree(env_root, ignore_errors=True)
    env_root.mkdir(parents=True)
    showcase.neutral_env(env_root)
    scratch = env_root / "scratch"
    scratch.mkdir()
    ctx = scenes.Ctx(scene, work, env_root / "home" / "work", values, rehearse=rehearse)
    parts = stories.scenes_of(chosen).SCENES[scene.id](ctx)
    firsts = [part.first for part in parts]
    if firsts != sorted(firsts) or firsts[0] != 0:
        raise scenes.SceneStopped(f"{scene.id}: the parts do not start with captions in script order: {firsts}")
    cued = [c for part in parts for c in ([s.cue for s in part.segments if s.cue is not None] if part.kind == "frames"
                                          else [caption for _at, caption in part.cues])]
    if cued != sorted(set(cued)) or (cued and cued[-1] >= len(scene.captions)):
        raise scenes.SceneStopped(f"{scene.id}: the cues {cued} are not in the order of the script's captions")
    chapter = chapter_of(parsed, scene.id)
    try:
        bands = [caption_png(caption.text, chapter) for caption in scene.captions]
    except ValueError as error:
        raise scenes.SceneStopped(f"{scene.id}: {error}") from None
    if rehearse:
        say(f"    rehearsed: {len(parts)} part(s), cues {cued}; blanked {ctx.blanked or 'nothing'}")
        return 0
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
                f"edit tools/demovideo/{chosen.narration.name} to what was recorded")
        if caption["end"] - caption["start"] < rules.MIN_CAPTION_SECONDS:
            raise scenes.SceneStopped(f"{scene.id}: caption {i} would be on screen for {caption['end'] - caption['start']:.1f} s; "
                                      f"give it more time in tools/demovideo/{chosen.narration.name}")
    inputs: list[str] = []
    for path in files:
        inputs += ["-i", str(path)]
    chain = ["".join(f"[{i}:v]" for i in range(len(files))) + f"concat=n={len(files)}:v=1:a=0,fps={rules.FPS},setsar=1"
             f"{fade_filter(parsed, scene.id, scene.seconds)}[v0]"]
    label, index = "v0", len(files)
    for i, caption in enumerate(captions):
        strip = scratch / f"caption-{i:02d}.png"
        bands[i].save(strip)
        inputs += ["-i", str(strip)]
        until = caption["end"] if i + 1 < len(captions) else scene.seconds + 5
        chain.append(f"[{label}][{index}:v]overlay=0:H-h:enable='gte(t,{caption['start']:.3f})*lt(t,{until:.3f})'[v{index}]")
        label, index = f"v{index}", index + 1
    fastest = max(info["speed"] for info in encoded)
    if fastest > BADGE_FROM:
        inputs += ["-i", str(badge_png(fastest, scratch / "badge.png"))]
        chain.append(f"[{label}][{index}:v]overlay=W-w-16:44[v{index}]")
        label = f"v{index}"
    clips = work / chosen.clips
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
    parsed = script(a.story)
    recorders = stories.scenes_of(a.story).SCENES
    unknown = [name for name in a.ids if name not in recorders or name not in [s.id for s in parsed.scenes]]
    if unknown:
        print(f"make.py: no scene {unknown} in story {a.story}; scenes: {', '.join(s.id for s in parsed.scenes)}", file=sys.stderr)
        return 2
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"make.py: {tool} is not on PATH; install ffmpeg (it brings ffprobe)", file=sys.stderr)
            return 2
    work = a.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    stop = too_big(work)
    if stop:
        print(stop, file=sys.stderr)
        return 1
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
            record_scene(scene, parsed, work, values, a.rehearse, a.story)
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


def voice_lines(lines: list[tuple[str, float, float]], build: Path, cache: Path,
                backend: str) -> tuple[list[tuple[Path, float]], str]:
    """Each line (spoken text, its caption's start in the video, seconds on screen) voiced into a WAV of
    `build`, fitted to its seconds less VOICE_LEAD and placed VOICE_LEAD after its caption: the (wav, start)
    list to mix and the description of the voice for `## The voice`."""
    delays: list[tuple[Path, float]] = []
    if backend == "local":
        rates = []
        for n, (text, start, seconds) in enumerate(lines):
            wav = build / f"{n:02d}.wav"
            rates.append(speak(text, wav, seconds - VOICE_LEAD))
            delays.append((wav, start + VOICE_LEAD))
        return delays, (f"One mono audio stream: each caption's spoken line read by espeak-ng on this machine (a synthetic voice; "
                        f"nothing was sent anywhere), at {min(rates)} to {max(rates)} words a minute. The captions carry the "
                        f"narration without it.")
    if backend not in ("elevenlabs", "kokoro"):
        raise tts.TtsError(f"no voice backend {backend!r}")
    kokoro = backend == "kokoro"
    settings = tts.KokoroSettings.from_env(os.environ) if kokoro else tts.Settings.from_env(os.environ)
    limit = tts.KOKORO_MAX_TEMPO if kokoro else tts.MAX_TEMPO
    misfits, made, cached, tempos = [], 0, 0, []
    for n, (text, start, seconds) in enumerate(lines):
        if kokoro:
            audio, hit = tts.kokoro_line(text, cache, settings)      # a failed line stops here
        else:
            audio, hit = tts.fetch_line(text, cache, settings)       # a failed request stops here
        cached, made = cached + hit, made + (not hit)
        wav = build / f"line-{n:03d}.wav"
        try:
            tempos.append(tts.fit_line(audio, wav, seconds - VOICE_LEAD, max_tempo=limit))
        except tts.TtsError as error:
            misfits.append(f"{text!r}: {error}")
            continue
        delays.append((wav, start + VOICE_LEAD))
    if misfits:
        raise tts.TtsError("these spoken lines are too long to say in their caption's time; shorten them in "
                           "tools/demovideo/narration.md: " + "; ".join(misfits))
    faster = [t for t in tempos if t > 1.0]
    fitted = (f"{len(faster)} line(s) were played up to ×{max(tempos, default=1.0):.2f} faster to fit their caption. "
              f"The captions carry the narration without it.")
    if kokoro:
        say(f"voice: Kokoro {settings.voice}, {made} line(s) made, {cached} from the cache")
        return delays, (f"One mono audio stream: each caption's spoken line read by the Kokoro voice {settings.voice} on this "
                        f"machine (a synthetic voice; nothing was sent anywhere), through kit/runtime/voice/say.py. " + fitted)
    say(f"voice: ElevenLabs {settings.voice_id} ({settings.model}), {made} line(s) fetched, {cached} from the cache")
    return delays, (f"One mono audio stream: each caption's spoken line read by the ElevenLabs voice {settings.voice_id} "
                    f"(model {settings.model}, a synthetic voice). Only the spoken lines were sent to the ElevenLabs service. " + fitted)


def voice_note(backend: str, detail: str) -> str:
    """The line on the closing card that names the synthetic voice; empty without a voice."""
    if backend == "kokoro":
        return f"The voice is synthetic: Kokoro ({detail})"
    if backend == "elevenlabs":
        return f"The voice is synthetic: ElevenLabs (voice {detail})"
    if backend == "local":
        return "The voice is synthetic: espeak-ng"
    return ""


def voice_note_png(text: str, path: Path) -> Path:
    from PIL import Image, ImageDraw
    import term
    face = sans(20, False)
    width = int(face.getlength(text)) + 32
    img = Image.new("RGBA", (width, 40), (13, 17, 23, 0))
    d = ImageDraw.Draw(img)
    d.text((width // 2, 20), text, font=face, fill=term.DIM, anchor="mm")
    img.save(path)
    return path


def mix_filter(delays: list[tuple[Path, float]], total: float) -> list[str]:
    """The voice lines, each at its start, mixed into one stream of `total` seconds, loudness-normalised."""
    mix = [f"[{n}:a]aresample=44100,adelay={int(round(start * 1000))}:all=1[a{n}]" for n, (_wav, start) in enumerate(delays)]
    mix.append("".join(f"[a{n}]" for n in range(len(delays)))
               + f"amix=inputs={len(delays)}:normalize=0:dropout_transition=0,apad=whole_dur={total:.3f},"
               + f"{LOUDNORM},aresample=44100[a]")
    return mix


def timed_narration(parsed: rules.Script, records: dict[str, dict], total: float, voice: str,
                    narration: Path = NARRATION, video: str = VIDEO) -> str:
    """The committed script with the times of the final video (its spoken `  > ` lines copied as they are),
    then what a viewer should know."""
    lines, starts, at = [], {}, 0.0
    for scene in parsed.scenes:
        starts[scene.id] = at
        at += scene.seconds
    current, nth = None, 0
    for line in narration.read_text(encoding="utf-8").splitlines():
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
    mute = MUTE if video == VIDEO else f"ffmpeg -i {video} -c:v copy -an {Path(video).stem}-silent.mp4"
    lines += ["", "## The voice", "", voice, "", f"mute: {mute}", ""]
    return "\n".join(lines)


def cmd_assemble(a: argparse.Namespace) -> int:
    showcase = _showcase()
    work = a.work.resolve()
    stop = too_big(work)
    if stop:
        print(stop, file=sys.stderr)
        return 1
    story = stories.get(a.story)
    parsed = script(story.name)
    clips = work / story.clips
    missing = [s.id for s in parsed.scenes if not (clips / f"{s.id}.mp4").is_file() or not (clips / f"{s.id}.json").is_file()]
    if missing:
        chosen = "" if story.name == stories.DEFAULT else f" --story {story.name}"
        print(f"make.py assemble: no clip for {', '.join(missing)}: record with `make.py scene ID{chosen} --work {a.work}`",
              file=sys.stderr)
        return 1
    records = {s.id: json.loads((clips / f"{s.id}.json").read_text()) for s in parsed.scenes}
    stale = [s.id for s in parsed.scenes if records[s.id]["seconds"] != s.seconds
             or [c["text"] for c in records[s.id]["captions"]] != [c.text for c in s.captions]]
    if stale:
        print(f"make.py assemble: the script changed since {', '.join(stale)} was recorded: record again", file=sys.stderr)
        return 1
    total = parsed.seconds
    try:
        backend = "none" if a.no_voice else tts.choose(a.voice, os.environ, bool(shutil.which("espeak-ng")),
                                                         tts.kokoro_ready(os.environ))
    except tts.TtsError as error:
        print(f"make.py assemble: {error}", file=sys.stderr)
        return 2
    out = (a.out or default_out()).resolve()
    out.mkdir(parents=True, exist_ok=True)
    build = work / "assemble"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True)
    inputs: list[str] = []
    for scene in parsed.scenes:
        inputs += ["-i", str(clips / f"{scene.id}.mp4")]
    count = len(parsed.scenes)
    joined = "".join(f"[{i}:v]" for i in range(count)) + f"concat=n={count}:v=1:a=0,fps={rules.FPS}"
    chain = [joined + ",format=yuv420p[v]"]
    maps = ["-map", "[v]"]
    audio: list[str] = ["-an"]
    voice = "No voice: the captions carry the whole narration."
    if backend != "none":
        lines, at, note_at = [], 0.0, 0.0
        for scene in parsed.scenes:
            for i, caption in enumerate(records[scene.id]["captions"]):
                spoken = parsed.scene(scene.id).captions[i].say or caption["text"]
                lines.append((spoken, at + caption["start"], caption["end"] - caption["start"]))
            note_at = at + records[scene.id]["captions"][-1]["start"]
            at += scene.seconds
        try:
            delays, voice = voice_lines(lines, build, work / "voice-cache", backend)
        except tts.TtsError as error:
            print("make.py assemble: " + tts.scrub(str(error), os.environ.get(tts.KEY_VAR)).replace(
                "tools/demovideo/narration.md", f"tools/demovideo/{story.narration.name}"), file=sys.stderr)
            return 1
        mix_inputs: list[str] = []
        for wav, _start in delays:
            mix_inputs += ["-i", str(wav)]
        voice_wav = build / "voice.wav"
        showcase.ffmpeg(*mix_inputs, "-filter_complex", ";".join(mix_filter(delays, total)), "-map", "[a]", "-t", f"{total:.3f}",
                        "-ac", "1", str(voice_wav))
        inputs += ["-i", str(voice_wav)]
        maps += ["-map", f"{count}:a"]
        audio = AUDIO
        detail = (tts.KokoroSettings.from_env(os.environ).voice if backend == "kokoro"
                  else tts.Settings.from_env(os.environ).voice_id if backend == "elevenlabs" else "")
        note = voice_note(backend, detail)
        if note:
            index = len([x for x in inputs if x == "-i"])
            inputs += ["-i", str(voice_note_png(note, build / "voice-note.png"))]
            chain = [f"{joined}[vj]",
                     f"[vj][{index}:v]overlay=(W-w)/2:H-{rules.STRIP_H}-h-20:enable='gte(t,{note_at:.3f})',format=yuv420p[v]"]
    target = build / story.video
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
    timed = build / story.timed
    timed.write_text(timed_narration(parsed, records, seconds, voice, story.narration, story.video), encoding="utf-8")
    problems = rules.validate(rules.parse(timed.read_text(encoding="utf-8")))
    if problems:
        print("make.py assemble: the timed script does not hold: " + "; ".join(problems), file=sys.stderr)
        return 1
    for made, name in ((target, story.video), (timed, story.timed)):       # the work folder may be on another disk: copy, then rename
        landing = out / f".{name}.part"
        shutil.copyfile(made, landing)
        os.replace(landing, out / name)
    say(f"{out / story.video}: {seconds:.1f} s, {(out / story.video).stat().st_size / 1e6:.1f} MB; {out / story.timed}")
    return 0


def cmd_clean(a: argparse.Namespace) -> int:
    work = a.work.resolve()
    if work == ROOT or ROOT.is_relative_to(work):
        print(f"make.py clean: {work} holds this repository; nothing is removed", file=sys.stderr)
        return 2
    if (work / forgejo.STATE).exists():
        print(f"make.py clean: a Forgejo instance is recorded in {a.work}; run make.py forgejo down --work {a.work} first",
              file=sys.stderr)
        return 2
    shutil.rmtree(work, ignore_errors=True)
    say(f"removed {work}")
    return 0


def cmd_forgejo(a: argparse.Namespace) -> int:
    work = a.work.resolve()
    return forgejo.up(work, a.port) if a.action == "up" else forgejo.down(work)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record and assemble the five-minute demo video of al.")
    sub = ap.add_subparsers(dest="verb", required=True)
    sc = sub.add_parser("scene", help="record scenes into DIR/clips")
    sc.add_argument("ids", nargs="+", metavar="ID")
    sc.add_argument("--work", type=Path, required=True, metavar="DIR")
    sc.add_argument("--rehearse", action="store_true", help="run the same steps without video")
    sc.add_argument("--story", choices=tuple(stories.STORIES), default=stories.DEFAULT,
                    help="rota (the default): narration.md; testbed: flask on a local Forgejo, narration-testbed.md")
    sc.set_defaults(run=cmd_scene)
    ass = sub.add_parser("assemble", help="join the clips into OUT/al-demo.mp4 and write OUT/narration.md")
    ass.add_argument("--work", type=Path, required=True, metavar="DIR")
    ass.add_argument("--out", type=Path, default=None, metavar="OUT", help=f"default: {PROOF_OUT} in a run, else {MEDIA_OUT}")
    voices = ass.add_mutually_exclusive_group()
    voices.add_argument("--voice", choices=tts.BACKENDS, default="auto",
                        help="auto: ElevenLabs when ELEVENLABS_API_KEY is set, else Kokoro when its model files are cached, "
                             "else espeak-ng when installed, else none")
    voices.add_argument("--no-voice", action="store_true", help="the same as --voice none")
    ass.add_argument("--story", choices=tuple(stories.STORIES), default=stories.DEFAULT,
                     help="rota (the default) writes al-demo.mp4; testbed writes al-demo-testbed.mp4")
    ass.set_defaults(run=cmd_assemble)
    fj = sub.add_parser("forgejo", help="start (up) or remove (down) the throwaway local Forgejo of the testbed story")
    fj.add_argument("action", choices=("up", "down"))
    fj.add_argument("--work", type=Path, required=True, metavar="DIR")
    fj.add_argument("--port", type=int, default=forgejo.DEFAULT_PORT, metavar="N",
                    help=f"the loopback port it is published on (default {forgejo.DEFAULT_PORT})")
    fj.set_defaults(run=cmd_forgejo)
    cl = sub.add_parser("clean", help="remove DIR")
    cl.add_argument("--work", type=Path, required=True, metavar="DIR")
    cl.set_defaults(run=cmd_clean)
    a = ap.parse_args(argv)
    return a.run(a)


if __name__ == "__main__":
    sys.exit(main())
