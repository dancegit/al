"""How al's own layer (agentic/agentic.rkt) delivers: every run lands on the stage `integration`, cut
from `main`; `main` is the final stage, which agents may not merge into and which changes only by a
promotion a person approves. The full suites are the checks of `main`, so the promotion runs them on the
integration head, and the testing rhythm runs them per batch. The committed agentic/agentic-layer.json is
the compiled definition, and CI runs on main, integration and pull requests into main, with the tools
suite on Python 3.12 and the newest. Run with:

    env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_own_delivery.py -q
"""
from __future__ import annotations

import json

import pytest

from conftest import ROOT, al

FULL_SUITES = {"test-compiler", "test-runtime", "test-dashboard", "test-tools", "test-integration", "test-docs"}


@pytest.fixture(scope="module")
def own_ir() -> dict:
    """The compiled IR of al's own layer; `al compile` prints it and writes nothing."""
    return json.loads(al("compile", ROOT / "agentic").stdout)


def _stage(ir: dict, name: str) -> dict:
    return next(s for s in ir["stages"] if s["name"] == name)


def test_own_layer_has_an_integration_stage_and_main_final(own_ir):
    assert [s["name"] for s in own_ir["stages"]] == ["integration", "main"]
    integration = _stage(own_ir, "integration")
    assert integration["branch"] == "integration"
    assert integration["from"] == "main"
    assert integration["into"] == ""
    assert sorted(integration["accepts"]) == ["bug", "chore", "feature", "patch"]
    assert integration["agentsMayMerge"] is True
    assert integration["entry"] == "merge"
    main = _stage(own_ir, "main")
    assert main["branch"] == own_ir["codebase"]["defaultBranch"] == "main"
    assert main["from"] == "" and main["into"] == ""
    assert main["accepts"] == []
    assert main["agentsMayMerge"] is False


def test_every_shipping_workflow_delivers_to_integration(own_ir):
    shipping = [a for a in own_ir["adws"] if any(s["kind"] == "ship" for s in a["steps"])]
    assert {a["name"] for a in shipping} == {"sdlc", "reviewed", "zte"}
    for adw in shipping:
        assert adw["deliverTo"] == "integration", adw["name"]


def test_promotion_integration_to_main_needs_a_person_and_the_full_suites(own_ir):
    assert len(own_ir["promotions"]) == 1
    promotion = own_ir["promotions"][0]
    assert promotion["from"] == "integration"
    assert promotion["to"] == "main"
    assert promotion["approval"] == "human"
    assert promotion["approvers"] == 1
    assert promotion["method"] == "merge"
    assert "checks" in promotion["requires"]
    assert promotion["version"] is None
    assert promotion["tag"] == ""
    assert promotion["after"] == []
    assert promotion["origin"] == "declared"
    integration, main = _stage(own_ir, "integration"), _stage(own_ir, "main")
    assert set(integration["checks"]) | set(main["checks"]) == FULL_SUITES
    # A stage's checks are added to every run delivering into it: integration has none of its own.
    assert integration["checks"] == []


def test_batches_prove_the_full_suites_on_integration(own_ir):
    testing = own_ir["testing"]
    assert testing["depth"] == "targeted"
    assert testing["perRun"] in ("inherit", "targeted")
    assert testing["onStage"] == "full"
    assert testing["batchEvery"] == 5
    assert testing["batchAfterHours"] == 24


def test_committed_layer_is_generated_from_the_definition(own_ir):
    committed = json.loads((ROOT / "agentic" / "agentic-layer.json").read_text())
    for key in ("stages", "promotions", "testing"):
        assert committed.get(key) == own_ir[key], key
    assert ({a["name"]: a.get("deliverTo") for a in committed["adws"]}
            == {a["name"]: a.get("deliverTo") for a in own_ir["adws"]})


def _ci() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def test_ci_runs_on_main_integration_and_pull_requests_into_main():
    workflow = _ci()
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert sorted(triggers["push"]["branches"]) == ["integration", "main"]
    assert triggers["pull_request"]["branches"] == ["main"]


def test_ci_runs_the_tools_suite_on_python_312_and_the_newest():
    tools = _ci()["jobs"]["tools"]
    strategy = tools["strategy"]
    pythons = [str(p) for p in strategy["matrix"]["python"]]
    assert "3.12" in pythons
    assert max(tuple(int(n) for n in p.split(".")) for p in pythons) >= (3, 15)
    assert strategy["fail-fast"] is False
    setup_uv = [s for s in tools["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert len(setup_uv) == 1
    assert setup_uv[0]["with"]["python-version"] == "${{ matrix.python }}"
    runs = [s["run"] for s in tools["steps"] if "run" in s]
    assert len(runs) == 1
    assert '-m "not podman and not browser and not slow"' in runs[0]
