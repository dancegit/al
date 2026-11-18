"""Where a run executes: the groundwork (the `remote` block of a run's state, per-host slot pools),
reaching a host (hostlink) and the scheduler (placement): survey, choice, pins, reservations, the
login a host needs, and the table a person reads. Hosts answer through fake links; no ssh."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import remote_fixtures as rf
from adw_modules import events, hostlink, ir, placement, selfrun, slots, state as state_module
from adw_modules.errors import LayerError, StepFailed

RUN = "91ac0001"


# ---------------------------------------------------------------- state.remote

def test_state_json_has_no_remote_key_for_a_local_run(tmp_path):
    state = state_module.ADWState(adw_id=RUN, adw="demo")
    data = json.loads(state.save(tmp_path).read_text())
    assert "remote" not in data and "delivery" not in data and "promotion" not in data


def test_remote_survives_save_and_load(tmp_path):
    state = state_module.ADWState(adw_id=RUN, adw="demo")
    state.remote = {"host": "build-1", "phase": "remote", "delivered": {"sha": "1" * 40, "branch": f"chore/{RUN}-x"}}
    state.save(tmp_path)
    assert json.loads((tmp_path / "agents" / RUN / "state.json").read_text())["remote"]["host"] == "build-1"
    assert state_module.load(tmp_path, RUN).remote == state.remote


def test_the_new_event_types_are_declared():
    for name in ("placement.decision", "remote.start", "remote.sync", "remote.push", "remote.lost", "remote.takeover"):
        assert name in events.TYPES


# ---------------------------------------------------------------- slots

def _layer(directory: Path, **ir_fields) -> SimpleNamespace:
    directory.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(layer_dir=directory, ir={"concurrency": {"maxRuns": 1, "maxAgents": 2, "maxChecks": 2,
                                                                  "waitSeconds": 1}, **ir_fields})


def test_slots_use_the_host_directory_when_named(tmp_path, monkeypatch):
    one, two = _layer(tmp_path / "run-1" / "layer"), _layer(tmp_path / "run-2" / "layer")
    shared = tmp_path / "host" / "slots"
    monkeypatch.setenv("AL_SLOTS_DIR", str(shared))
    with slots.slot(one, "runs"):
        assert (shared / "runs.0.lock").is_file() and not (one.layer_dir / "agents" / ".slots").exists()
        assert slots.free_slots(two, "runs") == 0
        with pytest.raises(StepFailed, match="no free runs slot"):   # the second run of the host waits, then gives up
            with slots.slot(two, "runs", poll=0.05):
                pass
    with slots.slot(two, "runs"):
        pass
    monkeypatch.delenv("AL_SLOTS_DIR")                               # without it: each layer directory has its own
    with slots.slot(one, "runs"), slots.slot(two, "runs"):
        assert (one.layer_dir / "agents" / ".slots" / "runs.0.lock").is_file()


def test_max_runs_per_host_replaces_max_runs(tmp_path):
    layer = _layer(tmp_path / "layer", placement={"hosts": ["home"], "maxRunsPerHost": 1})
    layer.ir["concurrency"]["maxRuns"] = 4
    assert slots.limits(layer)["runs"] == 1
    with slots.slot(layer, "runs"):
        assert slots.free_slots(layer, "runs") == 0
    layer.ir["placement"]["maxRunsPerHost"] = 0                      # 0: (concurrency #:max-runs) on each host
    assert slots.limits(layer)["runs"] == 4


def test_limits_are_unchanged_without_placement(tmp_path):
    layer = _layer(tmp_path / "layer")
    layer.ir["concurrency"] = {"maxRuns": 3, "maxAgents": 5, "maxChecks": 7, "waitSeconds": 11}
    assert slots.limits(layer) == {"runs": 3, "agents": 5, "checks": 7, "wait": 11}
    assert slots.limits(SimpleNamespace(layer_dir=tmp_path, ir={})) == {"runs": 4, "agents": 2, "checks": 2, "wait": 3600}
    assert slots._directory(layer) == layer.layer_dir / "agents" / ".slots"


# ---------------------------------------------------------------- hostlink

@pytest.fixture
def plain(tmp_path, make_layer, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for name in ("AL_HOSTS_LOCAL", "AL_HOST_BUILD_1", "AL_HOST_BUILD_1_LAYER"):
        monkeypatch.delenv(name, raising=False)
    return ir.load(make_layer(rf.demo_ir()))


def test_link_address_comes_from_the_environment_or_local_env_never_the_ir(plain, monkeypatch, tmp_path):
    assert hostlink.env_name("build-1") == "AL_HOST_BUILD_1"
    assert "builder.example.org" not in json.dumps(plain.ir)
    (plain.layer_dir / "local.env").write_text("AL_HOST_BUILD_1=deploy@builder.example.org\n")
    link = hostlink.link_for(plain, "build-1")
    assert isinstance(link, hostlink.SshLink) and link.destination == "deploy@builder.example.org"
    monkeypatch.setenv("AL_HOST_BUILD_1", "other-alias")                   # the environment wins
    assert hostlink.link_for(plain, "build-1").destination == "other-alias"
    monkeypatch.delenv("AL_HOST_BUILD_1")
    (plain.layer_dir / "local.env").unlink()
    config = tmp_path / "xdg" / "al" / "test-layer.env"
    config.parent.mkdir(parents=True)
    config.write_text("AL_HOST_BUILD_1=from-the-config-file\n")
    assert hostlink.link_for(plain, "build-1").destination == "from-the-config-file"
    # where the layer is on the host: what al deploy creates, unless the variable says otherwise
    assert hostlink.host_layer_dir(plain, "build-1") == "al/layer"
    in_repo = ir.Layer({**plain.ir, "layer": {"kind": "in-repo", "dir": "agentic"}}, tmp_path / "notes" / "agentic", tmp_path / "notes")
    assert hostlink.host_layer_dir(in_repo, "build-1") == "al/notes/agentic"      # no repository there: the directory's name
    subprocess.run(["git", "init", "-q", str(tmp_path / "notes")], check=True)
    subprocess.run(["git", "-C", str(tmp_path / "notes"), "remote", "add", "origin", "git@forge.example.org:team/notebook.git"], check=True)
    assert hostlink.host_layer_dir(in_repo, "build-1") == "al/notebook/agentic"   # as al deploy names the checkout
    monkeypatch.setenv("AL_HOST_BUILD_1_LAYER", "/srv/notes/agentic")
    assert hostlink.host_layer_dir(in_repo, "build-1") == "/srv/notes/agentic"
    with pytest.raises(LayerError):
        hostlink.link_for(plain, "home")


def test_a_host_without_an_address_is_reported_by_variable_name(plain):
    with pytest.raises(hostlink.HostSilent) as caught:
        hostlink.link_for(plain, "build-1")
    assert caught.value.host_id == "build-1"
    assert "set AL_HOST_BUILD_1 in the environment, in local.env" in caught.value.reason


def test_local_link_runs_in_the_host_home_with_its_path(plain, tmp_path, monkeypatch):
    root = tmp_path / "hosts" / "build-1"
    (root / ".hostbin").mkdir(parents=True)
    for tool in ("sh", "pwd", "cat", "sleep"):
        (root / ".hostbin" / tool).symlink_to(subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip())
    (root / "bin").mkdir()
    (root / "bin" / "uv").write_text('#!/bin/sh\necho "{\\"protocol\\": 1, \\"cwd\\": \\"$(pwd)\\", \\"args\\": \\"$*\\", \\"in\\": \\"$(cat)\\"}"\n')
    (root / "bin" / "uv").chmod(0o755)
    (root / "al" / "layer").mkdir(parents=True)
    monkeypatch.setenv("AL_HOSTS_LOCAL", str(tmp_path / "hosts"))
    link = hostlink.link_for(plain, "build-1")
    assert isinstance(link, hostlink.LocalLink)
    done = link.run('echo "$HOME|$PATH|$(pwd)"')
    assert done.stdout.decode().strip() == f"{root}|{root}/.hostbin:{root}/bin|{root}"
    answer = link.tool(plain, "build-1", "pull", "5e1f0001", stdin="the cursor")
    assert answer == {"protocol": 1, "cwd": str(root / "al" / "layer"),
                      "args": "run --quiet adws/host.py pull 5e1f0001", "in": "the cursor"}
    assert link.tool(plain, "build-1", "load", raw=True).startswith(b'{"protocol": 1')
    # an older layer, an answer that is not JSON, a failing tool, a host that is gone: all silent, with a reason
    (root / "bin" / "uv").write_text('#!/bin/sh\necho \'{"protocol": 0}\'\n')
    with pytest.raises(hostlink.HostSilent, match="its layer is older than this one: al deploy it again"):
        link.tool(plain, "build-1", "load")
    (root / "bin" / "uv").write_text("#!/bin/sh\necho hello\n")
    with pytest.raises(hostlink.HostSilent, match="gave no JSON answer"):
        link.tool(plain, "build-1", "load")
    (root / "bin" / "uv").write_text("#!/bin/sh\necho 'no such layer' >&2\nexit 1\n")
    with pytest.raises(hostlink.HostSilent, match="adws/host.py load failed: no such layer"):
        link.tool(plain, "build-1", "load")
    (root / "bin" / "uv").write_text("#!/bin/sh\nsleep 5\n")
    with pytest.raises(hostlink.HostSilent, match="no answer within 1 s"):
        link.tool(plain, "build-1", "load", timeout=1)
    rf.vanish(root)
    with pytest.raises(hostlink.HostSilent):
        link.tool(plain, "build-1", "load")


def test_ssh_link_argv():
    link = hostlink.SshLink("deploy@builder", "build-1", connect_timeout=7)
    script = "cd al/notes/agentic && uv run --quiet adws/host.py load"
    assert link.argv(script) == ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7", "deploy@builder",
                                 "sh -lc 'cd al/notes/agentic && uv run --quiet adws/host.py load'"]
    assert hostlink.SshLink("builder").argv("true")[5] == f"ConnectTimeout={hostlink.CONNECT_TIMEOUT}"
    for bad in ("-oProxyCommand=evil", "", "two words"):
        with pytest.raises(LayerError, match="is not a host"):
            hostlink.SshLink(bad, "build-1")


# ---------------------------------------------------------------- the scheduler

def load(runs=0, max_runs=4, load1=0.5, cpus=8, image=None, logins=None) -> dict:
    return {"protocol": selfrun.PROTOCOL, "runs": runs, "max_runs": max_runs, "cpus": cpus, "load1": load1,
            "mem_total_kb": 16_000_000, "mem_available_kb": 9_000_000, "disk_free_kb": 120_000_000, "image": image,
            "logins": logins or {"mock": "not needed"}}


class FakeLinks:
    """link(layer, host) for placement.survey: every host answers with the load given for it, or is silent."""

    def __init__(self, **loads) -> None:
        self.loads = {name.replace("_", "-"): value for name, value in loads.items()}
        self.asked: list[str] = []

    def __call__(self, layer, host_id):
        links = self

        class Link:
            def tool(self, layer, host, verb, *args, **kw):
                assert verb == "load" and kw.get("timeout") == placement.SURVEY_TIMEOUT
                links.asked.append(host)
                answer = links.loads[host]
                if isinstance(answer, str):
                    raise hostlink.HostSilent(host, answer)
                return answer
        return Link()


@pytest.fixture
def home(tmp_path, make_layer, app_repo, monkeypatch):
    """The home layer of a placement, committed and at the git source; home's own load is fixed."""
    def build(ir_overrides=None, **placement_fields):
        data = ir_overrides or rf.demo_ir(**placement_fields)
        layer_dir = rf.build_layer_repo(tmp_path, make_layer, app_repo, data)
        rf.sources(tmp_path, app_repo, layer_dir)
        for key, value in rf.home_env(tmp_path).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(placement, "home_load", lambda layer: load())
        return ir.load(layer_dir)
    return build


