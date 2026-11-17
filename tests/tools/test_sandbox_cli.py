"""`al sandbox LAYER build|doctor|shell|prune|status` as a user sees it. podman is a stub script on
PATH that answers from a JSON file (images it has built, what the doctor script prints inside the
container); git, the worktree and the layer's own runtime are real."""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from conftest import LAYER_TEMPLATE, al

STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    """A stand-in for podman: state in $PODMAN_STUB (JSON), every call appended to its "calls"."""
    import json, os, sys, time
    path = os.environ["PODMAN_STUB"]
    data = json.load(open(path))
    args = sys.argv[1:]
    data["calls"].append(args)
    images = data["images"]
    def save():
        json.dump(data, open(path, "w"))
    def find(ref):
        ref = ref.removeprefix("localhost/")
        return images.get(ref)
    code, out = 0, ""
    if args[:2] == ["image", "exists"]:
        code = 0 if find(args[2]) else 1
    elif args[:2] == ["image", "inspect"]:
        found = find(args[2])
        code, out = (0, json.dumps([found])) if found else (125, "[]")
    elif args[0] == "build":
        for line in data["build_lines"]:
            print(line, flush=True)
        ref = args[args.index("-t") + 1]
        labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
        images[ref] = {"Id": ("id" + ref.split(":")[1]).ljust(64, "0"), "Size": 312000000,
                       "Created": time.time(), "Labels": labels}
    elif args[0] == "tag":
        images[args[2]] = find(args[1])
    elif args[0] == "rmi":
        images.pop(args[1], None)
    elif args[0] == "images":
        rows = {}
        for ref, info in images.items():
            row = rows.setdefault(info["Id"], dict(info, Names=[]))
            row["Names"].append("localhost/" + ref)
        out = json.dumps(list(rows.values()))
    elif args[0] in ("ps", "pod") and "json" in args:
        out = "[]"
    elif args[0] == "run":
        out = "0123456789ab"
    elif args[0] == "exec":
        out = data["doctor_output"]
    save()
    sys.stdout.write(out)
    sys.exit(code)
