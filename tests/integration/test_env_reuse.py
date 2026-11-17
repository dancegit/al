"""Reused environments, built and run for real through `al verify`: a flake dev shell wrapped by the
layer runs a browser page test, a repository's Containerfile is the base of al's thin image, and a
dev container's image, containerEnv and postCreateCommand are honoured. The applications are the
fixtures of tests/tools/env_fixtures.py; each test has its own sibling layer in tmp_path and leaves
the fixture repository exactly as it was. Each test skips without its tool and never downloads an
image or a browser."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

import alit

sys.path.insert(0, str(alit.ROOT / "tests" / "tools"))
import env_fixtures  # noqa: E402

pytestmark = pytest.mark.slow


def al(*args, ok: bool = True, timeout: float = 1800):
    cp = alit.sh([alit.AL, *map(str, args)], timeout=timeout)
    if ok:
        assert cp.returncode == 0, f"al {' '.join(map(str, args))} exited {cp.returncode}\n{cp.stdout}\n{cp.stderr}"
    return cp


@pytest.fixture
def reuse(tmp_path: Path):
    """(build a fixture app, write its sibling layer); teardown removes what the runs made in podman
    and checks the application is unchanged."""
    made: list[tuple[Path, str]] = []
    before = alit.podman_snapshot()

    def app(name: str, files: dict[str, str] | None = None) -> Path:
        repo = env_fixtures.build(tmp_path, name, files)
        made.append((repo, alit.git(repo, "rev-parse", "HEAD")))
        return repo

    def layer(repo: Path, name: str, body: str) -> Path:
        directory = tmp_path / f"{name}.al"
        directory.mkdir()
        (directory / "agentic.rkt").write_text(f'#lang al\n(agentic-layer #:name "{name}"\n{body})\n')
        return directory

    yield app, layer
    names = [p.name.removesuffix(".al") for p in tmp_path.glob("*.al")]
    alit.podman_cleanup(before, names)
    for repo, head in made:
        assert alit.git(repo, "rev-parse", "HEAD") == head
        assert alit.git(repo, "status", "--porcelain", "--untracked-files=all") == ""
        assert not list(repo.glob("RAN-*"))


@pytest.mark.nix
@pytest.mark.network
@pytest.mark.browser
def test_flake_proposal_runs_the_page_test(reuse):
    app, layer = reuse
    meta = alit.sh(["nix", "flake", "metadata", "nixpkgs", "--json"], timeout=300)
    if meta.returncode != 0:
        pytest.skip(f"nix flake metadata nixpkgs failed: {meta.stderr.strip()[:200]}")
    nixpkgs = json.loads(meta.stdout).get("url") or json.loads(meta.stdout)["resolvedUrl"]
    out = alit.sh(["nix", "eval", "--raw", f"{nixpkgs}#chromium.outPath"], timeout=600)
    if out.returncode != 0 or not Path(out.stdout.strip()).exists():
        pytest.skip("chromium is not in the nix store; this test never downloads a browser")
    repo = app("lumen", {"flake.nix": env_fixtures.LUMEN_FLAKE.replace("@NIXPKGS@", nixpkgs)})
    proposal = json.loads(al("scout", repo, "--json").stdout)["environment"]["proposal"]
    top = re.sub(r" #:prepare \([^)]*\)", "", proposal["layer"])  # the page test installs nothing
    top = top.replace("#:browser #t)", f'#:browser #t #:nixpkgs "{nixpkgs}")')
    assert '#:flake "." #:shell "default" #:browser #t' in top
    directory = layer(repo, "lumen", f"""  (codebase #:root "../lumen" #:languages (javascript)
    (cmd e2e "sh e2e.sh" #:timeout 600))
{top}
  (adw page #:environment ({' '.join(proposal['ids'])}) (steps (check e2e #:cmd e2e))))""")
    al("gen", directory)
    al("verify", directory, "--cmd", "e2e")


@pytest.mark.podman
def test_containerfile_reuse_builds_and_runs(reuse):
    if alit.sh(["podman", "image", "exists", alit.PYTHON_IMAGE]).returncode != 0:
        pytest.skip(f"{alit.PYTHON_IMAGE} is not in local storage (`podman pull` it once)")
    app, layer = reuse
    repo = app("tarn")
    directory = layer(repo, "tarn-reuse", """  (codebase #:root "../tarn" #:languages (python)
    (cmd marker "cat /opt/tarn-marker"))
  (environment worktree)
  (environment podman #:id sandbox #:containerfile "Containerfile" #:target "dev" #:agent-inside #f #:network none)
  (adw check #:environment (worktree sandbox) (steps (check marker #:cmd marker)))""")
    al("gen", directory)
    first = al("verify", directory, "--cmd", "marker")  # base built, thin layer applied, ENTRYPOINT reset
    assert "environment image built" in first.stdout + first.stderr
    second = al("verify", directory, "--cmd", "marker")
    assert "environment image built" not in second.stdout + second.stderr
    assert "building the repository's" not in second.stdout + second.stderr


@pytest.mark.podman
def test_devcontainer_reuse_builds_and_prepares(reuse):
    if alit.sh(["podman", "image", "exists", alit.PYTHON_IMAGE]).returncode != 0:
        pytest.skip(f"{alit.PYTHON_IMAGE} is not in local storage (`podman pull` it once)")
    app, layer = reuse
    plain = json.dumps({"image": alit.PYTHON_IMAGE, "containerEnv": {"QUILL_MODE": "dev"},
                        "postCreateCommand": "echo prepared > .prepared"})
    repo = app("quill", {".devcontainer/devcontainer.json": plain + "\n"})
    directory = layer(repo, "quill-reuse", """  (codebase #:root "../quill" #:languages (python)
    (cmd mode "test \\"$QUILL_MODE\\" = dev && test -f .prepared"))
  (environment worktree)
  (environment podman #:id sandbox #:devcontainer ".devcontainer/devcontainer.json" #:agent-inside #f #:network none)
  (adw check #:environment (worktree sandbox) (steps (check mode #:cmd mode)))""")
    al("gen", directory)
    report = json.loads(al("verify", directory, "--cmd", "mode", "--json").stdout)
    assert [row["name"] for row in report] == ["prepare:postCreateCommand", "mode"]
    assert all(row["exit"] == 0 for row in report)