def run_state(run: str = RUN, klass: str = "chore", fixed: bool = True, dry_run: bool = False):
    return state_module.ADWState(adw_id=run, adw="demo", klass=klass, class_fixed=fixed, dry_run=dry_run)


def decide(layer, links, state=None, **kw):
    return placement.decide(layer, layer.adw("demo"), state or run_state(), link=links, **kw)


def test_declared_is_none_without_a_placement(home):
    layer = home()
    assert placement.declared(layer) == {"hosts": ["home", "build-1", "build-2"], "prefer": "least-loaded", "max_runs": 0,
                                         "reserve": None, "source_env": "AL_GIT_SOURCE", "lost_after": 120}
    without = ir.Layer({k: v for k, v in layer.ir.items() if k != "placement"}, layer.layer_dir, layer.app_root)
    assert placement.declared(without) is None
    with pytest.raises(LayerError):
        placement.survey(without)


def test_least_loaded_picks_the_host_with_the_lowest_share_of_runs(home):
    layer = home(hosts=["build-1", "build-2"])
    links = FakeLinks(build_1=load(runs=2, max_runs=4), build_2=load(runs=1, max_runs=4))
    decision = decide(layer, links)
    assert decision["host"] == "build-2" and decision["skipped"] == []
    assert decision["reasons"] == ["least-loaded: 1 of 4 runs in use"]
    assert sorted(links.asked) == ["build-1", "build-2"]                 # one call per host
    # a share, not a count: 3 of 8 is less than 2 of 4
    assert decide(layer, FakeLinks(build_1=load(runs=3, max_runs=8), build_2=load(runs=2, max_runs=4)),
                  run_state("91ac0002"))["host"] == "build-1"


