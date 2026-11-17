"""AL_AGENT_ISOLATED=1 (set by `al improve`): the runtime starts `claude` without the account's personal
settings, hooks, CLAUDE.md, plugins and MCP servers, in a clean configuration folder under the step's
directory that holds only a link to the credentials file. Without the variable nothing changes."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from adw_modules import agents, models

AGENT = {"permissionMode": "acceptEdits", "maxTurns": 40, "allowedTools": [], "safeMode": False}
CHOICE = models.Choice(tier="base", tier_from="command", model="", model_from="agent", effort="default")


def fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"hooks": {"Stop": []}}\n')
    (home / ".claude" / "CLAUDE.md").write_text("personal instructions\n")
    (home / ".claude" / ".credentials.json").write_text("{}\n")
    monkeypatch.setenv("HOME", str(home))
    return home


def test_claude_argv_isolated_when_asked(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("AL_AGENT_ISOLATED", "1")
    layer = SimpleNamespace(layer_dir=tmp_path / "layer")
    argv = agents.claude_argv(layer, AGENT, CHOICE, None)
    assert argv[argv.index("--setting-sources") + 1] == "project" and "--strict-mcp-config" in argv
    step = tmp_path / "layer" / "agents" / "run1" / "plan"
    step.mkdir(parents=True)
    env = agents._claude_env(SimpleNamespace(env={}), step)
    config = Path(env["CLAUDE_CONFIG_DIR"])
    assert config.parent == step and config != home / ".claude"
    assert sorted(p.name for p in config.iterdir()) == [".credentials.json"]
    assert (config / ".credentials.json").resolve() == (home / ".claude" / ".credentials.json").resolve()
    again = agents._claude_env(SimpleNamespace(env={}), step)        # a second call of the same step: clean again
    assert sorted(p.name for p in Path(again["CLAUDE_CONFIG_DIR"]).iterdir()) == [".credentials.json"]


def test_claude_argv_unchanged_by_default(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    monkeypatch.delenv("AL_AGENT_ISOLATED", raising=False)
    layer = SimpleNamespace(layer_dir=tmp_path / "layer")
    argv = agents.claude_argv(layer, AGENT, CHOICE, None)
    assert "--setting-sources" not in argv and "--strict-mcp-config" not in argv
    step = tmp_path / "step"
    step.mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert "CLAUDE_CONFIG_DIR" not in agents._claude_env(SimpleNamespace(env={}), step)
    assert not (step / "claude-config").exists()
