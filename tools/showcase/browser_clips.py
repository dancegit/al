"""The browser clips: the dashboard's own test fixtures (and, for the Repos clip, the real al tool with
the mock author), the dashboard started in this process with the tests' start_server, driven by
Playwright in the host's Chrome or Chromium (never downloaded) with Playwright's video recording on.

Every record_* returns (the .webm recording, cues): a cue is (seconds into the recording, caption),
and make.py shows each caption from its time to the next cue's. Before a cue is taken the page's
whole text passes the privacy check; any page error or console error aborts the clip, so no
accidental failure is ever recorded."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import fixture_imports as fx
import privacy

ROOT = Path(__file__).resolve().parents[2]
AL = ROOT / "tools" / "al"
FORBIDDEN: list[str] = []          # make.py sets this to privacy.forbidden_values() of the real HOME
VIEWPORT = {"width": 1280, "height": 720}


def chrome_path() -> str:
    """tests' chrome_path (kit/dashboard/tests/test_browser.py), failing with a sentence."""
    explicit = os.environ.get("AL_DASHBOARD_CHROME")
    if explicit:
        if Path(explicit).exists():
            return explicit
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                return found
    raise SystemExit("browser clips need Chrome or Chromium; set AL_DASHBOARD_CHROME")


def system_ffmpeg_for_playwright(folder: Path) -> None:
    """Playwright records video through an ffmpeg of its own that it would download. Nothing is
    downloaded here: PLAYWRIGHT_BROWSERS_PATH points at `folder`, where ffmpeg-<revision>/ffmpeg-linux
    is a link to the system's ffmpeg (it reads Playwright's mjpeg pipe and writes VP8 the same way)."""
    import playwright
    system = shutil.which("ffmpeg")
    if not system:
        raise SystemExit("browser clips need ffmpeg on PATH")
    browsers = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    revision = next(b["revision"] for b in json.loads(browsers.read_text())["browsers"] if b["name"] == "ffmpeg")
    target = folder / f"ffmpeg-{revision}" / "ffmpeg-linux"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.symlink_to(system)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(folder)


def open_(page, route: str) -> None:
    page.goto(f"{page.base}/#{route}")
    page.wait_for_selector("#conn.conn-live", timeout=10000)


def until(page, expression: str, timeout: int = 10000) -> None:
    """Poll a JS expression with page.evaluate (the dashboard's CSP refuses wait_for_function strings)."""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for: {expression}")


class Recorder:
    """A headless Chromium context recording 1280×720 video of one page.

        with Recorder(running.base) as r:
            open_(r.page, "/layers"); r.cue("…"); r.pause(2)
        webm, cues = r.result
    """

    def __init__(self, base: str, video: bool = True, ignore: tuple[str, ...] = ("favicon",)) -> None:
        self.base, self.video, self.ignore = base, video, ignore
        self.cues: list[tuple[float, str]] = []
        self.errors: list[str] = []
        self.result: tuple[Path | None, list[tuple[float, str]]] = (None, [])

    def __enter__(self) -> "Recorder":
        from playwright.sync_api import sync_playwright
        self._tmp = Path(tempfile.mkdtemp(prefix="showcase-video-"))
        system_ffmpeg_for_playwright(self._tmp / "playwright")
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(executable_path=chrome_path(), headless=True)
        options = {"viewport": VIEWPORT, "device_scale_factor": 1}
        if self.video:
            options.update(record_video_dir=str(self._tmp), record_video_size=VIEWPORT)
        self.context = self.browser.new_context(**options)
        self.page = self.context.new_page()
        self.t0 = time.monotonic()
        self.page.base = self.base  # type: ignore[attr-defined]
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.on("console", lambda msg: self.errors.append(f"{msg.text} {(msg.location or {}).get('url', '')}")
                     if msg.type == "error" else None)
        self.page.on("dialog", lambda dialog: dialog.accept())
        return self

    def check(self) -> None:
        errors = [e for e in self.errors if not any(i in e for i in self.ignore)]
        if errors:
            raise RuntimeError(f"the page reported errors; nothing is recorded: {errors}")

    def cue(self, text: str) -> None:
        self.check()
        privacy.check_private(self.page.inner_text("body"), FORBIDDEN, allowed=(str(Path.home()),))
        self.cues.append((time.monotonic() - self.t0, text))

    def pause(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))

    def __exit__(self, kind, value, tb) -> None:
        video = self.page.video if self.video else None
        try:
            self.context.close()
            path = Path(video.path()) if video else None
            self.browser.close()
        finally:
            self._pw.stop()
        if kind is None:
            self.check()
            self.result = (path, list(self.cues))


def _serve(args: list[str]):
    return fx.dashboard_helpers().start_server(args)


def _stop(running) -> None:
    fx.dashboard_helpers().stop_server(running)