def test_ties_go_to_load_then_image_then_declared_order(home):
    layer = home(hosts=["build-1", "build-2"])
    by_load = FakeLinks(build_1=load(load1=4.0, cpus=4), build_2=load(load1=4.0, cpus=16))
    assert decide(layer, by_load, run_state("91ac0011"))["host"] == "build-2"
    (layer.layer_dir / "agents" / "hosts.json").unlink()
    by_image = FakeLinks(build_1=load(image=False), build_2=load(image=True))
    assert decide(layer, by_image, run_state("91ac0012"))["host"] == "build-2"
    (layer.layer_dir / "agents" / "hosts.json").unlink()
    assert decide(layer, FakeLinks(build_1=load(), build_2=load()), run_state("91ac0013"))["host"] == "build-1"


def test_first_picks_the_first_host_with_a_free_run(home):
    layer = home(hosts=["build-1", "build-2"], prefer="first")
    assert decide(layer, FakeLinks(build_1=load(runs=3), build_2=load(runs=0)))["host"] == "build-1"
    (layer.layer_dir / "agents" / "hosts.json").unlink()
    full = decide(layer, FakeLinks(build_1=load(runs=4), build_2=load(runs=3)), run_state("91ac0002"))
    assert full["host"] == "build-2" and full["reasons"] == ["first: 3 of 4 runs in use"]
    (layer.layer_dir / "agents" / "hosts.json").unlink()
    queued = decide(layer, FakeLinks(build_1=load(runs=4), build_2=load(runs=4)), run_state("91ac0003"))
    assert queued["host"] == "build-1" and "the run waits for one" in queued["reasons"][-1]


