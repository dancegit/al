"""`al author LAYER`: the hand-off of GUIDE Step 4 as a command. With the mock agent (no LLM) it writes
the definition, compiles it, never overwrites an existing one and never writes outside the layer."""
from __future__ import annotations

import json
import os
import sys
import time
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


def test_author_claude_argv_is_a_fixed_list(tmp_path, monkeypatch):
    monkeypatch.delenv("AL_AGENT_PERSONAL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))     # no personal tools.env
    isolation = al_author.author_isolation(tmp_path)
    try:
        argv = al_author.claude_argv(ROOT, tmp_path, isolation=isolation)
    finally:
        isolation.cleanup()
    assert isinstance(argv, list) and all(isinstance(x, str) for x in argv)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--setting-sources") + 1] == "project" and "--strict-mcp-config" in argv
    assert "--add-dir" not in argv
    tools = argv[argv.index("--allowedTools") + 1].split(",")
    assert f"Edit(/{tmp_path.resolve()}/**)" in tools and f"Write(/{tmp_path.resolve()}/**)" in tools
    assert f"Bash({ROOT}/tools/al compile:*)" in tools
    assert not [t for t in tools if t.startswith(("Edit(", "Write(")) and str(tmp_path.resolve()) not in t]
    prompt = al_author.author_prompt(tmp_path, "some preferences")
    assert not any(prompt in x or "author-layer.md" in x for x in argv)   # the prompt goes on stdin
    assert "meta/commands/author-layer.md" in prompt and f"{tmp_path}/LLM.txt" in prompt
    assert f"{tmp_path}/AL-DSL.llms.txt" in prompt and f"{ROOT}/tools/al compile {tmp_path}" in prompt
    assert "## Decision Table" in prompt and "some preferences" in prompt   # the command itself is inlined


def rendered_definition() -> str:
    return DEFINITION.replace("${root}", "../notes").replace("${name}", "notes")


def test_author_stops_before_the_agent_when_racket_is_missing(tmp_path):
    (tmp_path / "notes" / ".git").mkdir(parents=True)
    layer = tmp_path / "notes.al"
    layer.mkdir()
    (layer / "LLM.txt").write_text("# notes\n")
    missing = al_author.al_tools.Found("racket", looked=["AL_RACKET (not set)", "PATH", "~/racket/bin"])

    def need(names, where=None):
        raise al_author.al_tools.ToolMissing([missing])

    class FakeAl:
        CompilerUnavailable = type("CompilerUnavailable", (Exception,), {})

        def tools_module(self):
            return type("Tools", (), {"need": staticmethod(need), "agent_bin": staticmethod(al_author.al_tools.agent_bin),
                                      "ToolMissing": al_author.al_tools.ToolMissing})

        def compile_layer(self, layer):
            raise AssertionError("nothing is compiled when racket is missing")

    said: list[str] = []
    began = time.monotonic()
    with pytest.raises(al_author.al_tools.ToolMissing) as caught:
        al_author.author(FakeAl(), layer, "mock", mock_file(tmp_path, {"agentic.rkt": DEFINITION}), None, say=said.append)
    assert time.monotonic() - began < 1
    assert "racket is missing" in str(caught.value) and "AL_RACKET" in str(caught.value)
    assert al_author.REPAIR_HINT not in str(caught.value) and caught.value.hint == ""
    assert not (layer / "agentic.rkt").exists() and said == []


def test_a_compiler_that_cannot_start_does_not_blame_the_file(app, tmp_path):
    al("init", app)
    layer = app.with_name("notes.al")
    mock = mock_file(tmp_path, {"agentic.rkt": DEFINITION})
    # a racket that is there and executable but cannot be started (its interpreter does not exist)
    broken = tmp_path / "broken" / "racket"
    broken.parent.mkdir()
    broken.write_text("#!/nonexistent/interpreter\n")
    broken.chmod(0o755)
    cp = al("author", layer, "--agent", "mock", "--mock", mock, env={"AL_RACKET": str(broken)}, ok=False)
    assert cp.returncode == 4, cp.stdout + cp.stderr
    assert "racket" in cp.stderr and "--accept-existing" in cp.stderr and "could not be started" in cp.stderr
    for blame in ("repair it", "delete it", "Errno", "does not compile"):
        assert blame not in cp.stderr, cp.stderr
    kept = (layer / "agentic.rkt").read_text()
    assert '#:root "../notes"' in kept
    # the same file, the next attempt, with a racket that is not an executable file at all
    plain = tmp_path / "plain-racket"
    plain.write_text("not a program\n")
    cp = al("author", layer, "--agent", "mock", "--mock", mock, "--accept-existing", env={"AL_RACKET": str(plain)}, ok=False)
    assert cp.returncode == 4 and "al: author: racket is missing: AL_RACKET names" in cp.stderr
    assert "--accept-existing" in cp.stderr and "repair it" not in cp.stderr and "Errno" not in cp.stderr
    assert (layer / "agentic.rkt").read_text() == kept


def test_accept_existing_takes_a_definition_that_compiles(app, tmp_path):
    al("init", app)
    layer = app.with_name("notes.al")
    (layer / "agentic.rkt").write_text(rendered_definition())
    before = (layer / "agentic.rkt").read_bytes()
    other = mock_file(tmp_path, {"agentic.rkt": "#lang al\n(something else)\n"}, output="the mock agent ran")
    cp = al("author", layer, "--agent", "mock", "--mock", other, "--accept-existing")
    assert "accepted (no agent was asked)" in cp.stdout and "the mock agent ran" not in cp.stdout
    assert (layer / "agentic.rkt").read_bytes() == before


def test_accept_existing_with_a_broken_definition_keeps_the_repair_hint(app, tmp_path):
    al("init", app)
    layer = app.with_name("notes.al")
    bad = '#lang al\n(agentic-layer #:name "notes" (no-such-form))\n'
    (layer / "agentic.rkt").write_text(bad)
    cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path, {"agentic.rkt": DEFINITION}),
            "--accept-existing", ok=False)
    assert cp.returncode == 1 and "does not compile" in cp.stderr
    assert "meta/commands/repair-layer.md" in cp.stderr and "delete it" in cp.stderr
    assert (layer / "agentic.rkt").read_text() == bad


