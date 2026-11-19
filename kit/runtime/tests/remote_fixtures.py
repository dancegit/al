"""Test helper (not a test): a layer that is its own git repository, bare repositories that play
the git sources, and directories that play the homes of other hosts.

    layer = build_layer_repo(tmp_path, make_layer, app_repo, ir_overrides)
    app_git, layer_git = sources(tmp_path, app_repo, layer)
    host_home(tmp_path, "build-1", layer)          # tmp_path/hosts/build-1
    monkeypatch.setenv(…) for each of home_env(tmp_path) and local_hosts_env(tmp_path)

A host's home has `bin/uv` (a shim that runs `uv run [--quiet] SCRIPT ARGS…` with this python),
`.hostbin/` (links to the real programs a run calls; renaming it makes every command fail), a
clone of the layer at `al/layer` (where hostlink looks for a layer that is not in the application
repository), `.config/al/<name>.env` with the git sources as seen from that host, and the agent's
login file unless login=False. Nothing here reaches a real machine: hostlink runs the host's
commands with sh under that HOME and PATH (AL_HOSTS_LOCAL).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

RUNTIME = Path(__file__).resolve().parents[1]
HOST_TOOLS = ("sh", "bash", "git", "env", "cat", "ls", "mkdir", "rm", "cp", "mv", "sleep", "true", "false", "test",
              "echo", "sed", "grep", "head", "tail", "date", "uname", "tr", "cut", "wc", "sort", "dirname",
              "basename", "touch", "chmod", "find", "xargs", "mktemp", "tee", "readlink", "git-upload-pack",
              "git-receive-pack")
SCRIPT = """import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adw_modules.{module} import {name}
raise SystemExit({call})
"""
STEPS = [{"name": "plan", "kind": "run", "command": "plan"},
         {"name": "build", "kind": "run", "command": "build"},
         {"name": "test", "kind": "check", "cmd": "test", "resolve": "", "retries": 0},
         {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout.strip()


def demo_ir(steps: list[dict[str, Any]] | None = None, **placement: Any) -> dict[str, Any]:
    """A layer whose `demo` workflow is plan → build → check → review → ship with an auto review
    that merges, the mock agent, and a placement on home, build-1 and build-2."""
    return {
        "name": "test-layer",
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": name, "template": name, "modelSet": "base", "output": output}
                     for name, output in (("plan", "path"), ("build", "text"), ("commit", "text"), ("review", "json"))],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 20}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore", "bug"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": ["test"], "ship": "merge"}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 26400,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "layer": {"id": "sibling", "kind": "sibling", "status": "stable", "dir": "", "git": True},
        "concurrency": {"id": "slots", "kind": "slots", "status": "stable", "maxRuns": 4, "maxAgents": 2,
                        "maxChecks": 2, "waitSeconds": 60},
        "hosts": [{"id": "build-1", "images": "build"}, {"id": "build-2", "images": "build"}],
        "registry": None,
        "placement": {"id": "auto", "kind": "auto", "status": "stable", "hosts": ["home", "build-1", "build-2"],
                      "prefer": "least-loaded", "maxRunsPerHost": 0, "reserve": None, "sourceEnv": "AL_GIT_SOURCE",
                      "layerSourceEnv": "AL_LAYER_SOURCE", "lostAfter": 120, **placement},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["worktree"],
                  "classes": ["chore", "bug"], "pinnedHome": [], "steps": steps or STEPS}],
    }


def script(layer: Path, name: str, data: dict[str, Any], adw: str = "demo") -> None:
    path = layer / "mock" / adw / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def build_layer_repo(tmp_path: Path, make_layer: Any, app_repo: Path, ir_overrides: dict[str, Any] | None = None) -> Path:
    """A layer directory that is its own git repository: the runtime (adws/adw_modules), the
    workflow's script, adws/host.py and the mock scripts, committed."""
    del tmp_path, app_repo
    layer = make_layer(ir_overrides if ir_overrides is not None else demo_ir())
    adws = layer / "adws"
    adws.mkdir(exist_ok=True)
    shutil.copytree(RUNTIME / "adw_modules", adws / "adw_modules", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (adws / "adw_demo.py").write_text(SCRIPT.format(module="runner", name="main",
                                                    call='main("demo", Path(__file__).resolve().parents[1])'))
    (adws / "host.py").write_text(SCRIPT.format(module="selfrun", name="tool_main",
                                                call="tool_main(sys.argv[1:], Path(__file__).resolve().parents[1])"))
    if not (layer / "mock" / "demo" / "build.json").exists():
        script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    (layer / ".gitignore").write_text("agents/\ntrees/\nartefacts/\nspecs/\nlocal.env\n__pycache__/\n")
    if not (layer / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main", str(layer)], check=True, capture_output=True, timeout=30)
    commit_all(layer, "the layer")
    return layer


def sources(tmp_path: Path, app_repo: Path, layer: Path) -> tuple[Path, Path]:
    """Two bare repositories, app.git and layer.git, holding `main` of the application and the layer."""
    made = []
    for name, repo in (("app.git", app_repo), ("layer.git", layer)):
        bare = tmp_path / "sources" / name
        if not bare.exists():
            bare.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True, timeout=30)
        git(repo, "push", "-q", str(bare), "main:main")
        made.append(bare)
    return made[0], made[1]


def _shim(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def host_home(tmp_path: Path, host_id: str, layer: Path, login: bool = True, env: dict[str, str] | None = None) -> Path:
    """tmp_path/hosts/<host_id>: the home of a host with this layer deployed."""
    root = tmp_path / "hosts" / host_id
    hostbin = root / ".hostbin"
    hostbin.mkdir(parents=True)
    for tool in HOST_TOOLS:
        real = shutil.which(tool)
        if real:
            (hostbin / tool).symlink_to(real)
    _shim(hostbin / "python3", f'exec {sys.executable} "$@"\n')
    _shim(root / "bin" / "uv", '[ "$1" = run ] || { echo "uv shim: only uv run" >&2; exit 2; }\nshift\n'
                               '[ "$1" = --quiet ] && shift\n' f'exec {sys.executable} "$@"\n')
    name = json.loads((layer / "agentic-layer.json").read_text())["name"]
    subprocess.run(["git", "clone", "-q", str(tmp_path / "sources" / "layer.git"), str(root / "al" / layer.name)],
                   check=True, capture_output=True, timeout=60)
    values = {"AL_GIT_SOURCE": str(tmp_path / "sources" / "app.git"),
              "AL_LAYER_SOURCE": str(tmp_path / "sources" / "layer.git"), **(env or {})}
    config = root / ".config" / "al" / f"{name}.env"
    config.parent.mkdir(parents=True)
    config.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    config.chmod(0o600)
    if login:
        credentials = root / ".claude" / ".credentials.json"
        credentials.parent.mkdir(parents=True)
        credentials.write_text('{"stub": true}\n')
    return root


def vanish(root: Path) -> None:
    """Every command on the host fails from now on (its programs are gone)."""
    (root / ".hostbin").rename(root / ".hostbin.gone")
    (root / "bin").rename(root / "bin.gone")


def back(root: Path) -> None:
    (root / ".hostbin.gone").rename(root / ".hostbin")
    (root / "bin.gone").rename(root / "bin")


def local_hosts_env(tmp_path: Path, hosts: tuple[str, ...] = ("build-1", "build-2")) -> dict[str, str]:
    """The variables of the test process that make hostlink reach the directories above."""
    found = {"AL_HOSTS_LOCAL": str(tmp_path / "hosts")}
    for host_id in hosts:
        found["AL_HOST_" + host_id.upper().replace("-", "_") + "_LAYER"] = "al/layer"
    return found


def home_env(tmp_path: Path) -> dict[str, str]:
    """The git sources as the home machine (the test process) reaches them."""
    return {"AL_GIT_SOURCE": str(tmp_path / "sources" / "app.git"),
            "AL_LAYER_SOURCE": str(tmp_path / "sources" / "layer.git")}


def host_storage(root: Path, name: str = "test-layer") -> Path:
    return root / ".local" / "state" / "al" / name


def host_run_layer(root: Path, run_id: str, name: str = "test-layer") -> Path:
    """The layer directory of a run in a host's storage (a layer that is its own repository)."""
    return host_storage(root, name) / "runs" / run_id / "layer"