def test_three_decisions_in_a_row_spread_over_three_hosts(home, monkeypatch):
    layer = home(maxRunsPerHost=2)
    links = FakeLinks(build_1=load(max_runs=2), build_2=load(max_runs=2))
    monkeypatch.setattr(placement, "home_load", lambda layer: load(max_runs=2))
    runs = ["91ac0021", "91ac0022", "91ac0023"]
    chosen = [decide(layer, links, run_state(run))["host"] for run in runs]
    assert chosen == ["home", "build-1", "build-2"]
    record = json.loads((layer.layer_dir / "agents" / "hosts.json").read_text())
    assert [(p["host"], p["run"]) for p in record["pending"]] == list(zip(chosen, runs))
    assert sorted(record["hosts"]) == ["build-1", "build-2", "home"] and record["at"]
    # a reservation counts until the host reports the run, or 60 s pass
    assert placement._live_pending(layer, record["pending"], now=time.time() + 59) == record["pending"]
    assert placement._live_pending(layer, record["pending"], now=time.time() + 61) == []
    reported = run_state(runs[1])
    reported.remote = {"host": "build-1", "phase": "remote", "status": "running"}
    reported.save(layer.layer_dir)
    assert [p["run"] for p in placement._live_pending(layer, record["pending"])] == [runs[0], runs[2]]


