"""Real builds of a toolchain's sandbox image, both ways, and the tools in it under the runtime's
hardening flags. Slow (a first build downloads several hundred MB) and needs podman and network;
the host way also needs nix. With AL_TOOLCHAIN_SHOTS=DIR the headless chromium screenshot is copied
there as toolchain-browser.png. See tests/README.md.

The image is the layer's recipe image, al-<name>:<recipe hash>; al-<name>:latest points at it, and
that is the name these tests run.

The builder's store volume (al-nix-store-…) is kept between sessions as a cache; the images this
module makes are removed."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import alit

pytestmark = [pytest.mark.slow, pytest.mark.podman, pytest.mark.network]

sys.path.insert(0, str(alit.ROOT / "kit" / "runtime"))
from adw_modules import envs  # noqa: E402

LAYER = """#lang al
(agentic-layer #:name "{name}"
  (codebase #:root "{root}" #:languages (shell) (cmd test "true"))
  (toolchain nix #:id dev #:packages ("hello" "jq" "git") #:features (browser agent-cli))
  (agent claude-code)
  (environment worktree)
  (environment podman #:id sandbox #:toolchain dev #:agent-inside #t #:read-only-root #t)
  (command plan #:template plan-chore #:output path)
  (adw plan #:environment (worktree sandbox) (steps (run plan #:command plan #:args ("$prompt")))))
"""
PAGE = "<html><body style='background:#2a6;font:48px sans-serif'><h1>al toolchain</h1></body></html>"


def make_layer(tmp_path: Path) -> tuple[Path, str]:
    app = tmp_path / "app"
    app.mkdir()
    alit.sh(["git", "init", "-q", "-b", "main", app], check=True)
    (app / "README.md").write_text("app\n")
    alit.sh(["git", "-C", app, "add", "-A"], check=True)
    alit.sh(["git", "-C", app, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "app"], check=True)
    name = f"tc-{secrets.token_hex(3)}"
    layer = tmp_path / "layer"
    layer.mkdir()
    (layer / "agentic.rkt").write_text(LAYER.format(name=name, root=app))
    return layer, name


def al(*args, env: dict[str, str] | None = None, timeout: float = 7200) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    full.update(env or {})
    return alit.sh([alit.AL, *args], env=full, timeout=timeout)


def remove_images(name: str) -> None:
    for image in (f"al-{name}:latest", f"localhost/al-{name}-toolchain-dev:latest"):
        alit.sh(["podman", "rmi", "-f", image], timeout=300)


def build(tmp_path: Path, way: str) -> tuple[Path, str, subprocess.CompletedProcess[str], float]:
    layer, name = make_layer(tmp_path)
    env = {"AL_SANDBOX_BUILD": way}
    gen = al("gen", layer, env=env)
    assert gen.returncode == 0, alit.describe(gen)
    assert "lock: env/flake.lock written" in gen.stdout, gen.stdout
    started = time.monotonic()
    cp = al("sandbox", layer, "build", env=env)
    return layer, name, cp, time.monotonic() - started


@pytest.fixture(scope="module")
def container_built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("toolchain")
    layer, name, cp, seconds = build(tmp, "container")
    yield layer, name, cp, seconds
    remove_images(name)


def run_in_image(name: str, *argv: str, mounts: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    flags = envs.hardening_flags({"readOnlyRoot": True})
    volumes = [x for m in mounts for x in ("-v", m)]
    return alit.sh(["podman", "run", "--rm", "--userns=keep-id", *flags, *volumes, "-e", "HOME=/tmp/home",
                    f"al-{name}:latest", *argv], timeout=600)


def test_builds_without_host_nix(container_built, capsys) -> None:
    layer, name, cp, _seconds = container_built
    assert cp.returncode == 0, alit.describe(cp)
    assert re.search(rf"^sandbox: al-{name}:[0-9a-f]{{12}} from toolchain `dev` \(no Nix on this host: built in the "
                     "builder container", cp.stdout, re.M), cp.stdout
    assert "  browser " in cp.stdout and "  agent-cli " in cp.stdout and "  image " in cp.stdout
    report = (layer / "agents/sandbox/dev.json").read_text()
    with capsys.disabled():
        print("\n===== al sandbox LAYER build (container way) =====")
        print(cp.stdout, end="" if cp.stdout.endswith("\n") else "\n")
        print("$ cat agents/sandbox/dev.json")
        print(report, end="" if report.endswith("\n") else "\n")
    target = os.environ.get("AL_TOOLCHAIN_SHOTS")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / "al-sandbox-build.txt").write_text(cp.stdout)
        (Path(target) / "dev.json").write_text(report)


@pytest.mark.nix
def test_builds_with_host_nix(tmp_path) -> None:
    layer, name, cp, _seconds = build(tmp_path, "nix")
    try:
        assert cp.returncode == 0, alit.describe(cp)
        assert re.search(rf"^sandbox: al-{name}:[0-9a-f]{{12}} from toolchain `dev` \(built with Nix on this host\)$",
                         cp.stdout, re.M), cp.stdout
        assert run_in_image(name, "hello").stdout.strip() == "Hello, world!"
    finally:
        remove_images(name)


def test_each_declared_package_runs_under_hardening_flags(container_built) -> None:
    _layer, name, cp, _seconds = container_built
    assert cp.returncode == 0, alit.describe(cp)
    script = ("set -e; hello; jq --version; git --version; whoami; test -w /tmp/home; /usr/bin/env sh -c true; "
              "echo all-ok")
    result = run_in_image(name, "bash", "-c", script)
    assert result.returncode == 0, alit.describe(result)
    assert "Hello, world!" in result.stdout and "jq-" in result.stdout and "git version" in result.stdout
    assert result.stdout.rstrip().endswith("all-ok")


def test_chromium_headless_screenshot(container_built, tmp_path, capsys) -> None:
    _layer, name, cp, _seconds = container_built
    assert cp.returncode == 0, alit.describe(cp)
    out = tmp_path / "out"
    out.mkdir()
    script = (f"printf '%s' \"{PAGE}\" > /tmp/p.html && chromium --headless --screenshot=/out/toolchain-browser.png "
              "--window-size=800,600 file:///tmp/p.html")
    result = run_in_image(name, "bash", "-c", script, mounts=(f"{out}:/out",))
    shot = out / "toolchain-browser.png"
    assert shot.is_file(), alit.describe(result)
    assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and shot.stat().st_size > 1024
    with capsys.disabled():
        print(f"\ntoolchain-browser.png: {shot.stat().st_size} bytes, sha256 {hashlib.sha256(shot.read_bytes()).hexdigest()}")
    target = os.environ.get("AL_TOOLCHAIN_SHOTS")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        shutil.copy(shot, Path(target) / "toolchain-browser.png")


def test_agent_cli_answers_version(container_built) -> None:
    _layer, name, cp, _seconds = container_built
    assert cp.returncode == 0, alit.describe(cp)
    result = run_in_image(name, "claude", "--version")
    assert result.returncode == 0, alit.describe(result)


def test_second_build_is_up_to_date_and_fast(container_built) -> None:
    layer, name, cp, _seconds = container_built
    assert cp.returncode == 0, alit.describe(cp)
    started = time.monotonic()
    again = al("sandbox", layer, "build", env={"AL_SANDBOX_BUILD": "container"})
    assert again.returncode == 0, alit.describe(again)
    assert re.fullmatch(rf"sandbox: al-{name}:[0-9a-f]{{12}} is up to date \(toolchain dev, lock and declaration unchanged\)",
                        again.stdout.strip()), again.stdout
    assert time.monotonic() - started < 60
