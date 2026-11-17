"""adw_modules/images.py: the sandbox image of a toolchain. Every command goes through an injected
runner, so nothing here starts a process, needs nix or podman, or touches the network."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from adw_modules import images

OPTION = {"id": "sandbox", "kind": "podman", "toolchain": "dev", "setup": ["true"]}
IMAGE = "al-notes:latest"
STORE_PATH = "/nix/store/abc-stream-al-notes-toolchain-dev"


def env_dir(tmp_path: Path) -> Path:
    env = tmp_path / "layer" / "env"
    env.mkdir(parents=True)
    for name, text in (("flake.nix", "{ }\n"), ("flake.lock", json.dumps(lock("aaaa", 1700000000)) + "\n"),
                       ("toolchain-dev.nix", "{ pkgs }: { }\n"), ("features.nix", "{ }: { }\n"),
                       ("agent-cli.json", "{}\n"), ("Containerfile", "FROM x\n")):
        (env / name).write_text(text)
    return env


def lock(rev: str, modified: int, name: str = "nixpkgs_dev") -> dict:
    return {"nodes": {"root": {"inputs": {name: name}},
                      name: {"locked": {"rev": rev, "lastModified": modified, "type": "github"}}},
            "root": "root", "version": 7}


class Runner:
    """A fake `run`: records every argv, answers by the first matching rule."""

    def __init__(self, rules: list[tuple[str, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.rules = rules or []

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        joined = " ".join(argv)
        for needle, out in self.rules:
            if needle in joined:
                if out.startswith("FAIL:"):
                    return subprocess.CompletedProcess(argv, 1, "", out[5:])
                return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


class Popen:
    """A fake Popen for the stream into `podman load`: records argv, never starts anything."""
    calls: list[list[str]] = []

    def __init__(self, argv, stdin=None, stdout=None, stderr=None, text=None):
        Popen.calls.append([str(a) for a in argv])
        self.argv = [str(a) for a in argv]
        self.stdout = self.stderr = None
        self.returncode = 0

    def communicate(self, timeout=None):
        loaded = "Loaded image: localhost/al-notes-toolchain-dev:latest\n" if self.argv[:2] == ["podman", "load"] else ""
        return loaded, ""

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_popen():
    Popen.calls = []


# --- the key --------------------------------------------------------------------------------------

def test_key_is_stable_for_unchanged_inputs(tmp_path) -> None:
    env = env_dir(tmp_path)
    assert images.key(env, "dev", ["true"]) == images.key(env, "dev", ["true"])
    assert len(images.key(env, "dev", ["true"])) == 64


def test_key_changes_with_lock_declaration_setup_and_builder(tmp_path) -> None:
    env = env_dir(tmp_path)
    before = images.key(env, "dev", ["true"])
    seen = {before}
    for name in ("flake.lock", "toolchain-dev.nix", "flake.nix", "features.nix", "agent-cli.json"):
        original = (env / name).read_text()
        (env / name).write_text(original + "# changed\n")
        changed = images.key(env, "dev", ["true"])
        assert changed not in seen, name
        seen.add(changed)
        (env / name).write_text(original)
    assert images.key(env, "dev", ["true"]) == before
    assert images.key(env, "dev", ["true", "echo more"]) != before
    other = "docker.io/nixos/nix@sha256:" + "0" * 64
    assert images.key(env, "dev", ["true"], builder=other) != before


# --- the way ----------------------------------------------------------------------------------------

def test_choose_way_prefers_host_nix_then_container(monkeypatch) -> None:
    monkeypatch.delenv("AL_SANDBOX_BUILD", raising=False)
    found = {"nix": "/usr/bin/nix", "podman": "/usr/bin/podman"}
    monkeypatch.setattr(images.shutil, "which", lambda name: found.get(name))
    assert images.choose_way() == "nix"
    del found["nix"]
    assert images.choose_way() == "container"
    del found["podman"]
    with pytest.raises(images.BuildError, match="neither nix nor podman"):
        images.choose_way()
    found.update(nix="/usr/bin/nix", podman="/usr/bin/podman")
    monkeypatch.setenv("AL_SANDBOX_BUILD", "container")
    assert images.choose_way() == "container"
    monkeypatch.setenv("AL_SANDBOX_BUILD", "nix")
    assert images.choose_way() == "nix"
    monkeypatch.setenv("AL_SANDBOX_BUILD", "docker")
    with pytest.raises(images.BuildError, match="AL_SANDBOX_BUILD"):
        images.choose_way()


def test_container_way_argv(tmp_path) -> None:
    env = env_dir(tmp_path)
    argv = images.nix_argv("container", env, ["build", f"{images.flake_ref('container', env)}#image-dev"])
    digest = images.NIX_BUILDER.split("@sha256:")[1]
    assert images.NIX_BUILDER.startswith("docker.io/nixos/nix@sha256:") and len(digest) == 64
    assert argv[:3] == ["podman", "run", "--rm"] and "--security-opt=no-new-privileges" in argv
    assert f"{images.store_volume()}:/nix" in argv and images.store_volume() == f"al-nix-store-{digest[:12]}"
    assert f"{env}:/src:ro" in argv
    assert images.NIX_BUILDER in argv
    rest = argv[argv.index(images.NIX_BUILDER) + 1:]
    assert rest[0] == "nix" and "nix-command flakes" in rest
    assert rest[rest.index("sandbox") - 1:rest.index("sandbox") + 2] == ["--option", "sandbox", "false"]
    assert rest[rest.index("build-users-group") - 1:rest.index("build-users-group") + 2] == ["--option", "build-users-group", ""]
    assert "path:/src#image-dev" in rest
    # a new builder gets a new store
    assert images.store_volume("docker.io/nixos/nix@sha256:" + "f" * 64) == "al-nix-store-ffffffffffff"


def test_host_way_argv(tmp_path) -> None:
    env = env_dir(tmp_path)
    argv = images.nix_argv("nix", env, ["build", f"{images.flake_ref('nix', env)}#image-dev"])
    assert argv[0] == "nix" and "nix-command flakes" in argv and f"path:{env}#image-dev" in argv
    assert "podman" not in argv


def test_lock_mounts_env_read_write(tmp_path) -> None:
    env = env_dir(tmp_path)
    (env / "flake.lock").unlink()
    run = Runner()
    assert images.lock(env, way="container", run=run) == "written"
    (call,) = run.calls
    assert f"{env}:/src" in call and f"{env}:/src:ro" not in call
    assert call[-3:] == ["flake", "lock", "path:/src"]


def test_lock_keeps_an_existing_lock(tmp_path) -> None:
    env = env_dir(tmp_path)
    run = Runner()
    assert images.lock(env, way="container", run=run) == "kept"
    assert run.calls == []


# --- ensure -------------------------------------------------------------------------------------------

def test_ensure_skips_when_label_matches(tmp_path) -> None:
    env = env_dir(tmp_path)
    current = images.key(env, "dev", OPTION["setup"])
    run = Runner([("image inspect", current + "\n")])
    report = images.ensure(env.parent, "notes", OPTION, IMAGE, say=lambda _l: None, run=run, popen=Popen,
                           way="container")
    assert report["status"] == "up-to-date" and report["key"] == current
    assert not any(" load" in c or " build" in c or c.startswith("nix") for c in run.joined()), run.joined()
    assert Popen.calls == []


def test_ensure_builds_loads_tags_and_labels(tmp_path) -> None:
    env = env_dir(tmp_path)
    run = Runner([("image inspect --format {{ index .Config.Labels", "<no value>\n"),
                  ("--print-out-paths", STORE_PATH + "\n"),
                  ("image inspect --format {{.Size}}", "123456789\n")])
    report = images.ensure(env.parent, "notes", OPTION, IMAGE, say=lambda _l: None, run=run, popen=Popen,
                           way="container")
    calls = run.joined()
    build = next(i for i, c in enumerate(calls) if "#image-dev" in c)
    tag = next(i for i, c in enumerate(calls) if c.startswith("podman tag"))
    label = next(i for i, c in enumerate(calls) if c.startswith("podman build"))
    assert build < tag < label
    assert [c[:2] for c in Popen.calls] == [["podman", "run"], ["podman", "load"]]   # stream → load, before the tag
    assert STORE_PATH in Popen.calls[0]
    assert calls[tag] == "podman tag localhost/al-notes-toolchain-dev:latest localhost/al-notes-toolchain-dev:latest"
    key = images.key(env, "dev", OPTION["setup"])
    assert calls[label] == f"podman build --label al.toolchain.key={key} -t {IMAGE} -f env/Containerfile env/"
    assert report["status"] == "built" and report["way"] == "container" and report["imageSize"] == 123456789
    assert report["storeVolume"] == images.store_volume()


def test_report_written_under_agents_sandbox(tmp_path) -> None:
    env = env_dir(tmp_path)
    run = Runner([("--print-out-paths", STORE_PATH + "\n"), ("{{.Size}}", "42\n")])
    images.ensure(env.parent, "notes", OPTION, IMAGE, say=lambda _l: None, run=run, popen=Popen, way="nix")
    written = json.loads((env.parent / "agents" / "sandbox" / "dev.json").read_text())
    assert written["toolchain"] == "dev" and written["image"] == IMAGE and written["way"] == "nix"
    assert written["key"] == images.key(env, "dev", OPTION["setup"])


def test_build_error_carries_tail_of_stderr(tmp_path) -> None:
    env = env_dir(tmp_path)
    stderr = "\n".join(f"line {i}" for i in range(100))
    run = Runner([("--print-out-paths", "FAIL:" + stderr)])
    with pytest.raises(images.BuildError) as caught:
        images.ensure(env.parent, "notes", OPTION, IMAGE, say=lambda _l: None, run=run, popen=Popen, way="nix")
    message = str(caught.value)
    assert "line 99" in message and "line 60" in message and "line 59\n" not in message
    assert "image-dev" in message


# --- lock diff and sizes ---------------------------------------------------------------------------------

def test_lock_diff_reports_rev_and_date_per_input() -> None:
    old = lock("a" * 40, 1700000000)
    new = lock("b" * 40, 1710000000)
    rows = images.lock_diff(old, new)
    assert rows == [{"input": "nixpkgs_dev", "old": "aaaaaaa", "new": "bbbbbbb",
                     "oldDate": "2023-11-14", "newDate": "2024-03-09", "changed": True}]
    assert images.describe_lock_diff(rows) == ["nixpkgs_dev: aaaaaaa (2023-11-14) → bbbbbbb (2024-03-09)"]


def test_lock_diff_no_change() -> None:
    rows = images.lock_diff(lock("a" * 40, 1700000000), lock("a" * 40, 1700000000))
    assert [r["changed"] for r in rows] == [False]
    assert images.describe_lock_diff(rows) == ["nixpkgs_dev: aaaaaaa (2023-11-14), unchanged"]
    assert images.lock_diff(None, lock("c" * 40, 1700000000))[0]["old"] is None


def test_feature_sizes_parse_path_info_json() -> None:
    # nix ≥ 2.19 prints an object keyed by path; older versions a list of objects with "path"
    as_object = json.dumps({"/nix/store/a-base": {"closureSize": 1000}, "/nix/store/b-browser": {"closureSize": 5000}})
    as_list = json.dumps([{"path": "/nix/store/a-base", "closureSize": 1000},
                          {"path": "/nix/store/b-browser", "closureSize": 5000}])
    for text in (as_object, as_list):
        assert images.parse_path_info(text) == {"/nix/store/a-base": 1000, "/nix/store/b-browser": 5000}
    run = Runner([("--print-out-paths", "/nix/store/a-base\n/nix/store/b-browser\n"), ("path-info", as_object)])
    sizes = images.feature_sizes(Path("/env"), "dev", ["browser"], way="nix", run=run)
    assert sizes == {"base": 1000, "browser": 5000}
    assert any("#toolchain-dev-base" in c and "#toolchain-dev-browser" in c for c in run.joined())


def test_human_size() -> None:
    assert images.human_size(512) == "512 B"
    assert images.human_size(1536) == "1.5 KiB"
    assert images.human_size(3 * 1024 ** 3) == "3.0 GiB"


def test_ensure_passes_extra_labels_after_the_key(tmp_path) -> None:
    env = env_dir(tmp_path)
    run = Runner([("image inspect --format {{ index .Config.Labels", "<no value>\n"),
                  ("--print-out-paths", STORE_PATH + "\n"),
                  ("image inspect --format {{.Size}}", "1\n")])
    ref = "al-notes:0123456789ab"
    images.ensure(env.parent, "notes", OPTION, ref, say=lambda _l: None, run=run, popen=Popen, way="nix",
                  labels=["al.layer=notes", "al.recipe=0123456789ab"])
    key = images.key(env, "dev", OPTION["setup"])
    assert next(c for c in run.joined() if c.startswith("podman build")) == (
        f"podman build --label al.toolchain.key={key} --label al.layer=notes --label al.recipe=0123456789ab "
        f"-t {ref} -f env/Containerfile env/")
