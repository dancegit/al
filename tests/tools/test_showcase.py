"""The showcase (tools/showcase, docs/showcase): the terminal renderer and the caption images are
deterministic and sized right, the privacy check refuses what would identify a machine or a person,
the manifest is consistent and every `al` command it shows is one the tool accepts, the committed
clips keep their limits, and the README lists them. One slow test records a clip end to end.

    uv run --with pytest --with pyyaml --with jsonschema --with pillow pytest tests/tools/test_showcase.py -q
    AL_SHOWCASE_RECORD=1 uv run --with pytest --with pyyaml --with jsonschema --with pillow pytest tests/tools/test_showcase.py -q -m slow
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import AL, ROOT

SHOWCASE = ROOT / "tools" / "showcase"
DOCS = ROOT / "docs" / "showcase"
sys.path.insert(0, str(SHOWCASE))

import clips  # noqa: E402
import privacy  # noqa: E402


def _term():
    pytest.importorskip("PIL")
    import term
    return term


def _fixed():
    term = _term()
    doctor = "\n".join(f"line {i} of al doctor" for i in range(1, 10)) + "\n"
    return term, term.Transcript([term.Step("al doctor notes.al --agent mock", doctor, "where al found each tool"),
                                  term.Step("git -C notes status --short", "", "nothing changed")], "al — test")


# ------------------------------------------------------------------------------ renderer and captions

def test_frame_count_and_size_for_a_fixed_transcript():
    term, t = _fixed()
    # by hand: 31 characters → 16 typing frames, 6 held, 9 lines → 3 frames, 22 held; 27 characters → 14, 6, 0, 22; 45 at the end
    expected = (16 + 6 + 3 + 22) + (14 + 6 + 0 + 22) + 45
    assert (term.CHARS_PER_FRAME, term.LINES_PER_FRAME, term.HOLD_AFTER_TYPE, term.HOLD_AFTER_OUTPUT, term.HOLD_END) == (2, 3, 6, 22, 45)
    assert term.frame_count(t) == expected
    frames = list(term.render_frames(t))
    assert len(frames) == expected
    assert all(f.size == (1280, 720) and f.mode == "RGB" for f in frames)


def test_a_still_is_byte_identical_when_rendered_twice():
    term, t = _fixed()
    first = list(term.render_frames(t))[30].tobytes()
    second = list(term.render_frames(t))[30].tobytes()
    assert first == second
    other = term.Transcript([term.Step("al doctor notes.al --agent mock", "something else\n" * 9, "where al found each tool"),
                             t.steps[1]], "al — test")
    assert list(term.render_frames(other))[30].tobytes() != first


def test_caption_card_is_one_centred_line():
    term = _term()
    card = term.caption_card("A run's documents, its artefacts, and switching a model")
    assert card.size == (1280, 720) and card.mode == "RGB"
    assert card.tobytes() == term.caption_card("A run's documents, its artefacts, and switching a model").tobytes()
    background = card.getpixel((0, 0))
    band = card.crop((40, 330, 1240, 390))
    assert len(band.getcolors(maxcolors=1 << 20)) > 1
    for corner in ((0, 0, 200, 120), (1080, 0, 1280, 120), (0, 600, 200, 720), (1080, 600, 1280, 720)):
        assert [color for _n, color in card.crop(corner).getcolors(maxcolors=1 << 20)] == [background]
    with pytest.raises(ValueError):
        term.caption_card("far too long " * 12)
    strip = term.caption_strip("a caption")
    assert strip.size == (1280, 64) and strip.mode == "RGBA"
    with pytest.raises(ValueError):
        term.caption_strip("far too long " * 12)


# ------------------------------------------------------------------------------ privacy

def test_privacy_check_rejects_private_text():
    home = "/srv/people/someone-real"
    forbidden = [home, "box-17.corp"]
    for text in (f"{home}/work/notes.al", "see /home/someone/x", "/Users/someone", "from 10.1.2.3 port 22",
                 "mail someone@corp.example.net", "ssh box-17.corp"):
        with pytest.raises(privacy.PrivateText):
            privacy.check_private(text, forbidden)
    for text in ("http://127.0.0.1:8765/", "by ada@example.org", "fixture@example.invalid", "tunnel to testhost.invalid",
                 "~/work/notes.al", "listening on 0.0.0.0", "t@t", "uv 0.13.8"):
        assert privacy.check_private(text, forbidden) == text


def test_committed_transcripts_are_private():
    logs = sorted((DOCS / "logs").glob("*.txt"))
    assert logs, "docs/showcase/logs holds the terminal clips' transcripts"
    forbidden = privacy.forbidden_values()
    for log in logs:
        privacy.check_private(log.read_text(encoding="utf-8"), forbidden)


# ------------------------------------------------------------------------------ manifest

def test_manifest_names_and_numbers():
    stems = [c.stem for c in clips.CLIPS]
    assert len(set(stems)) == len(stems) and all(re.fullmatch(r"\d\d-[a-z0-9-]+", s) for s in stems)
    assert [c.number for c in clips.CLIPS] == list(range(1, len(clips.CLIPS) + 1)) == list(range(1, 10))
    for c in clips.CLIPS:
        assert c.kind in clips.KINDS
        assert bool(c.commands) == (c.kind == "terminal"), c.stem
        assert 0 < len(c.card) <= clips.MAX_CARD_CHARS and "\n" not in c.card, c.stem
        assert c.proves and c.sentence and c.guide
    assert [c.stem for c in clips.by_name(["05", "delivery", "05-delivery", "7"])] == ["05-delivery", "07-adopt"]
    with pytest.raises(KeyError):
        clips.by_name(["nope"])


def _slug(heading: str) -> str:
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _headings(markdown: str) -> list[str]:
    """ATX headings outside code fences, as GitHub renders them: a fence opened by ``` (or ~~~) closes
    only on a line of the same fence with no info string after it."""
    found, fence = [], None
    for line in markdown.splitlines():
        stripped = line.strip()
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + ",}", stripped):
                fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opening:
            fence = opening.group(1)
            continue
        heading = re.match(r"^ {0,3}#{1,6} (.+?)\s*#*\s*$", line)
        if heading:
            found.append(heading.group(1))
    return found


def _readme_section() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("\n## More to see\n")
    end = text.index("\n## ", start + 5)
    return text[start:end]


def test_guide_anchors_exist():
    slugs = {_slug(h) for h in _headings((ROOT / "docs" / "GUIDE.md").read_text(encoding="utf-8"))}
    links = [c.guide for c in clips.CLIPS] + list(clips.MORE_GUIDE.values())
    links += re.findall(r"docs/GUIDE\.md#[\w-]+", _readme_section())
    anchors = {link.split("#", 1)[1] for link in links if link.startswith("docs/GUIDE.md#")}
    assert anchors and not anchors - slugs, sorted(anchors - slugs)
    showcase = {_slug(h) for h in _headings((DOCS / "README.md").read_text(encoding="utf-8"))}
    for link in links:
        if link.startswith("docs/showcase/README.md#"):
            assert link.split("#", 1)[1] in showcase, link


def test_every_shown_al_command_is_accepted():
    shown = [clips.fill(argv, clips.SAMPLES) for c in clips.CLIPS for argv in c.commands if argv[0] == "al"]
    assert len(shown) >= 10
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    for argv in shown:
        cp = subprocess.run([str(AL), *argv[1:], "--help"], capture_output=True, text=True, env=env, timeout=120)
        assert cp.returncode == 0, (argv, cp.stdout, cp.stderr)
        for flag in (a for a in argv if a.startswith("--")):
            assert flag in cp.stdout, (argv, flag)


# ------------------------------------------------------------------------------ the committed clips

def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR"
    return struct.unpack(">II", head[16:24])


def test_every_clip_has_its_three_files_within_limits():
    for c in clips.CLIPS:
        mp4, gif, png = (DOCS / f"{c.stem}{s}" for s in (".mp4", ".gif", ".png"))
        assert mp4.is_file() and gif.is_file() and png.is_file(), c.stem
        assert mp4.stat().st_size < clips.MAX_MP4 and gif.stat().st_size < clips.MAX_GIF, c.stem
        assert _png_size(png) == clips.SIZE, c.stem
        if c.kind == "terminal":
            assert (DOCS / "logs" / f"{c.stem}.txt").is_file(), c.stem
    assert (DOCS / "contact-sheet.png").is_file()
    assert sum(p.stat().st_size for p in DOCS.rglob("*") if p.is_file()) < clips.MAX_TOTAL
    assert len(list(DOCS.glob("*.png"))) <= 12


def test_clips_are_h264_720p_and_10_to_40_seconds():
    if not shutil.which("ffprobe"):
        pytest.skip("no ffprobe")
    for c in clips.CLIPS:
        cp = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=codec_name,width,height,pix_fmt:format=duration", "-of", "default=noprint_wrappers=1",
                             str(DOCS / f"{c.stem}.mp4")], capture_output=True, text=True, check=True)
        info = dict(line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line)
        assert (info["codec_name"], info["width"], info["height"], info["pix_fmt"]) == ("h264", "1280", "720", "yuv420p"), c.stem
        assert clips.MIN_SECONDS <= float(info["duration"]) <= clips.MAX_SECONDS, (c.stem, info["duration"])


def test_readme_more_to_see_lists_every_clip():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings[headings.index("Watching the agents work") + 1] == "More to see"
    section = _readme_section()
    for c in clips.CLIPS:
        assert f"docs/showcase/{c.stem}.gif" in section and f"docs/showcase/{c.stem}.mp4" in section, c.stem
    showcase = (DOCS / "README.md").read_text(encoding="utf-8")
    for c in clips.CLIPS:
        assert re.search(rf"^### {re.escape(c.stem)}$", showcase, re.M), c.stem


# ------------------------------------------------------------------------------ recording (slow)

@pytest.mark.slow
def test_recording_one_terminal_clip(tmp_path):
    if os.environ.get("AL_SHOWCASE_RECORD") != "1":
        pytest.skip("records with ffmpeg: set AL_SHOWCASE_RECORD=1")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("no ffmpeg")
    pytest.importorskip("PIL")
    out = tmp_path / "out"
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    cp = subprocess.run(["uv", "run", str(SHOWCASE / "make.py"), "07-adopt", "--out", str(out)], capture_output=True,
                        text=True, env=env, timeout=1800)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert (out / "07-adopt.mp4").stat().st_size < clips.MAX_MP4
    assert (out / "07-adopt.gif").stat().st_size < clips.MAX_GIF
    assert _png_size(out / "07-adopt.png") == clips.SIZE
    assert (out / "logs" / "07-adopt.txt").read_text().startswith("~/work $ al init larder")
