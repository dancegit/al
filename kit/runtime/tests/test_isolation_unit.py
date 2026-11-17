"""Per-run isolation with a FAKE podman: published ports, per-run pods with sidecars, hardening
flags, mount lists and stale-resource garbage collection. Git is real (sandboxed worktrees are
verified by the host). test_isolation_podman.py runs the same guarantees against real podman."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import envs, gitsafe
from adw_modules.errors import LayerError, StepFailed
from adw_modules.ir import load
from adw_modules.state import ADWState

ADW_ID = "1501a001"
SIDECAR_IMAGE = "docker.io/library/redis:7-alpine"


def _podman_option(**extra) -> dict:
    return {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "2", "memory": "4g",
            "network": "slirp", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
            "credentials": [], "publish": [], "services": [], "pidsLimit": 2048, "readOnlyRoot": False, **extra}


def _worktree_option(**extra) -> dict:
    return {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27100, "portsPerRun": 2,
            "keep": "never", "status": "stable", **extra}


SERVICES = [
    {"id": "db", "kind": "container", "image": SIDECAR_IMAGE, "env": ["POSTGRES_PASSWORD=pw", "A=b c"],
     "command": "redis-server --port 6379", "readyCmd": "redis-cli ping", "readyTimeout": 5, "status": "stable"},
    {"id": "mock-api", "kind": "container", "image": "localhost/mock:1", "env": [], "command": "", "readyCmd": "",
     "readyTimeout": 60, "status": "stable"},
]


class FakePodman:
    """Records podman argv; `fail` maps an argv predicate to a return code / stdout."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[list[str]] = []
        self.rules: list[tuple] = []
        real_run = envs.subprocess.run

        def fake_run(argv, **kwargs):
            if argv[0] != "podman":
                return real_run(argv, **kwargs)
            self.calls.append(list(argv))
            for predicate, code, stdout in self.rules:
                if predicate(argv):
                    return SimpleNamespace(returncode=code, stderr="boom" if code else "", stdout=stdout)
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(envs.subprocess, "run", fake_run)

    def on(self, predicate, code: int = 0, stdout: str = "") -> None:
        self.rules.append((predicate, code, stdout))

    def find(self, prefix: list[str]) -> list[list[str]]:
        return [call for call in self.calls if call[:len(prefix)] == prefix]

    def index(self, predicate) -> int:
        return next(i for i, call in enumerate(self.calls) if predicate(call))


def _layer(make_layer, *, podman=None, worktree=None, services=None):
    return load(make_layer({"environments": [_worktree_option(**(worktree or {})), _podman_option(**(podman or {}))],
                            "services": services if services is not None else SERVICES}))


def _stack(layer, adw_id: str = ADW_ID):
    state = ADWState(adw_id, "test", prompt="Isolate me", klass="chore")
    return state, envs.build(layer, state, ["wt", "box"])


def _main_start(fake: FakePodman, layer, adw_id: str = ADW_ID) -> list[str]:
    return next(call for call in fake.calls if call[:3] == ["podman", "run", "-d"]
                and call[call.index("--name") + 1] == f"al-{layer.name}-{adw_id}")


def _value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# ---------------------------------------------------------------- publish


def test_publish_maps_container_ports_to_the_leased_block_and_exports_them(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"publish": [8000, 5432]}, services=[])
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    first, second = state.ports
    assert stack.env["ADW_PUBLISHED_8000"] == str(first) and stack.env["ADW_PUBLISHED_5432"] == str(second)
    assert stack.env["ADW_PORT_0"] == str(first) and stack.env["ADW_PORT_1"] == str(second)
    argv = stack.wrap("python3 -m http.server 8000")
    start = _main_start(fake, layer)
    published = [_value(start[i:], "-p") for i, arg in enumerate(start) if arg == "-p"]
    assert published == [f"127.0.0.1:{first}:8000", f"127.0.0.1:{second}:5432"]   # loopback only, leased ports
    assert f"ADW_PUBLISHED_8000={first}" in argv and f"ADW_PORT_0={first}" in argv  # visible inside the container
    assert "--pod" not in start                                                  # no services, no pod
    stack.teardown(success=True)


