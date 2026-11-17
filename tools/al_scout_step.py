"""`al scout APP --into LAYER`: complete the facts of a layer from what the repository actually does.

1. `scout(APP)`, the deterministic facts (as `al scout --json`).
2. Each detected command that needs no network (all of them with `--network`) runs once, in a throwaway
   worktree of APP's HEAD under LAYER/trees/scout-<id> (removed afterwards), with a time limit; its exit
   code, duration and output (LAYER/agents/scout/<name>.log) are recorded as `runs`.
3. LAYER/scout.json gets the facts with the runs; LLM.txt is rewritten with the measured durations
   while it is still the untouched draft (or absent). A completed LLM.txt is kept.
4. An isolated agent (tools/al_tools.py `agent_isolation`) follows meta/commands/scout-codebase.md with
   APP, the draft and the facts: it reads the CI files for work classes and artefacts and may write
   only inside LAYER. Its event stream is condensed as `al author` condenses it (progress lines, then
   the report, through the dashboard kit's AgentJournal); `--raw` prints the stream itself, and a
   dashboard job runs `al scout … --into … --raw` and condenses it in its runner. With `--agent mock`
   that step is skipped, or `--scout-mock F` writes fixed files (the format of `al author --mock`).

APP is never changed: what only git changes there (HEAD, branch, index) is compared before and after.
Standard library only.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

import al_author
import al_tools

ROOT = Path(__file__).resolve().parent.parent
DRAFT_MARKER = "<!-- Draft produced by `al scout` from repository facts only."
SCOUT_TOOLS = ["Read", "Grep", "Glob"]


class ScoutError(Exception):
    pass


def result_of(code: int, output: str, timeout: int) -> str:
    if code == 0:
        return "ok"
    if code == 124 and output.rstrip().endswith(f"command timed out after {timeout}s"):
        return f"timed out after {timeout}s"
    return f"failed: exit {code}"


def verify_commands(al: Any, app: Path, layer: Path, commands: list[dict], timeout: int, network: bool,
                    say) -> list[dict[str, Any]]:
    """Run each command once in a throwaway worktree of APP; one row per command."""
    rows: list[dict[str, Any]] = []
    if not commands:
        return rows
    tree = layer / "trees" / f"scout-{secrets.token_hex(4)}"
    tree.parent.mkdir(parents=True, exist_ok=True)
    logs = layer / "agents" / "scout"
    logs.mkdir(parents=True, exist_ok=True)
    cp = al.run(al_tools.argv(["git", "-C", str(app), "worktree", "add", "--detach", str(tree), "HEAD"], layer), check=False)
    if cp.returncode != 0:
        raise ScoutError(f"cannot create the scout worktree: {cp.stderr.strip()}")
    try:
        say(f"scout: running {len(commands)} detected command(s) once in a throwaway worktree ({timeout}s each at most)")
        for c in commands:
            row: dict[str, Any] = {"name": c["name"], "shell": c["shell"], "network": c["network"]}
            if c.get("network") == "yes" and not network:
                row.update(exit=None, seconds=None, result="not run: needs network")
                say(f"  skip  {c['name']}  not run: needs network (--network runs it)")
                rows.append(row)
                continue
            started = time.monotonic()
            code, output = al._run_group(["sh", "-c", c["shell"]], tree, dict(os.environ), timeout)
            seconds = round(time.monotonic() - started, 1)
            slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", c["name"])
            (logs / f"{slug}.log").write_text(output, encoding="utf-8")
            row.update(exit=code, seconds=seconds, result=result_of(code, output, timeout),
                       log=f"agents/scout/{slug}.log")
            say(f"  {'ok  ' if code == 0 else 'FAIL'}  {c['name']}  {row['result']}  {seconds}s")
            rows.append(row)
    finally:
        al.run(al_tools.argv(["git", "-C", str(app), "worktree", "remove", "--force", str(tree)], layer), check=False)
        shutil.rmtree(tree, ignore_errors=True)
        al.run(al_tools.argv(["git", "-C", str(app), "worktree", "prune"], layer), check=False)
    return rows


def scout_prompt(app: Path, layer: Path, root: Path = ROOT) -> str:
    command = (root / "meta" / "commands" / "scout-codebase.md").read_text(encoding="utf-8")
    return (f"Follow the instructions of meta/commands/scout-codebase.md, given in full below, with these values:\n\n"
            f"- APP_DIR ($1) = {app}\n- DRAFT ($2) = {layer}/LLM.txt\n- FACTS ($3) = {layer}/scout.json\n"
            f"- TEMPLATE = {root}/meta/LLM.txt.template.md\n\n"
            f"You may write only inside {layer}.\n\n--- meta/commands/scout-codebase.md ---\n\n{command}")


def scout_isolation(app: Path, layer: Path) -> al_tools.Isolation:
    return al_tools.agent_isolation(layer, layer, [*SCOUT_TOOLS, f"Bash(git -C {app}:*)"])


def complete(al: Any, app: Path, layer: Path, agent: str = "claude-code", mock: Path | None = None,
             timeout: int = 600, network: bool = False, say=print, raw: bool = False, kit: Any = None) -> int:
    app, layer = Path(app).resolve(), Path(layer).resolve()
    if not (app / ".git").exists():
        raise ScoutError(f"{app} is not a git checkout")
    if layer == app:
        raise ScoutError("--into names the application itself: give the layer directory")
    layer.mkdir(parents=True, exist_ok=True)
    before = al.app_fingerprint(app)
    facts = al.scout(app)
    facts["runs"] = verify_commands(al, app, layer, facts["commands"], timeout, network, say)
    al.write_scout_json(layer, facts)
    llm = layer / "LLM.txt"
    if not llm.exists() or DRAFT_MARKER in llm.read_text(encoding="utf-8", errors="replace"):
        llm.write_text(al.render_llm_txt(facts), encoding="utf-8")
        say(f"scout: wrote {layer / 'scout.json'} and the draft {llm} with the measured commands")
    else:
        say(f"scout: wrote {layer / 'scout.json'}; {llm} was completed already, so it is kept as it is")
    if al.app_fingerprint(app) != before:
        raise ScoutError(f"THE APPLICATION CHECKOUT CHANGED during `al scout --into` — {app}")
    if agent == "mock":
        if mock is None:
            say("scout: agent completion skipped (mock agent)")
        else:
            output = al_author.run_mock(layer, mock, {"name": app.name, "root": os.path.relpath(app, layer)})
            if output:
                say(output)
    elif agent == "claude-code":
        isolation = scout_isolation(app, layer)
        al_author.run_agent_cli(
            layer, lambda claude: [claude, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode",
                                   "default", *isolation.argv_extra],
            isolation, scout_prompt(app, layer), say, raw=raw, kit=kit)
    else:
        raise ScoutError(f"unknown agent {agent}")
    if al.app_fingerprint(app) != before:
        raise ScoutError(f"THE APPLICATION CHECKOUT CHANGED while the scout agent ran — {app}")
    return 0
