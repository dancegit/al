"""A workspace of four invented repositories side by side, built in code, for the Repos view:

  pantry  an in-repo layer: agentic/agentic.rkt with a generated agentic-layer.json (the fixture IR)
  quill   rule files and a prompt (CLAUDE.md, AGENTS.md, .claude/commands/review.md): setup
  comet   a hand-built ADW suite (adws/adw_plan.py, adws/adw_build.py importing adws/adw_modules/): setup
  fern    a README and one source file: none

and `fake_al`, a standard-library stand-in for the al tool that records its argv and prints, sleeps,
writes and exits as told (no Racket, no LLM)."""
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
            line_sleep: float = 0, writes: dict[str, dict[str, str]] | None = None) -> Path:
    """An executable stand-in for `al` at `path`: appends its argv as one JSON line to `<path>.calls`,
    writes `writes[command]` (path → text), prints `lines` (each followed by `line_sleep` seconds) and
    `out`, sleeps `sleep` seconds and exits `exit`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    behaviour = {"sleep": sleep, "exit": exit, "out": out, "lines": lines or [], "line_sleep": line_sleep,
                 "writes": writes or {}}
    path.write_text(FAKE_AL.format(python=sys.executable, behaviour=repr(behaviour)))
    path.chmod(0o755)
    return path


def calls(path: Path) -> list[list[str]]:
    record = Path(str(path) + ".calls")
    if not record.is_file():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
