"""`al create APP` and `al adopt-all APP`: a layer for APP in one command, the same sequence the dashboard's
Create and Adopt actions run (kit/dashboard/dashboard.py builds its steps from the same argv; a test in
tests/tools/test_create.py keeps the two equal; the dashboard adds `--raw` to scout and author, whose agent
streams its runner condenses, while the command line shows their condensed view):

  init    al init APP [--in-repo]                       the layer beside APP, the draft LLM.txt, scout.json
  scout   al scout APP --into LAYER [--agent A] [--raw] measured commands, forge facts, the scout agent
  adopt   al adopt LAYER --app APP                      the existing agent setup (a clear no-op when there is none)
  author  al author LAYER [agent flags] [--raw] [--choice K=V]
                                                        agentic.rkt from preferences built from the facts
  gen     al gen LAYER
  check   al check LAYER
  result  al result LAYER --json                        the result card (on the command line: as text)

With `--in-repo` the layer is put inside the repository instead of beside it: APP/agentic, as
`al init APP --in-repo` creates it, committed with the code. Only the init step and the layer's path
differ; every other step is the same.

Each step is a `tools/al` subprocess; the first that fails stops the sequence with its exit code and
keeps what the steps before it wrote. `adopt-all` refuses a repository with nothing to adopt (use
`al create`). `al create APP --choices [--json]` prints the choices `al author` would make from the
facts (value, options, the fact behind it) and writes nothing. `al result LAYER [--json]` is the card:
the layer, its forge, workflows, what was adopted and left behind, the commands that failed or did not
run, and what to do next. Standard library only.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import al_author

SEQUENCE_NAMES = ("init", "scout", "adopt", "author", "gen", "check", "result")
TIMEOUTS = {"init": 300, "scout": 1800, "adopt": 600, "author": 3600, "gen": 600, "check": 600, "result": 120}
CARD_LINES = 12
# The directory of `al init APP --in-repo` (its --dir default). The dashboard's IN_REPO_DIR mirrors it; a
# test keeps the two equal.
IN_REPO_DIR = "agentic"


class CreateError(Exception):
    pass


def agent_of(flags: list[str]) -> str | None:
    """The value of --agent in `flags`, else None."""
    return flags[flags.index("--agent") + 1] if "--agent" in flags[:-1] else None


def choice_args(choices: dict[str, str] | None) -> list[str]:
    """`--choice KEY=VALUE` for each choice, sorted by key."""
    return [arg for key in sorted(choices or {}) for arg in ("--choice", f"{key}={(choices or {})[key]}")]


def layer_path(app: Path, in_repo: bool = False) -> Path:
    """Where the layer of APP goes: APP/agentic inside the repository, else the sibling APP.al."""
    return app / IN_REPO_DIR if in_repo else app.with_name(app.name + ".al")


def sequence(al: str, app: str, layer: str, agent_flags: list[str], choices: dict[str, str] | None = None,
             scout_flags: list[str] | None = None, raw: bool = False,
             in_repo: bool = False) -> list[tuple[str, list[str], int]]:
    """(name, argv, timeout) of every step, in order. `agent_flags` are al author's (--agent A [--mock F]);
    the scout step gets the same --agent, `scout_flags` (e.g. --scout-mock F) after it. With `raw` (the
    dashboard's steps), scout ends with --raw and author has --raw after the agent flags, before the choices.
    With `in_repo`, init gets --in-repo (and `layer` is APP/agentic); every other argv is unchanged."""
    agent = agent_of(agent_flags)
    argvs = {
        "init": [al, "init", app, *(["--in-repo"] if in_repo else [])],
        "scout": [al, "scout", app, "--into", layer, *(["--agent", agent] if agent else []), *(scout_flags or []),
                  *(["--raw"] if raw else [])],
        "adopt": [al, "adopt", layer, "--app", app],
        "author": [al, "author", layer, *agent_flags, *(["--raw"] if raw else []), *choice_args(choices)],
        "gen": [al, "gen", layer],
        "check": [al, "check", layer],
        "result": [al, "result", layer, "--json"],
    }
    return [(name, argvs[name], TIMEOUTS[name]) for name in SEQUENCE_NAMES]


def detect_choices(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """KEY → {value, options, multi, source, fact}: what `al author` would choose from these facts."""
    out = {}
    for key, d in al_author.decide(facts).items():
        fact = d["because"] if d["source"] == "fact" else f"default: {d['because']}"
        out[key] = {"value": d["value"], "options": d["options"], "multi": d["multi"], "source": d["source"], "fact": fact}
    return out


def nothing_to_adopt(facts: dict[str, Any]) -> bool:
    return not facts.get("existing_agent_setup") and not facts.get("existing_adw_suite")


def create(al: Any, app: Path, adopt_all: bool, agent: str = "claude-code", mock: Path | None = None,
           scout_mock: Path | None = None, choices: dict[str, str] | None = None, say=print,
           in_repo: bool = False) -> int:
    """Run the sequence through tools/al subprocesses; the exit code of the first step that fails, else 0.
    With `in_repo` the layer is APP/agentic (`al init APP --in-repo`), else the sibling APP.al."""
    app = Path(app).resolve()
    if not (app / ".git").exists():
        raise CreateError(f"{app} is not a git checkout")
    layer = layer_path(app, in_repo)
    if (layer / "agentic.rkt").exists():
        raise CreateError(f"{layer / 'agentic.rkt'} already exists; it is never overwritten (run al gen {layer})")
    if adopt_all and nothing_to_adopt(al.scout(app)):
        raise CreateError(f"nothing to adopt in {app}: no existing agent setup found; use al create {app}")
    if agent == "mock" and mock is None:
        raise CreateError("--agent mock needs --mock FILE (the author's files)")
    agent_flags = ["--agent", agent, *(["--mock", str(Path(mock).resolve())] if mock else [])]
    scout_flags = ["--scout-mock", str(Path(scout_mock).resolve())] if scout_mock else []
    tool = str(Path(al.ROOT) / "tools" / "al")
    for name, argv, _timeout in sequence(tool, str(app), str(layer), agent_flags, choices, scout_flags,
                                         in_repo=in_repo):
        if name == "result":
            argv = [a for a in argv if a != "--json"]   # the command line shows the text card
        say(f"== {name}")
        sys.stdout.flush()
        code = subprocess.call(argv)
        if code != 0:
            say(f"== {name} failed (exit {code}): stopped here; what the steps before it wrote is kept")
            return code
    return 0


# ------------------------------------------------------------------------------------------ result card

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def result_card(layer: Path) -> dict[str, Any]:
    """What a person needs to know after create or adopt, from the layer's own files."""
    layer = Path(layer).resolve()
    ir = _read_json(layer / "agentic-layer.json") or {}
    forge = ir.get("forge") or {"kind": "none"}
    facts = _read_json(layer / "scout.json") or {}
    codebase = _text(layer / "adopted" / "codebase.rkt")
    adopted_layer = _text(layer / "adopted" / "layer.rkt")
    report = _text(layer / "adopted" / "REPORT.md")
    not_adopted = report.split("## Not adopted", 1)[1] if "## Not adopted" in report else ""
    left = [m.group(1) for m in re.finditer(r"^- `([^`]+)`", not_adopted, re.M)]
    suite = [m.group(1) for m in re.finditer(r"^\(adw (\S+)", adopted_layer, re.M)]
    unverified = [{"name": r.get("name"), "shell": r.get("shell"), "result": r.get("result")}
                  for r in facts.get("runs") or [] if r.get("exit") != 0]
    token = str(forge.get("tokenEnv") or "")
    nxt = []
    if forge.get("kind") not in (None, "none") and token:
        nxt.append(f"set {token} in the environment (or {layer}/local.env) so the layer can talk to the forge")
    if not ir:
        nxt.append(f"al gen {layer}")
    nxt.append(f"al verify {layer}")
    if report:
        nxt.append(f"read {layer}/adopted/REPORT.md (\"For a person to decide\")")
    return {
        "layer": str(layer),
        "forge": {"kind": forge.get("kind", "none"), "host": forge.get("host", ""), "repo": forge.get("repo", ""),
                  "clause": forge_clause(forge)},
        "workflows": [a["name"] for a in ir.get("adws") or []],
        "adopted": {"rules": re.findall(r'^\(conditional-doc "([^"]+)"', codebase, re.M),
                    "commands": re.findall(r"^\(command (\S+)", adopted_layer, re.M),
                    "suite": suite},
        "left_behind": left,
        "unverified": unverified,
        "next": nxt,
    }


def forge_clause(forge: dict[str, Any]) -> str:
    kind = forge.get("kind") or "none"
    if kind == "none":
        return "(forge none)"
    parts = [f"#:host {json.dumps(forge['host'])}"] if kind == "forgejo" and forge.get("host") else []
    parts += [f"#:repo {json.dumps(forge['repo'])}"] if forge.get("repo") else []
    if kind == "github" and forge.get("host") not in (None, "", "github.com"):
        parts.append(f"#:host {json.dumps(forge['host'])}")
    return f"(forge {kind} {' '.join(parts)})".replace(" )", ")")


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def render_card(card: dict[str, Any]) -> str:
    """The card as at most CARD_LINES lines of text."""
    f = card["forge"]
    workflows = card["workflows"]
    a = card["adopted"]
    shown = ", ".join(workflows[:6]) + (f", … ({len(workflows)} in all)" if len(workflows) > 6 else "")
    lines = [f"layer:       {card['layer']}",
             f"forge:       {f['clause']}",
             f"workflows:   {shown or 'none'}",
             f"adopted:     {len(a['rules'])} rule file(s), {len(a['commands'])} command(s), "
             f"{len(a['suite'])} suite workflow(s)",
             f"left behind: {len(card['left_behind'])}" + (" (adopted/REPORT.md, \"Not adopted\")" if card["left_behind"] else "")]
    unverified = card["unverified"]
    lines.append("unverified:  " + (", ".join(f"{u['name']} ({u['result']})" for u in unverified[:3])
                                    + (f", … ({len(unverified)} in all)" if len(unverified) > 3 else "") if unverified else "none"))
    for index, step in enumerate(card["next"][:CARD_LINES - len(lines)]):
        lines.append(("next:        " if index == 0 else "             ") + step)
    return "\n".join(lines[:CARD_LINES])
