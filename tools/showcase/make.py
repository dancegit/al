#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10", "playwright>=1.45", "pytest>=8"]
# ///
"""Record the showcase clips again: every frame is real output of this repository on fixtures with
invented names (tools/showcase/clips.py lists them).

  uv run tools/showcase/make.py [NAME …] [--out DIR] [--media DIR] [--keep-work]

NAME: a clip's number, name or stem (default: all). Per clip, in --out (default docs/showcase):
NN-name.gif (preview), NN-name.png (poster: the last frame) and, for terminal clips,
logs/NN-name.txt (the captured output); then contact-sheet.png from every poster present. The video
itself (H.264, 1280×720) is not kept in the tree: it goes to --media (default: the ignored folder
media/<release>/, tools/media.py) as showcase-NN-name.mp4, the name it has as a release asset, and
its size and sha256 are recorded in docs/media.json when --media is that default folder. A person
uploads the folder with tools/release_media.sh. Exit 1 when a file breaks a limit of clips.py.

Needs ffmpeg and ffprobe, DejaVu Sans Mono (or AL_SHOWCASE_FONT), and for browser clips Chrome or
Chromium (AL_DASHBOARD_CHROME); nothing is downloaded but uv's packages. Each clip runs in its own
temporary folder with a neutral HOME (paths show as ~/work), a neutral git identity and none of the
caller's AL_* settings; every transcript and page text is checked for private values first.
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import clips  # noqa: E402
import privacy  # noqa: E402

ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import media  # noqa: E402  (tools/media.py: where the videos are)


def video_path(media_dir: Path, clip: clips.Clip) -> Path:
    """Where a clip's MP4 is written: the media folder, under its release asset name."""
    return media_dir / media.asset_name(f"docs/showcase/{clip.stem}.mp4")
W, H = clips.SIZE
FIT_MAX = clips.MAX_SECONDS - 2          # aim inside the limits, not at them
FIT_MIN = clips.MIN_SECONDS + 2
LINKED_TOOLS = ("racket", "raco", "uv", "git", "python3", "sh")
KEEP_AL = ("AL_DASHBOARD_CHROME", "AL_SHOWCASE_FONT")
GIT_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_QUARANTINE_PATH", "GIT_CEILING_DIRECTORIES",
                 "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


def say(text: str) -> None:
    print(text, flush=True)


def ffmpeg(*args: str, stdin=None) -> None:
    cp = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], stdin=stdin, capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(f"ffmpeg {' '.join(args[:12])} … failed:\n{cp.stderr}")


def probe(path: Path) -> dict[str, str]:
    cp = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=codec_name,width,height,pix_fmt:format=duration", "-of", "default=noprint_wrappers=1",
                         str(path)], capture_output=True, text=True, check=True)
    return dict(line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line)


def duration(path: Path) -> float:
    return float(probe(path)["duration"])


# ------------------------------------------------------------------------------------------ environment

