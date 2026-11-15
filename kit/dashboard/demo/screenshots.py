#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.49"]
# ///
"""Take dashboard screenshots of real local runs, refusing any that show something personal.

    demo/screenshots.py --layer DIR [--layer DIR …] [--scan DIR …] --out docs/demo/dashboard \\
        --shot NAME ROUTE [--shot NAME ROUTE …] [--forbid TEXT …]

ROUTE is the page's hash route, optionally followed by `|` and actions separated by `;`:
  click:SELECTOR   type:SELECTOR=TEXT   wait:SELECTOR   scroll:SELECTOR   top:SELECTOR   sleep:SECONDS   full
Before saving, the page's visible text is checked: the home directory, the user name (as a word),
`/home/` and `/Users/` and any --forbid text must not appear. A shot that fails is not written.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE.parent / "dashboard.py"


def forbidden(extra: list[str]) -> list[re.Pattern]:
    home = os.path.expanduser("~")
    user = getpass.getuser()
    patterns = [re.escape(home), r"/home/", r"/Users/", r"~/", rf"(?<![A-Za-z]){re.escape(user)}(?![A-Za-z])"]
    patterns += [re.escape(text) for text in extra]
    return [re.compile(p) for p in patterns]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", action="append", default=[])
    parser.add_argument("--scan", action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--shot", nargs=2, action="append", metavar=("NAME", "ROUTE"), required=True)
    parser.add_argument("--forbid", action="append", default=[])
    parser.add_argument("--allow", action="append", default=[],
                        help="an exact phrase that is fine although it matches a check (e.g. the user name as an English word)")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    args = parser.parse_args()
    chrome = os.environ.get("AL_DASHBOARD_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    argv = [sys.executable, str(DASHBOARD), "--port", str(port), "--no-podman"]
    argv += [x for layer in args.layer for x in ("--layer", layer)] + [x for scan in args.scan for x in ("--scan", scan)]
    server = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    checks = forbidden(args.forbid)
    failures = 0
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome, headless=True)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            args.out.mkdir(parents=True, exist_ok=True)
            for name, spec in args.shot:
                route, _, actions = spec.partition("|")
                page.goto(f"http://127.0.0.1:{port}/{route}")
                page.wait_for_selector("#conn.conn-live")
                time.sleep(1.2)
                full = False
                for action in filter(None, (a.strip() for a in actions.split(";"))):
                    verb, _, arg = action.partition(":")
                    if verb == "click":
                        page.click(arg)
                    elif verb == "type":
                        selector, _, text = arg.partition("=")
                        page.fill(selector, text)
                    elif verb == "wait":
                        page.wait_for_selector(arg)
                    elif verb == "scroll":
                        page.locator(arg).first.scroll_into_view_if_needed()
                    elif verb == "top":
                        page.locator(arg).first.evaluate("e => window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - 52)")
                    elif verb == "sleep":
                        time.sleep(float(arg))
                    elif verb == "full":
                        full = True
                time.sleep(0.8)
                text = page.evaluate("document.body.innerText")   # all text of the page, also below the fold
                for phrase in args.allow:
                    text = text.replace(phrase, "")
                hits = sorted({m.group(0) for pattern in checks for m in pattern.finditer(text)})
                if hits:
                    failures += 1
                    print(f"REFUSED {name}: visible text contains {hits}")
                    continue
                page.screenshot(path=str(args.out / name), full_page=full)
                print(f"{args.out / name}: {(args.out / name).stat().st_size} bytes; privacy check passed")
            browser.close()
    finally:
        server.terminate()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
