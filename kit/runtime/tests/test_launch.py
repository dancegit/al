"""The one start path (adw_modules/launch.py): what may be started, the request-key ledger, the
confirmations, the refusals, the prompt that never reaches a command line, and one single-command
run with the mock agent."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

from adw_modules import inputs, ir, launch
from adw_modules.errors import LayerError

KEY = "key-0001-abcdef"

TEXT = {"description": "", "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}


def _ir(app: Path) -> dict:
    return {
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "classify", "template": "classify", "modelSet": "base", "output": "json", **TEXT},
                     {"name": "plan-chore", "template": "plan-chore", "modelSet": "heavy", "output": "path", **TEXT},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json", **TEXT}],
        "hops": [{"name": "implement", "template": "run-plan", "modelSet": "heavy", "takes": "plan",
                  "produces": "changes", "description": ""}],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 5}]},
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""},
                    {"id": "auto", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": [], "ship": "merge", "deployCmd": ""}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27400,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "forge": {"id": "forgejo", "kind": "forgejo", "host": "http://127.0.0.1:9", "repo": "acme/notes",
                  "status": "stable", "botMarker": "[ADW-BOT]", "tokenEnv": "FORGEJO_TOKEN"},
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False},
                         {"id": "issue", "kind": "issue", "status": "stable", "labels": ["adw"], "keyword": "adw",
                          "classify": True}],
        "triggers": [{"id": "cron", "kind": "cron", "status": "stable", "intervalSeconds": 15, "maxConcurrent": 3}],
        "concurrency": {"maxRuns": 2, "maxAgents": 2, "maxChecks": 2, "waitSeconds": 30},
        "adws": [{"name": "plan-build", "description": "plan and build", "agent": "mock", "review": "human",
                  "environment": ["worktree"], "input": ["cli", "issue"], "trigger": ["cron"], "classes": [],
                  "steps": [{"name": "plan", "kind": "run", "command": "plan-chore", "args": ["$prompt"]},
                            {"name": "build", "kind": "run", "command": "implement", "args": ["$plan"]}]},
                 {"name": "ship-it", "description": "merges itself", "agent": "mock", "review": "auto",
                  "environment": ["worktree"], "input": ["cli"], "trigger": [], "classes": ["chore"],
                  "steps": [{"name": "plan", "kind": "run", "command": "plan-chore", "args": ["$prompt"]},
                            {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    }


@pytest.fixture
def layer_dir(make_layer, app_repo) -> Path:
    return make_layer(_ir(app_repo))


@pytest.fixture
def layer(layer_dir):
    return ir.load(layer_dir)


class Proc:
    pid = 4242

    def wait(self):
        return 0


class Spawn:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        return Proc()


class FakeForge:
    def __init__(self):
        self.created = []

    def create_issue(self, title, body, labels=()):
        self.created.append({"title": title, "body": body, "labels": list(labels)})
        return {"number": 40 + len(self.created), "url": f"http://forge/acme/notes/issues/{40 + len(self.created)}"}


def start(layer, text, **kw):
    target = launch.resolve_target(layer, text)
    kw.setdefault("request_key", KEY)
    kw.setdefault("spawn", Spawn())
    return launch.start(layer, target, target.prompt, **kw)


def test_single_command_adw_is_one_run_step_in_the_default_environment(layer):
    adw = launch.single_command_adw(layer, "plan-chore")
    assert adw["name"] == "command:plan-chore"
    assert adw["steps"] == [{"kind": "run", "name": "plan-chore", "command": "plan-chore", "args": ["$prompt"],
                             "modelSet": "inherit", "agent": "", "retries": 0, "when": "always"}]
    first = layer.ir["adws"][0]
    assert (adw["environment"], adw["agent"], adw["review"]) == (first["environment"], first["agent"], first["review"])
    assert adw["input"] == ["cli"] and adw["trigger"] == [] and adw["classes"] == []
    assert not {"ship", "review"} & {s["kind"] for s in adw["steps"]}
    assert launch.single_command_adw(layer, "implement")["steps"][0]["command"] == "implement"   # a hop too
    with pytest.raises(LayerError, match="Unknown command or hop: nope"):
        launch.single_command_adw(layer, "nope")
    layer.ir["adws"].append({**first, "name": "command:classify"})
    with pytest.raises(LayerError, match="declares a workflow named command:classify"):
        launch.single_command_adw(layer, "classify")


def test_resolve_target_prefers_the_workflow_and_command_prefix_forces_the_command(layer):
    assert launch.resolve_target(layer, "/plan-build add a flag") == launch.Target("workflow", "plan-build", "add a flag")
    assert launch.resolve_target(layer, "/plan-chore tidy\nthe readme") == launch.Target(
        "command", "plan-chore", "tidy\nthe readme")
    layer.ir["commands"].append({"name": "plan-build", "template": "plan-chore", "modelSet": "base", "output": "path"})
    assert launch.resolve_target(layer, "/plan-build x").kind == "workflow"
    assert launch.resolve_target(layer, "/command:plan-build x") == launch.Target("command", "plan-build", "x")
    assert launch.resolve_target(layer, "/plan-build x", kind="command").kind == "command"
    with pytest.raises(LayerError) as exc:
        launch.resolve_target(layer, "/nope x")
    assert "no workflow or command named nope; known: /plan-build, /ship-it, /classify, /plan-chore" in str(exc.value)
    assert "/implement" in str(exc.value)
    with pytest.raises(LayerError, match="start with /NAME"):
        launch.resolve_target(layer, "plan-build x")


def test_start_reserves_the_run_id_writes_the_prompt_file_and_detaches(layer, layer_dir):
    spawn = Spawn()
    result = start(layer, "/plan-build add a flag", spawn=spawn, klass="chore", model_set="heavy",
                   models=["base=claude-sonnet-5-5"], dry_run=True)
    assert result["status"] == "started" and result["existing"] is False and result["queued"] == ""
    run_id = result["run_id"]
    assert result["target"] == {"kind": "workflow", "name": "plan-build"}
    call, = spawn.calls
    argv = call["argv"]
    assert argv[:4] == ["uv", "run", "--quiet", str(layer_dir / "adws" / "adw_plan-build.py")]
    request = layer_dir / "agents" / run_id / "request.md"
    assert argv[argv.index("--adw-id") + 1] == run_id
    assert argv[argv.index("--prompt-file") + 1] == str(request)
    assert argv[argv.index("--class") + 1] == "chore" and argv[argv.index("--model-set") + 1] == "heavy"
    assert argv[argv.index("--model") + 1] == "base=claude-sonnet-5-5" and argv[-1] == "--dry-run"
    assert call["start_new_session"] is True and call["cwd"] == str(layer_dir)
    assert call["stdin"] == subprocess.DEVNULL
    assert request.read_text() == "add a flag\n" and request.stat().st_mode & 0o777 == 0o600
    skeleton = json.loads((layer_dir / "agents" / run_id / "state.json").read_text())
    assert (skeleton["adw"], skeleton["status"], skeleton["pid"]) == ("plan-build", "running", 0)
    ledger = json.loads((layer_dir / "agents" / "starts.json").read_text())["starts"]
    assert ledger[KEY]["run_id"] == run_id and ledger[KEY]["target"] == "workflow:plan-build"
    command = start(layer, "/plan-chore tidy", request_key="key-0002-abcdef")
    assert command["status"] == "started"


def test_a_command_start_runs_dispatch_with_the_command(layer, layer_dir):
    spawn = Spawn()
    result = start(layer, "/implement specs/x.md", spawn=spawn)
    argv = spawn.calls[0]["argv"]
    assert argv[:6] == ["uv", "run", "--quiet", str(layer_dir / "adws" / "dispatch.py"), "--command", "implement"]
    skeleton = json.loads((layer_dir / "agents" / result["run_id"] / "state.json").read_text())
    assert skeleton["adw"] == "command:implement" and skeleton["definition"]["steps"][0]["command"] == "implement"


def test_the_prompt_never_enters_argv(layer, layer_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hostile = "$(touch pwned); rm -rf ~ `touch pwned2` && echo 'x\" ; touch pwned3"
    spawn = Spawn()
    result = start(layer, f"/plan-chore {hostile}", spawn=spawn)
    assert result["status"] == "started"
    for call in spawn.calls:
        assert all("pwned" not in arg and "rm -rf" not in arg for arg in call["argv"])
    assert (layer_dir / "agents" / result["run_id"] / "request.md").read_text() == hostile + "\n"
    large = "x" * (60 * 1024)
    result = start(layer, f"/plan-chore {large}", spawn=spawn, request_key="key-0003-abcdef")
    assert (layer_dir / "agents" / result["run_id"] / "request.md").read_text() == large + "\n"
    assert not list(tmp_path.rglob("pwned*")) and not list(layer_dir.rglob("pwned*"))


def test_same_request_key_starts_one_run(layer, layer_dir):
    spawn = Spawn()
    first = start(layer, "/plan-build add a flag", spawn=spawn)
    again = start(layer, "/plan-build add a flag", spawn=spawn)
    assert len(spawn.calls) == 1
    assert again["status"] == "started" and again["run_id"] == first["run_id"] and again["existing"] is True
    assert [p.name for p in (layer_dir / "agents").iterdir() if len(p.name) == 8] == [first["run_id"]]


def test_identical_prompt_within_the_window_asks_for_confirmation(layer):
    spawn = Spawn()
    first = start(layer, "/plan-build add a flag", spawn=spawn)
    second = start(layer, "/plan-build add a flag", spawn=spawn, request_key="key-0002-abcdef")
    assert second["status"] == "confirm-duplicate" and second["previous"] == first["run_id"]
    assert first["run_id"] in second["message"] and len(spawn.calls) == 1
    other = start(layer, "/plan-build add another flag", spawn=spawn, request_key="key-0003-abcdef")
    assert other["status"] == "started"
    confirmed = start(layer, "/plan-build add a flag", spawn=spawn, request_key="key-0002-abcdef",
                      confirm_duplicate=True)
    assert confirmed["status"] == "started" and confirmed["run_id"] != first["run_id"] and len(spawn.calls) == 3


def test_unattended_shipping_workflow_needs_confirmation(layer):
    spawn = Spawn()
    asked = start(layer, "/ship-it add a flag", spawn=spawn)
    assert asked["status"] == "confirm-ships" and "merges without a person" in asked["message"]
    assert spawn.calls == [] and launch.EXIT[asked["status"]] == 4
    assert start(layer, "/ship-it add a flag", spawn=spawn, confirm_ships=True)["status"] == "started"
    assert start(layer, "/plan-build add a flag", spawn=spawn, request_key="key-0002-abcdef")["status"] == "started"
    assert start(layer, "/review it", spawn=spawn, request_key="key-0003-abcdef")["status"] == "started"
    assert launch.ships_unattended(layer, layer.adw("ship-it")) == "merge"
    assert launch.ships_unattended(layer, layer.adw("plan-build")) == ""


@pytest.mark.parametrize("text, kw, error", [
    ("/nope x", {}, "no workflow named nope; known: plan-build, ship-it"),
    ("/plan-build x", {"request_key": "short"}, "request key"),
    ("/plan-build x", {"request_key": "has space in it"}, "request key"),
    ("/plan-build x", {"klass": "epic"}, "unknown class"),
    ("/plan-build x", {"model_set": "Not-A-Tier"}, "is not a tier name"),
    ("/plan-build x", {"models": ["base=has space"]}, "model id must be"),
    ("/plan-build x", {"models": ["nonsense"]}, "TIER=MODEL_ID"),
    ("/plan-build x", {"issue": "maybe"}, "issue: one of"),
    ("/plan-build", {}, "a prompt is required"),
    ("/plan-chore x", {"issue": "trigger"}, "a trigger starts workflows only"),
    ("/ship-it x", {"issue": "trigger"}, "no cron or webhook trigger starts ship-it"),
])
def test_unknown_name_and_bad_key_are_invalid(layer, layer_dir, text, kw, error):
    spawn = Spawn()
    if text == "/nope x":
        result = launch.start(layer, launch.Target("workflow", "nope"), "x", request_key=KEY, spawn=spawn)
    else:
        target = launch.resolve_target(layer, text)
        result = launch.start(layer, target, target.prompt, spawn=spawn, **{"request_key": KEY, **kw})
    assert result["status"] == "invalid" and error in result["error"], result
    assert spawn.calls == [] and not (layer_dir / "agents" / "starts.json").exists()
    assert launch.EXIT["invalid"] == 2


def test_an_issue_needs_a_forge(make_layer, app_repo):
    data = _ir(app_repo)
    data["forge"] = {"id": "none", "kind": "none", "status": "stable", "remote": ""}
    lay = ir.load(make_layer(data))
    result = start(lay, "/plan-build x", issue="start")
    assert result["status"] == "invalid" and "no forge" in result["error"]


def test_day_limit_refuses_without_starting(make_layer, app_repo):
    data = _ir(app_repo)
    data["budget"] = {"id": "limits", "kind": "limits", "status": "stable", "dayUsd": 1.0}
    layer_dir = make_layer(data)
    spent = layer_dir / "agents" / "aaaa0001"
    spent.mkdir(parents=True)
    from adw_modules import state as state_module
    (spent / "state.json").write_text(json.dumps({"started": state_module.now(), "cost": {"total": 1.5}}))
    spawn = Spawn()
    result = start(ir.load(layer_dir), "/plan-build x", spawn=spawn)
    assert result["status"] == "refused" and "budget:day" in result["message"]
    assert spawn.calls == [] and launch.EXIT["refused"] == 3
    assert not (layer_dir / "agents" / "starts.json").exists()


def test_full_run_slots_say_queued(layer, layer_dir):
    directory = layer_dir / "agents" / ".slots"
    directory.mkdir(parents=True)
    handles = []
    try:
        for index in range(2):
            handle = os.open(directory / f"runs.{index}.lock", os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append(handle)
        result = start(layer, "/plan-build x")
    finally:
        for handle in handles:
            os.close(handle)
    assert result["status"] == "started"
    assert result["queued"] == "all 2 run slots are in use; it waits up to 30 s"
    assert start(layer, "/plan-build y", request_key="key-0002-abcdef")["queued"] == ""


def test_issue_start_now_claims_the_issue_and_runs_from_it(layer, layer_dir):
    spawn, forge = Spawn(), FakeForge()
    result = start(layer, "/plan-build Add a flag\n\nwith details", spawn=spawn, forge=forge, issue="start",
                   klass="chore")
    created, = forge.created
    assert created["title"] == "Add a flag" and created["labels"] == ["chore"]
    assert created["body"] == f"Add a flag\n\nwith details\n\n{inputs.claim_marker(result['run_id'])}"
    assert inputs.is_claimed(created)
    argv = spawn.calls[0]["argv"]
    assert argv[argv.index("--source") + 1] == "issue:41" and "--prompt-file" not in argv
    assert result["issue"]["number"] == 41
    command = start(layer, "/plan-chore tidy", spawn=spawn, forge=forge, issue="start", request_key="key-0002-abcdef")
    assert "--prompt-file" in spawn.calls[1]["argv"] and "--source" not in spawn.calls[1]["argv"]
    assert inputs.claim_marker(command["run_id"]) in forge.created[1]["body"]


def test_issue_trigger_mode_creates_the_issue_and_starts_nothing(layer, layer_dir):
    spawn, forge = Spawn(), FakeForge()
    result = start(layer, "/plan-build Add a flag", spawn=spawn, forge=forge, issue="trigger", klass="bug")
    assert result["status"] == "issue-created" and result["run_id"] == "" and spawn.calls == []
    assert forge.created == [{"title": "Add a flag", "body": "Add a flag\n\nadw: plan-build", "labels": ["adw", "bug"]}]
    assert not inputs.is_claimed(forge.created[0])
    again = start(layer, "/plan-build Add a flag", spawn=spawn, forge=forge, issue="trigger")
    assert again["status"] == "issue-created" and again["existing"] is True and len(forge.created) == 1


def test_single_command_run_records_like_a_run_and_never_ships(layer_dir, app_repo):
    code = launch.run_command(layer_dir, "plan-chore", ["tidy the readme", "--adw-id", "c0ffee01"])
    assert code == 0
    run = layer_dir / "agents" / "c0ffee01"
    state = json.loads((run / "state.json").read_text())
    assert state["status"] == "passed" and state["adw"] == "command:plan-chore"
    assert state["definition"]["steps"][0]["command"] == "plan-chore"
    assert state["plan"] and Path(state["plan"]).is_file() and Path(state["plan"]).is_relative_to(layer_dir / "specs")
    assert [s["name"] for s in state["steps"]] == ["plan-chore"] and state["ship"] == {}
    kinds = [json.loads(line)["type"] for line in (run / "events.jsonl").read_text().splitlines()]
    assert kinds[0] == "run.start" and kinds[-1] == "run.end"
    assert not list(run.rglob("PR.md")) and not (layer_dir / "PR.md").exists()
    assert state["agent_calls"][0]["tier"] == "heavy"   # the command's own tier
    assert json.loads((layer_dir / "agentic-layer.json").read_text())["adws"][-1]["name"] == "ship-it"   # IR unchanged
