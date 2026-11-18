"""The showcase manifest: one `Clip` per recorded feature, in order, and the limits every clip and the
folder it lands in must keep. Standard library only (the tests read it without Pillow).

  number, name   the files are docs/showcase/<NN>-<name>.gif / .png in the tree, and the video
                 showcase-<NN>-<name>.mp4 in the ignored media folder (a release asset, docs/media.json)
  kind           "browser" (the dashboard in Chrome) or "terminal" (real tools/al output, rendered)
  card           the one-line caption card the clip opens with
  proves         what the clip shows to be true (docs/showcase/README.md)
  sentence       the README's "What it shows" cell
  guide          where to read more (a docs/GUIDE.md section, or the showcase README)
  commands       terminal clips: the exact argv shown on screen, in order, with paths relative to the
                 work folder (~/work). terminal_clips.py runs exactly these. Values only known at run
                 time are placeholders filled then: {week} (the ISO week of today, e.g. 2026-s47),
                 {commit} (the commit a run delivered), {port} (a free local port).
"""
from __future__ import annotations

from dataclasses import dataclass

SIZE = (1280, 720)
FPS = 15
MAX_MP4 = 3_000_000
MAX_GIF = 2_000_000
MAX_TOTAL = 30_000_000
MIN_SECONDS = 10
MAX_SECONDS = 40
CARD_SECONDS = 3
MAX_CARD_CHARS = 64          # what fits one line at the card font (term.caption_card checks the pixels)
KINDS = ("browser", "terminal")

# sample values for the placeholders (tests; the recording fills the real ones)
SAMPLES = {"week": "2026-s47", "commit": "0123abc", "port": "18765"}


@dataclass(frozen=True)
class Clip:
    number: int
    name: str
    kind: str
    card: str
    proves: str
    sentence: str
    guide: str
    commands: tuple[tuple[str, ...], ...] = ()

    @property
    def stem(self) -> str:
        return f"{self.number:02d}-{self.name}"


def fill(argv: tuple[str, ...], values: dict[str, str]) -> list[str]:
    """argv with its {placeholders} filled."""
    out = []
    for arg in argv:
        for key, value in values.items():
            arg = arg.replace("{" + key + "}", value)
        out.append(arg)
    return out


