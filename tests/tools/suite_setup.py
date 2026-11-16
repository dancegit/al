"""A small application with a hand-built ADW suite of its own, as a team builds one over time:
executable workflow scripts in adws/ that share a runtime package (adws/adw_modules/), an entry
script with subcommands, prompts in .claude/commands/ with schemas, a run journal outside git
(.work/adw/<id>/), a model-routing file, a wrapper, and a systemd unit and timer that run a watcher.

Everything is standard library only and runs offline: `agent.run_prompt` writes the prompt and a
canned output instead of calling an LLM. Written by the tests rather than kept as files in this
repository (like existing_setup.py)."""
from __future__ import annotations

import subprocess
from pathlib import Path

PEP723 = """#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""

FILES: dict[str, str] = {
    "orbit/__init__.py": "",
    "orbit/core.py": "def period(radius: float) -> float:\n    return radius ** 1.5\n",
    "tests/test_core.py": "from orbit.core import period\n\n\ndef test_period():\n    assert period(1.0) == 1.0\n",
    "pyproject.toml": "[project]\nname = \"orbit\"\nversion = \"0.1.0\"\n\n[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
    "README.md": "# Orbit\n\nOrbit computes the period of a circular orbit, one radius at a time.\n",
    ".gitignore": ".work/\n__pycache__/\n",
    # ---------------------------------------------------------------- the suite's runtime package
    "adws/adw_modules/__init__.py": "",
    "adws/adw_modules/state.py": '''"""The run journal of the orbit workflows: one folder per run under JOURNAL_ROOT, outside git."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

JOURNAL_ROOT = ".work/adw"


def run_folder(adw_id: str) -> Path:
    folder = Path(JOURNAL_ROOT) / adw_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_state(adw_id: str, data: dict) -> None:
    (run_folder(adw_id) / "state.json").write_text(json.dumps(data, indent=2) + "\\n")


def record(adw_id: str, kind: str, subject: str, **fields) -> dict:
    """Append one event to events.jsonl; each line carries the hash of the one before it."""
    path = run_folder(adw_id) / "events.jsonl"
    prev = ""
    if path.exists():
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        if lines:
            prev = json.loads(lines[-1]).get("hash", "")
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "subject": subject,
             **fields, "prev": prev}
    entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    with path.open("a") as handle:
        handle.write(json.dumps(entry) + "\\n")
    return entry
''',
    "adws/adw_modules/agent.py": '''"""Run one prompt of .claude/commands/ for a workflow step (offline: a canned answer)."""
from __future__ import annotations

import json
from pathlib import Path

from adw_modules.state import record, run_folder


def run_prompt(adw_id: str, step_dir: str, prompt_name: str, arguments: str) -> dict:
    name = prompt_name.lstrip("/")
    text = (Path(".claude/commands") / f"{name}.md").read_text()
    folder = run_folder(adw_id) / step_dir
    folder.mkdir(parents=True, exist_ok=True)
    record(adw_id, "agent.start", name, step=step_dir)
    (folder / "prompt.md").write_text(text.replace("$ARGUMENTS", arguments))
    output = {"prompt": name, "arguments": arguments, "result": "ok"}
    (folder / "output.json").write_text(json.dumps(output, indent=2) + "\\n")
    record(adw_id, "agent.end", name, step=step_dir,
           usage={"input_tokens": 1200, "output_tokens": 340}, cost_usd=0.02)
    return output
''',
    "adws/adw_modules/guards.py": '''"""Guards the orbit workflows run before they touch anything."""
from __future__ import annotations

from pathlib import Path


def guard_clean_tree() -> bool:
    """Refuse to start when a previous run left a lock behind."""
    return not Path(".work/adw/LOCK").exists()
''',
    # ---------------------------------------------------------------- the workflow scripts
    "adws/adw_orbit.py": PEP723 + '''"""The orbit workflows: one entry point for every one of them.

Usage: adws/adw_orbit.py COMMAND [ARGS]

Commands:
  plan REQUEST [--adw-id ID]                      plan a change from a one-line request
  build SPEC [--adw-id ID]                        build a planned change
  review ADW_ID                                   review the change of a run
  watch [--adw-id ID] [--resume ADW_ID] [--no-wait]  wait for the review queue, then review
  status [ADW_ID]                                 show the state of a run
"""
import secrets
import subprocess
import sys
from pathlib import Path

from adw_modules import state

HERE = Path(__file__).resolve().parent
SCRIPTS = {"plan": "adw_orbit_plan.py", "watch": "adw_orbit_watch.py"}


def option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else ""


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    if command in SCRIPTS:
        return subprocess.call([sys.executable, str(HERE / SCRIPTS[command]), *rest])
    adw_id = option(rest, "--adw-id") or (rest[0] if rest and not rest[0].startswith("-") and command != "build" else "")
    adw_id = adw_id or secrets.token_hex(4)
    if command in ("build", "review", "status"):
        state.record(adw_id, f"{command}.done", command, arguments=rest)
        print(f"{command}: {adw_id}")
        return 0
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
''',
    "adws/adw_orbit_plan.py": PEP723 + '''"""Plan a change to orbit from a one-line request.

Usage:
  adws/adw_orbit_plan.py REQUEST [--adw-id ID]
  adws/adw_orbit_plan.py --resume ADW_ID

Steps:
  1. (code) read the request
  2. (LLM) /orbit-plan writes the plan
  3. (LLM) /orbit-summarise condenses it
  4. (code) save state
"""
import argparse
import secrets
import sys