class OlderLink(hostlink.Link):
    host_id = "build-1"

    def run(self, script, stdin=None, timeout=20):
        return subprocess.CompletedProcess([], 0, b'{"protocol": 0, "runs": 0}\n', b"")


@pytest.mark.parametrize("silent, reason", [("no answer within 10 s", "no answer: no answer within 10 s"),
                                            ("older", "no answer: its layer is older than this one: al deploy it again")])
def test_a_silent_host_is_skipped_and_reported(home, silent, reason):
    layer = home(hosts=["build-1", "build-2"])
    fake = FakeLinks(build_1=silent, build_2=load(runs=3))
    links = fake if silent != "older" else (lambda layer_, host: OlderLink() if host == "build-1" else fake(layer_, host))
    decision = decide(layer, links)
    assert decision["host"] == "build-2"
    assert decision["skipped"] == [{"host": "build-1", "reason": reason}]
    with events.recording(layer.layer_dir, RUN):
        placement.emit_decision(decision)
    event = events.read(layer.layer_dir / "agents" / RUN / "events.jsonl")[-1]
    assert event["type"] == "placement.decision" and event["host"] == "build-2"
    assert event["skipped"] == [{"host": "build-1", "reason": reason}] and event["reasons"]
    record = json.loads((layer.layer_dir / "agents" / "hosts.json").read_text())
    assert "silent" in record["hosts"]["build-1"] and record["hosts"]["build-2"]["runs"] == 3


SENTENCE = ("host build-1 has no usable login for claude-code: log in on it, or name a token (ANTHROPIC_API_KEY or "
            "CLAUDE_CODE_OAUTH_TOKEN) in its env file; see: al host LAYER build-1 doctor")


def _claude_ir(**placement_fields) -> dict:
    data = rf.demo_ir(**placement_fields)
    data["agents"].append({"id": "claude", "kind": "claude-code", "status": "stable"})
    data["adws"][0]["agent"] = "claude"
    return data


def test_a_host_without_a_usable_login_is_skipped_and_named(home):
    layer = home(_claude_ir(hosts=["build-1", "build-2"]))
    links = FakeLinks(build_1=load(logins={"claude-code": "none"}),
                      build_2=load(runs=3, logins={"claude-code": "token ANTHROPIC_API_KEY in its env file"}))
    decision = decide(layer, links)
    assert decision["host"] == "build-2" and decision["skipped"] == [{"host": "build-1", "reason": SENTENCE}]


