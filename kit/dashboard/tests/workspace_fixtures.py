"""A workspace of four invented repositories side by side, built in code, for the Repos view:

  pantry  an in-repo layer: agentic/agentic.rkt with a generated agentic-layer.json (the fixture IR)
  quill   rule files and a prompt (CLAUDE.md, AGENTS.md, .claude/commands/review.md): setup
  comet   a hand-built ADW suite (adws/adw_plan.py, adws/adw_build.py importing adws/adw_modules/): setup
  fern    a README and one source file: none

and `fake_al`, a standard-library stand-in for the al tool that records its argv and prints, replays a
recorded agent stream (data/job-streams/, through `job_stream`), sleeps, writes and exits as told (no
Racket, no LLM). Its `doctor` (a job's prerequisite check) answers `ok` at
once unless `fail` names it."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import fixtures

IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]

FILES: dict[str, dict[str, str]] = {
    "pantry": {
        "README.md": "# Pantry\n\nKeeps track of what is in the cupboard.\n",
        "pantry.py": "ITEMS = []\n",
        "agentic/agentic.rkt": "#lang al\n;; fixture: the IR in agentic-layer.json is hand-written\n",
    },
    "quill": {
        "README.md": "# Quill\n\nA small note-taking tool.\n",
        "CLAUDE.md": "# Rules\n\n- Never commit generated files.\n- Always run the tests before a commit.\n",
        "AGENTS.md": "# Agents\n\n- Keep functions short.\n",
        ".claude/commands/review.md": "---\ndescription: Review the current change\nargument-hint: [pr-number]\n---\n\n"
                                      "Review the change $ARGUMENTS against the rules in CLAUDE.md.\n",
        "quill.py": "def main():\n    return 0\n",
    },
    "comet": {
        "README.md": "# Comet\n\nA tiny service with its own agent workflows.\n",
        "adws/adw_modules/__init__.py": "",
        "adws/adw_modules/agent.py": '"""Runs a prompt through the agent CLI."""\n\n\ndef run(prompt):\n    return prompt\n',
        "adws/adw_plan.py": '#!/usr/bin/env python3\n"""Plan a change with the agent."""\nimport sys\n\n'
                            "from adw_modules import agent\n\n\ndef main():\n    agent.run(' '.join(sys.argv[1:]))\n\n\n"
                            "if __name__ == '__main__':\n    main()\n",
        "adws/adw_build.py": '#!/usr/bin/env python3\n"""Build the planned change."""\nimport sys\n\n'
                             "from adw_modules import agent\n\n\ndef main():\n    agent.run(' '.join(sys.argv[1:]))\n\n\n"
                             "if __name__ == '__main__':\n    main()\n",
        "comet.py": "print('comet')\n",
    },
    "fern": {
        "README.md": "# Fern\n\nGrows slowly.\n",
        "fern.py": "LEAVES = 3\n",
    },
}


def git(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0, f"git {' '.join(args)}: {cp.stderr}"
    return cp.stdout


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, *IDENTITY, "commit", "-qm", message)


def make_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = Path(root) / name
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    commit(repo, f"{name}: first commit")
    return repo


def build(root: Path) -> dict[str, Path]:
    """The four repositories in `root`, each on `main` with one commit and a clean tree."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    out = {name: make_repo(root, name, files) for name, files in FILES.items()}
    layer = out["pantry"] / "agentic"
    ir = dict(fixtures.IR, name="pantry", codebase={**fixtures.IR["codebase"], "root": ".."})
    (layer / "agentic-layer.json").write_text(json.dumps(ir, indent=2))
    (out["pantry"] / ".gitignore").write_text("agentic/agentic-layer.json\nagentic/agents/\n")
    commit(out["pantry"], "pantry: ignore the generated IR")
    return out


