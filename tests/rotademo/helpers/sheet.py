"""Contact sheet of video keyframes: 4 columns x 2 rows, like docs/demo/webdemo/b-keyframes-sheet.png.

    uv run --with pillow python sheet.py OUT.png FRAME.png ...
"""
import sys

from PIL import Image

TILE_W, GAP, COLS, BG = 420, 10, 4, (15, 23, 42)


def main() -> int:
    out, frames = sys.argv[1], sys.argv[2:]
    if not frames:
        print("sheet: no frames", file=sys.stderr)
        return 1
    images = [Image.open(path).convert("RGB") for path in frames]
    tile_h = round(TILE_W * images[0].height / images[0].width)
    rows = (len(images) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * TILE_W + (COLS + 1) * GAP, rows * tile_h + (rows + 1) * GAP), BG)
    for index, image in enumerate(images):
        row, col = divmod(index, COLS)
        sheet.paste(image.resize((TILE_W, tile_h), Image.LANCZOS), (GAP + col * (TILE_W + GAP), GAP + row * (tile_h + GAP)))
    sheet.save(out, optimize=True)
    print(f"sheet: {len(images)} frames -> {sheet.width}x{sheet.height}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