from adw_modules import agent, state


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="adw_orbit_plan")
    parser.add_argument("request", nargs="?", default="")
    parser.add_argument("--adw-id")
    parser.add_argument("--resume", metavar="ADW_ID")
    args = parser.parse_args(argv)
    adw_id = args.resume or args.adw_id or secrets.token_hex(4)
    state.record(adw_id, "step.start", "read-request")
    request = args.request.strip()
    state.record(adw_id, "step.end", "read-request", request=request)
    state.record(adw_id, "step.start", "plan")
    agent.run_prompt(adw_id, "plan", "/orbit-plan", request)
    state.record(adw_id, "step.end", "plan")
    state.save_state(adw_id, {"adw_id": adw_id, "request": request, "status": "planned"})
    print(f"planned: {request} ({adw_id})")


if __name__ == "__main__":
    main(sys.argv[1:])
    sys.exit(0)
''',
    "adws/adw_orbit_watch.py": PEP723 + '''"""Watch the review queue of an orbit change and review it once it is ready.

Usage:
  adws/adw_orbit_watch.py [--adw-id ID] [--resume ADW_ID] [--no-wait]

Exit codes:
  0   the review finished
  75  still waiting for the review queue; run again with --resume
"""
import argparse
import secrets
import sys
import time
from pathlib import Path

from adw_modules import agent, state

EXIT_WAITING = 75


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="adw_orbit_watch")
    parser.add_argument("--adw-id")
    parser.add_argument("--resume", metavar="ADW_ID")
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args(argv)
    adw_id = args.resume or args.adw_id or secrets.token_hex(4)
    checks = 1 if args.no_wait else 3
    for number in range(checks):
        ready = Path("orbit.ready").exists()
        state.record(adw_id, "watch.check", "review-queue", ready=ready, check=number + 1)
        if ready:
            break
        time.sleep(0.1)
    else:
        state.save_state(adw_id, {"adw_id": adw_id, "status": "waiting"})
        sys.exit(EXIT_WAITING)
    agent.run_prompt(adw_id, "round-review", "orbit-round-review", "the change of " + adw_id)
    state.save_state(adw_id, {"adw_id": adw_id, "status": "reviewed"})
    print(f"reviewed: {adw_id}")
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
''',
    "adws/adw_orbit_sync.sh": """#!/bin/sh
# Sync the orbit cache.
# Usage: adws/adw_orbit_sync.sh [--adw-id ID]
case "$1" in
  ""|--adw-id) ;;
  *) echo "unknown argument: $1" >&2; exit 3 ;;
esac
mkdir -p .work/cache
exit 0
""",
    "adws/adw_orbit_misc.py": """import pathlib

pathlib.Path("misc-ran.txt").write_text("the misc script ran\\n")
""",
    # ---------------------------------------------------------------- prompts and their schemas
    ".claude/commands/orbit-plan.md": """---
description: Plan a change to orbit
schema: orbit-plan.schema.json
output: json
effort: high
---

# Plan

Write a plan for: $ARGUMENTS
""",
    ".claude/commands/orbit-round-review.md": """---
description: Review one round of an orbit change
schema: orbit-review.schema.json
output: json
---

# Review

Review $ARGUMENTS and answer with the review schema.
""",
    ".claude/commands/orbit-notes.md": """---
description: Write release notes for orbit
---

# Notes

Write release notes for $ARGUMENTS.
""",
    "adws/schemas/orbit-plan.schema.json": '{"type": "object", "required": ["steps"], "properties": {"steps": {"type": "array"}}}\n',
    "adws/schemas/orbit-review.schema.json": '{"type": "object", "required": ["success"], "properties": {"success": {"type": "boolean"}}}\n',
    # ---------------------------------------------------------------- model routing, wrapper, units
    "adws/model-routing.json": """{
  "tiers": {"deep": "claude-opus-5-5", "quick": "claude-haiku-5-5"},
  "default": "quick",
  "steps": {"orbit-plan": "deep", "orbit-round-review": "deep"}
}
""",
    "adws/bin/orbit": '#!/bin/sh\nexec uv run adws/adw_orbit.py "$@"\n',
    "ops/systemd/orbit-watch.service": """[Unit]
Description=Watch the orbit review queue

[Service]
Type=oneshot
WorkingDirectory=%h/orbit
ExecStart=/usr/bin/env adws/bin/orbit watch --no-wait
""",
    "ops/systemd/orbit-watch.timer": """[Unit]
Description=Watch the orbit review queue every ten minutes

[Timer]
OnCalendar=*:0/10

[Install]
WantedBy=timers.target
""",
}

EXECUTABLE = ("adws/adw_orbit.py", "adws/adw_orbit_plan.py", "adws/adw_orbit_watch.py", "adws/adw_orbit_sync.sh",
              "adws/adw_orbit_misc.py", "adws/bin/orbit")


def _write(app: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for rel in EXECUTABLE:
        if (app / rel).is_file():
            (app / rel).chmod(0o755)
    run = lambda *a: subprocess.run(["git", "-C", str(app), *a], check=True, capture_output=True, timeout=30)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "orbit with its hand-built ADW suite")
    return app


def build(root: Path) -> Path:
    """The application at root/orbit, committed on `main`."""
    return _write(Path(root) / "orbit", FILES)


def build_variant(root: Path, drop: list[str], replace: dict[str, str]) -> Path:
    """A degraded copy at root/orbit: FILES without `drop`, with `replace` written over (or added)."""
    files = {rel: text for rel, text in FILES.items() if rel not in drop}
    files.update(replace)
    return _write(Path(root) / "orbit", files)
