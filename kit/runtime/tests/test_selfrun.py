"""A self-contained run on a host: the clone, the layer, the image, the steps, the push, and the
host tool's verbs. Local bare repositories play the git sources, a directory plays the host
(remote_fixtures), the agent is the mock. No ssh, no real podman: the fake one of
tests/tools/fake_podman.py where the image and a run's container are tested."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import remote_fixtures as rf
from adw_modules import envs, events, hostlink, ir, sandbox, selfrun, state as state_module
from adw_modules.errors import LayerError, StepFailed

RUN = "5e1f1001"
BRANCH = f"chore/{RUN}-add-a-feature"


@pytest.fixture
def world(tmp_path, make_layer, app_repo, monkeypatch):
    """(layer, host root, link): the home layer, the home of host build-1 with the layer deployed,
    and the link the home machine reaches it through."""
    def build(ir_overrides=None, **host):
        layer = rf.build_layer_repo(tmp_path, make_layer, app_repo, ir_overrides)
        rf.sources(tmp_path, app_repo, layer)
        root = rf.host_home(tmp_path, "build-1", layer, **host)
        for key, value in rf.local_hosts_env(tmp_path).items():
            monkeypatch.setenv(key, value)
        return ir.load(layer), root, hostlink.link_for(ir.load(layer), "build-1")
    return build


@pytest.fixture
def on_host(monkeypatch):
    """Make this process look at the world as a host's process does (its home, state and cache)."""
    def enter(root: Path) -> ir.Layer:
        monkeypatch.setenv("HOME", str(root))
        for name in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(name, raising=False)
        return ir.load(root / "al" / "layer")
    return enter


def seed(layer: ir.Layer, run: str = RUN, **fields) -> dict:
    revision = rf.git(layer.layer_dir, "rev-parse", "HEAD")
    return {"protocol": selfrun.PROTOCOL, "run": run, "adw": "demo", "host": "build-1", "base_branch": "main",
            "layer_rev": revision, "prompt": "Add a feature", "klass": "chore", "class_fixed": True, "model_set": "",
            "overrides": {}, "models": {}, "limits": {}, "dry_run": False, "source": "cli", "delivery": {}, "seq": 0,
            "events": [{"type": "remote.start", "fields": {"host": "build-1"}}], "restart": {}, **fields}


def start(layer: ir.Layer, link, root: Path, run: str = RUN, wait: bool = True, **fields) -> dict:
    """Start a run on the host and (by default) wait for its end; returns its state there."""
    answer = link.tool(layer, "build-1", "start", stdin=json.dumps(seed(layer, run, **fields)), timeout=120)
    assert answer["started"] is True
    path = rf.host_run_layer(root, run) / "agents" / run / "state.json"
    deadline = time.monotonic() + 90
    while wait and time.monotonic() < deadline:
        facts = next(r for r in link.tool(layer, "build-1", "runs")["runs"] if r["run"] == run)
        if facts["status"] not in ("starting", "running"):
            time.sleep(0.5)   # the summary is written after the state
            break
        time.sleep(0.2)
    return json.loads(path.read_text()) if path.exists() else {}


def _files(directory: Path):
    return [p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()]


def test_prepare_clones_the_base_commit_into_the_runs_own_storage(world, on_host, tmp_path, app_repo):
    layer, root, _link = world()
    deployed = on_host(root)
    url = str(tmp_path / "sources" / "app.git")
    storage = selfrun.run_storage(deployed.name, RUN)
    storage.mkdir(parents=True)
    assert rf.host_storage(root) / "runs" / RUN == storage      # under the host's own state directory
    where = selfrun.prepare(deployed, storage, seed(layer), selfrun.localenv.load(deployed))
    repo = Path(where["app_root"])
    assert repo == storage / "repo" and (repo / ".git").is_dir()
    assert where["base"] == rf.git(app_repo, "rev-parse", "main") == rf.git(repo, "rev-parse", "HEAD")
    assert rf.git(repo, "symbolic-ref", "HEAD") == "refs/heads/main" and rf.git(repo, "status", "--porcelain") == ""
    assert rf.git(repo, "remote") == "" and "url" not in rf.git(repo, "config", "--local", "--list")
    for path in _files(storage):
        assert url.encode() not in path.read_bytes(), path