def _type(page, selector: str, text: str) -> None:
    page.click(selector)
    page.keyboard.type(text, delay=45)


# ------------------------------------------------------------------------------------------ 01

def record_01_start_work(root: Path):
    site = fx.start_layer().build_site(root, None)
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN"):
        os.environ.pop(name, None)
    running = _serve(["--layer", str(site["notes"])])
    try:
        with Recorder(running.base, ignore=("favicon", "409")) as r:
            page = r.page
            open_(page, "/layers")
            page.wait_for_selector(".start-panel .start-prompt")
            r.cue("The Layers view: start work on a layer from its Start panel")
            r.pause(1.5)
            page.select_option(".start-target", "workflow:plan-build")
            _type(page, ".start-prompt", "Add a word count to the notes page")
            r.cue("A prompt, the workflow plan-build, and a model for its heavy tier")
            heavy = page.locator(".start-model[data-tier='heavy']")
            heavy.click()
            heavy.fill("")
            page.keyboard.type("claude-sonnet-5-5", delay=40)
            r.pause(1.5)
            page.click(".start-go")
            until(page, "location.hash.startsWith('#/run/notes/')", timeout=30000)
            run_id = page.evaluate("location.hash").rsplit("/", 1)[1]
            r.cue(f"Started: run {run_id} (the mock agent plays the model)")
            page.wait_for_selector(".g-node[data-step='build']", timeout=60000)
            until(page, "document.querySelector(\".g-node[data-step='build']\").getAttribute('class').includes('st-passed')",
                  timeout=120000)
            r.cue("Plan and build passed; a person reviews what was built")
            r.pause(3)
        return r.result
    finally:
        _stop(running)


# ------------------------------------------------------------------------------------------ 02

def _repos_site(work: Path, aux: Path) -> tuple[Path, Path]:
    """The four repositories in `work`; the al wrapper, its settings file and the author mock in `aux`."""
    wt = fx.workspace_test()
    fx.workspace_fixtures().build(work)
    bin_dir = aux / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    env_file = bin_dir / "al.env"
    # racket "not installed yet": the setting names a program that does not exist. (A path under HOME would
    # be shown unshortened by al's sentence; see docs/showcase/README.md, "Bugs found while recording".)
    missing = Path("/opt/not-installed/racket/bin/racket")
    if missing.exists():
        raise SystemExit(f"{missing} exists here; the Repos clip needs a racket that is not installed")
    env_file.write_text(f"AL_RACKET={missing}\n")
    wrapper = bin_dir / "al"
    wrapper.write_text(f'#!/bin/sh\nset -a\n. "{env_file}"\nset +a\nexec "{AL}" "$@"\n')
    wrapper.chmod(0o755)
    mock = aux / "author-mock.json"
    mock.write_text(json.dumps({"files": {"agentic.rkt": wt.DEFINITION}, "output": "wrote agentic.rkt"}))
    return wrapper, mock


def _create_fern(page, r: "Recorder | None") -> None:
    open_(page, "/repos")
    page.wait_for_selector(".repo-row")
    if r:
        r.cue("Four repositories side by side: a layer, two agent setups, nothing yet")
        r.pause(2.5)
    page.locator('.repo-row[data-repo="fern"]').click()
    until(page, "location.hash === '#/repo/fern'")
    button = page.locator('.al-panel [data-action="create"] button')
    button.wait_for()
    if r:
        r.cue("fern has no layer: Create writes one beside it with the real al tool")
        r.pause(2)
    button.click()
    page.wait_for_selector('.action-confirm [data-role="confirm"]')
    if r:
        r.cue("What it will run and write, before anything is written")
        r.pause(2.5)
    page.locator('.action-confirm [data-role="confirm"]').click()
    until(page, "location.hash.startsWith('#/job/')")
    page.wait_for_selector(".job-step")
    until(page, "document.querySelector('.run-head .pill') && document.querySelector('.run-head .pill').textContent === 'failed'",
          120000)
    page.wait_for_selector(".job-step .why")


def _retry(page, env_file: Path) -> None:
    env_file.write_text("")                              # racket is installed now
    page.locator(".job-retry button").click()
    until(page, "document.querySelector('.run-head .pill') && document.querySelector('.run-head .pill').textContent === 'passed'",
          300000)
    until(page, "document.querySelectorAll('.job-step').length > 0 && "
                "document.querySelectorAll('.job-step.s-passed').length === document.querySelectorAll('.job-step').length")


