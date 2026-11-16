"""The sandbox image follows its recipe and is checked before work starts (adw_modules/sandbox.py).

podman is faked here (a recorder answering from a dict the test controls); git is real. The same
flows against real rootless podman are in test_sandbox_lifecycle_podman.py."""
from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import events, sandbox
from adw_modules.errors import LayerError, StepFailed
from adw_modules.ir import load
from adw_modules.runner import main

ADW_ID = "5d5d0001"


def _podman_option(**extra) -> dict:
    return {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian:stable-slim", "cpus": "1",
            "memory": "1g", "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run",
            "caches": [], "credentials": [], **extra}


def _worktree_option(**extra) -> dict:
    return {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 24900, "portsPerRun": 0,
            "keep": "never", "status": "stable", **extra}


def _layer(make_layer, containerfile: str = "FROM debian:stable-slim\nRUN echo one\n", *, envs_=None, **ir):
    layer_dir = make_layer({"environments": envs_ or [_worktree_option(), _podman_option()], **ir})
    (layer_dir / "env").mkdir(exist_ok=True)
    (layer_dir / "env" / "Containerfile").write_text(containerfile, encoding="utf-8")
    return layer_dir


class FakePodman:
    """Answers the podman calls sandbox.py makes from `self.images` {ref: {Id, Size, Created, Labels}}
    and `self.containers`; records every call. Everything that is not podman runs for real."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[list[str]] = []
        self.images: dict[str, dict] = {}
        self.containers: list[dict] = []
        self.build_lines = ["STEP 1/2: FROM debian", "STEP 2/2: RUN echo one", "COMMIT"]
        self.build_exit = 0
        self.build_delay = 0.0
        self.doctor_output = ""
        self.lock = threading.Lock()
        real_run, real_popen = subprocess.run, subprocess.Popen
        fake = self

        def run(argv, **kwargs):
            if not argv or argv[0] != "podman":
                return real_run(argv, **kwargs)
            with fake.lock:
                fake.calls.append(list(argv))
            return fake.answer(list(argv[1:]))

        class Popen:
            def __new__(cls, argv, *args, **kwargs):
                if argv[0] != "podman":                  # a real process, usable by subprocess.run as well
                    return real_popen(argv, *args, **kwargs)
                return super().__new__(cls)

            def __init__(self, argv, **kwargs):
                with fake.lock:
                    fake.calls.append(list(argv))
                time.sleep(fake.build_delay)
                self.stdout = io.StringIO("".join(f"{line}\n" for line in fake.build_lines))
                self.argv = argv
                self.returncode = None

            def wait(self, timeout=None):
                if fake.build_exit == 0:
                    ref = self.argv[self.argv.index("-t") + 1]
                    labels = dict(self.argv[i + 1].split("=", 1) for i, a in enumerate(self.argv) if a == "--label")
                    with fake.lock:
                        fake.images[ref] = {"Id": f"id-{ref.split(':')[1]}".ljust(64, "0"), "Size": 300_000_000,
                                            "Created": time.time(), "Labels": labels}
                self.returncode = fake.build_exit
                return self.returncode

            def kill(self):
                pass

        monkeypatch.setattr(sandbox.subprocess, "run", run)
        monkeypatch.setattr(sandbox.subprocess, "Popen", Popen)

    def _find(self, ref: str) -> dict | None:
        ref = ref.removeprefix("localhost/")
        return self.images.get(ref)

    def answer(self, args: list[str]):
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")  # noqa: E731
        fail = SimpleNamespace(returncode=1, stdout="", stderr="no such image")
        if args[:2] == ["image", "exists"]:
            return ok() if self._find(args[2]) else fail
        if args[:2] == ["image", "inspect"]:
            found = self._find(args[2])
            return ok(json.dumps([found])) if found else SimpleNamespace(returncode=125, stdout="[]", stderr="")
        if args[0] == "tag":
            self.images[args[2]] = self._find(args[1])
            return ok()
        if args[0] == "rmi":
            self.images.pop(args[1], None)
            return ok()
        if args[0] == "images":
            seen, rows = set(), []
            for ref, info in self.images.items():
                if id(info) in seen:
                    continue
                seen.add(id(info))
                names = [f"localhost/{r}" for r, i in self.images.items() if i is info]
                rows.append({**info, "Names": names})
            return ok(json.dumps(rows))
        if args[:2] == ["ps", "-a"]:
            return ok(json.dumps(self.containers))
        if args[0] == "exec" and "bash" in args:
            return ok(self.doctor_output)
        return ok()

    def builds(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:2] == ["build"]]


# ------------------------------------------------------------------ recipe identity

def test_recipe_hash_changes_with_containerfile(make_layer) -> None:
    layer = load(_layer(make_layer))
    first = sandbox.recipe_hash(layer)
    assert first == sandbox.recipe_hash(layer) and len(first) == 12
    (layer.layer_dir / "env" / "Containerfile").write_text("FROM debian:stable-slim\nRUN echo two\n")
    assert sandbox.recipe_hash(layer) != first


def test_recipe_hash_changes_with_a_copied_file_and_its_mode(make_layer) -> None:
    layer = load(_layer(make_layer, "FROM debian:stable-slim\nCOPY tools/ /opt/tools/\n"))
    tools = layer.layer_dir / "env" / "tools"
    tools.mkdir()
    (tools / "run.sh").write_text("echo hi\n")
    first = sandbox.recipe_hash(layer)
    (tools / "run.sh").write_text("echo hello\n")
    second = sandbox.recipe_hash(layer)
    assert second != first
    (tools / "run.sh").chmod(0o755)
    assert sandbox.recipe_hash(layer) != second


def test_recipe_hash_changes_with_the_base_image_reference(make_layer) -> None:
    first = sandbox.recipe_hash(load(_layer(make_layer)))
    other = sandbox.recipe_hash(load(_layer(make_layer, envs_=[_worktree_option(), _podman_option(baseImage="debian:13")])))
    assert first != other


def test_recipe_hash_ignores_env_files_the_containerfile_does_not_copy(make_layer) -> None:
    layer = load(_layer(make_layer))
    (layer.layer_dir / "env" / "flake.nix").write_text("{ }\n")
    first = sandbox.recipe_hash(layer)
    (layer.layer_dir / "env" / "flake.nix").write_text("{ inputs = {}; }\n")
    assert sandbox.recipe_hash(layer) == first


def test_copied_sources_skips_from_stage_urls_and_flags() -> None:
    text = ("FROM debian AS build\n"
            "COPY --from=build /out /out\n"
            "ADD https://example.org/tool.tgz /opt/\n"
            "COPY --chown=1000:1000 --chmod=755 tools/ a.sh /opt/tools/\n"
            "COPY [\"conf/app.toml\", \"/etc/app.toml\"]\n"
            "ADD \\\n  vendor /vendor\n"
            "RUN echo COPY nothing\n")
    assert sandbox.copied_sources(text) == ["tools/", "a.sh", "conf/app.toml", "vendor"]


def test_recipe_hash_refuses_a_copy_source_outside_env(make_layer) -> None:
    layer = load(_layer(make_layer, "FROM debian\nCOPY ../agentic-layer.json /x\n"))
    with pytest.raises(LayerError, match="outside env"):
        sandbox.recipe_hash(layer)
    (layer.layer_dir / "env" / "Containerfile").unlink()
    with pytest.raises(LayerError, match="al gen"):
        sandbox.recipe_hash(layer)


def test_image_ref_is_layer_name_and_short_hash(make_layer) -> None:
    layer = load(_layer(make_layer))
    assert sandbox.image_ref(layer) == f"al-test-layer:{sandbox.recipe_hash(layer)}"


# ------------------------------------------------------------------ build / ensure / prune / status

def test_ensure_image_builds_when_the_hash_is_missing_and_not_when_present(make_layer, monkeypatch) -> None:
    layer = load(_layer(make_layer))
    fake = FakePodman(monkeypatch)
    result = sandbox.ensure_image(layer, say=lambda line: None)
    ref = sandbox.image_ref(layer)
    assert result["built"] and result["image"] == ref
    (build,) = fake.builds()
    assert build[2:4] == ["-t", ref]
    labels = [build[i + 1] for i, arg in enumerate(build) if arg == "--label"]
    assert labels == ["al.layer=test-layer", f"al.layer-dir={layer.layer_dir.resolve()}",
                      f"al.recipe={sandbox.recipe_hash(layer)}"]
    assert ["podman", "tag", ref, "al-test-layer:latest"] in fake.calls
    fake.calls.clear()
    assert sandbox.ensure_image(layer)["built"] is False
    assert fake.builds() == []


def test_build_streams_output_and_records_build_events(make_layer, monkeypatch) -> None:
    layer = load(_layer(make_layer))
    FakePodman(monkeypatch)
    seen: list[str] = []
    with events.recording(layer.layer_dir, ADW_ID):
        result = sandbox.build_image(layer, say=seen.append)
    assert seen == ["STEP 1/2: FROM debian", "STEP 2/2: RUN echo one", "COMMIT"]
    found = [e for e in events.read(events.path_for(layer.layer_dir, ADW_ID)) if e["type"].startswith("env.build")]
    assert [e["type"] for e in found] == ["env.build.start", "env.build.end"]
    end = found[1]
    assert end["status"] == "passed" and end["size"] == 300_000_000 and end["seconds"] >= 0
    assert end["image"] == result["image"] and end["recipe"] == sandbox.recipe_hash(layer)


def test_build_failure_raises_with_the_last_lines(make_layer, monkeypatch) -> None:
    layer = load(_layer(make_layer))
    fake = FakePodman(monkeypatch)
    fake.build_lines = [f"line {i}" for i in range(60)] + ["E: Unable to locate package pytest"]
    fake.build_exit = 100
    with events.recording(layer.layer_dir, ADW_ID):
        with pytest.raises(StepFailed) as raised:
            sandbox.build_image(layer, say=lambda line: None)
    assert "exit 100" in raised.value.reason and "Unable to locate package pytest" in raised.value.reason
    assert "line 20" not in raised.value.reason and "line 59" in raised.value.reason
    end = events.read(events.path_for(layer.layer_dir, ADW_ID))[-1]
    assert end["type"] == "env.build.end" and end["status"] == "failed" and end["error"] == "exit 100"


def test_concurrent_ensure_builds_once(make_layer, monkeypatch) -> None:
    layer = load(_layer(make_layer))
    fake = FakePodman(monkeypatch)
    fake.build_delay = 0.5
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(sandbox.ensure_image(layer, say=lambda line: None)))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(fake.builds()) == 1
    assert sorted(result["built"] for result in results) == [False, True]


def _image(recipe: str, days: float, layer_dir: Path) -> dict:
    return {"Id": f"id{recipe}".ljust(64, "0"), "Size": 1000, "Created": time.time() - days * 86400,
            "Labels": {"al.layer": "test-layer", "al.layer-dir": str(layer_dir.resolve()), "al.recipe": recipe}}


def test_prune_keeps_current_in_flight_and_recent_images(make_layer, monkeypatch) -> None:
    layer = load(_layer(make_layer))
    fake = FakePodman(monkeypatch)
    current = sandbox.recipe_hash(layer)
    for recipe, days in ((current, 0.1), ("aaaaaaaaaaa1", 1), ("aaaaaaaaaaa2", 2), ("aaaaaaaaaaa3", 3),
                         ("aaaaaaaaaaa4", 4), ("aaaaaaaaaaa5", 5), ("aaaaaaaaaaa6", 15)):
        fake.images[f"al-test-layer:{recipe}"] = _image(recipe, days, layer.layer_dir)
    # in use by an existing container (by image id) …
    fake.containers = [{"Image": "", "ImageID": fake.images["al-test-layer:aaaaaaaaaaa1"]["Id"]}]
    # … and recorded by a run that is alive (state running + a live lease)
    run = layer.layer_dir / "agents" / "0a0a0a0a"
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"status": "running", "image": "al-test-layer:aaaaaaaaaaa2"}))
    (layer.layer_dir / "agents" / ".ports.json").write_text(json.dumps({"0a0a0a0a": {"ports": [], "pid": os.getpid()}}))
    removed = sandbox.prune_images(layer, keep_old=2, max_age_days=14)
    # unused old images newest first: 3, 4, 5, 6 → 5 beyond the 2 newest, 6 older than 14 days
    assert sorted(removed) == ["al-test-layer:aaaaaaaaaaa5", "al-test-layer:aaaaaaaaaaa6"]
    removed = sandbox.prune_images(layer)
    assert sorted(removed) == ["al-test-layer:aaaaaaaaaaa3", "al-test-layer:aaaaaaaaaaa4"]
    assert set(fake.images) == {f"al-test-layer:{current}", "al-test-layer:aaaaaaaaaaa1", "al-test-layer:aaaaaaaaaaa2"}
    assert not any(call[1] == "rmi" and "-f" in call for call in fake.calls)


def test_status_lists_every_podman_env_with_the_shared_recipe(make_layer, monkeypatch) -> None:
    layer = load(_layer(make_layer, envs_=[_worktree_option(), _podman_option(), _podman_option(id="web", caches=["npm"])]))
    fake = FakePodman(monkeypatch)
    rows = sandbox.image_status(layer)
    current = sandbox.recipe_hash(layer)
    assert [(r["env"], r["recipe"], r["built"], r["recipe_from"]) for r in rows] == [
        ("box", current, False, "box"), ("web", current, False, "box")]
    fake.images[f"al-test-layer:{current}"] = _image(current, 0, layer.layer_dir)
    fake.images["al-test-layer:bbbbbbbbbbbb"] = _image("bbbbbbbbbbbb", 3, layer.layer_dir)
    rows = sandbox.image_status(layer)
    assert [r["built"] for r in rows[:2]] == [True, True]
    assert rows[2]["old"] and rows[2]["image"] == "al-test-layer:bbbbbbbbbbbb" and rows[2]["age"] == "3d"


# ------------------------------------------------------------------ what a workflow needs

@pytest.mark.parametrize("run, expected", [
    ("pytest -q", "pytest"), ("FOO=1 uv run x", "uv"), ("cd web && npm test", "npm"), ("./test.sh", "./test.sh"),
    ("env A=b make", "make"), ("true", None), ("export X=1; exec python3 -m pytest", "python3"),
    ("(cd web && bun test) | tee log", "bun"), ("echo 'unbalanced", None)])
def test_first_executable(run, expected) -> None:
    assert sandbox.first_executable(run) == expected


def _workflow_layer(make_layer, **review):
    return load(_layer(make_layer, **{
        "codebase": {"commands": [{"name": "lint", "run": "ruff check ."}, {"name": "test", "run": "pytest -q"},
                                  {"name": "e2e", "run": "npx playwright test"}, {"name": "sync", "run": "./sync.sh"}]},
        "artefacts": [{"id": "shots", "kind": "image", "glob": "*.png", "status": "stable"},
                      {"id": "log", "kind": "log", "status": "stable"}],
        "reviews": [{"id": "auto", "kind": "auto", "status": "stable", "artefacts": [], "command": "review",
                     "reviewers": 1, "maxRetries": 0, "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": ["test", "e2e"], "ship": "pr", **review}],
        "agents": [{"id": "mock-agent", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 30},
                   {"id": "cc", "kind": "claude-code", "status": "stable", "timeout": 30}],
        "adws": [{"name": "demo", "agent": "mock-agent", "review": "auto", "environment": ["wt", "box"],
                  "classes": ["chore"], "steps": [
                      {"name": "sync", "kind": "script", "cmd": "sync"},
                      {"name": "test", "kind": "check", "cmd": "test"},
                      {"name": "lint", "kind": "check", "cmd": "lint", "collect": ["log"]},
                      {"name": "again", "kind": "check", "cmd": "test"},
                      {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}]}))


def test_workflow_commands_are_check_script_and_required_checks(make_layer) -> None:
    layer = _workflow_layer(make_layer)
    adw = layer.adw("demo")
    assert [c["name"] for c in sandbox.workflow_commands(layer, adw)] == ["sync", "test", "lint", "e2e"]
    no_ship = {**adw, "steps": [s for s in adw["steps"] if s["kind"] != "ship"]}   # the gate re-runs them only when shipping
    assert [c["name"] for c in sandbox.workflow_commands(layer, no_ship)] == ["sync", "test", "lint"]
    assert sandbox.needs_browser(layer, adw) is False
    assert sandbox.needs_browser(_workflow_layer(make_layer, artefacts=["shots"]), adw) is True


@pytest.mark.parametrize("agent, inside, expected, host", [
    ("mock-agent", True, None, None),
    ("cc", True, ("claude", "claude-code"), None),
    ("cc", False, None, ("claude", "claude-code"))])
def test_agent_requirement(make_layer, agent, inside, expected, host) -> None:
    layer = _workflow_layer(make_layer)
    layer.ir["environments"][1]["agentInside"] = inside
    adw = {**layer.adw("demo"), "agent": agent}
    assert sandbox.agent_requirement(layer, adw) == expected
    assert sandbox.host_agent_requirement(layer, adw) == host


# ------------------------------------------------------------------ doctor

def test_parse_doctor_output_and_table_put_misses_first() -> None:
    text = ("noise from the image\nAL-DOCTOR\tgit\tok\trev-parse works\n"
            "AL-DOCTOR\tcommand:test\tmiss\tpytest not found on PATH\nAL-DOCTOR\tinit\tskip\tscope command\n"
            "AL-DOCTOR\tbroken\n")
    checks = sandbox.parse_doctor_output(text)
    assert [(c.name, c.status) for c in checks] == [("git", "ok"), ("command:test", "miss"), ("init", "skip")]
    checks[1].fix = "add pytest"
    table = sandbox.format_table(checks).splitlines()
    assert table[0].split(" | ")[:3] == ["check       ", "result", "detail                  "]
    assert [line.split(" | ")[0].strip() for line in table[1:]] == ["command:test", "git", "init"]
    assert table[1].endswith("| add pytest")


class _Stack:
    """Just what doctor() reads from an EnvStack; wrap returns a sh -c that prints `output`."""

    def __init__(self, option: dict, worktree: Path, output: str = "", error: Exception | None = None) -> None:
        self.podman_option, self.cwd, self.error, self.output = option, worktree, error, output
        self.state = SimpleNamespace(worktree=str(worktree), image="", adw_id=ADW_ID)
        self.scripts: list[str] = []
        self.argvs: list[list[str]] = []
        self._pod = None

    def _podman_argv(self, argv, interactive=False):
        self.argvs.append(argv)
        return self.wrap(argv[-1])

    def wrap(self, script: str) -> list[str]:
        self.scripts.append(script)
        if self.error:
            raise self.error
        return ["sh", "-c", f"printf '%b' {json.dumps(self.output)}"]

    def process_env(self) -> dict:
        return dict(os.environ)

    def services(self) -> list:
        return [{"id": "db", "readyCmd": "pg_isready"}] if self.podman_option.get("services") else []


def test_doctor_names_the_missing_command_and_its_declaration(make_layer, tmp_path) -> None:
    layer = _workflow_layer(make_layer)
    stack = _Stack(layer.ir["environments"][1], tmp_path,
                   "AL-DOCTOR\\tcommand:test\\tmiss\\tpytest not found on PATH\\nAL-DOCTOR\\tgit\\tok\\tfine\\n")
    checks = sandbox.doctor(layer, stack, layer.adw("demo"), fast=True)
    row = next(c for c in checks if c.name == "command:test")
    assert row.status == "miss" and "(environment podman #:id box" in row.fix and "(cmd test" in row.fix
    assert "`pytest`" in row.fix
    script = stack.scripts[0]
    assert "command -v pytest" in script and "command -v ruff" in script and "[ -x ./sync.sh ]" in script
    assert "/proc/1/comm" in script


def test_doctor_runs_its_script_with_sh_not_bash(make_layer, tmp_path) -> None:
    layer = _workflow_layer(make_layer)
    stack = _Stack(layer.ir["environments"][1], tmp_path, "AL-DOCTOR\\tgit\\tok\\tfine\\n")
    sandbox.doctor(layer, stack, layer.adw("demo"), fast=True)
    assert stack.argvs[0][:2] == ["sh", "-c"]
    assert stack.argvs[0][2].startswith("#!/bin/sh")


def test_doctor_never_prints_a_credential(make_layer, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")
    layer = _workflow_layer(make_layer)
    option = {**layer.ir["environments"][1], "agentInside": True, "credentials": ["claude"]}
    stack = _Stack(option, tmp_path, "AL-DOCTOR\\tagent\\tmiss\\tclaude starts, but no credential (ANTHROPIC_API_KEY)\\n")
    adw = {**layer.adw("demo"), "agent": "cc"}
    checks = sandbox.doctor(layer, stack, adw)
    script, table = stack.scripts[0], sandbox.format_table(checks)
    assert "ANTHROPIC_API_KEY" in script and "claude --version" in script
    assert "secret-value" not in script and "secret-value" not in table
    assert "#:credentials (claude)" in next(c for c in checks if c.name == "agent").fix


def test_doctor_reports_a_sidecar_that_never_becomes_ready(make_layer, tmp_path) -> None:
    layer = _workflow_layer(make_layer)
    stack = _Stack(layer.ir["environments"][1], tmp_path,
                   error=StepFailed("environment setup", "sidecar db not ready after 60s (readyCmd 'pg_isready')"))
    checks = sandbox.doctor(layer, stack, layer.adw("demo"), fast=True)
    assert checks[0].name == "service:db" and checks[0].status == "miss" and "(service container #:id db" in checks[0].fix
    assert {c.status for c in checks[1:]} == {"skip"}
    assert {"git", "init", "disk", "command:test"} <= {c.name for c in checks[1:]}


def test_doctor_script_of_a_scope_command_option_has_no_init_probe(make_layer, tmp_path) -> None:
    layer = _workflow_layer(make_layer)
    stack = _Stack({**layer.ir["environments"][1], "scope": "command", "caches": ["uv"]}, tmp_path)
    script = sandbox.doctor_script(stack, [], agent=None, browser=True)
    assert "/proc/1/comm" not in script and "r init skip" in script
    assert "/caches/uv" in script and "--dump-dom about:blank" in script


# ------------------------------------------------------------------ preflight through the runner

def _run_layer(make_layer, podman=None):
    environments = [_worktree_option()] + ([_podman_option(**podman)] if podman is not None else [])
    return _layer(make_layer, envs_=environments, **{
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"commands": [{"name": "test", "run": "pytest -q"}]},
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "auto", "environment": [e["id"] for e in environments],
                  "classes": ["chore"], "steps": [{"name": "plan", "kind": "run", "command": "plan"},
                                                  {"name": "test", "kind": "check", "cmd": "test"}]}]})


def _fake_container(monkeypatch) -> None:
    """The run's own podman calls (container start, exec, rm) succeed without podman."""
    real = subprocess.run

    def run(argv, **kwargs):
        if argv and argv[0] == "podman":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real(argv, **kwargs)
    monkeypatch.setattr(sandbox.subprocess, "run", run)