def test_the_clone_is_shallow_and_falls_back_to_a_full_fetch(world, on_host, tmp_path, app_repo, monkeypatch):
    (app_repo / "more.py").write_text("MORE = 1\n")
    rf.commit_all(app_repo, "a second commit")
    layer, root, _link = world()
    deployed = on_host(root)
    url = "file://" + str(tmp_path / "sources" / "app.git")      # a transport that honours --depth
    values = {**selfrun.localenv.load(deployed), "AL_GIT_SOURCE": url}
    shallow = selfrun.run_storage(deployed.name, RUN)
    shallow.mkdir(parents=True)
    where = selfrun.prepare(deployed, shallow, seed(layer), values)
    assert rf.git(Path(where["app_root"]), "rev-list", "--count", "HEAD") == "1"
    assert where["base"] == rf.git(app_repo, "rev-parse", "main")
    monkeypatch.setenv("AL_SELFRUN_NO_SHALLOW", "1")              # a source that refuses shallow fetches
    full = selfrun.run_storage(deployed.name, "5e1f1002")
    full.mkdir(parents=True)
    where = selfrun.prepare(deployed, full, seed(layer, "5e1f1002"), values)
    assert rf.git(Path(where["app_root"]), "rev-list", "--count", "HEAD") == "2"
    assert not (Path(where["app_root"]) / ".git" / "shallow").exists()


