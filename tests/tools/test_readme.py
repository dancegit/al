"""The README is the only document most readers see, and it promises a lot: what al is and why it exists
on its first screen, one block per principle with a pointer into the repository, commands a reader can
type, and walk-throughs one link away in docs/EXAMPLES.md. This keeps those promises true without
running anything: every link and image resolves to a file git tracks, every `tools/al …` command shown
is one al's own argument parser accepts (parsing only; nothing runs past it), every principle links
into the repository, the first screen says what, why and which method, and the README stays short.

    uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_readme.py -q
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import re
import shlex
from pathlib import PurePosixPath

import pytest

from conftest import ROOT, git
from test_links import CODE, FENCE, resolve, targets

DOCUMENTS = ("README.md", "docs/EXAMPLES.md", "docs/showcase/README.md")
LINE_BUDGET = 330
PRINCIPLES = "## The principles, and what al does about each"
SHELL_FENCE = re.compile(r"^(`{3,}|~{3,})[ \t]*(bash|sh|shell|console)[ \t]*\n(.*?)^\1[ \t]*$", re.M | re.S)
COMMAND = re.compile(r"^(tools/al|al) ([a-z][a-z-]*)(\s|$)")
# al's subcommands, read from its parser: a line such as "al is a tool" is prose, "al gen L" is a command
SUBCOMMANDS = set(re.findall(r'\bsub\.add_parser\("([a-z-]+)"', (ROOT / "tools" / "al").read_text(encoding="utf-8")))


def _tracked() -> tuple[set[str], set[str]]:
    tracked = set(git(ROOT, "ls-files").splitlines())
    directories = {str(PurePosixPath(p).parent) for p in tracked} | {
        "/".join(p.split("/")[:n]) for p in tracked for n in range(1, p.count("/") + 1)}
    return tracked, directories


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _normalise(line: str) -> str:
    return shlex.join(shlex.split(line, comments=True))


def _shown_commands(text: str) -> list[str]:
    """The al commands a reader is shown: lines of shell code blocks that start with `tools/al ` or `al `
    and a subcommand word, then inline code spans outside code blocks that start with `tools/al `. Other
    code blocks (a folder tree, the repository map) are pictures, not commands."""
    found = []
    for block in SHELL_FENCE.finditer(text):
        for line in block.group(3).splitlines():
            match = COMMAND.match(line.strip())
            if match and (match.group(1) == "tools/al" or match.group(2) in SUBCOMMANDS):
                found.append(_normalise(line.strip()))
    for span in CODE.finditer(FENCE.sub("", text)):
        inner = span.group(0).strip("`").strip()
        if inner.startswith("tools/al "):
            found.append(_normalise(inner))
    return found


def test_links_and_images_resolve_to_tracked_files() -> None:
    tracked, directories = _tracked()
    assert "docs/EXAMPLES.md" in tracked, "docs/EXAMPLES.md must be tracked (git add it) for the README's links to resolve"
    broken = []
    for document in DOCUMENTS:
        for target in targets((ROOT / document).read_text(encoding="utf-8")):
            path = resolve(document, target)
            if path and path not in tracked and path.rstrip("/") not in directories:
                broken.append(f"{document}: {target}")
    assert not broken, "links to files that are not in the repository:\n  " + "\n  ".join(broken)


class _Parsed(Exception):
    """Raised right after the top-level parse succeeded, so main() goes no further."""


def _parser_module(monkeypatch: pytest.MonkeyPatch):
    """tools/al loaded as a module, its parser patched so that a command that parses stops right there."""
    loader = importlib.machinery.SourceFileLoader("al_readme", str(ROOT / "tools" / "al"))
    spec = importlib.util.spec_from_loader("al_readme", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    original = argparse.ArgumentParser.parse_known_args

    def wrapper(self, args=None, namespace=None):
        _, rest = original(self, args, namespace)
        if rest:
            self.error(f"unrecognized arguments: {' '.join(rest)}")
        raise _Parsed

    # main() parses with parse_known_args (it hands a left-over RUN to `al assign` itself); parse_args
    # goes through it too, so either way a command that parses stops here and left-overs are refused
    monkeypatch.setattr(argparse.ArgumentParser, "parse_known_args", wrapper)
    return module


def _refused(module, commands: list[str]) -> list[str]:
    """The commands al's parser does not accept, each with its usage error."""
    refused = []
    for command in commands:
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                module.main(shlex.split(command)[1:])
        except _Parsed:
            continue
        except SystemExit as exc:
            refused.append(f"{command}  (exit {exc.code}): {err.getvalue().strip()}")
            continue
        refused.append(f"{command}  (main returned without parsing)")
    return refused


