from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from adw_modules import ir, state
from adw_modules.agents import run_agent
from adw_modules.envs import build
from adw_modules.errors import StepFailed


def setup_run(make_layer, overrides=None):
    layer_dir = make_layer({
        "commands": [
            {"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
            {"name": "classify", "template": "classify", "modelSet": "base", "output": "json"},
            {"name": "review", "template": "review", "modelSet": "base", "output": "json"},
            {"name": "text", "template": "text", "modelSet": "base", "output": "text"},
        ],
        "adws": [{"name": "demo", "agent": "mock-agent", "environment": ["worktree"]}],
        "environments": [{"id": "worktree", "kind": "worktree", "portsPerRun": 0}],
        **(overrides or {}),
    })
    layer = ir.load(layer_dir)
    run_state = state.ADWState("a1b2c3d4", "demo", prompt="Add feature")
    env = build(layer, run_state, ["worktree"])
    env.setup()
    return layer, run_state, env


def test_mock_script_precedence_and_file_substitution(make_layer):
    layer, run_state, env = setup_run(make_layer)
    generic = layer.layer_dir / "mock" / "build.json"
    specific = layer.layer_dir / "mock" / "demo" / "build.json"
    generic.parent.mkdir(parents=True)
    specific.parent.mkdir(parents=True)
    generic.write_text(json.dumps({"output": "wrong"}), encoding="utf-8")
    specific.write_text(json.dumps({"output": "ok ${prompt}", "files": {"new.txt": "${adw_id} $1"},
                                    "layer_files": {"specs/data.txt": "${class}"}, "delete": ["app.py"]}), encoding="utf-8")
    output = run_agent(layer, run_state, "build", "text", ["argument"], "base", env)
    assert output == "ok Add feature"
    assert (Path(run_state.worktree) / "new.txt").read_text() == "a1b2c3d4 argument"
    assert not (Path(run_state.worktree) / "app.py").exists()
    assert (layer.layer_dir / "specs/data.txt").read_text() == "chore"
    assert (layer.layer_dir / "agents/a1b2c3d4/build/raw.jsonl").read_text().startswith('{"script"')


def test_synthesized_outputs_and_json_extraction(make_layer):
    layer, run_state, env = setup_run(make_layer)
    plan = run_agent(layer, run_state, "plan", "plan", [], "base", env)
    assert Path(plan).is_file() and Path(plan).name == "plan-a1b2c3d4-plan.md"
    classified = run_agent(layer, run_state, "classify", "classify", [], "base", env)
    assert json.loads(classified)["class"] == "chore"
    reviewed = run_agent(layer, run_state, "review", "review", [], "base", env)
    assert json.loads(reviewed)["success"] is True
    script = layer.layer_dir / "mock/demo/review.json"
    script.parent.mkdir(parents=True)
    script.write_text('{"output":"```json\\n{\\\"success\\\":true,\\\"summary\\\":\\\"fine\\\",\\\"issues\\\":[]}\\n```"}', encoding="utf-8")
    assert json.loads(run_agent(layer, run_state, "review", "review", [], "base", env))["summary"] == "fine"


def test_bad_json_and_path_outputs_fail(make_layer):
    layer, run_state, env = setup_run(make_layer)
    script = layer.layer_dir / "mock/demo/classify.json"
    script.parent.mkdir(parents=True)
    script.write_text('{"output":"{\\"class\\":\\"unknown\\"}"}', encoding="utf-8")
    with pytest.raises(StepFailed, match="classify JSON"):
        run_agent(layer, run_state, "classify", "classify", [], "base", env)
    script = layer.layer_dir / "mock/demo/plan.json"
    script.write_text(json.dumps({"output": "missing.md"}), encoding="utf-8")
    with pytest.raises(StepFailed, match="does not name an existing file"):
        run_agent(layer, run_state, "plan", "plan", [], "base", env)


def test_mock_paths_cannot_escape_layer_or_worktree(make_layer):
    layer, run_state, env = setup_run(make_layer)
    script = layer.layer_dir / "mock/demo/text.json"
    script.parent.mkdir(parents=True)
    script.write_text(json.dumps({"files": {"../../escape": "bad"}}), encoding="utf-8")
    with pytest.raises(StepFailed, match="escapes"):
        run_agent(layer, run_state, "text", "text", [], "base", env)


def test_claude_stream_json_and_error(make_layer, monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "claude-call.txt"
    executable = bindir / "claude"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {record}\n"
        f"cat > {record}.stdin\n"
        f"echo \"CLAUDECODE=${{CLAUDECODE:-unset}} PORT=${{ADW_PORT_0:-unset}}\" >> {record}\n"
        "echo 'not json diagnostics'\n"
        "printf '%s\\n' '{\"type\":\"assistant\",\"message\":{}}'\n"
        "printf '%s\\n' '{\"type\":\"result\",\"result\":\"done\",\"is_error\":false}'\n",
        encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDECODE", "1")
    layer, run_state, env = setup_run(make_layer, {"agents": [{"id": "mock-agent", "kind": "claude-code",
        "status": "stable", "modelBase": "m-base", "modelHeavy": "m-heavy", "permissionMode": "acceptEdits",
        "safeMode": True, "maxTurns": 7, "timeout": 10}]})
    env.env["ADW_PORT_0"] = "9100"
    assert run_agent(layer, run_state, "text", "text", ["x"], "heavy", env) == "done"
    argv = record.read_text().splitlines()
    # the prompt travels on stdin (templates start with `---`, which the CLI would read as an option)
    assert argv[0] == "-p" and argv[1] == "--output-format"
    assert not any(arg.startswith("---") or arg.startswith("# ") for arg in argv)
    assert Path(f"{record}.stdin").read_text().startswith("# text")
    for flag, value in (("--output-format", "stream-json"), ("--model", "m-heavy"),
                        ("--permission-mode", "acceptEdits"), ("--max-turns", "7"), ("--add-dir", str(layer.layer_dir))):
        assert argv[argv.index(flag) + 1] == value
    assert "--verbose" in argv and "--safe-mode" in argv
    assert argv[-1] == "CLAUDECODE=unset PORT=9100"
    assert "not json" in (layer.layer_dir / "agents/a1b2c3d4/text/raw.jsonl").read_text()
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '{\"type\":\"result\",\"result\":\"bad\",\"is_error\":true}'\n", encoding="utf-8")
    with pytest.raises(StepFailed, match="is_error"):
        run_agent(layer, run_state, "text2", "text", [], "base", env)
    executable.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", encoding="utf-8")
    with pytest.raises(StepFailed, match="exited 3: boom"):
        run_agent(layer, run_state, "text3", "text", [], "base", env)


def test_custom_agent_and_timeout(make_layer):
    layer, run_state, env = setup_run(make_layer, {"agents": [{"id": "mock-agent", "kind": "custom",
        "status": "stable", "command": "cat", "modelBase": "", "modelHeavy": "", "timeout": 3}]})
    assert "# text" in run_agent(layer, run_state, "text", "text", [], "base", env)
    layer.ir["agents"][0].update(command="sleep 5", timeout=1)
    with pytest.raises(StepFailed, match="timed out"):
        run_agent(layer, run_state, "slow", "text", [], "base", env)


def test_dry_run_forces_mock_for_real_agents(make_layer):
    layer, run_state, env = setup_run(make_layer, {"agents": [{"id": "mock-agent", "kind": "claude-code",
        "status": "stable", "modelBase": "m", "modelHeavy": "h", "permissionMode": "acceptEdits",
        "safeMode": True, "maxTurns": 3, "timeout": 3}]})
    run_state.dry_run = True
    assert run_agent(layer, run_state, "text", "text", [], "base", env) == "mock: text ok"


def test_json_extraction_prefers_last_fenced_block_then_braces():
    from adw_modules.agents import extract_json

    text = 'x ```json\n{"a":1}\n``` y ```json\n{"a":2}\n```'
    assert extract_json(text, "s") == {"a": 2}
    assert extract_json('Verdict follows {"a": {"b": 3}} done', "s") == {"a": {"b": 3}}
    with pytest.raises(StepFailed, match="no JSON"):
        extract_json("nothing here", "s")


def test_a_step_can_name_its_own_agent(make_layer, tmp_path):
    """`(run NAME … #:agent ID)`: that step runs on the named agent, the others on the ADW's agent."""
    layer, run_state, env = setup_run(make_layer, {"agents": [
        {"id": "mock-agent", "kind": "mock", "status": "stable", "scriptDir": "mock", "timeout": 5},
        {"id": "other", "kind": "mock", "status": "stable", "scriptDir": "mock-other", "timeout": 5}]})
    other = Path(layer.layer_dir) / "mock-other"
    other.mkdir(exist_ok=True)
    (other / "text.json").write_text('{"output": "from the step agent"}')
    assert run_agent(layer, run_state, "text", "text", ["x"], "base", env, "other") == "from the step agent"
    assert run_agent(layer, run_state, "text", "text", ["x"], "base", env) != "from the step agent"


def test_extract_json_survives_a_code_fence_inside_a_string() -> None:
    """A reviewer quoted a fenced code block inside its description: the short match ended there."""
    from adw_modules.agents import extract_json

    answer = ('Here is my verdict.\n```json\n{\n  "success": true,\n  "summary": "ok",\n  "issues": [\n    {"severity": '
              '"tech_debt", "description": "only blocks tagged ```bash are read, not ```console"}\n  ]\n}\n```\n')
    verdict = extract_json(answer, "review-2")
    assert verdict["success"] is True and "```bash" in verdict["issues"][0]["description"]
    # a raw line break inside a string is accepted too
    assert extract_json('```json\n{"summary": "line one\nline two"}\n```', "review-1")["summary"] == "line one\nline two"
    # two objects in one answer: the last fenced one is still the answer
    assert extract_json('```json\n{"a": 1}\n```\nthen\n```json\n{"a": 2}\n```', "x") == {"a": 2}
