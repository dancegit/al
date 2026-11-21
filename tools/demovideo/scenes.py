"""The scenes of the five-minute demo video: one recorder function per scene id of narration.md.

Nothing here draws a frame of its own design except the title cards: terminal scenes are real commands
run with tools/showcase/terminal_clips.run_shown and rendered by tools/showcase/term.py, browser scenes
are the dashboard in the host's Chromium, recorded by tools/showcase/browser_clips.Recorder. A scene
function takes a `Ctx` and returns its parts in order; make.py fits each part to its length, lays the
captions of narration.md over it and writes the clip.

  demo_site    the demo application `cart` and its layer, written beside each other in a work folder
  COMMANDS     every `al …` command a terminal scene shows ({run} and {port} are filled at run time)
  SCENES       scene id → recorder function

Captured text is changed in two ways only, and both are listed in the clip's JSON file: paths of this
machine are shown short (`~/work`, `.`, `agentic`), and a private match is blanked with █ (rules.blank).
This module imports only the standard library at the top: Pillow, Playwright and the showcase's
renderers are imported by the functions that draw or record.
"""
from __future__ import annotations

import json
import os
import re
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

import rules  # noqa: E402

AL = ROOT / "tools" / "al"
REPLAY = ROOT / "kit" / "dashboard" / "demo" / "replay_agent.py"
LAYER_SOURCE = HERE / "demo_layer.rkt.in"
PROMPT_A = "Add a percentage discount to the cart"
PROMPT_B = "Note in the lint tool what it also checks"
APPROVAL = "edit:tools/lint.py"
STEPS_A = ("classify", "plan", "prove", "build", "lint", "test", "commit", "evidence", "review", "ship")
REPOSITORY = "github.com/dancegit/al"

# every `al …` command a terminal scene shows, as typed (tests/tools/test_demo_video.py runs each with --help)
COMMANDS: dict[str, tuple[str, ...]] = {
    "gen": ("al", "gen", "cart.al"),
    "check": ("al", "check", "cart.al"),
    "dashboard": ("al", "dashboard", "cart.al", "--port", "{port}"),
    "approve": ("al", "approve", "cart.al", "{run}", APPROVAL, "--who", "Ada Example"),
}
SAMPLES = {"run": "0123abcd", "port": "18765"}

PLAN = """# Feature: a percentage discount

Add `Cart.discounted(percent)`: the total after a percentage discount, in cents.

## Steps

1. add `discounted()` to `cart.py`
2. add a test for ten percent off

## How it is proven

### Proof plan
| # | Criterion | Proven by | Writes | Needs |
|---|---|---|---|---|
| C1 | every test of the cart passes, the new one included | `test` | - | none |
| C2 | every file compiles and no line is too long | `lint` | - | none |
"""


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
    own_plan: Path | None = None
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

