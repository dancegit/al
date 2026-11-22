"""The stories of the demo video: story name → narration file, scene module, clips folder, output names.

`rota` is the default: the hospital rota app and the `cart` demo library (narration.md, scenes.py).
`testbed` is the same seven-part arc on the pinned public codebase flask, delivered to a local Forgejo
(narration-testbed.md, scenes_testbed.py). Each story has its own narration, scene module, clips folder
and output names, so both can share one `--work` DIR and one OUT folder. Standard library only.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Story:
    name: str
    narration: Path                 # the committed script
    module: str                     # the module whose SCENES records it
    video: str                      # the names `make.py assemble` writes into OUT
    timed: str
    sheet: str                      # the names `verify.py` writes beside them
    report: str
    clips: str                      # the folder of its clips in --work DIR


STORIES: dict[str, Story] = {
    "rota": Story("rota", HERE / "narration.md", "scenes", "al-demo.mp4", "narration.md",
                  "al-demo-contact-sheet.png", "privacy-ocr.json", "clips"),
    "testbed": Story("testbed", HERE / "narration-testbed.md", "scenes_testbed", "al-demo-testbed.mp4",
                     "narration-testbed.md", "al-demo-testbed-contact-sheet.png", "privacy-ocr-testbed.json",
                     "clips-testbed"),
}
DEFAULT = "rota"


def get(name: str) -> Story:
    try:
        return STORIES[name]
    except KeyError:
        raise KeyError(f"no story {name!r}; stories: {', '.join(STORIES)}") from None


def scenes_of(story: Story | str):
    """The scene module of a story, imported when first asked for."""
    if isinstance(story, str):
        story = get(story)
    return importlib.import_module(story.module)
