"""The nested container engine against REAL rootless podman: a test inside the run's sandbox builds
and starts a container, which cannot see or affect the host's containers; the run's container
keeps its hardening; inner processes stay in the run's cgroup; the engine's volume goes with the run.

Skipped cleanly without podman, a local base image (see test_sandbox_podman.sandbox_image) or apt
network for the engine image. Nothing is pulled, also not inside: inner images are `FROM scratch`
with the image's static busybox. The run tests need the one-line change to the protected envs.py
(docs/DESIGN.md "An engine for the sandbox"); until a person applies it they skip with the
REQUIRES HUMAN reason, and test_preflight_refuses_a_nested_run_when_envs_is_not_wired runs instead.

With AL_SANDBOX_PROOF=<dir>: nested-isolation.txt, nested-run-summary.txt and doctor-engine.txt."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import envs, nested_engine, sandbox, steps
from adw_modules.ir import load
from adw_modules.runner import main
from test_sandbox_podman import _podman, sandbox_image  # noqa: F401 — the fixture is reused

pytestmark = pytest.mark.podman

ENGINE_IMAGE = "localhost/al-test-engine:1"   # bump when the recipe below changes (the image is cached)
NOT_WIRED = "REQUIRES HUMAN: envs.py does not pass nested_engine.podman_flags yet"
INNER = ("d=$(mktemp -d) && cp /bin/busybox \"$d/busybox\" && "
         "printf 'FROM scratch\\nCOPY busybox /busybox\\n' > \"$d/Containerfile\" && "
         "podman build -q -t localhost/al-inner:1 \"$d\" >/dev/null")


def _proof(name: str, text: str) -> None:
    target = os.environ.get("AL_SANDBOX_PROOF")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / name).write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def engine_image(sandbox_image) -> str:  # noqa: F811
    if _podman("image", "exists", ENGINE_IMAGE).returncode == 0:
        return ENGINE_IMAGE
    packages = " ".join([*nested_engine.PACKAGES, "busybox-static"])
    with tempfile.TemporaryDirectory() as context:
        (Path(context) / "containers.conf").write_text(nested_engine.CONTAINERS_CONF, encoding="utf-8")
        (Path(context) / "storage.conf").write_text(nested_engine.STORAGE_CONF, encoding="utf-8")
        (Path(context) / "Containerfile").write_text(
            f"FROM {sandbox_image}\nRUN apt-get update && apt-get install -y --no-install-recommends {packages}"
            " && rm -rf /var/lib/apt/lists/*\nCOPY containers.conf storage.conf /etc/containers/\n", encoding="utf-8")
        try:
            built = _podman("build", "-t", ENGINE_IMAGE, "-f", "Containerfile", context, timeout=1800)
        except subprocess.TimeoutExpired:
            pytest.skip("building the engine test image timed out (slow network?)")
    if built.returncode != 0:
        tail = "\n".join((built.stdout + built.stderr).strip().splitlines()[-5:])
        pytest.skip(f"could not build the engine test image (no network for apt?):\n{tail}")
    return ENGINE_IMAGE


def _options(**box) -> list[dict]:
    return [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 25300, "portsPerRun": 0,
             "keep": "never", "status": "stable"},
            {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "local", "cpus": "1", "memory": "1g",
             "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
             "credentials": [], "nestedEngine": True, "pidsLimit": 1024, **box}]


def _nested_wired() -> bool:
    """Does the protected envs.py pass nested_engine.podman_flags? A throwaway EnvStack on a
    nestedEngine option answers it; nothing is started."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        layer = SimpleNamespace(name="wired-probe", layer_dir=root, app_root=root / "app", ir={"services": []})
        state = SimpleNamespace(adw_id="0b0b0b0b", worktree=str(root / "trees" / "0b0b0b0b"), ports=[])
        stack = envs.EnvStack(layer, state, _options())
        return "/dev/fuse" in stack._podman_base()


@pytest.fixture
def wired() -> None:
    if not _nested_wired():
        pytest.skip(NOT_WIRED)


def _layer(make_layer, app_repo: Path, engine_image: str, script: str, **check) -> Path:
    (app_repo / "engine-test.sh").write_text(script, encoding="utf-8")
    subprocess.run(["git", "-C", str(app_repo), "add", "engine-test.sh"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(app_repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm",
                    "engine test"], check=True, capture_output=True)
    layer_dir = make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "codebase": {"commands": [{"name": "engine-test", "timeout": 600, "run": "sh engine-test.sh"}]},
        "environments": _options(),
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False}],
        "adws": [{"name": "zte", "agent": "mock", "review": "human", "environment": ["wt", "box"],
                  "classes": ["chore"], "steps": [
                      {"name": "engine-test", "kind": "check", "cmd": "engine-test", "resolve": "", "retries": 0,
                       "collect": [], "in": "", "maxSkipped": -1, "forbidSkipReason": "no podman", **check}]}],
    })
    (layer_dir / "env").mkdir(exist_ok=True)
    (layer_dir / "env" / "Containerfile").write_text(
        f"FROM {engine_image}\nRUN echo engine-{secrets.token_hex(4)} > /tmp/recipe\n", encoding="utf-8")
    return layer_dir


