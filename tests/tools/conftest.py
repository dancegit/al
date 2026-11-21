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

# DEPLOY_LAYER plus a podman environment, a registry and two kinds of host: `server` (provisioned by
# the shell script, the image pulled from the registry) and `nix` (Home Manager, the image streamed).
PROVISION_LAYER = DEPLOY_LAYER.replace(
    "  (prompt-input tasklist)\n",
    "  (prompt-input tasklist)\n  (environment worktree)\n  (environment podman)\n"
    '  (registry #:address-env "AL_REGISTRY")\n'
    "  (host #:id server #:provision script #:units (cron dashboard) #:dashboard-port 8770 #:images pull)\n"
    "  (host #:id nix #:provision home-manager #:images load)\n")
assert PROVISION_LAYER.count("(host ") == 2


# DEPLOY_LAYER without a forge and its webhook, plus a mock agent next to the default one, a podman environment, two
# hosts and a placement on them, one check and an auto review that merges: what `al run` places.
PLACEMENT_LAYER = (DEPLOY_LAYER
                   .replace('  (forge forgejo #:host "https://forge.example.org" #:repo "acme/notes")\n',
                            '  (forge none #:remote "origin")\n  (agent claude-code)\n  (agent mock #:id mock)\n'
                            "  (environment worktree)\n  (environment podman)\n"
                            "  (host #:id build-1)\n  (host #:id build-2)\n"
                            "  (placement auto #:hosts (home build-1 build-2) #:max-runs-per-host 2)\n"
                            "  (command review #:template review #:output json)\n"
                            "  (review auto #:require-checks (test) #:allowed-classes (chore bug feature patch))\n")
                   .replace("  (trigger webhook #:port 8001)\n", "")      # a webhook listens to forge events
                   .replace("    #:input (tasklist) #:trigger (cron webhook)",
                            "    #:input (tasklist) #:trigger (cron) #:environment (worktree podman) #:review auto"))
assert "(placement auto" in PLACEMENT_LAYER and "#:review auto" in PLACEMENT_LAYER


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
    """tools/al with this environment. AL_SANDBOX_OFFLINE=1 unless `env` says otherwise: `al gen` of a
    layer with a toolchain does not try to pin nixpkgs (nothing here downloads)."""
    full_env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    full_env["AL_SANDBOX_OFFLINE"] = "1"
    full_env.update(env or {})
    cp = sh([AL, *args], cwd=cwd, env=full_env, timeout=timeout)
    if ok:
        assert cp.returncode == 0, f"al {' '.join(map(str, args))} exited {cp.returncode}\n{cp.stdout}\n{cp.stderr}"
    return cp


def stub_bin(tmp_path: Path, name: str, script: str) -> Path:
    """Put an executable `name` with the given script (its own #! line) into tmp_path/stub-bin, and
    return that directory: put it first on PATH to stand in for nix or podman."""
    directory = tmp_path / "stub-bin"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(script)
    path.chmod(0o755)
    return directory


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


def gen_provision_layer(app: Path, pins: Path, text: str = PROVISION_LAYER) -> Path:
    """<app>/agentic with `text` (PROVISION_LAYER by default), generated with the pins file `pins`
    and committed with the app."""
    al("init", app, "--in-repo")
    layer = app / "agentic"
    (layer / "agentic.rkt").write_text(text.format(root="..", placement='(layer in-repo #:dir "agentic")'))
    al("gen", layer, env={"AL_HOST_PINS": str(pins)})
    commit(app, "the agentic layer", "agentic")
    assert status(app) == ""
    return layer


@pytest.fixture
def provisionable(app: Path, tmp_path: Path) -> Path:
    """<app>/agentic: PROVISION_LAYER, generated with pins that match what the fake host's curl
    serves (deploy_host.DOWNLOADS) and committed with the app."""
    from deploy_host import test_pins
    pins_dir = tmp_path / "pins"
    pins_dir.mkdir(exist_ok=True)
    return gen_provision_layer(app, test_pins(pins_dir))


