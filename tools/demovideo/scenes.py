"""The scenes of the five-minute demo video: one recorder function per scene id of narration.md.

Nothing here draws a frame of its own design except the cards: terminal scenes are real commands run
with tools/showcase/terminal_clips.run_shown and rendered by tools/showcase/term.py, browser scenes are
the dashboard in the host's Chromium, recorded by tools/showcase/browser_clips.Recorder. A scene function
takes a `Ctx` and returns its parts in order; make.py fits each part to its length, lays the captions of
narration.md over it and writes the clip.

  demo_site    Rota (a copy of tests/rotademo/app) and beside it the scripted layer `rota.al`
  rota_site    a copy of the Rota app (tests/rotademo/app); install_rota_layer puts its real definition beside it
  COMMANDS     every `al …` command a terminal scene shows ({run} is filled at run time)
  SCENES       scene id → recorder function

What guides the eye: cards build line by line (card_frames), pictures of the recorded Rota runs
(PICTURES, committed under docs/demo/rotademo) are shown on cards with their source (picture_frames,
gif_frames), a terminal scene can dim every row but the ones the voice talks about (highlight_frame), and
the dashboard is shown at page zoom ZOOM with an outline on the element the voice talks about (Browser.spot).

Captured text is changed in two ways only, and both are listed in the clip's JSON file: paths of this
machine are shown short (`~/work`, `.`, `agentic`), and a private match is blanked with █ (rules.blank).
This module imports only the standard library at the top: Pillow, Playwright and the showcase's
renderers are imported by the functions that draw or record.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SHOWCASE = ROOT / "tools" / "showcase"
for _folder in (str(ROOT / "tools"), str(SHOWCASE), str(HERE)):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

import rota_agent  # noqa: E402  (standard library only; importing it runs nothing)
import rules  # noqa: E402

AL = ROOT / "tools" / "al"
STAND_IN = HERE / "rota_agent.py"
LAYER_SOURCE = HERE / "demo_layer.rkt.in"
PROMPT_A = "Enforce 11 hours of rest between shifts"
PROMPT_B = "Note in CONTRIBUTING.md where the rest rule belongs"
APPROVAL = "edit:CONTRIBUTING.md"
STEPS_A = ("classify", "plan", "prove", "build", "test", "isolation", "commit", "evidence", "review", "ship")
RID_REFUSED = "c1000101"                        # the fixture run of 05-review-no
REPOSITORY = "github.com/dancegit/al"
WHO = "Ada Example"                             # the fictional person who approves in the dashboard (05-protected)

# Rota, the shift-scheduling app for hospital wards, and its real layer definition
ROTA = ROOT / "tests" / "rotademo"
PICTURES = {name: ROOT / "docs" / "demo" / "rotademo" / file for name, file in (
    ("board", "before.png"), ("rest", "a-rest-rule.gif"), ("merged", "a-pr-merged.png"), ("handoff", "b-pr-why-human-top.png"))}
# the forge of the Rota layer: a reserved example domain that nothing is sent to (al gen and al check
# only compile; the definition needs a base URL, built here so this file holds no address)
FORGE_BASE = "https" + ":" + "//forge.example.org"
# run in ROTA: lines 10, 12, 14, 18, 19, 42 and 43 of layer.rkt.in (never line 21, the forge line)
ROTA_EXCERPT = "grep -nE '\\((cmd|review auto) |#:protected|\"req|#:req' layer.rkt.in | cut -c1-96"
# the caption of 02-layer → the substrings of the excerpt's rows that stay bright under it
HIGHLIGHTS: dict[int, tuple[str, ...]] = {1: ("(cmd ",), 2: ("#:protected", "requirements.txt"),
                                          3: ("(review auto", "#:require-checks")}

# every `al …` command a scene shows, as typed (tests/tools/test_demo_video.py runs each with --help)
COMMANDS: dict[str, tuple[str, ...]] = {
    "init": ("al", "init", "rota"),
    "gen": ("al", "gen", "rota.al"),
    "check": ("al", "check", "rota.al"),
    "run": ("al", "run", "rota.al", "/feature", "Enforce 11 hours of rest between shifts"),
}
SAMPLES: dict[str, str] = {}

PLAN = """# Feature: 11 hours of rest between shifts

A worker may not be assigned to a shift that starts less than 11 hours after another of their shifts
ends, or that ends less than 11 hours before another starts.

## Steps

1. add the rest rule to `rules.py`
2. add two tests to `tests/test_rules.py`: less than 11 hours is refused, exactly 11 hours is allowed

## How it is proven

