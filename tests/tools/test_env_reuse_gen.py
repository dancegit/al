"""`al gen` reuses the environment the application describes: a flake dev shell is wrapped
(env/flake.nix, inputsFrom), a Containerfile or dev container becomes the base of a thin
env/Containerfile, and env/source.json / env/SOURCE.md say what is reused and what is not. The
application's files are read from its default branch and never edited. Needs Racket."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import env_fixtures
from conftest import al, manifest, status

pytestmark = pytest.mark.skipif(shutil.which("racket") is None, reason="needs racket")


def layer_for(app: Path, options: str, *, name: str = "reuse", cmds: str = "", agent: str = "",
              stack: str = "") -> Path:
    """A sibling layer of `app` with the given environment options and one ADW using all of them."""
    layer = app.parent / f"{app.name}.al"
    layer.mkdir(exist_ok=True)
    ids = stack or " ".join(["worktree"] + [opt.split("#:id ")[1].split()[0].rstrip(")") if "#:id " in opt
                                           else opt.split()[1] for opt in options.strip().splitlines()
                                           if opt.strip().startswith("(environment") and " worktree" not in opt])
    (layer / "agentic.rkt").write_text(f"""#lang al
(agentic-layer #:name "{name}"
  (codebase #:root "../{app.name}" #:languages (python) (cmd test "true"){cmds})
  {agent}
  {options if "(environment worktree" in options else "(environment worktree)" + chr(10) + "  " + options}
  (adw a #:environment ({ids}) (steps (check test #:cmd test))))
""")
    return layer


def gen(layer: Path, ok: bool = True):
    return al("gen", layer, ok=ok)


def test_flake_wrapper(tmp_path):
    app = env_fixtures.build(tmp_path, "lumen")
    layer = layer_for(app, '(environment nix-devshell #:flake "." #:shell "ci" #:packages ("jq") #:browser #t)')
    gen(layer)
    flake = (layer / "env/flake.nix").read_text()
    assert f'inputs.app.url = "git+file://{app}?ref=main";' in flake
    assert 'inputsFrom = [ app.devShells.${system}."ci" ];' in flake
    assert "(with pkgs; [ jq ]) ++ lib.optionals pkgs.stdenv.isLinux [ pkgs.chromium ]" in flake
    assert "export AL_BROWSER=${pkgs.chromium}/bin/chromium" in flake and "CHROME_BIN=$AL_BROWSER" in flake
    local = json.loads((layer / ".al-manifest.local.json").read_text())["files"]
    assert "env/flake.nix" in local and "env/flake.nix" not in manifest(layer)["files"]
    assert "/env/flake.nix" in (layer / ".gitignore").read_text()
    source = json.loads((layer / "env/source.json").read_text())
    assert source == {"version": 1, "podman": None, "nix": {"option": "nix-devshell", "flake": ".", "shell": "ci"}}
    assert "dev shell `ci` of `flake.nix`" in (layer / "env/SOURCE.md").read_text()
    # a flake in a subdirectory
    sub = env_fixtures.build(tmp_path / "sub", "lumen", {"nix/flake.nix": env_fixtures.LUMEN_FLAKE})
    sub_layer = layer_for(sub, '(environment nix-devshell #:flake "nix")')
    gen(sub_layer)
    assert f'inputs.app.url = "git+file://{sub}?ref=main&dir=nix";' in (sub_layer / "env/flake.nix").read_text()
    assert status(app) == "" and status(sub) == ""


def test_flake_must_exist_on_the_default_branch(tmp_path):
    app = env_fixtures.build(tmp_path, "lumen")
    (app / "nope").mkdir()
    (app / "nope/flake.nix").write_text(env_fixtures.LUMEN_FLAKE)  # in the working tree only: not on main
    cp = gen(layer_for(app, '(environment nix-devshell #:flake "nope")'), ok=False)
    assert cp.returncode != 0
    assert "(environment nix-devshell): nope/flake.nix is not on branch main of the application" in cp.stderr


def test_thin_containerfile(tmp_path):
    app = env_fixtures.build(tmp_path, "tarn")
    before = (app / "Containerfile").read_bytes()
    layer = layer_for(app, '(environment podman #:id sandbox #:containerfile "Containerfile" #:target "dev" #:context "." #:agent-inside #f)',
                      name="tarn")
    gen(layer)
    thin = (layer / "env/Containerfile").read_text()
    body = [line for line in thin.splitlines() if not line.startswith("#")]
    assert body[0] == "FROM localhost/al-tarn-base:latest"
    for line in ("USER root", "ENV HOME=/tmp/home", "ENTRYPOINT []", "CMD []"):
        assert line in body
    assert not set(env_fixtures.TARN_CONTAINERFILE.splitlines()) & set(body)
    assert json.loads((layer / "env/source.json").read_text()) == {
        "version": 1, "nix": None,
        "podman": {"option": "sandbox", "build": {"containerfile": "Containerfile", "target": "dev", "context": "."},
                   "image": "", "publish": [], "prepare": "", "notHonoured": []}}
    assert (app / "Containerfile").read_bytes() == before and status(app) == ""


def test_agent_cli_is_added_only_when_the_agent_runs_inside(tmp_path):
    app = env_fixtures.build(tmp_path, "tarn")
    layer = layer_for(app, '(environment podman #:id sandbox #:containerfile "Containerfile" #:agent-inside #t)',
                      agent="(agent claude-code)")
    gen(layer)
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader("al_tool", str(Path(__file__).resolve().parents[2] / "tools/al"))
    spec = importlib.util.spec_from_loader("al_tool", loader)
    tool = importlib.util.module_from_spec(spec)
    loader.exec_module(tool)
    assert f"RUN {tool.AGENT_CLI_INSTALL['claude-code']}" in (layer / "env/Containerfile").read_text()
    layer_for(app, '(environment podman #:id sandbox #:containerfile "Containerfile" #:agent-inside #f)',
              agent="(agent claude-code)")
    gen(layer)
    assert "claude.ai/install.sh" not in (layer / "env/Containerfile").read_text()


def test_devcontainer_image(tmp_path):
    app = env_fixtures.build(tmp_path, "quill")
    layer = layer_for(app, '(environment podman #:id sandbox #:devcontainer ".devcontainer/devcontainer.json" #:agent-inside #f)')
    cp = gen(layer)
    thin = (layer / "env/Containerfile").read_text()
    assert "\nFROM docker.io/library/python:3.12-slim\n" in thin
    apt = next(line for line in thin.splitlines() if "nodejs npm" in line)
    assert apt.startswith("RUN if command -v apt-get")
    assert 'ENV QUILL_MODE="dev"' in thin
    source = json.loads((layer / "env/source.json").read_text())["podman"]
    assert source["image"] == "docker.io/library/python:3.12-slim" and source["build"] is None
    assert source["publish"] == [8000] and source["prepare"] == "echo prepared > .prepared"
    md = (layer / "env/SOURCE.md").read_text()
    for item in ("features.ghcr.io/devcontainers/features/docker-in-docker:2", "features.node.version", "remoteUser",
                 "customizations"):
        row = next(line for line in md.splitlines() if line.startswith(f"- not honoured: {item} — "))
        assert len(row) > len(f"- not honoured: {item} — ") + 10  # with a reason
        note = next(line for line in cp.stdout.splitlines() if line.startswith("note: ") and f": {item} of " in line)
        assert "is not honoured: " in note
    assert status(app) == ""


def test_devcontainer_build_form(tmp_path):
    dc = '{ "build": { "dockerfile": "Dockerfile", "context": "..", "target": "dev" } }\n'
    app = env_fixtures.build(tmp_path, "quill", {".devcontainer/devcontainer.json": dc,
                                                 ".devcontainer/Dockerfile": "FROM docker.io/library/python:3.12-slim AS dev\n"})
    layer = layer_for(app, '(environment podman #:id sandbox #:devcontainer ".devcontainer/devcontainer.json")')
    gen(layer)
    source = json.loads((layer / "env/source.json").read_text())["podman"]
    assert source["build"] == {"containerfile": ".devcontainer/Dockerfile", "context": ".", "target": "dev"}
    assert source["image"] == ""
    assert "\nFROM localhost/al-reuse-base:latest\n" in (layer / "env/Containerfile").read_text()


def test_devcontainer_compose_form_is_refused(tmp_path):
    dc = '{ "dockerComposeFile": "compose.yaml", "service": "app" }\n'
    app = env_fixtures.build(tmp_path, "quill", {".devcontainer/devcontainer.json": dc})
    cp = gen(layer_for(app, '(environment podman #:id sandbox #:devcontainer ".devcontainer/devcontainer.json")'), ok=False)
    assert cp.returncode != 0 and "dockerComposeFile" in cp.stderr
    assert "#:containerfile" in cp.stderr and "#:base-image" in cp.stderr


def test_prepare_overrides_post_create(tmp_path):
    app = env_fixtures.build(tmp_path, "quill")
    layer = layer_for(app, '(environment worktree #:prepare (install))\n'
                           '  (environment podman #:id sandbox #:devcontainer ".devcontainer/devcontainer.json")',
                      cmds='\n    (cmd install "uv sync --locked")', stack="worktree sandbox")
    gen(layer)
    assert json.loads((layer / "env/source.json").read_text())["podman"]["prepare"] == ""
    assert "- postCreateCommand: replaced by #:prepare" in (layer / "env/SOURCE.md").read_text()


PLAIN_CONTAINERFILE = """# Generated by al from agentic.rkt — do not edit.
FROM docker.io/library/debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*
RUN echo one
ENV HOME=/tmp/home
RUN mkdir -p /tmp/home && chmod 1777 /tmp/home
WORKDIR /work
"""

PLAIN_FLAKE = """# Generated by al from agentic.rkt — do not edit.
{
  description = "Agentic layer toolchain for plain";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in {
      devShells = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = with pkgs; [ python312 jq ];
            shellHook = ''
              export AL_LAYER="plain"
              echo hi
            '';
          };
        });
    };
}
"""


def test_browser_in_a_base_image_layer(tmp_path):
    app = env_fixtures.build(tmp_path, "moss")
    layer = layer_for(app, '(environment podman #:id sandbox #:base-image "debian:stable-slim" #:browser #t)')
    gen(layer)
    thin = (layer / "env/Containerfile").read_text()
    assert "apt-get install -y --no-install-recommends chromium fonts-liberation" in thin
    assert "ENV AL_BROWSER=/usr/bin/chromium CHROME_BIN=/usr/bin/chromium" in thin
    assert not (layer / "env/source.json").exists()
    # a layer that uses none of the new options generates exactly what it did before
    plain = layer_for(app, '(environment nix-devshell #:packages ("python312" "jq") #:shell-hook "echo hi")\n'
                           '  (environment podman #:id sandbox #:base-image "docker.io/library/debian:stable-slim" '
                           '#:packages ("git" "curl") #:setup ("echo one"))', name="plain")
    gen(plain)
    assert (plain / "env/Containerfile").read_text() == PLAIN_CONTAINERFILE
    assert (plain / "env/flake.nix").read_text() == PLAIN_FLAKE
    assert not (plain / "env/source.json").exists() and not (plain / "env/SOURCE.md").exists()


def test_gen_twice_changes_nothing(tmp_path):
    app = env_fixtures.build(tmp_path, "quill")
    layer = layer_for(app, '(environment nix-devshell #:flake "." #:packages ("jq"))\n'
                           '  (environment podman #:id sandbox #:devcontainer ".devcontainer/devcontainer.json")',
                      stack="worktree nix-devshell sandbox")
    (app / "flake.nix").write_text(env_fixtures.LUMEN_FLAKE)
    import subprocess
    subprocess.run(["git", "-C", str(app), "add", "flake.nix"], check=True)
    subprocess.run(["git", "-C", str(app), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "flake"], check=True)
    gen(layer)
    assert "(0 changed)" in gen(layer).stdout


def test_verify_prepare_rows_come_first(tmp_path):
    app = env_fixtures.build(tmp_path, "lumen")
    layer = layer_for(app, "(environment worktree #:prepare (install))", cmds='\n    (cmd install "true")',
                      stack="worktree")
    gen(layer)
    al("verify", layer, "--json")
    report = json.loads((layer / "agents/verify/report.json").read_text())
    first = report[0]
    assert first["name"] == "prepare:install" and first["exit"] == 0
    assert first["workflow"] == "a" and first["stack"] and first["stack"] == report[-1]["stack"]
    assert [row["name"] for row in report].count("prepare:install") == 1
    assert status(app) == ""
