"""Readable crops of a Forgejo pull request page, taken in the demo image's headless Chromium.

    python pr_crop.py URL OUT_PREFIX SPEC ...

SPEC is `header` (the PR title with its Open/Merged state) or `FROM..TO`: the part of the PR
description from the heading FROM up to (not including) the heading TO; `FROM..` runs to the end of
the description. Writes OUT_PREFIX-1.png, OUT_PREFIX-2.png, ... in SPEC order (helpers/stack.py
joins them). Exit 1 when a heading is not found. Nothing on the page is changed; these are clips.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if Path("/opt/pw").is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw")

PAD = 14


def heading(page, text: str):
    for element in page.locator(".markup h2").all():
        if element.inner_text().strip() == text:
            return element
    raise SystemExit(f"pr_crop: no heading {text!r} in the PR description")


def main() -> int:
    from playwright.sync_api import sync_playwright

    url, prefix, specs = sys.argv[1], sys.argv[2], sys.argv[3:]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1300, "height": 1000}, device_scale_factor=1)
        page.goto(url, wait_until="networkidle")
        markup = page.locator(".markup").first.bounding_box()
        for number, spec in enumerate(specs, start=1):
            if spec == "header":
                box = page.locator(".issue-title-header, .issue-title").first.bounding_box()
                clip = {"x": box["x"] - PAD, "y": max(0, box["y"] - PAD), "width": box["width"] + 2 * PAD,
                        "height": box["height"] + 2 * PAD}
            else:
                start_text, end_text = spec.split("..", 1)
                start = heading(page, start_text).bounding_box()
                end_y = heading(page, end_text).bounding_box()["y"] if end_text else markup["y"] + markup["height"]
                clip = {"x": markup["x"] - PAD, "y": start["y"] - PAD, "width": markup["width"] + 2 * PAD,
                        "height": end_y - start["y"] + PAD}
            page.screenshot(path=f"{prefix}-{number}.png", full_page=True, clip=clip)
            print(f"pr_crop: {spec} -> {prefix}-{number}.png ({int(clip['width'])}x{int(clip['height'])})")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