### Proof plan
| # | Criterion | Proven by | Writes | Needs |
|---|---|---|---|---|
| C1 | every Rota test passes, the new rest tests included | `test` | - | none |
| C2 | no route leaks or changes another organisation's data | `isolation` | - | none |
"""

LLM_TXT = """Rota: shift scheduling for hospital wards. All data is synthetic.
A small Flask and SQLite app: app.py holds the routes, rules.py the pure business rules.
Tenant data is read and written only through data.py, scoped to one organisation.
"""

CONTRIBUTING_NOTE = ("\nRest between shifts is a business rule like the others: it belongs in `rules.py`, "
                     "with its tests in `tests/test_rules.py`.\n")


class SceneStopped(RuntimeError):
    """A scene that must not be recorded as it is: the sentence says why."""


def fill(argv: tuple[str, ...], values: dict[str, str]) -> list[str]:
    out = []
    for arg in argv:
        for key, value in values.items():
            arg = arg.replace("{" + key + "}", value)
        out.append(arg)
    return out


# ------------------------------------------------------------------------------------------ what a scene returns

@dataclass
class Segment:
    """Frames of a terminal or card part. `cue`: the caption (its index in the scene) that starts with
    these frames, or None when they go on under the caption before."""
    cue: int | None
    frames: list = field(default_factory=list)


@dataclass
class Part:
    kind: str                                   # "frames" or "video"
    first: int                                  # the caption this part starts with
    segments: list[Segment] = field(default_factory=list)
    video: Path | None = None                   # a browser recording
    cues: list[tuple[float, int]] = field(default_factory=list)     # (seconds on the clock since it began, caption)
    wall: float = 0.0                           # seconds on the clock from the recording's beginning to its end


@dataclass
class Ctx:
    scene: rules.Scene
    work: Path                                  # --work DIR
    home_work: Path                             # this scene's neutral ~/work
    values: dict[str, list[str]]                # rules.machine_values of the real machine
    rehearse: bool = False
    blanked: list[dict] = field(default_factory=list)
    stand_ins: list[str] = field(default_factory=list)
    shortened: list[str] = field(default_factory=list)

    def offset(self, caption: int) -> float:
        """Seconds into the scene at which caption `caption` starts in the script; the scene's length
        for the index after the last one."""
        captions = self.scene.captions
        return captions[caption].offset if caption < len(captions) else self.scene.seconds

    def stand_in(self, sentence: str) -> None:
        if sentence not in self.stand_ins:
            self.stand_ins.append(sentence)

    def note_blanks(self, where: str, matches: dict[str, int]) -> None:
        for match, count in sorted(matches.items()):
            kind = next((h.kind for h in rules.find_private(match, self.values)), "home")
            what = match.lower() if kind == "token" else f"{len(match)} characters"
            same = next((b for b in self.blanked if (b["where"], b["kind"], b["what"]) == (where, kind, what)), None)
            if same is None:
                self.blanked.append({"where": where, "kind": kind, "count": count, "what": what})
            else:
                same["count"] = max(same["count"], count)       # a page is counted again at each caption

    def check(self, text: str, where: str) -> None:
        """Nothing private may be left in a text that is about to be shown."""
        hits = rules.find_private(text, self.values)
        if hits:
            raise SceneStopped(f"{self.scene.id}: {where} still holds a private match ({hits[0].kind}): …{hits[0].excerpt}…")

    def say(self, text: str) -> None:
        print(f"    {text}", flush=True)


# ------------------------------------------------------------------------------------------ the demo application

def _git(repo: Path, *args: str) -> None:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if cp.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {cp.stderr}")


def _al_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    env.update({"PYTHONUNBUFFERED": "1", "TERM": "dumb", "NO_COLOR": "1"})
    return env


def hidden(argv: list, cwd: Path, expect: int = 0, timeout: float = 540) -> str:
    """Setup that no frame shows; another exit code than expected stops the scene."""
    cp = subprocess.run([str(a) for a in argv], cwd=cwd, env=_al_env(), capture_output=True, text=True, timeout=timeout)
    if cp.returncode != expect:
        raise SceneStopped(f"setup `{' '.join(str(a) for a in argv)}` exited {cp.returncode}, expected {expect}\n"
                           f"{cp.stdout[-3000:]}\n{cp.stderr[-3000:]}")
    return cp.stdout + cp.stderr


def demo_site(work: Path, generated: bool = False) -> dict:
    """Rota (a copy of tests/rotademo/app, committed once by a neutral identity: rota_site) and beside it
    the scripted layer `rota.al`: its definition (demo_layer.rkt.in), LLM.txt and the mock agent's
    scripts. With generated=True `al gen` has run too. Rota's Python is not fetched here: warm_rota_python."""
    site = rota_site(work)
    app, layer = site["app"], site["layer"]
    if not (layer / "agentic.rkt").is_file():
        mock = layer / "mock"
        mock.mkdir(parents=True, exist_ok=True)
        (layer / "agentic.rkt").write_text(LAYER_SOURCE.read_text(encoding="utf-8").replace("@STAND_IN@", str(STAND_IN)),
                                           encoding="utf-8")
        (layer / "LLM.txt").write_text(LLM_TXT, encoding="utf-8")
        (mock / "classify.json").write_text(json.dumps({"output": json.dumps({"class": "feature", "reason": "new behaviour"})}))
        (mock / "plan.json").write_text(json.dumps({"layer_files": {"specs/feature-rest-rule.md": PLAN},
                                                    "output": "specs/feature-rest-rule.md"}))
        (mock / "commit.json").write_text(json.dumps({"commit": True, "output": "Committed the change with a one-line message."}))
        contributing = (app / "CONTRIBUTING.md").read_text(encoding="utf-8") + CONTRIBUTING_NOTE
        (mock / "build.json").write_text(json.dumps({"files": {"CONTRIBUTING.md": contributing},
                                                     "output": "Added a line on the rest rule to CONTRIBUTING.md."}))
    if generated and not (layer / "agentic-layer.json").is_file():
        hidden([AL, "gen", layer], work)
    return {"work": work, "app": app, "layer": layer}


def warm_rota_python(work: Path) -> None:
    """Resolve Rota's Flask and pytest once (uv's cache), so the first check of a recording does not wait on a download."""
    hidden([*rota_agent.PYTHON, "-c", "import flask, pytest"], work, timeout=600)


def add_refused_rota_run(layer: Path) -> str:
    """The fixture run of 05-review-no, written into the Rota layer in the record format of
    kit/dashboard/tests/test_review_rejected_model.py: the planted change of scenario C
    (tests/rotademo/planted), refused by both reviewers in three rounds and handed to a person."""
    import fixture_imports as fx
    f = fx.dash_fixtures()
    rec, write = f.rec, f.write
    rid, base = RID_REFUSED, 30000.0
    run = layer / "agents" / rid
    verdict = {"success": False, "summary": "Criterion 3 is not met: an expired worker can still be selected",
               "issues": [{"severity": "blocker",
                           "description": "templates/shifts.html still offers an expired worker in the assign dropdown; "
                                          "nothing disables the option.",
                           "resolution": "Disable the option of a worker whose registration has expired and show it in "
                                         "e2e/scenario.py."}]}
    plan = layer / "specs" / f"feature-{rid}.md"
    t = base
    records = [rec("classify", "run", "passed", t, t + 10, '{"class":"feature"}'),
               rec("plan", "run", "passed", t + 10, t + 40, str(plan)),
               rec("build", "run", "passed", t + 40, t + 90, "Applied the planted change."),
               rec("test", "check", "passed", t + 90, t + 100, "exit 0"),
               rec("isolation", "check", "passed", t + 100, t + 104, "exit 0"),
               rec("commit", "run", "passed", t + 104, t + 114, "Committed.")]
    t += 114
    records += [rec("review", "review", "rejected", t, t + 60, f"fail after 3 round(s): {verdict['summary']}"),
                rec("ship", "ship", "passed", t + 60, t + 70, "ok")]
    for n in (1, 2, 3):
        for i in (1, 2):
            name = f"review-{i}" if n == 1 else f"review-{n}-{i}"
            start = t + (n - 1) * 20 + (i - 1) * 10
            f.agent(run, name, start, start + 10, f.mock_raw(json.dumps(verdict)))
    patch = (ROTA / "planted" / "planted.patch").read_text(encoding="utf-8")
    changed = re.findall(r"^\+\+\+ b/(\S+)", patch, re.M)
    reasons = ["review-failed"]
    ship = {"action": "pr", "shipped": False, "target": f"agents/{rid}/PR.md", "commit": "3" * 40, "pr": None,
            "reasons": reasons, "handed_over": ["review-rejected", "review-failed"],
            "gate": {"action": "pr", "ship": False, "reasons": reasons, "diff_lines": 120, "changed_files": changed}}
    rounds = [{"round": n, "reviewers": [verdict, verdict], "verdict": {**verdict, "issues": verdict["issues"] * 2}}
              for n in (1, 2, 3)]
    review = {"mode": "auto", "verdict": {**verdict, "issues": verdict["issues"] * 2, "mode": "auto"}, "rounds": rounds}
    end = t + 70
    prompt = ("Show an Expired badge for an expired registration; an expired worker cannot be selected in the "
              "assign dropdown")
    write(run / "state.json", json.dumps(f.state(
        rid, "feature", prompt, "handed-over", base, end, records, klass="feature", branch=f"feature/{rid}-expired-badge",
        worktree=str(layer / "trees" / rid), plan=str(plan), review=review, ship=ship)), end)
    write(run / "run.log", f"[adw {rid}] feature start: class=feature dry_run=False\n", end)
    events = [{"ts": f.iso(t + n * 20), "seq": n, "type": "review.round", "step": "review", "round": n, "success": False,
               "reviewers": 2, "summary": verdict["summary"]} for n in (1, 2, 3)]
    write(run / "events.jsonl", "".join(json.dumps(event) + "\n" for event in events), end)
    return rid