@pytest.fixture
def cleanup():
    layers: list = []
    yield layers.append
    for layer in layers:
        for row in sandbox.layer_images(layer):
            for ref in row["refs"]:
                _podman("rmi", ref)
    _podman("rmi", "al-test-layer:latest")


ENGINE_TEST = f"""#!/bin/sh
set -e
{INNER}
podman run --rm localhost/al-inner:1 /busybox echo inner-ok
echo "1 passed in 0.10s"
"""


def _throwaway_stack(make_layer, app_repo, engine_image, cleanup, monkeypatch):
    layer_dir = _layer(make_layer, app_repo, engine_image, ENGINE_TEST)
    layer = load(layer_dir)
    cleanup(layer)
    monkeypatch.setattr(envs.EnvStack, "image", property(lambda self: engine_image))
    return layer, sandbox.throwaway(layer, ["wt", "box"], "nested")


def _inside(stack: envs.EnvStack, cmd: str) -> tuple[int, str]:
    return steps.run_command(stack.wrap(cmd), stack.cwd, stack.process_env(), 600)


def test_inner_container_cannot_see_or_affect_a_host_container(make_layer, app_repo, engine_image, cleanup,
                                                               monkeypatch, wired, sandbox_image) -> None:  # noqa: F811
    victim = f"al-victim-{secrets.token_hex(4)}"
    started = _podman("run", "-d", "--name", victim, sandbox_image, "sleep", "600")
    assert started.returncode == 0, started.stderr
    try:
        layer, throwaway = _throwaway_stack(make_layer, app_repo, engine_image, cleanup, monkeypatch)
        with throwaway as stack:
            code, ps = _inside(stack, "podman ps -a --format '{{.Names}}'")
            assert code == 0, ps
            code, images = _inside(stack, "podman images --format '{{.Repository}}:{{.Tag}}'")
            assert code == 0, images
            stop_code, stop_out = _inside(stack, f"podman stop -t 0 {victim}")
            rm_code, rm_out = _inside(stack, f"podman rm -f {victim}")
            code, inner = _inside(stack, f"{INNER} && podman run --rm localhost/al-inner:1 /busybox echo inner-ok")
            assert code == 0 and "inner-ok" in inner, inner
            inspect = json.loads(_podman("inspect", stack.run_name).stdout or "[{}]")[0]
        assert victim not in ps
        assert sandbox_image.removeprefix("localhost/").split(":")[0] not in images
        assert stop_code != 0
        assert rm_code != 0 or "no such container" in rm_out.lower()
        state = _podman("inspect", "--format", "{{.State.Status}}", victim).stdout.strip()
        assert state == "running"
        host_ps = _podman("ps", "-a", "--format", "{{.Names}} {{.Image}}").stdout
        assert "al-inner" not in host_ps
        host = inspect.get("HostConfig", {})
        _proof("nested-isolation.txt", "\n".join([
            f"inner podman ps -a:\n{ps or '(none)'}", f"inner podman images:\n{images or '(none)'}",
            f"inner podman stop {victim}: exit {stop_code}: {stop_out.strip()}",
            f"inner podman rm -f {victim}: exit {rm_code}: {rm_out.strip()}",
            f"inner build + run: {inner.strip()}",
            f"host: {victim} is {state}; host podman ps -a:\n{host_ps}",
            f"outer CapDrop: {host.get('CapDrop')}", f"outer CapAdd: {host.get('CapAdd')}",
            f"outer SecurityOpt: {host.get('SecurityOpt')}",
            f"outer Devices: {host.get('Devices')}", ""]))
    finally:
        _podman("rm", "-f", "-t", "0", victim)


def test_outer_container_keeps_its_hardening(make_layer, app_repo, engine_image, cleanup, monkeypatch, wired) -> None:
    layer, throwaway = _throwaway_stack(make_layer, app_repo, engine_image, cleanup, monkeypatch)
    with throwaway as stack:
        code, status = _inside(stack, "grep -E '^(CapEff|CapPrm|NoNewPrivs):' /proc/self/status")
        assert code == 0, status
        inspect = json.loads(_podman("inspect", stack.run_name).stdout or "[{}]")[0]
        checks = sandbox.doctor(layer, stack, layer.adw("zte"))
    host = inspect.get("HostConfig", {})
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    assert fields["CapEff"].strip() == "0000000000000000" and fields["CapPrm"].strip() == "0000000000000000"
    assert fields["NoNewPrivs"].strip() == "1"
    assert not host.get("CapAdd")
    assert len(host.get("CapDrop") or []) >= 10
    assert [d.get("PathOnHost") for d in host.get("Devices") or []] == ["/dev/fuse"]
    assert not host.get("Privileged")
    table = sandbox.format_table(checks)
    _proof("doctor-engine.txt", table + "\n" + sandbox.summary(checks) + "\n")
    rows = {check.name: check for check in checks}
    assert all(rows[name].status == "ok" for name in nested_engine.ROWS), table


