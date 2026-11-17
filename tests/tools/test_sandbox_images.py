"""`al sandbox LAYER push|pull`: the sandbox image built once, labelled with its recipe hash, shipped
through the declared registry and checked before it is used. Against the stateful fake podman
(fake_podman.py: images and a registry as JSON files); one optional test against a real local
registry (marker `podman`), which never downloads anything.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from conftest import PROVISION_LAYER, ROOT, al, gen_provision_layer, git
from deploy_host import fake_podman, test_pins

import importlib.util
import sys

_spec = importlib.util.spec_from_file_location("al_sandbox", ROOT / "tools" / "al_sandbox.py")
al_sandbox = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("al_sandbox", al_sandbox)
_spec.loader.exec_module(al_sandbox)  # type: ignore[union-attr]

ADDRESS = "registry-sentinel.test/team"
WITH_LOGIN = PROVISION_LAYER.replace(
    '(registry #:address-env "AL_REGISTRY")',
    '(registry #:address-env "AL_REGISTRY" #:username-env "AL_REGISTRY_USER" #:password-env "AL_REGISTRY_PASSWORD")')


class Machine:
    """This machine's podman: the fake, with its state and a registry directory."""

    def __init__(self, tmp_path: Path, name: str = "local"):
        self.bin = tmp_path / f"{name}-bin"
        self.bin.mkdir()
        self.state = tmp_path / f"{name}-podman"
        self.registry = tmp_path / "registry"
        fake_podman.install(self.bin, self.state, self.registry)

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "AL_REGISTRY": ADDRESS, "XDG_CONFIG_HOME": str(self.bin.parent / "xdg"),
                **extra}

    def calls(self) -> list[list[str]]:
        return fake_podman.calls(self.state)

    def labels(self, tag: str) -> dict | None:
        image = next((i for i in fake_podman.images(self.state) if fake_podman.norm(tag) in i["tags"]), None)
        return image["labels"] if image else None


@pytest.fixture
def layer(app: Path, tmp_path: Path) -> Path:
    pins = tmp_path / "pins"
    pins.mkdir()
    return gen_provision_layer(app, test_pins(pins))


def recipe(layer: Path) -> str:
    """The recipe hash the layer's own runtime gives its sandbox image (adw_modules/sandbox.py), in a
    child process so this one keeps its imports."""
    code = "import sys; from adw_modules import ir, sandbox; print(sandbox.recipe_hash(ir.load(sys.argv[1])))"
    done = subprocess.run([sys.executable, "-c", code, str(layer)], cwd=layer / "adws", capture_output=True, text=True, check=True)
    return done.stdout.strip()


def test_recipe_hash_is_the_runtime_recipe_hash(layer):
    code = "import sys; sys.path.insert(0, sys.argv[1]); import al_sandbox; print(al_sandbox.recipe_hash(sys.argv[2]))"
    mine = lambda: subprocess.run([sys.executable, "-c", code, str(ROOT / "tools"), str(layer)],  # noqa: E731
                                  capture_output=True, text=True, check=True).stdout.strip()
    h = recipe(layer)
    assert len(h) == 12 and mine() == h
    (layer / "env/setup.sh").write_text("echo one\n")                  # a file the Containerfile COPYs
    with (layer / "env/Containerfile").open("a") as handle:
        handle.write("COPY setup.sh /opt/setup.sh\n")
    copying = mine()
    assert copying == recipe(layer) != h
    (layer / "env/setup.sh").write_text("echo two\n")                  # only the copied file changes
    assert mine() == recipe(layer) != copying
    assert al_sandbox.registry_ref({"name": "notes"}, "r.test/x", "a" * 12) == "r.test/x/notes:recipe-" + "a" * 12


def test_push_builds_with_the_recipe_label_and_pushes_to_the_declared_registry(app, tmp_path):
    pins = tmp_path / "pins"
    pins.mkdir()
    layer = gen_provision_layer(app, test_pins(pins), WITH_LOGIN)
    machine = Machine(tmp_path)
    h = recipe(layer)
    cp = al("sandbox", layer, "push", env=machine.env(AL_REGISTRY_USER="robot", AL_REGISTRY_PASSWORD="sentinel-password"))
    calls = machine.calls()
    commands = [c[0] for c in calls]
    build = calls[commands.index("build")]
    assert f"al.recipe={h}" in build and build[build.index("-t") + 1] == f"al-notes:{h}"
    ref = f"{ADDRESS}/notes:recipe-{h}"
    assert ["tag", f"al-notes:{h}", ref] in calls
    login = calls[commands.index("login")]
    assert "--password-stdin" in login and "--authfile" in login and login[-1] == "registry-sentinel.test"
    authfile = Path(login[login.index("--authfile") + 1])
    push = calls[commands.index("push")]
    assert push[-1] == ref and push[push.index("--authfile") + 1] == str(authfile)
    assert commands.index("build") < commands.index("tag") < commands.index("login") < commands.index("push")
    assert not authfile.exists() and not authfile.parent.exists()
    assert all("sentinel-password" not in " ".join(c) for c in calls)
    assert "sentinel-password" not in cp.stdout + cp.stderr
    assert f"pushed al-notes:{h} to the registry as notes:recipe-{h}" in cp.stdout
    assert json.loads(next(machine.registry.iterdir()).read_text())["labels"]["al.recipe"] == h


