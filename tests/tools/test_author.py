"""`al author LAYER`: the hand-off of GUIDE Step 4 as a command. With the mock agent (no LLM) it writes
the definition, compiles it, never overwrites an existing one and never writes outside the layer."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import LAYER_TEMPLATE, ROOT, al, status

sys.path.insert(0, str(ROOT / "tools"))
import al_author  # noqa: E402

DEFINITION = LAYER_TEMPLATE.format(root="${root}", placement="(layer sibling)").replace('#:name "notes"', '#:name "${name}"')


def mock_file(tmp_path: Path, files: dict[str, str], output: str = "wrote agentic.rkt") -> Path:
    (tmp_path / "mocks").mkdir(exist_ok=True)
    path = tmp_path / "mocks" / "author-mock.json"
    path.write_text(json.dumps({"files": files, "output": output}))
    return path


def test_author_with_the_mock_agent_writes_a_definition_that_compiles(app, tmp_path):
    al("init", app)
    layer = app.with_name("notes.al")
    out = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path, {"agentic.rkt": DEFINITION})).stdout
    assert "wrote agentic.rkt" in out and "agentic.rkt compiles" in out
    al("compile", layer)
    text = (layer / "agentic.rkt").read_text()
    assert '#:root "../notes"' in text and '#:name "notes"' in text
    assert status(app) == ""


def test_author_refuses_an_existing_definition(app, tmp_path):
    al("init", app)
    layer = app.with_name("notes.al")
    (layer / "agentic.rkt").write_bytes(b"anything at all\n")
    cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path, {"agentic.rkt": DEFINITION}), ok=False)
    assert cp.returncode == 1
    assert "al: author: " in cp.stderr and "agentic.rkt already exists; it is never overwritten" in cp.stderr
    assert (layer / "agentic.rkt").read_bytes() == b"anything at all\n"


def test_author_needs_llm_txt(app, tmp_path):
    layer = app.with_name("notes.al")
    layer.mkdir()
    cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path, {"agentic.rkt": DEFINITION}), ok=False)
    assert cp.returncode == 1 and "al init APP" in cp.stderr
    assert not (layer / "agentic.rkt").exists()


def test_author_reports_a_definition_that_does_not_compile(app, tmp_path):
    al("init", app)
    layer = app.with_name("notes.al")
    bad = '#lang al\n(agentic-layer #:name "notes" (no-such-form))\n'
    cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path, {"agentic.rkt": bad}), ok=False)
    assert cp.returncode == 1
    assert "does not compile" in cp.stderr
    assert "meta/commands/repair-layer.md" in cp.stderr and "delete it" in cp.stderr
    assert (layer / "agentic.rkt").read_text() == bad


def test_author_mock_paths_stay_inside_the_layer(app, tmp_path):
    al("init", app)
    layer = app.with_name("notes.al")
    mock = mock_file(tmp_path, {"../escape.rkt": "x", "agentic.rkt": DEFINITION})
    before = sorted(p.name for p in layer.parent.iterdir())
    cp = al("author", layer, "--agent", "mock", "--mock", mock, ok=False)
    assert cp.returncode == 1 and "outside the layer" in cp.stderr
    assert not (layer.parent / "escape.rkt").exists() and not (layer / "agentic.rkt").exists()
    assert sorted(p.name for p in layer.parent.iterdir()) == before


def test_author_claude_argv_is_a_fixed_list(tmp_path):
    argv = al_author.claude_argv(ROOT, tmp_path)
    assert isinstance(argv, list) and all(isinstance(x, str) for x in argv)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    prompt = al_author.author_prompt(tmp_path, "some preferences")
    assert not any(prompt in x or "author-layer.md" in x for x in argv)   # the prompt goes on stdin
    assert "meta/commands/author-layer.md" in prompt and f"{tmp_path}/LLM.txt" in prompt