def test_on_a_host_without_a_login_is_refused_with_the_sentence(home):
    layer = home(_claude_ir(hosts=["home", "build-1", "build-2"]))
    links = FakeLinks(build_1=load(logins={"claude-code": "none"}), build_2=load(logins={"claude-code": "own login"}))
    with pytest.raises(placement.Refused) as refused:
        decide(layer, links, on="build-1")
    assert str(refused.value) == SENTENCE
    assert links.asked == ["build-1"]                                     # only the named host is asked
    assert decide(layer, links, run_state("91ac0002"), on="build-2")["host"] == "build-2"
    # a dry run needs no login, and neither does the mock agent
    assert decide(layer, links, run_state("91ac0003", dry_run=True), on="build-1")["host"] == "build-1"
    mock = home(hosts=["home", "build-1"])
    assert decide(mock, FakeLinks(build_1=load(logins={"mock": "not needed"})), run_state("91ac0004"), on="build-1")["host"] == "build-1"
    with pytest.raises(placement.Refused, match=r"build-9 is not a host of \(placement"):
        decide(layer, links, on="build-9")


def test_a_reserved_host_takes_only_its_classes(home):
    layer = home(hosts=["build-1", "build-2"], reserve={"host": "build-2", "classes": ["chore", "bug"]})
    busy = dict(build_1=load(runs=3), build_2=load(runs=0))
    feature = decide(layer, FakeLinks(**busy), run_state("91ac0031", klass="feature"))
    assert feature["host"] == "build-1"
    assert feature["skipped"] == [{"host": "build-2", "reason": "reserved for chore, bug; this run is a feature"}]
    unfixed = decide(layer, FakeLinks(**busy), run_state("91ac0032", klass="chore", fixed=False))
    assert unfixed["host"] == "build-1"
    assert unfixed["skipped"] == [{"host": "build-2", "reason": "reserved for chore, bug; this run's class is not fixed at start"}]
    assert decide(layer, FakeLinks(**busy), run_state("91ac0033", klass="chore"))["host"] == "build-2"
    # a person who names the host decides
    assert decide(layer, FakeLinks(**busy), run_state("91ac0034", klass="feature"), on="build-2")["host"] == "build-2"


def test_a_pinned_workflow_stays_home_and_says_why(home):
    data = rf.demo_ir(hosts=["build-1", "build-2"])
    data["adws"][0]["pinnedHome"] = ["cmd db-test needs home (#:needs-home)"]
    layer = home(data)
    links = FakeLinks(build_1=load(), build_2=load())
    decision = decide(layer, links)
    assert decision == {"host": "home", "reasons": ["runs on home only: cmd db-test needs home (#:needs-home)"], "skipped": []}
    assert links.asked == [] and not (layer.layer_dir / "agents" / "hosts.json").exists()   # nobody is asked
    with pytest.raises(placement.Refused, match="workflow demo runs on home only: cmd db-test needs home"):
        decide(layer, links, on="build-1")
    # a single command and a fan-out candidate are not placed either
    single = {"name": "command:plan", "agent": "mock", "review": "policy", "environment": ["worktree"], "steps": []}
    assert placement.decide(layer, single, run_state(), link=links)["host"] == "home"
    candidate = run_state("91ac0002")
    candidate.fanout_parent = {"run": "91ac0001", "step": "race", "index": 0}
    unpinned = home(hosts=["build-1", "build-2"])
    assert decide(unpinned, links, candidate)["host"] == "home"


def test_home_is_never_chosen_when_it_is_not_listed(home):
    layer = home(hosts=["build-1", "build-2"])
    for index in range(4):
        decision = decide(layer, FakeLinks(build_1=load(runs=4), build_2=load(runs=4)), run_state(f"91ac004{index}"))
        assert decision["host"] in ("build-1", "build-2")
    with pytest.raises(placement.Refused, match=r"home is not a host of \(placement"):
        decide(layer, FakeLinks(), on="home")