def test_without_publish_nothing_reaches_the_host(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, services=[])
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    stack.wrap("true")
    start = _main_start(fake, layer)
    assert "-p" not in start and "--publish" not in start and "--network" not in start  # own netns, nothing exposed
    assert not any(key.startswith("ADW_PUBLISHED_") for key in stack.env)
    stack.teardown(success=True)


def test_scope_command_containers_publish_too(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"publish": [8000], "scope": "command"}, services=[])
    FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    argv = stack.wrap("serve")
    assert argv[:3] == ["podman", "run", "--rm"] and f"127.0.0.1:{state.ports[0]}:8000" in argv
    stack.teardown(success=True)


def test_publish_longer_than_ports_per_run_is_refused_before_anything_is_created(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"publish": [8000, 8001, 8002]}, worktree={"portsPerRun": 2}, services=[])
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    with pytest.raises(LayerError, match=r"publishes 3 ports.*portsPerRun=2"):
        stack.setup()
    assert not state.worktree and not (layer.layer_dir / "trees" / ADW_ID).exists() and fake.calls == []


@pytest.mark.parametrize("network", ["none", "host"])
@pytest.mark.parametrize("what", ["publish", "services"])
def test_network_without_own_namespace_conflicts_with_publish_and_services(make_layer, monkeypatch, network, what) -> None:
    podman = {"network": network, "publish": [8000]} if what == "publish" else {"network": network, "services": ["db"]}
    layer = _layer(make_layer, podman=podman)
    FakePodman(monkeypatch)
    state, stack = _stack(layer)
    with pytest.raises(LayerError, match=rf"network '{network}' conflicts with {what}"):
        stack.setup()
    assert not state.worktree


def test_undeclared_service_is_refused(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"services": ["db", "cache"]})
    FakePodman(monkeypatch)
    with pytest.raises(LayerError, match=r"undeclared services \['cache'\]"):
        _stack(layer)[1].setup()


# ---------------------------------------------------------------- pod + sidecars


def test_services_run_in_a_per_run_pod_started_before_the_run_container(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"services": ["db", "mock-api"], "publish": [8000]})
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    assert stack.env["ADW_SERVICE_DB"] == "1" and stack.env["ADW_SERVICE_MOCK_API"] == "1"
    argv = stack.wrap("pytest")
    pod = f"al-{layer.name}-{ADW_ID}"
    create = fake.find(["podman", "pod", "create"])
    assert len(create) == 1 and _value(create[0], "--name") == pod
    assert "--userns=keep-id" in create[0]                                     # the pod owns userns …
    assert f"127.0.0.1:{state.ports[0]}:8000" in create[0]                    # … and the published ports
    db = next(call for call in fake.find(["podman", "run", "-d"]) if _value(call, "--name") == f"{pod}-db")
    api = next(call for call in fake.find(["podman", "run", "-d"]) if _value(call, "--name") == f"{pod}-mock-api")
    assert _value(db, "--pod") == pod and _value(api, "--pod") == pod
    assert db[-4:] == [SIDECAR_IMAGE, "redis-server", "--port", "6379"] and api[-1] == "localhost/mock:1"
    assert "POSTGRES_PASSWORD=pw" in db and "A=b c" in db
    ready = fake.find(["podman", "exec", f"{pod}-db"])
    assert ready and ready[0][-3:] == ["sh", "-c", "redis-cli ping"]
    main = _main_start(fake, layer)
    assert _value(main, "--pod") == pod
    assert "--userns=keep-id" not in main and "-p" not in main                # podman refuses both inside a pod
    order = [fake.index(lambda c: c[:3] == ["podman", "pod", "create"]),
             fake.index(lambda c: c[:3] == ["podman", "run", "-d"] and _value(c, "--name") == f"{pod}-db"),
             fake.index(lambda c: c[:3] == ["podman", "exec", f"{pod}-db"]),
             fake.index(lambda c: c[:3] == ["podman", "run", "-d"] and _value(c, "--name") == f"{pod}-mock-api"),
             fake.index(lambda c: c[:3] == ["podman", "run", "-d"] and _value(c, "--name") == pod)]
    assert order == sorted(order)                                             # pod → sidecar → ready → … → run container
    assert "ADW_SERVICE_DB=1" in argv and argv[-4:] == [pod, "bash", "-c", "pytest"]
    before = len(fake.calls)
    stack.teardown(success=True)
    teardown = fake.calls[before:]
    assert ["podman", "rm", "-f", "-t", "0", pod] in teardown
    assert ["podman", "rm", "-f", "-v", "-t", "0", f"{pod}-db"] in teardown
    assert ["podman", "pod", "rm", "-f", "-t", "0", pod] in teardown


