"""The scenes of the demo video's second story: one recorder per scene id of narration-testbed.md.

The same seven parts as scenes.py, on the pinned public codebase flask (tests/authored/flask, pinned in
tests/integration/testbeds.toml) in every scene, delivered to a throwaway local Forgejo (forgejo.py):
the run's ship step pushes a branch and opens a real pull request there, and 04-pr shows that page.

  testbed_site        a copy of the pinned flask at work/flask: the tree only, committed once by a neutral
                      identity, with the Forgejo repository as its origin when a forge is given
  install_flask_layer the authored definition (tests/authored/flask/agentic.rkt) as work/flask.al/agentic.rkt
  demo_layer          the layer the runs of parts 4 and 5 use (flask_layer.rkt.in): flask's protected paths,
                      one test module, the mock agent and the scripted stand-in flask_replay.py
  require_forge       the instance `make.py forgejo up` recorded for the work folder, or SceneStopped:
                      the story never fakes its pull request
  close_handoff       closes the stopped run's `[needs human]` pull request through the Forgejo API, before
                      the approval: Forgejo refuses a second open pull request for the same head and base
  SCENES              scene id → recorder function

Every helper that draws, records or checks is scenes.py's own, imported here and never copied. This
module imports only the standard library at the top.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

import scenes
from scenes import (AL, Ctx, Part, Segment, SceneStopped, Browser, hidden, shell, terminal_part, highlight_frame, card_part,
                    card_frames, _frames_part, _serve, _node, _to_top, _to_text, _open_step, wait_for, run_state, step_status,
                    new_run, runs_of, _waits_for_approval, _git, fill, REPOSITORY, WHO)

import forgejo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
AUTHORED = ROOT / "tests" / "authored" / "flask"
PIN = tomllib.loads((ROOT / "tests" / "integration" / "testbeds.toml").read_text(encoding="utf-8"))["flask"]
CACHE = ROOT / "testbeds" / "flask"
LAYER_SOURCE = HERE / "flask_layer.rkt.in"
REPLAY = HERE / "flask_replay.py"
PROMPT_A = "Treat FLASK_DEBUG=off as off"
PROMPT_B = "Note in sessions.py where the cookie defaults are documented"
PROMPT_C = "Add a note about debug mode to the README"
APPROVAL = "edit:src/flask/sessions.py"
# the steps of this story's own layer (flask_layer.rkt.in), which differ from the rota story's
STEPS_A = ("classify", "plan", "prove", "build", "lint", "test", "commit", "evidence", "review", "ship")
# flask's locked dependencies, the virtual environment outside the worktree, no cache folder in it; the
# runtime's own VIRTUAL_ENV is dropped, or uv prints a warning with a cache path of this machine into the log
TEST_CMD = ('unset VIRTUAL_ENV; UV_PROJECT_ENVIRONMENT="$HOME/.cache/al-demo/flask-venv" uv run --locked --no-default-groups '
            '--group tests pytest -q -p no:cacheprovider tests/test_helpers.py')
# run in AUTHORED: the three checks and the review gate of the authored definition (five rows) …
EXCERPT = "grep -nE '\\(cmd (lint|typecheck|test) |\\(review auto|#:require-c' agentic.rkt | cut -c1-96"
# … and its protected paths (two rows; the second holds src/flask/sessions.py)
PROTECTED_SHOWN = "grep -n '#:protected' agentic.rkt | fold -w 96"
# the caption of 02-layer → the substrings of the shown rows that stay bright under it
HIGHLIGHTS: dict[int, tuple[str, ...]] = {1: ("(cmd ",), 2: ("#:protected", "sessions.py"),
                                          3: ("(review auto", "#:require-checks")}

# every `al …` command a scene shows, as typed (tests/tools/test_demo_video.py runs each with --help)
COMMANDS: dict[str, tuple[str, ...]] = {
    "init": ("al", "init", "flask"),
    "adopt": ("al", "adopt", "flask.al"),
    "gen": ("al", "gen", "flask.al"),
    "check": ("al", "check", "flask.al"),
    "run": ("al", "run", "flask.al", "/feature", PROMPT_A),
}
SAMPLES: dict[str, str] = {}
FORGEJO_IGNORE: tuple[str, ...] = ("favicon",)      # substrings of console errors of the Forgejo page that do not stop 04-pr

PLAN = """# Feature: FLASK_DEBUG=off turns debug mode off