def test_preflight_fails_the_run_before_the_first_step(make_layer, monkeypatch) -> None:
    layer = _run_layer(make_layer, podman={})
    _fake_container(monkeypatch)
    monkeypatch.setattr(sandbox, "ensure_image", lambda layer, say=print: {"built": False})
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: [
        sandbox.Check("command:test", "miss", "pytest not found on PATH", "add the package that provides `pytest`"),
        sandbox.Check("git", "ok", "fine")])
    assert main("demo", layer, ["Do it", "--adw-id", ADW_ID]) == 1
    found = events.read(layer / "agents" / ADW_ID / "events.jsonl")
    assert not [e for e in found if e["type"] == "step.start"]
    (pre,) = [e for e in found if e["type"] == "env.preflight"]
    assert pre["status"] == "failed" and {"name": "command:test", "status": "miss",
                                          "detail": "pytest not found on PATH"} in pre["checks"]
    log = (layer / "agents" / ADW_ID / "run.log").read_text()
    assert "the sandbox is missing what this workflow needs" in log and "command:test | miss" in log
    state = json.loads((layer / "agents" / ADW_ID / "state.json").read_text())
    assert state["status"] == "failed" and state["steps"] == []


def test_preflight_honours_the_per_environment_switch(make_layer, monkeypatch) -> None:
    layer = _run_layer(make_layer, podman={"preflight": []})
    _fake_container(monkeypatch)
    monkeypatch.setattr(sandbox, "ensure_image", lambda layer, say=print: {"built": False})
    monkeypatch.setattr(sandbox, "needs_browser", lambda layer, adw: True)
    seen: list[dict] = []
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: seen.append(k) or [])
    main("demo", layer, ["Do it", "--adw-id", ADW_ID])
    assert seen and seen[0]["browser"] is False and seen[0]["services"] is False and seen[0]["fast"] is True
    assert [c["name"] for c in seen[0]["cmds"]] == ["test"]