def test_inner_processes_stay_in_the_run_cgroup(make_layer, app_repo, engine_image, cleanup, monkeypatch, wired) -> None:
    layer, throwaway = _throwaway_stack(make_layer, app_repo, engine_image, cleanup, monkeypatch)
    with throwaway as stack:
        code, outer = _inside(stack, "cat /proc/self/cgroup")
        assert code == 0, outer
        code, inner = _inside(stack, f"{INNER} && podman run --rm localhost/al-inner:1 /busybox cat /proc/self/cgroup")
        assert code == 0, inner
        code, memory = _inside(stack, "cat /sys/fs/cgroup/memory.max")
    assert inner.strip().splitlines()[-1] == outer.strip().splitlines()[-1]
    if code != 0:
        pytest.skip(f"memory.max is not readable in the container: {memory.strip()}")
    assert memory.strip() == str(1024 ** 3)   # #:memory "1g"


def test_engine_volume_is_removed_with_the_run(make_layer, app_repo, engine_image, cleanup, monkeypatch, wired) -> None:
    layer, throwaway = _throwaway_stack(make_layer, app_repo, engine_image, cleanup, monkeypatch)
    with throwaway as stack:
        volume = nested_engine.volume_name(layer, stack.state)
        code, out = _inside(stack, f"{INNER} && podman images --format '{{{{.Repository}}}}'")
        assert code == 0 and "al-inner" in out, out
        assert _podman("volume", "exists", volume).returncode == 0
    assert _podman("volume", "exists", volume).returncode != 0


def _summary(layer_dir: Path, adw_id: str) -> str:
    log = (layer_dir / "agents" / adw_id / "run.log").read_text()
    return log[log.index("=== ADW summary ==="):] if "=== ADW summary ===" in log else log[-3000:]


def test_fixture_workflow_runs_a_test_that_builds_and_starts_a_container(make_layer, app_repo, engine_image, cleanup,
                                                                         wired) -> None:
    layer_dir = _layer(make_layer, app_repo, engine_image, ENGINE_TEST)
    cleanup(load(layer_dir))
    adw_id = "7e7e0001"
    try:
        code = main("zte", layer_dir, ["Run the engine test", "--adw-id", adw_id])
    finally:
        _podman("rm", "-f", f"al-test-layer-{adw_id}")
    summary = _summary(layer_dir, adw_id)
    _proof("nested-run-summary.txt", summary)
    assert code == 0, summary
    state = json.loads((layer_dir / "agents" / adw_id / "state.json").read_text())
    record = state["checks"]["engine-test"]
    assert record["exit"] == 0 and record["skips"]["skipped"] == 0
    assert "inner-ok" in (layer_dir / record["log"]).read_text()
    assert _podman("volume", "exists", f"al-engine-test-layer-{adw_id}").returncode != 0


def test_forbidden_skip_fails_the_real_check(make_layer, app_repo, engine_image, cleanup, wired) -> None:
    script = "#!/bin/sh\necho 'SKIPPED [1] t.py:3: no podman'\necho '1 skipped in 0.01s'\n"
    layer_dir = _layer(make_layer, app_repo, engine_image, script)
    cleanup(load(layer_dir))
    adw_id = "7e7e0002"
    try:
        assert main("zte", layer_dir, ["Run the engine test", "--adw-id", adw_id]) == 1
    finally:
        _podman("rm", "-f", f"al-test-layer-{adw_id}")
    state = json.loads((layer_dir / "agents" / adw_id / "state.json").read_text())
    record = state["checks"]["engine-test"]
    assert record["exit"] == 1
    log = (layer_dir / record["log"]).read_text().rstrip().splitlines()
    assert log[-1] == ('al: check engine-test fails: 1 tests skipped because "no podman", '
                       'forbidden by #:forbid-skip-reason "no podman"')


def test_preflight_refuses_a_nested_run_when_envs_is_not_wired(make_layer, app_repo, engine_image, cleanup) -> None:
    if _nested_wired():
        pytest.skip("envs.py passes the nested-engine flags: the run tests above cover a wired engine")
    layer_dir = _layer(make_layer, app_repo, engine_image, ENGINE_TEST)
    cleanup(load(layer_dir))
    adw_id = "7e7e0003"
    try:
        assert main("zte", layer_dir, ["Run the engine test", "--adw-id", adw_id]) == 1
    finally:
        _podman("rm", "-f", f"al-test-layer-{adw_id}")
    log = (layer_dir / "agents" / adw_id / "run.log").read_text()
    start = log.find("the sandbox is missing what this workflow needs")
    assert start >= 0, log[-3000:]
    _proof("doctor-engine.txt", log[start:start + 4000])
    assert "engine:fuse" in log and "envs.py" in log
    state = json.loads((layer_dir / "agents" / adw_id / "state.json").read_text())
    assert state["steps"] == []
    assert _podman("volume", "exists", f"al-engine-test-layer-{adw_id}").returncode != 0
