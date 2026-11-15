"""Fixtures of the tools/al suite: small throwaway applications with generated layers. No network,
no LLM (the mock agent), no podman. Run with:

    uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AL = ROOT / "tools" / "al"
KIT = ROOT / "kit"

LAYER_TEMPLATE = """#lang al
(agentic-layer #:name "notes" #:description "Agentic layer of a tiny test app"
  (codebase #:root "{root}" #:languages (python) #:description "A tiny app: app.py and test.sh."
    (cmd test "./test.sh" #:timeout 60 #:description "the tests pass")
    (paths #:source ("app.py") #:tests ("test.sh") #:protected ("test.sh")))
  {placement}
  (command plan #:template plan-chore #:model-set heavy #:output path #:validation (test))
  (command tidy #:template plan-chore #:model-set heavy #:output path)
  (command commit #:template commit)
  (hop implement #:template run-plan #:takes plan)
  (hop new-adw #:template meta-adw #:produces adw)
  (expert storage #:domain "How notes are stored" #:paths ("app.py"))
  (adw change #:description "Plan, build, test; a person reviews"
    (steps (run plan #:command plan #:args ("$prompt")) (run build #:command implement #:args ("$plan"))
           (check test #:cmd test) (run commit #:command commit) (review) (ship))))
"""

# LAYER_TEMPLATE plus what `al deploy` keeps running: a poller, a webhook and a forge that needs a token.
DEPLOY_LAYER = (LAYER_TEMPLATE
                .replace("  {placement}\n", "  {placement}\n"
                         '  (forge forgejo #:host "https://forge.example.org" #:repo "acme/notes")\n'
                         "  (prompt-input tasklist)\n  (trigger cron)\n  (trigger webhook #:port 8001)\n")
                .replace('(adw change #:description "Plan, build, test; a person reviews"',
                         '(adw change #:description "Plan, build, test; a person reviews"\n'
                         "    #:input (tasklist) #:trigger (cron webhook)"))
assert DEPLOY_LAYER.count("(trigger ") == 2 and "#:trigger (cron webhook)" in DEPLOY_LAYER


def sh(argv, cwd=None, env=None, timeout=900) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def git(repo: Path, *args: str) -> str:
    cp = sh(["git", "-C", repo, *args])
    assert cp.returncode == 0, f"git {' '.join(args)}: {cp.stderr}"
    return cp.stdout.strip()


def commit(repo: Path, message: str, *paths: str) -> None:
    git(repo, "add", *(paths or ["-A"]))
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


def al(*args, cwd=None, env=None, ok=True, timeout=900) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    full_env.update(env or {})
    cp = sh([AL, *args], cwd=cwd, env=full_env, timeout=timeout)
    if ok:
        assert cp.returncode == 0, f"al {' '.join(map(str, args))} exited {cp.returncode}\n{cp.stdout}\n{cp.stderr}"
    return cp


def status(repo: Path) -> str:
    return git(repo, "status", "--porcelain", "--untracked-files=all")


@pytest.fixture
def app(tmp_path: Path) -> Path:
    repo = tmp_path / "notes"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("NOTES = []\n")
    (repo / "test.sh").write_text("#!/bin/sh\npython3 -c 'import app'\n")
    (repo / "test.sh").chmod(0o755)
    commit(repo, "the app")
    return repo


@pytest.fixture
def in_repo(app: Path) -> Path:
    """<app>/agentic: an in-repo layer, generated and committed with the app."""
    al("init", app, "--in-repo")
    layer = app / "agentic"
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="..", placement='(layer in-repo #:dir "agentic")'))
    al("gen", layer)
    commit(app, "the agentic layer", "agentic")
    assert status(app) == ""
    return layer


@pytest.fixture
def deployable(app: Path) -> Path:
    """<app>/agentic: an in-repo layer with a cron trigger, a webhook trigger and a forgejo forge,
    generated and committed with the app (what `al deploy` puts on a host)."""
    al("init", app, "--in-repo")
    layer = app / "agentic"
    (layer / "agentic.rkt").write_text(DEPLOY_LAYER.format(root="..", placement='(layer in-repo #:dir "agentic")'))
    al("gen", layer)
    commit(app, "the agentic layer", "agentic")
    assert status(app) == ""
    return layer


@pytest.fixture
def sibling(app: Path) -> Path:
    """<app>.al: a sibling layer (its own git repository, nothing committed yet)."""
    layer = app.parent / "notes.al"
    layer.mkdir()
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="../notes", placement="(layer sibling)"))
    al("gen", layer)
    return layer


def manifest(layer: Path) -> dict:
    return json.loads((layer / ".al-manifest.json").read_text())


def mock_scripts(tmp_path: Path, scripts: dict[str, dict]) -> Path:
    """A directory of <step>.json mock responses for `al improve --dry-run --mock-scripts`."""
    directory = tmp_path / "mock-scripts"
    directory.mkdir(exist_ok=True)
    for step, data in scripts.items():
        (directory / f"{step}.json").write_text(json.dumps(data))
    return directory


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "browser: drives the host's Chrome through Playwright (skipped without one)")


# A run's sandbox points git at the run's own object store through environment variables. Tests build
# their own repositories, so those variables must not reach them: clear them once, before any test.
import os as _os
for _name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
              "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_QUARANTINE_PATH", "GIT_CEILING_DIRECTORIES"):
    _os.environ.pop(_name, None)