def record_02_repos(root: Path):
    # a rehearsal without video first: if the job does not pass after the retry, nothing is recorded
    for take in ("rehearsal", "recording"):
        work = root.parent / "rehearsal" / "work" if take == "rehearsal" else root
        aux = root.parent / ("rehearsal" if take == "rehearsal" else "setup")
        wrapper, mock = _repos_site(work, aux)
        running = _serve(["--workspace", str(work), "--jobs-dir", str(aux / "jobs"), "--al", str(wrapper),
                          "--author-mock", str(mock)])
        try:
            with Recorder(running.base, video=take == "recording") as r:
                page = r.page
                _create_fern(page, r if take == "recording" else None)
                if take == "recording":
                    page.locator(".job-step .why").first.scroll_into_view_if_needed()
                    r.cue("The job checks its tools first: racket is not installed yet, and al says so")
                    r.pause(4)
                    env_file = wrapper.parent / "al.env"
                    env_file.write_text("")
                    r.cue("racket installed: Retry from the step that stopped; the passed steps are kept")
                    r.pause(2)
                    _retry(page, env_file)
                    r.cue("The job passed: fern has a generated, checked layer")
                    r.pause(3.5)
                else:
                    _retry(page, wrapper.parent / "al.env")
            if take == "recording":
                return r.result
        finally:
            _stop(running)
    raise AssertionError("unreachable")


# ------------------------------------------------------------------------------------------ 03

PLAN = """# Feature: word count on the notes page

Show how many words each note has, next to its title.

## Steps

1. count the words of a note in `notes/count.py`
2. show the count in the list
3. test both

## Acceptance

- a note of three words shows **3 words**
- an empty note shows *0 words*
"""

REPORT = """# Review report

Two reviewers, one round. **Approved.**

## Checked

- the count ignores punctuation
- the list shows the count beside the title
- `tests/test_count.py` covers the empty note

## Notes

- the helper could be named `word_count` (skippable)
"""


def _documents_run(layer: Path) -> str:
    """A finished run with a plan and a review report, as test_browser_documents.running_run records one."""
    dash = fx.dash_fixtures()
    rid = "a2000020"
    (layer / "specs" / f"feature-{rid}-word-count.md").write_text(PLAN)
    plan = f"{dash.LAYER_DIR}/specs/feature-{rid}-word-count.md"
    steps = [{"name": "classify", "kind": "run", "status": "passed", "started": "2026-11-01T12:00:00+00:00",
              "ended": "2026-11-01T12:00:10+00:00"},
             {"name": "plan-feature", "kind": "run", "status": "passed", "started": "2026-11-01T12:00:10+00:00",
              "ended": "2026-11-01T12:00:40+00:00", "detail": plan},
             {"name": "build", "kind": "run", "status": "passed", "started": "2026-11-01T12:00:40+00:00",
              "ended": "2026-11-01T12:03:00+00:00"},
             {"name": "review", "kind": "review", "status": "passed", "started": "2026-11-01T12:03:00+00:00",
              "ended": "2026-11-01T12:04:00+00:00", "detail": "pass after 1 round(s): approved"}]
    run = layer / "agents" / rid
    (run / "review-1").mkdir(parents=True)
    (run / "REVIEW.md").write_text(REPORT)
    (run / "review-1" / "output.txt").write_text('{"success": true, "summary": "approved", "issues": []}\n')
    state = {"adw_id": rid, "adw": "zte", "prompt": "Add a word count to the notes page", "status": "passed",
             "klass": "feature", "started": "2026-11-01T12:00:00+00:00", "ended": "2026-11-01T12:04:00+00:00",
             "steps": steps, "plan": plan, "outputs": {"plan-feature": plan}}
    tmp = run / "state.tmp"
    tmp.write_text(json.dumps(state))
    os.replace(tmp, run / "state.json")
    return rid


def record_03_run_documents(root: Path):
    fx.dash_fixtures().build(root)
    layer = root / "demo-shop.al"
    rid = _documents_run(layer)
    running = _serve(["--layer", str(layer)])
    try:
        with Recorder(running.base) as r:
            page = r.page
            open_(page, f"/run/demo-shop/{rid}/documents")
            page.locator("table.docs tbody tr").first.wait_for()
            r.cue("A run's Documents tab: everything it wrote, listed")
            r.pause(2)
            page.locator(f"table.docs tr[data-path='specs/feature-{rid}-word-count.md'] button[data-role=view]").click()
            page.locator(".doc-view .md h1").wait_for()
            r.cue("The plan, rendered in the page")
            r.pause(3)
            page.locator(f"table.docs tr[data-path='agents/{rid}/REVIEW.md'] button[data-role=view]").click()
            until(page, "document.querySelector('.doc-view .md h1') && "
                        "document.querySelector('.doc-view .md h1').textContent === 'Review report'")
            r.cue("The review report; each document as raw text or a download")
            r.pause(2)
            page.locator(f"table.docs tr[data-path='agents/{rid}/REVIEW.md'] a[data-role=download]").hover()
            r.pause(1.5)
            open_(page, "/run/demo-shop/a1000001/artefacts")
            page.wait_for_selector(".thumb img")
            until(page, "document.querySelector('.thumb img').naturalWidth > 0")
            r.cue("The Artefacts tab: the screenshots and recording a run collected")
            r.pause(2)
            page.click(".thumb")
            r.pause(2)
            page.keyboard.press("Escape")
            open_(page, "/now")
            row = page.locator(".models-panel[data-layer='demo-shop'] .tier-row[data-tier='heavy']")
            row.wait_for()
            row.scroll_into_view_if_needed()
            page.mouse.wheel(0, 300)
            r.cue("The Models panel: which model each tier uses, and where that comes from")
            r.pause(2)
            row.locator("select").select_option("claude-fable-5-2")
            r.pause(1)
            row.locator("button.apply").click()
            until(page, "[...document.querySelectorAll(\".models-panel[data-layer='demo-shop'] .tier-row[data-tier='heavy'] .source\")]"
                        ".some(e => e.textContent === 'local setting')")
            r.cue("heavy is now claude-fable-5-2, a local setting for runs started from now on")
            r.pause(3.5)
        return r.result
    finally:
        _stop(running)