def test_author_without_accept_existing_still_refuses_an_existing_definition(app, tmp_path):
    al("init", app)
    layer = app.with_name("notes.al")
    (layer / "agentic.rkt").write_text(rendered_definition())
    before = (layer / "agentic.rkt").read_bytes()
    cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path, {"agentic.rkt": DEFINITION}), ok=False)
    assert cp.returncode == 1 and "agentic.rkt already exists; it is never overwritten" in cp.stderr
    assert (layer / "agentic.rkt").read_bytes() == before


# --- what `al author` prints: the condensed view by default, the agent's event stream with --raw --------

FINISHED_STREAM = ROOT / "kit" / "dashboard" / "tests" / "data" / "job-streams" / "author-finished.jsonl"
FAKE_CLAUDE = """#!{python}
\"\"\"A stand-in for the claude CLI (tests): replays a recorded event stream and writes the definition.\"\"\"
import sys
sys.stdin.read()
with open({definition!r}) as source, open("agentic.rkt", "w") as target:
    target.write(source.read())
with open({stream!r}, "rb") as stream:
    for line in stream:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
"""


def fake_claude(tmp_path: Path) -> dict[str, str]:
    """A `claude` first on PATH that replays author-finished.jsonl and writes a definition that compiles."""
    definition = tmp_path / "definition.rkt"
    definition.write_text(rendered_definition())
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(FAKE_CLAUDE.format(python=sys.executable, definition=str(definition), stream=str(FINISHED_STREAM)))
    claude.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def test_author_prints_a_condensed_view_by_default(app, tmp_path, monkeypatch):
    monkeypatch.delenv("AL_CLAUDE", raising=False)
    al("init", app)
    layer = app.with_name("notes.al")
    out = al("author", layer, env=fake_claude(tmp_path)).stdout
    lines = out.splitlines()
    assert not any(line.startswith('{"type"') for line in lines), out[:2000]
    assert "fernhook-sentinel" not in out and "hook configuration" not in out
    assert "  · read " in out and "  ! Bash failed" in out and "  = 14 turns, 3m 04s, $0.42" in out
    assert out.count("### Decisions") == 1 and "UNVERIFIED: the tests may need network access" in out
    assert "Decisions:" in lines and "Left out and why:" in lines
    assert "agentic.rkt compiles; next: al gen" in out
    assert len(out) < 10_000


def test_author_raw_prints_the_event_stream(app, tmp_path, monkeypatch):
    monkeypatch.delenv("AL_CLAUDE", raising=False)
    al("init", app)
    layer = app.with_name("notes.al")
    out = al("author", layer, "--raw", env=fake_claude(tmp_path)).stdout
    lines = out.splitlines()
    position = 0
    for expected in FINISHED_STREAM.read_text().splitlines():
        position = lines.index(expected, position) + 1          # every line of the stream, in order
    assert "agentic.rkt compiles; next: al gen" in out
