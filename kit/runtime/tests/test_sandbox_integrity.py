"""gitsafe refuses to run host git against a tampered sandboxed worktree (and git never starts)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import envs, gitsafe
from adw_modules.errors import StepFailed
from adw_modules.ir import load
from adw_modules.state import ADWState

ADW_ID = "5a5a0002"


@pytest.fixture
def sandbox(make_layer, monkeypatch):
    layer = load(make_layer({"environments": [
        {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 24700, "portsPerRun": 0,
         "keep": "always", "status": "stable"},
        {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "2", "memory": "4g",
         "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
         "credentials": []}]}))
    real_run = envs.subprocess.run
    monkeypatch.setattr(envs.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
                        if argv[0] == "podman" else real_run(argv, **kw))
    state = ADWState(ADW_ID, "test", prompt="tamper", klass="chore")
    stack = envs.build(layer, state, ["wt", "box"])
    stack.setup()
    sb = stack.sandbox
    gitsafe.run(sb, ["status", "--porcelain"], "test")            # untampered: fine
    return sb


@pytest.fixture
def git_calls(monkeypatch):
    calls: list[list[str]] = []
    real_run = gitsafe.subprocess.run

    def spy(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)
    monkeypatch.setattr(gitsafe.subprocess, "run", spy)
    return calls


def _tamper_git_file(sb):
    (sb.worktree / ".git").write_text(f"gitdir: {sb.quarantine}\n")


def _git_file_symlink(sb):
    (sb.worktree / ".git").unlink()
    (sb.worktree / ".git").symlink_to(sb.admin)


def _tamper_commondir(sb):
    (sb.admin / "commondir").write_text("/tmp/evil\n")


def _tamper_gitdir(sb):
    (sb.admin / "gitdir").write_text("/tmp/elsewhere/.git\n")


def _add_config_worktree(sb):
    (sb.admin / "config.worktree").write_text("[core]\n\tfsmonitor = touch /tmp/pwned\n")


def _add_hooks(sb):
    (sb.admin / "hooks").mkdir()
    (sb.admin / "hooks" / "post-commit").write_text("#!/bin/sh\ntouch /tmp/pwned\n")


def _symlink_in_admin(sb):
    (sb.admin / "logs" / "HEAD").unlink(missing_ok=True)
    (sb.admin / "evil").symlink_to(Path.home())


def _nested_symlink_in_admin(sb):
    (sb.admin / "logs").mkdir(exist_ok=True)
    (sb.admin / "logs" / "HEAD").unlink(missing_ok=True)
    (sb.admin / "logs" / "HEAD").symlink_to(Path.home() / ".bashrc")


def _symbolic_head(sb):
    (sb.admin / "HEAD").write_text("ref: refs/heads/main\n")


def _symlink_in_quarantine(sb):
    (sb.quarantine / "ab").mkdir(exist_ok=True)
    (sb.quarantine / "ab" / ("c" * 38)).symlink_to("/etc/passwd")


def _quarantine_dir_symlink(sb):
    (sb.quarantine / "pack").symlink_to(sb.admin.parent.parent / "objects" / "pack")


def _quarantine_alternates(sb):
    (sb.quarantine / "info").mkdir(exist_ok=True)
    (sb.quarantine / "info" / "alternates").write_text("/home\n")


def _quarantine_stray_entry(sb):
    (sb.quarantine / "hooks").mkdir()


def _quarantine_fifo(sb):
    (sb.quarantine / "info").mkdir(exist_ok=True)
    os.mkfifo(sb.quarantine / "info" / "packs")


TAMPERINGS = [_tamper_git_file, _git_file_symlink, _tamper_commondir, _tamper_gitdir, _add_config_worktree,
              _add_hooks, _symlink_in_admin, _nested_symlink_in_admin, _symbolic_head, _symlink_in_quarantine,
              _quarantine_dir_symlink, _quarantine_alternates, _quarantine_stray_entry, _quarantine_fifo]


@pytest.mark.parametrize("tamper", TAMPERINGS, ids=[t.__name__.lstrip("_") for t in TAMPERINGS])
def test_tampering_is_refused_before_git_runs(sandbox, git_calls, tamper) -> None:
    tamper(sandbox)
    with pytest.raises(StepFailed) as raised:
        gitsafe.run(sandbox, ["status"], "test")
    assert raised.value.step == "git-integrity"
    assert git_calls == []                                          # git was never started
    with pytest.raises(StepFailed, match="git-integrity|integrity"):
        gitsafe.import_objects(sandbox, "HEAD", "test")
    assert git_calls == []


def test_helper_pins_git_dir_and_hardening(sandbox, git_calls) -> None:
    gitsafe.run(sandbox, ["status"], "test")
    argv = git_calls[-1]
    for flag in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "protocol.file.allow=user"):
        assert flag in argv
    env = gitsafe.worktree_env(sandbox)
    assert env["GIT_DIR"] == str(sandbox.admin) and env["GIT_WORK_TREE"] == str(sandbox.worktree)
    assert env["GIT_OBJECT_DIRECTORY"] == str(sandbox.quarantine)
    assert env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == str(sandbox.app_git / "objects")
    assert "GIT_OBJECT_DIRECTORY" not in gitsafe.app_env(sandbox)


def test_admin_dir_hooks_never_run_even_if_check_were_skipped(sandbox, tmp_path) -> None:
    """Belt and braces: the hardening flags alone neutralise an app-level hook (not just the integrity check)."""
    marker = tmp_path / "hook-ran"
    hook = sandbox.app_git / "hooks" / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (sandbox.worktree / "x.txt").write_text("x")
    gitsafe.run(sandbox, ["add", "-A"], "test")
    gitsafe.run(sandbox, ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], "test")
    assert not marker.exists()
    assert subprocess.run(["git", "-C", str(sandbox.app_git.parent), "status", "--porcelain"],
                          capture_output=True, text=True).stdout == ""