def test_the_mirror_cache_only_speeds_things_up(world, on_host, tmp_path, app_repo):
    layer, root, _link = world()
    deployed = on_host(root)
    values = selfrun.localenv.load(deployed)
    url = values["AL_GIT_SOURCE"]
    mirror = selfrun.mirror_path(url)
    assert root in mirror.parents and url not in str(mirror)
    tip = rf.git(app_repo, "rev-parse", "main")

    def clone(run: str) -> Path:
        storage = selfrun.run_storage(deployed.name, run)
        storage.mkdir(parents=True)
        where = selfrun.prepare(deployed, storage, seed(layer, run), values)
        repo = Path(where["app_root"])
        assert where["base"] == rf.git(repo, "rev-parse", "HEAD")
        assert not (repo / ".git" / "objects" / "info" / "alternates").exists()
        assert rf.git(repo, "fsck", "--no-dangling") == ""
        return repo

    clone("5e1f1011")
    assert (mirror / "HEAD").is_file() and rf.git(mirror, "rev-parse", "refs/heads/main") == tip
    assert url.encode() not in b"".join(p.read_bytes() for p in _files(mirror))
    (app_repo / "more.py").write_text("MORE = 1\n")
    tip = rf.commit_all(app_repo, "the source moves on")
    rf.git(app_repo, "push", "-q", url, "main:main")
    assert rf.git(clone("5e1f1012"), "rev-parse", "HEAD") == tip          # through the mirror, brought up to date
    assert rf.git(mirror, "rev-parse", "refs/heads/main") == tip
    for pack in (mirror / "objects").rglob("*"):                           # a corrupted mirror
        if pack.is_file():
            pack.chmod(0o644)
            pack.write_bytes(b"garbage")
    assert rf.git(clone("5e1f1013"), "rev-parse", "HEAD") == tip
    import shutil
    shutil.rmtree(mirror)                                                  # a missing one
    assert rf.git(clone("5e1f1014"), "rev-parse", "HEAD") == tip
    handle = os.open(mirror.with_suffix(".lock"), os.O_CREAT | os.O_RDWR)  # a locked one: not waited for
    fcntl.flock(handle, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        assert rf.git(clone("5e1f1015"), "rev-parse", "HEAD") == tip
        assert time.monotonic() - started < 30
    finally:
        os.close(handle)


def test_an_in_repo_layer_comes_with_the_code(tmp_path, app_repo, monkeypatch, make_layer):
    data = rf.demo_ir()
    data["layer"] = {"id": "in-repo", "kind": "in-repo", "status": "stable", "dir": "agentic"}
    made = make_layer(data)
    (app_repo / "agentic").mkdir()
    (app_repo / "agentic" / "agentic-layer.json").write_text((made / "agentic-layer.json").read_text())
    rf.commit_all(app_repo, "the layer, in the repository")
    source = tmp_path / "sources" / "app.git"
    source.parent.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(source)], check=True)
    rf.git(app_repo, "push", "-q", str(source), "main:main")
    root = tmp_path / "hosts" / "build-1"
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    for name in ("XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    deployed = ir.Layer(data, app_repo / "agentic", app_repo)       # what `al deploy` put on the host
    storage = selfrun.run_storage(deployed.name, RUN)
    storage.mkdir(parents=True)
    where = selfrun.prepare(deployed, storage, {"base_branch": "main"}, {"AL_GIT_SOURCE": str(source)})
    assert Path(where["layer_dir"]) == storage / "repo" / "agentic" and Path(where["app_root"]) == storage / "repo"
    assert (Path(where["layer_dir"]) / "agentic-layer.json").is_file() and not (storage / "layer").exists()
    # a base branch without the layer is refused in words a person can act on
    rf.git(app_repo, "rm", "-rq", "agentic")
    rf.commit_all(app_repo, "the layer is gone")
    rf.git(app_repo, "push", "-q", str(source), "main:main")
    other = selfrun.run_storage(deployed.name, "5e1f1002")
    other.mkdir(parents=True)
    with pytest.raises(LayerError, match="holds no layer in agentic/: commit and push it"):
        selfrun.prepare(deployed, other, {"base_branch": "main"}, {"AL_GIT_SOURCE": str(source)})


def test_a_sibling_layer_is_fetched_at_its_exact_revision(world, on_host, tmp_path, app_repo):
    layer, root, _link = world()
    wanted = seed(layer)
    rf.script(layer.layer_dir, "build", {"files": {"later.py": "LATER = True\n"}})
    moved = rf.commit_all(layer.layer_dir, "the layer moves on")
    rf.sources(tmp_path, app_repo, layer.layer_dir)
    assert moved != wanted["layer_rev"]
    deployed = on_host(root)
    storage = selfrun.run_storage(deployed.name, RUN)
    storage.mkdir(parents=True)
    where = selfrun.prepare(deployed, storage, wanted, selfrun.localenv.load(deployed))
    assert Path(where["layer_dir"]) == storage / "layer" and Path(where["app_root"]) == storage / "repo"
    assert rf.git(Path(where["layer_dir"]), "rev-parse", "HEAD") == wanted["layer_rev"]
    assert "feature.py" in (Path(where["layer_dir"]) / "mock" / "demo" / "build.json").read_text()
    assert rf.git(Path(where["layer_dir"]), "remote") == ""
    layer_url = str(tmp_path / "sources" / "layer.git").encode()
    assert not [p for p in _files(storage) if layer_url in p.read_bytes()]
    with pytest.raises(LayerError, match="does not hold revision"):
        again = selfrun.run_storage(deployed.name, "5e1f1002")
        again.mkdir(parents=True)
        selfrun.prepare(deployed, again, {**wanted, "layer_rev": "f" * 40}, selfrun.localenv.load(deployed))


def test_a_self_contained_run_pushes_its_branch_and_ends_delivered(world, tmp_path, app_repo):
    layer, root, link = world()
    source = tmp_path / "sources" / "app.git"
    main_before = rf.git(source, "rev-parse", "refs/heads/main")
    state = start(layer, link, root)
    assert state["status"] == "delivered", (rf.host_storage(root) / "runs" / RUN / "tool.log").read_text()
    assert [(s["name"], s["status"]) for s in state["steps"]] == [("plan", "passed"), ("build", "passed"),
                                                                  ("test", "passed"), ("review", "passed")]
    assert state["ship"] == {} and state["branch"] == BRANCH
    delivered = state["remote"]["delivered"]
    assert delivered["branch"] == BRANCH and delivered["step"] == "ship"
    assert rf.git(source, "rev-parse", f"refs/heads/{BRANCH}") == delivered["sha"]
    assert rf.git(source, "show", f"{BRANCH}:feature.py") == "FEATURE = True"
    assert rf.git(source, "rev-parse", "refs/heads/main") == main_before == state["remote"]["base"]
    run_layer = rf.host_run_layer(root, RUN)
    assert Path(state["worktree"]).parent == run_layer / "trees"          # the same layout as at home, in the clone
    found = events.read(run_layer / "agents" / RUN / "events.jsonl")
    types = [e["type"] for e in found]
    assert types[0] == "remote.start" and types[1] == "run.start" and types[-1] == "run.end"
    pushes = [e for e in found if e["type"] == "remote.push"]
    assert [(e["kind"], e["sha"], e["branch"]) for e in pushes] == [("delivered", delivered["sha"], BRANCH)]
    assert not [e for e in found if e["type"] in ("ship.result", "gate.decision", "handoff", "notify.sent")]
    assert [e["seq"] for e in found] == list(range(1, len(found) + 1))
    pushed_state = json.loads((run_layer / "agents" / RUN / "pushed-state.json").read_text())
    assert pushed_state["state"]["adw_id"] == RUN and pushed_state["layer_dir"] == str(run_layer)
    url = str(source).encode()
    for path in _files(rf.host_storage(root) / "runs" / RUN):
        assert url not in path.read_bytes(), path


def test_delivery_never_forces_and_never_touches_other_branches(world, tmp_path, app_repo):
    layer, root, link = world()
    source = tmp_path / "sources" / "app.git"
    rf.git(app_repo, "checkout", "-q", "--orphan", "elsewhere")
    (app_repo / "other.txt").write_text("someone else's work\n")
    unrelated = rf.commit_all(app_repo, "unrelated")
    rf.git(app_repo, "push", "-q", str(source), f"elsewhere:refs/heads/{BRANCH}")
    rf.git(app_repo, "checkout", "-q", "-f", "main")
    refs_before = rf.git(source, "for-each-ref")
    state = start(layer, link, root)
    assert state["status"] == "failed"
    log = (rf.host_run_layer(root, RUN) / "agents" / RUN / "run.log").read_text()
    assert f"the git source already has a different {BRANCH}" in log
    assert rf.git(source, "for-each-ref") == refs_before
    assert rf.git(source, "rev-parse", f"refs/heads/{BRANCH}") == unrelated
    assert "delivered" not in state["remote"] or not state["remote"]["delivered"].get("sha")
    # a branch that is not the run's own is refused before anything is asked of the source
    staged = ir.Layer({**layer.ir, "stages": [{"name": "sprint", "branch": "sprint/2026-s47"}]}, layer.layer_dir, layer.app_root)
    for branch in ("main", "sprint/2026-s47", "al/ship/main", f"chore/{'0' * 8}-add-a-feature", ""):
        with pytest.raises(StepFailed, match="refusing to pu"):
            selfrun.check_branch(staged, branch, RUN)
        fake = state_module.ADWState(adw_id=RUN, adw="demo")
        fake.branch = branch
        with pytest.raises(StepFailed, match="refusing to pu"):
            selfrun.push(staged, fake, "0" * 40, "deliver", "delivered")   # no AL_GIT_SOURCE here: never reached
    selfrun.check_branch(staged, BRANCH, RUN)
    source_text = Path(selfrun.__file__).read_text()
    assert '"--force"' not in source_text and "+refs/heads/{branch}" not in source_text and 'f"+{' not in source_text


def test_a_failed_run_still_delivers_its_work_and_makes_no_handoff(world, tmp_path):
    data = rf.demo_ir()
    data["codebase"]["commands"][0]["run"] = "echo broken; exit 1"
    layer, root, link = world(data)
    source = tmp_path / "sources" / "app.git"
    state = start(layer, link, root)
    assert state["status"] == "failed"
    assert [(s["name"], s["status"]) for s in state["steps"]] == [("plan", "passed"), ("build", "passed"), ("test", "failed")]
    delivered = state["remote"]["delivered"]
    assert delivered["stopped"] is True and delivered["step"] == "test"
    assert rf.git(source, "rev-parse", f"refs/heads/{BRANCH}") == delivered["sha"]
    assert rf.git(source, "show", f"{BRANCH}:feature.py") == "FEATURE = True"
    assert "needs a human" in rf.git(source, "log", "-1", "--format=%s", BRANCH)
    run_dir = rf.host_run_layer(root, RUN) / "agents" / RUN
    assert not (run_dir / "PR.md").exists() and "action" not in state["ship"]
    assert not [e for e in events.read(run_dir / "events.jsonl") if e["type"] == "handoff"]


def test_the_agent_token_comes_from_the_hosts_env_file_and_nothing_else_does(world, on_host, monkeypatch, tmp_path):
    data = rf.demo_ir()
    data["environments"].append({"id": "box", "kind": "podman", "status": "stable", "credentials": ["claude"]})
    data["adws"][0]["environment"] = ["worktree", "box"]
    data["registry"] = {"addressEnv": "AL_REGISTRY", "usernameEnv": "AL_REGISTRY_USER", "passwordEnv": "AL_REGISTRY_PASSWORD",
                        "tlsVerify": True}
    layer, root, _link = world(data, env={"ANTHROPIC_API_KEY": "sk-host-token", "OPENAI_API_KEY": "sk-other-vendor",
                                         "AL_REGISTRY_PASSWORD": "registry-secret", "AL_NOTIFY_TO": "ops@example.org"})
    deployed = on_host(root)
    spawned: list[dict] = []

    class Process:
        pid = 4242

    real = subprocess.Popen

    def popen(argv, **kw):
        if argv[0] != "uv":            # git, for the clone
            return real(argv, **kw)
        spawned.append({"argv": argv, **kw})
        return Process()

    monkeypatch.setattr(selfrun.subprocess, "Popen", popen)
    assert selfrun._tool_start(deployed, seed(layer))["started"] is True
    env = spawned[0]["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-host-token" and env["AL_SELF_CONTAINED"] == "1"
    assert env["AL_SLOTS_DIR"] == str(rf.host_storage(root) / "slots") and Path(env["AL_RUN_SEED"]).is_file()
    assert env["AL_APP_ROOT"] == str(rf.host_storage(root) / "runs" / RUN / "repo")
    for name in ("OPENAI_API_KEY", "AL_REGISTRY_PASSWORD", "AL_NOTIFY_TO", "AL_GIT_SOURCE", "AL_LAYER_SOURCE"):
        assert name not in env
    leaked = [k for k, v in env.items() if any(secret in v for secret in
                                               ("registry-secret", "sk-other-vendor", str(tmp_path / "sources")))]
    assert leaked == []
    assert spawned[0]["start_new_session"] is True and "--prompt-file" in spawned[0]["argv"]
    request = Path(spawned[0]["argv"][spawned[0]["argv"].index("--prompt-file") + 1])
    assert request.read_text() == "Add a feature\n" and oct(request.stat().st_mode & 0o777) == "0o600"
    assert oct(Path(env["AL_RUN_SEED"]).stat().st_mode & 0o777) == "0o600"


def _fake_podman():
    path = Path(__file__).resolve().parents[3] / "tests" / "tools" / "fake_podman.py"
    spec = importlib.util.spec_from_file_location("fake_podman", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == flag]


def _contains(argv: list[str], part: list[str]) -> bool:
    return any(argv[i:i + len(part)] == part for i in range(len(argv) - len(part) + 1))


def test_a_run_in_a_container_on_a_host_keeps_the_isolation_rules(world, make_layer, tmp_path, app_repo):
    fake = _fake_podman()
    box = {"id": "box", "kind": "podman", "status": "stable", "agentInside": True, "baseImage": "debian:stable-slim",
           "cpus": "1", "memory": "1g", "network": "none", "packages": [], "setup": [], "scope": "run", "caches": [],
           "credentials": ["claude"], "pidsLimit": 321, "readOnlyRoot": True}
    data = rf.demo_ir()
    data["environments"].append(box)
    data["adws"][0]["environment"] = ["worktree", "box"]
    recipe = make_layer(data) / "env" / "Containerfile"
    recipe.parent.mkdir()
    recipe.write_text("FROM debian:stable-slim\n")
    layer, root, link = world(data, env={"ANTHROPIC_API_KEY": "sk-host-token", "OPENAI_API_KEY": "sk-other-vendor",
                                         "AL_REGISTRY_PASSWORD": "registry-secret"})
    podman = tmp_path / "podman-build-1"
    fake.install(root / ".hostbin", podman)                      # the only container engine there is, on the host
    state = start(layer, link, root)
    storage = rf.host_storage(root) / "runs" / RUN
    assert state["status"] == "delivered", (storage / "tool.log").read_text()
    run_layer, clone_git = rf.host_run_layer(root, RUN), storage / "repo" / ".git"
    worktree = run_layer / "trees" / RUN
    calls = fake.calls(podman)
    starts = [c for c in calls if c[:2] == ["run", "-d"]]
    assert len(starts) == 1 and f"al-test-layer-{RUN}" in _flag_values(starts[0], "--name")
    started = starts[0]
    # the same hardening as at home
    flags = envs.hardening_flags(box)
    assert "--read-only" in flags and "321" in flags
    assert "--cap-drop=ALL" in started and "--security-opt=no-new-privileges" in started
    assert _contains(started, ["--pids-limit", "321"]) and _contains(started, ["--tmpfs", "/tmp:rw,mode=1777"])
    assert all(flag in started for flag in flags)
    assert "--userns=keep-id" in started and _contains(started, ["--network", "none"]) and "--privileged" not in started
    # the application's history is the clone's, read-only; the clone's working tree is not there at all
    mounts = [m.split(":") for m in _flag_values(started, "-v")]
    assert [m for m in mounts if m[0] == str(clone_git)] == [[str(clone_git), str(clone_git), "ro"]]
    writable = {m[0] for m in mounts if m[-1] not in ("ro", "O")}
    assert str(worktree) in writable and str(clone_git / "worktrees" / RUN) in writable
    inside_git = {m[0] for m in mounts if Path(m[0]) != clone_git and clone_git in Path(m[0]).parents}
    assert inside_git - {str(clone_git / "refs"), str(clone_git / "logs")} == {str(clone_git / "worktrees" / RUN)}
    assert {m[-1] for m in mounts if m[0] in (str(clone_git / "refs"), str(clone_git / "logs"))} <= {"O"}
    assert str(storage / "repo") not in {m[0] for m in mounts} and str(storage) not in {m[0] for m in mounts}
    assert not [m for m in mounts if str(app_repo) in m[0] or str(tmp_path / "sources") in m[0]]
    assert not [m for m in mounts if not Path(m[0]).is_relative_to(storage)]            # nothing of the host but the run
    # the declared login and no other
    in_home = [m for m in mounts if m[1].startswith(envs.CONTAINER_HOME)]
    assert in_home == [[str(run_layer / "agents" / RUN / "credentials" / "claude.json"),
                        f"{envs.CONTAINER_HOME}/.claude/.credentials.json", "ro"]]
    variables = {v.partition("=")[0] for c in calls if c[0] in ("run", "exec") for v in _flag_values(c, "-e")}
    assert "ANTHROPIC_API_KEY" in variables
    assert not variables & {"OPENAI_API_KEY", "AL_REGISTRY_PASSWORD", "AL_GIT_SOURCE", "AL_LAYER_SOURCE", "CLAUDE_CODE_OAUTH_TOKEN"}
    everything = "\n".join(arg for c in calls for arg in c)
    for secret in ("sk-other-vendor", "registry-secret", str(tmp_path / "sources")):
        assert secret not in everything
    # and the check ran in that container
    checks = [c for c in calls if c[0] == "exec" and c[-1] == "./test.sh"]
    assert checks and f"al-test-layer-{RUN}" in checks[0]
    assert [s["status"] for s in state["steps"]] == ["passed"] * 4
    assert rf.git(tmp_path / "sources" / "app.git", "show", f"{BRANCH}:feature.py") == "FEATURE = True"


def test_pull_image_uses_the_recipe_tag_and_checks_the_label(make_layer, tmp_path, monkeypatch):
    fake = _fake_podman()
    data = rf.demo_ir()
    data["environments"].append({"id": "box", "kind": "podman", "status": "stable", "baseImage": "debian:stable-slim"})
    data["registry"] = {"addressEnv": "AL_REGISTRY", "usernameEnv": "", "passwordEnv": "", "tlsVerify": True}
    layer_dir = make_layer(data)
    (layer_dir / "env").mkdir()
    (layer_dir / "env" / "Containerfile").write_text("FROM debian:stable-slim\n")
    layer = ir.load(layer_dir)
    recipe = sandbox.recipe_hash(layer)
    address = "registry.invalid/team"
    remote = f"{address}/test-layer:recipe-{recipe}"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("AL_SELF_CONTAINED", "1")

    def machine(name: str, label: str | None):
        state, registry, bin_dir = tmp_path / name / "podman", tmp_path / name / "registry", tmp_path / name / "bin"
        bin_dir.mkdir(parents=True)
        registry.mkdir()
        fake.install(bin_dir, state, registry)
        if label is not None:
            (registry / (fake.norm(remote).replace("/", "__") + ".json")).write_text(
                json.dumps({"id": "abc123", "labels": {"al.recipe": label}}))
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        return state

    def tags(state: Path) -> set[str]:
        return {tag for image in fake.images(state) for tag in image["tags"]}

    # in the registry, built from this recipe: pulled by its recipe tag, tagged as the layer's image, nothing built
    monkeypatch.setenv("AL_REGISTRY", address)
    state = machine("pulls", recipe)
    said: list[str] = []
    result = sandbox.ensure_image(layer, say=said.append)
    assert result["built"] is False and result["image"] == f"al-test-layer:{recipe}"
    calls = fake.calls(state)
    assert ["pull", remote] in calls and not [c for c in calls if c[0] == "build"]
    assert {f"al-test-layer:{recipe}", "al-test-layer:latest"} <= tags(state)
    # the label differs: not used, and the image is built here
    state = machine("differs", "f" * 12)
    said.clear()
    assert selfrun.pull_image(layer) is False and "another recipe" in selfrun.pull_image.reason
    assert f"al-test-layer:{recipe}" not in tags(state)
    result = sandbox.ensure_image(layer, say=said.append)
    assert result["built"] is True and [c for c in fake.calls(state) if c[0] == "build"]
    assert any(line.startswith("image: not in the registry (") and line.endswith("; building it here") for line in said)
    # no address on this machine: no pull at all
    monkeypatch.delenv("AL_REGISTRY")
    state = machine("unset", recipe)
    assert selfrun.pull_image(layer) is False and "AL_REGISTRY is not set" in selfrun.pull_image.reason
    assert sandbox.ensure_image(layer, say=said.append)["built"] is True
    assert not [c for c in fake.calls(state) if c[0] == "pull"]
    # and at home (not a self-contained run) the registry is never asked
    monkeypatch.setenv("AL_REGISTRY", address)
    monkeypatch.delenv("AL_SELF_CONTAINED")
    state = machine("home", recipe)
    assert sandbox.ensure_image(layer, say=said.append)["built"] is True
    assert not [c for c in fake.calls(state) if c[0] == "pull"]


def test_tool_load_is_one_json_document(world):
    layer, root, link = world()
    script = f"cd {hostlink.host_layer_dir(layer, 'build-1')} && uv run --quiet adws/host.py load"
    done = link.run(script)
    assert done.returncode == 0 and done.stderr == b""
    assert done.stdout.count(b"\n") == 1
    load = json.loads(done.stdout)
    assert sorted(load) == sorted(["protocol", "runs", "max_runs", "cpus", "load1", "mem_total_kb", "mem_available_kb",
                                   "disk_free_kb", "image", "logins"])
    assert load["protocol"] == selfrun.PROTOCOL and load["runs"] == 0 and load["max_runs"] == 4
    assert load["cpus"] >= 1 and load["mem_total_kb"] > 0 and load["disk_free_kb"] > 0 and load["image"] is None
    assert load["logins"] == {"mock": "not needed"}
    text = done.stdout.decode()
    for private in (str(root), socket.gethostname(), os.environ.get("USER") or "no-user-name", "/"):
        assert private not in text
    # a run in flight is counted
    start(layer, link, root, wait=False)
    assert link.tool(layer, "build-1", "load")["runs"] == 1
    assert link.run(f"cd {hostlink.host_layer_dir(layer, 'build-1')} && uv run --quiet adws/host.py nonsense").returncode == 2


def test_tool_doctor_says_which_login_the_host_has(tmp_path, make_layer, app_repo, monkeypatch):
    data = rf.demo_ir()
    data["agents"].append({"id": "claude", "kind": "claude-code", "status": "stable"})
    layer_dir = rf.build_layer_repo(tmp_path, make_layer, app_repo, data)
    rf.sources(tmp_path, app_repo, layer_dir)
    rf.host_home(tmp_path, "build-1", layer_dir, login=True)
    rf.host_home(tmp_path, "build-2", layer_dir, login=False, env={"ANTHROPIC_API_KEY": "sk-host-token"})
    rf.host_home(tmp_path, "build-3", layer_dir, login=False)
    for key, value in rf.local_hosts_env(tmp_path, ("build-1", "build-2", "build-3")).items():
        monkeypatch.setenv(key, value)
    layer = ir.load(layer_dir)
    found = {}
    for host in ("build-1", "build-2", "build-3"):
        answer = hostlink.link_for(layer, host).tool(layer, host, "doctor", timeout=60)
        found[host] = answer["logins"]["claude-code"]
        rows = {row["name"]: row for row in answer["rows"]}
        assert rows["git"]["ok"] and rows["uv"]["ok"] and rows["git source"] == {
            "name": "git source", "ok": True, "detail": "answers", "fix": ""}
        assert rows["login claude-code"]["ok"] is (host != "build-3")
        assert "sk-host-token" not in json.dumps(answer) and str(tmp_path) not in json.dumps(answer)
        if host == "build-3":
            assert "ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN" in rows["login claude-code"]["fix"]
    assert found == {"build-1": "own login", "build-2": "token ANTHROPIC_API_KEY in its env file", "build-3": "none"}


def test_tool_start_is_idempotent_per_run_id(world):
    layer, root, link = world()
    state = start(layer, link, root)
    assert state["status"] == "delivered"
    again = link.tool(layer, "build-1", "start", stdin=json.dumps(seed(layer)), timeout=120)
    assert again == {"protocol": selfrun.PROTOCOL, "run": RUN, "started": False, "existing": True}
    found = events.read(rf.host_run_layer(root, RUN) / "agents" / RUN / "events.jsonl")
    assert [e["type"] for e in found].count("run.start") == 1
    # a seed of another protocol, or of a workflow this host does not have, starts nothing
    for bad in ({"protocol": 99}, {"adw": "nothing-like-it"}):
        with pytest.raises(hostlink.HostSilent):
            link.tool(layer, "build-1", "start", stdin=json.dumps(seed(layer, "5e1f1002", **bad)), timeout=120)
        assert not (rf.host_storage(root) / "runs" / "5e1f1002").exists()


def test_tool_clean_removes_only_a_finished_run(world):
    data = rf.demo_ir()
    data["codebase"]["commands"][0]["run"] = "sleep 4; ./test.sh"
    layer, root, link = world(data)
    start(layer, link, root, wait=False)
    storage = rf.host_storage(root) / "runs" / RUN
    refused = link.tool(layer, "build-1", "clean", RUN)
    assert refused["removed"] is False and refused["reason"] == "the run is still running" and storage.is_dir()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        facts = link.tool(layer, "build-1", "runs")["runs"]
        if facts and facts[0]["status"] == "delivered":
            break
        time.sleep(0.3)
    time.sleep(1.0)
    assert link.tool(layer, "build-1", "clean", RUN)["removed"] is True and not storage.exists()
    assert link.tool(layer, "build-1", "clean", RUN) == {"protocol": selfrun.PROTOCOL, "run": RUN, "removed": False,
                                                        "reason": "no such run"}
    assert link.tool(layer, "build-1", "runs")["runs"] == []
    with pytest.raises(hostlink.HostSilent):
        link.tool(layer, "build-1", "pull", "../../etc", stdin="{}", raw=True)
