"""A small application with a realistic agent setup of its own, as a team builds one over time:
rule files for several tools (one per directory too), a library of Claude Code commands, two
workflow scripts in adws/, a CI workflow that runs an agent, Claude Code settings with a hook, an
MCP configuration and a specs/ folder.

Written by the tests rather than kept as files in this repository: a CLAUDE.md or AGENTS.md lying
in al's own tree would be read as a rule file of al itself by any agent working here."""
from __future__ import annotations

import subprocess
from pathlib import Path

FILES: dict[str, str] = {
    "larder/__init__.py": "",
    "larder/stock.py": "def low(items, threshold=2):\n    return [name for name, count in items.items() if count < threshold]\n",
    "larder/api/routes.py": "from larder.stock import low\n\n\ndef low_stock(items):\n    return {'low': low(items)}\n",
    "tests/test_stock.py": "from larder.stock import low\n\n\ndef test_low():\n    assert low({'rice': 1, 'oats': 5}) == ['rice']\n",
    "pyproject.toml": "[project]\nname = \"larder\"\nversion = \"0.1.0\"\n\n[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
    "README.md": "# Larder\n\nLarder keeps track of what is in a shared pantry and tells you what is running low, "
                 "so the weekly shop is one list instead of five guesses.\n",
    # ---------------------------------------------------------------- rule files
    "CLAUDE.md": """# Working on Larder

Read README.md first.

## Rules

- Run `uv run pytest -q` before you say a change works.
- Never commit directly; leave committing to the developer who asked for the change.
- Do not add dependencies without asking first.
- Keep functions in `larder/stock.py` pure: no I/O.
- Write commit messages in the imperative mood.
""",
    "AGENTS.md": """# Agents

1. Tests live in `tests/`, one `test_<module>.py` per module.
2. Always type-annotate new public functions.
3. Never edit `pyproject.toml` by hand to change versions.
""",
    "larder/api/AGENTS.md": """# The API package

- Every route returns a dict; never return a bare list.
- Do not rename existing routes: clients depend on them.
""",
    ".cursorrules": "Prefer small functions. Never use print for logging; use the logging module.\n",
    ".github/copilot-instructions.md": "# Copilot\n\nUse pytest style asserts. Avoid mocking the stock module.\n",
    # ---------------------------------------------------------------- prompt library
    ".claude/commands/review-pr.md": """---
description: Review a pull request against our rules
argument-hint: [pr-number]
allowed-tools: Bash(gh pr view:*), Read
---

# Review a pull request

Review pull request $ARGUMENTS of Larder.

- Check every changed function in `larder/` has a test in `tests/`.
- Report problems as a list, most serious first.
""",
    ".claude/commands/commit.md": """---
description: Write our kind of commit message
---

Write a commit message for the staged changes: imperative mood, at most 60 characters, no trailing period.
""",
    ".claude/commands/frontend/component.md": """---
description: Scaffold a UI component
argument-hint: <name> <props>
model: claude-sonnet-5-5
---

# Component {{name}}

Create the component $1 with the props $2 ({{props}}).
Look at what is staged first: !`git status --short`
Follow the layout in @docs/ui.md.
""",
    ".claude/agents/test-writer.md": "---\nname: test-writer\ndescription: Writes pytest tests for a module\n---\n\nYou write tests.\n",
    ".claude/skills/release/SKILL.md": "---\nname: release\ndescription: Cut a release of Larder\n---\n\n# Release\n\nBump, tag, push.\n",
    ".claude/settings.json": """{
  "permissions": {"allow": ["Bash(uv run pytest:*)"], "deny": ["Bash(git push:*)"]},
  "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "uv run ruff format"}]}]}
}
""",
    ".mcp.json": '{"mcpServers": {"pantry-db": {"command": "uv", "args": ["run", "tools/db_mcp.py"]}}}\n',
    # ---------------------------------------------------------------- workflow scripts
    "adws/adw_plan_build.py": '''#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Plan and build a change from a one-line request: our first workflow script."""
import os
import sys
from pathlib import Path

request = " ".join(sys.argv[1:])
Path("WORKLOG.md").write_text(f"request: {request}\\nrun: {os.environ.get('ADW_ID', '?')}\\n")
print(f"planned and built: {request}")
sys.exit(3 if "please fail" in request else 0)
''',
    "adws/adw_test.py": '''#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Run the tests and summarise them for the agent."""
import subprocess
import sys

sys.exit(subprocess.call(["uv", "run", "pytest", "-q"]))
''',
    "adws/adw_modules/__init__.py": "",
    "adws/adw_modules/agent.py": "def prompt(text):\n    return text\n",
    # ---------------------------------------------------------------- CI
    ".github/workflows/claude.yml": """name: claude
on:
  issue_comment:
    types: [created]
  issues:
    types: [opened]
jobs:
  claude:
    runs-on: ubuntu-latest
    steps:
      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
""",
    ".github/workflows/ci.yml": "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: uv run pytest -q\n",
    # ---------------------------------------------------------------- plans and docs
    "specs/feature-low-stock-alert.md": "# Feature: low stock alert\n\nTell the household when rice runs low.\n",
    "ai_docs/uv.md": "# uv notes\n\nHow we use uv here.\n",
}


def build(root: Path) -> Path:
    """The application at root/larder, committed on `main`."""
    app = root / "larder"
    for rel, text in FILES.items():
        path = app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for rel in ("adws/adw_plan_build.py", "adws/adw_test.py"):
        (app / rel).chmod(0o755)
    run = lambda *a: subprocess.run(["git", "-C", str(app), *a], check=True, capture_output=True, timeout=30)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "larder with its agent setup")
    return app
