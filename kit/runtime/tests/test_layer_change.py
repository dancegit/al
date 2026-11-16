"""An agentic layer checked in with the application (`layer in-repo`) and layer-change workflows:
implicit protection of the layer directory, the AL_APP_ROOT override, dry runs that never touch a
forge, the #:body-cmd lead of a pull request, and the sandbox mounts of a layer inside the app."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import envs, ir as ir_module, review, ship
from adw_modules.errors import LayerError
from adw_modules.ir import load
from adw_modules.runner import main
from adw_modules.state import ADWState

ADW_ID = "1a1a0001"
GITIGNORE = "/trees/\n/agents/\n/artefacts/\n/specs/\n/.al-improve/\n__pycache__/\n"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commands() -> list[dict]:
    return [{"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
            {"name": "build", "template": "build", "modelSet": "base", "output": "text"},
            {"name": "review", "template": "review", "modelSet": "base", "output": "json"}]


def _review(kind: str = "auto", **extra) -> dict:
    return {"id": "policy", "kind": kind, "status": "stable", "command": "review", "reviewers": 1, "maxRetries": 0,
            "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True, "maxDiffLines": 100,
            "requireChecks": ["test"], "ship": "merge", "bodyCmd": "", **extra}


def _steps() -> list[dict]:
    return [{"name": "plan", "kind": "run", "command": "plan"}, {"name": "build", "kind": "run", "command": "build"},
            {"name": "test", "kind": "check", "cmd": "test"}, {"name": "review", "kind": "review"},
            {"name": "ship", "kind": "ship"}]


@pytest.fixture
def in_repo(make_layer, app_repo):
    """factory(overrides) -> <app>/agentic: an in-repo layer, committed with the app."""
    def factory(overrides: dict | None = None, changes: str = "codebase") -> Path:
        base = {"agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
                "commands": _commands(),
                "codebase": {"root": "..", "commands": [{"name": "test", "run": "./test.sh", "timeout": 10}]},
                "reviews": [_review()],
                "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                                  "portBase": 27000, "portsPerRun": 0, "keep": "never", "status": "stable"}],
                "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
                "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["worktree"],
                          "classes": ["chore"], "changes": changes, "steps": _steps()}]}
        for key, value in (overrides or {}).items():
            base[key] = value
        built = make_layer(base)
        layer = app_repo / "agentic"
        shutil.copytree(built, layer)
        shutil.rmtree(built)
        data = json.loads((layer / "agentic-layer.json").read_text())
        data["codebase"]["root"] = ".."
        data["layer"] = {"id": "in-repo", "kind": "in-repo", "dir": "agentic", "status": "stable"}
        (layer / "agentic-layer.json").write_text(json.dumps(data, indent=2))
        (layer / ".gitignore").write_text(GITIGNORE)
        _git(app_repo, "add", "agentic")
        _git(app_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "the layer, checked in")
        return layer
    return factory


def _script(layer: Path, step: str, data: dict) -> None:
    """A scripted mock step. In an in-repo layer mock scripts are source: they are committed."""
    path = layer / "mock" / "demo" / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    app = layer.parent
    if (app / ".git").is_dir() and layer.name == "agentic":
        _git(app, "add", "agentic/mock")
        _git(app, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"mock {step}")


# ---------------------------------------------------------------- AL_APP_ROOT

def test_ir_load_honours_the_app_root_override(make_layer, app_repo, tmp_path, monkeypatch) -> None:
    layer = make_layer({"codebase": {"root": "../nowhere"}})
    assert load(layer).app_root == (layer / "../nowhere").resolve()
    monkeypatch.setenv(ir_module.APP_ROOT_ENV, str(app_repo))
    assert load(layer).app_root == app_repo.resolve()
    monkeypatch.setenv(ir_module.APP_ROOT_ENV, str(tmp_path / "missing"))
    with pytest.raises(LayerError, match="AL_APP_ROOT=.* is not a directory"):
        load(layer)


# ---------------------------------------------------------------- implicit protection: the decision

def _facts(**extra) -> dict:
    return {"check_exits": {"test": 0}, "require_exits": {"test": 0}, "verdict": {"success": True, "issues": []},
            "klass": "chore", "diff_lines": 3, "changed_files": ["app.py"], "protected": [], **extra}


def test_the_in_repo_layer_is_protected_whatever_the_review_option_says() -> None:
    touching = _facts(changed_files=["app.py", "agentic/commands/plan.md"], layer_protected=["agentic"])
    for option in (_review("auto"), _review("auto", blockProtectedPaths=False)):
        decision = review.decide(option, touching)
        assert decision["action"] == "pr" and not decision["ship"]
        assert "protected-path:agentic/commands/plan.md" in decision["reasons"]
        assert review.LAYER_CHANGED in decision["reasons"]
    for kind in ("human", "agent"):
        reasons = review.decide(_review(kind), touching)["reasons"]
        assert reasons[0] == f"review-{kind}: a human merges" and "protected-path:agentic/commands/plan.md" in reasons
    untouched = review.decide(_review("auto"), _facts(layer_protected=["agentic"]))
    assert untouched == {"action": "merge", "ship": True, "reasons": []}
    near_miss = review.decide(_review("auto"), _facts(changed_files=["agentic-notes.md"], layer_protected=["agentic"]))
    assert near_miss["ship"]                                      # a sibling name is not the layer


def test_a_layer_change_workflow_is_exempt_but_never_ships_without_a_person() -> None:
    facts = _facts(changed_files=["agentic/agentic.rkt"], layer_protected=[], changes="layer")
    decision = review.decide(_review("auto", ship="merge"), facts)
    assert decision["action"] == "pr" and review.LAYER_CHANGE_NEEDS_A_PERSON in decision["reasons"]
    assert not any(r.startswith("protected-path:") for r in decision["reasons"])
    assert review.decide(_review("human"), facts)["reasons"] == ["review-human: a human merges"]


# ---------------------------------------------------------------- implicit protection: a full run

def test_ordinary_run_that_changes_the_layer_falls_back_to_a_person(in_repo, app_repo) -> None:
    layer = in_repo()
    _script(layer, "build", {"files": {"app.py": "print('changed')\n", "agentic/notes.md": "the agent wrote this\n"}})
    main_before = _git(app_repo, "rev-parse", "refs/heads/main")
    assert main("demo", layer, ["Tidy up", "--adw-id", ADW_ID]) == 0
    state = json.loads((layer / "agents" / ADW_ID / "state.json").read_text())
    assert state["ship"]["action"] == "pr" and state["ship"]["shipped"] is False
    assert "protected-path:agentic/notes.md" in state["ship"]["reasons"]
    assert review.LAYER_CHANGED in state["ship"]["reasons"]
    assert _git(app_repo, "rev-parse", "refs/heads/main") == main_before          # nothing merged
    assert "protected-path:agentic/notes.md" in (layer / "agents" / ADW_ID / "PR.md").read_text()
    assert _git(app_repo, "status", "--porcelain") == ""


def test_ordinary_run_that_leaves_the_layer_alone_still_ships(in_repo, app_repo) -> None:
    layer = in_repo()
    _script(layer, "build", {"files": {"app.py": "print('changed')\n"}})
    assert main("demo", layer, ["Tidy up", "--adw-id", ADW_ID]) == 0
    state = json.loads((layer / "agents" / ADW_ID / "state.json").read_text())
    assert state["ship"]["action"] == "merge" and state["ship"]["reasons"] == [], state["ship"]
    # main is checked out in the app, so the result waits on al/ship/main: never in the working tree
    assert state["ship"]["target"] == "al/ship/main"
    assert _git(app_repo, "status", "--porcelain") == ""


def test_in_repo_run_works_in_an_ignored_worktree_and_uses_the_main_layer(in_repo, app_repo) -> None:
    layer = in_repo()
    _script(layer, "build", {"files": {"app.py": "print('changed')\n"}, "output": "built in ${worktree}, layer ${layer_dir}"})
    data = json.loads((layer / "agentic-layer.json").read_text())
    data["environments"][0]["keep"] = "always"
    (layer / "agentic-layer.json").write_text(json.dumps(data))
    _git(app_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "keep worktrees")
    assert main("demo", layer, ["Tidy up", "--adw-id", ADW_ID]) == 0
    state = json.loads((layer / "agents" / ADW_ID / "state.json").read_text())
    worktree = Path(state["worktree"])
    assert worktree == (layer / "trees" / ADW_ID).resolve()                       # inside the app's working tree …
    assert (worktree / "agentic" / "agentic-layer.json").is_file()               # … with its committed copy of the layer
    assert f"layer {layer.resolve()}" in state["outputs"]["build"]                # but the run used the MAIN layer
    assert str(layer.resolve()) in state["plan"]
    assert _git(app_repo, "status", "--porcelain") == ""                          # ignored: git status stays clean
    assert str(worktree) in _git(app_repo, "worktree", "list")


# ---------------------------------------------------------------- dry runs never use the forge

def test_a_dry_run_never_opens_a_pull_request_on_the_forge(make_layer, app_repo, monkeypatch) -> None:
    layer = make_layer({
        "commands": _commands(), "reviews": [_review("human")],
        "agents": [{"id": "claude", "kind": "claude-code", "status": "stable", "timeout": 30}],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 10}]},
        "forge": {"id": "forgejo", "kind": "forgejo", "status": "stable", "host": "http://127.0.0.1:9",
                  "repo": "o/r", "tokenEnv": "NOPE", "botMarker": "[ADW-BOT]"},
        "adws": [{"name": "demo", "agent": "claude", "review": "policy", "environment": ["worktree"],
                  "classes": ["chore"], "steps": _steps()}]})
    monkeypatch.setattr(ship, "_forge_client", lambda layer: SimpleNamespace(
        create_pr=lambda *a, **k: pytest.fail("a dry run must not reach the forge")))
    assert main("demo", layer, ["Tidy up", "--adw-id", ADW_ID, "--dry-run"]) == 0
    state = json.loads((layer / "agents" / ADW_ID / "state.json").read_text())
    assert state["ship"]["target"] == f"agents/{ADW_ID}/PR.md" and state["ship"]["pr"] is None


# ---------------------------------------------------------------- #:body-cmd

def test_body_cmd_opens_the_pull_request_description(make_layer, app_repo) -> None:
    layer = make_layer({
        "commands": _commands(), "reviews": [_review("human", bodyCmd="explain")],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 10},
                                  {"name": "explain", "timeout": 10, "run":
                                   "printf '## For the reviewer\\n\\nrequest: %s base: %s\\n' "
                                   "\"$(python3 -c 'import json,os; print(json.load(open(os.environ[\"ADW_STATE\"]))[\"prompt\"])')\" "
                                   "\"$ADW_BASE\""}]},
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["worktree"],
                  "classes": ["chore"], "steps": _steps()}]})
    base = _git(app_repo, "rev-parse", "HEAD")
    assert main("demo", layer, ["Explain me", "--adw-id", ADW_ID]) == 0
    body = (layer / "agents" / ADW_ID / "PR.md").read_text()
    assert body.index("## For the reviewer") < body.index("## Run record") < body.index("## Checks")
    assert f"request: Explain me base: {base}" in body
    state = json.loads((layer / "agents" / ADW_ID / "state.json").read_text())
    assert state["ship"]["body_cmd"]["exit"] == 0


def test_a_failing_body_cmd_leaves_the_standard_description(make_layer, app_repo) -> None:
    layer = make_layer({
        "commands": _commands(), "reviews": [_review("human", bodyCmd="explain")],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 10},
                                  {"name": "explain", "run": "echo partial; exit 3", "timeout": 10}]},
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["worktree"],
                  "classes": ["chore"], "steps": _steps()}]})
    assert main("demo", layer, ["Explain me", "--adw-id", ADW_ID]) == 0
    body = (layer / "agents" / ADW_ID / "PR.md").read_text()
    assert "partial" not in body and body.startswith("# chore: Explain me\n\n## Summary")
    state = json.loads((layer / "agents" / ADW_ID / "state.json").read_text())
    assert state["ship"]["body_cmd"]["exit"] == 3


def test_a_layer_change_pull_request_says_so_in_its_title(in_repo, app_repo) -> None:
    layer = in_repo({"reviews": [_review("human")]}, changes="layer")
    _script(layer, "build", {"files": {"agentic/agentic.rkt": "#lang al\n"}})
    assert main("demo", layer, ["Every plan ends with a rollback section", "--adw-id", ADW_ID]) == 0
    body = (layer / "agents" / ADW_ID / "PR.md").read_text()
    assert body.startswith("# agentic layer: Every plan ends with a rollback section")
    assert "protected-path" not in body                           # exempt: this workflow changes the layer


# ---------------------------------------------------------------- sandbox mounts of a layer inside the app

def test_in_repo_sandbox_mounts_the_layer_entry_by_entry_and_nothing_else_of_the_app(in_repo, app_repo, monkeypatch) -> None:
    layer = in_repo({"environments": [
        {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27100, "portsPerRun": 0,
         "keep": "never", "status": "stable"},
        {"id": "box", "kind": "podman", "agentInside": True, "baseImage": "debian", "cpus": "1", "memory": "1g",
         "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
         "credentials": []}]})
    (layer / ".al-improve" / "trees").mkdir(parents=True)        # another workflow's run state
    calls: list[list[str]] = []
    real_run = envs.subprocess.run
    monkeypatch.setattr(envs.subprocess, "run", lambda argv, **kw: real_run(argv, **kw) if argv[0] != "podman"
                        else (calls.append(list(argv)) or SimpleNamespace(returncode=0, stderr="", stdout="")))
    loaded = load(layer)
    state = ADWState(ADW_ID, "demo", prompt="Sandbox the in-repo layer", klass="chore")
    stack = envs.build(loaded, state, ["wt", "box"])
    stack.setup()
    stack.wrap("true")
    start = next(call for call in calls if call[:3] == ["podman", "run", "-d"])
    sources = [arg.split(":")[0] for arg in start if arg.startswith("/") and ":" in arg]
    app, lay = app_repo.resolve(), layer.resolve()
    assert str(app) not in sources                                 # the application working tree is never mounted
    assert not any(s.startswith(str(lay / ".al-improve")) for s in sources)      # nor al improve's run state
    assert not any(s == str(lay / "trees") or s == str(lay / "agents") for s in sources)  # nor other runs
    assert f"{lay / 'agentic-layer.json'}:{lay / 'agentic-layer.json'}:ro" in start
    assert f"{app / '.git'}:{app / '.git'}:ro" in start
    outside = [s for s in sources if not s.startswith(str(lay)) and not s.startswith(str(app / ".git"))]
    assert outside == [], outside                                  # only the layer and the app's .git
    assert Path(state.worktree).is_relative_to(lay / "trees")
    stack.teardown(success=True)
    assert _git(app_repo, "status", "--porcelain") == ""


@pytest.mark.podman
def test_in_repo_sandboxed_run_with_real_podman(in_repo, app_repo, monkeypatch) -> None:
    image = _real_image()
    monkeypatch.setattr(envs.EnvStack, "image", property(lambda self: image))
    layer = in_repo({"environments": [
        {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27200, "portsPerRun": 0,
         "keep": "never", "status": "stable"},
        {"id": "box", "kind": "podman", "agentInside": True, "baseImage": "debian", "cpus": "1", "memory": "1g",
         "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
         "credentials": []}]})
    (layer / ".al-improve").mkdir()
    (layer / ".al-improve" / "secret.txt").write_text("another workflow's state\n")
    data = json.loads((layer / "agentic-layer.json").read_text())
    probe = (f"test ! -e {app_repo.resolve()}/app.py && test ! -e {layer.resolve()}/.al-improve/secret.txt "
             f"&& test -r {layer.resolve()}/agentic-layer.json && ! touch {layer.resolve()}/agentic-layer.json "
             f"&& test -e agentic/agentic-layer.json && python3 -B -c 'import feature' && ./test.sh")
    data["codebase"]["commands"] = [{"name": "test", "run": probe, "timeout": 120}]
    data["adws"][0]["environment"] = ["wt", "box"]
    (layer / "agentic-layer.json").write_text(json.dumps(data))
    (layer / "env").mkdir(exist_ok=True)
    (layer / "env" / "Containerfile").write_text(f"FROM {image}\n")   # the preflight builds it: nothing pulled
    _git(app_repo, "add", "-A")
    _git(app_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "sandboxed layer")
    _script(layer, "build", {"files": {"feature.py": "FEATURE = 1\n"}})
    before = _git(app_repo, "rev-parse", "refs/heads/main")
    try:
        code = main("demo", layer, ["Add a feature", "--adw-id", "1a1a0002"])
        assert code == 0, (layer / "agents/1a1a0002/run.log").read_text()
        state = json.loads((layer / "agents/1a1a0002/state.json").read_text())
        assert state["checks"]["test"]["exit"] == 0
        assert state["ship"]["action"] == "merge" and state["ship"]["reasons"] == []
        assert _git(app_repo, "rev-parse", "refs/heads/main") == before          # main is checked out: al/ship/main
        assert _git(app_repo, "status", "--porcelain") == ""
    finally:
        subprocess.run(["podman", "rm", "-f", "al-test-layer-1a1a0002"], capture_output=True, timeout=120)


def _real_image() -> str:
    """The cached git-enabled image of test_sandbox_podman (built there from a local base image)."""
    import test_sandbox_podman as recipe
    if shutil.which("podman") is None or recipe._podman("info").returncode != 0:
        pytest.skip("podman is not usable here")
    if recipe._podman("image", "exists", recipe.TEST_IMAGE).returncode != 0:
        pytest.skip(f"{recipe.TEST_IMAGE} is not built yet (run test_sandbox_podman.py once)")
    return recipe.TEST_IMAGE