`get_debug_flag()` in `src/flask/helpers.py` treats `0`, `false` and `no` as off. `off` turns debug
mode on today. Make `off` mean off.

## Steps

1. add `off` to the values `get_debug_flag()` treats as off
2. add a test for `FLASK_DEBUG=off` to `tests/test_helpers.py`

## How it is proven

### Proof plan
| # | Criterion | Proven by | Writes | Needs |
|---|---|---|---|---|
| C1 | the helper tests pass, the new one for `off` included | `test` | - | none |
| C2 | every file under src/flask compiles | `lint` | - | none |
"""
REFUSAL = "the note repeats the documentation and has no test"
NO_FORGE = ("no Forgejo instance for this work folder: run `make.py forgejo up --work DIR` first (needs podman); "
            "the testbed story never fakes its pull request")
COMMIT_DATE = "2026-01-01T00:00:00+00:00"


# ------------------------------------------------------------------------------------------ the flask copy and its layers

def _git_out(repo: Path, *args: str, env: dict | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env, timeout=timeout)


def _pinned_source(work: Path) -> tuple[Path, str]:
    """Where the pinned commit of flask is: the integration suite's checkout when it holds it, else a
    hidden shallow fetch into work/.flask-source."""
    commit = str(PIN["commit"])
    if (CACHE / ".git").exists() and _git_out(CACHE, "cat-file", "-e", commit + "^{commit}").returncode == 0:
        return CACHE, commit
    source = work / ".flask-source"
    if not source.is_dir() or _git_out(source, "cat-file", "-e", commit + "^{commit}").returncode != 0:
        shutil.rmtree(source, ignore_errors=True)
        source.mkdir(parents=True)
        _git(source, "init", "-q")
        fetched = _git_out(source, "fetch", "-q", "--depth", "1", str(PIN["url"]), commit)
        if fetched.returncode != 0:
            shutil.rmtree(source, ignore_errors=True)
            raise SceneStopped("the pinned flask is not on this machine: it needs the checkout testbeds/flask "
                               "(tests/integration) or network to fetch it")
    return source, commit


def _set_origin(app: Path, forge: dict) -> None:
    """The forge's repository as `origin` of the copy, and `main` pushed to it (again: a no-op)."""
    origin = forge.get("git") or f"{forge['url']}/{forge['owner']}/{forge['repo']}.git"
    if _git_out(app, "remote", "get-url", "origin").returncode != 0:
        _git(app, "remote", "add", "origin", str(origin))
    if str(origin).startswith(("http://", "https://")):
        # in the copy's own config, never in a URL that a command could print
        basic = base64.b64encode(f"{forge['owner']}:{forge['token']}".encode()).decode()
        _git(app, "config", "http.extraHeader", f"Authorization: Basic {basic}")
    _git(app, "push", "-q", "origin", "main")


def testbed_site(work: Path, source: Path | None = None, commit: str | None = None, forge: dict | None = None) -> dict:
    """A copy of flask at work/flask: the tree of `commit` in `source` (default: the pinned commit) and
    no history, committed once by a neutral identity at a fixed date, so every scene's copy has the same
    commit. With `forge`, its repository is the copy's origin and holds `main`. A folder already there
    is used as it is. The layer work/flask.al is not made here."""
    app = work / "flask"
    if not app.is_dir():
        if source is None or commit is None:
            source, commit = _pinned_source(work)
        archive = _git_out(source, "archive", "--format=tar", commit)
        if archive.returncode != 0:
            raise SceneStopped(f"git archive of flask at {commit[:12]} failed: {archive.stderr.decode(errors='replace')[-300:]}")
        app.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(app, filter="data")
        _git(app, "init", "-q", "-b", "main")
        _git(app, "add", "-A")
        dated = {**os.environ, "GIT_AUTHOR_DATE": COMMIT_DATE, "GIT_COMMITTER_DATE": COMMIT_DATE}
        done = _git_out(app, "-c", "user.name=Showcase", "-c", "user.email=showcase@example.org", "-c", "commit.gpgsign=false",
                        "commit", "-qm", "flask", env=dated)
        if done.returncode != 0:
            raise SceneStopped(f"the flask copy could not be committed: {done.stderr.decode(errors='replace')[-300:]}")
    if forge is not None:
        _set_origin(app, forge)
    return {"work": work, "app": app, "layer": work / "flask.al"}


