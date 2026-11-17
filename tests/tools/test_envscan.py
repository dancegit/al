"""`al scout` reads the environment a repository already describes (flake, shell.nix, devenv,
Containerfile, dev container, compose, version files, lockfiles, CI, .env examples) without
executing anything, and writes an `## Environment` section with a proposed declaration whose every
line cites its file. The JSON (`al scout --json`) carries the same data the text is made of."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import env_fixtures
from conftest import ROOT, al, status

sys.path.insert(0, str(ROOT / "tools"))
import al_envscan  # noqa: E402


def scan(tmp_path: Path, name: str, files: dict[str, str] | None = None) -> tuple[Path, dict, str]:
    repo = env_fixtures.build(tmp_path, name, files)
    data = json.loads(al("scout", repo, "--json").stdout)
    return repo, data["environment"], al("scout", repo).stdout


def section(text: str) -> str:
    return text[text.index("## Environment"):text.index("\n\n## Artefacts")]


def in_process(repo: Path, **kw) -> dict:
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    return al_envscan.detect(repo, tracked, **kw)


def source(env: dict, kind: str) -> dict:
    return next(s for s in env["sources"] if s["kind"] == kind)


def test_flake_dev_shells_are_listed(tmp_path):
    _, env, text = scan(tmp_path, "lumen")
    flake = source(env, "nix-flake")
    assert flake["file"] == "flake.nix" and flake["dir"] == "."
    assert flake["shells"] == ["ci", "default"]
    lines = env_fixtures.LUMEN_FLAKE.splitlines()
    assert "default = pkgs.mkShell" in lines[flake["shell_lines"]["default"] - 1]
    assert "ci = pkgs.mkShell" in lines[flake["shell_lines"]["ci"] - 1]
    assert f"- `flake.nix:{flake['line']}` (nix-flake): dev shells ci, default" in text


def test_shell_nix_and_devenv_are_listed_not_reused(tmp_path):
    _, moss, moss_text = scan(tmp_path, "moss")
    shell = source(moss, "shell-nix")
    assert shell["packages"] == ["python312", "sqlite"] and "listed, not reused" in shell["says"]
    assert '(environment nix-devshell #:packages ("python312" "sqlite")) ; from shell.nix:3' in moss["proposal"]["layer"]
    _, fen, fen_text = scan(tmp_path, "fen")
    devenv = source(fen, "devenv")
    assert "listed, not reused" in devenv["says"]
    assert [e["file"] for e in fen["tools"]["go"]] == ["devenv.nix"]
    assert '(environment nix-devshell #:packages ("go" "git"))' in fen["proposal"]["layer"]
    assert "listed, not reused" in moss_text and "listed, not reused" in fen_text


def test_containerfile_stages_targets_and_ports(tmp_path):
    _, env, text = scan(tmp_path, "tarn")
    cf = source(env, "containerfile")
    assert cf["stages"] == ["dev", "prod"] and cf["target"] == "dev" and cf["ports"] == [8080]
    assert '#:containerfile "Containerfile" #:target "dev" #:context "."' in env["proposal"]["layer"]
    assert "EXPOSE 8080" in text


def test_devcontainer_is_read_as_jsonc_and_planned(tmp_path):
    _, env, text = scan(tmp_path, "quill")
    dc = env["devcontainer"]
    assert dc["file"] == ".devcontainer/devcontainer.json"
    assert dc["image"] == "docker.io/library/python:3.12-slim" and dc["build"] is None
    assert dc["packages"] == ["nodejs", "npm"]
    assert "features.ghcr.io/devcontainers/features/node:1 → apt nodejs npm" in dc["honoured"]
    not_honoured = {n["item"]: n["reason"] for n in dc["not_honoured"]}
    assert "version" in "".join(k for k in not_honoured if k.startswith("features.node."))
    assert {"features.node.version", "features.ghcr.io/devcontainers/features/docker-in-docker:2", "remoteUser",
            "customizations"} <= set(not_honoured)
    assert all(not_honoured.values())
    assert dc["container_env"] == ["QUILL_MODE"] and dc["post_create"] == "echo prepared > .prepared"
    assert dc["forward_ports"] == [8000]
    assert '#:devcontainer ".devcontainer/devcontainer.json"' in env["proposal"]["layer"]
    for needle in ("not honoured: remoteUser", "docker-in-docker", "postCreateCommand: `echo prepared > .prepared`",
                   "forwardPorts: 8000", "containerEnv names: QUILL_MODE"):
        assert needle in text


def test_read_devcontainer_strips_comments_and_trailing_commas_only_outside_strings():
    data = al_envscan.read_devcontainer('{ // c\n "a": "http://x/*y*/", /* z */ "b": [1, 2,], "c": "q\\"//",\n}')
    assert data == {"a": "http://x/*y*/", "b": [1, 2], "c": 'q"//'}


def test_compose_services_become_service_declarations(tmp_path):
    repo, env, text = scan(tmp_path, "brook")
    layer = env["proposal"]["layer"]
    assert ('(service container #:id db #:image "docker.io/library/postgres:16-alpine" '
            '#:env ("POSTGRES_PASSWORD=SET-ME" "POSTGRES_DB=SET-ME") #:ready-cmd "pg_isready -U postgres")') in layer
    assert '(service container #:id cache #:image "docker.io/library/redis:7-alpine" #:ready-cmd "redis-cli ping")' in layer
    assert "#:id app" not in layer and "#:services (db cache)" in layer
    raw = json.dumps(json.loads(al("scout", repo, "--json").stdout))
    assert "brook-secret-value" not in raw and "brook-secret-value" not in text


def test_versions_and_conflicts(tmp_path):
    _, env, _ = scan(tmp_path, "heron")
    tools = env["tools"]
    assert [(e["version"], e["at"]) for e in tools["node"]] == [
        ("20.11.0", ".tool-versions:1"), ("20", "mise.toml:2"), ("20", ".nvmrc:1"), (">=18 <19", "package.json engines.node")]
    assert [e["version"] for e in tools["python"]] == ["3.12.1", "3.11", ">=3.12"]
    assert [e["version"] for e in tools["rust"]] == ["1.79"] and [e["version"] for e in tools["go"]] == ["1.22"]
    assert env["package_manager"] == {"name": "pnpm", "version": "9.1.0",
                                      "from": ["package.json packageManager", "pnpm-lock.yaml"]}
    assert env["conflicts"] == [
        "node: 20 (.tool-versions:1, mise.toml:2, .nvmrc:1) vs >=18 <19 (package.json engines.node)",
        "python: 3.12.1 (.tool-versions:2) vs 3.11 (.python-version:1)"]
    assert env["proposal"]["layer"].count(";; conflict: ") == 2


def test_ci_system_packages_and_services(tmp_path):
    _, env, text = scan(tmp_path, "wren")
    line = env_fixtures.WREN_CI.splitlines().index(
        "      - run: sudo apt-get update && sudo apt-get install -y libpq-dev pkg-config") + 1
    assert env["system_packages"] == [{"name": "libpq-dev", "file": ".github/workflows/ci.yml", "line": line},
                                      {"name": "pkg-config", "file": ".github/workflows/ci.yml", "line": line}]
    assert [e["version"] for e in env["tools"]["node"]] == ["20"]
    assert [e["version"] for e in env["tools"]["python"]] == ["3.12"]
    assert [(s["id"], s["image"]) for s in env["services"]] == [("postgres", "docker.io/library/postgres:16")]
    assert f"- System packages: `libpq-dev` (.github/workflows/ci.yml:{line})" in text
    assert "wren-ci-password-value" not in text


def test_env_examples_list_names_never_values(tmp_path):
    repo, env, text = scan(tmp_path, "wren")
    assert env["env_files"] == [{"file": ".env.example", "names": ["SECRET_KEY", "DATABASE_URL"]},
                                {"file": ".env.sample", "names": ["API_TOKEN"]}]
    raw = al("scout", repo, "--json").stdout
    for secret in ("wren-example-secret-value", "postgres://u:p@h/db"):
        assert secret not in raw and secret not in text
    layer = env["proposal"]["layer"]
    assert '(environment worktree #:copy-files (".env"))' in layer
    comment = next(l for l in layer.splitlines() if l.startswith(";; the run needs"))
    assert "SECRET_KEY, DATABASE_URL, API_TOKEN" in comment and "local.env" in comment


def test_browser_need_and_why(tmp_path):
    assert scan(tmp_path, "lumen")[1]["browser"] == {"needed": True, "why": "package.json devDependencies @playwright/test",
                                                     "file": "package.json"}
    assert scan(tmp_path, "kestrel")[1]["browser"]["why"] == "pyproject.toml dependencies playwright"
    assert scan(tmp_path, "marten")[1]["browser"]["why"] == "package.json devDependencies cypress"
    assert scan(tmp_path, "tarn")[1]["browser"]["needed"] is False


@pytest.mark.parametrize("lock", sorted(al_envscan.LOCK_PREPARE))
def test_prepare_from_lockfile(tmp_path, lock):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / lock).write_text("")
    env = al_envscan.detect(repo, [lock])
    expected = {"uv.lock": "uv sync --locked", "bun.lock": "bun install --frozen-lockfile",
                "bun.lockb": "bun install --frozen-lockfile", "package-lock.json": "npm ci",
                "pnpm-lock.yaml": "pnpm install --frozen-lockfile", "yarn.lock": "yarn install --frozen-lockfile",
                "Cargo.lock": "cargo fetch --locked", "go.sum": "go mod download"}[lock]
    cache = {"uv.lock": "uv", "Cargo.lock": "cargo", "go.sum": "go"}.get(lock, "npm")
    assert env["prepare"] == [{"cmd_name": "install", "shell": expected, "cache": cache, "file": lock, "existing": False}]
    assert f'(cmd install "{expected}" #:description "dependencies resolve") ; from {lock}' in env["proposal"]["codebase"]
    assert "#:prepare (install)" in env["proposal"]["layer"]


def test_prepare_cmd_name_does_not_clash(tmp_path):
    repo = env_fixtures.build(tmp_path, "quill")
    other = in_process(repo, commands=[{"name": "install", "shell": "uv sync"}])
    assert other["prepare"][0]["cmd_name"] == "prepare-deps"
    same = in_process(repo, commands=[{"name": "install", "shell": "uv sync --locked"}])
    assert same["prepare"][0]["cmd_name"] == "install" and same["prepare"][0]["existing"] is True
    assert same["proposal"]["codebase"].startswith(";; (cmd install ")
    assert in_process(repo)["prepare"][0]["cmd_name"] == "install"


def test_mixed_fixtures(tmp_path):
    _, kestrel, _ = scan(tmp_path, "kestrel")
    layer = kestrel["proposal"]["layer"]
    assert '(environment nix-devshell #:flake "." #:shell "default" #:browser #t)' in layer
    assert ';; alternative: (environment podman #:id sandbox #:devcontainer ".devcontainer.json")' in layer
    _, marten, _ = scan(tmp_path, "marten")
    layer = marten["proposal"]["layer"]
    assert '(environment podman #:id sandbox #:containerfile "Dockerfile" #:context "." #:browser #t' in layer
    assert "#:target" not in layer and "#:services (db)" in layer
    assert "marten-secret-value" not in json.dumps(marten) and "marten-example-secret" not in json.dumps(marten)
    _, osprey, _ = scan(tmp_path, "osprey")
    assert '(environment nix-devshell #:packages ("python3"))' in osprey["proposal"]["layer"]
    node = next(c for c in osprey["conflicts"] if c.startswith("node: "))
    assert "18.19.0 (.tool-versions:1)" in node and "20 (.github/workflows/ci.yml:" in node
    assert [p["cmd_name"] for p in osprey["prepare"]] == ["install", "install-go"]


CITE = re.compile(r" ; from (.+)$")


@pytest.mark.parametrize("name", sorted(env_fixtures.FIXTURES))
def test_proposal_lines_cite_their_source(tmp_path, name):
    repo, env, _ = scan(tmp_path, name)
    tracked = set(subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True).stdout.split())
    lines = [l for l in env["proposal"]["lines"] if l.strip()]
    assert lines
    for line in lines:
        if line.startswith(";;"):
            continue
        m = CITE.search(line)
        assert m, f"no `; from` citation: {line}"
        for cited in m.group(1).split(", "):
            if cited.startswith("al ("):
                continue  # al's own default or guess, said in so many words
            path = re.sub(r"(:\d+| [\w.-]+)$", "", cited)
            assert path in tracked, f"{line}: {cited} is not a tracked file"


def test_json_equals_the_text(tmp_path):
    for name in ("quill", "heron", "wren"):
        repo = env_fixtures.build(tmp_path, name)
        data = json.loads(al("scout", repo, "--json").stdout)
        assert al_envscan.render_section(data["environment"]) == section(al("scout", repo).stdout)


def test_detection_executes_nothing(tmp_path, monkeypatch):
    for name in env_fixtures.FIXTURES:
        repo = env_fixtures.build(tmp_path, name)
        al("scout", repo)
        al("scout", repo, "--json")
        assert not list(repo.glob("RAN-*")) and status(repo) == "", name
    repo = env_fixtures.build(tmp_path / "again", "quill")
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True).stdout.split()

    def refuse(*_a, **_k):
        raise AssertionError("detection tried to run a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    assert al_envscan.detect(repo, tracked)["devcontainer"]["file"] == ".devcontainer/devcontainer.json"
    assert not re.search(r"^\s*(import|from) (subprocess|os)\b", Path(al_envscan.__file__).read_text(), re.M)


def test_untracked_files_are_ignored(tmp_path):
    repo = env_fixtures.build(tmp_path, "moss")
    (repo / "flake.nix").write_text(env_fixtures.LUMEN_FLAKE)
    env = json.loads(al("scout", repo, "--json").stdout)["environment"]
    assert not [s for s in env["sources"] if s["kind"] == "nix-flake"]


def test_unparsable_files_become_notes(tmp_path):
    repo, env, text = scan(tmp_path, "brook", {"docker-compose.yml": "services:\n  db: [unclosed\n",
                                               ".devcontainer.json": "{ \"image\": "})
    assert any(n.startswith("docker-compose.yml: does not parse as YAML") for n in env["notes"])
    assert any(n.startswith(".devcontainer.json: does not parse") for n in env["notes"])
    assert "- note: docker-compose.yml: does not parse as YAML" in text


def test_a_repository_without_environment_files(app):
    env = json.loads(al("scout", app, "--json").stdout)["environment"]
    text = section(al("scout", app).stdout)
    assert "### Found\n\n- (nothing found)" in text
    assert env["proposal"]["layer"].splitlines() == [
        "(environment worktree) ; from al (every run has its own worktree)",
        '(environment nix-devshell #:packages ("python312" "uv" "git")) ; from al (nixpkgs guess from the languages)']