def test_preflight_does_nothing_without_a_podman_environment(make_layer, monkeypatch) -> None:
    layer = _run_layer(make_layer)
    called: list[str] = []
    monkeypatch.setattr(sandbox, "ensure_image", lambda *a, **k: called.append("ensure"))
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: called.append("doctor") or [])
    main("demo", layer, ["Do it", "--adw-id", ADW_ID])
    assert called == []
    assert not [e for e in events.read(layer / "agents" / ADW_ID / "events.jsonl") if e["type"] == "env.preflight"]
    assert json.loads((layer / "agents" / ADW_ID / "state.json").read_text())["image"] == ""


def test_preflight_skips_a_workflow_that_runs_nothing_in_the_container(make_layer, monkeypatch) -> None:
    layer = _run_layer(make_layer, podman={})
    data = json.loads((layer / "agentic-layer.json").read_text())
    data["adws"][0]["steps"] = data["adws"][0]["steps"][:1]          # only an agent step, and the agent runs outside
    (layer / "agentic-layer.json").write_text(json.dumps(data))
    called: list[str] = []
    monkeypatch.setattr(sandbox, "ensure_image", lambda *a, **k: called.append("ensure"))
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: called.append("doctor") or [])
    assert main("demo", layer, ["Do it", "--adw-id", ADW_ID]) == 0
    assert called == []
    assert "preflight: skipped" in (layer / "agents" / ADW_ID / "run.log").read_text()