def test_push_reuses_a_labelled_image(layer, tmp_path):
    machine = Machine(tmp_path)
    al("sandbox", layer, "push", env=machine.env())
    al("sandbox", layer, "push", env=machine.env())
    assert [c[0] for c in machine.calls()].count("build") == 1
    assert [c[0] for c in machine.calls()].count("push") == 2


def test_pull_verifies_the_label_and_tags_latest(layer, tmp_path):
    builder, host = Machine(tmp_path, "builder"), Machine(tmp_path, "host")
    al("sandbox", layer, "push", env=builder.env())
    assert host.labels("al-notes:latest") is None
    cp = al("sandbox", layer, "pull", env=host.env())
    h = recipe(layer)
    assert host.labels("al-notes:latest")["al.recipe"] == h and host.labels(f"al-notes:{h}")["al.recipe"] == h
    assert [c[0] for c in host.calls()].count("build") == 0
    assert f"recipe {h} checked, tagged al-notes:{h} and al-notes:latest" in cp.stdout


def test_pull_refuses_an_image_whose_recipe_differs(layer, tmp_path):
    host = Machine(tmp_path, "host")
    h = recipe(layer)
    host.registry.mkdir()
    other = "f" * 12
    (host.registry / (fake_podman.norm(f"{ADDRESS}/notes:recipe-{h}").replace("/", "__") + ".json")).write_text(
        json.dumps({"id": "abc", "labels": {"al.recipe": other}}))
    cp = al("sandbox", layer, "pull", env=host.env(), ok=False)
    assert cp.returncode != 0
    assert other in cp.stderr and h in cp.stderr
    assert host.labels("al-notes:latest") is None and host.labels(f"al-notes:{h}") is None


def test_registry_value_never_printed(layer, tmp_path):
    builder, host = Machine(tmp_path, "builder"), Machine(tmp_path, "host")
    for machine, action in ((builder, "push"), (host, "pull"), (builder, "push")):
        for extra in ([], ["--dry-run"]):
            cp = al("sandbox", layer, action, *extra, env=machine.env())
            assert ADDRESS not in cp.stdout + cp.stderr and "registry-sentinel" not in cp.stdout + cp.stderr
    missing = al("sandbox", layer, "push", env={**builder.env(), "AL_REGISTRY": ""}, ok=False)
    assert missing.returncode == 1 and "AL_REGISTRY is not set" in missing.stderr


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.mark.podman
def test_real_registry_round_trip(layer, tmp_path):
    if shutil.which("podman") is None:
        pytest.skip("podman is not installed")
    if not _listening(5000):
        pytest.skip("no registry listening on 127.0.0.1:5000 (start one yourself; this test never downloads)")
    base = subprocess.run(["podman", "image", "exists", "docker.io/library/debian:stable-slim"], capture_output=True)
    if base.returncode != 0:
        pytest.skip("the base image is not present locally, and this test never downloads it")
    (layer / "local.env").write_text("AL_REGISTRY=127.0.0.1:5000/al-test\n")
    text = (layer / "agentic.rkt").read_text().replace('(registry #:address-env "AL_REGISTRY")',
                                                       '(registry #:address-env "AL_REGISTRY" #:tls-verify #f)')
    (layer / "agentic.rkt").write_text(text)
    al("gen", layer)
    env = {"AL_REGISTRY": ""}
    al("sandbox", layer, "push", env=env)
    h = recipe(layer)
    subprocess.run(["podman", "untag", "al-notes:latest"], capture_output=True)
    subprocess.run(["podman", "untag", f"al-notes:{h}"], capture_output=True)
    cp = al("sandbox", layer, "pull", env=env)
    assert f"checked, tagged al-notes:{h} and al-notes:latest" in cp.stdout
    label = subprocess.run(["podman", "image", "inspect", "--format", '{{index .Labels "al.recipe"}}', "al-notes:latest"],
                           capture_output=True, text=True).stdout.strip()
    assert label == h
    assert git(layer.parent, "status", "--porcelain", "--", "agentic/local.env") == ""     # git-ignored
