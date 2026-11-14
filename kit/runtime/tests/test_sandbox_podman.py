"""Escape attempts from inside a REAL rootless podman container, asserted on the host.

Skipped cleanly when podman or a usable local image is missing. Nothing large is pulled: the
git-enabled test image is derived (once, cached as al-sandbox-test:latest) from an image that
is already present locally."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from adw_modules import envs, gitsafe, steps
from adw_modules.errors import StepFailed
from adw_modules.ir import load
from adw_modules.runner import main
from adw_modules.state import ADWState

pytestmark = pytest.mark.podman

TEST_IMAGE = "localhost/al-sandbox-test:v2"  # bump when the recipe below changes (the image is cached)
BASES = ("al-flask-envtest:latest", "docker.io/library/python:3.12-slim")


def _podman(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def sandbox_image() -> str:
    if shutil.which("podman") is None:
        pytest.skip("podman is not installed")
    if _podman("info").returncode != 0:
        pytest.skip("podman is installed but not usable here")
    if _podman("image", "exists", TEST_IMAGE).returncode == 0:
        return TEST_IMAGE
    base = next((image for image in BASES if _podman("image", "exists", image).returncode == 0), None)
    if base is None:
        pytest.skip(f"none of the local base images exist ({', '.join(BASES)}); not pulling anything")
    with tempfile.TemporaryDirectory() as context:
        # No USER line: an explicit image user would override --userns=keep-id's default (the host
        # uid), and git inside would then refuse the worktree as "dubious ownership".
        (Path(context) / "Containerfile").write_text(
            f"FROM {base}\nRUN apt-get update && apt-get install -y --no-install-recommends git"
            " && rm -rf /var/lib/apt/lists/*\n", encoding="utf-8")
        try:
            built = _podman("build", "-t", TEST_IMAGE, "-f", "Containerfile", context, timeout=900)
        except subprocess.TimeoutExpired:
            pytest.skip("building the git test image timed out (slow network?)")
    if built.returncode != 0:
        pytest.skip(f"could not build the git test image from {base}: {built.stderr.strip()[-300:]}")
    return TEST_IMAGE


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _options(network: str = "none", keep: str = "never") -> list[dict]:
    return [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 24800, "portsPerRun": 0,
             "keep": keep, "status": "stable"},
            {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "1",
             "memory": "1g", "network": network, "packages": [], "setup": [], "status": "stable", "scope": "run",
             "caches": [], "credentials": []}]


@pytest.fixture
def stack(make_layer, sandbox_image, monkeypatch, request):
    monkeypatch.setattr(envs.EnvStack, "image", property(lambda self: sandbox_image))
    layer = load(make_layer({"environments": _options()}))
    adw_id = "5b" + hashlib.sha1(request.node.name.encode()).hexdigest()[:6]
    state = ADWState(adw_id, "test", prompt="escape attempt", klass="chore")
    env_stack = envs.build(layer, state, ["wt", "box"])
    env_stack.setup()
    yield env_stack
    try:
        env_stack.teardown(success=True)
    except StepFailed:
        pass  # tests that tamper on purpose leave a refused worktree behind
    finally:
        _podman("rm", "-f", f"al-{layer.name}-{adw_id}")


def _inside(env_stack: envs.EnvStack, cmd: str) -> tuple[int, str]:
    return steps.run_command(env_stack.wrap(cmd), env_stack.cwd, env_stack.process_env(), 120)


def _snapshot(app: Path) -> dict[str, str]:
    """Everything in the app's .git that could carry host code execution or ref moves."""
    git = app / ".git"
    files = {str(path.relative_to(git)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(git.glob("hooks/*")) + [git / "config", git / "HEAD"]
             + sorted(git.glob("packed-refs")) + sorted(git.glob("info/*")) if path.is_file()}
    files["for-each-ref"] = _git(app, "for-each-ref", "--format=%(refname) %(objectname)")
    files["hooks-listing"] = " ".join(sorted(p.name for p in (git / "hooks").iterdir()))
    files["status"] = _git(app, "status", "--porcelain")
    files["head-symbolic"] = _git(app, "symbolic-ref", "HEAD")
    return files


def test_container_cannot_change_app_hooks_config_or_refs(stack, app_repo) -> None:
    app_git = app_repo / ".git"
    other = _git(app_repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "other")
    before = _snapshot(app_repo)
    hook, _ = _inside(stack, f"printf '#!/bin/sh\\ntouch /tmp/pwned\\n' > {app_git}/hooks/pre-commit")
    config, _ = _inside(stack, f"printf '[core]\\n\\tfsmonitor = touch /tmp/pwned\\n' >> {app_git}/config")
    update_ref, update_out = _inside(stack, f"git update-ref refs/heads/main {other}")
    branch, branch_out = _inside(stack, "git branch evil")
    assert hook != 0 and config != 0                     # read-only file system
    # ref writes only reach the container's private overlay of refs/: harmless either way
    print("update-ref exit", update_ref, update_out, "branch exit", branch, branch_out)
    assert _snapshot(app_repo) == before
    assert _git(app_repo, "rev-parse", "refs/heads/main") != other
    assert _git(app_repo, "branch", "--list", "evil") == ""


def test_container_sees_no_app_tree_no_home_and_no_network(stack, app_repo) -> None:
    code, out = _inside(stack, f"test ! -e {app_repo}/app.py && echo planted > {app_repo}/planted.txt; "
                               f"test ! -e {app_repo}/app.py")
    assert code == 0, out                                   # the app working tree is simply not there
    assert not (app_repo / "planted.txt").exists()          # a write to that path stays in the container
    home = Path.home()
    visible = [entry.name for entry in sorted(home.iterdir())][:5]
    checks = " && ".join(f"test ! -e '{home / name}'" for name in [".ssh", *visible])
    code, out = _inside(stack, checks)
    assert code == 0, f"host home leaked into the container: {out}"
    code, out = _inside(stack, "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\"")
    assert code != 0, "network: none must make the internet unreachable"
    assert _git(app_repo, "status", "--porcelain") == ""


def test_commit_inside_lands_in_quarantine_only(stack, app_repo) -> None:
    sandbox = stack.sandbox
    head, symbolic = _git(app_repo, "rev-parse", "HEAD"), _git(app_repo, "symbolic-ref", "HEAD")
    code, out = _inside(stack, "set -e; git status --porcelain; git ls-files; echo change >> app.py; git diff --stat; "
                               "git add -A; git -c user.name=t -c user.email=t@t commit -qm inside; "
                               "echo more >> app.py; git stash -q; git stash list; git stash pop -q; "
                               "git -c user.name=t -c user.email=t@t commit -qam second; git rev-parse HEAD")
    assert code == 0, out
    commit = out.strip().splitlines()[-1]
    assert (sandbox.quarantine / commit[:2] / commit[2:]).is_file()
    assert not (app_repo / ".git" / "objects" / commit[:2] / commit[2:]).exists()
    assert _git(app_repo, "cat-file", "-e", commit, check=False) == "" and \
        subprocess.run(["git", "-C", str(app_repo), "cat-file", "-e", commit], capture_output=True).returncode != 0
    assert _git(app_repo, "status", "--porcelain") == ""
    assert (_git(app_repo, "rev-parse", "HEAD"), _git(app_repo, "symbolic-ref", "HEAD")) == (head, symbolic)
    assert _git(app_repo, "rev-parse", "--verify", "--quiet", "refs/stash", check=False) == ""
    # the host still reads the sandboxed history through gitsafe
    log = gitsafe.run(sandbox, ["log", "--format=%s", "-3"], "test").stdout.split()
    assert log[:2] == ["second", "inside"]


def test_host_freezes_container_while_running_git(stack) -> None:
    _inside(stack, "nohup sh -c 'while :; do sleep 0.1; done' >/dev/null 2>&1 &")
    name = stack._container
    with gitsafe.session(stack.sandbox):
        assert _podman("inspect", "-f", "{{.State.Status}}", name).stdout.strip() == "paused"
        gitsafe.run(stack.sandbox, ["status"], "test")
    assert _podman("inspect", "-f", "{{.State.Status}}", name).stdout.strip() == "running"


@pytest.mark.parametrize("attack", [
    "printf '[core]\\n\\tpager = touch /tmp/pwned\\n' > $ADMIN/config.worktree",
    "mkdir -p $ADMIN/hooks && printf '#!/bin/sh\\ntouch /tmp/pwned\\n' > $ADMIN/hooks/post-commit",
    "printf 'ref: refs/heads/main\\n' > $ADMIN/HEAD",
    "printf '../../../../tmp/evil\\n' > $ADMIN/commondir",
    "printf 'gitdir: /tmp/evil\\n' > .git",
    "ln -s /etc $GIT_OBJECT_DIRECTORY/ab",
], ids=["config.worktree", "admin-hooks", "symbolic-HEAD", "commondir", "dot-git-file", "quarantine-symlink"])
def test_tampering_from_inside_is_refused_by_the_host(stack, app_repo, attack) -> None:
    before = _snapshot(app_repo)
    code, out = _inside(stack, f"ADMIN={stack.sandbox.admin}; {attack}")
    assert code == 0, out                                  # the container CAN write these …
    with pytest.raises(StepFailed) as raised:              # … but the host refuses to use them
        gitsafe.run(stack.sandbox, ["status"], "test")
    assert raised.value.step == "git-integrity"
    with pytest.raises(StepFailed):
        stack.teardown(success=True)                       # no publish, worktree left for inspection
    assert _snapshot(app_repo) == before


def test_end_to_end_sandboxed_mock_run(make_layer, app_repo, sandbox_image, monkeypatch) -> None:
    monkeypatch.setattr(envs.EnvStack, "image", property(lambda self: sandbox_image))
    adw_id = "5c5c0001"
    layer = make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
                     {"name": "build", "template": "build", "modelSet": "base", "output": "text"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"commands": [{"name": "test", "timeout": 120, "run":
                                   "test \"$(id -u)\" != 0 && python3 -B -c 'import feature; assert feature.FEATURE' && "
                                   "git status --porcelain >/dev/null && git diff HEAD --stat >/dev/null && "
                                   "git ls-files --cached --others --exclude-standard | grep -q feature.py"}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": ["test"], "ship": "merge"}],
        "environments": _options(),
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["wt", "box"],
                  "classes": ["chore"], "steps": [
                      {"name": "plan", "kind": "run", "command": "plan"},
                      {"name": "build", "kind": "run", "command": "build"},
                      {"name": "test", "kind": "check", "cmd": "test"},
                      {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    })
    script = layer / "mock" / "demo" / "build.json"
    script.parent.mkdir(parents=True)
    script.write_text(json.dumps({"files": {"feature.py": "FEATURE = True\n"}}), encoding="utf-8")
    before = _snapshot(app_repo)
    try:
        assert main("demo", layer, ["Add a feature", "--adw-id", adw_id]) == 0, \
            (layer / f"agents/{adw_id}/run.log").read_text()
    finally:
        _podman("rm", "-f", f"al-test-layer-{adw_id}")
    state = json.loads((layer / f"agents/{adw_id}/state.json").read_text())
    assert state["status"] == "passed" and state["checks"]["test"]["exit"] == 0
    assert state["ship"]["action"] == "merge" and state["ship"]["target"] == "al/ship/main"
    assert _git(app_repo, "show", "al/ship/main:feature.py") == "FEATURE = True"
    assert state["ship"]["commit"] == _git(app_repo, "rev-parse", "al/ship/main")
    fsck = subprocess.run(["git", "-C", str(app_repo), "fsck", "--connectivity-only"], capture_output=True, text=True)
    assert fsck.returncode == 0 and "missing" not in fsck.stdout + fsck.stderr, fsck.stdout + fsck.stderr
    assert not (layer / "agents" / adw_id / "objects").exists()            # quarantine removed
    assert not Path(state["worktree"]).exists()
    assert not (app_repo / "feature.py").exists()                          # app working tree untouched
    after = _snapshot(app_repo)
    assert {k: v for k, v in after.items() if k != "for-each-ref"} == {k: v for k, v in before.items() if k != "for-each-ref"}
    assert set(after["for-each-ref"].splitlines()) - set(before["for-each-ref"].splitlines()) == \
        {f"refs/heads/al/ship/main {state['ship']['commit']}"}             # the only ref change: the shipment
    assert _podman("container", "exists", f"al-test-layer-{adw_id}").returncode != 0
