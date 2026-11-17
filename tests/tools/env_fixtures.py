"""Small repositories that describe their environment in their own files: a flake dev shell, a dev
container, a Containerfile, shell.nix, devenv, a compose file, version files, CI workflows and
.env examples, alone and mixed. The names are invented. Written by the tests rather than kept as
files in this repository (like suite_setup.py).

Every fixture holds a `trap.sh` and commands that would leave a file `RAN-<name>` in the repository
if anything executed them (a flake shellHook, a Containerfile RUN, a dev container lifecycle
command, a package.json preinstall script, a CI step), so a test can prove that reading the setup
ran nothing."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _trap(name: str) -> str:
    return f'#!/bin/sh\n# would mark the repository if anything ran it\ntouch "$(dirname "$0")/RAN-{name}"\n'


LUMEN_FLAKE = """{
  description = "Lumen: a page that says it is ready";

  inputs.nixpkgs.url = "@NIXPKGS@";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in {
      devShells = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = [ pkgs.coreutils pkgs.gnugrep ];
            shellHook = ''
              export LUMEN_SHELL=default
            '';
          };
          ci = pkgs.mkShell {
            packages = [ pkgs.coreutils ];
            shellHook = ''
              sh trap.sh
            '';
          };
        });
    };
}
"""

LUMEN_PACKAGE = """{
  "name": "lumen",
  "private": true,
  "scripts": {
    "preinstall": "sh trap.sh",
    "e2e": "sh e2e.sh"
  },
  "devDependencies": {
    "@playwright/test": "^1.48.0"
  }
}
"""

QUILL_DEVCONTAINER = """// Quill's development container
{
  "name": "quill",
  /* the image the team works in */
  "image": "docker.io/library/python:3.12-slim",
  "features": {
    "ghcr.io/devcontainers/features/node:1": { "version": "20" },
    "ghcr.io/devcontainers/features/docker-in-docker:2": {},
  },
  "containerEnv": { "QUILL_MODE": "dev" },
  "onCreateCommand": "sh trap.sh",
  "postCreateCommand": "echo prepared > .prepared",
  "forwardPorts": [8000],
  "remoteUser": "vscode",
  "customizations": {
    "vscode": { "extensions": ["ms-python.python"] },
  },
}
"""

TARN_CONTAINERFILE = """FROM docker.io/library/python:3.12-slim AS dev
RUN echo tarn-dev > /opt/tarn-marker
RUN touch RAN-tarn
EXPOSE 8080
ENTRYPOINT ["/bin/false"]

FROM dev AS prod
CMD ["python", "-m", "tarn"]
"""

MOSS_SHELL = """{ pkgs ? import <nixpkgs> {} }:
pkgs.mkShell {
  buildInputs = with pkgs; [ python312 sqlite ];
  shellHook = "sh trap.sh";
}
"""

FEN_DEVENV = """{ pkgs, ... }: {
  languages.go.enable = true;
  packages = [ pkgs.git ];
  enterShell = "sh trap.sh";
}
"""

DEVENV_YAML = """inputs:
  nixpkgs:
    url: github:cachix/devenv-nixpkgs/rolling
"""

BROOK_COMPOSE = """services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: brook-secret-value
      POSTGRES_DB: brook
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
  cache:
    image: redis:7-alpine
  app:
    build: .
    command: sh trap.sh
"""

HERON_PACKAGE = """{
  "name": "heron",
  "private": true,
  "packageManager": "pnpm@9.1.0",
  "engines": { "node": ">=18 <19" },
  "scripts": { "preinstall": "sh trap.sh" }
}
"""

WREN_CI = """name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: wren-ci-password-value
    steps:
      - uses: actions/checkout@v4
      - run: sudo apt-get update && sudo apt-get install -y libpq-dev pkg-config
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: sh trap.sh
"""

KESTREL_FLAKE = """{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let pkgs = nixpkgs.legacyPackages.x86_64-linux;
    in {
      devShells.x86_64-linux.default = pkgs.mkShell {
        packages = [ pkgs.python312 pkgs.uv ];
        shellHook = "sh trap.sh";
      };
    };
}
"""

MARTEN_COMPOSE = """services:
  db:
    image: docker.io/library/postgres:16
    environment:
      - POSTGRES_PASSWORD=marten-secret-value