def test_no_usable_host_refuses_the_run(home):
    layer = home(hosts=["build-1", "build-2"])
    with pytest.raises(placement.Refused) as refused:
        decide(layer, FakeLinks(build_1="no answer within 10 s", build_2="connection refused"))
    assert str(refused.value) == ("no host can take the run: build-1: no answer: no answer within 10 s; "
                                  "build-2: no answer: connection refused")
    assert json.loads((layer.layer_dir / "agents" / "hosts.json").read_text())["pending"] == []


@pytest.mark.parametrize("drift", ["layer", "base"])
def test_the_layer_must_be_at_the_git_source(home, app_repo, drift, monkeypatch):
    layer = home()
    links = FakeLinks(build_1=load(), build_2=load())
    monkeypatch.setattr(placement, "home_load", lambda layer_: load(runs=3))
    assert placement.source_problem(layer) == ""
    assert decide(layer, links)["host"] == "build-1"
    if drift == "layer":       # what runs here is not what a host would clone
        data = json.loads((layer.layer_dir / "agentic-layer.json").read_text())
        data["description"] = "changed and not committed"
        (layer.layer_dir / "agentic-layer.json").write_text(json.dumps(data, indent=2))
    else:                      # the base branch here is ahead of the source
        (app_repo / "local.py").write_text("LOCAL = 1\n")
        rf.commit_all(app_repo, "not pushed")
    layer = ir.load(layer.layer_dir)
    links.asked.clear()
    decision = decide(layer, links, run_state("91ac0002"))
    assert decision["host"] == "home" and links.asked == []
    assert decision["skipped"] == [{"host": host, "reason": "the layer on this machine is not at the git source: commit and push it"}
                                   for host in ("build-1", "build-2")]
    with pytest.raises(placement.Refused, match="not at the git source: commit and push it"):
        decide(layer, links, run_state("91ac0003"), on="build-1")
    monkeypatch.delenv("AL_GIT_SOURCE")
    assert "AL_GIT_SOURCE is not set on this machine" in placement.source_problem(layer)


def test_status_table_shows_what_the_scheduler_sees(home):
    layer = home(_claude_ir(maxRunsPerHost=2))
    found = placement.survey(layer, FakeLinks(
        build_1=load(runs=1, max_runs=2, load1=0.42, image=True, logins={"mock": "not needed", "claude-code": "own login"}),
        build_2="no answer within 10 s"))
    assert list(found) == ["home", "build-1", "build-2"]
    rows = placement.table(found)
    assert [row["host"] for row in rows] == ["home", "build-1", "build-2"]
    assert rows[1] == {"host": "build-1", "runs": "1/2", "cpus": "8", "load": "0.42", "memory free": "8.6 GB",
                       "disk free": "114 GB", "image": "yes", "login": "claude-code: own login", "state": "ok"}
    assert rows[2]["state"] == "no answer: no answer within 10 s" and rows[2]["runs"] == "-"
    assert rows[0]["image"] == "-" and rows[0]["login"] == "not needed"
    text = placement.render(rows)
    assert text.splitlines()[0].split() == ["host", "runs", "cpus", "load", "memory", "free", "disk", "free", "image", "login", "state"]
    assert len(text.splitlines()) == 4 and "no answer" in text.splitlines()[3]
    assert placement.headline(layer) == "placement: auto, prefer least-loaded, at most 2 runs per host"
    record = json.loads((layer.layer_dir / "agents" / "hosts.json").read_text())
    assert record["hosts"]["build-2"] == {"silent": "no answer within 10 s"} and record["pending"] == []


def test_the_real_load_of_this_machine_has_the_fields_of_a_host(home, monkeypatch):
    layer = home()
    monkeypatch.undo()
    facts = placement.home_load(layer)
    assert sorted(facts) == sorted(load()) and facts["runs"] == 0 and facts["max_runs"] == 4
    with slots.slot(layer, "runs"):
        assert placement.home_load(layer)["runs"] == 1