@pytest.mark.parametrize("success,keep", [(False, "on-failure"), (True, "always"), (False, "never")])
def test_pod_is_removed_on_failure_and_when_the_worktree_is_kept(make_layer, monkeypatch, success, keep) -> None:
    layer = _layer(make_layer, podman={"services": ["mock-api"]}, worktree={"keep": keep})
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    stack.wrap("true")
    stack.teardown(success=success)
    assert ["podman", "pod", "rm", "-f", "-t", "0", f"al-{layer.name}-{ADW_ID}"] in fake.calls
    assert stack._pod is None and stack._container is None


def test_sidecar_start_failure_removes_the_pod_and_never_starts_the_run(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"services": ["db", "mock-api"]})
    fake = FakePodman(monkeypatch)
    pod = f"al-{layer.name}-{ADW_ID}"
    fake.on(lambda argv: argv[:3] == ["podman", "run", "-d"] and f"{pod}-mock-api" in argv, code=125)
    state, stack = _stack(layer)
    stack.setup()
    with pytest.raises(StepFailed, match="sidecar mock-api failed to start") as raised:
        stack.wrap("true")
    assert raised.value.step == "environment setup"
    assert ["podman", "pod", "rm", "-f", "-t", "0", pod] in fake.calls
    assert ["podman", "rm", "-f", "-v", "-t", "0", f"{pod}-db"] in fake.calls
    assert not any(call[:3] == ["podman", "run", "-d"] and _value(call, "--name") == pod for call in fake.calls)
    assert stack._pod is None
    stack.teardown(success=False)


def test_readiness_times_out_with_the_sidecars_last_log_lines(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"services": ["db"]})
    fake = FakePodman(monkeypatch)
    pod = f"al-{layer.name}-{ADW_ID}"
    fake.on(lambda argv: argv[:3] == ["podman", "exec", f"{pod}-db"], code=1)
    fake.on(lambda argv: argv[:2] == ["podman", "logs"], stdout="line1\nFATAL: cannot bind\n")
    state, stack = _stack(layer)
    stack.setup()
    # the fake clock starts after setup: `time` is one module for the process, and setup's real git
    # calls (subprocess's own wait back-off sleeps 0.001, 0.002, …) must neither fill nor move it
    clock = {"now": 1000.0}
    sleeps: list[float] = []
    monkeypatch.setattr(envs.time, "monotonic", lambda: clock["now"])
    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds
    monkeypatch.setattr(envs.time, "sleep", sleep)
    with pytest.raises(StepFailed, match=r"(?s)sidecar db not ready after 5s.*FATAL: cannot bind") as raised:
        stack.wrap("true")
    assert raised.value.step == "environment setup"
    assert sleeps and set(sleeps) == {1.0} and len(fake.find(["podman", "exec", f"{pod}-db"])) == 6  # every second, 5s
    assert ["podman", "logs", "--tail", "20", f"{pod}-db"] in fake.calls
    assert ["podman", "pod", "rm", "-f", "-t", "0", pod] in fake.calls
    stack.teardown(success=False)


def test_readiness_polls_until_ready(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"services": ["db"]})
    fake = FakePodman(monkeypatch)
    attempts = {"n": 0}
    real = envs.subprocess.run
    def flaky(argv, **kwargs):
        if argv[:2] == ["podman", "exec"] and argv[2].endswith("-db"):
            attempts["n"] += 1
            fake.calls.append(list(argv))
            return SimpleNamespace(returncode=0 if attempts["n"] >= 3 else 1, stderr="", stdout="")
        return real(argv, **kwargs)
    monkeypatch.setattr(envs.subprocess, "run", flaky)
    monkeypatch.setattr(envs.time, "sleep", lambda seconds: None)
    state, stack = _stack(layer)
    stack.setup()
    stack.wrap("true")
    assert attempts["n"] == 3 and stack._pod and stack._container
    stack.teardown(success=True)


