"""`al author LAYER`: have the layer definition written (GUIDE Step 4 as a command).

It hands meta/commands/author-layer.md to the agent CLI with the layer's LLM.txt, exactly as a person
does by hand, then compiles the result. It never overwrites an existing agentic.rkt and writes nothing
into the application checkout. `--agent mock --mock FILE` writes fixed files instead (tests, no LLM):

    {"files": {"agentic.rkt": "#lang al … #:root \\"${root}\\" …"}, "output": "text the agent printed"}

`${name}` (the application's directory name) and `${root}` (the application relative to the layer)
are substituted; every path must stay inside the layer.

Before any agent (or the mock) runs, the prerequisites are checked (racket, and the agent's CLI) through
tools/al_tools.py: a missing one stops it at once with exit 4. A compiler that cannot be started is never
blamed on the file. `--accept-existing` (a dashboard retry) takes an agentic.rkt that is already there
when it compiles, and asks no agent. Standard library only.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import al_tools   # tools/ is on sys.path: tools/al loads this module from there

ROOT = Path(__file__).resolve().parent.parent
REPAIR_HINT = ("the file is kept: repair it with meta/commands/repair-layer.md (the compiler error above names the clause) "
               "or delete it and run `al author` again")
INCLUDE_SENTENCE = ('The layer folder has an adopted/ folder (written by `al adopt`): put (include "adopted/codebase.rkt") '
                    'inside (codebase …) and (include "adopted/layer.rkt") at the top level.')


class AuthorError(Exception):
    pass


def default_preferences(app_rel: str, adopted: bool) -> str:
    text = ("tasks from the command line; started by hand; agents work in a git worktree; a human reviews; no forge. "
            f"The codebase root, relative to the layer folder, is {app_rel}.")
    return text + (" " + INCLUDE_SENTENCE if adopted else "")


def author_prompt(layer: Path, preferences: str, root: Path = ROOT) -> str:
    return (f"Follow {root}/meta/commands/author-layer.md with $1 = {layer}/LLM.txt  $2 = {layer}/agentic.rkt  "
            f"$3 = {preferences}. The language reference is {root}/docs/AL-DSL.llms.txt; "
            f"compile with {root}/tools/al compile {layer}.")


def claude_argv(root: Path, layer: Path, claude: str = "claude") -> list[str]:
    """The agent CLI's argv: a fixed list, no shell; the prompt goes on stdin and the run's cwd is the layer.
    `claude` is the program: run_claude passes the path tools/al_tools.py found."""
    return [claude, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
            "--add-dir", str(root / "docs"), "--add-dir", str(root / "meta")]


def run_claude(layer: Path, prompt: str, say) -> None:
    found = al_tools.find("claude", layer)
    if not found.path:
        raise al_tools.ToolMissing([found])
    try:
        proc = subprocess.Popen(claude_argv(ROOT, layer, found.path), cwd=layer, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=False)
    except OSError as exc:
        raise AuthorError(f"{al_tools.missing_sentence(found)} ({found.path} could not be started: "
                          f"{exc.strerror or exc}; or use --agent mock --mock FILE)") from exc
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(prompt)
    proc.stdin.close()
    result = ""
    for line in proc.stdout:
        say(line.rstrip("\n"))
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = str(event.get("result") or "")
    code = proc.wait()
    if result:
        say(result)
    if code != 0:
        raise AuthorError(f"the agent exited {code}")


def run_mock(layer: Path, mock: Path, values: dict[str, str]) -> str:
    """Write the mock's files into the layer (every path inside it) and return its output text."""
    try:
        spec: Any = json.loads(Path(mock).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthorError(f"cannot read the mock file {mock}: {exc}") from exc
    files = spec.get("files") if isinstance(spec, dict) else None
    if not isinstance(files, dict):
        raise AuthorError(f"{mock}: expected an object with \"files\"")
    base = layer.resolve()
    targets = {}
    for rel, text in files.items():
        target = (base / str(rel)).resolve()
        if target == base or base not in target.parents:
            raise AuthorError(f"mock path {rel} is outside the layer {layer}")
        targets[target] = str(text)
    for target, text in targets.items():
        for key, value in values.items():
            text = text.replace("${" + key + "}", value)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return str(spec.get("output") or "")


def never_compiled(layer: Path) -> str:
    return (f"the file is kept and was not checked: it was never compiled; when the compiler is found, "
            f"run al author {layer} --accept-existing")


def author(al: Any, layer: Path, agent: str, mock: Path | None, preferences: str | None, say=print,
           accept_existing: bool = False) -> int:
    layer = layer.resolve()
    definition = layer / "agentic.rkt"
    if definition.exists() and not accept_existing:
        raise AuthorError(f"{definition} already exists; it is never overwritten")
    if not (layer / "LLM.txt").is_file():
        raise AuthorError(f"{layer} has no LLM.txt: create the layer with `al init APP` first")
    app = layer.with_name(layer.name.removesuffix(".al")) if layer.name.endswith(".al") else None
    if app is not None and not (app / ".git").exists():
        app = None
    if app is None and not preferences:
        raise AuthorError(f"cannot tell where the application is (no git checkout {layer.name.removesuffix('.al')} "
                          "beside the layer): say it with --preferences, e.g. \"The codebase root, relative to the "
                          "layer folder, is ../myapp.\"")
    app_rel = os.path.relpath(app, layer) if app is not None else ""
    adopted = (layer / "adopted").is_dir()
    prefs = preferences or default_preferences(app_rel, adopted)
    tools = al.tools_module()
    try:   # before any agent or mock runs: a missing compiler or agent CLI costs no agent time
        tools.need(("racket",) + tools.agent_bin(agent), layer)
    except tools.ToolMissing as exc:
        if definition.exists():
            exc.hint = never_compiled(layer)
        raise
    if definition.exists():   # accept_existing: take it when it compiles, ask no agent
        try:
            al.compile_layer(layer)
        except al.CompilerUnavailable as exc:
            raise al.CompilerUnavailable(f"{exc}\n{never_compiled(layer)}") from exc
        except tools.ToolMissing as exc:
            exc.hint = never_compiled(layer)
            raise
        except Exception as exc:  # noqa: BLE001 — AlError of tools/al: the compiler's own text
            raise AuthorError(f"{exc}\n{REPAIR_HINT}") from exc
        say(f"agentic.rkt already exists and compiles; accepted (no agent was asked); next: al gen {layer}")
        return 0
    if agent == "mock":
        if mock is None:
            raise AuthorError("--agent mock needs --mock FILE")
        name = app.name if app is not None else layer.name.removesuffix(".al")
        output = run_mock(layer, mock, {"name": name, "root": app_rel})
        if output:
            say(output)
    elif agent == "claude-code":
        run_claude(layer, author_prompt(layer, prefs), say)
    else:
        raise AuthorError(f"unknown agent {agent}")
    if not definition.is_file():
        raise AuthorError(f"the agent did not write {definition}")
    try:
        al.compile_layer(layer)
    except al.CompilerUnavailable as exc:   # the file was never checked: not its fault
        raise al.CompilerUnavailable(f"{exc}\n{never_compiled(layer)}") from exc
    except al.tools_module().ToolMissing as exc:
        exc.hint = never_compiled(layer)
        raise
    except Exception as exc:  # noqa: BLE001 — AlError of tools/al: the compiler's own text
        raise AuthorError(f"{exc}\n{REPAIR_HINT}") from exc
    say(f"agentic.rkt compiles; next: al gen {layer}")
    return 0