def install_flask_layer(work: Path) -> Path:
    """Hidden setup after `al init flask`: the authored definition as work/flask.al/agentic.rkt, its root
    pointing at the copy. The LLM.txt that `al init` wrote stays."""
    layer = work / "flask.al"
    text = (AUTHORED / "agentic.rkt").read_text(encoding="utf-8")
    there, here = '#:root "../../testbeds/flask"', '#:root "../flask"'
    if text.count(there) != 1:
        raise SceneStopped("tests/authored/flask/agentic.rkt does not name its root as ../../testbeds/flask")
    layer.mkdir(parents=True, exist_ok=True)
    (layer / "agentic.rkt").write_text(text.replace(there, here), encoding="utf-8")
    return layer


def _racket_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _mock_text(path: Path, added: str) -> str:
    """The copy's own text of `path` plus `added`: what a mock build writes. The mock agent fills $1 and
    ${name} in such a text, so one that holds either is refused."""
    text = path.read_text(encoding="utf-8")
    if "${" in text or "$ARGUMENTS" in text or any(f"${digit}" in text for digit in "123456789"):
        raise SceneStopped(f"{path.name} of the pinned flask holds a $ placeholder the mock agent would fill")
    return text + added


def demo_layer(work: Path, forge: dict, test_cmd: str = TEST_CMD, generated: bool = False) -> dict:
    """The layer work/flask.al of the runs in parts 4 and 5, beside the copy testbed_site made: its
    definition (flask_layer.rkt.in) with the forge and the test command filled in, the stand-in's
    settings, the mock agent's scripts and local.env with the forge's access value (mode 0600, the only
    file that holds it). Written once; with generated=True `al gen` has run too."""
    app, layer = work / "flask", work / "flask.al"
    if not (layer / "replay.json").is_file():
        mock = layer / "mock"
        for folder in (mock / "tidy", mock / "strict"):
            folder.mkdir(parents=True, exist_ok=True)
        text = LAYER_SOURCE.read_text(encoding="utf-8")
        for key, value in (("@FORGE_HOST@", forge["url"]), ("@FORGE_REPO@", f"{forge['owner']}/{forge['repo']}"),
                           ("@REPLAY@", str(REPLAY)), ("@REPLAY_CONFIG@", str((layer / "replay.json").resolve())),
                           ("@TEST_CMD@", test_cmd)):
            text = text.replace(key, _racket_string(value))
        (layer / "agentic.rkt").write_text(text, encoding="utf-8")
        shutil.copyfile(AUTHORED / "LLM.txt", layer / "LLM.txt")
        secret = layer / "local.env"
        secret.unlink(missing_ok=True)
        with os.fdopen(os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as out:
            out.write(f"{forgejo.TOKEN_VAR}={forge['token']}\n")
        os.chmod(secret, 0o600)
        (mock / "classify.json").write_text(json.dumps({"output": json.dumps({"class": "feature", "reason": "new behaviour"})}))
        (mock / "plan.json").write_text(json.dumps({"layer_files": {"specs/feature-debug-flag-off.md": PLAN},
                                                    "output": "specs/feature-debug-flag-off.md"}))
        (mock / "commit.json").write_text(json.dumps({"commit": True, "output": "Committed the change with a one-line message."}))
        sessions = _mock_text(app / "src" / "flask" / "sessions.py",
                              "\n# The cookie defaults are documented in docs/web-security.rst.\n")
        (mock / "tidy" / "build.json").write_text(json.dumps({"files": {"src/flask/sessions.py": sessions},
                                                              "output": "Added a note to the session module."}))
        readme = _mock_text(app / "README.md", "\nDebug mode is described in the documentation of the flask command.\n")
        (mock / "strict" / "build.json").write_text(json.dumps({"files": {"README.md": readme},
                                                                "output": "Added a note about debug mode to the README."}))
        # the two reviewers run as the steps review-1 and review-2 (kit/runtime/adw_modules/review.py)
        for step, detail, resolution in (
                ("review-1", "README.md: " + REFUSAL, "link the documentation instead of repeating it, or drop the note"),
                ("review-2", "README.md: nothing in the plan's proof covers a change to the README",
                 "say in the plan how the note is proven, or leave the README as it is")):
            verdict = {"success": False, "summary": REFUSAL,
                       "issues": [{"severity": "blocker", "description": detail, "resolution": resolution,
                                   "artefact": "", "criterion": "", "kind": "needs-person"}]}
            (mock / "strict" / f"{step}.json").write_text(json.dumps({"output": json.dumps(verdict)}))
        (layer / "replay.json").write_text(json.dumps({"test": test_cmd}))      # last: the layer is complete
    if generated and not (layer / "agentic-layer.json").is_file():
        hidden([AL, "gen", layer], work)
    return {"work": work, "app": app, "layer": layer}


def require_forge(work: Path) -> dict:
    """{"url", "owner", "repo", "token"} of the reachable instance recorded in `work`, or SceneStopped."""
    found = forgejo.status(work)
    if found is None or not forgejo.reachable(found["url"]):
        raise SceneStopped(NO_FORGE)
    return found


def pull_request_url(layer: Path, run: str) -> str:
    pr = (run_state(layer, run).get("ship") or {}).get("pr")
    url = pr.get("url") if isinstance(pr, dict) else ""
    if not url:
        raise SceneStopped(f"run {run} records no pull request")
    return str(url)


def close_handoff(forge: dict, layer: Path, run: str) -> list[int]:
    """Close the open `[needs human]` pull request(s) of the stopped run `run` on the forge, and
    return their numbers. Forgejo refuses a second open pull request for the same head and base
    (HTTP 409), and the ship step of the resumed run opens the delivery one with a plain POST."""
    branch = str(run_state(layer, run).get("branch") or "")
    if not branch:
        raise SceneStopped(f"run {run} records no branch")
    base = f"{forge['url'].rstrip('/')}/api/v1/repos/{forge['owner']}/{forge['repo']}/pulls"
    headers = {"Authorization": f"token {forge['token']}", "Content-Type": "application/json", "Accept": "application/json"}
    status, data = forgejo._http("GET", base + "?state=open&limit=50", headers, None)
    try:
        listed = json.loads(data) if status == 200 else None
    except ValueError:
        listed = None
    if not isinstance(listed, list):
        raise SceneStopped(f"the forge did not list its pull requests (HTTP {status})")
    closed = []
    for pull in listed:
        if not (isinstance(pull, dict) and pull.get("state") == "open" and (pull.get("head") or {}).get("ref") == branch
                and "[needs human]" in str(pull.get("title") or "")):
            continue
        number = pull.get("number")
        status, _data = forgejo._http("PATCH", f"{base}/{number}", headers, json.dumps({"state": "closed"}).encode("utf-8"))
        if status not in (200, 201):
            raise SceneStopped(f"the forge did not close pull request {number} (HTTP {status})")
        closed.append(number)
    return closed


def _guard(ctx: Ctx, forge: dict) -> None:
    """The forge's access value joins the values no frame may hold (a copy: the list is shared between
    scenes), so rules.find_private blanks it and stops the scene if it ever reached a page or a transcript."""
    ctx.values = {**ctx.values, "hosts": [*ctx.values.get("hosts", []), forge["token"]]}


def _forge_site(ctx: Ctx, forge: dict) -> dict:
    """In this scene's work folder: the flask copy with the forge as its origin, and the generated demo layer."""
    _guard(ctx, forge)
    site = testbed_site(ctx.home_work, forge=forge)
    demo_layer(site["work"], forge, generated=True)
    return site


# ------------------------------------------------------------------------------------------ parts 1 to 3

def scene_01_problem(ctx: Ctx) -> list[Part]:
    flask = ["Flask", "a Python web framework", "one of the five pinned example codebases"]
    source = ["# docs/EXAMPLES.md"]
    stakes = ["Where mistakes spread", "dependencies locked in uv.lock", "any warning fails the test suite",
              "sessions.py signs every user's cookie"]
    needs = ["An agent needs to know", "how do I run the tests?", "what must I never touch?", "how do I prove my change works?"]
    return [_frames_part(ctx, [(0, flask + source, lambda: card_frames(flask, source)),
                               (1, stakes, lambda: card_frames(stakes)),
                               (2, needs, lambda: card_frames(needs))])]


def scene_02_layer(ctx: Ctx) -> list[Part]:
    import term
    checks = shell(ctx, EXCERPT, AUTHORED)
    protected = shell(ctx, PROTECTED_SHOWN, AUTHORED)
    rows = checks.output.splitlines() + protected.output.splitlines()
    missing = [s for keep in HIGHLIGHTS.values() for s in keep if not any(s in row for row in rows)]
    if (len(checks.output.splitlines()), len(protected.output.splitlines())) != (5, 2) or missing:
        raise SceneStopped(f"02-layer: the excerpts of tests/authored/flask/agentic.rkt are not the 5 and 2 rows the captions "
                           f"talk about (missing {missing}):\n{checks.output}{protected.output}")
    title = "al — the real layer of flask (tests/authored/flask/agentic.rkt)"
    if len(title) > term.COLS:
        title = "al — the real layer of flask"
    # the second command is typed when its own caption starts: both under the first caption take longer than it has
    part = terminal_part(ctx, title, [(0, checks), (2, protected)])
    typed = (checks.command, protected.command)

    def still(cue: int, steps: list):
        return highlight_frame(ctx, title, steps, keep=lambda t, k=HIGHLIGHTS[cue]: t not in typed and any(s in t for s in k))

    first, second, third = still(1, [checks]), still(2, [checks, protected]), still(3, [checks, protected])
    part.segments.insert(1, Segment(1) if first is None else Segment(1, [first]))
    if second is not None:
        part.segments[2].frames.append(second)
    part.segments.append(Segment(3) if third is None else Segment(3, [third]))
    generates = card_part(ctx, [(4, ["One definition generates", "prompts for each kind of job", "workflows and their checks",
                                     "the rules and the gate", "a live dashboard"], None)])
    return [part, generates]


def scene_03_use(ctx: Ctx) -> list[Part]:
    site = testbed_site(ctx.home_work)
    if site["layer"].exists():                  # a layer left by an earlier recording: `al init` makes it again on screen
        shutil.rmtree(site["layer"])
    steps = [(0, shell(ctx, COMMANDS["init"], site["work"]))]
    install_flask_layer(site["work"])
    steps += [(1, shell(ctx, COMMANDS["adopt"], site["work"])),
              (2, shell(ctx, COMMANDS["gen"], site["work"])),
              (3, shell(ctx, COMMANDS["check"], site["work"]))]
    adopt, gen, check = (step.output for _cue, step in steps[1:])
    if "nothing to adopt" not in adopt or "generated " not in gen or not check.strip().endswith("check: ok"):
        raise SceneStopped(f"03-use: al adopt, al gen or al check did not print what the captions say:\n{adopt}\n{gen}\n{check}")
    ctx.stand_in("03-use: agentic.rkt is the committed authored definition (tests/authored/flask/agentic.rkt); in use a model "
                 "writes it with al author.")
    start = " ".join(fill(COMMANDS["run"][:4], SAMPLES)) + f' "{PROMPT_A}"'
    return [terminal_part(ctx, "al — from a repository to its layer", steps),
            card_part(ctx, [(4, ["Start a run", "from the command line, or the dashboard's Start panel"], [start])])]


# ------------------------------------------------------------------------------------------ part 4

def scene_04_run(ctx: Ctx) -> list[Part]:
    import browser_clips
    forge = require_forge(ctx.work)
    site = _forge_site(ctx, forge)
    layer, app = site["layer"], site["app"]
    # once, hidden: flask's locked dependencies are installed before the recorded check needs them
    hidden(["sh", "-c", TEST_CMD], app, timeout=1500)
    dirty = _git_out(app, "status", "--porcelain").stdout.decode(errors="replace")
    if dirty.strip():
        raise SceneStopped(f"04-run: running flask's tests left files in the copy:\n{dirty}")
    running = _serve(layer)
    try:
        with Browser(ctx, running.base, 0, len(ctx.scene.captions), ignore=("favicon", "409")) as b:
            page = b.page
            browser_clips.open_(page, "/layers")
            page.wait_for_selector(".start-panel .start-prompt")
            _to_top(page, ".start-panel", above=60)
            b.spot(".start-panel")
            b.cue(0)
            b.ready(1, 0.3)
            b.spot(".start-prompt")
            b.cue(1)
            before = runs_of(layer)
            page.select_option(".start-target", "workflow:change")
            b.pause(0.6)
            browser_clips._type(page, ".start-prompt", PROMPT_A)
            b.pause(1.0)
            page.click(".start-go")
            browser_clips.until(page, "location.hash.startsWith('#/run/flask/')", timeout=30000)
            run = page.evaluate("location.hash").rsplit("/", 1)[1]
            run_route = f"/run/flask/{run}"
            page.wait_for_selector(_node("build"), timeout=60000)
            _to_top(page, ".graph-wrap", above=40)
            b.spot(_node("build"))
            b.cue(2)
            wait_for("the build step", lambda: step_status(layer, run, "build") != "", 120, page)
            b.ready(3, 0.6)
            _open_step(b, "build")
            wait_for("the first tool call of the build", lambda: page.locator(".ev.k-tool").count() > 0
                     or step_status(layer, run, "build") == "passed", 120, page)
            b.spot(".ev.k-tool", last=True)          # SPOTTER moves the outline to each new tool call
            b.cue(3)
            wait_for("the end of the build", lambda: step_status(layer, run, "build") not in ("", "running"), 300, page)
            b.pause(1.0)
            browser_clips.open_(page, run_route)
            page.wait_for_selector(_node("test"))
            wait_for("the test check", lambda: step_status(layer, run, "test") != "", 120, page)
            _open_step(b, "test")
            wait_for("the log of the test check", lambda: page.locator(".log span").count() > 0, 120, page)
            b.spot(".log")
            b.cue(4)
            wait_for("the end of the run", lambda: run_state(layer, run).get("status") not in (None, "", "running")
                     and run_state(layer, run).get("ended"), 600, page)
            state = run_state(layer, run)
            steps = [(s.get("name"), s.get("status")) for s in state.get("steps", [])]
            if state.get("status") != "passed" or sorted(steps) != sorted((name, "passed") for name in STEPS_A):
                raise SceneStopped(f"04-run: the run ended {state.get('status')} with steps {steps}; nothing is recorded")
            if new_run(layer, before) != run:
                raise SceneStopped("04-run: the page is not on the run it started")
            pr = pull_request_url(layer, run)
            b.ready(5)
            browser_clips.open_(page, run_route)
            page.wait_for_selector(_node("review"))
            page.click(_node("review"))
            page.wait_for_selector(".step-panel .step-rounds")
            _to_top(page, ".step-panel .step-rounds", above=120)
            b.spot(".step-panel .step-rounds")
            b.cue(5)
            b.ready(6)
            browser_clips.open_(page, run_route)
            page.wait_for_selector(_node("ship") + ".st-passed")
            _to_text(page, "Outcome")
            number = (state.get("ship") or {}).get("pr", {}).get("number")
            body = page.inner_text("body")
            if pr not in body and f"#{number}" not in body:
                raise SceneStopped("04-run: the run page does not show the pull request the run opened")
            b.spot(".outcome-block", "Gate and ship")
            b.cue(6)
            b.ready(7, 0.4)
            _to_top(page, ".graph-wrap", above=40)
            b.spot(_node("ship") + ".st-passed")
            b.cue(7)
            b.finish()
        (ctx.work / "testbed-run-a.json").write_text(json.dumps({"work": str(site["work"]), "layer": str(layer), "run": run,
                                                                 "pr": pr}))
        ctx.stand_in("04-run: classify, plan, commit and both reviewers are the mock agent; the build is the scripted stand-in "
                     "tools/demovideo/flask_replay.py, which really edits the worktree and runs flask's tests. No model is called.")
        ctx.stand_in("04-run: the layer is tools/demovideo/flask_layer.rkt.in (flask's protected paths, one test module), not "
                     "the authored definition of parts 2 and 3; the forge is a local Forgejo started by make.py forgejo up.")
        return [b.part]
    finally:
        browser_clips._stop(running)


def _run_a(ctx: Ctx) -> dict:
    """The run of scene 04-run when it passed and its layer is still there; else one made here, hidden,
    the same way."""
    record = ctx.work / "testbed-run-a.json"
    if record.is_file():
        found = json.loads(record.read_text())
        if run_state(Path(found["layer"]), found["run"]).get("status") == "passed":
            return found
    site = _forge_site(ctx, require_forge(ctx.work))
    before = runs_of(site["layer"])
    hidden([AL, "run", site["layer"], "/change", PROMPT_A], site["work"], timeout=1500)
    run = new_run(site["layer"], before)
    return {"work": str(site["work"]), "layer": str(site["layer"]), "run": run, "pr": pull_request_url(site["layer"], run)}


def scene_04_pr(ctx: Ctx) -> list[Part]:
    forge = require_forge(ctx.work)
    _guard(ctx, forge)
    found = _run_a(ctx)
    layer, run = Path(found["layer"]), found["run"]
    url = pull_request_url(layer, run)
    branch = str(run_state(layer, run).get("branch") or "")
    with Browser(ctx, forge["url"], 0, 2, work=Path(found["work"]), ignore=FORGEJO_IGNORE) as b:
        page = b.page
        page.goto(url)
        try:
            wait_for("the pull request page", lambda: bool(branch) and branch in page.inner_text("body")
                     or PROMPT_A in page.inner_text("body"), 30, page)
        except SceneStopped:
            raise SceneStopped("04-pr: the Forgejo page does not show the pull request") from None
        # an outline that finds nothing never stops the scene; the missing text above does
        b.spot(".issue-title") or b.spot("h1")
        b.cue(0)
        b.ready(1)
        for selector in (".comment .render-content", ".markup"):
            if page.locator(selector).count():
                page.locator(selector).first.scroll_into_view_if_needed()
                b.spot(selector)
                break
        b.cue(1)
        b.finish()
    ctx.stand_in("04-pr: the page is the local Forgejo instance started by make.py forgejo up for this recording and removed "
                 "after it; the pull request is the one the run of 04-run opened.")
    return [b.part]


# ------------------------------------------------------------------------------------------ part 5

def scene_05_proof(ctx: Ctx) -> list[Part]:
    import browser_clips
    found = _run_a(ctx)
    running = _serve(Path(found["layer"]))
    try:
        with Browser(ctx, running.base, 0, len(ctx.scene.captions), work=Path(found["work"])) as b:
            page = b.page
            browser_clips.open_(page, f"/run/flask/{found['run']}")
            page.wait_for_selector("[data-role='proof-plan']")
            card = page.locator("[data-role='proof-plan']")
            _to_text(page, "Proof plan", above=90)
            rows = card.locator("tr[data-criterion]")
            states = [rows.nth(i).get_attribute("data-state") for i in range(rows.count())]
            if states != ["proven", "proven"]:
                raise SceneStopped(f"05-proof: the proof table shows {states}, not two proven claims")
            b.spot("[data-role='proof-plan']")
            b.cue(0)
            b.ready(1, 0.3)
            rows.nth(0).hover()
            b.spot("[data-role='proof-plan'] tr[data-criterion]")
            b.cue(1)
            rows.nth(1).hover()
            b.spot("[data-role='proof-plan'] tr[data-criterion]", last=True)
            b.finish()
        ctx.stand_in("05-proof: the run of 04-run (mock agent and scripted stand-in).")
        return [b.part]
    finally:
        browser_clips._stop(running)


def scene_05_protected(ctx: Ctx) -> list[Part]:
    import browser_clips
    forge = require_forge(ctx.work)
    site = _forge_site(ctx, forge)
    layer = site["layer"]
    before = runs_of(layer)
    hidden([AL, "run", layer, "/tidy", PROMPT_B], site["work"], expect=1, timeout=1500)
    run = new_run(layer, before)
    if "evidence:needs-person" not in json.dumps(run_state(layer, run)):
        raise SceneStopped(f"05-protected: run {run} did not stop at evidence:needs-person")
    if not close_handoff(forge, layer, run):      # hidden: Forgejo refuses a second open pull request per branch
        raise SceneStopped(f"05-protected: run {run} has no open [needs human] pull request to close")
    route, row = f"/run/flask/{run}", f"tbody tr[data-key='flask/{run}']"
    running = _serve(layer)
    try:
        # the dashboard's own loop asks the layer what the run waits for, every APPROVABLE_EVERY (5 s)
        wait_for("the dashboard to offer Approve & resume", lambda: _waits_for_approval(layer, run), 90)
        with Browser(ctx, running.base, 0, 3) as first:
            page = first.page
            browser_clips.open_(page, route)
            page.wait_for_selector("[data-role='proof-plan'] .proof-stop")
            _to_text(page, "Proof plan", above=70)
            if APPROVAL not in page.locator("[data-role='proof-plan'] .proof-stop").inner_text():
                raise SceneStopped("05-protected: the run page does not ask for " + APPROVAL)
            first.spot("[data-role='proof-plan'] .proof-stop")
            first.cue(0)
            first.ready(1)
            browser_clips.open_(page, "/runs")
            page.wait_for_selector(f"{row} button.approve-resume", timeout=30000)
            first.spot(row)
            first.cue(1)
            first.ready(2)
            page.click(f"{row} button.approve-resume")
            page.wait_for_selector("[data-role='approve-resume-dialog']")
            browser_clips._type(page, "[data-role='approve-resume-dialog'] input[aria-label='your name']", WHO)
            first.spot("[data-role='approve-resume-dialog']")
            first.cue(2)
            first.pause(3.0)
            page.click("[data-role='approve-resume-dialog'] button.approve-resume-go")
            try:
                page.wait_for_selector("[data-role='approve-resume-dialog']", state="detached", timeout=30000)
                page.wait_for_function("() => (document.querySelector('.start-report') || {}).textContent"
                                       "?.includes('resuming from step evidence')", timeout=30000)
            except Exception as error:      # playwright's TimeoutError
                raise SceneStopped(f"05-protected: Approve & resume did not report `resuming from step evidence`: {error}") from None
            first.spot(".start-report")
            first.finish()
        wait_for(f"run {run} to pass after the approval", lambda: run_state(layer, run).get("status") == "passed", 300)
        approvals = run_state(layer, run).get("approvals") or []
        if not any(a.get("who") == WHO and a.get("via") == "dashboard" for a in approvals):
            raise SceneStopped(f"05-protected: run {run} records no approval by {WHO} from the dashboard: {approvals}")
        pull_request_url(layer, run)                # only after the approval is its pull request on the forge
        with Browser(ctx, running.base, 3, len(ctx.scene.captions)) as last:
            page = last.page
            browser_clips.open_(page, route)
            page.wait_for_selector(_node("ship") + ".st-passed", timeout=20000)
            last.spot(_node("ship"))
            last.cue(3)
            last.pause(3.0)
            _to_text(page, "Proof plan", above=70)
            last.finish()
        ctx.stand_in(f"05-protected: a second run of the demo layer; the mock agent's build edits src/flask/sessions.py, a "
                     f"protected path. The approval is given in the dashboard (Approve & resume) by the fictional {WHO}.")
        ctx.stand_in("05-protected: before the approval the recorder closes the run's [needs human] pull request through the "
                     "Forgejo API, unseen: Forgejo refuses a second open pull request for a branch, and the resumed run "
                     "opens the delivery one.")
        return [first.part, last.part]
    finally:
        browser_clips._stop(running)


def scene_05_review_no(ctx: Ctx) -> list[Part]:
    import browser_clips
    forge = require_forge(ctx.work)
    site = _forge_site(ctx, forge)
    layer = site["layer"]
    before = runs_of(layer)
    hidden([AL, "run", layer, "/strict", PROMPT_C], site["work"], timeout=1500)      # handed-over exits 0, as passed does
    run = new_run(layer, before)
    state = run_state(layer, run)
    if state.get("status") != "handed-over" or "review-rejected" not in json.dumps((state.get("ship") or {}).get("handed_over")):
        raise SceneStopped(f"05-review-no: run {run} ended {state.get('status')}, not handed over after a refused review")
    running = _serve(layer)
    try:
        with Browser(ctx, running.base, 0, len(ctx.scene.captions)) as b:
            page = b.page
            browser_clips.open_(page, f"/run/flask/{run}")
            page.wait_for_selector(_node("review"))
            page.click(_node("review"))
            page.wait_for_selector(".step-panel .step-rounds")
            page.locator(".step-panel .step-rounds").scroll_into_view_if_needed()
            if "not approved" not in page.locator(".step-panel .step-rounds").inner_text():
                raise SceneStopped("05-review-no: the review step does not show a refused round")
            b.spot(".step-panel .step-rounds")
            b.cue(0)
            b.ready(1)
            page.evaluate("window.scrollTo(0, 0)")
            if page.locator(".badge.handed-over").count() == 0:
                raise SceneStopped("05-review-no: the run page does not say the run was handed to a person")
            b.spot(".badge.handed-over")
            b.cue(1)
            b.finish()
        ctx.stand_in("05-review-no: a third scripted run of the demo layer whose mock reviewers refuse "
                     "(mock/strict/review-1.json and review-2.json); no model reviewed anything.")
        return [b.part]
    finally:
        browser_clips._stop(running)


SCENES = {
    "01-problem": scene_01_problem,
    "02-layer": scene_02_layer,
    "03-use": scene_03_use,
    "04-run": scene_04_run,
    "04-pr": scene_04_pr,
    "05-proof": scene_05_proof,
    "05-protected": scene_05_protected,
    "05-review-no": scene_05_review_no,
    "06-fit": scenes.scene_06_fit,
    "07-close": scenes.scene_07_close,
}
