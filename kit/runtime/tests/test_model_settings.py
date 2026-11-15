"""The layer's local model setting (agents/settings/models.json): its format, the order of the
sources of a tier's model id (--model > AL_MODELS > local setting > layer > built-in default), and
what a run records about them. Git is real; the agent is a fake `claude` that records its argv."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from adw_modules import events, ir, models
from adw_modules.errors import LayerError
from adw_modules.runner import main

ADW_ID = "c0d9e700"
TIERS = [{"tier": "base", "model": "claude-opus-5-5", "effort": "default"},
         {"tier": "heavy", "model": "claude-opus-5-5", "effort": "default"},
         {"tier": "fast", "model": "claude-haiku-5-5", "effort": "default"}]
CLAUDE = {"id": "claude", "kind": "claude-code", "status": "stable", "models": TIERS, "permissionMode": "acceptEdits",
          "safeMode": True, "allowedTools": [], "maxTurns": 5, "timeout": 20}
TEXT = {"template": "build", "output": "text", "description": "", "file": "", "focus": [], "fabric": [],
        "relevant": [], "validation": []}


def _layer(make_layer, agents=None) -> Path:
    agents = agents or [CLAUDE]
    return make_layer({
        "agents": agents,
        "commands": [{"name": "plan", "modelSet": "heavy", **TEXT}, {"name": "build", "modelSet": "base", **TEXT}],
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27950,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "adws": [{"name": "demo", "agent": agents[0]["id"], "review": "human", "environment": ["wt"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [],
                  "steps": [{"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"}]}],
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "budget": None,
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
    })


def _state(layer: Path, adw_id: str = ADW_ID) -> dict:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text())


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (models.ENV_TIER, models.ENV_MODELS, models.ENV_MAX_COST, "FAKE_SUBTYPE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A `claude` on PATH that records its argv; it fails when $FAKE_SUBTYPE is not `success`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "claude-argv.jsonl"
    exe = bindir / "claude"
    exe.write_text("#!/usr/bin/env python3\nimport json, os, sys\nsys.stdin.read()\n"
                   f"open({str(record)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                   "print(json.dumps({'type': 'result', 'subtype': os.environ.get('FAKE_SUBTYPE', 'success'),"
                   " 'is_error': os.environ.get('FAKE_SUBTYPE', 'success') != 'success', 'result': 'done',"
                   " 'total_cost_usd': 0.1, 'num_turns': 1}))\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def calls() -> list[list[str]]:
        return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
    return calls


# ------------------------------------------------------------------------------------ the file

def test_settings_round_trip_in_the_canonical_format(tmp_path):
    mapping = {"heavy": "claude-fable-5-2", "base": "my-model"}
    path = models.write_settings(tmp_path, mapping)
    assert path == tmp_path / models.SETTINGS_FILE
    assert models.read_settings(tmp_path) == mapping
    text = path.read_text(encoding="utf-8")
    assert text == models.render_settings(mapping)
    assert text == ('{\n  "models": {\n    "base": "my-model",\n    "heavy": "claude-fable-5-2"\n  },\n'
                    '  "version": 1\n}\n')
    assert models.read_settings(tmp_path / "elsewhere") == {}


def test_write_is_atomic_and_an_empty_mapping_removes_the_file(tmp_path, monkeypatch):
    path = models.write_settings(tmp_path, {"heavy": "claude-fable-5-2"})
    directory = path.parent

    def broken(*_args, **_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(models.os, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        models.write_settings(tmp_path, {"heavy": "claude-haiku-5-5"})
    monkeypatch.undo()
    assert not list(directory.glob(".models.*.tmp"))
    assert models.read_settings(tmp_path) == {"heavy": "claude-fable-5-2"}   # the old file is intact
    assert models.write_settings(tmp_path, {}) == path
    assert not path.exists()
    models.write_settings(tmp_path, {})   # removing a missing file is fine
    assert not list(directory.iterdir())


def test_check_setting_rejects_unknown_tiers_and_bad_ids():
    known = ["base", "heavy"]
    for tier, model in (("nope", "claude-fable-5-2"), ("Heavy", "claude-fable-5-2"), ("heavy", ""),
                        ("heavy", "a b"), ("heavy", "x\n"), ("heavy", "x\ty"), ("heavy", "x" * 201)):
        with pytest.raises(ValueError):
            models.check_setting(tier, model, known)
    with pytest.raises(ValueError, match="no agent of this layer has a tier `nope`; declared: base, heavy"):
        models.check_setting("nope", "claude-fable-5-2", known)
    note = models.check_setting("heavy", "my-custom-model", known)
    assert note and "my-custom-model is not a known model id" in note and "passed to the agent as is" in note
    assert models.check_setting("heavy", "claude-fable-5-2", known) is None


@pytest.mark.parametrize("text", ["{not json", "[]", '{"models": {"heavy": 3}}', '{"models": []}',
                                  '{"models": {"heavy": "a b"}}'])
def test_unreadable_settings_are_a_layer_error(tmp_path, text):
    path = tmp_path / models.SETTINGS_FILE
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LayerError) as caught:
        models.read_settings(tmp_path)
    assert str(path) in str(caught.value) and "al models LAYER --reset" in str(caught.value)


def test_model_order_flag_env_local_layer_default(make_layer):
    settings = {"heavy": "s", "fast": "s2"}
    over = models.overrides(None, ["heavy=f"], None, {"AL_MODELS": "heavy=e,base=e2"}, settings=settings)
    assert over["models"] == {"heavy": "f", "fast": "s2", "base": "e2"}
    assert over["models_from"] == {"heavy": "--model", "base": "AL_MODELS", "fast": "local setting"}
    assert over["from"]["models"] == "--model and AL_MODELS"
    only_local = models.overrides(None, None, None, {}, settings=settings)
    assert "models" not in only_local["from"] and only_local["models_from"] == {"heavy": "local setting",
                                                                               "fast": "local setting"}
    other = dict(CLAUDE, models=[TIERS[0], {"tier": "heavy", "model": "claude-fable-5-2", "effort": "high"}, TIERS[2]])
    layer = ir.load(_layer(make_layer, agents=[other]))
    assert models.resolve_tiers(layer, "claude", over) == {
        "base": {"model": "e2", "source": "AL_MODELS"}, "heavy": {"model": "f", "source": "--model"},
        "fast": {"model": "s2", "source": "local setting"}}
    assert models.resolve_tiers(layer, "claude", models.overrides(None, None, None, {})) == {
        "base": {"model": "claude-opus-5-5", "source": "built-in default"},
        "heavy": {"model": "claude-fable-5-2", "source": "layer"},
        "fast": {"model": "claude-haiku-5-5", "source": "layer"}}


def test_choose_marks_local_settings_as_local_and_old_states_as_override():
    over = models.overrides(None, None, None, {}, settings={"heavy": "claude-fable-5-2"})
    choice = models.choose(CLAUDE, "heavy", "command", over)
    assert (choice.model, choice.model_from) == ("claude-fable-5-2", "local")
    assert "local setting" in choice.why()
    old = {"tier": "", "models": {"heavy": "claude-fable-5-2"}, "max_cost": None, "from": {"models": "AL_MODELS"}}
    assert models.choose(CLAUDE, "heavy", "command", old).model_from == "override"
    env = models.overrides(None, None, None, {"AL_MODELS": "heavy=x"}, settings={"heavy": "claude-fable-5-2"})
    assert models.choose(CLAUDE, "heavy", "command", env).model_from == "override"
    assert models.choose(CLAUDE, "base", "command", over).model_from == "agent"


def test_settings_for_undeclared_tiers_are_ignored_with_a_note(make_layer):
    layer_dir = _layer(make_layer)
    models.write_settings(layer_dir, {"heavy": "claude-fable-5-2", "turbo": "x"})
    settings, notes = models.local_settings(ir.load(layer_dir))
    assert settings == {"heavy": "claude-fable-5-2"}
    assert notes == ["local setting for tier `turbo` ignored: no agent declares it"]


# ------------------------------------------------------------------------------------ runs

def test_a_run_records_each_tier_s_model_and_source(make_layer, fake_claude):
    layer_dir = _layer(make_layer)
    models.write_settings(layer_dir, {"heavy": "claude-fable-5-2"})
    assert main("demo", layer_dir, ["Add a thing", "--adw-id", ADW_ID]) == 0
    data = _state(layer_dir)
    assert data["models"] == {"base": {"model": "claude-opus-5-5", "source": "built-in default"},
                              "heavy": {"model": "claude-fable-5-2", "source": "local setting"},
                              "fast": {"model": "claude-haiku-5-5", "source": "layer"}}
    start = next(e for e in events.read(layer_dir / "agents" / ADW_ID / "events.jsonl") if e["type"] == "run.start")
    assert start["models"]["heavy"] == {"model": "claude-fable-5-2", "source": "local setting"}
    (call,) = data["agent_calls"]
    assert (call["tier"], call["model"], call["model_from"]) == ("heavy", "claude-fable-5-2", "local")
    assert _flag(fake_claude()[0], "--model") == "claude-fable-5-2"


def test_flags_and_environment_beat_the_local_setting(make_layer, fake_claude, monkeypatch):
    layer_dir = _layer(make_layer)
    models.write_settings(layer_dir, {"heavy": "claude-fable-5-2"})
    monkeypatch.setenv("AL_MODELS", "heavy=e")
    assert main("demo", layer_dir, ["x", "--adw-id", "c0d9e701"]) == 0
    assert _state(layer_dir, "c0d9e701")["models"]["heavy"] == {"model": "e", "source": "AL_MODELS"}
    assert main("demo", layer_dir, ["x", "--adw-id", "c0d9e702", "--model", "heavy=f"]) == 0
    assert _state(layer_dir, "c0d9e702")["models"]["heavy"] == {"model": "f", "source": "--model"}
    assert [_flag(a, "--model") for a in fake_claude()] == ["e", "f"]


def test_a_resumed_run_keeps_the_models_it_started_with(make_layer, fake_claude, monkeypatch):
    layer_dir = _layer(make_layer)
    models.write_settings(layer_dir, {"heavy": "claude-fable-5-2"})
    monkeypatch.setenv("FAKE_SUBTYPE", "error_during_execution")
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID, "--keep-worktree"]) == 1
    before = _state(layer_dir)["models"]
    models.write_settings(layer_dir, {"heavy": "claude-haiku-5-5"})
    monkeypatch.delenv("FAKE_SUBTYPE")
    assert main("demo", layer_dir, ["--resume", "--adw-id", ADW_ID]) == 0
    assert _state(layer_dir)["models"] == before
    assert [_flag(a, "--model") for a in fake_claude()] == ["claude-fable-5-2", "claude-fable-5-2"]


def test_dry_run_summary_prints_the_tiers_line(make_layer, capsys):
    layer_dir = _layer(make_layer)
    models.write_settings(layer_dir, {"heavy": "claude-fable-5-2"})
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID, "--dry-run"]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("tiers:"))
    assert line == ("tiers:     base=claude-opus-5-5 (built-in default); fast=claude-haiku-5-5 (layer); "
                    "heavy=claude-fable-5-2 (local setting)")


def test_a_bad_settings_file_stops_the_start_with_a_usage_error(make_layer, capsys):
    layer_dir = _layer(make_layer)
    path = layer_dir / models.SETTINGS_FILE
    path.parent.mkdir(parents=True)
    path.write_text("{oops", encoding="utf-8")
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID, "--dry-run"]) == 2
    assert "al models LAYER --reset" in capsys.readouterr().err