"""

OSPREY_CI = """name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: sh trap.sh
"""

FIXTURES: dict[str, dict[str, str]] = {
    "lumen": {
        "flake.nix": LUMEN_FLAKE,
        "package.json": LUMEN_PACKAGE,
        "bun.lock": '{\n  "lockfileVersion": 1\n}\n',
        "web/index.html": "<!doctype html>\n<html><body><h1>Lumen ready</h1></body></html>\n",
        "e2e.sh": "\"$AL_BROWSER\" --headless --no-sandbox --disable-gpu --dump-dom \"file://$PWD/web/index.html\" | grep -q 'Lumen ready'\n",
    },
    "quill": {
        ".devcontainer/devcontainer.json": QUILL_DEVCONTAINER,
        "pyproject.toml": '[project]\nname = "quill"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n',
        "uv.lock": "version = 1\n",
        "quill.py": "MODE = 'dev'\n",
    },
    "tarn": {
        "Containerfile": TARN_CONTAINERFILE,
        ".tool-versions": "nodejs 20.11.0\n",
        "tarn.py": "print('tarn')\n",
    },
    "moss": {
        "shell.nix": MOSS_SHELL,
        "moss.py": "print('moss')\n",
    },
    "fen": {
        "devenv.nix": FEN_DEVENV,
        "devenv.yaml": DEVENV_YAML,
        "main.go": "package main\n\nfunc main() {}\n",
    },
    "brook": {
        "docker-compose.yml": BROOK_COMPOSE,
        "brook.py": "print('brook')\n",
    },
    "heron": {
        ".tool-versions": "nodejs 20.11.0\npython 3.12.1\n",
        "mise.toml": '[tools]\nnode = "20"\n',
        ".nvmrc": "20\n",
        "package.json": HERON_PACKAGE,
        "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        "rust-toolchain.toml": '[toolchain]\nchannel = "1.79"\n',
        "Cargo.toml": '[package]\nname = "heron"\nversion = "0.1.0"\nedition = "2021"\n',
        "Cargo.lock": "version = 3\n",
        ".python-version": "3.11\n",
        "pyproject.toml": '[project]\nname = "heron"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n',
        "go.mod": "module heron\n\ngo 1.22\n",
    },
    "wren": {
        ".github/workflows/ci.yml": WREN_CI,
        ".env.example": "# copy to .env\nSECRET_KEY=wren-example-secret-value\nexport DATABASE_URL=postgres://u:p@h/db\n",
        ".env.sample": "API_TOKEN=\n",
        "wren.py": "print('wren')\n",
    },
    "kestrel": {
        "flake.nix": KESTREL_FLAKE,
        ".devcontainer.json": '{ "image": "docker.io/library/debian:stable-slim", "postCreateCommand": "sh trap.sh" }\n',
        "pyproject.toml": '[project]\nname = "kestrel"\nversion = "0.1.0"\ndependencies = ["playwright>=1.40"]\n',
        "uv.lock": "version = 1\n",
    },
    "marten": {
        "Dockerfile": "FROM docker.io/library/node:20-slim\nRUN touch RAN-marten\nEXPOSE 3000\n",
        "compose.yaml": MARTEN_COMPOSE,
        ".env.example": "SESSION_SECRET=marten-example-secret\n",
        "package.json": '{\n  "name": "marten",\n  "scripts": { "preinstall": "sh trap.sh" },\n  "devDependencies": { "cypress": "^13.0.0" }\n}\n',
        "package-lock.json": '{\n  "lockfileVersion": 3\n}\n',
    },
    "osprey": {
        "devenv.nix": '{ pkgs, ... }: {\n  languages.python.enable = true;\n  enterShell = "sh trap.sh";\n}\n',
        "devenv.yaml": DEVENV_YAML,
        ".tool-versions": "nodejs 18.19.0\n",
        ".github/workflows/ci.yml": OSPREY_CI,
        "uv.lock": "version = 1\n",
        "go.mod": "module osprey\n\ngo 1.22\n",
        "go.sum": "",
    },
}


def build(root: Path, name: str, files: dict[str, str] | None = None) -> Path:
    """<root>/<name>: a git repository on branch main with the fixture's files (plus trap.sh and a
    README) committed. `files` replaces or adds files before the commit."""
    repo = root / name
    repo.mkdir(parents=True)
    content = {"README.md": f"# {name}\n", "trap.sh": _trap(name), **FIXTURES[name], **(files or {})}
    for rel, text in content.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"{name}"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True)
    return repo