def neutral_env(root: Path) -> dict[str, str]:
    """Make os.environ neutral for one clip (the in-process dashboard reads it too): HOME=root/home
    with the work folder root/home/work, a git identity of its own, UTC, no AL_* setting but the
    browser and font ones, no GIT_DIR-style variable, PATH = ~/bin (links to the tools al runs) and
    the system folders. uv keeps its cache and its Pythons."""
    keep_uv = {}
    for name, argv in (("UV_CACHE_DIR", ["uv", "cache", "dir"]), ("UV_PYTHON_INSTALL_DIR", ["uv", "python", "dir"])):
        cp = subprocess.run(argv, capture_output=True, text=True)
        if cp.returncode == 0 and cp.stdout.strip():
            keep_uv[name] = cp.stdout.strip()
    found = {tool: shutil.which(tool) for tool in LINKED_TOOLS}
    home = root / "home"
    (home / "work").mkdir(parents=True, exist_ok=True)
    bin_dir = home / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool, path in found.items():
        if path:
            (bin_dir / tool).symlink_to(path)
    gitconfig = home / ".gitconfig-showcase"
    gitconfig.write_text("[user]\n\tname = Showcase\n\temail = showcase@example.org\n[init]\n\tdefaultBranch = main\n")
    for name in list(os.environ):
        if (name.startswith("AL_") and name not in KEEP_AL) or name in GIT_VARIABLES:
            del os.environ[name]
    os.environ.update(keep_uv)
    os.environ.update({
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"), "USER": "demo", "LOGNAME": "demo", "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC", "LANG": "C.UTF-8", "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin"})
    os.environ.pop("LC_ALL", None)
    return dict(os.environ)


# ------------------------------------------------------------------------------------------ recording

def strip_png(text: str, path: Path) -> Path:
    import term
    term.caption_strip(text).save(path)
    return path


def record_terminal(clip: clips.Clip, work: Path, scratch: Path, out: Path) -> Path:
    import term
    import terminal_clips
    transcript = terminal_clips.RECORDERS[clip.number](work)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    log = terminal_clips.transcript_text(transcript)
    privacy.check_private(log, terminal_clips.FORBIDDEN)
    (out / "logs" / f"{clip.stem}.txt").write_text(log)
    body = scratch / "body.mp4"
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                             "-r", str(clips.FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "8",
                             "-pix_fmt", "yuv420p", str(body)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    previous, raw = None, b""
    for frame in term.render_frames(transcript):
        if frame is not previous:
            raw, previous = frame.tobytes(), frame
        proc.stdin.write(raw)
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg (terminal frames) failed: {proc.stderr.read().decode() if proc.stderr else ''}")
    say(f"    {term.frame_count(transcript)} frames, {term.frame_count(transcript) / clips.FPS:.1f} s before fitting")
    return body


def record_browser(clip: clips.Clip, work: Path, scratch: Path) -> Path:
    import browser_clips
    webm, cues = browser_clips.RECORDERS[clip.number](work)
    if webm is None or not cues:
        raise RuntimeError(f"{clip.stem}: no recording")
    start = max(0.0, cues[0][0] - 0.8)                  # the blank page before the first view loaded is cut
    end = duration(webm)
    inputs, chain, label = ["-ss", f"{start:.3f}", "-i", str(webm)], [f"[0:v]fps={clips.FPS},scale={W}:{H}[v0]"], "v0"
    for i, (at, text) in enumerate(cues):
        until = cues[i + 1][0] if i + 1 < len(cues) else end + 1
        inputs += ["-i", str(strip_png(text, scratch / f"cue-{i:02d}.png"))]
        nxt = f"v{i + 1}"
        chain.append(f"[{label}][{i + 1}:v]overlay=0:H-h:enable='between(t,{at - start:.3f},{until - start:.3f})'[{nxt}]")
        label = nxt
    body = scratch / "body.mp4"
    ffmpeg(*inputs, "-filter_complex", ";".join(chain), "-map", f"[{label}]", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "8", "-pix_fmt", "yuv420p", "-an", str(body))
    say(f"    {len(cues)} cues, {duration(body):.1f} s before fitting")
    return body


def record(clip: clips.Clip, work: Path, scratch: Path, out: Path) -> Path:
    if clip.kind == "terminal":
        return record_terminal(clip, work, scratch, out)
    return record_browser(clip, work, scratch)


# ------------------------------------------------------------------------------------------ encoding

def fit(seconds: float) -> str:
    """The filter that brings the body into the limits with the card: faster when too long, the last
    frame held when too short."""
    total = clips.CARD_SECONDS + seconds
    if total > FIT_MAX:
        return f"setpts=PTS*{(FIT_MAX - clips.CARD_SECONDS) / seconds:.5f},fps={clips.FPS}"
    if total < FIT_MIN:
        return f"fps={clips.FPS},tpad=stop_mode=clone:stop_duration={FIT_MIN - total:.3f}"
    return f"fps={clips.FPS}"


def encode(clip: clips.Clip, body: Path, scratch: Path, media_dir: Path) -> Path:
    import term
    card = scratch / "card.png"
    term.caption_card(clip.card).save(card)
    target = video_path(media_dir, clip)
    graph = (f"[0:v]fps={clips.FPS},format=yuv420p,setsar=1[c];[1:v]{fit(duration(body))},scale={W}:{H},"
             f"format=yuv420p,setsar=1[b];[c][b]concat=n=2:v=1:a=0,scale=out_range=tv,format=yuv420p[v]")
    crf = 26
    while True:
        ffmpeg("-loop", "1", "-framerate", str(clips.FPS), "-t", str(clips.CARD_SECONDS), "-i", str(card), "-i", str(body),
               "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "slow", "-tune", "animation",
               "-crf", str(crf), "-pix_fmt", "yuv420p", "-color_range", "tv", "-r", str(clips.FPS), "-movflags", "+faststart",
               "-an", str(target))
        if target.stat().st_size < clips.MAX_MP4:
            return target
        crf += 3
        if crf > 40:
            raise RuntimeError(f"{target.name} stays over {clips.MAX_MP4} bytes even at CRF 40")


def poster(mp4: Path, target: Path) -> Path:
    """The last frame of `mp4` as `target` (docs/showcase/NN-name.png)."""
    path = target
    ffmpeg("-sseof", "-0.2", "-i", str(mp4), "-frames:v", "1", "-update", "1", str(path))
    return path


def gif(mp4: Path, target: Path) -> Path:
    """A smaller preview of `mp4` as `target` (docs/showcase/NN-name.gif)."""
    path = target
    for width, fps in ((640, 8), (560, 6), (480, 5)):
        ffmpeg("-i", str(mp4), "-vf", f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
               "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
               str(path))
        if path.stat().st_size < clips.MAX_GIF:
            return path
    raise RuntimeError(f"{path.name} stays over {clips.MAX_GIF} bytes even at 480 px, 5 fps")


def contact_sheet(out: Path) -> Path | None:
    posters = [out / f"{c.stem}.png" for c in clips.CLIPS if (out / f"{c.stem}.png").is_file()]
    if not posters:
        return None
    cols = 3
    rows = math.ceil(len(posters) / cols)
    cells = rows * cols
    inputs: list[str] = []
    for p in posters:
        inputs += ["-i", str(p)]
    chain = [f"[{i}:v]scale=640:360,setsar=1[p{i}]" for i in range(len(posters))]
    for i in range(len(posters), cells):
        chain.append(f"color=c=black:s=640x360:d=1,setsar=1[p{i}]")
    layout = "|".join(f"{(i % cols) * 640}_{(i // cols) * 360}" for i in range(cells))
    chain.append("".join(f"[p{i}]" for i in range(cells)) + f"xstack=inputs={cells}:layout={layout}:fill=black[s]")
    target = out / "contact-sheet.png"
    ffmpeg(*inputs, "-filter_complex", ";".join(chain), "-map", "[s]", "-frames:v", "1", "-update", "1", str(target))
    return target


# ------------------------------------------------------------------------------------------ limits

def report(out: Path, media_dir: Path) -> bool:
    """Print every file with its size (and duration); False when a limit is broken. The MP4 is looked
    for in the media folder, the GIF and the poster in `out`."""
    ok = True
    total = 0
    for clip in clips.CLIPS:
        for suffix, limit in ((".mp4", clips.MAX_MP4), (".gif", clips.MAX_GIF), (".png", None)):
            path = video_path(media_dir, clip) if suffix == ".mp4" else out / f"{clip.stem}{suffix}"
            if not path.is_file():
                continue
            size = path.stat().st_size
            extra = ""
            if suffix == ".mp4":
                seconds = duration(path)
                extra = f"  {seconds:5.1f} s"
                if not clips.MIN_SECONDS <= seconds <= clips.MAX_SECONDS:
                    ok, extra = False, extra + f"  (outside {clips.MIN_SECONDS}–{clips.MAX_SECONDS} s)"
            if limit and size >= limit:
                ok, extra = False, extra + f"  (over {limit} bytes)"
            say(f"  {path.name}  {size / 1e6:6.2f} MB{extra}")
    for path in sorted(out.rglob("*")):
        if path.is_file():
            total += path.stat().st_size
    pngs = len(list(out.glob("*.png")))
    say(f"  total {total / 1e6:.2f} MB in {out}, {pngs} PNG file(s)")
    if total >= clips.MAX_TOTAL:
        say(f"  over the total of {clips.MAX_TOTAL} bytes")
        ok = False
    if pngs > 12:
        say("  more than 12 PNG files (the showcase-stills artefact keeps 12)")
        ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record the showcase clips again.")
    ap.add_argument("names", nargs="*", metavar="NAME")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "showcase")
    ap.add_argument("--media", type=Path, default=None, help="where the MP4 files go, as showcase-NN-name.mp4 "
                    "(default: the ignored media folder, whose files docs/media.json records)")
    ap.add_argument("--keep-work", action="store_true", help="keep each clip's temporary folder and say where")
    a = ap.parse_args(argv)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"make.py: {tool} is not on PATH; install ffmpeg (it brings ffprobe)", file=sys.stderr)
            return 2
    try:
        chosen = clips.by_name(a.names)
    except KeyError as error:
        print(f"make.py: {error.args[0]}", file=sys.stderr)
        return 2
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    default_media = media.media_dir().resolve()                 # before the environment changes
    media_dir = (a.media or default_media).resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    forbidden = privacy.forbidden_values(Path.home())          # before HOME changes
    original = dict(os.environ)
    import browser_clips
    import terminal_clips
    terminal_clips.FORBIDDEN[:] = forbidden
    browser_clips.FORBIDDEN[:] = forbidden
    for clip in chosen:
        say(f"{clip.stem} ({clip.kind}): {clip.card}")
        root = Path(tempfile.mkdtemp(prefix=f"al-{clip.number:02d}-"))
        try:
            neutral_env(root)
            work = root / "home" / "work"
            scratch = root / "scratch"
            scratch.mkdir()
            body = record(clip, work, scratch, out)
            mp4 = encode(clip, body, scratch, media_dir)
            poster(mp4, out / f"{clip.stem}.png")
            gif(mp4, out / f"{clip.stem}.gif")
            if media_dir == default_media:          # a recording elsewhere is a rehearsal: the manifest is not touched
                media.record(mp4, was=f"docs/showcase/{clip.stem}.mp4")
        finally:
            os.environ.clear()
            os.environ.update(original)
            if a.keep_work:
                say(f"    kept: {root}")
            else:
                shutil.rmtree(root, ignore_errors=True)
    contact_sheet(out)
    return 0 if report(out, media_dir) else 1


if __name__ == "__main__":
    sys.exit(main())