''')

ENVIRONMENTS = "  (environment worktree)\n  (environment podman #:id box #:agent-inside #f)\n"


@pytest.fixture
def podman(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "podman").write_text(STUB)
    (bin_dir / "podman").chmod(0o755)
    state = tmp_path / "podman.json"
    state.write_text(json.dumps({"calls": [], "images": {}, "doctor_output": "",
                                 "build_lines": ["STEP 1/3: FROM docker.io/library/debian:stable-slim",
                                                 "STEP 2/3: ENV HOME=/tmp/home", "STEP 3/3: WORKDIR /work"]}))
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "PODMAN_STUB": str(state)}
    return {"env": env, "state": state}


def _stub(podman: dict) -> dict:
    return json.loads(podman["state"].read_text())


def _layer(app: Path) -> Path:
    layer = app.parent / "notes.al"
    layer.mkdir()
    source = LAYER_TEMPLATE.format(root="../notes", placement="(layer sibling)")
    source = source.replace("  (command plan ", ENVIRONMENTS + "  (command plan ", 1)
    source = source.replace('(adw change #:description "Plan, build, test; a person reviews"',
                            '(adw change #:description "Plan, build, test; a person reviews" #:environment (worktree box)')
    (layer / "agentic.rkt").write_text(source)
    al("gen", layer)
    return layer


def test_sandbox_help_lists_the_five_actions() -> None:
    text = al("sandbox", "--help").stdout
    for action in ("build", "doctor", "shell", "prune", "status"):
        assert action in text
    assert "sandbox" in al("--help").stdout


def test_sandbox_status_shows_the_recipe_hash_and_not_built(app, podman) -> None:
    layer = _layer(app)
    cp = al("sandbox", layer, "status", env=podman["env"])
    line = cp.stdout.splitlines()[0]
    assert line.startswith("box  recipe ") and "not built" in line and "al-notes:" in line, cp.stdout
    recipe = line.split()[2]
    assert len(recipe) == 12 and f"al-notes:{recipe}" in line


def test_sandbox_build_streams_the_build_output(app, podman) -> None:
    layer = _layer(app)
    cp = al("sandbox", layer, "build", env=podman["env"])
    lines = cp.stdout.splitlines()
    built = next(i for i, line in enumerate(lines) if line.startswith("built al-notes:"))
    assert [line for line in lines[:built] if line.startswith("STEP")] == [
        "STEP 1/3: FROM docker.io/library/debian:stable-slim", "STEP 2/3: ENV HOME=/tmp/home", "STEP 3/3: WORKDIR /work"]
    assert lines[built].endswith("312 MB")
    stub = _stub(podman)
    (build,) = [call for call in stub["calls"] if call[0] == "build"]
    assert f"al.layer-dir={layer.resolve()}" in build and "al.layer=notes" in build
    assert "al-notes:latest" in stub["images"]
    cp = al("sandbox", layer, "build", env=podman["env"])
    assert "is current (recipe" in cp.stdout and len([c for c in _stub(podman)["calls"] if c[0] == "build"]) == 1
    assert "built" in al("sandbox", layer, "status", env=podman["env"]).stdout.splitlines()[0]


def test_sandbox_doctor_exits_nonzero_and_names_the_command(app, podman) -> None:
    layer = _layer(app)
    data = _stub(podman)
    data["doctor_output"] = ("AL-DOCTOR\tcommand:test\tmiss\t./test.sh is not an executable file\n"
                             "AL-DOCTOR\tgit\tok\trev-parse and status work in the worktree\n"
                             "AL-DOCTOR\tinit\tok\tcatatonit\nAL-DOCTOR\tdisk\tok\t90000 MB free\n")
    podman["state"].write_text(json.dumps(data))
    cp = al("sandbox", layer, "doctor", env=podman["env"], ok=False)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    table = cp.stdout.splitlines()
    header = next(i for i, line in enumerate(table) if line.startswith("check "))
    assert table[header + 1].startswith("command:test | miss")
    assert "(environment podman #:id box" in table[header + 1] and "(cmd test" in table[header + 1]
    assert "doctor: " in cp.stdout and "missed" in cp.stdout.splitlines()[-1]
    calls = _stub(podman)["calls"]
    assert any(call[:2] == ["run", "-d"] and "--init" in call for call in calls)        # started as a run starts it
    assert any(call[0] == "rm" for call in calls)                                       # and removed again
    assert not list((layer / "trees").iterdir())                                        # no worktree left behind


def test_sandbox_on_a_layer_generated_before_it_says_run_al_gen(app, podman) -> None:
    layer = _layer(app)
    (layer / "adws" / "adw_modules" / "sandbox.py").unlink()
    cp = al("sandbox", layer, "status", env=podman["env"], ok=False)
    assert cp.returncode == 1 and "generated before `al sandbox`: run `al gen" in cp.stderr


def test_sandbox_unknown_env_or_workflow_exits_2(app, podman, in_repo) -> None:
    cp = al("sandbox", in_repo, "status", env=podman["env"], ok=False)
    assert cp.returncode == 2 and "declares no (environment podman" in cp.stderr
    layer = _layer(app)
    cp = al("sandbox", layer, "doctor", "--env", "nope", env=podman["env"], ok=False)
    assert cp.returncode == 2 and "podman environments: box" in cp.stderr
    cp = al("sandbox", layer, "doctor", "--workflow", "nope", env=podman["env"], ok=False)
    assert cp.returncode == 2 and "workflows: change" in cp.stderr


def test_push_and_pull_are_actions_of_al_sandbox(app, podman) -> None:
    layer = _layer(app)
    text = al("sandbox", layer, "--help").stdout
    for action in ("build", "doctor", "shell", "prune", "status", "push", "pull"):
        assert action in text
    cp = al("sandbox", "push", layer, ok=False)
    assert cp.returncode == 2, cp.stdout + cp.stderr
    cp = al("sandbox", layer, "push", "--dry-run", env=podman["env"], ok=False)
    assert cp.returncode != 0 and "no (registry" in cp.stderr, cp.stdout + cp.stderr
