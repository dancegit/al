"""The rules of the five-minute demo video (tools/demovideo): what must never be on a frame, how to
blank it, the narration script's form and its checks, and how a scene is fitted to its length.
Standard library only: the tests read it without Pillow.

The privacy patterns are stricter than tools/showcase/privacy.py: no e-mail address at all, no IPv4
address at all (loopback too), no home-folder path and not the word `token`. None of them relies on a
space or a word boundary beside the match, because OCR joins neighbouring words.

The narration script (narration.md):

    ## N. Title                          a part, N = 1…6 with the titles of TITLES
    ### scene-id | kind | SECONDS s      a scene; kind is card, terminal or browser
    - [+S.S] text                        a caption, S.S seconds into its scene, until the next one

The timed copy that `make.py assemble` writes beside the video has the same form with times in the
final video added: `### scene-id | kind | SECONDS s | m:ss.s–m:ss.s` and `- [+S.S | m:ss.s–m:ss.s] text`.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SIZE = (1280, 720)
FPS = 15
MIN_SECONDS = 270
MAX_SECONDS = 300
SHEET_EVERY = 10
OCR_EVERY = 5
STRIP_H = 64
MAX_CAPTION_CHARS = 64
MIN_CAPTION_SECONDS = 2.5
TITLES = ("What al is", "Writing a layer", "A real run", "Proof and guardrails", "Honest limits", "What shipped")
KINDS = ("card", "terminal", "browser")
BLOCK = "█"

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
PATTERNS: dict[str, re.Pattern[str]] = {
    "mail": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", re.I),
    "home": re.compile(r"/home/|/Users/", re.I),
    "ipv4": re.compile(rf"(?<!\d)(?<!\d\.){_OCTET}(?:\.{_OCTET}){{3}}(?!\d)(?!\.\d)", re.I),
    "token": re.compile(r"token", re.I),
}
KINDS_OF_HIT = ("mail", "home", "ipv4", "token", "host", "repo")
# a home-folder path up to its end: what the recorder blanks, so that no folder name is left behind it
HOME_PATH = re.compile(r"(?:/home/|/Users/)[^\s\"'`<>|()\[\]{},;]*", re.I)


@dataclass(frozen=True)
class Hit:
    kind: str
    match: str
    excerpt: str
    start: int = 0


# ------------------------------------------------------------------------------------------ privacy

def _main_checkout(root: Path) -> Path | None:
    try:
        cp = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    common = cp.stdout.strip()
    if cp.returncode != 0 or not common:
        return None
    return Path(common).parent


def _clean(values: list[str], drop: tuple[str, ...] = ()) -> list[str]:
    out: list[str] = []
    for value in values:
        value = (value or "").strip()
        if len(value) < 3 or value.lower() == "localhost" or value.lower() in drop:
            continue
        if value.lower() not in [v.lower() for v in out]:
            out.append(value)
    return out


def machine_values(root: Path | str) -> dict[str, list[str]]:
    """This machine's own names: `hosts` (the host name, its FQDN, AL_HOST_ID, and every "host" of a
    run record under the main checkout's layer) and `repos` (the folders beside the main checkout that
    are repositories, plus AL_DEMO_FORBIDDEN_NAMES, minus al). Nothing found is an empty list."""
    hosts: list[str] = []
    try:
        name = socket.gethostname()
        hosts.append(name)
        hosts.append(socket.getfqdn(name))
    except OSError:
        pass
    hosts.append(os.environ.get("AL_HOST_ID", ""))
    repos: list[str] = []
    main = _main_checkout(Path(root))
    if main is not None:
        try:
            records = sorted((main / "agentic" / "agents").glob("*/state.json"))
        except OSError:
            records = []
        for record in records:
            try:
                found = re.search(r'"host"\s*:\s*"([^"]+)"', record.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if found:
                hosts.append(found.group(1))
        try:
            repos += [p.name for p in sorted(main.parent.iterdir()) if p.is_dir() and (p / ".git").exists()]
        except OSError:
            pass
    repos += os.environ.get("AL_DEMO_FORBIDDEN_NAMES", "").split(",")
    return {"hosts": _clean(hosts), "repos": _clean(repos, drop=("al",))}


def _excerpt(text: str, start: int, end: int) -> str:
    return text[max(0, start - 20): end + 20].replace("\n", "⏎")


def find_private(text: str, values: dict[str, list[str]] | None = None) -> list[Hit]:
    """Every private match in `text`, in order: the four patterns, then this machine's host names and
    the other repositories' names (case-insensitive substrings)."""
    hits: list[Hit] = []
    for kind, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            hits.append(Hit(kind, m.group(0), _excerpt(text, m.start(), m.end()), m.start()))
    lower = text.lower()
    for kind, key in (("host", "hosts"), ("repo", "repos")):
        for value in (values or {}).get(key, []):
            needle, at = value.lower(), 0
            if not needle:
                continue
            while (at := lower.find(needle, at)) >= 0:
                hits.append(Hit(kind, text[at: at + len(needle)], _excerpt(text, at, at + len(needle)), at))
                at += len(needle)
    return sorted(hits, key=lambda h: (h.start, KINDS_OF_HIT.index(h.kind)))


def blank(text: str, values: dict[str, list[str]] | None = None, whole_paths: bool = False) -> tuple[str, list[Hit]]:
    """`text` with each private match replaced by █ repeated to the same length, and the matches.
    With whole_paths a home-folder match reaches to the end of its path (HOME_PATH)."""
    hits = find_private(text, values)
    if whole_paths:
        hits = [h for h in hits if h.kind != "home"]
        hits += [Hit("home", m.group(0), _excerpt(text, m.start(), m.end()), m.start()) for m in HOME_PATH.finditer(text)]
    chars = list(text)
    for hit in hits:
        for i in range(hit.start, hit.start + len(hit.match)):
            if chars[i] != "\n":
                chars[i] = BLOCK
    return "".join(chars), hits