def test_preflight_records_the_image_in_state(make_layer, monkeypatch) -> None:
    layer = _run_layer(make_layer, podman={})
    _fake_container(monkeypatch)
    monkeypatch.setattr(sandbox, "ensure_image", lambda layer, say=print: {"built": False})
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: [sandbox.Check("git", "ok", "fine")])
    main("demo", layer, ["Do it", "--adw-id", ADW_ID])
    state = json.loads((layer / "agents" / ADW_ID / "state.json").read_text())
    assert state["image"] == sandbox.image_ref(load(layer))
    (pre,) = [e for e in events.read(layer / "agents" / ADW_ID / "events.jsonl") if e["type"] == "env.preflight"]
    assert pre["status"] == "passed"
    found = [e["type"] for e in events.read(layer / "agents" / ADW_ID / "events.jsonl")]
    assert found.index("env.preflight") < found.index("step.start")


def test_a_run_rebuilds_after_a_recipe_change_and_not_otherwise(make_layer, monkeypatch) -> None:
    layer = _run_layer(make_layer, podman={})
    fake = FakePodman(monkeypatch)
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: [])

    def run(adw_id: str):
        fake.calls.clear()
        main("demo", layer, ["Do it", "--adw-id", adw_id])
        found = events.read(layer / "agents" / adw_id / "events.jsonl")
        return fake.builds(), [e for e in found if e["type"].startswith("env.build")]

    first = sandbox.recipe_hash(load(layer))
    builds, found = run("5d5d0011")
    assert len(builds) == 1 and builds[0][2:4] == ["-t", f"al-test-layer:{first}"]
    assert [e["type"] for e in found] == ["env.build.start", "env.build.end"]
    assert found[-1]["status"] == "passed" and found[-1]["recipe"] == first

    (layer / "env" / "Containerfile").write_text("FROM debian:stable-slim\nRUN echo two\n", encoding="utf-8")
    second = sandbox.recipe_hash(load(layer))
    assert second != first

    builds, found = run("5d5d0012")
    assert len(builds) == 1 and builds[0][2:4] == ["-t", f"al-test-layer:{second}"]
    assert found[-1]["type"] == "env.build.end"
    assert found[-1]["status"] == "passed" and found[-1]["recipe"] == second

    builds, found = run("5d5d0013")
    assert builds == [] and found == []
    state = json.loads((layer / "agents" / "5d5d0013" / "state.json").read_text())
    assert state["image"] == f"al-test-layer:{second}"