def test_every_shown_al_command_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _parser_module(monkeypatch)
    commands = _shown_commands(_readme())
    assert len(commands) >= 8, f"the README shows too few al commands: {commands}"
    refused = _refused(module, commands)
    assert not refused, "commands in the README that al does not accept:\n  " + "\n  ".join(refused)


GUIDE_BACKUP = "### Back up, regenerate or write the definition again"


def test_guide_backup_and_redefine_commands_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    text = (ROOT / "docs" / "GUIDE.md").read_text(encoding="utf-8")
    start = text.index("\n" + GUIDE_BACKUP + "\n")
    end = text.index("\n#", start + len(GUIDE_BACKUP) + 2)
    commands = _shown_commands(text[start:end])
    assert len(commands) >= 6, f"the GUIDE section shows too few al commands: {commands}"
    assert all(c.split()[1] in ("snapshot", "gen", "author") for c in commands), commands
    refused = _refused(_parser_module(monkeypatch), commands)
    assert not refused, "commands in the GUIDE section that al does not accept:\n  " + "\n  ".join(refused)


def test_every_principle_links_into_the_repository() -> None:
    text = _readme()
    start = text.index("\n" + PRINCIPLES + "\n")
    end = text.index("\n## ", start + 1)
    blocks = re.split(r"^### ", text[start:end], flags=re.M)[1:]
    assert len(blocks) >= 10, f"expected at least 10 principles, found {len(blocks)}"
    tracked, directories = _tracked()
    unlinked = []
    for block in blocks:
        heading = block.splitlines()[0]
        paths = [resolve("README.md", t) for t in targets(block)]
        if not any(p and (p in tracked or p.rstrip("/") in directories) for p in paths):
            unlinked.append(heading)
    assert not unlinked, "principles without a link into the repository: " + ", ".join(unlinked)


def test_first_screen_says_what_why_and_method() -> None:
    text = _readme()
    starts = [m.start() for m in re.finditer(r"^## ", text, re.M)]
    assert len(starts) >= 2
    first_screen = text[:starts[1]]
    assert text[starts[0]:].splitlines()[0] == "## Why it exists"
    assert "docs/demo/pipeline.png" in first_screen
    assert "Tactical Agentic Coding" in first_screen


def test_readme_within_line_budget() -> None:
    count = len(_readme().splitlines())
    assert count < LINE_BUDGET, f"README.md has {count} lines; the budget is under {LINE_BUDGET}"


def test_the_command_finder_finds_what_it_must() -> None:
    assert {"init", "gen", "verify", "sandbox", "improve"} <= SUBCOMMANDS and "is" not in SUBCOMMANDS
    sample = ("Some prose.\n"
              "```bash\ntools/al gen L  # c\nuv run x.py\n```\n"
              "```\ntools/al          init · scout · gen\n```\n"
              "al is a tool\n"
              "Run `tools/al check L` and then `al gen`.\n")
    assert _shown_commands(sample) == ["tools/al gen L", "tools/al check L"]
    assert _shown_commands("```sh\nal improve L 'a request'\nal is a tool\n```\n") == ["al improve L 'a request'"]
