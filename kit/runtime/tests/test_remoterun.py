"""A run placed on another host: started there through the host's deployed layer, its record pulled
while it runs, its branch pushed to the git source, and the rest of the workflow (the gate, the
merge, the handoff) done here. Two directories play the hosts (remote_fixtures), local bare
repositories play the git sources, the agent is the mock. No ssh, no podman."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import remote_fixtures as rf
from adw_modules import events, hostlink, ir, remoterun, state as state_module
from adw_modules.runner import main

RUN = "5e1f0001"


@pytest.fixture
def placed(tmp_path, make_layer, app_repo, monkeypatch):
    """(layer, app, hosts): the home layer and application, and the homes of build-1 and build-2."""
    def build(ir_overrides=None, logins=(True, True)):
        layer = rf.build_layer_repo(tmp_path, make_layer, app_repo, ir_overrides)
        rf.sources(tmp_path, app_repo, layer)
        hosts = {name: rf.host_home(tmp_path, name, layer, login=login)
                 for name, login in zip(("build-1", "build-2"), logins)}
        for key, value in {**rf.home_env(tmp_path), **rf.local_hosts_env(tmp_path)}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(remoterun, "POLL", 0.2)
        return layer, app_repo, hosts
    return build


def _state(layer: Path, run: str = RUN) -> dict:
    return json.loads((layer / "agents" / run / "state.json").read_text())


def _events(layer: Path, run: str = RUN) -> list[dict]:
    return events.read(layer / "agents" / run / "events.jsonl")


def _hold_runs_slot(root: Path, index: int = 0):
    """Hold a `runs` slot of a host, as a run there would."""
    directory = rf.host_storage(root) / "slots"
    directory.mkdir(parents=True, exist_ok=True)
    handle = os.open(directory / f"runs.{index}.lock", os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_a_run_is_placed_on_the_less_loaded_host_and_merges_through_the_home_gate(placed, tmp_path):
    layer, app, hosts = placed(rf.demo_ir(hosts=["build-1", "build-2"]))
    source = tmp_path / "sources" / "app.git"
    main_before = rf.git(source, "rev-parse", "refs/heads/main")
    held = _hold_runs_slot(hosts["build-1"])
    seen: list[int] = []
    original = remoterun.pull

    def watching(layer_, state, link):   # the home record grows while the run is on the host
        result = original(layer_, state, link)
        path = layer / "agents" / RUN / "events.jsonl"
        seen.append(path.stat().st_size if path.exists() else 0)
        return result

    remoterun.pull = watching
    try:
        code = main("demo", layer, ["Add a feature", "--adw-id", RUN, "--class", "chore"])
    finally:
        remoterun.pull = original
        os.close(held)
    state = _state(layer)
    assert code == 0, (layer / "agents" / RUN / "run.log").read_text()
    assert state["status"] == "passed"
    assert state["remote"]["host"] == "build-2"
    assert state["ship"]["action"] == "merge"
    shipped = rf.git(app, "rev-parse", "al/ship/main")
    assert state["ship"]["commit"] == shipped == state["remote"]["delivered"]["sha"]
    assert rf.git(app, "show", "al/ship/main:feature.py") == "FEATURE = True"
    assert (layer / "agents" / RUN / "checks" / "gate-test.log").is_file()
    assert len(set(seen)) > 2, seen
    # the host pushed its own branch and nothing else; its storage for the run is gone
    assert rf.git(source, "rev-parse", "refs/heads/main") == main_before
    branch = state["remote"]["delivered"]["branch"]
    assert branch == f"chore/{RUN}-add-a-feature"
    assert rf.git(source, "rev-parse", f"refs/heads/{branch}") == shipped
    assert rf.git(source, "for-each-ref", "--format=%(refname)").split() == ["refs/heads/" + branch, "refs/heads/main"]
    assert not (rf.host_storage(hosts["build-2"]) / "runs" / RUN).exists()
    assert not (rf.host_storage(hosts["build-1"]) / "runs" / RUN).exists()
    types = [e["type"] for e in _events(layer)]
    for wanted in ("placement.decision", "remote.start", "remote.push", "remote.sync", "remote.takeover", "gate.decision"):
        assert wanted in types, types
    assert "host:      build-2" in (layer / "agents" / RUN / "run.log").read_text()


def _slow(seconds: float = 3.0, steps=None, **placement) -> dict:
    """The demo layer with a check that takes a while, so things can happen in the middle of a run."""
    data = rf.demo_ir(steps, hosts=["build-1", "build-2"], **placement)
    data["codebase"]["commands"][0]["run"] = f"sleep {seconds}; ./test.sh"
    return data


COMMIT = {"name": "commit", "kind": "run", "command": "commit"}
COMMIT_FIRST = [rf.STEPS[0], rf.STEPS[1], COMMIT, *rf.STEPS[2:]]      # the slow check runs after the commit is pushed
CHECK_FIRST = [rf.STEPS[0], rf.STEPS[1], rf.STEPS[2], COMMIT, *rf.STEPS[3:]]   # … or before anything is pushed


def _during(monkeypatch, when, then):
    """Call `then()` once, right after the first pull at which `when(home state)` holds."""
    original = remoterun.pull
    done: list[bool] = []

    def hooked(layer, state, link):
        result = original(layer, state, link)
        if not done:
            current = json.loads((Path(layer.layer_dir) / "agents" / state.adw_id / "state.json").read_text())
            if when(current):
                done.append(True)
                then()
        return result

    monkeypatch.setattr(remoterun, "pull", hooked)
    return done


def _passed(name: str):
    return lambda state: any(s["name"] == name and s["status"] == "passed" for s in state["steps"])


def test_the_home_record_equals_the_hosts_record(placed, tmp_path, monkeypatch):
    layer, _app, hosts = placed(rf.demo_ir(hosts=["build-1"]))
    kept = tmp_path / "kept"
    original = hostlink.Link.tool

    def keeping(self, layer_, host_id, verb, *args, **kw):   # the host's record, as it was when the home machine cleaned up
        if verb == "clean":
            import shutil
            shutil.copytree(rf.host_run_layer(hosts[host_id], RUN) / "agents" / RUN, kept)
        return original(self, layer_, host_id, verb, *args, **kw)

    monkeypatch.setattr(hostlink.Link, "tool", keeping)
    assert main("demo", layer, ["Add a feature", "--adw-id", RUN]) == 0
    home = layer / "agents" / RUN
    theirs = (kept / "events.jsonl").read_bytes()
    assert json.loads(theirs.splitlines()[-1])["type"] == "run.end"
    assert (home / "events.jsonl").read_bytes().startswith(theirs)
    assert (home / "run.log").read_bytes().startswith((kept / "run.log").read_bytes())
    compared = 0
    for path in sorted(kept.rglob("*")):
        relative = path.relative_to(kept)
        if path.is_dir() or relative.parts[0] in ("objects", "credentials") or relative.name in ("state.json", "events.jsonl", "run.log"):
            continue
        assert (home / relative).read_bytes() == path.read_bytes(), relative
        compared += 1
    assert compared >= 6 and (home / "plan" / "prompt.md").is_file() and (home / "checks" / "test.1.log").is_file()
    assert not (home / "credentials").exists() and not (home / "incoming.git").exists()
    seqs = [e["seq"] for e in _events(layer)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_a_dropped_connection_is_caught_up_without_duplicates(placed, monkeypatch):
    layer, _app, _hosts = placed(_slow(2.0))
    original = hostlink.Link.tool
    pulls: list[int] = []

    def dropping(self, layer_, host_id, verb, *args, **kw):
        if verb == "pull":
            pulls.append(1)
            if len(pulls) in (3, 4, 5):
                raise hostlink.HostSilent(host_id, "connection reset")
            if len(pulls) == 6:   # an answer that was sent and never arrived
                original(self, layer_, host_id, verb, *args, **kw)
                raise hostlink.HostSilent(host_id, "connection reset")
        return original(self, layer_, host_id, verb, *args, **kw)

    monkeypatch.setattr(hostlink.Link, "tool", dropping)
    assert main("demo", layer, ["Add a feature", "--adw-id", RUN, "--on", "build-1"]) == 0
    assert len(pulls) > 6
    seqs = [e["seq"] for e in _events(layer)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert _state(layer)["status"] == "passed"


def test_a_host_that_vanishes_mid_run_is_marked_lost_with_what_was_last_known(placed, monkeypatch, capsys):
    layer, app, hosts = placed(_slow(4.0))
    monkeypatch.setenv("AL_REMOTE_LOST_SECONDS", "2")
    done = _during(monkeypatch, _passed("build"), lambda: rf.vanish(hosts["build-1"]))
    assert main("demo", layer, ["Add a feature", "--adw-id", RUN, "--on", "build-1"]) == 1
    assert done
    state = _state(layer)
    assert state["status"] == "lost" and state["ended"]
    assert state["remote"]["host"] == "build-1" and state["remote"]["lost"]["at"]
    assert "gave no answer" in state["remote"]["lost"]["reason"]
    assert [s["name"] for s in state["steps"] if s["status"] == "passed"][:2] == ["plan", "build"]
    lost = [e for e in _events(layer) if e["type"] == "remote.lost"]
    assert len(lost) == 1 and lost[0]["host"] == "build-1"
    assert "--restart --adw-id " + RUN in capsys.readouterr().out
    assert subprocess.run(["git", "-C", str(app), "rev-parse", "--verify", "--quiet", "al/ship/main"],
                          capture_output=True).returncode != 0


@pytest.mark.parametrize("pushed", [True, False])
def test_a_lost_run_restarts_elsewhere_from_its_last_pushed_state(placed, monkeypatch, pushed):
    layer, app, hosts = placed(_slow(3.0, COMMIT_FIRST if pushed else CHECK_FIRST))
    rf.script(layer, "commit", {"commit": True})
    rf.commit_all(layer, "the commit step commits")
    rf.sources(layer.parent, app, layer)
    for root in hosts.values():
        rf.git(root / "al" / "layer", "pull", "-q", "origin", "main")
    monkeypatch.setenv("AL_REMOTE_LOST_SECONDS", "2")
    when = (lambda s: bool((s.get("remote") or {}).get("pushed"))) if pushed else _passed("plan")
    _during(monkeypatch, when, lambda: rf.vanish(hosts["build-1"]))
    assert main("demo", layer, ["Add a feature", "--adw-id", RUN, "--on", "build-1"]) == 1
    lost = _state(layer)
    assert lost["status"] == "lost" and bool(lost["remote"].get("pushed")) is pushed
    before = len(_events(layer))
    assert main("demo", layer, ["--restart", "--adw-id", RUN]) == 0
    state = _state(layer)
    assert state["status"] == "passed" and state["remote"]["host"] == "build-2"
    assert state["remote"]["restart_of"] == "build-1"
    assert rf.git(app, "show", "al/ship/main:feature.py") == "FEATURE = True"
    again = [e["step"] for e in _events(layer)[before:] if e["type"] == "step.start"]
    if pushed:
        assert again == ["test", "review", "ship"]
        assert rf.git(app, "merge-base", "--is-ancestor", lost["remote"]["pushed"]["sha"], "al/ship/main") == ""
    else:
        assert again == ["plan", "build", "test", "commit", "review", "ship"]
    seqs = [e["seq"] for e in _events(layer)]
    assert len(seqs) == len(set(seqs))


def _spawn_run(layer: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(layer / "adws" / "adw_demo.py"), *args], cwd=layer,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)


def _wait_for(condition, seconds: float = 60.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_follow_reattaches_after_the_proxy_died(placed):
    layer, app, _hosts = placed(_slow(3.0))
    proxy = _spawn_run(layer, "Add a feature", "--adw-id", RUN, "--on", "build-1")

    def plan_passed():
        path = layer / "agents" / RUN / "state.json"
        try:
            return _passed("plan")(json.loads(path.read_text()))
        except (OSError, ValueError):
            return False

    _wait_for(plan_passed)
    os.killpg(proxy.pid, signal.SIGKILL)
    proxy.wait(timeout=30)
    assert _state(layer)["status"] == "running"
    assert main("demo", layer, ["--follow", "--adw-id", RUN]) == 0
    state = _state(layer)
    assert state["status"] == "passed" and state["remote"]["host"] == "build-1"
    assert rf.git(app, "show", "al/ship/main:feature.py") == "FEATURE = True"
    seqs = [e["seq"] for e in _events(layer)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_a_failed_remote_run_is_handed_over_at_home(placed, tmp_path):
    data = rf.demo_ir(hosts=["build-1"])
    data["codebase"]["commands"][0]["run"] = "echo the tests are broken; exit 1"
    layer, app, hosts = placed(data)
    assert main("demo", layer, ["Add a feature", "--adw-id", RUN]) == 1
    state = _state(layer)
    assert state["status"] == "failed"
    assert state["ship"]["action"] == "handoff" and state["ship"]["stopped_at"] == "test"
    document = (layer / "agents" / RUN / "PR.md").read_text()
    assert "needs human" in document and "the tests are broken" in document
    assert str(hosts["build-1"]) not in document
    branch = state["remote"]["delivered"]["branch"]
    assert rf.git(app, "rev-parse", f"refs/heads/{branch}") == state["remote"]["delivered"]["sha"]
    assert rf.git(app, "show", f"{branch}:feature.py") == "FEATURE = True"
    assert subprocess.run(["git", "-C", str(app), "rev-parse", "--verify", "--quiet", "al/ship/main"],
                          capture_output=True).returncode != 0
    types = [e["type"] for e in _events(layer)]
    assert "remote.takeover" in types and "handoff" in types


def test_a_pushed_branch_that_is_not_the_delivered_commit_is_refused(placed, tmp_path, monkeypatch):
    layer, app, _hosts = placed(rf.demo_ir(hosts=["build-1"]))
    source = tmp_path / "sources" / "app.git"

    def move_branch():
        branch = f"chore/{RUN}-add-a-feature"
        rf.git(source, "update-ref", f"refs/heads/{branch}", rf.git(source, "rev-parse", "refs/heads/main"))

    _during(monkeypatch, lambda s: bool((s.get("remote") or {}).get("delivered")), move_branch)
    assert main("demo", layer, ["Add a feature", "--adw-id", RUN]) == 1
    state = _state(layer)
    assert state["status"] == "failed"
    assert "the pushed branch is not the commit the run delivered" in (layer / "agents" / RUN / "run.log").read_text()
    assert subprocess.run(["git", "-C", str(app), "rev-parse", "--verify", "--quiet", "al/ship/main"],
                          capture_output=True).returncode != 0
    assert not state["ship"].get("shipped")


def test_per_host_limits(placed):
    layer, _app, hosts = placed(_slow(1.5, maxRunsPerHost=1))
    runs = ["5e1f00a1", "5e1f00a2", "5e1f00a3"]
    processes = [_spawn_run(layer, f"Add feature {run[-1]}", "--adw-id", run, "--on", "build-1") for run in runs]
    outputs = [p.communicate(timeout=180)[0] for p in processes]
    assert [p.returncode for p in processes] == [0, 0, 0], "\n".join(outputs)
    held = os.listdir(rf.host_storage(hosts["build-1"]) / "slots")   # one directory for every run of the host
    assert "runs.0.lock" in held and "runs.1.lock" not in held and "checks.0.lock" in held
    spans, waited = [], 0
    for run in runs:
        remote_part = []
        for event in _events(layer, run):
            if event["type"] == "remote.sync":
                break
            remote_part.append(event)
        acquire = next(e for e in remote_part if e["type"] == "slot.acquire" and e["pool"] == "runs")
        release = next(e for e in remote_part if e["type"] == "slot.release" and e["pool"] == "runs")
        assert acquire["slot"] == 0
        spans.append((acquire["ts"], release["ts"]))
        waited += any(e["type"] == "slot.wait" and e["pool"] == "runs" and e["limit"] == 1 for e in remote_part)
    spans.sort()
    assert all(earlier[1] <= later[0] for earlier, later in zip(spans, spans[1:])), spans
    assert waited == 2


def test_a_rejected_review_on_the_host_becomes_a_handover_at_home(placed):
    layer, app, _hosts = placed(rf.demo_ir(hosts=["build-1"]))
    rf.script(layer, "review-1", {"output": json.dumps({"success": False, "summary": "not good enough", "issues": [
        {"severity": "blocker", "description": "the feature flag is never read", "resolution": "read it"}]})})
    rf.commit_all(layer, "a strict reviewer")
    rf.sources(layer.parent, app, layer)
    rf.git(layer.parent / "hosts" / "build-1" / "al" / "layer", "pull", "-q", "origin", "main")
    assert main("demo", layer, ["Add a feature", "--adw-id", RUN]) == 0
    state = _state(layer)
    assert state["status"] == "handed-over"
    assert "review-rejected" in state["ship"]["handed_over"]
    assert state["ship"]["action"] == "pr" and state["ship"]["shipped"] is False
    assert (layer / "agents" / RUN / "PR.md").is_file()
    assert [s["status"] for s in state["steps"] if s["name"] == "review"] == ["rejected"]
    assert subprocess.run(["git", "-C", str(app), "rev-parse", "--verify", "--quiet", "al/ship/main"],
                          capture_output=True).returncode != 0


def test_the_delivered_commit_enters_a_sandboxed_run_through_the_quarantine(placed, tmp_path):
    """A stack with a container: the fetched objects go to the run's quarantine (never straight into
    the application), the worktree stays detached, and the unchanged import of the ship step takes
    them from there. No container is started for this."""
    from adw_modules import envs, gitsafe

    data = rf.demo_ir(hosts=["build-1"])
    data["environments"].append({"id": "box", "kind": "podman", "status": "stable", "agentInside": False})
    data["adws"][0]["environment"] = ["worktree", "box"]
    layer_dir, app, _hosts = placed(data)
    layer = ir.load(layer_dir)
    source = tmp_path / "sources" / "app.git"
    branch = f"chore/{RUN}-add-a-feature"
    elsewhere = tmp_path / "elsewhere"                      # where the run worked: another clone of the source
    subprocess.run(["git", "clone", "-q", str(source), str(elsewhere)], check=True)
    (elsewhere / "feature.py").write_text("FEATURE = True\n")
    sha = rf.commit_all(elsewhere, "the run's work")
    rf.git(elsewhere, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    state = state_module.ADWState(adw_id=RUN, adw="demo", prompt="Add a feature")
    state.remote = {"host": "build-1", "phase": "gate", "status": "delivered",
                    "delivered": {"sha": sha, "branch": branch, "step": "ship"}}
    stack = envs.build(layer, state, ["worktree", "box"])
    stack.setup()
    try:
        assert stack.sandbox is not None
        with events.recording(layer.layer_dir, RUN):
            remoterun.place_result(layer, state, stack)
        missing = subprocess.run(["git", "-C", str(app), "cat-file", "-e", sha], capture_output=True)
        assert missing.returncode != 0                                              # not in the application yet
        assert list((stack.sandbox.quarantine / "pack").glob("*.pack"))             # in the run's quarantine
        assert gitsafe.run(stack.sandbox, ["rev-parse", "HEAD"], "test").stdout.strip() == sha
        assert (Path(state.worktree) / "feature.py").read_text() == "FEATURE = True\n"
        assert state.branch == branch and not (layer.layer_dir / "agents" / RUN / "incoming.git").exists()
        assert subprocess.run(["git", "-C", str(app), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                              capture_output=True).returncode != 0                  # no ref of the application moved
        assert gitsafe.import_objects(stack.sandbox, "HEAD", "ship") == sha         # what ship.ship does next
        assert subprocess.run(["git", "-C", str(app), "cat-file", "-e", sha], capture_output=True).returncode == 0
        # a commit that is not the delivered one never gets this far
        rf.git(source, "update-ref", f"refs/heads/{branch}", rf.git(source, "rev-parse", "refs/heads/main"))
        with pytest.raises(Exception, match="the pushed branch is not the commit the run delivered"):
            remoterun.fetch_result(layer, state, stack)
    finally:
        stack.keep_worktree = False
        stack.teardown(True)