def run_state(layer: Path, run: str) -> dict:
    try:
        return json.loads((layer / "agents" / run / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def step_status(layer: Path, run: str, step: str) -> str:
    return next((s.get("status", "") for s in run_state(layer, run).get("steps", []) if s.get("name") == step), "")


def wait_for(what: str, condition, timeout: float, page=None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if page is not None:
            page.wait_for_timeout(200)
        else:
            time.sleep(0.2)
    raise SceneStopped(f"timed out after {timeout:g} s waiting for {what}")


def new_run(layer: Path, before: set[str]) -> str:
    found = [p.name for p in (layer / "agents").iterdir() if p.name not in before and (p / "state.json").is_file()]
    if len(found) != 1:
        raise SceneStopped(f"expected one new run, found {found}")
    return found[0]


def runs_of(layer: Path) -> set[str]:
    return {p.name for p in (layer / "agents").iterdir()} if (layer / "agents").is_dir() else set()


def rota_site(work: Path) -> dict:
    """A copy of the Rota app (tests/rotademo/app) at work/rota, committed once by a neutral identity; a
    folder already there is used as it is. The layer work/rota.al is not made here: `al init` makes it on screen."""
    app = work / "rota"
    if not app.is_dir():
        shutil.copytree(ROTA / "app", app, ignore=shutil.ignore_patterns("__pycache__", "e2e-results", "*.db", ".pytest_cache"))
        _git(app, "init", "-q", "-b", "main")
        _git(app, "add", "-A")
        _git(app, "-c", "user.name=Showcase", "-c", "user.email=showcase@example.org", "commit", "-qm", "rota")
    return {"work": work, "app": app, "layer": work / "rota.al"}


def install_rota_layer(work: Path) -> Path:
    """Hidden setup after `al init rota`: the real Rota definition as work/rota.al/agentic.rkt (its
    placeholders filled with neutral values) and the folder `planted` its planter agent names. The
    LLM.txt and scout.json that `al init` wrote stay."""
    layer = work / "rota.al"
    text = (ROTA / "layer.rkt.in").read_text(encoding="utf-8")
    for key, value in (("@FORGE_HOST@", FORGE_BASE), ("@REPO@", "adw/rota"), ("@APP_DIR@", "rota")):
        text = text.replace(key, value)
    layer.mkdir(parents=True, exist_ok=True)
    (layer / "agentic.rkt").write_text(text, encoding="utf-8")
    if (layer / "planted").exists():
        shutil.rmtree(layer / "planted")
    shutil.copytree(ROTA / "planted", layer / "planted", ignore=shutil.ignore_patterns("__pycache__"))
    return layer


_EXAMPLE_ROW = re.compile(r"^\| \[([^\]]+)\]\(https:/{2}github\.com/[^)]+\) \| ([^|]+?) \|")


def example_rows() -> list[tuple[str, str]]:
    """(name, language) of each row of the table under "## The example repositories" in docs/EXAMPLES.md."""
    text = (ROOT / "docs" / "EXAMPLES.md").read_text(encoding="utf-8")
    at = text.find("## The example repositories")
    rows: list[tuple[str, str]] = []
    if at >= 0:
        for line in text[at:].splitlines()[1:]:
            if line.startswith("## "):
                break
            if m := _EXAMPLE_ROW.match(line):
                rows.append((m.group(1), m.group(2).strip()))
    if len(rows) < 5:
        raise SceneStopped(f"docs/EXAMPLES.md: the table of the example repositories has {len(rows)} rows, not 5")
    return rows


# ------------------------------------------------------------------------------------------ text shown in a terminal

def repository_paths() -> list[tuple[str, str]]:
    """How this repository's own paths are shown: the root as `.`, the layer folder as `agentic`."""
    pairs = [(str(ROOT), ".")]
    main = rules._main_checkout(ROOT)
    if main is not None and main != ROOT:
        pairs += [(str(main / "agentic"), "agentic"), (str(main), ".")]
    return pairs


def shorten(text: str, pairs: list[tuple[str, str]]) -> str:
    for real, display in pairs:
        text = text.replace(real, display)
    return text


@contextmanager
def _checked_after(pairs: list[tuple[str, str]], values: dict[str, list[str]]):
    """While a shown command runs, terminal_clips' own privacy check sees the text as it will be shown:
    this repository's paths short and private matches blanked."""
    import privacy
    import terminal_clips

    class Checked:
        PrivateText = privacy.PrivateText

        @staticmethod
        def check_private(text, forbidden=(), allowed=()):
            return privacy.check_private(rules.blank(shorten(text, pairs), values, whole_paths=True)[0], forbidden, allowed)

    saved = terminal_clips.privacy
    terminal_clips.privacy = Checked
    try:
        yield
    finally:
        terminal_clips.privacy = saved


def shell(ctx: Ctx, command: str | list[str] | tuple[str, ...], cwd: Path, expect: int | None = 0,
          interrupt_after: float | None = None, rows: int | None = None):
    """One shown command with its real output, as a term.Step without a caption. A string is a shell
    pipeline (run by `sh -c`, shown as typed); a list is an argv (`al` is this repository's tools/al).
    The output is cut to `rows` lines when given, and to 100 columns by the renderer."""
    import term
    import terminal_clips
    argv = ["sh", "-c", command] if isinstance(command, str) else list(command)
    typed = command if isinstance(command, str) else terminal_clips.shown(argv)
    if len(typed) > term.COLS - len(term.PROMPT):
        raise SceneStopped(f"{ctx.scene.id}: `{typed}` does not fit one line of the terminal")
    pairs = repository_paths()
    env = terminal_clips._env(ctx.home_work)
    with _checked_after(pairs, ctx.values):
        _code, text = terminal_clips.run_shown(argv, cwd, env, interrupt_after=interrupt_after, expect=expect)
    short = shorten(text, pairs)
    if short != text:
        ctx.shortened.append(f"`{typed}`: this repository's paths are shown as `.` and `agentic`")
    text, hits = rules.blank(short, ctx.values, whole_paths=True)
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.match] = counts.get(hit.match, 0) + 1
    ctx.note_blanks(f"`{typed}`", counts)
    if rows is not None:
        lines = text.splitlines()
        text = "\n".join(lines[:rows] + (["^C"] if interrupt_after is not None and len(lines) > rows else [])) + "\n"
    ctx.check(typed + "\n" + text, f"the output of `{typed}`")
    return term.Step(typed, text, "")


def terminal_part(ctx: Ctx, title: str, steps: list) -> Part:
    """A part from shown commands: `steps` is a list of (caption index or None, term.Step). The frames
    are term.render_frames' own, split where each step starts; the prompt held at the end is left out."""
    import term
    import terminal_clips
    transcript = term.Transcript([step for _cue, step in steps], title)
    log = terminal_clips.transcript_text(transcript)
    ctx.check(title + "\n" + log, "the transcript")
    for line in log.rstrip().splitlines():
        ctx.say(line[:140])
    part = Part("frames", next(cue for cue, _step in steps if cue is not None))
    if ctx.rehearse:
        part.segments = [Segment(cue) for cue, _step in steps if cue is not None]
        return part
    frames = list(term.render_frames(transcript))
    starts = [term.frame_count(term.Transcript(transcript.steps[:k], title)) - term.HOLD_END for k in range(len(steps))]
    starts.append(len(frames) - term.HOLD_END)
    for k, (cue, _step) in enumerate(steps):
        chunk = frames[starts[k]:starts[k + 1]]
        if cue is None and part.segments:
            part.segments[-1].frames += chunk
        else:
            part.segments.append(Segment(cue, chunk))
    return part


def shown_lines(transcript) -> list[tuple[str, str]]:
    """The lines term.render_frames shows after the last step's output: each command, then its output."""
    import term
    lines: list[tuple[str, str]] = []
    for step in transcript.steps:
        lines.append(("cmd", step.command))
        lines += [("out", line) for line in term.output_lines(step.output)]
    return lines


def highlight_frame(ctx: Ctx, title: str, steps: list, keep):
    """The terminal as it is after `steps` (term.Step), every visible row whose text does not satisfy
    `keep` dimmed: the rows the voice talks about stand out. None when rehearsing."""
    if ctx.rehearse:
        return None
    from PIL import Image, ImageDraw
    import term
    lines = shown_lines(term.Transcript(list(steps), title))
    visible = lines[-term.ROWS:]
    if not any(keep(text) for _kind, text in visible):
        raise SceneStopped(f"{ctx.scene.id}: {title}: no line to highlight")
    base = term._draw(title, lines, False, "").convert("RGBA")
    veil = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(veil)
    bg = tuple(int(term.BG[i:i + 2], 16) for i in (1, 3, 5))
    for row, (_kind, text) in enumerate(visible):
        if not keep(text):
            y = term.TITLE_H + 10 + row * term.LINE_H
            d.rectangle([0, y, term.W, y + term.LINE_H - 1], fill=(*bg, 170))
    return Image.alpha_composite(base, veil).convert("RGB")


# ------------------------------------------------------------------------------------------ cards

FADE_FRAMES, LINE_FRAMES = 6, 5


def card_image(lines: list[str], small: list[str] | None = None, shown: int | None = None):
    """A title card in the showcase's colours: a bold first line with the blue rule under it, then
    centred lines; `small` lines (real text of a file, with its source) are set in the terminal's font.
    The layout is that of every line; only the first `shown` of them (the head is one; None: all) are drawn."""
    from PIL import Image, ImageDraw
    import term
    img = Image.new("RGB", (term.W, term.H), term.BG)
    d = ImageDraw.Draw(img)
    head, body = term.font(term.CARD_SIZE + 12, True), term.font(term.CARD_SIZE)
    mono = term.font(term.FONT_SIZE - 2)
    for face, text in [(head, lines[0])] + [(body, line) for line in lines[1:]] + [(mono, line) for line in small or []]:
        if face.getlength(text) > term.CARD_WIDTH:
            raise ValueError(f"card line does not fit {term.CARD_WIDTH} px: {text!r}")
    every = len(lines) + len(small or [])
    shown = every if shown is None else shown
    height = 80 + 52 * len(lines[1:]) + (30 + 28 * len(small) if small else 0)
    y = (term.H - term.STRIP_H - height) // 2 + 24
    n = 0
    if n < shown:
        d.text((term.W // 2, y), lines[0], font=head, fill="#ffffff", anchor="mm")
        d.line([term.W // 2 - 60, y + 40, term.W // 2 + 60, y + 40], fill=term.BLUE, width=3)
    n += 1
    y += 96
    for line in lines[1:]:
        if n < shown:
            d.text((term.W // 2, y), line, font=body, fill=term.FG, anchor="mm")
        n += 1
        y += 52
    if small:
        y += 20
        left = (term.W - max(mono.getlength(line) for line in small)) // 2
        for line in small:
            if n < shown:
                d.text((left, y), line, font=mono, fill=term.DIM if line.startswith("#") else term.FG, anchor="lm")
            n += 1
            y += 28
    return img


def _fade_in(target) -> list:
    """FADE_FRAMES frames from the empty background to `target`."""
    from PIL import Image
    import term
    empty = Image.new("RGB", target.size, term.BG)
    return [Image.blend(empty, target, (k + 1) / FADE_FRAMES) for k in range(FADE_FRAMES)]


def card_frames(lines: list[str], small: list[str] | None = None) -> list:
    """A card that builds: its first line fades in, then each further line is added and held LINE_FRAMES
    frames; the last frame is the whole card."""
    every = len(lines) + len(small or [])
    frames = _fade_in(card_image(lines, small, shown=1))
    for shown in range(2, every + 1):
        frames += [card_image(lines, small, shown=shown)] * LINE_FRAMES
    if every == 1:
        frames.append(card_image(lines, small))
    return frames


def card_part(ctx: Ctx, cards: list[tuple[int, list[str], list[str] | None]]) -> Part:
    """A part of title cards: (caption index, lines, small lines) each; a card builds, then stays until the next."""
    for _cue, lines, small in cards:
        ctx.check("\n".join(lines + (small or [])), "a card")
        ctx.say("card: " + " / ".join(lines + (small or []))[:150])
    part = Part("frames", cards[0][0])
    for cue, lines, small in cards:
        part.segments.append(Segment(cue, [] if ctx.rehearse else card_frames(lines, small)))
    return part


def _picture_on_card(picture, title: str, source: str):
    """`picture` (an RGB image) scaled to fit under a bold title with the blue rule, in a 1 px border, with
    its source small and dim at the bottom."""
    from PIL import Image, ImageDraw
    import term
    head, small = term.font(term.CARD_SIZE + 12, True), term.font(term.FONT_SIZE - 2)
    for face, text in ((head, title), (small, source)):
        if face.getlength(text) > term.CARD_WIDTH:
            raise ValueError(f"picture text does not fit {term.CARD_WIDTH} px: {text!r}")
    img = Image.new("RGB", (term.W, term.H), term.BG)
    d = ImageDraw.Draw(img)
    d.text((term.W // 2, 70), title, font=head, fill="#ffffff", anchor="mm")
    d.line([term.W // 2 - 60, 110, term.W // 2 + 60, 110], fill=term.BLUE, width=3)
    room_w, room_h = 1180, term.H - term.STRIP_H - 170
    scale = min(room_w / picture.width, room_h / picture.height)
    size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
    left, top = (term.W - size[0]) // 2, 125 + (room_h - size[1]) // 2
    img.paste(picture.resize(size, Image.LANCZOS), (left, top))
    d.rectangle([left - 1, top - 1, left + size[0], top + size[1]], outline=term.BAR, width=1)
    d.text((term.W // 2, term.H - term.STRIP_H - 30), source, font=small, fill=term.DIM, anchor="mm")
    return img


def picture_image(path: Path, title: str, source: str, crop: tuple[int, int, int, int] | None = None):
    """One frame: the picture at `path` (cut to `crop`, a box, when given) under its title, with its source."""
    from PIL import Image
    with Image.open(path) as opened:
        picture = opened.convert("RGB")
    if crop is not None:
        picture = picture.crop(crop)
    return _picture_on_card(picture, title, source)


def picture_frames(path: Path, title: str, source: str, crop: tuple[int, int, int, int] | None = None) -> list:
    """The picture fading in, then held."""
    image = picture_image(path, title, source, crop)
    return _fade_in(image) + [image]


def gif_frames(path: Path, title: str, source: str, max_frames: int) -> list:
    """Each frame of the GIF at `path` laid out like picture_image, repeated for its own duration at
    rules.FPS; the first fades in, and the whole is cut to `max_frames`."""
    from PIL import Image, ImageSequence
    frames: list = []
    with Image.open(path) as gif:
        for k, frame in enumerate(ImageSequence.Iterator(gif)):
            image = _picture_on_card(frame.convert("RGB"), title, source)
            repeat = max(1, round(frame.info.get("duration", gif.info.get("duration", 100)) / 1000 * rules.FPS))
            frames += (_fade_in(image) if k == 0 else []) + [image] * repeat
            if len(frames) >= max_frames:
                break
    return frames[:max(1, max_frames)]


# ------------------------------------------------------------------------------------------ the dashboard in a browser

# Installed in the page (context.add_init_script and page.evaluate; the page's CSP refuses injected
# <script> and <style> tags, not these): every text node has this machine's paths shown short and each
# private match replaced by █, now and whenever the page changes; a short label that held one is hidden.
BLANKER = """(cfg) => {
  if (window.__alDemo) return window.__alDemo.blanked;
  const patterns = cfg.patterns.map((source) => new RegExp(source, 'gi'));
  const state = { blanked: {} };
  const fix = (node) => {
    const before = node.nodeValue;
    if (!before) return;
    const parent = node.parentNode && node.parentNode.nodeName;
    if (parent === 'SCRIPT' || parent === 'STYLE') return;
    let text = before;
    for (const [real, shown] of cfg.paths) if (text.includes(real)) text = text.split(real).join(shown);
    for (const re of patterns) {
      text = text.replace(re, (m) => { state.blanked[m] = (state.blanked[m] || 0) + 1; return '█'.repeat(m.length); });
    }
    if (text !== before) {
      node.nodeValue = text;
      // a short label that held a match (the dashboard's "tokens" headings) is hidden, so no █ is in view
      if (before.trim().length <= 24 && text.includes('█') && node.parentNode && node.parentNode.style) node.parentNode.style.visibility = 'hidden';
    }
  };
  const walk = (root) => {
    if (!root) return;
    if (root.nodeType === 3) { fix(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) fix(node);
  };
  walk(document);
  new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'characterData') fix(record.target);
      for (const node of record.addedNodes) walk(node);
    }
  }).observe(document, { subtree: true, childList: true, characterData: true });
  window.__alDemo = state;
  return state.blanked;
}"""


# The page zoom of every browser scene, set through the CSSOM (not an injected style tag), now and on load.
ZOOM = 1.25
ZOOMER = ("(z) => { const set = () => { if (document.documentElement) document.documentElement.style.zoom = String(z); }; "
          "set(); document.addEventListener('DOMContentLoaded', set); }")

# An outline on the element the voice talks about: window.__alSpotSet(selector, text, last) picks the first
# (or the last) element of `selector` whose text holds `text`, outlines it and keeps doing so every 300 ms,
# so a live re-render of the page does not lose it.
SPOTTER = """() => {
  if (window.__alSpotSet) return;
  let args = null;
  const clear = (e) => {
    e.style.outline = ''; e.style.outlineOffset = ''; e.style.borderRadius = '';
    e.removeAttribute('data-al-spot');
  };
  const apply = () => {
    if (!args) return false;
    const [selector, text, last] = args;
    let found = [...document.querySelectorAll(selector)];
    if (text) found = found.filter((e) => (e.textContent || '').includes(text));
    const el = found.length ? (last ? found[found.length - 1] : found[0]) : null;
    for (const old of document.querySelectorAll('[data-al-spot]')) if (old !== el) clear(old);
    if (el && !el.hasAttribute('data-al-spot')) {
      el.style.outline = '3px solid #58a6ff'; el.style.outlineOffset = '4px'; el.style.borderRadius = '6px';
      el.setAttribute('data-al-spot', '1');
    }
    return !!el;
  };
  window.__alSpotSet = (selector, text, last) => { args = [selector, text || null, !!last]; return apply(); };
  setInterval(apply, 300);
}"""


def _blanker_config(ctx: Ctx, paths: list[tuple[str, str]]) -> dict:
    patterns = [rules.HOME_PATH.pattern] + [p.pattern for kind, p in rules.PATTERNS.items() if kind != "home"]
    patterns += [re.escape(value) for key in ("hosts", "repos") for value in ctx.values.get(key, [])]
    return {"patterns": patterns, "paths": [list(pair) for pair in paths]}


class Browser:
    """One recorded browser part: browser_clips.Recorder with the blanker installed. `cue(i)` waits until
    caption i is due in the script (so the page is paced by narration.md), checks the page's text and
    notes the time; `finish()` stays on the page until the part's time is over."""

    def __init__(self, ctx: Ctx, base: str, first: int, until: int, work: Path | None = None,
                 ignore: tuple[str, ...] = ("favicon",)) -> None:
        import browser_clips
        self.ctx, self.first, self.until = ctx, first, until
        work = work or ctx.home_work
        home = Path(os.environ["HOME"])
        self.paths = [(str(work.resolve()), "~/work"), (str(work), "~/work"), (str(home.resolve()), "~"), (str(home), "~")]
        self.paths += repository_paths()
        self.recorder = browser_clips.Recorder(base, video=not ctx.rehearse, ignore=ignore)
        self.part = Part("video", first)
        self.started: float | None = None
        self.shots = 0
        self.spotted: tuple | None = None

    def __enter__(self) -> "Browser":
        self.recorder.__enter__()
        self.config = json.dumps(_blanker_config(self.ctx, self.paths))
        self.recorder.context.add_init_script(f"({BLANKER})({self.config})")
        self.recorder.context.add_init_script(f"({ZOOMER})({ZOOM})")
        self.recorder.context.add_init_script(f"({SPOTTER})()")
        self.page = self.recorder.page
        return self

    def spot(self, selector: str, text: str | None = None, last: bool = False) -> bool:
        """Outline the element the voice talks about; False when the page has no such element (yet)."""
        self.spotted = (selector, text, last)
        self.page.evaluate(f"({SPOTTER})()")
        return bool(self.page.evaluate("([s, t, l]) => window.__alSpotSet(s, t, l)", [selector, text, last]))

    def _blanks(self) -> None:
        found = self.page.evaluate(f"({BLANKER})({self.config})")
        self.ctx.note_blanks(f"the dashboard page in {self.ctx.scene.id}", dict(found or {}))

    def _wait_until(self, offset: float) -> None:
        if self.started is None:
            return
        due = self.started + (offset - self.ctx.offset(self.first))
        left = due - time.monotonic()
        if left > 0:
            self.page.wait_for_timeout(int(left * 1000))

    def ready(self, caption: int, lead: float = 0.8) -> None:
        """Wait until `lead` seconds before caption `caption` is due: the moment to change what the page shows."""
        self._wait_until(self.ctx.offset(caption) - lead)

    def cue(self, caption: int) -> None:
        self._wait_until(self.ctx.offset(caption))
        self._blanks()
        text = self.page.inner_text("body")
        self.ctx.check(text, f"the page at caption {caption}")
        if self.spotted and not self.page.evaluate("() => !!document.querySelector('[data-al-spot]')"):
            self.ctx.say(f"cue {caption}: no element to outline for {self.spotted}")
        self.recorder.cue(str(caption))
        if self.started is None:
            self.started = time.monotonic()
        late = time.monotonic() - self.started - (self.ctx.offset(caption) - self.ctx.offset(self.first))
        self.ctx.say(f"cue {caption} at +{self.ctx.offset(caption) + late:.1f} s (script +{self.ctx.offset(caption):g}): "
                     f"{self.ctx.scene.captions[caption].text}")
        self.look(f"cue-{caption}")

    def look(self, name: str) -> None:
        """A still of the page as it is now, for the person who rehearses."""
        folder = self.ctx.work / "check"
        folder.mkdir(parents=True, exist_ok=True)
        self.shots += 1
        self.page.screenshot(path=str(folder / f"{self.ctx.scene.id}-page-{name}.png"))
        if self.ctx.rehearse:
            self.page.screenshot(path=str(folder / f"{self.ctx.scene.id}-full-{name}.png"), full_page=True)

    def pause(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))

    def finish(self) -> None:
        self._blanks()
        self.ctx.check(self.page.inner_text("body"), "the page at the end of the part")
        self._wait_until(self.ctx.offset(self.until) - 0.3)

    def __exit__(self, kind, value, tb) -> None:
        self.part.wall = time.monotonic() - self.recorder.t0
        self.recorder.__exit__(kind, value, tb)
        if kind is None:
            video, cues = self.recorder.result
            self.part.video = video
            self.part.cues = [(at, int(text)) for at, text in cues]


def _serve(layer: Path):
    import browser_clips
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN"):
        os.environ.pop(name, None)
    return browser_clips._serve(["--layer", str(layer)])


def _node(step: str) -> str:
    return f".g-node[data-step='{step}']"


# scrollIntoView, not offsets computed from getBoundingClientRect: the page is zoomed (ZOOM)
def _to_top(page, selector: str, above: int = 12) -> None:
    """Scroll so that the element starts just under the top of the window."""
    page.wait_for_selector(selector)
    page.evaluate("([s, above]) => { document.querySelector(s).scrollIntoView({block: 'start'}); window.scrollBy(0, -above); }",
                  [selector, above])


def _to_text(page, text: str, above: int = 16) -> None:
    """Scroll so that the first element with exactly this text is about `above` pixels under the top of the window."""
    target = page.get_by_text(text, exact=True).first
    target.wait_for()
    target.evaluate("(e, above) => { e.scrollIntoView({block: 'start'}); window.scrollBy(0, -above); }", above)


def _open_step(b: Browser, step: str) -> None:
    b.page.click(_node(step))
    _to_top(b.page, ".step-panel")


# ------------------------------------------------------------------------------------------ part 1

def _frames_part(ctx: Ctx, shown: list[tuple[int, list[str], object]]) -> Part:
    """A frames part of cards and pictures: (caption index, the texts it shows, a function giving its
    frames) each. Every text is checked and said first; the frames are made only when not rehearsing."""
    for _cue, texts, _make in shown:
        ctx.check("\n".join(texts), "a card")
        ctx.say("card: " + " / ".join(texts)[:150])
    return Part("frames", shown[0][0], [Segment(cue, [] if ctx.rehearse else make()) for cue, _texts, make in shown])


def scene_01_problem(ctx: Ctx) -> list[Part]:
    board = ("Rota · a ward manager's shifts board", "# docs/demo/rotademo/before.png · synthetic data")
    stakes = ["Where mistakes are expensive", "an expired registration must not work", "staff need rest between shifts",
              "two organisations, data kept apart"]
    needs = ["An agent needs to know", "how do I run the tests?", "what must I never touch?", "how do I prove my change works?"]
    return [_frames_part(ctx, [(0, list(board), lambda: picture_frames(PICTURES["board"], *board)),
                               (1, stakes, lambda: card_frames(stakes)),
                               (2, needs, lambda: card_frames(needs))])]


# ------------------------------------------------------------------------------------------ part 2

def scene_02_layer(ctx: Ctx) -> list[Part]:
    import term
    step = shell(ctx, ROTA_EXCERPT, ROTA)
    rows = step.output.splitlines()
    missing = [s for keep in HIGHLIGHTS.values() for s in keep if s not in step.output]
    if len(rows) != 7 or missing:
        raise SceneStopped(f"02-layer: the excerpt of tests/rotademo/layer.rkt.in is not the 7 lines the captions talk about "
                           f"(missing {missing}):\n{step.output}")
    title = "al — the real layer of Rota (tests/rotademo/layer.rkt.in)"
    if len(title) > term.COLS:
        title = "al — the real layer of Rota"
    part = terminal_part(ctx, title, [(0, step)])
    for cue in (1, 2, 3):
        still = highlight_frame(ctx, title, [step], keep=lambda t, k=HIGHLIGHTS[cue]: any(s in t for s in k))
        part.segments.append(Segment(cue) if still is None else Segment(cue, [still]))
    generates = card_part(ctx, [(4, ["One definition generates", "prompts for each kind of job", "workflows and their checks",
                                     "the rules and the gate", "a live dashboard"], None)])
    return [part, generates]


# ------------------------------------------------------------------------------------------ part 3

def scene_03_use(ctx: Ctx) -> list[Part]:
    site = rota_site(ctx.home_work)
    if site["layer"].exists():                  # a layer left by an earlier recording: `al init` makes it again on screen
        shutil.rmtree(site["layer"])
    steps = [(0, shell(ctx, COMMANDS["init"], site["work"]))]
    install_rota_layer(site["work"])
    steps += [(1, shell(ctx, ["ls", "rota.al"], site["work"])),
              (2, shell(ctx, COMMANDS["gen"], site["work"])),
              (3, shell(ctx, COMMANDS["check"], site["work"]))]
    gen, check = steps[2][1].output, steps[3][1].output
    if "generated " not in gen or not check.strip().endswith("check: ok"):
        raise SceneStopped(f"03-use: al gen or al check did not print what the captions say:\n{gen}\n{check}")
    ctx.stand_in("03-use: agentic.rkt is the committed Rota definition (tests/rotademo/layer.rkt.in); in use an LLM writes it "
                 "with al author, or al adopt takes over an existing agent setup.")
    start = " ".join(COMMANDS["run"][:4]) + ' "Enforce 11 hours of rest …"'
    return [terminal_part(ctx, "al — from a repository to its layer", steps),
            card_part(ctx, [(4, ["Start a run", "from the command line, or the dashboard's Start panel"], [start])])]


# ------------------------------------------------------------------------------------------ part 4

def scene_04_run(ctx: Ctx) -> list[Part]:
    import browser_clips
    site = demo_site(ctx.home_work, generated=True)
    warm_rota_python(site["work"])
    layer = site["layer"]
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
            page.select_option(".start-target", "workflow:feature")
            b.pause(0.6)
            browser_clips._type(page, ".start-prompt", PROMPT_A)
            b.pause(1.0)
            page.click(".start-go")
            browser_clips.until(page, "location.hash.startsWith('#/run/rota/')", timeout=30000)
            run = page.evaluate("location.hash").rsplit("/", 1)[1]
            run_route = f"/run/rota/{run}"
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
            wait_for("the log of the test check", lambda: page.locator(".log span").count() > 0, 60, page)
            b.spot(".log")
            b.cue(4)
            wait_for("the end of the run", lambda: run_state(layer, run).get("status") not in (None, "", "running")
                     and run_state(layer, run).get("ended"), 300, page)
            state = run_state(layer, run)
            steps = [(s.get("name"), s.get("status")) for s in state.get("steps", [])]
            if state.get("status") != "passed" or sorted(steps) != sorted((name, "passed") for name in STEPS_A):
                raise SceneStopped(f"04-run: the run ended {state.get('status')} with steps {steps}; nothing is recorded")
            if new_run(layer, before) != run:
                raise SceneStopped("04-run: the page is not on the run it started")
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
            if "a human merges" not in page.inner_text("body") or "PR.md" not in page.inner_text("body"):
                raise SceneStopped("04-run: the run page does not say that a person merges")
            b.spot(".outcome-block", "Gate and ship")
            b.cue(6)
            b.ready(7, 0.4)
            _to_top(page, ".graph-wrap", above=40)
            b.spot(_node("ship") + ".st-passed")
            b.cue(7)
            b.finish()
        (ctx.work / "run-a.json").write_text(json.dumps({"work": str(site["work"]), "layer": str(layer), "run": run}))
        ctx.stand_in("04-run: the Rota app and its scripted layer (tools/demovideo/demo_layer.rkt.in); classify, plan, commit "
                     "and both reviewers are the mock agent, the build is the scripted stand-in tools/demovideo/rota_agent.py, "
                     "which really adds the 11-hour rest rule and its tests; Rota's test and isolation checks really run. "
                     "No model is called.")
        return [b.part]
    finally:
        browser_clips._stop(running)


def scene_04_rota(ctx: Ctx) -> list[Part]:
    rest = ("Rota, a real model: the 11-hour rest rule", "# docs/demo/rotademo/a-rest-rule.gif · the run's own proof video")
    merged = ("Both reviewers approved; the gate merged", "# docs/demo/rotademo/a-pr-merged.png")
    frames = int((ctx.offset(1) - ctx.offset(0)) * rules.FPS) - 2
    ctx.stand_in("04-rota: pictures of a recorded Rota run with Claude Opus 5.5 (docs/EXAMPLES.md); no run happened here.")
    return [_frames_part(ctx, [(0, list(rest), lambda: gif_frames(PICTURES["rest"], *rest, max_frames=frames)),
                               (1, list(merged), lambda: picture_frames(PICTURES["merged"], *merged, crop=(0, 0, 862, 470)))])]


# ------------------------------------------------------------------------------------------ part 5

def _run_a(ctx: Ctx) -> dict:
    """The run of scene 04-run when its work folder is still there; else one made here, hidden, the same way."""
    record = ctx.work / "run-a.json"
    if record.is_file():
        found = json.loads(record.read_text())
        if run_state(Path(found["layer"]), found["run"]).get("status") == "passed":
            return found
    site = demo_site(ctx.home_work, generated=True)
    warm_rota_python(site["work"])
    before = runs_of(site["layer"])
    hidden([AL, "run", site["layer"], "/feature", PROMPT_A], site["work"])
    return {"work": str(site["work"]), "layer": str(site["layer"]), "run": new_run(site["layer"], before)}


def scene_05_proof(ctx: Ctx) -> list[Part]:
    import browser_clips
    found = _run_a(ctx)
    running = _serve(Path(found["layer"]))
    try:
        with Browser(ctx, running.base, 0, len(ctx.scene.captions), work=Path(found["work"])) as b:
            page = b.page
            browser_clips.open_(page, f"/run/rota/{found['run']}")
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
        ctx.stand_in("05-proof: the Rota run of 04-run (the scripted Rota layer: mock agent and the stand-in rota_agent.py).")
        return [b.part]
    finally:
        browser_clips._stop(running)


def _waits_for_approval(layer: Path, run: str) -> bool:
    try:
        return json.loads((layer / "agents" / run / "approvable.json").read_text()).get("waiting") is True
    except (OSError, ValueError, AttributeError):
        return False


def scene_05_protected(ctx: Ctx) -> list[Part]:
    import browser_clips
    site = demo_site(ctx.home_work, generated=True)
    layer = site["layer"]
    warm_rota_python(site["work"])
    before = runs_of(layer)
    hidden([AL, "run", layer, "/tidy", PROMPT_B], site["work"], expect=1)
    run = new_run(layer, before)
    if "evidence:needs-person" not in json.dumps(run_state(layer, run)):
        raise SceneStopped(f"05-protected: run {run} did not stop at evidence:needs-person")
    route, row = f"/run/rota/{run}", f"tbody tr[data-key='rota/{run}']"
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
        wait_for(f"run {run} to pass after the approval", lambda: run_state(layer, run).get("status") == "passed", 120)
        approvals = run_state(layer, run).get("approvals") or []
        if not any(a.get("who") == WHO and a.get("via") == "dashboard" for a in approvals):
            raise SceneStopped(f"05-protected: run {run} records no approval by {WHO} from the dashboard: {approvals}")
        with Browser(ctx, running.base, 3, len(ctx.scene.captions)) as last:
            page = last.page
            browser_clips.open_(page, route)
            page.wait_for_selector(_node("ship") + ".st-passed", timeout=20000)
            last.spot(_node("ship"))
            last.cue(3)
            last.pause(3.0)
            _to_text(page, "Proof plan", above=70)
            last.finish()
        ctx.stand_in(f"05-protected: a second run of the scripted Rota layer; the mock agent's build edits CONTRIBUTING.md, "
                     f"a protected path of the Rota layer. "
                     f"The approval is given in the dashboard (Approve & resume) by the fictional {WHO}.")
        return [first.part, last.part]
    finally:
        browser_clips._stop(running)


def scene_05_handoff(ctx: Ctx) -> list[Part]:
    top = ("Rota: a schema change, handed to a person", "# docs/demo/rotademo/b-pr-why-human-top.png · a real run")
    ask = ("What the agent asks a person to add", "# docs/demo/rotademo/b-pr-why-human-top.png")
    ctx.stand_in("05-handoff: pictures of a recorded Rota run with Claude Opus 5.5 (docs/EXAMPLES.md); no run happened here.")
    return [_frames_part(ctx, [(0, list(top), lambda: picture_frames(PICTURES["handoff"], *top, crop=(0, 0, 872, 700))),
                               (1, list(ask), lambda: picture_frames(PICTURES["handoff"], *ask, crop=(0, 700, 872, 1047)))])]


def scene_05_review_no(ctx: Ctx) -> list[Part]:
    import browser_clips
    layer = demo_site(ctx.home_work, generated=True)["layer"]
    rid = add_refused_rota_run(layer)
    running = _serve(layer)
    try:
        with Browser(ctx, running.base, 0, len(ctx.scene.captions)) as b:
            page = b.page
            browser_clips.open_(page, f"/run/rota/{rid}")
            page.wait_for_selector(_node("review"))
            b.cue(0)
            b.pause(1.5)
            page.click(_node("review"))
            page.wait_for_selector(".step-panel .step-rounds")
            page.locator(".step-panel .step-rounds").scroll_into_view_if_needed()
            if page.locator(".step-panel .step-rounds").inner_text().count("not approved") < 3:
                raise SceneStopped("05-review-no: the review step does not show three refused rounds")
            b.spot(".step-panel .step-rounds")
            b.ready(1)
            page.evaluate("window.scrollTo(0, 0)")
            if page.locator(".badge.handed-over").count() == 0:
                raise SceneStopped("05-review-no: the run page does not say the run was handed to a person")
            b.spot(".badge.handed-over")
            b.cue(1)
            b.finish()
        ctx.stand_in("05-review-no: a fixture run written by scenes.add_refused_rota_run into the Rota layer: the planted change "
                     "of tests/rotademo/planted (scenario C) refused in three rounds; no review ran here.")
        return [b.part]
    finally:
        browser_clips._stop(running)


# ------------------------------------------------------------------------------------------ part 6

def scene_06_fit(ctx: Ctx) -> list[Part]:
    examples = [f"{name:<10}{language}" for name, language in example_rows()]
    readme = next((line for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines() if "Not yet shown" in line), None)
    if readme is None:
        raise SceneStopped("06-fit: README.md has no line holding \"Not yet shown\"; the caption that quotes it must go")
    readme = readme.strip().lstrip(">").strip().rstrip("\\").strip().replace("*", "")[:96]
    return [card_part(ctx, [
        (0, ["The examples", "five public codebases, five languages"], ["# docs/EXAMPLES.md"] + examples),
        (1, ["Where it fits", "a repository with real tests and commands", "work that must be proven, not claimed",
             "a person who decides what ships"], None),
        (2, ["Not yet shown", "from the README's own status"], ["# README.md", readme]),
    ])]


# ------------------------------------------------------------------------------------------ part 7

def scene_07_close(ctx: Ctx) -> list[Part]:
    return [card_part(ctx, [(0, [REPOSITORY, "the code, the guide, the examples, the tests"], None)])]


SCENES = {
    "01-problem": scene_01_problem,
    "02-layer": scene_02_layer,
    "03-use": scene_03_use,
    "04-run": scene_04_run,
    "04-rota": scene_04_rota,
    "05-proof": scene_05_proof,
    "05-protected": scene_05_protected,
    "05-handoff": scene_05_handoff,
    "05-review-no": scene_05_review_no,
    "06-fit": scene_06_fit,
    "07-close": scene_07_close,
}
