#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.49"]
# ///
"""Record the layer's own dashboard in the host's Chrome while one real workflow run executes.

    record_run.py record  --repo REPO --work DIR --request FILE [--adw sdlc] [--check test-tools]
    record_run.py publish --repo REPO --work DIR --out docs/demo/al-for-al [--allow PHRASE …]

Adapted from kit/dashboard/demo/live_demo.py. `record` starts `uv run agentic/adws/dashboard.py`,
opens it in Chrome with Playwright recording video, starts `uv run agentic/adws/adw_<adw>.py REQUEST`
and follows the run: the Now view, the workflow graph, the build step's transcript in follow mode, a
check's log while it grows, the finished run. It keeps the raw recording, the page texts it saw, the
scene times and the stills in the work directory. `publish` applies the privacy check of
kit/dashboard/demo/screenshots.py to every page text that was on screen (home directory, `/home/`,
`~/`, the user name, the work directory and the repository path) and refuses to write anything when
one matches; otherwise it cuts the waiting time (each scene is sped up to a fixed length, so the
video is a time-lapse of one continuous real run) and writes dashboard-live.mp4 / .gif and the stills.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

CHROME = os.environ.get("AL_DASHBOARD_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
# scene -> seconds it gets in the published video
SCENES = {"now": 6, "graph": 12, "transcript": 24, "graph-checks": 4, "check-log": 12, "graph-rest": 10,
          "finished": 9, "transcript-done": 6, "history": 4}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Attached:
    """A run (or dashboard) that is already going: the recorder was started after it."""

    def __init__(self, pid: int) -> None:
        self.pid, self.returncode = pid, None

    def poll(self):
        if self.pid and not Path(f"/proc/{self.pid}").exists():
            self.returncode = -1   # unknown to this process: read the run's state.json for the outcome
        return self.returncode

    def terminate(self) -> None:
        pass


def record(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    repo, work = args.repo.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    request = args.request.read_text().strip()
    port = args.attach_port or free_port()
    dashboard = Attached(0) if args.attach_port else subprocess.Popen(["uv", "run", "--quiet", "agentic/adws/dashboard.py", "--port", str(port), "--poll", "0.5"],
                                 cwd=repo, stdout=subprocess.DEVNULL, stderr=(work / "dashboard.log").open("w"))
    run: subprocess.Popen | None = None
    base = f"http://127.0.0.1:{port}"
    marks: list[tuple[str, float]] = []
    texts: list[str] = []
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROME, headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800}, record_video_dir=str(work / "video"),
                                          record_video_size={"width": 1280, "height": 800}, device_scale_factor=1)
            page = context.new_page()
            started = time.monotonic()

            def now() -> float:
                return time.monotonic() - started

            def mark(name: str) -> None:
                marks.append((name, now()))
                print(f"[{now():7.1f}s] scene {name}", flush=True)

            def see() -> None:
                text = page.evaluate("document.body.innerText")
                if not texts or texts[-1] != text:
                    texts.append(text)

            def has(selector: str) -> bool:
                return page.locator(selector).count() > 0

            def until(condition, timeout: float, what: str) -> bool:
                """Poll the page (keeping what it shows) until condition() or the run's process has ended."""
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    see()
                    if condition():
                        return True
                    if run is not None and run.poll() is not None:
                        print(f"[{now():7.1f}s] run ended while waiting for {what}", flush=True)
                        return condition()
                    time.sleep(0.5)
                print(f"[{now():7.1f}s] timed out waiting for {what}", flush=True)
                return False

            def linger(seconds: float) -> None:
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    see()
                    time.sleep(0.5)

            def node(step: str, *states: str) -> str:
                return ", ".join(f".g-node.st-{s}[data-step='{step}']" for s in states) if states else f".g-node[data-step='{step}']"

            page.goto(f"{base}/#/now")
            page.wait_for_selector("#conn.conn-live")
            mark("now")
            linger(2.0)
            log = (work / "run.log").open("a")
            run = Attached(args.attach_pid) if args.attach_pid else subprocess.Popen(["uv", "run", f"agentic/adws/adw_{args.adw}.py", request] + (["--dry-run"] if args.dry_run else []), cwd=repo,
                                   stdout=log, stderr=subprocess.STDOUT)
            run_started = time.monotonic()
            until(lambda: has(".run-card"), 90, "the run's card")
            linger(4.0)
            page.locator(".run-card").first.click()
            mark("graph")
            until(lambda: has(".g-node"), 30, "the workflow graph")
            run_url = page.url
            run_id = run_url.rstrip("/").split("/")[-1]
            events = repo / "agentic" / "agents" / run_id / "events.jsonl"

            def step(name: str) -> str:
                """'' (not started), 'running' or 'ended', from the run's own event file."""
                state = ""
                if events.exists():
                    for line in events.read_text().splitlines():
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("step") == name and event.get("type") == "step.start":
                            state = "running"
                        if event.get("step") == name and event.get("type") == "step.end":
                            state = "ended"
                return state

            until(lambda: step("build") != "", 1200, "the build step")
            linger(1.5)
            page.click(node("build"))
            mark("transcript")
            until(lambda: has(".ev.k-tool") or step("build") == "ended", 300, "the first tool call")
            until(lambda: step("build") == "ended", 1800, "the end of the build step")
            linger(2.0)
            page.goto(run_url)
            mark("graph-checks")
            until(lambda: step(args.check) != "", 1800, f"the {args.check} check")
            linger(1.0)
            page.click(node(args.check))
            mark("check-log")
            until(lambda: page.evaluate("() => (document.querySelector('.log') || {textContent: ''}).textContent.length") > 0
                  or step(args.check) == "ended", 300, "the check's log")
            until(lambda: step(args.check) == "ended", 1800, f"the end of {args.check}")
            linger(2.0)
            page.goto(run_url)
            mark("graph-rest")
            end = time.monotonic() + 3600
            while run.poll() is None and time.monotonic() < end:
                see()
                time.sleep(0.5)
            seconds = time.monotonic() - run_started
            linger(4.0)
            mark("finished")
            linger(5.0)
            page.screenshot(path=str(work / "run.png"))
            # the graph is wider than the window: show its last steps (commit, review, ship) too
            page.evaluate("() => { const n = document.querySelector(\".g-node[data-step='ship']\"); if (n) n.scrollIntoView({inline: 'end', block: 'nearest'}); }")
            linger(3.0)
            page.screenshot(path=str(work / "run-end.png"))
            page.click(node("build"))
            mark("transcript-done")
            linger(5.0)
            page.screenshot(path=str(work / "transcript.png"))
            page.keyboard.press("2")
            mark("history")
            linger(4.0)
            page.screenshot(path=str(work / "history.png"))
            mark("end")
            video = Path(page.video.path())
            context.close()
            browser.close()
        shutil.copy(video, work / "raw.webm")
        (work / "texts.json").write_text(json.dumps(texts))
        (work / "marks.json").write_text(json.dumps({"marks": marks, "run_seconds": round(seconds, 1),
                                                     "run_exit": run.returncode, "run_url": run_url.split("#", 1)[-1]}))
        print(f"recorded {marks[-1][1]:.0f} s; the run took {seconds:.0f} s and exited {run.returncode}")
    finally:
        if run is not None and run.poll() is None:
            run.terminate()
        dashboard.terminate()
    return 0


