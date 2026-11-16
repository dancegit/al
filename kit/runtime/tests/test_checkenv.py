"""`(check … #:in ENV)`: the check runs in another environment option (adw_modules/checkenv.py),
in the check step, the review's re-checks and the gate's re-run. podman is faked; nothing here
starts a container."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import checkenv, envs, events, sandbox, steps
from adw_modules.errors import StepFailed
from adw_modules.ir import load
from adw_modules.runner import main
from adw_modules.state import ADWState

ADW_ID = "c4ec0001"


def _podman(id_="box", **extra) -> dict:
    return {"id": id_, "kind": "podman", "agentInside": False, "baseImage": "debian:stable-slim", "cpus": "1",
            "memory": "1g", "network": "slirp", "packages": [], "setup": [], "status": "stable", "scope": "run",
            "caches": [], "credentials": [], "publish": [], "services": [], **extra}


WT = {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 24970, "portsPerRun": 0,
      "keep": "never", "status": "stable"}
NIX = {"id": "nix", "kind": "nix-devshell", "packages": ["go"], "status": "stable", "pure": False}


def _check(name: str, cmd: str, in_: str = "", **extra) -> dict:
    return {"name": name, "kind": "check", "cmd": cmd, "resolve": "", "retries": 0, "collect": [], "in": in_,
            "maxSkipped": -1, "forbidSkipReason": "", **extra}


def _layer(make_layer, *, environment=("wt", "box"), steps_=None, engine=None, services=None) -> Path:
    return make_layer({
        "environments": [WT, _podman("box"), _podman("engine", nestedEngine=True, **(engine or {})), NIX],
        "services": services or [],
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"commands": [{"name": "test", "run": "pytest -q", "timeout": 10},
                                  {"name": "engine-test", "run": "sh engine-test.sh", "timeout": 10},
                                  {"name": "go-test", "run": "go test -v ./...", "timeout": 10}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": ["test", "engine-test"], "ship": "merge"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": list(environment),
                  "classes": ["chore"], "steps": steps_ or [
                      _check("test", "test"), _check("engine-test", "engine-test", "engine"),
                      _check("go-test", "go-test", "nix"), {"name": "review", "kind": "review"},
                      {"name": "ship", "kind": "ship"}]}]})


class FakePodman:
    def __init__(self, monkeypatch) -> None:
        self.calls: list[list[str]] = []
        self.ps = ""
        real = subprocess.run
        fake = self

        def run(argv, **kwargs):
            if not argv or argv[0] != "podman":
                return real(argv, **kwargs)
            fake.calls.append(list(argv[1:]))
            if argv[1:3] == ["ps", "-aq"]:
                return SimpleNamespace(returncode=0, stdout=fake.ps, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(subprocess, "run", run)


@pytest.fixture
def fake_podman(monkeypatch) -> FakePodman:
    return FakePodman(monkeypatch)


@pytest.fixture(autouse=True)
def _forget_open_stacks():
    checkenv._open.clear()
    yield
    checkenv._open.clear()


def _run(layer_dir: Path, tmp_path: Path):
    layer = load(layer_dir)
    worktree = tmp_path / "wt-run"
    worktree.mkdir(exist_ok=True)
    state = ADWState(adw_id=ADW_ID, adw="demo", worktree=str(worktree))
    primary = envs.build(layer, state, layer.adw("demo")["environment"])
    primary.cwd = worktree
    return layer, state, primary


def _step(layer, name: str) -> dict:
    return next(step for step in layer.adw("demo")["steps"] if step["name"] == name)


def test_target_defaults_to_the_adws_stack():
    adw = {"environment": ["wt", "box"]}
    assert checkenv.target(adw, _check("t", "test")) is None
    assert checkenv.target(adw, {"name": "t", "kind": "check", "cmd": "test"}) is None   # an IR from before #:in
    assert checkenv.target(adw, _check("t", "test", "box")) is None                     # its own option: no second stack
    assert checkenv.target(adw, _check("t", "test", "engine")) == "engine"


def test_check_in_podman_runs_in_its_own_named_container(make_layer, tmp_path, fake_podman):
    layer, state, primary = _run(_layer(make_layer), tmp_path)
    assert checkenv.stack_for(layer, state, _step(layer, "test"), primary) is primary
    stack = checkenv.stack_for(layer, state, _step(layer, "engine-test"), primary)
    assert isinstance(stack, checkenv.CheckStack) and stack is checkenv.stack_for(layer, state, _step(layer, "engine-test"), primary)
    assert stack.run_name == f"al-test-layer-{ADW_ID}-engine"
    assert stack.cwd == primary.cwd and stack.env is primary.env and stack.sandbox is primary.sandbox
    argv = stack.wrap("sh engine-test.sh")
    assert argv[:3] == ["podman", "run", "--rm"]
    assert "--label" in argv and "al.check-env=engine" in argv and f"al.run={ADW_ID}" in argv
    assert f"{state.worktree}:{state.worktree}" in argv
    assert argv[-3:] == ["bash", "-c", "sh engine-test.sh"]
    assert "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges" in argv   # nothing relaxed
    assert primary._container is None and not any(call[:2] == ["run", "-d"] for call in fake_podman.calls)
    assert ["volume", "create"] == next(c for c in fake_podman.calls if c[0] == "volume")[:2]  # its engine volume


def test_check_in_nix_runs_under_nix_with_quarantine_git_env(make_layer, tmp_path, fake_podman):
    layer, state, primary = _run(_layer(make_layer), tmp_path)
    stack = checkenv.stack_for(layer, state, _step(layer, "go-test"), primary)
    assert stack.wrap("go test ./...")[:2] == ["nix", "develop"]
    env = stack.process_env()
    assert env["GIT_OBJECT_DIRECTORY"] == str(primary.sandbox.quarantine)
    assert env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == str(primary.sandbox.app_git / "objects")
    plain, plain_state, plain_primary = _run(_layer(make_layer, environment=("wt",)), tmp_path)
    checkenv._open.clear()
    unsandboxed = checkenv.stack_for(plain, plain_state, _step(plain, "go-test"), plain_primary)
    assert "GIT_OBJECT_DIRECTORY" not in unsandboxed.process_env()   # conftest clears it from the test process


def _record_commands(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def run_command(argv, cwd, env, timeout, log_path=None):
        seen.append(list(argv))
        Path(log_path).write_text("1 passed in 0.01s\n")
        return 0, "1 passed in 0.01s\n"
    monkeypatch.setattr(steps, "run_command", run_command)
    return seen


def test_gate_rerun_uses_the_check_environment(make_layer, tmp_path, fake_podman, monkeypatch):
    layer, state, primary = _run(_layer(make_layer), tmp_path)
    primary.wrap = lambda cmd: ["primary", cmd]
    seen = _record_commands(monkeypatch)
    log_dir = Path(layer.layer_dir) / "agents" / ADW_ID / "checks"
    log_dir.mkdir(parents=True)
    assert steps.run_logged(layer, state, primary, layer.cmd("engine-test"), log_dir / "gate-engine-test.log",
                            "gate", "gate") == 0
    assert seen[-1][:3] == ["podman", "run", "--rm"] and "al.check-env=engine" in seen[-1]
    steps.run_logged(layer, state, primary, layer.cmd("test"), log_dir / "gate-test.log", "gate", "gate")
    assert seen[-1] == ["primary", "pytest -q"]
    steps.run_logged(layer, state, primary, layer.cmd("engine-test"), log_dir / "deploy.log", "deploy", "deploy")
    assert seen[-1] == ["primary", "sh engine-test.sh"]   # the deploy phase is unchanged


def test_review_recheck_uses_the_check_environment(make_layer, tmp_path, fake_podman, monkeypatch):
    layer, state, primary = _run(_layer(make_layer), tmp_path)
    primary.wrap = lambda cmd: ["primary", cmd]
    seen = _record_commands(monkeypatch)
    steps.run_check_with_resolve(layer, state, _step(layer, "engine-test"), primary)
    assert seen[-1][:3] == ["podman", "run", "--rm"] and "al.check-env=engine" in seen[-1]
    assert state.checks["engine-test"]["in"] == "engine" and state.checks["engine-test"]["exit"] == 0
    steps.run_check_with_resolve(layer, state, _step(layer, "test"), primary)
    assert seen[-1] == ["primary", "pytest -q"] and "in" not in state.checks["test"]


def test_close_all_removes_check_containers_and_pods(make_layer, tmp_path, fake_podman):
    services = [{"id": "db", "kind": "container", "image": "docker.io/library/postgres:16-alpine", "env": [],
                 "command": "", "readyCmd": "", "readyTimeout": 5, "status": "stable"}]
    layer, state, primary = _run(_layer(make_layer, engine={"services": ["db"]}, services=services), tmp_path)
    stack = checkenv.stack_for(layer, state, _step(layer, "engine-test"), primary)
    stack.wrap("sh engine-test.sh")   # starts the check environment's pod with its sidecar
    pod = f"al-test-layer-{ADW_ID}-engine"
    assert stack._pod == pod and ["pod", "create", "--name", pod] == next(c for c in fake_podman.calls if c[:2] == ["pod", "create"])[:4]
    fake_podman.ps = "0123abcd\n4567ef01\n"   # a container that outlived a timed-out command is still there
    fake_podman.calls.clear()
    checkenv.close_all(state)
    assert ["ps", "-aq", "--filter", f"label=al.run={ADW_ID}", "--filter", "label=al.check-env=engine"] in fake_podman.calls
    assert ["rm", "-f", "-t", "0", "0123abcd"] in fake_podman.calls and ["rm", "-f", "-t", "0", "4567ef01"] in fake_podman.calls
    assert ["pod", "rm", "-f", "-t", "0", pod] in fake_podman.calls
    assert ["rm", "-f", "-v", "-t", "0", f"{pod}-db"] in fake_podman.calls
    assert checkenv._open == {}
    assert not any(call[:2] == ["worktree", "remove"] for call in fake_podman.calls)


def test_preflight_checks_each_check_environment_with_its_own_commands(make_layer, monkeypatch, fake_podman):
    layer_dir = _layer(make_layer)
    (layer_dir / "env").mkdir()
    (layer_dir / "env" / "Containerfile").write_text("FROM debian:stable-slim\n")
    monkeypatch.setattr(sandbox, "ensure_image", lambda layer, say=print: {"built": False})
    seen: dict[str, list[str]] = {}

    def doctor(layer, stack, adw, **kwargs):
        env = getattr(stack, "env_id", "primary")
        seen[env] = [cmd["name"] for cmd in kwargs["cmds"]]
        if env == "engine":
            return [sandbox.Check("engine:fuse", "miss", "the run's container has no /dev/fuse", "envs.py")]
        return [sandbox.Check("git", "ok", "fine")]
    monkeypatch.setattr(sandbox, "doctor", doctor)
    assert main("demo", layer_dir, ["Do it", "--adw-id", ADW_ID]) == 1
    assert seen == {"primary": ["test"], "engine": ["engine-test"]}   # the nix check runs on the host: no doctor
    found = events.read(layer_dir / "agents" / ADW_ID / "events.jsonl")
    assert not [e for e in found if e["type"] == "step.start"]
    pre = [e for e in found if e["type"] == "env.preflight"]
    assert [(e["env"], e["status"]) for e in pre] == [("box", "passed"), ("engine", "failed")]
    log = (layer_dir / "agents" / ADW_ID / "run.log").read_text()
    assert "check environment engine is missing what its checks need" in log and "engine:fuse | miss" in log
    assert any(call[:3] == ["ps", "-aq", "--filter"] for call in fake_podman.calls)   # closed at the end
    with pytest.raises(StepFailed):
        layer = load(layer_dir)
        state = ADWState(adw_id="c4ec0002", adw="demo", worktree=str(layer_dir))
        checkenv.preflight(layer, state, layer.adw("demo"), envs.build(layer, state, ["wt", "box"]))


def test_workflow_commands_filters_by_environment(make_layer):
    layer = load(_layer(make_layer))
    adw = layer.adw("demo")
    assert [c["name"] for c in sandbox.workflow_commands(layer, adw)] == ["test"]
    assert [c["name"] for c in sandbox.workflow_commands(layer, adw, "engine")] == ["engine-test"]
    assert [c["name"] for c in sandbox.workflow_commands(layer, adw, "nix")] == ["go-test"]
    assert sandbox.workflow_commands(layer, adw, "box") == []
