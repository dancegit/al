"""The prepare step runs inside the run's container, with the podman option's caches mounted: a
cache volume written by one run's prepare is there for the next run. Needs rootless podman and
docker.io/library/python:3.12-slim already present (never pulled)."""
from __future__ import annotations

import secrets
import shutil
import subprocess

import pytest

from adw_modules.runner import main

pytestmark = pytest.mark.podman

IMAGE = "docker.io/library/python:3.12-slim"


def _podman(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=300, check=False)


@pytest.fixture
def local_image():
    if shutil.which("podman") is None:
        pytest.skip("podman is not on PATH")
    if _podman("image", "exists", IMAGE).returncode != 0:
        pytest.skip(f"{IMAGE} is not present locally (this test never pulls)")


def test_prepare_runs_in_the_container_with_its_cache(make_layer, local_image):
    tag = secrets.token_hex(3)
    cache, name = f"prep{tag}", f"prep-{tag}"
    variable = f"AL_CACHE_PREP{tag.upper()}"
    volume = f"al-cache-{cache}"
    created = _podman("volume", "exists", volume).returncode != 0
    line = f'test "${variable}" = /caches/{cache} && echo run >> /caches/{cache}/al-prepare-test'
    layer = make_layer({
        "name": name,
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "codebase": {"commands": [{"name": "install", "run": line, "timeout": 120, "description": ""},
                                  {"name": "test", "run": "true", "timeout": 60, "description": ""}]},
        "environments": [
            {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 28100, "portsPerRun": 0,
             "keep": "never", "status": "stable", "prepare": ["install"]},
            {"id": "box", "kind": "podman", "status": "stable", "baseImage": IMAGE, "packages": [], "setup": [],
             "network": "none", "memory": "1g", "cpus": "1", "agentInside": False, "scope": "run", "caches": [cache],
             "credentials": [], "publish": [], "services": [], "pidsLimit": 512, "readOnlyRoot": False,
             "containerfile": "", "target": "", "context": ".", "devcontainer": "", "browser": False}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "adws": [{"name": "check", "agent": "mock", "review": "human", "environment": ["wt", "box"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [],
                  "steps": [{"name": "test", "kind": "check", "cmd": "test", "resolve": "", "retries": 0, "collect": []}]}],
    })
    (layer / "env").mkdir()
    (layer / "env/Containerfile").write_text(f"FROM {IMAGE}\nENV HOME=/tmp/home\nRUN mkdir -p /tmp/home && chmod 1777 "
                                             "/tmp/home\nWORKDIR /work\n")
    try:
        assert main("check", layer, ["one", "--adw-id", "a1a1a1a1"]) == 0
        assert main("check", layer, ["two", "--adw-id", "b2b2b2b2"]) == 0
        seen = _podman("run", "--rm", "-v", f"{volume}:/c", IMAGE, "cat", "/c/al-prepare-test")
        assert seen.stdout.splitlines() == ["run", "run"]
    finally:
        _podman("rmi", "-f", f"al-{name}:latest")
        if created:
            _podman("volume", "rm", "-f", volume)
