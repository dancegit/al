"""Frames of a terminal clip and the caption images both kinds of clip share, at 1280×720.

A `Transcript` is the title of the window and the steps that ran: the command as typed, the output it
really printed, and a one-line caption. `render_frames` turns it into exactly `frame_count` RGB images
(typing, output appearing a few lines a frame, holds to read), a pure function of the transcript and
the font. `caption_card` is the clip's opening card; `caption_strip` the band at the bottom of a frame
(drawn into terminal frames, overlaid on browser recordings by make.py). Colours follow
tools/demo/record.py.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterator

from PIL import Image, ImageDraw, ImageFont

import clips

W, H = clips.SIZE
COLS, ROWS = 100, 26
PROMPT = "~/work $ "
CHARS_PER_FRAME = 2
LINES_PER_FRAME = 3
HOLD_AFTER_TYPE = 6
HOLD_AFTER_OUTPUT = 22
HOLD_END = 45

BG, FG, DIM, GREEN, RED, YELLOW, BLUE = "#0d1117", "#c9d1d9", "#6e7681", "#3fb950", "#f85149", "#d29922", "#58a6ff"
BAR = "#161b22"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")

TITLE_H = 32
STRIP_H = 64
FONT_SIZE, LINE_H, PAD = 19, 23, 18
CARD_SIZE, STRIP_SIZE = 30, 22
CARD_WIDTH, STRIP_WIDTH = 1200, 1240


def font_path(bold: bool = False) -> str:
    explicit = os.environ.get("AL_SHOWCASE_FONT")
    if explicit:
        if bold:
            sibling = explicit.replace(".ttf", "-Bold.ttf")
            return sibling if os.path.isfile(sibling) else explicit
        return explicit
    query = "DejaVu Sans Mono:bold" if bold else "DejaVu Sans Mono"
    try:
        found = subprocess.run(["fc-match", "-f", "%{file}", query], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        found = ""
    if not found or not os.path.isfile(found):
        raise RuntimeError("no monospace font: install fonts-dejavu-core or set AL_SHOWCASE_FONT")
    return found


@lru_cache(maxsize=None)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(bold), size)


@dataclass
class Step:
    command: str
    output: str
    caption: str = ""


@dataclass
class Transcript:
    steps: list[Step] = field(default_factory=list)
    title: str = ""


def output_lines(output: str) -> list[str]:
    """The output as shown: ANSI stripped, tabs expanded, each line cut at COLS."""
    text = ANSI.sub("", output).expandtabs(4).rstrip("\n")
    return [line[:COLS] for line in text.split("\n")] if text else []


def frame_count(t: Transcript) -> int:
    total = HOLD_END
    for step in t.steps:
        total += (math.ceil(len(step.command) / CHARS_PER_FRAME) + HOLD_AFTER_TYPE
                  + math.ceil(len(output_lines(step.output)) / LINES_PER_FRAME) + HOLD_AFTER_OUTPUT)
    return total


def colour(line: str) -> str:
    if re.search(r"\b(FAIL|FAILED|failed|error|Error|rejected|miss|missing|stopped)\b", line):
        return RED
    if re.search(r"\b(ok|passed|PASSED|merged|shipped|pass|tagged|found)\b", line):
        return GREEN
    if line.startswith("#") or "skipped" in line or "waiting" in line:
        return YELLOW if "waiting" in line else DIM
    return FG


def caption_strip(text: str) -> Image.Image:
    """An RGBA 1280×64 band: semi-opaque, one line of white text. ValueError when it does not fit."""
    img = Image.new("RGBA", (W, STRIP_H), (13, 17, 23, 215))
    if not text:
        return img
    face = font(STRIP_SIZE, True)
    if "\n" in text or face.getlength(text) > STRIP_WIDTH:
        raise ValueError(f"caption does not fit one line of {STRIP_WIDTH} px: {text!r}")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 6, STRIP_H], fill=BLUE)
    d.text((W // 2, STRIP_H // 2), text, font=face, fill="#ffffff", anchor="mm")
    return img


def caption_card(text: str) -> Image.Image:
    """The clip's opening card: one centred line. ValueError when it does not fit 1200 px."""
    face = font(CARD_SIZE, True)
    if "\n" in text or face.getlength(text) > CARD_WIDTH:
        raise ValueError(f"card does not fit one line of {CARD_WIDTH} px: {text!r}")
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W // 2, H // 2), text, font=face, fill=FG, anchor="mm")
    d.line([W // 2 - 60, H // 2 + 34, W // 2 + 60, H // 2 + 34], fill=BLUE, width=3)
    return img


def _draw(title: str, lines: list[tuple[str, str]], cursor: bool, caption: str) -> Image.Image:
    """One frame: lines are (kind, text), kind "cmd" (a prompt line) or "out"."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, TITLE_H], fill=BAR)
    for i, c in enumerate((RED, YELLOW, GREEN)):
        d.ellipse([14 + i * 22, 10, 26 + i * 22, 22], fill=c)
    d.text((W // 2, TITLE_H // 2), title, font=font(15, True), fill=DIM, anchor="mm")
    regular, bold = font(FONT_SIZE), font(FONT_SIZE, True)
    cw = regular.getlength("M")
    shown = lines[-ROWS:]
    top = TITLE_H + 10
    for row, (kind, text) in enumerate(shown):
        y = top + row * LINE_H
        if kind == "cmd":
            d.text((PAD, y), PROMPT, font=bold, fill=GREEN)
            d.text((PAD + cw * len(PROMPT), y), text[: COLS - len(PROMPT)], font=bold, fill=BLUE)
        else:
            d.text((PAD, y), text, font=regular, fill=colour(text))
    if cursor and shown:
        kind, text = shown[-1]
        x = PAD + cw * min(len(text) + (len(PROMPT) if kind == "cmd" else 0), COLS)
        y = top + (len(shown) - 1) * LINE_H
        d.rectangle([x, y + 2, x + cw - 2, y + LINE_H - 3], fill=FG)
    if caption:
        strip = caption_strip(caption)
        img.paste(strip, (0, H - STRIP_H), strip)
    return img


def render_frames(t: Transcript) -> Iterator[Image.Image]:
    """Exactly frame_count(t) 1280×720 RGB frames (identical neighbours are the same object)."""
    lines: list[tuple[str, str]] = []
    caption = ""
    last: Image.Image | None = None
    for step in t.steps:
        caption = step.caption
        lines.append(("cmd", ""))
        for k in range(1, math.ceil(len(step.command) / CHARS_PER_FRAME) + 1):
            lines[-1] = ("cmd", step.command[: k * CHARS_PER_FRAME])
            yield _draw(t.title, lines, True, caption)
        last = _draw(t.title, lines, True, caption)
        for _ in range(HOLD_AFTER_TYPE):
            yield last
        out = output_lines(step.output)
        for k in range(math.ceil(len(out) / LINES_PER_FRAME)):
            lines.extend(("out", line) for line in out[k * LINES_PER_FRAME:(k + 1) * LINES_PER_FRAME])
            yield _draw(t.title, lines, False, caption)
        last = _draw(t.title, lines, False, caption)
        for _ in range(HOLD_AFTER_OUTPUT):
            yield last
    lines.append(("cmd", ""))
    last = _draw(t.title, lines, True, caption)
    for _ in range(HOLD_END):
        yield last