@pytest.fixture
def placed(app: Path, tmp_path: Path):
    """A layer whose runs may execute on `home`, `build-1` and `build-2`:
    (layer directory, {id: deploy_host.Host}, the bare repository that is the git source, the environment for `al`).

    PLACEMENT_LAYER is generated in-repo; the podman option is then taken out of the workflow's environment
    list in the generated agentic-layer.json (the fake hosts have no container engine; the compiler rule that
    zero-touch needs podman is covered by the compiler tests). The repository is committed and pushed to
    `source.git` (its `origin`), deployed to both fake hosts with `al deploy … --as build-N`, and each host's
    env file names the git source."""
    from deploy_host import make_hosts

    al("init", app, "--in-repo")
    layer = app / "agentic"
    (layer / "agentic.rkt").write_text(PLACEMENT_LAYER.format(root="..", placement='(layer in-repo #:dir "agentic")'))
    al("gen", layer)
    path = layer / "agentic-layer.json"
    data = json.loads(path.read_text())
    for adw in data["adws"]:
        adw["environment"] = [e for e in adw["environment"] if e != "podman"]
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    commit(app, "the agentic layer", "agentic")
    source = tmp_path / "source.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "source.git")
    git(app, "remote", "add", "origin", str(source))
    git(app, "push", "-q", "origin", "main")
    hosts = make_hosts(tmp_path, ("build-1", "build-2"))
    for ident, host in hosts.items():
        host.runtime()
        host.login("claude-code")
        host.set_env_file("notes", AL_GIT_SOURCE=str(source))
        al("deploy", layer, "--to", f"{ident}.example", "--as", ident, "--no-enable", env=host.env())
    env = {"AL_HOSTS_LOCAL": str(tmp_path / "hosts"), "AL_GIT_SOURCE": str(source),
           "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    return layer, hosts, source, env


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


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "browser: drives the host's Chrome through Playwright (skipped without one)")
    config.addinivalue_line("markers", "slow: records clips with ffmpeg/Chrome (runs only with AL_SHOWCASE_RECORD=1)")
    config.addinivalue_line("markers", "podman: needs the real podman (skipped when the binary is missing); some tests also need a registry on 127.0.0.1:5000")
    config.addinivalue_line("markers", "nix: needs nix (skipped when the binary is missing)")
    # Every skip prints its reason (as -rs would), so a log says why a test did not run.
    if "s" not in (config.option.reportchars or ""):
        config.option.reportchars = (config.option.reportchars or "") + "s"


@pytest.fixture(scope="session", autouse=True)
def _al_labelled_podman():
    """Every image and container this session's tests make is labelled al.test (a podman shim first on
    PATH, kit/runtime/adw_modules/diskuse.py) and removed when the session ends; leftovers older than six
    hours go when it starts. Nothing happens without that file or without podman."""
    import importlib.util
    path = ROOT / "kit" / "runtime" / "adw_modules" / "diskuse.py"
    if not path.is_file():
        yield ""
        return
    spec = importlib.util.spec_from_file_location("al_diskuse", path)
    diskuse = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diskuse)
    with diskuse.labelled_session() as session:
        yield session


def pytest_runtest_setup(item: pytest.Item) -> None:
    import shutil
    for marker, binary in (("podman", "podman"), ("nix", "nix")):
        if item.get_closest_marker(marker) and shutil.which(binary) is None:
            pytest.skip(f"marked `{marker}` and `{binary}` is not on PATH")


# A run's sandbox points git at the run's own object store through environment variables. Tests build
# their own repositories, so those variables must not reach them: clear them once, before any test.
# SANDBOX_GIT keeps what was cleared, for the few tests that read the objects of this repository
# itself (the blobs of its index); outside a run it is empty. The values travel on under another
# name, because an xdist worker starts from the environment this process has already cleared.
import os as _os
SANDBOX_GIT: dict[str, str] = {}
for _name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
              "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_QUARANTINE_PATH", "GIT_CEILING_DIRECTORIES"):
    if _name in _os.environ:
        _os.environ["TESTS_SANDBOX_" + _name] = _os.environ.pop(_name)
    if "TESTS_SANDBOX_" + _name in _os.environ:
        SANDBOX_GIT[_name] = _os.environ["TESTS_SANDBOX_" + _name]