def excerpts(text: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """The two line ranges of the layer definition that scene 02-layer shows: the codebase with its
    checks, the protected path and the review rule; then the workflow `change`."""
    lines = text.splitlines()
    review = next(i for i, line in enumerate(lines, 1) if line.strip().startswith("(review agent"))
    start = next(i for i, line in enumerate(lines, 1) if line.strip().startswith("(adw change"))
    end = next(i for i, line in enumerate(lines, 1) if i > start and "(ship)" in line)
    return (1, review), (start, end)


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
    """The `cart` application (the file texts of kit/dashboard/demo/live_demo.py), committed once by a
    neutral identity, and beside it the layer `cart.al` with its definition, LLM.txt and the mock
    agent's scripts. With generated=True `al gen` has run too."""
    import fixture_imports as fx
    live = fx.load(ROOT / "kit" / "dashboard" / "demo" / "live_demo.py", "demovideo_live_demo")
    app, layer = work / "cart", work / "cart.al"
    if not (layer / "agentic.rkt").is_file():
        for path, text in {"cart.py": live.CART, "tests/__init__.py": "", "tests/test_cart.py": live.TEST_CART,
                           "tools/run_tests.py": live.RUN_TESTS, "tools/lint.py": live.LINT,
                           "README.md": "# cart\n\nA demo library.\n", ".gitignore": "__pycache__/\n*.pyc\n"}.items():
            (app / path).parent.mkdir(parents=True, exist_ok=True)
            (app / path).write_text(text)
        _git(app, "init", "-q", "-b", "main")
        _git(app, "add", "-A")
        _git(app, "-c", "user.name=Showcase", "-c", "user.email=showcase@example.org", "commit", "-qm", "cart")
        mock = layer / "mock"
        mock.mkdir(parents=True)
        (layer / "agentic.rkt").write_text(LAYER_SOURCE.read_text(encoding="utf-8").replace("@REPLAY@", str(REPLAY)))
        (layer / "LLM.txt").write_text(live.LLM_TXT)
        (mock / "classify.json").write_text(json.dumps({"output": json.dumps({"class": "feature", "reason": "new behaviour"})}))
        (mock / "plan.json").write_text(json.dumps({"layer_files": {"specs/feature-discount.md": PLAN},
                                                    "output": "specs/feature-discount.md"}))
        (mock / "commit.json").write_text(json.dumps({"commit": True, "output": "Committed the change with a one-line message."}))
        (mock / "build.json").write_text(json.dumps({"files": {"tools/lint.py": live.LINT + "\n# also checked: trailing blank lines\n"},
                                                     "output": "Added a note to the lint tool."}))
    if generated and not (layer / "agentic-layer.json").is_file():
        hidden([AL, "gen", layer], work)
    return {"work": work, "app": app, "layer": layer}


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


# ------------------------------------------------------------------------------------------ cards

def card_image(lines: list[str], small: list[str] | None = None):
    """A title card in the showcase's colours: a bold first line with the blue rule under it, then
    centred lines; `small` lines (real text of a file, with its source) are set in the terminal's font."""
    from PIL import Image, ImageDraw
    import term
    img = Image.new("RGB", (term.W, term.H), term.BG)
    d = ImageDraw.Draw(img)
    head, body = term.font(term.CARD_SIZE + 6, True), term.font(term.CARD_SIZE - 6)
    mono = term.font(term.FONT_SIZE - 2)
    for face, text in [(head, lines[0])] + [(body, line) for line in lines[1:]] + [(mono, line) for line in small or []]:
        if face.getlength(text) > term.CARD_WIDTH:
            raise ValueError(f"card line does not fit {term.CARD_WIDTH} px: {text!r}")
    height = 70 + 44 * len(lines[1:]) + (30 + 28 * len(small) if small else 0)
    y = (term.H - term.STRIP_H - height) // 2 + 20
    d.text((term.W // 2, y), lines[0], font=head, fill="#ffffff", anchor="mm")
    d.line([term.W // 2 - 60, y + 36, term.W // 2 + 60, y + 36], fill=term.BLUE, width=3)
    y += 86
    for line in lines[1:]:
        d.text((term.W // 2, y), line, font=body, fill=term.FG, anchor="mm")
        y += 44
    if small:
        y += 20
        left = (term.W - max(mono.getlength(line) for line in small)) // 2
        for line in small:
            d.text((left, y), line, font=mono, fill=term.DIM if line.startswith("#") else term.FG, anchor="lm")
            y += 28
    return img


def card_part(ctx: Ctx, cards: list[tuple[int, list[str], list[str] | None]]) -> Part:
    """A part of title cards: (caption index, lines, small lines) each; a card stays until the next."""
    for _cue, lines, small in cards:
        ctx.check("\n".join(lines + (small or [])), "a card")
        ctx.say("card: " + " / ".join(lines + (small or []))[:150])
    part = Part("frames", cards[0][0])
    for cue, lines, small in cards:
        part.segments.append(Segment(cue, [] if ctx.rehearse else [card_image(lines, small)]))
    return part


# ------------------------------------------------------------------------------------------ the dashboard in a browser

# Installed in the page (context.add_init_script and page.evaluate; the page's CSP refuses injected
# <script> and <style> tags, not these): every text node has this machine's paths shown short and each
# private match replaced by █, now and whenever the page changes.
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
    if (text !== before) node.nodeValue = text;
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

    def __enter__(self) -> "Browser":
        self.recorder.__enter__()
        self.config = json.dumps(_blanker_config(self.ctx, self.paths))
        self.recorder.context.add_init_script(f"({BLANKER})({self.config})")
        self.page = self.recorder.page
        return self

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


def _to_top(page, selector: str) -> None:
    """Scroll so that the element starts just under the top of the window."""
    page.wait_for_selector(selector)
    page.evaluate("(s) => { const e = document.querySelector(s); window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - 12); }",
                  selector)


def _to_text(page, text: str, above: int = 16) -> None:
    """Scroll so that the first element with exactly this text is `above` pixels under the top of the window."""
    target = page.get_by_text(text, exact=True).first
    target.wait_for()
    target.evaluate("(e, above) => window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - above)", above)


def _open_step(b: Browser, step: str) -> None:
    b.page.click(_node(step))
    _to_top(b.page, ".step-panel")


# ------------------------------------------------------------------------------------------ part 1

def scene_01_what(ctx: Ctx) -> list[Part]:
    return [card_part(ctx, [
        (0, ["al", "an agentic layer for any codebase"], None),
        (1, ["agentic.rkt", "one definition of how agents work on this codebase:",
             "its commands, its checks, protected paths, workflows, review"], None),
        (2, ["al gen", "the compiler checks the definition,",
             "the generator writes prompts, workflows, rules and a dashboard"], None),
        (3, ["definition  →  layer  →  runs  →  proof", "what the next five minutes show, in this order"], None),
    ])]


# ------------------------------------------------------------------------------------------ part 2

def scene_02_layer(ctx: Ctx) -> list[Part]:
    site = demo_site(ctx.home_work)
    (a, b), (c, d) = excerpts((site["layer"] / "agentic.rkt").read_text())
    steps = [(0, shell(ctx, ["sed", "-n", f"{a},{b}p", "cart.al/agentic.rkt"], site["work"])),
             (2, shell(ctx, ["sed", "-n", f"{c},{d}p", "cart.al/agentic.rkt"], site["work"])),
             (3, shell(ctx, COMMANDS["gen"], site["work"])),
             (4, shell(ctx, COMMANDS["check"], site["work"]))]
    if "generated" not in steps[2][1].output or "check: ok" not in steps[3][1].output:
        raise SceneStopped(f"02-layer: al gen or al check did not print what the captions say:\n"
                           f"{steps[2][1].output}\n{steps[3][1].output}")
    ctx.stand_in("The demo application `cart` and its layer are written for the demo (tools/demovideo/demo_layer.rkt.in).")
    return [terminal_part(ctx, "al — a layer for the cart library", steps)]


# ------------------------------------------------------------------------------------------ part 3

def scene_03_start(ctx: Ctx) -> list[Part]:
    import terminal_clips
    site = demo_site(ctx.home_work, generated=True)
    argv = fill(COMMANDS["dashboard"], {"port": str(terminal_clips.free_port())})
    step = shell(ctx, argv, site["work"], interrupt_after=5, expect=130, rows=5)
    if "al dashboard on" not in step.output or "watching: cart" not in step.output:
        raise SceneStopped(f"03-start: the dashboard did not print its first lines:\n{step.output}")
    ctx.stand_in("03-start: the dashboard of the browser scenes is the same server started inside the recorder, on another port.")
    return [terminal_part(ctx, "al — the dashboard", [(0, step)])]


def scene_03_run(ctx: Ctx) -> list[Part]:
    import browser_clips
    site = demo_site(ctx.home_work, generated=True)
    layer = site["layer"]
    running = _serve(layer)
    try:
        with Browser(ctx, running.base, 0, len(ctx.scene.captions), ignore=("favicon", "409")) as b:
            page = b.page
            browser_clips.open_(page, "/layers")
            page.wait_for_selector(".start-panel .start-prompt")
            b.cue(0)
            b.cue(1)
            before = runs_of(layer)
            page.select_option(".start-target", "workflow:change")
            b.pause(0.6)
            browser_clips._type(page, ".start-prompt", PROMPT_A)
            b.pause(1.0)
            page.click(".start-go")
            browser_clips.until(page, "location.hash.startsWith('#/run/cart/')", timeout=30000)
            run = page.evaluate("location.hash").rsplit("/", 1)[1]
            run_route = f"/run/cart/{run}"
            page.wait_for_selector(_node("build"), timeout=60000)
            b.cue(2)
            wait_for("the build step", lambda: step_status(layer, run, "build") != "", 120, page)
            b.ready(3, 0.6)
            _open_step(b, "build")
            wait_for("the first tool call of the build", lambda: page.locator(".ev.k-tool").count() > 0
                     or step_status(layer, run, "build") == "passed", 120, page)
            b.cue(3)
            wait_for("the end of the build", lambda: step_status(layer, run, "build") not in ("", "running"), 300, page)
            b.pause(1.0)
            browser_clips.open_(page, run_route)
            page.wait_for_selector(_node("test"))
            wait_for("the test check", lambda: step_status(layer, run, "test") != "", 120, page)
            _open_step(b, "test")
            wait_for("the log of the test check", lambda: page.locator(".log span").count() > 0, 60, page)
            b.cue(4)
            wait_for("the end of the run", lambda: run_state(layer, run).get("status") not in (None, "", "running")
                     and run_state(layer, run).get("ended"), 300, page)
            state = run_state(layer, run)
            steps = [(s.get("name"), s.get("status")) for s in state.get("steps", [])]
            if state.get("status") != "passed" or sorted(steps) != sorted((name, "passed") for name in STEPS_A):
                raise SceneStopped(f"03-run: the run ended {state.get('status')} with steps {steps}; nothing is recorded")
            if new_run(layer, before) != run:
                raise SceneStopped("03-run: the page is not on the run it started")
            b.ready(5)
            browser_clips.open_(page, run_route)
            page.wait_for_selector("[data-role='proof-plan']", timeout=20000)
            _to_text(page, "What the checks proved")
            b.cue(5)
            b.ready(6)
            browser_clips.open_(page, run_route)
            page.wait_for_selector(_node("review"))
            page.click(_node("review"))
            page.wait_for_selector(".step-panel .step-rounds")
            page.locator(".step-panel .step-rounds").scroll_into_view_if_needed()
            b.cue(6)
            b.ready(7)
            browser_clips.open_(page, run_route)
            page.wait_for_selector(_node("ship") + ".st-passed")
            _to_text(page, "Outcome")
            if "a human merges" not in page.inner_text("body") or "PR.md" not in page.inner_text("body"):
                raise SceneStopped("03-run: the run page does not say that a person merges")
            b.cue(7)
            b.ready(8, 0.4)
            page.evaluate("window.scrollTo(0, 0)")
            b.cue(8)
            b.pause(6)
            _to_text(page, "Timeline")
            b.finish()
        (ctx.work / "run-a.json").write_text(json.dumps({"work": str(site["work"]), "layer": str(layer), "run": run}))
        ctx.stand_in("03-run: classify, plan, commit and both reviewers are the mock agent; the build is the scripted "
                     "stand-in kit/dashboard/demo/replay_agent.py, which really edits the worktree and runs the tests. No model is called.")
        return [b.part]
    finally:
        browser_clips._stop(running)


# ------------------------------------------------------------------------------------------ part 4

def _run_a(ctx: Ctx) -> dict:
    """The run of scene 03-run when its work folder is still there; else one made here, hidden, the same way."""
    record = ctx.work / "run-a.json"
    if record.is_file():
        found = json.loads(record.read_text())
        if run_state(Path(found["layer"]), found["run"]).get("status") == "passed":
            return found
    site = demo_site(ctx.home_work, generated=True)
    before = runs_of(site["layer"])
    hidden([AL, "run", site["layer"], "/change", PROMPT_A], site["work"])
    return {"work": str(site["work"]), "layer": str(site["layer"]), "run": new_run(site["layer"], before)}


def scene_04_proof(ctx: Ctx) -> list[Part]:
    import browser_clips
    found = _run_a(ctx)
    running = _serve(Path(found["layer"]))
    try:
        with Browser(ctx, running.base, 0, len(ctx.scene.captions), work=Path(found["work"])) as b:
            page = b.page
            browser_clips.open_(page, f"/run/cart/{found['run']}")
            page.wait_for_selector("[data-role='proof-plan']")
            card = page.locator("[data-role='proof-plan']")
            _to_text(page, "Proof plan", above=90)
            rows = card.locator("tr[data-criterion]")
            states = [rows.nth(i).get_attribute("data-state") for i in range(rows.count())]
            if states != ["proven", "proven"]:
                raise SceneStopped(f"04-proof: the proof table shows {states}, not two proven claims")
            b.cue(0)
            b.ready(1, 0.3)
            rows.nth(0).hover()
            b.cue(1)
            rows.nth(1).hover()
            b.finish()
        ctx.stand_in("04-proof: the run of 03-run (mock agent and scripted stand-in).")
        return [b.part]
    finally:
        browser_clips._stop(running)


def scene_04_own_plan(ctx: Ctx) -> list[Part]:
    if ctx.own_plan is None or not ctx.own_plan.is_file():
        raise SceneStopped("04-own-plan shows the Proof plan of the run that records the video: pass --own-plan PATH "
                           "(the plan's file in the layer's specs folder)")
    plan = ctx.own_plan.resolve()
    if plan.parent.name != "specs":
        raise SceneStopped(f"04-own-plan: {plan.name} is not in a layer's specs folder")
    layer = plan.parent.parent
    where = f"{layer.name}/specs"
    steps = [(0, shell(ctx, f"cd {where}", layer.parent)),
             (None, shell(ctx, f"grep '^| C' {plan.name} | cut -d'|' -f2,3 | cut -c1-58", plan.parent))]
    if len(steps[1][1].output.splitlines()) < 2:
        raise SceneStopped(f"04-own-plan: {plan.name} has no Proof plan rows")
    return [terminal_part(ctx, "al — the plan of the run that recorded this video", steps)]


def scene_04_protected(ctx: Ctx) -> list[Part]:
    import browser_clips
    site = demo_site(ctx.home_work, generated=True)
    layer = site["layer"]
    before = runs_of(layer)
    hidden([AL, "run", layer, "/tidy", PROMPT_B], site["work"], expect=1)
    run = new_run(layer, before)
    if "evidence:needs-person" not in json.dumps(run_state(layer, run)):
        raise SceneStopped(f"04-protected: run {run} did not stop at evidence:needs-person")
    route = f"/run/cart/{run}"
    running = _serve(layer)
    try:
        with Browser(ctx, running.base, 0, 2) as first:
            page = first.page
            browser_clips.open_(page, route)
            page.wait_for_selector("[data-role='proof-plan'] .proof-stop")
            _to_text(page, "Proof plan", above=70)
            if APPROVAL not in page.locator("[data-role='proof-plan'] .proof-stop").inner_text():
                raise SceneStopped("04-protected: the run page does not ask for " + APPROVAL)
            first.cue(0)
            first.ready(1)
            browser_clips.open_(page, "/runs")
            page.wait_for_selector(f"tbody tr[data-key='cart/{run}']")
            first.cue(1)
            first.finish()
        approve = shell(ctx, fill(COMMANDS["approve"], {"run": run}), site["work"])
        if "resuming from step evidence" not in approve.output:
            raise SceneStopped(f"04-protected: al approve printed:\n{approve.output}")
        terminal = terminal_part(ctx, "al — a person approves one edit", [(2, approve)])
        wait_for(f"run {run} to pass after the approval", lambda: run_state(layer, run).get("status") == "passed", 120)
        with Browser(ctx, running.base, 3, len(ctx.scene.captions)) as last:
            page = last.page
            browser_clips.open_(page, route)
            page.wait_for_selector(_node("ship") + ".st-passed", timeout=20000)
            last.cue(3)
            last.pause(3.0)
            _to_text(page, "Proof plan", above=70)
            last.finish()
        ctx.stand_in("04-protected: a second run of the demo layer; the mock agent's build edits tools/lint.py, a protected path.")
        return [first.part, terminal, last.part]
    finally:
        browser_clips._stop(running)


def scene_04_review_no(ctx: Ctx) -> list[Part]:
    import browser_clips
    import fixture_imports as fx
    fx.dash_fixtures().build(ctx.home_work)
    layer = ctx.home_work / "demo-shop.al"
    fx.review_model().add_review_runs(layer)
    running = _serve(layer)
    try:
        with Browser(ctx, running.base, 0, len(ctx.scene.captions)) as b:
            page = b.page
            browser_clips.open_(page, "/run/demo-shop/a1000101")
            page.wait_for_selector(_node("review"))
            b.cue(0)
            b.pause(1.5)
            page.click(_node("review"))
            page.wait_for_selector(".step-panel .step-rounds")
            page.locator(".step-panel .step-rounds").scroll_into_view_if_needed()
            if page.locator(".step-panel .step-rounds").inner_text().count("not approved") < 3:
                raise SceneStopped("04-review-no: the review step does not show three refused rounds")
            b.ready(1)
            browser_clips.open_(page, "/runs")
            page.wait_for_selector("tbody tr[data-key='demo-shop/a1000101']")
            browser_clips.until(page, "[...document.querySelectorAll('.stat')].some((s) => s.querySelector('.label')?.textContent.trim() === "
                                      "'handed over' && s.querySelector('.value')?.textContent.trim() === '2')")
            b.cue(1)
            b.finish()
        ctx.stand_in("04-review-no: the fixture run a1000101 of kit/dashboard/tests (fixtures.py, test_review_rejected_model.py), "
                     "as in docs/showcase/04-review-says-no; no review ran here.")
        return [b.part]
    finally:
        browser_clips._stop(running)


# ------------------------------------------------------------------------------------------ part 5

def _first_that_prints(ctx: Ctx, commands: list, rows: int = 6):
    """The step of the first command that prints a line without a private value; None when none does."""
    import privacy
    import terminal_clips
    for command in commands:
        try:
            step = shell(ctx, command, ROOT, rows=rows)
        except (terminal_clips.UnexpectedExit, privacy.PrivateText, SceneStopped) as error:
            ctx.say(f"not shown ({type(error).__name__}): {command if isinstance(command, str) else ' '.join(command)}")
            continue
        if step.output.strip():
            return step
    return None


def scene_05_limits(ctx: Ctx) -> list[Part]:
    cases = [
        (0, [['grep', '-n', '-m3', 'skipped-only', 'kit/runtime/adw_modules/evidence.py'],
             'grep -n -m2 "skipped-only" docs/GUIDE.md docs/ARTEFACTS.md | cut -c1-100']),
        (1, ['grep -m1 "5 of 10 runs" agentic/LLM.txt | fold -s -w 100 | head -4']),
        (None, ['grep -n -m1 "never weaken" agentic/agentic.rkt | cut -c1-100']),
        (2, [["git", "log", "--oneline", "-1", "221390e"], 'git log --oneline -i --grep="of CI" -1']),
        (3, [["git", "log", "--oneline", "-1", "8c6ff53"]]),
        # `al doctor agentic` prints these lines for this machine, with a temporary folder named after
        # its user: the guide's copy of them is shown instead
        (None, ['grep -h -m3 "^disk:" docs/GUIDE.md | cut -c1-100']),
    ]
    steps, missing = [], []
    for cue, commands in cases:
        step = _first_that_prints(ctx, commands)
        if step is None:
            missing.append(commands[0] if isinstance(commands[0], str) else " ".join(commands[0]))
            continue
        steps.append((cue, step))
    shown = [cue for cue, _step in steps if cue is not None]
    if shown != [0, 1, 2, 3]:
        raise SceneStopped(f"05-limits: no record in this checkout for {missing}; name the case on the 05-not-yet card "
                           "as \"not shown: no record in this checkout\" and take its caption out of narration.md")
    return [terminal_part(ctx, "al — limits, from this repository's own files", steps)]


def scene_05_not_yet(ctx: Ctx) -> list[Part]:
    def lines_of(command: str) -> list[str]:
        step = shell(ctx, command, ROOT, rows=4)
        found = [line[:96] for line in step.output.splitlines() if line.strip()]
        if not found:
            raise SceneStopped(f"05-not-yet: `{command}` printed nothing; the caption that quotes it must go")
        return found
    kpis = lines_of("sed -n '/^Generated by/p;/^| passed/p;/^| handed over/p' docs/KPIS.md | cut -c1-96")
    readme = lines_of("grep -m1 'Not yet shown' README.md | cut -c1-96")
    return [card_part(ctx, [
        (0, ["Counted, not claimed", "the real runs of al's own layer"], ["# docs/KPIS.md"] + kpis),
        (1, ["Not yet shown", "from the README's own status"], ["# README.md"] + readme),
        (2, ["This demo run", "mock agent + a scripted stand-in for the build", "no model was called"], None),
    ])]


# ------------------------------------------------------------------------------------------ part 6

def scene_06_close(ctx: Ctx) -> list[Part]:
    ref = next((r for r in ("main", "origin/main") if subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", "-q", r], capture_output=True).returncode == 0), "HEAD")
    log = shell(ctx, ["git", "log", "--oneline", "--first-parent", "--merges", "-3", ref], ROOT)
    if len(log.output.splitlines()) != 3 or "pull request" not in log.output:
        raise SceneStopped(f"06-close: `git log` does not show three merged pull requests:\n{log.output}")
    ci = shell(ctx, ["sed", "-n", "1,8p", ".github/workflows/ci.yml"], ROOT)
    return [terminal_part(ctx, "al — what is on main", [(0, log), (1, ci)]),
            card_part(ctx, [(2, [REPOSITORY, "the code, the guide, the tests, the recorded runs"], None)])]


SCENES = {
    "01-what": scene_01_what,
    "02-layer": scene_02_layer,
    "03-start": scene_03_start,
    "03-run": scene_03_run,
    "04-proof": scene_04_proof,
    "04-own-plan": scene_04_own_plan,
    "04-protected": scene_04_protected,
    "04-review-no": scene_04_review_no,
    "05-limits": scene_05_limits,
    "05-not-yet": scene_05_not_yet,
    "06-close": scene_06_close,
}