# ---------------------------------------------------------------- hardening + mounts


@pytest.mark.parametrize("read_only", [False, True])
def test_hardening_flags_on_the_run_container_and_every_sidecar(make_layer, monkeypatch, read_only) -> None:
    layer = _layer(make_layer, podman={"services": ["db", "mock-api"], "pidsLimit": 321, "readOnlyRoot": read_only})
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    stack.wrap("true")
    starts = fake.find(["podman", "run", "-d"])
    assert len(starts) == 3
    for start in starts:
        assert "--cap-drop=ALL" in start and "--security-opt=no-new-privileges" in start
        assert _value(start, "--pids-limit") == "321"
        assert ("--read-only" in start) is read_only
        assert (["--tmpfs", "/tmp:rw,mode=1777"] == start[start.index("--tmpfs"):start.index("--tmpfs") + 2]) \
            if read_only else "--tmpfs" not in start
        assert _value(start, "--label").startswith("al.layer=")
    assert "HOME=/tmp/home" in _main_start(fake, layer)                      # HOME is per container, never the host's
    stack.teardown(success=True)


def test_scope_command_hardening_and_pod(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"services": ["mock-api"], "scope": "command", "readOnlyRoot": True})
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    argv = stack.wrap("make test")
    assert argv[:3] == ["podman", "run", "--rm"] and _value(argv, "--pod") == f"al-{layer.name}-{ADW_ID}"
    assert "--cap-drop=ALL" in argv and "--read-only" in argv and "--userns=keep-id" not in argv
    assert len(fake.find(["podman", "pod", "create"])) == 1
    stack.wrap("make lint")
    assert len(fake.find(["podman", "pod", "create"])) == 1                   # one pod per run, not per command
    stack.teardown(success=True)


def test_sidecars_get_no_worktree_git_layer_cache_or_credentials(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"services": ["db", "mock-api"], "agentInside": True, "caches": ["uv"],
                                       "credentials": ["claude"]})
    fake = FakePodman(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    state, stack = _stack(layer)
    stack.setup()
    stack.wrap_agent(["claude", "-p", "hi"])
    pod = f"al-{layer.name}-{ADW_ID}"
    sidecars = [call for call in fake.find(["podman", "run", "-d"]) if _value(call, "--name") != pod]
    assert len(sidecars) == 2
    forbidden = [str(state.worktree), str(layer.app_root), str(layer.layer_dir), "/caches", "al-cache-", "ANTHROPIC",
                 ".credentials", "GIT_"]
    for sidecar in sidecars:
        assert "-v" not in sidecar and "--volume" not in sidecar and "--mount" not in sidecar
        joined = " ".join(arg for i, arg in enumerate(sidecar) if sidecar[i - 1] != "--label")  # labels are not mounts
        assert not any(item in joined for item in forbidden), joined
    main = _main_start(fake, layer)
    assert f"{state.worktree}:{state.worktree}" in main                      # the run container still has its worktree
    stack.teardown(success=True)


def test_agent_inside_mounts_only_this_runs_private_dirs(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"agentInside": True}, services=[])
    for other in ("ffff0001",):
        (layer.layer_dir / "agents" / other).mkdir(parents=True)
        (layer.layer_dir / "artefacts" / other).mkdir(parents=True)
        (layer.layer_dir / "trees" / other).mkdir(parents=True)
    (layer.layer_dir / "escape").symlink_to("/etc")
    for shared in ("specs", "docs", "experts"):          # exist already (an earlier run): still mounted only once
        (layer.layer_dir / shared).mkdir()
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    stack.wrap("true")
    start = _main_start(fake, layer)
    destinations = [start[i + 1].split(":")[1] for i, arg in enumerate(start) if arg == "-v"]
    assert len(destinations) == len(set(destinations))   # podman refuses duplicate mount destinations
    sources = [arg.split(":")[0] for arg in start if arg.startswith("/")]
    for private in ("agents", "artefacts", "trees"):
        root = str(layer.layer_dir / private)
        assert root not in sources                                            # never the shared parent …
        assert not any(source.startswith(f"{root}/ffff0001") for source in sources)  # … nor another run's dir
    assert str(layer.layer_dir / "agents" / ADW_ID) in sources and str(layer.layer_dir / "artefacts" / ADW_ID) in sources
    assert str(layer.layer_dir / "escape") not in sources                    # symlinks would leave the layer
    stack.teardown(success=True)