# ------------------------------------------------------------------------------------------ narration

@dataclass
class Caption:
    offset: float
    text: str
    start: float | None = None          # seconds into the final video (the timed copy only)
    end: float | None = None


@dataclass
class Scene:
    id: str
    kind: str
    seconds: float
    captions: list[Caption] = field(default_factory=list)
    start: float | None = None
    end: float | None = None


@dataclass
class Part:
    number: int
    title: str
    scenes: list[Scene] = field(default_factory=list)


@dataclass
class Script:
    parts: list[Part] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def scenes(self) -> list[Scene]:
        return [scene for part in self.parts for scene in part.scenes]

    @property
    def seconds(self) -> float:
        return sum(scene.seconds for scene in self.scenes)

    def scene(self, scene_id: str) -> Scene:
        for scene in self.scenes:
            if scene.id == scene_id:
                return scene
        raise KeyError(f"no scene {scene_id!r} in the narration script")


_TIME = r"\d+:\d\d(?:\.\d)?"
_PART = re.compile(r"^## (\d+)\. (.+?)\s*$")
_SCENE = re.compile(rf"^### (\S+) \| (\w+) \| (\d+(?:\.\d+)?) s(?: \| ({_TIME})–({_TIME}))?\s*$")
_CAPTION = re.compile(rf"^- \[\+(\d+(?:\.\d+)?)(?: \| ({_TIME})–({_TIME}))?\] (.+?)\s*$")


def clock(seconds: float) -> str:
    """m:ss.s"""
    tenths = int(round(seconds * 10))
    return f"{tenths // 600}:{(tenths % 600) // 10:02d}.{tenths % 10}"


def unclock(text: str | None) -> float | None:
    if text is None:
        return None
    minutes, seconds = text.split(":")
    return int(minutes) * 60 + float(seconds)


def parse(text: str) -> Script:
    """The script in `text`. A line of a known kind in the wrong place is noted in `problems`."""
    script = Script()
    scene: Scene | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            m = _PART.match(line)
            if not m:
                if script.parts:                  # the timed copy ends with plain `## ` sections
                    break
                script.problems.append(f"line {number}: not a part heading: {line!r}")
                continue
            script.parts.append(Part(int(m.group(1)), m.group(2)))
            scene = None
        elif line.startswith("### "):
            m = _SCENE.match(line)
            if not m or not script.parts:
                script.problems.append(f"line {number}: not a scene heading: {line!r}")
                scene = None
                continue
            scene = Scene(m.group(1), m.group(2), float(m.group(3)), start=unclock(m.group(4)), end=unclock(m.group(5)))
            script.parts[-1].scenes.append(scene)
        elif line.startswith("- [") and script.parts:
            m = _CAPTION.match(line)
            if not m or scene is None:
                script.problems.append(f"line {number}: not a caption of a scene: {line!r}")
                continue
            scene.captions.append(Caption(float(m.group(1)), m.group(4), unclock(m.group(2)), unclock(m.group(3))))
    return script


def validate(script: Script) -> list[str]:
    """What is wrong with the script; empty when nothing is."""
    problems = list(script.problems)
    if [(p.number, p.title) for p in script.parts] != list(enumerate(TITLES, 1)):
        problems.append("the parts are not 1…6 in order with the titles " + ", ".join(TITLES))
    total = script.seconds
    if not MIN_SECONDS <= total <= MAX_SECONDS:
        problems.append(f"the scenes add up to {total:g} s, outside {MIN_SECONDS}–{MAX_SECONDS} s")
    seen: set[str] = set()
    for part in script.parts:
        if not part.scenes:
            problems.append(f"part {part.number} has no scene")
        for scene in part.scenes:
            if scene.id in seen:
                problems.append(f"{scene.id}: the scene id is used twice")
            seen.add(scene.id)
            if scene.kind not in KINDS:
                problems.append(f"{scene.id}: kind {scene.kind!r} is not one of {', '.join(KINDS)}")
            if not scene.captions or scene.captions[0].offset != 0.0:
                problems.append(f"{scene.id}: no caption at +0.0")
            for i, caption in enumerate(scene.captions):
                until = scene.captions[i + 1].offset if i + 1 < len(scene.captions) else scene.seconds
                if i and caption.offset <= scene.captions[i - 1].offset:
                    problems.append(f"{scene.id}: the offset +{caption.offset:g} does not come after the one before it")
                elif until - caption.offset < MIN_CAPTION_SECONDS - 1e-9:
                    problems.append(f"{scene.id}: the caption at +{caption.offset:g} is on screen for {until - caption.offset:g} s, "
                                    f"under {MIN_CAPTION_SECONDS} s")
                if len(caption.text) > MAX_CAPTION_CHARS:
                    problems.append(f"{scene.id}: the caption at +{caption.offset:g} has {len(caption.text)} characters, "
                                    f"over {MAX_CAPTION_CHARS}")
                for hit in find_private(caption.text):
                    problems.append(f"{scene.id}: the caption at +{caption.offset:g} holds a private match ({hit.kind}): {hit.match}")
    return problems


# ------------------------------------------------------------------------------------------ fitting

def fit(real_seconds: float, target_seconds: float) -> tuple[float, float]:
    """(speed, hold): the body is played `speed` times faster (never slower) and its last frame is
    held for `hold` seconds, so that it lasts `target_seconds`."""
    if real_seconds > target_seconds:
        return real_seconds / target_seconds, 0.0
    return 1.0, target_seconds - real_seconds