def publish(args: argparse.Namespace) -> int:
    repo, work, out = args.repo.resolve(), args.work.resolve(), args.out
    sys.path.insert(0, str(repo / "kit" / "dashboard" / "demo"))
    from screenshots import forbidden   # the same privacy rule as the dashboard's own demo scripts

    checks = forbidden([str(work), str(repo), str(repo.parent)])
    texts = json.loads((work / "texts.json").read_text())
    hits: dict[str, set[str]] = {}
    for text in texts:
        for phrase in args.allow:
            text = text.replace(phrase, "")
        for pattern in checks:
            for m in pattern.finditer(text):
                hits.setdefault(m.group(0), set()).add(text[max(0, m.start() - 40):m.end() + 40].replace("\n", " ⏎ "))
    if hits:
        for hit, contexts in hits.items():
            print(f"REFUSED: the page showed {hit!r} in {len(contexts)} place(s):")
            for context in sorted(contexts)[:12]:
                print(f"    …{context}…")
        return 1
    data = json.loads((work / "marks.json").read_text())
    marks = data["marks"]
    parts, labels = [], []
    for n, ((name, t0), (_, t1)) in enumerate(zip(marks, marks[1:])):
        target = SCENES[name]
        speed = max(1.0, (t1 - t0) / target)
        parts.append(f"[0:v]trim=start={t0:.2f}:end={t1:.2f},setpts=(PTS-STARTPTS)/{speed:.4f},fps=12,"
                     f"scale=1280:800:flags=lanczos,setsar=1[v{n}]")
        labels.append(f"[v{n}]")
        print(f"{name:16s} {t1 - t0:7.1f} s real  → {min(target, t1 - t0):5.1f} s  (×{speed:.1f})")
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=1:a=0[out]"
    out.mkdir(parents=True, exist_ok=True)
    cut = work / "cut.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(work / "raw.webm"), "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    "-map_metadata", "-1", str(cut)], check=True)
    shutil.copy(cut, out / "dashboard-live.mp4")
    subprocess.run([sys.executable, str(repo / "kit" / "dashboard" / "demo" / "gif.py"), str(cut), str(out / "dashboard-live.gif"),
                    "--max-bytes", "2500000"], check=True)
    for name in ("run.png", "transcript.png", "history.png"):
        shutil.copy(work / name, out / name)
    for path in sorted(out.iterdir()):
        print(f"{path.name}: {path.stat().st_size} bytes")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--repo", type=Path, default=Path("."))
    rec.add_argument("--work", type=Path, required=True)
    rec.add_argument("--request", type=Path, required=True)
    rec.add_argument("--adw", default="sdlc")
    rec.add_argument("--attach-port", type=int, default=0, help="record a dashboard that is already running on this port")
    rec.add_argument("--attach-pid", type=int, default=0, help="follow a run that is already going (its process id)")
    rec.add_argument("--dry-run", action="store_true", help="rehearse with the stand-in agent (no model, no cost)")
    rec.add_argument("--check", default="test-tools", help="the check whose log is watched while it grows")
    pub = sub.add_parser("publish")
    pub.add_argument("--repo", type=Path, default=Path("."))
    pub.add_argument("--work", type=Path, required=True)
    pub.add_argument("--out", type=Path, required=True)
    pub.add_argument("--allow", action="append", default=[],
                     help="an exact phrase that is fine although it matches a check (the user name as an English word)")
    pub.add_argument("--crf", type=int, default=30)
    args = parser.parse_args()
    if args.verb == "record" and not CHROME:
        sys.exit("no Chrome found (set AL_DASHBOARD_CHROME)")
    return record(args) if args.verb == "record" else publish(args)


if __name__ == "__main__":
    sys.exit(main())
