"""Every relative link and image in the repository's documents points at a file git tracks. A
target that exists only on the disk (untracked, or git-ignored like docs/layers once was) looks
fine locally and is broken for everyone who clones the repository."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from conftest import ROOT, git

PATTERNS = ("README.md", "docs/**/*.md", "tests/**/README.md", "kit/**/README.md", "meta/README.md")
INLINE = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+\"[^\"]*\")?\s*$", re.M)
HTML = re.compile(r"<(?:img|a|video|source)\b[^>]*?\b(?:src|href)=\"([^\"]+)\"", re.I)
FENCE = re.compile(r"^(\s*)(`{3,}|~{3,}).*?^\1\2[ \t]*$", re.M | re.S)
CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)


def documents(tracked: set[str]) -> list[str]:
    return sorted({p.relative_to(ROOT).as_posix() for pattern in PATTERNS for p in ROOT.glob(pattern)} & tracked)


def targets(text: str) -> list[str]:
    text = CODE.sub("", FENCE.sub("", text))       # links shown as code are examples, not links
    found = INLINE.findall(text) + REFERENCE.findall(text) + HTML.findall(text)
    return [t for t in found if not re.match(r"^([a-z][a-z0-9+.-]*:|#|//)", t, re.I)]


def resolve(document: str, target: str) -> str:
    path = target.split("#", 1)[0].split("?", 1)[0]
    if path.startswith("/"):
        return path.lstrip("/")
    parts: list[str] = []
    for part in PurePosixPath(document).parent.joinpath(path).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def test_every_relative_link_points_at_a_tracked_file() -> None:
    tracked = set(git(ROOT, "ls-files").splitlines())
    directories = {str(PurePosixPath(p).parent) for p in tracked} | {
        "/".join(p.split("/")[:n]) for p in tracked for n in range(1, p.count("/") + 1)}
    docs = documents(tracked)
    assert "README.md" in docs and "docs/GUIDE.md" in docs and "meta/README.md" in docs
    broken = []
    for document in docs:
        for target in targets((ROOT / document).read_text(encoding="utf-8")):
            path = resolve(document, target)
            if path and path not in tracked and path.rstrip("/") not in directories:
                why = "exists but is not tracked by git" if (ROOT / path).exists() else "does not exist"
                broken.append(f"{document}: {target} ({why})")
    assert not broken, "links to files that are not in the repository:\n  " + "\n  ".join(broken)


def test_the_checker_finds_what_it_must() -> None:
    sample = ("[a](docs/x.md) ![b](img/y.png \"t\") [c](https://e.org) [d](#top) `[e](no.md)`\n"
              "```\n[f](fenced.md)\n```\n[g]: ref/z.md\n<img src=\"pic.gif\" alt=\"\">\n")
    assert targets(sample) == ["docs/x.md", "img/y.png", "ref/z.md", "pic.gif"]
    assert resolve("docs/GUIDE.md", "../README.md#run") == "README.md"
    assert resolve("docs/layers/README.md", "al.md") == "docs/layers/al.md"
    assert Path(ROOT / "README.md").is_file()
