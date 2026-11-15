#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Encode a dashboard screen recording as a small GIF with ffmpeg.

A generic palette spends its colours on the large dark areas and loses the few pixels that
matter (green / red / blue / amber status colours). This uses a fixed palette made of the
dashboard's own theme colours plus a ramp of greys for anti-aliased text, drops duplicate
frames, and lowers the frame rate until the file fits.

    demo/gif.py VIDEO OUT.gif [--max-bytes 2500000]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

THEME = [
    # backgrounds and borders
    "0d1015", "141920", "1a2029", "212935", "252d38", "313b49", "0b0e13",
    # text greys
    "4a5462", "566070", "7d8796", "aab3c0", "d8dee7", "ffffff",
    # status and type colours, and their dim variants on dark panels
    "3fb950", "2a6b37", "7ee2a0", "f2555a", "7a2e33", "ff9b9b", "58a6ff", "6ea8fe", "2b4a75", "bcd4ff",
    "d6a23a", "6e5525", "b18cf7", "33b8a6", "e47bc4", "8f7cf0", "f87171", "4ade80",
]


def palette_file(directory: Path) -> Path:
    """A 16×16 PPM image holding the palette (paletteuse wants exactly 256 pixels); the theme
    colours repeat to fill it, so the GIF uses only these colours."""
    path = directory / "palette.ppm"
    colours = [THEME[i % len(THEME)] for i in range(256)]
    pixels = bytes(int(colour[i:i + 2], 16) for colour in colours for i in (0, 2, 4))
    path.write_bytes(b"P6 16 16 255\n" + pixels)
    return path


def encode(video: Path, out: Path, fps: float, palette: Path) -> int:
    chain = f"fps={fps},mpdecimate=hi=1536:lo=640:frac=0.5,scale=1280:800:flags=lanczos"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(palette), "-lavfi",
                    f"{chain}[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle",
                    "-fps_mode", "vfr", str(out)], check=True)
    return out.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--max-bytes", type=int, default=2_500_000)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as work:
        palette = palette_file(Path(work))
        for fps in (4, 3, 2.5, 2, 1.5):
            size = encode(args.video, args.out, fps, palette)
            print(f"{args.out}: {size} bytes at {fps} fps, {len(THEME)} theme colours")
            if size <= args.max_bytes:
                return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