def test_freeze_pauses_the_run_container_only_and_always_unpauses(make_layer, monkeypatch) -> None:
    layer = _layer(make_layer, podman={"services": ["mock-api"]}, worktree={"keep": "always"})
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    stack.wrap("true")
    pod = f"al-{layer.name}-{ADW_ID}"
    before = len(fake.calls)
    with pytest.raises(RuntimeError):
        with gitsafe.session(stack.sandbox):
            raise RuntimeError("host git blew up")
    frozen = fake.calls[before:]
    assert frozen == [["podman", "pause", pod], ["podman", "unpause", pod]]    # sidecars are never paused
    stack.teardown(success=True)


# ---------------------------------------------------------------- garbage collection


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _gc_layer(make_layer):
    return load(make_layer({"environments": [_worktree_option(), _podman_option()], "services": []}))


def test_gc_removes_only_dead_runs_of_this_layer(make_layer, monkeypatch, tmp_path) -> None:
    layer = _gc_layer(make_layer)
    agents = layer.layer_dir / "agents"
    mine = str(layer.layer_dir.resolve())
    runs = {"aaaa0001": ("running", os.getpid()),   # live
            "aaaa0002": ("passed", os.getpid()),    # finished
            "aaaa0004": ("running", _dead_pid()),   # crashed: status still running, owner dead
            "aaaa0005": ("running", None),          # no lease at all
            "aaaa0009": ("failed", None)}           # unlabelled (older runtime), known to this layer
    leases = {}
    for run, (status, pid) in runs.items():
        (agents / run).mkdir(parents=True)
        (agents / run / "state.json").write_text(json.dumps({"status": status}))
        if pid:
            leases[run] = {"ports": [], "pid": pid}
    (agents / ".ports.json").write_text(json.dumps(leases))
    prefix = f"al-{layer.name}-"

    def labels(run: str, layer_dir: str = mine) -> dict:
        return {"al.layer": layer.name, "al.layer-dir": layer_dir, "al.run": run}
    containers = [
        {"Names": [f"{prefix}aaaa0001"], "Labels": labels("aaaa0001")},
        {"Names": [f"{prefix}aaaa0001-db"], "Labels": labels("aaaa0001")},
        {"Names": [f"{prefix}aaaa0002"], "Labels": labels("aaaa0002")},
        {"Names": [f"{prefix}aaaa0003-db"], "Labels": labels("aaaa0003")},          # state.json missing
        {"Names": [f"{prefix}aaaa0004"], "Labels": labels("aaaa0004")},
        {"Names": [f"{prefix}aaaa0005"], "Labels": labels("aaaa0005")},
        {"Names": [f"{prefix}aaaa0006"], "Labels": labels("aaaa0006", str(tmp_path / "other"))},  # same name, other layer
        {"Names": [f"{prefix}aaaa0008"], "Labels": None},                         # unlabelled, unknown run
        {"Names": [f"{prefix}aaaa0009"], "Labels": {}},                           # unlabelled, dead run of ours
        {"Names": [f"{prefix}extra-aaaa0007"], "Labels": labels("aaaa0007")},      # not a run resource name
        {"Names": ["al-other-layer-aaaa0002"], "Labels": labels("aaaa0002")},      # other layer's name
    ]
    pods = [{"Name": f"{prefix}aaaa0001", "Labels": labels("aaaa0001")},
            {"Name": f"{prefix}aaaa0004", "Labels": labels("aaaa0004")},
            {"Name": f"{prefix}aaaa0006", "Labels": labels("aaaa0006", str(tmp_path / "other"))}]
    fake = FakePodman(monkeypatch)
    fake.on(lambda argv: argv[:3] == ["podman", "ps", "-a"], stdout=json.dumps(containers))
    fake.on(lambda argv: argv[:3] == ["podman", "pod", "ps"], stdout=json.dumps(pods))
    removed = envs.collect_stale_containers(layer)
    assert sorted(removed) == sorted([f"{prefix}aaaa0004",  # pod
                                      f"{prefix}aaaa0002", f"{prefix}aaaa0003-db", f"{prefix}aaaa0004",
                                      f"{prefix}aaaa0005", f"{prefix}aaaa0009"])
    removals = [call for call in fake.calls if "rm" in call[1:3]]
    assert ["podman", "pod", "rm", "-f", "-t", "0", f"{prefix}aaaa0004"] in removals
    assert ["podman", "rm", "-f", "-v", "-t", "0", f"{prefix}aaaa0003-db"] in removals
    assert not any(name in " ".join(call) for call in removals
                   for name in ("aaaa0001", "aaaa0006", "aaaa0007", "aaaa0008", "al-other-layer"))
    listing = fake.find(["podman", "ps", "-a"])[0]
    assert _value(listing, "--filter") == f"name=^{prefix}" or _value(listing, "--filter").startswith("name=^al")