FAKE_AL = '''#!{python}
"""A stand-in for the al tool (tests): records its argv, then does what BEHAVIOUR says."""
import json, os, sys, time
HERE = os.path.abspath(__file__)
BEHAVIOUR = {behaviour}
with open(HERE + ".calls", "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
command = sys.argv[1] if len(sys.argv) > 1 else ""
if command in BEHAVIOUR.get("streams", {{}}):
    with open(BEHAVIOUR["streams"][command], "rb") as stream:
        for line in stream:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            time.sleep(BEHAVIOUR.get("line_sleep", 0))
time.sleep(BEHAVIOUR.get("hold", {{}}).get(command, 0))
if command in BEHAVIOUR.get("fail", {{}}):
    code, text = BEHAVIOUR["fail"][command]
    if text and command == "doctor":
        print(text, flush=True)
    elif text:
        print(text, file=sys.stderr, flush=True)
    sys.exit(code)
if command in BEHAVIOUR.get("streams", {{}}):
    sys.exit(BEHAVIOUR.get("exit", 0))
if command == "doctor":
    print("ok")
    sys.exit(0)
for path, text in BEHAVIOUR.get("writes", {{}}).get(command, {{}}).items():
    path = path.replace("{{arg2}}", sys.argv[2] if len(sys.argv) > 2 else "")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
for line in BEHAVIOUR.get("lines", []):
    print(line, flush=True)
    time.sleep(BEHAVIOUR.get("line_sleep", 0))
if BEHAVIOUR.get("out"):
    print(BEHAVIOUR["out"], flush=True)
if command == "explain" and "--md" in sys.argv:
    with open(sys.argv[sys.argv.index("--md") + 1], "w") as handle:
        handle.write("# explained\\n")
time.sleep(BEHAVIOUR.get("sleep", 0))
sys.exit(BEHAVIOUR.get("exit", 0))
'''


def fake_al(path: Path, *, sleep: float = 0, exit: int = 0, out: str = "", lines: list[str] | None = None,  # noqa: A002
            line_sleep: float = 0, writes: dict[str, dict[str, str]] | None = None,
            fail: dict[str, tuple[int, str]] | None = None, streams: dict[str, Path] | None = None,
            hold: dict[str, float] | None = None) -> Path:
    """An executable stand-in for `al` at `path`: appends its argv as one JSON line to `<path>.calls`,
    writes `writes[command]` (path → text), prints `lines` (each followed by `line_sleep` seconds) and
    `out`, sleeps `sleep` seconds and exits `exit`. `streams[command]`: that subcommand prints the file's
    lines byte for byte (each followed by `line_sleep` seconds) and nothing else, then fails or exits; `hold[command]`: that subcommand
    then sleeps that many seconds (which keeps its step running). `fail[command] = (code, text)`: that subcommand (argv[1]) then prints
    `text`, when there is one, on stderr (on stdout for `doctor`, like al doctor --for) and exits `code`.
    `doctor` otherwise prints ok and exits 0 at once. It can be rewritten between runs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    behaviour = {"sleep": sleep, "exit": exit, "out": out, "lines": lines or [], "line_sleep": line_sleep,
                 "writes": writes or {}, "fail": {k: list(v) for k, v in (fail or {}).items()},
                 "streams": {k: str(v) for k, v in (streams or {}).items()}, "hold": dict(hold or {})}
    path.write_text(FAKE_AL.format(python=sys.executable, behaviour=repr(behaviour)))
    path.chmod(0o755)
    return path


STREAMS = Path(__file__).resolve().parent / "data" / "job-streams"


def job_stream(name: str, workspace: Path) -> Path:
    """The recorded agent stream `name` (author-running, author-finished, author-failed) with its invented
    paths made real: /tmp/ws becomes `workspace`, /tmp/home the home directory (which every page must show
    as ~). Written beside the workspace; → its path."""
    text = (STREAMS / f"{name}.jsonl").read_text(encoding="utf-8")
    text = text.replace("/tmp/ws", str(Path(workspace).resolve())).replace("/tmp/home", str(Path.home()))
    target = Path(workspace).parent / "streams" / f"{name}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def calls(path: Path, preflight: bool = False) -> list[list[str]]:
    """Every argv the fake was called with; without `preflight`, the `doctor` checks a job runs before
    its steps are left out (so a list is the job's steps)."""
    record = Path(str(path) + ".calls")
    if not record.is_file():
        return []
    found = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
    return found if preflight else [c for c in found if c[:1] != ["doctor"]]