CLIPS: tuple[Clip, ...] = (
    Clip(1, "start-work", "browser",
         "Start work from the page: a prompt, a workflow, a model",
         "A run is started from the dashboard's Start form with a chosen workflow and a model for the heavy tier, "
         "and the page follows it until every step has passed (mock agent).",
         "Start a workflow from the dashboard with a prompt and a model; the run's graph fills in live.",
         "docs/GUIDE.md#start-work-from-the-dashboard-or-the-command-line"),
    Clip(2, "repos", "browser",
         "Repos beside each other; create a layer, retry a failed step",
         "The Repos view lists sibling repositories with their state, creates a layer for one with the real al "
         "tool, stops at the step that needs Racket with al's own sentence, and passes after Retry from that step.",
         "The Repos view: four repositories, a Create job with its command lines, a missing tool, Retry from.",
         "docs/GUIDE.md#work-with-several-repositories-from-the-dashboard"),
    Clip(3, "run-documents", "browser",
         "A run's documents, its artefacts, and switching a model",
         "A run's Documents tab renders its plan and review report with a raw view and a download, the Artefacts "
         "tab shows its screenshots, and the Models panel switches the heavy tier as a local setting.",
         "Plans and reports rendered in the run page, screenshots as artefacts, the heavy tier switched to another model.",
         "docs/GUIDE.md#choose-models-and-limit-spending"),
    Clip(4, "review-says-no", "browser",
         "A review that says no: three rounds, then a person",
         "A review that rejects three rounds is shown as rejected (⊗) with each round's reasons, the run is "
         "handed over to a person, and the Runs list counts it as handed over, not passed.",
         "A rejected review with its three rounds and reasons; the run is handed over, not counted as passed.",
         "docs/GUIDE.md#step-12--let-small-things-ship-by-themselves"),
    Clip(5, "delivery", "terminal",
         "Sprint delivery and promotion (the mock agent plays the model)",
         "With (delivery sprint), runs deliver into the cycle's sprint branch, a person merges each, and al promote "
         "waits for an approval, then merges into main, bumps the version, writes the changelog and tags.",
         "Runs delivered into a sprint branch; al promote waits for a person, then merges, versions and tags.",
         "docs/GUIDE.md#how-work-is-delivered",
         (("grep", "-n", "delivery\\|stage\\|promotion", "notes.al/agentic.rkt"),
          ("al", "cycle", "notes.al", "start"),
          ("al", "run", "notes.al", "/change", "Add a cart", "--class", "feature", "--dry-run"),
          ("git", "-C", "sprint", "merge", "--no-ff", "-q", "-m", "Merge the cart", "{commit}"),
          ("al", "run", "notes.al", "/change", "Add search", "--class", "feature", "--dry-run"),
          ("git", "-C", "sprint", "merge", "--no-ff", "-q", "-m", "Merge search", "{commit}"),
          ("git", "-C", "notes", "log", "--oneline", "sprint/{week}"),
          ("al", "promote", "notes.al", "sprint", "main"),
          ("al", "decide", "notes.al", "sprint", "main", "approve", "--by", "Ada Example"),
          ("al", "promote", "notes.al", "sprint", "main"))),
    Clip(6, "sandbox", "terminal",
         "The sandbox: status, doctor, the preflight, al doctor",
         "al sandbox reports the image recipe and checks a throwaway container like a run does, a run stops at its "
         "preflight with the table of what is missing, and al doctor lists the tools al found.",
         "al sandbox status and doctor, a run stopped by its preflight table, and al doctor's tool list.",
         "docs/GUIDE.md#the-container-al-sandbox",
         (("al", "sandbox", "notes.al", "status"),
          ("al", "sandbox", "notes.al", "doctor"),
          ("al", "run", "notes.al", "/change", "Tidy the readme", "--dry-run"),
          ("al", "doctor", "notes.al", "--agent", "mock"))),
    Clip(7, "adopt", "terminal",
         "al adopt: an existing agent setup becomes a layer",
         "al adopt reads a repository's own rule files, prompts and workflow scripts, shows what it would write, "
         "writes them into the layer with a report, and leaves the application untouched.",
         "An existing agent setup (rule files, prompts, scripts) adopted into a new layer; the app is untouched.",
         "docs/GUIDE.md#step-2--describe-the-codebase",
         (("al", "init", "larder"),
          ("al", "adopt", "larder.al", "--app", "larder", "--dry-run"),
          ("al", "adopt", "larder.al", "--app", "larder"),
          ("head", "-n", "16", "larder.al/adopted/REPORT.md"),
          ("git", "-C", "larder", "status", "--short"))),
    Clip(8, "deploy-remote", "terminal",
         "al deploy and al dashboard --remote (the tests' fake host)",
         "al deploy --dry-run prints every command it would run on the host and changes nothing, and al dashboard "
         "--remote opens an ssh tunnel, loopback on both ends, and closes it on Ctrl-C.",
         "A dry-run deploy to a host, then the dashboard of a deployed layer through an ssh tunnel.",
         "docs/showcase/README.md#08-deploy-remote",
         (("al", "deploy", "notes/agentic", "--to", "deploy-host.example", "--dry-run"),
          ("al", "dashboard", "notes/agentic", "--remote", "testhost.invalid", "--port", "{port}"))),
    Clip(9, "promotion-pack", "browser",
         "A promotion in the page: its changes, accept or refuse, the pack",
         "The layer page lists the open promotion, its card shows each change with criteria and proof to accept "
         "or refuse, and the presentation pack page shows the pictures and recordings.",
         "The open promotion of a layer, its card with accept/refuse per change, and the presentation pack.",
         "docs/GUIDE.md#decide"),
)

# extra guide sections a README row also links (the "Watch / read" column)
# (`## Promote a stage` is not a heading as GitHub renders docs/GUIDE.md: an unclosed code fence above it
# swallows it, see docs/showcase/README.md; its first heading that renders is `### Run it`)
MORE_GUIDE = {3: "docs/GUIDE.md#watch-the-work", 5: "docs/GUIDE.md#run-it",
              6: "docs/GUIDE.md#when-al-cannot-find-a-tool-al-doctor"}


def by_name(names: list[str] | tuple[str, ...]) -> list[Clip]:
    """The clips named by number (`05`, `5`), stem (`05-delivery`) or name (`delivery`); all when empty."""
    if not names:
        return list(CLIPS)
    out = []
    for wanted in names:
        found = [c for c in CLIPS if wanted in (c.stem, c.name) or (wanted.isdigit() and int(wanted) == c.number)]
        if not found:
            raise KeyError(f"no clip {wanted!r}; clips: {', '.join(c.stem for c in CLIPS)}")
        out += [c for c in found if c not in out]
    return out