def test_collect_garbage_runs_container_gc_even_without_gc_days(make_layer, monkeypatch) -> None:
    layer = _gc_layer(make_layer)
    fake = FakePodman(monkeypatch)
    assert envs.collect_garbage(layer, _worktree_option(gcDays=0)) == []
    assert fake.find(["podman", "ps", "-a"]) and fake.find(["podman", "pod", "ps"])


def test_gc_never_calls_podman_for_a_layer_without_a_podman_environment(make_layer, monkeypatch) -> None:
    layer = load(make_layer({"environments": [_worktree_option()]}))
    fake = FakePodman(monkeypatch)
    assert envs.collect_stale_containers(layer) == []
    assert fake.calls == []


def test_zero_port_runs_still_record_their_owner(tmp_path: Path) -> None:
    (tmp_path / "agents" / "bbbb0001").mkdir(parents=True)
    (tmp_path / "agents" / "bbbb0001" / "state.json").write_text("{}")
    assert envs.lease_ports(tmp_path, "bbbb0001", 27500, 0) == []
    table = json.loads((tmp_path / "agents" / ".ports.json").read_text())
    assert table["bbbb0001"] == {"ports": [], "pid": os.getpid()}
    envs.release_ports(tmp_path, "bbbb0001")
    assert "bbbb0001" not in json.loads((tmp_path / "agents" / ".ports.json").read_text())


def test_sidecars_start_as_namespace_root_with_a_minimal_capability_set() -> None:
    """Service images chown their data dir and drop privileges themselves; they cannot do that as
    the host uid with every capability dropped (postgres:16-alpine fails initdb). The run container
    keeps the stricter flags."""
    option = {"pidsLimit": 256, "readOnlyRoot": False}
    sidecar = envs.sidecar_flags(option)
    assert sidecar[:2] == ["--user", "0:0"]
    assert "--cap-drop=ALL" in sidecar and "--security-opt=no-new-privileges" in sidecar
    assert f"--cap-add={envs.SIDECAR_CAPABILITIES}" in sidecar
    assert set(envs.SIDECAR_CAPABILITIES.split(",")) == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}
    run = envs.hardening_flags(option)
    assert not any(flag.startswith("--cap-add") for flag in run) and "--user" not in run


