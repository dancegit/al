"""The nested container engine (adw_modules/nested_engine.py): flags, configuration, volume
lifecycle, doctor rows, host checks and the preflight refusal. podman is faked here; the same
behaviour against real rootless podman is in test_nested_engine_podman.py."""
from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import envs, events, nested_engine, sandbox
from adw_modules.errors import StepFailed
from adw_modules.runner import main

ADW_ID = "e9e90001"
LAYER = SimpleNamespace(name="test-layer", layer_dir=Path("/tmp/layer-x"))
STATE = SimpleNamespace(adw_id=ADW_ID)


def _option(**extra) -> dict:
    return {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian:stable-slim", "cpus": "1",
            "memory": "1g", "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run",
            "caches": [], "credentials": [], "nestedEngine": True, **extra}


class FakePodman:
    """Records podman calls and answers them from `answers` {tuple(args prefix): (code, stdout, stderr)}."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[list[str]] = []
        self.answers: dict[tuple[str, ...], tuple[int, str, str]] = {}
        self.missing = False
        real = subprocess.run
        fake = self

        def run(argv, **kwargs):
            if not argv or argv[0] != "podman":
                return real(argv, **kwargs)
            if fake.missing:
                raise FileNotFoundError("podman")
            fake.calls.append(list(argv[1:]))
            for prefix, (code, out, err) in sorted(fake.answers.items(), key=lambda item: -len(item[0])):
                if tuple(argv[1:1 + len(prefix)]) == prefix:
                    return SimpleNamespace(returncode=code, stdout=out, stderr=err)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(subprocess, "run", run)


@pytest.fixture
def fake_podman(monkeypatch) -> FakePodman:
    return FakePodman(monkeypatch)


def test_flags_are_empty_without_the_option():
    assert nested_engine.podman_flags(None, LAYER, STATE) == []
    assert nested_engine.podman_flags(_option(nestedEngine=False), LAYER, STATE) == []
    assert nested_engine.podman_flags({k: v for k, v in _option().items() if k != "nestedEngine"}, LAYER, STATE) == []


def test_flags_add_fuse_and_the_run_volume_only():
    flags = nested_engine.podman_flags(_option(), LAYER, STATE)
    assert flags == ["--device", "/dev/fuse", "-v", f"al-engine-test-layer-{ADW_ID}:/al-engine:U"]
    text = " ".join(flags)
    for forbidden in ("--cap-add", "--privileged", "seccomp", "apparmor", "unmask", "no-new-privileges"):
        assert forbidden not in text


def test_configs_keep_inner_containers_in_the_run_namespaces_and_cgroup():
    conf, storage = nested_engine.CONTAINERS_CONF, nested_engine.STORAGE_CONF
    for line in ('netns = "host"', 'pidns = "host"', 'userns = "host"', 'ipcns = "host"', 'utsns = "host"',
                 'cgroupns = "host"', 'cgroups = "disabled"', 'log_driver = "k8s-file"',
                 'cgroup_manager = "cgroupfs"', 'events_logger = "file"', 'runtime = "crun"'):
        assert line in conf.splitlines()
    for line in ('driver = "overlay"', 'rootless_storage_path = "/al-engine/storage"',
                 'mount_program = "/usr/bin/fuse-overlayfs"', 'ignore_chown_errors = "true"'):
        assert line in storage.splitlines()
    for text in (conf, storage):  # every setting is preceded by a comment saying why
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if "=" in line and not line.startswith("#"):
                above = next(l for l in reversed(lines[:index]) if not ("=" in l and not l.startswith("#")))
                assert above.startswith("#"), line


def test_flag_reasons_cover_every_added_and_kept_flag():
    reasons = nested_engine.FLAG_REASONS
    assert set(reasons) == set(nested_engine.ADDED) | set(nested_engine.KEPT)
    assert all(len(reason) > 30 for reason in reasons.values())
    flags = nested_engine.podman_flags(_option(), LAYER, STATE)
    assert f"{flags[0]} {flags[1]}" in reasons
    assert flags[3].replace(f"test-layer-{ADW_ID}", "<layer>-<run>") in " ".join(reasons)
    for flag in envs.hardening_flags(_option()):
        if flag.startswith("-"):
            assert any(flag in key for key in reasons), flag
    assert "--userns=keep-id" in reasons


def test_prepare_creates_a_labelled_volume_and_tolerates_resume(fake_podman):
    nested_engine.prepare(LAYER, STATE, _option(nestedEngine=False))
    assert fake_podman.calls == []
    nested_engine.prepare(LAYER, STATE, _option())
    (call,) = fake_podman.calls
    assert call[:2] == ["volume", "create"] and call[-1] == f"al-engine-test-layer-{ADW_ID}"
    assert {"al.layer=test-layer", f"al.layer-dir={LAYER.layer_dir.resolve()}", f"al.run={ADW_ID}"} <= set(call)
    fake_podman.answers[("volume", "create")] = (125, "", "Error: volume with name x already exists: volume already exists")
    nested_engine.prepare(LAYER, STATE, _option())   # a resumed run
    fake_podman.answers[("volume", "create")] = (125, "", "Error: no space left on device")
    with pytest.raises(StepFailed) as failed:
        nested_engine.prepare(LAYER, STATE, _option())
    assert failed.value.step == "preflight" and "no space left" in str(failed.value)


def test_remove_volume_never_raises(fake_podman, capsys):
    fake_podman.answers[("volume", "exists")] = (1, "", "")
    nested_engine.remove_volume(LAYER, STATE)
    assert [c[:2] for c in fake_podman.calls] == [["volume", "exists"]]
    fake_podman.answers[("volume", "exists")] = (0, "", "")
    fake_podman.answers[("volume", "rm")] = (2, "", "volume is being used")
    nested_engine.remove_volume(LAYER, STATE)
    assert "could not remove volume" in capsys.readouterr().out
    fake_podman.missing = True
    nested_engine.remove_volume(LAYER, STATE)


def test_collect_stale_volumes_keeps_live_runs(fake_podman, tmp_path):
    layer = SimpleNamespace(name="test-layer", layer_dir=tmp_path)
    for run, status in (("aaaa0001", "running"), ("aaaa0002", "running"), ("aaaa0003", "passed")):
        (tmp_path / "agents" / run).mkdir(parents=True)
        (tmp_path / "agents" / run / "state.json").write_text(json.dumps({"status": status}))
    (tmp_path / "agents" / ".ports.json").write_text(json.dumps({"aaaa0001": {"ports": [], "pid": os.getpid()},
                                                                  "aaaa0002": {"ports": [], "pid": 0}}))
    owner = str(tmp_path.resolve())
    volumes = [{"Name": f"al-engine-test-layer-{run}", "Labels": {"al.layer-dir": owner, "al.run": run}}
               for run in ("aaaa0001", "aaaa0002", "aaaa0003")]
    volumes.append({"Name": "al-engine-test-layer-aaaa0004", "Labels": {"al.layer-dir": "/elsewhere", "al.run": "aaaa0004"}})
    fake_podman.answers[("volume", "ls")] = (0, json.dumps(volumes), "")
    removed = nested_engine.collect_stale_volumes(layer)
    assert removed == ["al-engine-test-layer-aaaa0002", "al-engine-test-layer-aaaa0003"]
    assert ["volume", "ls", "--format", "json", "--filter", f"label=al.layer-dir={owner}"] in fake_podman.calls
    assert not any("aaaa0001" in " ".join(c) for c in fake_podman.calls if c[:2] == ["volume", "rm"])


class _Stack:
    def __init__(self, option: dict, worktree: Path, output: str = "") -> None:
        self.podman_option, self.cwd, self.output = option, worktree, output
        self.state = SimpleNamespace(worktree=str(worktree), image="", adw_id=ADW_ID)
        self.scripts: list[str] = []
        self._pod = None

    def _podman_argv(self, argv, interactive=False):
        self.scripts.append(argv[-1])
        return ["sh", "-c", f"printf '%b' {json.dumps(self.output)}"]

    def process_env(self) -> dict:
        return dict(os.environ)

    def services(self) -> list:
        return []


def test_doctor_lines_only_with_the_option_and_never_print_values(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTAINER_HOST", "secret-host")
    assert nested_engine.doctor_lines(_option(nestedEngine=False)) == []
    without = sandbox.doctor_script(_Stack(_option(nestedEngine=False), tmp_path), [], agent=None, browser=False)
    assert "engine" not in without
    script = sandbox.doctor_script(_Stack(_option(), tmp_path), [], agent=None, browser=False)
    for row in nested_engine.ROWS:
        assert f"r {row} ok" in script or f"r {row} miss" in script or f"r {row} \"" in script
    assert "secret-host" not in script
    assert "CONTAINER_HOST" in script and "DOCKER_HOST" in script and "/var/run/docker.sock" in script
    assert script.index("engine:userns") < script.index("r disk")
    # the lines are valid POSIX sh, and run where there is no engine they report it
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PATH": "/usr/bin:/bin"})
    rows = {check.name: check for check in sandbox.parse_doctor_output(result.stdout)}
    assert set(nested_engine.ROWS) <= set(rows)
    assert rows["engine:isolation"].status == "miss"
    # the finding named is the machine's own: the first host engine socket that exists, else the variable
    found = next((p for p in ["/run/podman/podman.sock", *sorted(glob.glob("/run/user/*/podman/podman.sock")),
                              "/var/run/docker.sock"] if os.path.exists(p)), None)
    expected = f"a host engine socket is reachable: {found}" if found else "CONTAINER_HOST is set"
    assert rows["engine:isolation"].detail == expected


def test_parse_engine_rows_and_fixes(make_layer, tmp_path, monkeypatch):
    from adw_modules.ir import load

    layer = load(make_layer({"environments": [{"id": "wt", "kind": "worktree", "dir": "trees"}, _option()]}))
    monkeypatch.setattr(nested_engine, "HOST_FUSE", tmp_path / "fuse")
    (tmp_path / "fuse").write_text("")
    output = ("AL-DOCTOR\\tengine\\tok\\t/usr/bin/podman\\n"
              "AL-DOCTOR\\tengine:fuse\\tmiss\\tthe run's container has no /dev/fuse\\n"
              "AL-DOCTOR\\tengine:userns\\tmiss\\tcannot clone: Operation not permitted\\n")
    checks = sandbox.doctor(layer, _Stack(_option(), tmp_path, output), None, fast=True, cmds=[], agent=None,
                            browser=False)
    by_name = {check.name: check for check in checks}
    assert "envs.py" in by_name["engine:fuse"].fix and "docs/DESIGN.md" in by_name["engine:fuse"].fix
    assert "user.max_user_namespaces" in by_name["engine:userns"].fix
    assert "apparmor_restrict_unprivileged_userns" in by_name["engine:userns"].fix
    assert by_name["engine:storage"].status == "miss" and "not reported" in by_name["engine:storage"].detail
    assert by_name["engine:host-fuse"].status == "ok"
    assert nested_engine.fixes(SimpleNamespace(name="engine"), _option()).endswith(
        "(environment podman #:id box #:nested-engine #t)")


def test_host_checks_read_proc(tmp_path, monkeypatch):
    assert nested_engine.host_checks(_option(nestedEngine=False)) == []
    monkeypatch.setattr(nested_engine, "HOST_FUSE", tmp_path / "fuse")
    monkeypatch.setattr(nested_engine, "MAX_USER_NAMESPACES", tmp_path / "max_user_namespaces")
    monkeypatch.setattr(nested_engine, "APPARMOR_USERNS", tmp_path / "apparmor")
    (tmp_path / "max_user_namespaces").write_text("0\n")
    rows = {check.name: check for check in nested_engine.host_checks(_option())}
    assert rows["engine:host-fuse"].status == "miss"
    assert rows["engine:host-userns"].status == "miss" and "user.max_user_namespaces=0" in rows["engine:host-userns"].detail
    (tmp_path / "fuse").write_text("")
    (tmp_path / "max_user_namespaces").write_text("63412\n")
    (tmp_path / "apparmor").write_text("1\n")
    rows = {check.name: check for check in nested_engine.host_checks(_option())}
    assert rows["engine:host-fuse"].status == "ok"
    host = rows["engine:host-userns"]
    assert host.status == "ok" and "apparmor_restrict_unprivileged_userns=1" in host.detail  # a detail, not a miss


def test_preflight_refuses_a_nested_run_without_fuse(make_layer, monkeypatch, fake_podman):
    environments = [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 24950,
                     "portsPerRun": 0, "keep": "never", "status": "stable"}, _option()]
    layer = make_layer({
        "environments": environments,
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "plan", "template": "plan", "modelSet": "base", "output": "path"}],
        "codebase": {"commands": [{"name": "test", "run": "pytest -q"}]},
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "auto", "environment": ["wt", "box"],
                  "classes": ["chore"], "steps": [{"name": "plan", "kind": "run", "command": "plan"},
                                                  {"name": "test", "kind": "check", "cmd": "test"}]}]})
    (layer / "env").mkdir()
    (layer / "env" / "Containerfile").write_text("FROM debian:stable-slim\n")
    monkeypatch.setattr(sandbox, "ensure_image", lambda layer, say=print: {"built": False})
    fix = nested_engine.fixes(SimpleNamespace(name="engine:fuse"), _option())
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: [
        sandbox.Check("engine:fuse", "miss", nested_engine.NO_FUSE, fix), sandbox.Check("engine", "ok", "podman")])
    assert main("demo", layer, ["Do it", "--adw-id", ADW_ID]) == 1
    found = events.read(layer / "agents" / ADW_ID / "events.jsonl")
    assert not [e for e in found if e["type"] == "step.start"]
    (pre,) = [e for e in found if e["type"] == "env.preflight"]
    assert pre["status"] == "failed" and pre["env"] == "box"
    assert {"name": "engine:fuse", "status": "miss", "detail": nested_engine.NO_FUSE} in pre["checks"]
    log = (layer / "agents" / ADW_ID / "run.log").read_text()
    assert "the sandbox is missing what this workflow needs" in log and "engine:fuse | miss" in log and "envs.py" in log
    volume = f"al-engine-test-layer-{ADW_ID}"
    calls = [" ".join(call) for call in fake_podman.calls]
    assert any(call.startswith("volume create") and call.endswith(volume) for call in calls)
    assert f"volume rm -f {volume}" in calls   # removed at the end of the run
    assert calls.index(f"volume rm -f {volume}") > max(i for i, c in enumerate(calls) if c.startswith("volume create"))
