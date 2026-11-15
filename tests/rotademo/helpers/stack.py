"""Stack PNG clips vertically, separated by a thin grey rule (a visible cut, not a seamless edit).

    uv run --with pillow python stack.py OUT.png CLIP.png ...
"""
import sys

from PIL import Image

RULE, GAP = (148, 163, 184), 6


def main() -> int:
    out, clips = sys.argv[1], [Image.open(path).convert("RGB") for path in sys.argv[2:]]
    width = max(clip.width for clip in clips)
    height = sum(clip.height for clip in clips) + GAP * (len(clips) - 1)
    sheet = Image.new("RGB", (width, height), (255, 255, 255))
    y = 0
    for index, clip in enumerate(clips):
        if index:
            sheet.paste(Image.new("RGB", (width, 2), RULE), (0, y + GAP // 2 - 1))
            y += GAP
        sheet.paste(clip, (0, y))
        y += clip.height
    sheet.save(out, optimize=True)
    print(f"stack: {len(clips)} clip(s) -> {width}x{height}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