def test_the_container_gets_a_credential_copy_that_follows_the_hosts_renewals(make_layer, monkeypatch, tmp_path) -> None:
    """The agent CLI on the host replaces its credential file when it renews the login, and the old
    login is revoked. A file mount of the host's file would keep the replaced file for the whole run."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    host = home / ".claude" / ".credentials.json"
    host.write_text('{"token": "first"}')
    monkeypatch.setattr(Path, "home", lambda: home)
    layer = _layer(make_layer, podman={"agentInside": True, "credentials": ["claude"]})
    fake = FakePodman(monkeypatch)
    state, stack = _stack(layer)
    stack.setup()
    stack.wrap_agent(["claude", "-p", "hi"])
    copy = Path(layer.layer_dir) / "agents" / ADW_ID / "credentials" / "claude.json"
    main = " ".join(_main_start(fake, layer))
    assert f"{copy}:" in main and str(host) not in main          # the copy is mounted, never the host's file
    assert copy.read_text() == '{"token": "first"}' and (copy.stat().st_mode & 0o777) == 0o600
    inode = copy.stat().st_ino
    replacement = home / ".claude" / "new"
    replacement.write_text('{"token": "second, and longer"}')
    replacement.replace(host)                                    # how the CLI renews: a new file takes the name
    stack.wrap_agent(["claude", "-p", "again"])
    assert copy.read_text() == '{"token": "second, and longer"}'
    assert copy.stat().st_ino == inode                           # rewritten in place: the mount sees it
    host.write_text('{"t": 1}')
    stack.wrap_agent(["claude", "-p", "third"])
    assert copy.read_text() == '{"t": 1}'                        # a shorter file leaves no tail behind
    stack.teardown(success=True)


def test_the_credential_copy_follows_the_host_during_an_agent_call(make_layer, monkeypatch, tmp_path) -> None:
    """A call can last hours; the host's login may be renewed in the middle of it."""
    import time

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    host = home / ".claude" / ".credentials.json"
    host.write_text("first")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(envs, "CREDENTIAL_FOLLOW_SECONDS", 0.02)
    layer = _layer(make_layer, podman={"agentInside": True, "credentials": ["claude"]})
    FakePodman(monkeypatch)
    _state, stack = _stack(layer)
    stack.setup()
    stack.wrap_agent(["claude", "-p", "hi"])                     # the call starts; nothing calls wrap_agent again
    copy = Path(layer.layer_dir) / "agents" / ADW_ID / "credentials" / "claude.json"
    host.write_text("renewed")
    deadline = time.monotonic() + 5
    while copy.read_text() != "renewed" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert copy.read_text() == "renewed"
    stack.teardown(success=True)
    host.write_text("after the run")
    time.sleep(0.2)
    assert copy.read_text() == "renewed"                         # the follower stopped with the run


def test_a_refused_login_is_tried_once_more_and_then_named(monkeypatch, tmp_path) -> None:
    from adw_modules import agents, models

    monkeypatch.setattr(agents, "LOGIN_RETRY_SECONDS", 0)
    monkeypatch.setattr(agents, "LOGIN_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(agents, "claude_argv", lambda *a, **k: ["claude"])
    monkeypatch.setattr(agents, "_claude_env", lambda stack, directory=None: {})
    monkeypatch.setattr(agents, "parse_stream_json_result", lambda out, step: out)
    stack = SimpleNamespace(wrap_agent=lambda argv: argv)
    agent = {"timeout": 5}
    calls: list[int] = []

    def refused_once(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise StepFailed("build", 'agent exited 1: "api_error_type":"authentication_error" OAuth token revoked')
        return "done"

    monkeypatch.setattr(agents, "_run_process", refused_once)
    choice = models.Choice.__new__(models.Choice)
    assert agents._run_claude(None, agent, "p", choice, tmp_path, stack, "build", tmp_path / "raw") == "done"
    assert len(calls) == 2

    def always(*_a, **_k):
        raise StepFailed("build", "agent exited 1: OAuth token revoked")

    monkeypatch.setattr(agents, "_run_process", always)
    with pytest.raises(StepFailed, match="login was refused 4 times"):
        agents._run_claude(None, agent, "p", choice, tmp_path, stack, "build", tmp_path / "raw")

    def other(*_a, **_k):
        calls.append(1)
        raise StepFailed("build", "agent exited 1: something else")

    calls.clear()
    monkeypatch.setattr(agents, "_run_process", other)
    with pytest.raises(StepFailed, match="something else"):
        agents._run_claude(None, agent, "p", choice, tmp_path, stack, "build", tmp_path / "raw")
    assert len(calls) == 1                                       # any other failure is not retried
