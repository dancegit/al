"""Every testbed: LLM.txt + agentic.rkt -> IR -> generated layer -> a mock-agent run."""
from __future__ import annotations

import json
import re
import shutil

import jsonschema
import pytest

import alit

PROMPT = "Add a note to the README about running the tests"


@pytest.mark.parametrize("bed", alit.BEDS)
def test_pipeline(layer, bed):
    lay = layer(bed)

    compiled = json.loads(lay.al("compile").stdout)
    schema = json.loads((alit.ROOT / "schemas" / "agentic-layer-v1.schema.json").read_text())
    errors = [f"{'/'.join(map(str, e.path))}: {e.message}"
              for e in jsonschema.Draft202012Validator(schema).iter_errors(compiled)]
    assert not errors, "IR does not validate against the schema:\n" + "\n".join(errors[:10])
    assert compiled == lay.ir, "agentic-layer.json written by gen differs from what compile prints"
    assert compiled["name"] == lay.name and compiled["codebase"]["root"] == str(lay.app)

    assert "generated" in lay.al("gen").stdout  # gen is repeatable on an existing layer
    check = lay.al("check", check=False)
    assert check.returncode == 0 and "check: ok" in check.stdout, alit.describe(check)

    files = sorted((lay.dir / "commands").glob("*.md")) + sorted((lay.dir / "hops").glob("*.md"))
    # every declared command and hop, plus the prompt of the kit workflow `integrate`, which every layer
    # gets without declaring it (commands/resolve-conflicts.md, see adw_modules/integrate.py)
    assert {f"commands/{c['name']}.md" for c in compiled["commands"]} | {f"hops/{h['name']}.md" for h in compiled["hops"]} \
        | {"commands/resolve-conflicts.md"} == {f"{p.parent.name}/{p.name}" for p in files}
    assert (lay.dir / "adws" / "integrate.py").is_file()
    for path in files:
        left = alit.LEFTOVER.findall(path.read_text())
        assert not left, f"{path.relative_to(lay.dir)} has unfilled placeholders {sorted(set(left))}"

    assert lay.dir not in lay.app.parents and lay.app not in lay.dir.parents and lay.app != lay.dir


@pytest.mark.parametrize("bed", alit.BEDS)
def test_plan_build_dry_run(layer, bed):
    lay = layer(bed)
    status_before = alit.git(lay.app, "status", "--porcelain", "--ignored")

    run = lay.run_adw("plan-build", PROMPT, "--dry-run")
    assert run.returncode == 0, alit.describe(run)

    state = lay.only_state()
    assert state["status"] == "passed" and state["dry_run"] is True and state["adw"] == "plan-build"
    assert {s["status"] for s in state["steps"]} <= {"passed", "skipped"}, state["steps"]
    assert state["steps"][-1]["status"] == "passed" and state["steps"][-1]["kind"] == "run"
    plans = sorted((lay.dir / "specs").glob("plan-*.md"))
    assert plans, f"no plan under {lay.dir / 'specs'}"
    assert state["plan"] == str(plans[0]) and PROMPT in plans[0].read_text()

    # nothing was written into the testbed (ignored files included), and the run cleaned up after itself
    assert alit.git(lay.app, "status", "--porcelain", "--ignored") == status_before
    assert lay.trees() == [], "a passed run must remove its worktree"
    assert alit.worktrees_under(lay.app, lay.dir) == []


@pytest.mark.parametrize("bed", alit.BEDS)
def test_golden_commands(layer, bed, update_golden):
    lay = layer(bed)
    actual = alit.rendered(lay)
    assert actual, "the layer rendered no commands"
    golden = alit.GOLDEN / bed
    if update_golden:
        shutil.rmtree(golden, ignore_errors=True)
        for rel, text in actual.items():
            (golden / rel).parent.mkdir(parents=True, exist_ok=True)
            (golden / rel).write_text(text)
    assert golden.is_dir(), f"no snapshots for {bed}: run pytest with --update-golden"
    expected = {p.relative_to(golden).as_posix(): p.read_text() for p in sorted(golden.rglob("*.md"))}
    assert sorted(actual) == sorted(expected), "the set of rendered commands/hops changed (--update-golden to accept)"
    for rel in actual:
        assert actual[rel] == expected[rel], f"{bed}/{rel} differs from its snapshot (--update-golden to accept)"


GOLDEN_ENV = alit.HERE / "golden-env"


def ir_and_env(lay: alit.Layer) -> dict[str, str]:
    """agentic-layer.json and every file under env/, with the paths and the name of this build replaced."""
    files = [lay.dir / "agentic-layer.json"] + sorted(p for p in (lay.dir / "env").rglob("*") if p.is_file())
    upper = re.sub(r"[^A-Z0-9]", "_", lay.name.upper())   # the name as in AL_<NAME>_LAYER
    return {p.relative_to(lay.dir).as_posix(): forge_repo(alit.normalise(p.read_text(), lay).replace("<TESTBED>", "<APP>")
            .replace(upper, "<NAME_VAR>")) for p in files}


def forge_repo(text: str) -> str:
    """The forge `repo` (owner/name) replaced, so no user name lands in a snapshot."""
    return re.sub(r'("repo": )"[^"]*"', r'\1"<REPO>"', text)


@pytest.mark.parametrize("bed", alit.BEDS)
def test_golden_ir_and_env(layer, bed, update_golden):
    """A layer without a toolchain generates no toolchain file and no `toolchains` key."""
    lay = layer(bed)
    actual = ir_and_env(lay)
    golden = GOLDEN_ENV / bed
    if update_golden:
        shutil.rmtree(golden, ignore_errors=True)
        for rel, text in actual.items():
            (golden / rel).parent.mkdir(parents=True, exist_ok=True)
            (golden / rel).write_text(text)
    assert golden.is_dir(), f"no snapshots for {bed}: run pytest with --update-golden"
    expected = {p.relative_to(golden).as_posix(): forge_repo(p.read_text()) for p in sorted(golden.rglob("*")) if p.is_file()}
    assert sorted(actual) == sorted(expected), "the set of IR and env/ files changed (--update-golden to accept)"
    for rel in actual:
        assert actual[rel] == expected[rel], f"{bed}/{rel} differs from its snapshot (--update-golden to accept)"
