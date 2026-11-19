"""The acceptance scenario through `tools/al`: runs of one layer execute on the home machine and on
two other hosts, are seen on one dashboard model while they run, and merge through the home gate.
The hosts are the fake remote of the deploy tests (deploy_host.Host, reached through the runtime's
local link); a local bare repository is the git source; the agent is the mock (--dry-run)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

from conftest import AL, KIT, al, commit, git

SENTENCE = ("host build-1 has no usable login for claude-code: log in on it, or name a token (ANTHROPIC_API_KEY or "
            "CLAUDE_CODE_OAUTH_TOKEN) in its env file; see: al host LAYER build-1 doctor")


def _dashboard():
    spec = importlib.util.spec_from_file_location("al_dashboard_for_placement", KIT / "dashboard" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _states(layer: Path) -> dict[str, dict]:
    found = {}
    for path in sorted((layer / "agents").glob("*/state.json")):
        try:
            found[path.parent.name] = json.loads(path.read_text())
        except ValueError:
            pass
    return found


def _host_of(state: dict) -> str:
    return (state.get("remote") or {}).get("host") or "home"


def _start(layer: Path, env: dict, prompt: str, *args: str) -> subprocess.Popen:
    import os
    full = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    full.update(env)
    return subprocess.Popen([str(AL), "run", str(layer), "/change", prompt, "--yes", *args], env=full,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_three_runs_started_at_once_land_on_three_hosts_and_merge_at_home(placed):
    layer, hosts, source, env = placed
    app = layer.parent
    # long enough to be seen running; it leaves no file behind, so the three runs change nothing and their
    # merges cannot race each other (two gates that both moved main would: the second falls back to a person)
    (app / "test.sh").write_text("#!/bin/sh\nsleep 4\nPYTHONDONTWRITEBYTECODE=1 python3 -c 'import app'\n")
    commit(app, "a check that takes a moment", "test.sh")
    git(app, "push", "-q", "origin", "main")
    main_before = git(source, "rev-parse", "refs/heads/main")
    dashboard = _dashboard()
    model = dashboard.Model([layer], [])
    model.discover()
    processes = [_start(layer, env, f"tidy the notes, part {n}", "--dry-run", "--class", "chore") for n in (1, 2, 3)]
    seen: dict[str, str] = {}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and any(p.poll() is None for p in processes):
        model.tick()
        running = {key.split("/")[1]: summary["host"] for key, summary in model.summaries.items()
                   if summary["status"] == "running" and (summary.get("current") or {}).get("step")}
        if len(running) == 3 and len(set(running.values())) == 3:
            seen = running
            break
        time.sleep(0.3)
    outputs = [p.communicate(timeout=300)[0] for p in processes]
    assert [p.returncode for p in processes] == [0, 0, 0], "\n".join(outputs)
    # one dashboard model saw the three of them running, each on its own host, each at a step
    assert sorted(seen.values()) == ["build-1", "build-2", "home"], (seen, outputs)
    states = _states(layer)
    assert len(states) == 3
    assert sorted(_host_of(s) for s in states.values()) == ["build-1", "build-2", "home"]
    assert {run: _host_of(s) for run, s in states.items()} == seen
    for state in states.values():
        assert state["status"] == "passed", state
        assert state["ship"]["action"] == "merge" and state["ship"]["shipped"] is True
    model.tick()
    hosts_panel = {h["host"]: h for h in next(l for l in model.layer_list() if l["name"] == "notes")["hosts"]}
    assert list(hosts_panel) == ["home", "build-1", "build-2"] and all(h["state"] == "ok" for h in hosts_panel.values())
    # the hosts pushed their own run branches and nothing else; only this machine pushes main
    branches = set(git(source, "for-each-ref", "--format=%(refname:short)").split())
    remote_runs = {run: s for run, s in states.items() if s.get("remote")}
    assert branches == {"main"} | {s["remote"]["delivered"]["branch"] for s in remote_runs.values()}
    assert git(source, "rev-parse", "refs/heads/main") == main_before == git(app, "rev-parse", "refs/heads/main")
    for run, state in remote_runs.items():
        assert state["remote"]["delivered"]["branch"].startswith(f"chore/{run}-")
        assert not (hosts[state["remote"]["host"]].root / ".local" / "state" / "al" / "notes" / "runs" / run).exists()
        assert [e for e in map(json.loads, (layer / "agents" / run / "events.jsonl").read_text().splitlines())
                if e["type"] == "gate.decision"]                       # the gate ran here
    for host in hosts.values():
        assert not [line for line in host.log("git") if "refs/heads/main" in line and "push" in line]


def test_on_picks_the_host(placed):
    layer, hosts, source, env = placed
    app = layer.parent
    mock = layer / "mock" / "change" / "build.json"
    mock.parent.mkdir(parents=True)
    mock.write_text(json.dumps({"files": {"notes.txt": "a tidy note\n"}}))
    (app / "test.sh").write_text("#!/bin/sh\nPYTHONDONTWRITEBYTECODE=1 python3 -c 'import app'\n")
    commit(app, "what the mock agent builds", "agentic/mock", "test.sh")
    git(app, "push", "-q", "origin", "main")
    main_before = git(source, "rev-parse", "refs/heads/main")
    cp = al("run", layer, "/change", "add a tidy note", "--yes", "--dry-run", "--class", "chore", "--on", "build-2", env=env)
    (run, state), = _states(layer).items()
    assert state["status"] == "passed" and state["remote"]["host"] == "build-2" and state["remote"]["phase"] == "gate"
    assert "placed on host build-2: named with --on" in cp.stdout
    assert f"host:      build-2 (delivered chore/{run}-add-a-tidy-note at " in cp.stdout and "gate on home" in cp.stdout
    assert state["ship"]["shipped"] is True and state["ship"]["target"] == "origin/main"
    # the change is on the source's main, put there by this machine's gate; the host pushed its branch only
    assert git(source, "rev-parse", "refs/heads/main") != main_before
    assert git(source, "show", "main:notes.txt") == "a tidy note"
    delivered = state["remote"]["delivered"]
    assert git(source, "rev-parse", f"refs/heads/{delivered['branch']}") == delivered["sha"]
    assert git(source, "merge-base", "--is-ancestor", delivered["sha"], "refs/heads/main") == ""
    assert (layer / "agents" / run / "checks" / "gate-test.log").is_file()
    assert not (hosts["build-2"].root / ".local" / "state" / "al" / "notes" / "runs" / run).exists()
    assert not (hosts["build-1"].root / ".local" / "state").exists()        # the other host was never used
    # an unknown host is a usage error, before anything starts
    bad = al("run", layer, "/change", "another note", "--yes", "--dry-run", "--on", "build-9", env=env, ok=False)
    assert bad.returncode == 2 and "build-9 is not a host of the layer's (placement …); hosts: home, build-1, build-2" in bad.stderr
    assert len(_states(layer)) == 1


def test_on_a_host_without_login_is_refused_before_it_starts(placed):
    layer, hosts, _source, env = placed
    (hosts["build-1"].root / ".claude" / ".credentials.json").unlink()
    cp = al("run", layer, "/change", "tidy the notes", "--yes", "--class", "chore", "--on", "build-1", env=env, ok=False)
    assert cp.returncode == 3, cp.stdout + cp.stderr
    assert f"refused: {SENTENCE}" in cp.stderr
    assert not (hosts["build-1"].root / ".local" / "state").exists()            # no run directory on the host
    (run, state), = _states(layer).items()
    assert state["status"] == "failed" and not state.get("remote") and state["steps"] == []
    assert not (layer / "trees" / run).exists()


KEYS = ["adw", "adw_id", "agent_calls", "artefacts", "branch", "budget", "check_results", "checks", "class_fixed", "cost", "definition",
        "dry_run", "ended", "fanout", "fanout_parent", "heartbeat", "host", "image", "klass", "model_set", "models",
        "notified", "outputs", "overrides", "pid", "plan", "ports", "prompt", "review", "scripts", "ship", "source",
        "started", "status", "steps", "usage", "worktree"]
TYPES = ["agent.end", "agent.start", "check.end", "check.start", "gate.decision", "run.end", "run.start", "ship.result",
         "slot.acquire", "slot.release", "step.discipline", "step.end", "step.start"]
SUMMARY = ["adw", "status", "branch", "tiers", "plan", "checks", "review", "cost", "models", "ship", "artefacts", "state"]


def test_a_layer_without_placement_runs_as_before(in_repo):
    """The lists above were taken from a run of this layer on `main` before placement existed."""
    cp = al("run", in_repo, "/change", "tidy the notes", "--dry-run")
    (run, state), = _states(in_repo).items()
    assert sorted(state) == KEYS
    events = [json.loads(line) for line in (in_repo / "agents" / run / "events.jsonl").read_text().splitlines()]
    assert sorted({e["type"] for e in events}) == TYPES
    assert [line.split(":")[0] for line in cp.stdout.splitlines() if line.split(":")[0] in (*SUMMARY, "host")] == SUMMARY
    assert sorted(p.name for p in (in_repo / "agents").iterdir() if p.name != run) == [
        ".ports.json", ".ports.lock", ".slots", ".starts.lock", "starts.json"]      # no hosts.json, no .placement.lock
    assert not (in_repo / "adws" / "host.py").exists()
    assert "placement" not in json.loads((in_repo / "agentic-layer.json").read_text())