# ------------------------------------------------------------------------------------------ 04

def record_04_review_says_no(root: Path):
    fx.dash_fixtures().build(root)
    layer = root / "demo-shop.al"
    fx.review_model().add_review_runs(layer)
    running = _serve(["--layer", str(layer)])
    try:
        with Recorder(running.base) as r:
            page = r.page
            open_(page, "/run/demo-shop/a1000101")
            page.wait_for_selector(".g-node[data-step='review']")
            r.cue("Run a1000101: the review step is rejected (⊗), the run handed over")
            r.pause(2.5)
            page.click(".g-node[data-step='review']")
            page.wait_for_selector(".step-panel .step-rounds")
            page.locator(".step-panel .step-rounds").scroll_into_view_if_needed()
            r.cue("Three rounds, each not approved, with the reviewers' reasons")
            r.pause(3.5)
            open_(page, "/runs")
            page.wait_for_selector("tbody tr[data-key='demo-shop/a1000101']")
            until(page, "[...document.querySelectorAll('.stat')].some((s) => s.querySelector('.label')?.textContent.trim() === "
                        "'handed over' && s.querySelector('.value')?.textContent.trim() === '2')")
            r.cue("The Runs list counts it as handed over, not as passed")
            r.pause(3)
            open_(page, "/run/demo-shop/a1000101/step/review")
            page.wait_for_selector(".step-panel .step-rounds")
            page.locator(".step-panel .step-rounds").scroll_into_view_if_needed()
            r.cue("A person takes it from here, with the reasons in front of them")
            r.pause(3)
        return r.result
    finally:
        _stop(running)


# ------------------------------------------------------------------------------------------ 09

def record_09_promotion_pack(root: Path):
    site = fx.promotion_fixtures().build_promotion_site(root / "site")
    running = _serve(["--layer", str(site["layer"])])
    try:
        with Recorder(running.base) as r:
            page = r.page
            open_(page, "/layer/shop")
            page.wait_for_selector(".promotions-section .promotion-row")
            page.locator(".promotions-section").scroll_into_view_if_needed()
            r.cue("The layer page: the promotion sprint → main is open, waiting for people")
            r.pause(3)
            open_(page, f"/run/shop/{site['run']}")
            page.wait_for_selector(".promotion-card")
            page.locator(".promotion-card").scroll_into_view_if_needed()
            r.cue("Its card: each delivered change with its criteria and proof")
            r.pause(3)
            page.fill(".promotion-by", "Ada Example")
            page.fill(".promotion-comment", "not this sprint")
            page.check(".promotion-change[data-change='0000c002'] input[type=checkbox]")
            r.cue("A person accepts or refuses each change; refusing reverts it")
            r.pause(1.5)
            page.click("button:has-text('Refuse · revert')")
            until(page, "document.querySelector(\".promotion-change[data-change='0000c002'] .badge.warn\") !== null")
            page.locator(".promotion-change[data-change='0000c002']").scroll_into_view_if_needed()
            r.pause(2.5)
            page.goto(f"{page.base}/files/shop/artefacts/{site['run']}/pack/index.html")
            page.wait_for_selector("video")
            r.cue("The presentation pack: what the promotion brings, with pictures and recordings")
            r.pause(2.5)
            page.mouse.wheel(0, 450)
            r.pause(3)
            page.mouse.wheel(0, -450)
            r.pause(3)
        return r.result
    finally:
        _stop(running)


RECORDERS = {1: record_01_start_work, 2: record_02_repos, 3: record_03_run_documents, 4: record_04_review_says_no,
             9: record_09_promotion_pack}
