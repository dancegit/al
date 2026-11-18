"""The dashboard voice helper's agent call (adw_modules/helper.py, `adws/dispatch.py --helper`): claude-code
with every tool disabled in an empty directory, the tier and the local setting as for runs, the mock's
rules, codex and custom refused, and a call log without the prompt."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from adw_modules import helper, ir, models

RUNTIME = Path(__file__).resolve().parents[1]
VOICE = {"id": "kokoro", "kind": "kokoro", "status": "stable", "sttModel": "base.en", "ttsVoice": "af_heart",
         "dashboardHelper": True, "listenMode": "hold", "helperAgent": "", "helperModelSet": "base",
         "interrupt": True, "maxUtteranceSeconds": 30}
CLAUDE = {"id": "claude", "kind": "claude-code", "status": "stable", "permissionMode": "acceptEdits", "maxTurns": 600,
          "timeout": 900, "safeMode": True, "allowedTools": ["Read", "Edit", "Bash"],
          "models": [{"tier": "base", "model": "claude-opus-5-5", "effort": "default"},
                     {"tier": "heavy", "model": "claude-fable-5-2", "effort": "high"}]}
MOCK = {"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}
CODEX = {"id": "codex", "kind": "codex", "status": "stable", "sandbox": "workspace-write", "timeout": 900,
         "models": [{"tier": "base", "model": "gpt-5.4-mini", "effort": "default"}]}
CUSTOM = {"id": "own", "kind": "custom", "status": "stable", "command": "cat", "modelBase": "", "modelHeavy": "",
          "timeout": 30}
RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0123,
                     "num_turns": 1, "result": '{"say": "Two runs are going."}'})
PROMPT = "You help a person.\n<person-said>the secret plan is blue</person-said>\n### Latest\nwhat is running?\n"


def _layer(make_layer, agents, **voice):
    return make_layer({"agents": agents, "voice": {**VOICE, **voice}})


class FakeRun:
    def __init__(self, code=0, out=RESULT + "\n", err=""):
        self.calls = []
        self.code, self.out, self.err = code, out, err

    def __call__(self, argv, *, cwd, env, stdin_text, timeout):
        self.calls.append({"argv": list(argv), "cwd": Path(cwd), "listing": sorted(Path(cwd).iterdir()),
                           "env": dict(env), "stdin": stdin_text, "timeout": timeout})
        return self.code, self.out, self.err


def test_claude_argv_disables_every_tool():
    choice = models.choose(CLAUDE, "heavy", "voice", None)
    argv = helper.helper_argv(CLAUDE, choice)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--model") + 1] == "claude-fable-5-2"
    assert argv[argv.index("--effort") + 1] == "high" and "--safe-mode" in argv
    for never in ("--allowedTools", "--add-dir", "bypassPermissions", "acceptEdits", "Bash"):
        assert never not in argv
    plain = helper.helper_argv({**CLAUDE, "safeMode": False}, models.choose(CLAUDE, "base", "voice", None))
    assert "--effort" not in plain and "--safe-mode" not in plain


def test_helper_runs_in_an_empty_directory(make_layer, app_repo, monkeypatch):
    layer_dir = _layer(make_layer, [CLAUDE])
    monkeypatch.setenv("AL_DASHBOARD_TOKEN", "dashboard-secret")
    monkeypatch.setenv("AL_VOICE_TOKEN", "voice-secret")
    monkeypatch.setenv("CLAUDECODE", "1")
    run = FakeRun()
    result = helper.reply(ir.load(layer_dir), PROMPT, run=run)
    assert result == {"status": "ok", "reply": '{"say": "Two runs are going."}', "cost": 0.0123,
                      "model": "claude-opus-5-5"}
    call = run.calls[0]
    assert call["listing"] == [] and not call["cwd"].exists()           # empty while it ran, gone afterwards
    for place in (layer_dir, app_repo, Path.cwd()):
        assert not call["cwd"].resolve().is_relative_to(place.resolve())
    assert not {"AL_DASHBOARD_TOKEN", "AL_VOICE_TOKEN", "CLAUDECODE"} & set(call["env"])
    assert call["stdin"] == PROMPT and PROMPT not in " ".join(call["argv"])
    assert call["timeout"] == helper.TIMEOUT


def test_tier_and_local_setting(make_layer):
    layer_dir = _layer(make_layer, [CLAUDE], helperModelSet="heavy")
    run = FakeRun()
    assert helper.reply(ir.load(layer_dir), PROMPT, run=run)["model"] == "claude-fable-5-2"
    argv = run.calls[-1]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-fable-5-2" and "--effort" in argv
    models.write_settings(layer_dir, {"heavy": "claude-sonnet-5-5"})
    assert helper.reply(ir.load(layer_dir), PROMPT, run=run)["model"] == "claude-sonnet-5-5"
    assert "claude-sonnet-5-5" in run.calls[-1]["argv"]
    # an IR compiled before the helper existed (same layer directory): the defaults, first agent and tier base
    old = make_layer({"agents": [CLAUDE], "voice": {k: v for k, v in VOICE.items() if not k.startswith(("helper", "dash"))}})
    assert "helperModelSet" not in json.loads((old / "agentic-layer.json").read_text())["voice"]
    assert helper.reply(ir.load(old), PROMPT, run=run)["model"] == "claude-opus-5-5"   # the setting is for heavy only
    models.write_settings(old, {"base": "claude-haiku-5-5"})
    assert helper.reply(ir.load(old), PROMPT, run=run)["model"] == "claude-haiku-5-5"


def _mock(layer_dir: Path, data) -> None:
    (layer_dir / "mock").mkdir(exist_ok=True)
    (layer_dir / "mock" / "helper.json").write_text(json.dumps(data), encoding="utf-8")


def test_mock_rules_and_default(make_layer):
    layer_dir = _layer(make_layer, [CLAUDE, MOCK], helperAgent="mock")
    _mock(layer_dir, {"rules": [{"when": "secret", "reply": {"say": "never: an earlier block matched"}},
                                {"when": "WHAT IS RUN", "reply": {"say": "Nothing runs.", "tool": "list_runs",
                                                                  "args": {}}},
                                {"when": "broken", "reply": "not json at all"}],
                      "default": {"say": "Say that again?"}})
    layer = ir.load(layer_dir)
    result = helper.reply(layer, PROMPT)
    assert result["status"] == "ok" and result["model"] == "mock" and result["cost"] is None
    assert json.loads(result["reply"]) == {"say": "Nothing runs.", "tool": "list_runs", "args": {}}
    assert helper.reply(layer, "### Latest\nhello")["reply"] == json.dumps({"say": "Say that again?"})
    assert helper.reply(layer, "### Latest\nbroken please")["reply"] == "not json at all"
    _mock(layer_dir, {"rules": []})
    assert helper.reply(layer, "### Latest\nhello") == {
        "status": "failed", "error": "no mock helper rule matches and the script has no default"}
    (layer_dir / "mock" / "helper.json").unlink()
    assert "has no script" in helper.reply(layer, PROMPT)["error"]


def test_mock_rejects_unknown_keys(make_layer):
    layer_dir = _layer(make_layer, [MOCK])
    layer = ir.load(layer_dir)
    _mock(layer_dir, {"rules": [], "default": {"say": "x"}, "shell": "rm -rf /"})
    result = helper.reply(layer, PROMPT)
    assert result["status"] == "failed" and "unknown keys ['shell']" in result["error"]
    _mock(layer_dir, {"rules": [{"when": "x", "reply": {"say": "x"}, "run": "ls"}]})
    assert "exactly the keys when and reply" in helper.reply(layer, PROMPT)["error"]
    _mock(layer_dir, {"rules": [{"when": "(", "reply": {"say": "x"}}]})
    assert "not a regular expression" in helper.reply(layer, PROMPT)["error"]
    _mock(layer_dir, ["not", "an", "object"])
    assert "must be a JSON object" in helper.reply(layer, PROMPT)["error"]


def test_codex_and_custom_are_unavailable(make_layer):
    for agents, voice, kind in (([CODEX, CLAUDE], {}, "codex"), ([CLAUDE, CUSTOM], {"helperAgent": "own"}, "custom")):
        run = FakeRun()
        result = helper.reply(ir.load(_layer(make_layer, agents, **voice)), PROMPT, run=run)
        assert result["status"] == "unavailable" and not run.calls
        assert f"is {kind}, so the panel will speak and listen but cannot answer" in result["error"]
        assert "needs an agent that runs with no tools (claude-code or mock)" in result["error"]
    off = helper.reply(ir.load(_layer(make_layer, [MOCK], dashboardHelper=False)), PROMPT)
    assert off["status"] == "unavailable" and "#:dashboard-helper #f" in off["error"]
    none = helper.reply(ir.load(make_layer({"agents": [MOCK], "voice": None})), PROMPT)
    assert none == {"status": "unavailable", "error": "this layer declares no (voice …)"}


def test_dispatch_helper_json(make_layer):
    layer_dir = _layer(make_layer, [MOCK])
    _mock(layer_dir, {"rules": [{"when": "search box", "reply": {"say": "Shall I prepare it for approval?"}}],
                      "default": {"say": "Pardon?"}})
    adws = layer_dir / "adws"
    shutil.copytree(RUNTIME / "adw_modules", adws / "adw_modules", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(RUNTIME / "dispatch.py", adws / "dispatch.py")

    def call(data: bytes):
        return subprocess.run([sys.executable, str(adws / "dispatch.py"), "--helper", "--json"], input=data,
                              capture_output=True, timeout=60, cwd=str(layer_dir))

    done = call(b"### Latest\nstart a plan for adding a search box")
    lines = done.stdout.decode().splitlines()
    assert done.returncode == 0 and len(lines) == 1, done.stderr
    assert json.loads(lines[0]) == {"status": "ok", "reply": json.dumps({"say": "Shall I prepare it for approval?"}),
                                    "cost": None, "model": "mock"}
    big = call(b"x" * (64 * 1024 + 1))
    assert big.returncode == 2
    assert json.loads(big.stdout) == {"status": "invalid", "error": "the prompt is over 65536 bytes"}
    assert json.loads(call(b"\xff\xfe").stdout)["status"] == "invalid"


def test_call_log_has_no_prompt_text(make_layer):
    layer_dir = _layer(make_layer, [CLAUDE, MOCK])
    layer = ir.load(layer_dir)
    assert helper.reply(layer, PROMPT, run=FakeRun())["status"] == "ok"
    assert helper.reply(layer, PROMPT, run=FakeRun(code=1, err="boom"))["status"] == "failed"
    log = layer_dir / "agents" / "voice" / "helper-calls.jsonl"
    text = log.read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert [r["status"] for r in records] == ["ok", "failed"]
    assert records[0]["cost"] == 0.0123 and records[0]["model"] == "claude-opus-5-5" and records[0]["tier"] == "base"
    assert set(records[0]) == {"t", "agent", "kind", "tier", "model", "cost", "status", "seconds"}
    for secret in ("secret plan", "what is running", "Two runs are going", "person-said"):
        assert secret not in text
