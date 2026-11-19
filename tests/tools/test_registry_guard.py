"""The guard on what a generated layer can run: every executable script `al gen` writes under adws/
and every `(adw …)` must be in the runtime's registry (kit/runtime/adw_modules/registry.py), and
every workflow of the registry, declared or from the kit, must be in what the dashboard's two views
return. A kit workflow added to the generator without a row in the registry's KIT fails here, so it
cannot be left out of the dashboard silently (as `integrate` was).

Also here, because they read the same registry: `al run` naming the command that starts a kit
workflow, `al explain` listing the kit workflows, and `al gen` refusing a declared workflow that
takes a kit workflow's name. The layer is the full example (racket/al/examples/full.rkt), generated
into pytest's temporary directory with an invented application folder as its root.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from conftest import KIT, LAYER_TEMPLATE, ROOT, al, sh

pytestmark = pytest.mark.skipif(shutil.which("racket") is None, reason="racket is not on PATH")

FABRIC = ("create_prd", "review_code", "create_git_diff_commit", "write_pull-request")   # what full.rkt names
LIBRARIES = ("adw_modules", "dashboard")       # under adws/: libraries, not scripts a person or a trigger runs


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module          # dataclasses look their module up while it is executed
    spec.loader.exec_module(module)     # type: ignore[union-attr]
    return module


dashboard = _load("al_dashboard_for_guard", KIT / "dashboard" / "dashboard.py")
registry = _load("al_registry_for_guard", KIT / "runtime" / "adw_modules" / "registry.py")


def scripts_of(layer: Path) -> list[str]:
    """Every file with an executable bit under LAYER/adws/, outside its libraries, by relative path."""
    found = []
    for path in sorted((layer / "adws").rglob("*")):
        relative = path.relative_to(layer)
        if path.is_file() and os.access(path, os.X_OK) and relative.parts[1] not in LIBRARIES:
            found.append(relative.as_posix())
    return found


def guard(layer: Path) -> dict:
    """AssertionError naming what is missing; else the registry the dashboard built for the layer."""
    ir = json.loads((layer / "agentic-layer.json").read_text())
    model = dashboard.Model([layer], [])
    model.discover()
    (key, loaded), = model.layers.items()
    reg = dashboard.load_registry(loaded)
    assert reg["problems"] == [], reg["problems"]
    scripts = scripts_of(layer)
    assert scripts, f"{layer} has no generated script"
    for script in scripts:
        assert script in reg["scripts"], (f"{script} is generated but not in the registry: add a row to KIT in "
                                          "kit/runtime/adw_modules/registry.py (or its role to registry._scripts)")
    entries = {(e["name"], e["kind"]): e for e in reg["entries"]}
    for adw in ir["adws"]:
        assert (adw["name"], "workflow") in entries, f"(adw {adw['name']} …) has no entry in the registry"
        assert f"adws/adw_{adw['name']}.py" in reg["scripts"], f"adws/adw_{adw['name']}.py is not in the registry"
    catalog = dashboard.startables(loaded)
    detail = model.layer_detail(key)
    form = [w["name"] for w in catalog["workflows"]] + [k["name"] for k in catalog["kit"]]
    view = [a["name"] for a in detail["adws"]] + [k["name"] for k in detail["kit"]]
    for script, item in reg["scripts"].items():
        if item["role"] not in ("workflow", "kit-workflow"):
            continue
        assert item["entries"], f"{script} is a {item['role']} script that serves no entry of the registry"
        for name in item["entries"]:
            assert name in form, f"{name} ({script}) is not in what the start form gets (/api/startable)"
            assert name in view, f"{name} ({script}) is not in what the Layers view gets (/api/layer)"
    for entry in reg["entries"]:
        if entry["kind"] in ("workflow", "kit-workflow", "decider"):
            assert entry["name"] in form and entry["name"] in view, f"{entry['name']} is missing from a dashboard view"
    return reg


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The full example, generated: ROOT/full.al beside an invented application ROOT/webshop."""
    root = tmp_path_factory.mktemp("registry-guard")
    (root / "webshop").mkdir()
    for pattern in FABRIC:                       # the example names fabric patterns: invented ones
        (root / "fabric" / pattern).mkdir(parents=True)
        (root / "fabric" / pattern / "system.md").write_text(f"# {pattern}\n")
    layer = root / "full.al"
    layer.mkdir()
    shutil.copy(ROOT / "racket" / "al" / "examples" / "full.rkt", layer / "agentic.rkt")
    env = {"AL_FABRIC_DIR": str(root / "fabric")}
    al("gen", layer, env=env)
    return {"root": root, "layer": layer, "env": env}


