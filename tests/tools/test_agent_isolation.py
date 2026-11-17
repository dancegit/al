"""Every agent al starts runs without the account's personal settings, hooks, instructions and plugins, and
may write only inside the layer: the real `al author` and `al scout --into` run a fake `claude` program
(set through AL_CLAUDE) that records its argv and what its environment points at. AL_AGENT_PERSONAL=1
brings the personal setup back."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import AL, LAYER_TEMPLATE, ROOT, sh, status

sys.path.insert(0, str(ROOT / "tools"))
import al_tools  # noqa: E402

DEFINITION = LAYER_TEMPLATE.format(root="../notes", placement="(layer sibling)")

FAKE_CLAUDE = '''#!{python}
"""A stand-in for the claude CLI: records argv and its configuration folder, prints one result event."""
import json, os, sys
RECORD = {record!r}
os.makedirs(RECORD, exist_ok=True)
prompt = sys.stdin.read()
config = os.environ.get("CLAUDE_CONFIG_DIR")
with open(os.path.join(RECORD, "argv.json"), "w") as handle:
    json.dump(sys.argv[1:], handle)
with open(os.path.join(RECORD, "env.json"), "w") as handle:
    json.dump({{"CLAUDE_CONFIG_DIR": config,
               "listing": sorted(os.listdir(config)) if config and os.path.isdir(config) else None}}, handle)
if "author-layer.md" in prompt:
    with open("agentic.rkt", "w") as handle:
        handle.write({definition!r})
print(json.dumps({{"type": "result", "subtype": "success", "result": "done"}}), flush=True)
'''


@pytest.fixture
def world(tmp_path: Path):
    """A fake claude, a fake home with a personal Claude Code setup, and the environment al runs in."""
    record = tmp_path / "record"
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable, record=str(record), definition=DEFINITION))
    fake.chmod(0o755)
    racket = al_tools.find("racket").path
    uv_cache = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True).stdout.strip()
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "echo personal hook"}]}]}}))
    (home / ".claude" / "CLAUDE.md").write_text("# Personal instructions\n\n- Always answer in verse.\n")
    (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {}}\n')
    env = {k: v for k, v in os.environ.items() if k not in ("AL_APP_ROOT", "CLAUDE_CONFIG_DIR", "AL_AGENT_PERSONAL")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), AL_CLAUDE=str(fake), AL_RACKET=racket,
               **({"UV_CACHE_DIR": uv_cache} if uv_cache else {}))
    return {"record": record, "home": home, "env": env}


def run_al(world, *args, ok: bool = True):
    cp = sh([AL, *args], env=world["env"])
    if ok:
        assert cp.returncode == 0, f"al {' '.join(map(str, args))}: {cp.returncode}\n{cp.stdout}\n{cp.stderr}"
    return cp


def recorded(world) -> tuple[list[str], dict]:
    record = world["record"]
    return json.loads((record / "argv.json").read_text()), json.loads((record / "env.json").read_text())


def allowed(argv: list[str]) -> list[str]:
    return argv[argv.index("--allowedTools") + 1].split(",")


def assert_isolated(world, argv: list[str], env: dict, layer: Path) -> None:
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in argv
    assert "--add-dir" not in argv                                    # nothing outside the layer is opened up
    writes = [t for t in allowed(argv) if t.startswith(("Edit", "Write"))]
    assert sorted(writes) == sorted([f"Edit(/{layer.resolve()}/**)", f"Write(/{layer.resolve()}/**)"])
    config = env["CLAUDE_CONFIG_DIR"]
    assert config and Path(config) != world["home"] / ".claude"
    assert env["listing"] == [".credentials.json"]                     # no settings, hooks, CLAUDE.md or plugins
    assert not Path(config).exists()                                   # the clean folder is removed after the run


def test_author_agent_runs_without_personal_settings(app, world):
    run_al(world, "init", app)
    layer = app.with_name("notes.al")
    cp = run_al(world, "author", layer)
    assert "agentic.rkt compiles" in cp.stdout
    argv, env = recorded(world)
    assert_isolated(world, argv, env, layer)
    assert f"Bash({ROOT}/tools/al compile:*)" in allowed(argv)
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert status(app) == ""


def test_personal_setup_on_request(app, world):
    run_al(world, "init", app)
    layer = app.with_name("notes.al")
    (layer / "local.env").write_text("AL_AGENT_PERSONAL=1\n")
    run_al(world, "author", layer)
    argv, env = recorded(world)
    assert "--setting-sources" not in argv and env["CLAUDE_CONFIG_DIR"] is None
    writes = [t for t in allowed(argv) if t.startswith(("Edit", "Write"))]
    assert writes == [f"Edit(/{layer.resolve()}/**)", f"Write(/{layer.resolve()}/**)"]   # the write rules stay


def test_scout_agent_is_isolated_too(app, world):
    layer = app.with_name("notes.al")
    run_al(world, "init", app)
    run_al(world, "scout", app, "--into", layer, "--agent", "claude-code", "--timeout", "60")
    argv, env = recorded(world)
    assert_isolated(world, argv, env, layer)
    assert sorted(t for t in allowed(argv) if not t.startswith(("Edit", "Write"))) == sorted(
        ["Read", "Grep", "Glob", f"Bash(git -C {app.resolve()}:*)"])
    assert status(app) == "" and "scout-" not in subprocess.run(
        ["git", "-C", str(app), "worktree", "list"], capture_output=True, text=True).stdout
