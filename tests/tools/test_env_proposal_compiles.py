"""Every proposal `al scout` writes from a repository's own environment files is a valid layer
definition: put inside a minimal layer it compiles. Needs Racket (skipped without it)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import env_fixtures
from conftest import al

pytestmark = pytest.mark.skipif(shutil.which("racket") is None, reason="needs racket")


def proposed_layer(tmp_path: Path, name: str) -> Path:
    """A sibling layer whose agentic.rkt is the fixture's proposal and one ADW that uses it."""
    repo = env_fixtures.build(tmp_path, name)
    env = json.loads(al("scout", repo, "--json").stdout)["environment"]
    p = env["proposal"]
    layer = tmp_path / f"{name}.al"
    layer.mkdir()
    codebase = "\n".join(f"    {line}" for line in p["codebase"].splitlines())
    (layer / "agentic.rkt").write_text(f"""#lang al
(agentic-layer #:name "{name}"
  (codebase #:root "../{name}" #:languages (python)
    (cmd test "true")
{codebase}
    )
{p["layer"]}
  (adw check-only #:environment ({' '.join(p["ids"])})
    (steps (check test #:cmd test))))
""")
    return layer


@pytest.mark.parametrize("name", sorted(env_fixtures.FIXTURES))
def test_every_proposal_compiles(tmp_path, name):
    layer = proposed_layer(tmp_path, name)
    al("compile", layer)


def env_of(ir: dict, kind: str) -> dict:
    return next(o for o in ir["environments"] if o["kind"] == kind)


def test_lumen_proposal_reuses_the_flake_and_adds_the_browser(tmp_path):
    ir = json.loads(al("compile", proposed_layer(tmp_path, "lumen")).stdout)
    nix = env_of(ir, "nix-devshell")
    assert nix["flake"] == "." and nix["shell"] == "default" and nix["browser"] is True
    quill = json.loads(al("compile", proposed_layer(tmp_path, "quill")).stdout)
    assert env_of(quill, "podman")["devcontainer"] == ".devcontainer/devcontainer.json"