@pytest.fixture()
def layer(generated: dict, tmp_path: Path) -> Path:
    """A private copy of the generated layer (tests add and remove files)."""
    copy = tmp_path / "site"
    shutil.copytree(generated["root"], copy, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    return copy / "full.al"


def test_full_example_layer_is_fully_registered(layer):
    reg = guard(layer)
    assert scripts_of(layer) == sorted(reg["scripts"]), "the registry names exactly the scripts al gen wrote"
    names = [e["name"] for e in reg["entries"] if e["source"] == "kit"]
    assert names == ["integrate", "assign", "promote-sprint-main", "promote-hotfix-main", "improve", "decider"]
    assert not any(e["off"] for e in reg["entries"] if e["kind"] in ("workflow", "kit-workflow")), "all scripts exist"
    # what the generated launcher prints is the same registry
    printed = sh([sys.executable, layer / "adws" / "dispatch.py", "--registry", "--json"])
    assert printed.returncode == 0, printed.stderr
    assert [e["name"] for e in json.loads(printed.stdout)["entries"]] == [e["name"] for e in reg["entries"]]


def test_guard_fails_for_an_unregistered_kit_script(layer, monkeypatch):
    guard(layer)
    script = layer / "adws" / "newkit.py"
    script.write_text("#!/usr/bin/env python3\n")
    script.chmod(0o755)
    with pytest.raises(AssertionError, match=r"adws/newkit\.py is generated but not in the registry"):
        guard(layer)
    script.chmod(0o644)                          # not executable: a data file, not a script
    guard(layer)
    script.unlink()
    # the same for a kit workflow the generator writes and the registry's table does not declare
    model = dashboard.Model([layer], [])
    model.discover()
    module = dashboard.registry_module(next(iter(model.layers.values())))
    assert module.__file__ == str(layer / "adws" / "adw_modules" / "registry.py"), "the layer's own copy is read"
    monkeypatch.setattr(module, "KIT", [row for row in module.KIT if row["name"] != "integrate"])
    with pytest.raises(AssertionError, match=r"adws/integrate\.py is generated but not in the registry"):
        guard(layer)


def test_improve_steps_equal_the_kit_s_own_layer():
    """registry.IMPROVE_STEPS repeats the (steps …) of kit/self/agentic.rkt.in, which `al improve` runs."""
    text = (KIT / "self" / "agentic.rkt.in").read_text()
    block = text[text.index("(steps"):]
    steps = [m.group(2) or m.group(3) for m in re.finditer(r"\((run|check|script)\s+([\w-]+)|\((review|ship)\)", block)]
    assert steps == list(registry.IMPROVE_STEPS)
    assert text.count("(adw ") == 1 and "(adw improve" in text


def test_al_run_names_the_kit_way(generated):
    layer, env = generated["layer"], generated["env"]
    before = sorted(p.name for p in (layer / "agents").iterdir()) if (layer / "agents").is_dir() else []
    cp = al("run", layer, "/integrate x", ok=False, env=env)
    assert cp.returncode == 2, cp.stdout + cp.stderr
    assert cp.stderr.strip() == "al run: integrate is a kit workflow that takes a handed-over run: al integrate LAYER RUN"
    cp = al("run", layer, "/promote-sprint-main", ok=False, env=env)
    assert cp.returncode == 2
    assert "promote-sprint-main is a kit workflow that takes two stages: al promote LAYER sprint main" in cp.stderr
    cp = al("run", layer, "/improve", "make it faster", ok=False, env=env)
    assert cp.returncode == 2 and 'al improve LAYER "…"' in cp.stderr
    cp = al("run", layer, "/nope", "x", ok=False, env=env)
    assert cp.returncode == 2 and "no workflow or command named nope; known: /plan-build, /sdlc, /zte, " in cp.stderr
    assert cp.stderr.strip().endswith(
        "/integrate (kit), /assign (kit), /promote-sprint-main (kit), /promote-hotfix-main (kit), /improve (kit), /decider (kit)")
    after = sorted(p.name for p in (layer / "agents").iterdir()) if (layer / "agents").is_dir() else []
    assert after == before, "nothing was started"


def test_explain_lists_kit_workflows(generated, tmp_path):
    out = tmp_path / "explained.md"
    al("explain", generated["layer"], "--md", out, env=generated["env"])
    text = out.read_text()
    paragraph = text.split("## In one paragraph", 1)[1].split("## Block by block", 1)[0]
    assert ("The kit adds 6 more a person or a run can start: `integrate`, `assign`, `promote-sprint-main`, "
            "`promote-hotfix-main`, `improve` and `decider`.") in paragraph
    assert "6 workflows (`plan-build`, `sdlc`, `zte`, `hotfix`, `candidate` and `race`)" in paragraph
    heading = "### Kit workflows (every layer gets them; not declared in the definition)"
    assert text.count(heading) == 1
    section = text.split(heading, 1)[1].split("\n### ", 1)[0]
    bullets = [line for line in section.splitlines() if line.startswith("- ")]
    assert [line.split("`")[1] for line in bullets] == ["integrate", "assign", "promote-sprint-main", "promote-hotfix-main",
                                                       "improve", "decider"]
    by_name = {line.split("`")[1]: line for line in bullets}
    assert "started with `al integrate LAYER RUN` or from the dashboard" in by_name["integrate"]
    assert "setting `templates/integrate.json` (`enabled`): on" in by_name["integrate"]
    assert "started with `al promote LAYER sprint main` or from the dashboard" in by_name["promote-sprint-main"]
    assert "setting `agentic.rkt` (`stages`): on" in by_name["promote-hotfix-main"]
    assert 'started with `al improve LAYER "…"`' in by_name["improve"] and "no setting" in by_name["improve"]
    assert "setting `agents/settings/decider.json` (`mode`): off" in by_name["decider"]
    assert "turn it on: al decider LAYER --set suggest|auto" in by_name["decider"]
    # the section on integrate stays, after the list
    assert "### Kit workflow: integrate (every layer has it)" in text
    assert text.index(heading) < text.index("### Kit workflow: integrate (every layer has it)")
    assert text.index("## Block by block") < text.index(heading)


def test_explain_says_promote_is_off_without_stages(app, tmp_path):
    layer = tmp_path / "notes.al"
    layer.mkdir()
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root=app, placement=""))
    out = tmp_path / "explained.md"
    al("explain", layer, "--md", out)
    text = out.read_text()
    assert "The kit adds 5 more a person or a run can start: `integrate`, `assign`, `promote`, `improve` and `decider`." in text
    line = next(line for line in text.splitlines() if line.startswith("- `promote`"))
    assert "setting `agentic.rkt` (`stages`): off; turn it on: declare stages, e.g. `(delivery sprint)`" in line


@pytest.mark.parametrize("name, how", [
    ("integrate", "every layer has `integrate`: al integrate LAYER RUN"),
])
def test_declared_workflow_may_not_take_a_kit_name(app, tmp_path, name, how):
    layer = tmp_path / "notes.al"
    layer.mkdir()
    text = LAYER_TEMPLATE.format(root=app, placement="").replace("(adw change ", f"(adw {name} ")
    assert f"(adw {name} " in text
    (layer / "agentic.rkt").write_text(text)
    for command in ("gen", "compile"):
        cp = al(command, layer, ok=False)
        assert cp.returncode != 0, command
        assert f"workflow `{name}` takes the name of a kit workflow ({how}); rename it" in cp.stderr, cp.stderr
    assert not (layer / "adws").exists(), "nothing was generated"
    # a name the kit does not reserve stays free (kit/self declares `improve` itself)
    (layer / "agentic.rkt").write_text(text.replace(f"(adw {name} ", "(adw improve "))
    al("gen", layer)
    assert (layer / "adws" / "adw_improve.py").is_file()
    reg = json.loads((layer / "agentic-layer.json").read_text())
    assert [e["source"] for e in registry.build(reg)["entries"] if e["name"] == "improve"] == ["declared"]
