"""al's own layer (agentic/, in-repo, committed with the repository): its declared stand-in, what
`al explain` says about it, and that the committed generated layer equals its definition."""
from __future__ import annotations

import json

from conftest import ROOT, SANDBOX_GIT, al

LAYER = ROOT / "agentic"


def assert_declared_stand_in(s: dict) -> None:
    assert s["kind"] == "auto"
    assert s["may"] == ["resume", "restart", "answer", "integrate", "settle", "retry-job"]
    assert "land" not in s["may"] and "promote" not in s["may"] and s["never"] == []
    assert s["maxActionsPerRun"] == 2 and s["maxPerHour"] == 12
    assert s["escalateTo"] == ["dashboard"]
    assert s["modelSet"] == "heavy" and s["systemd"] is False


def test_own_layer_declares_its_stand_in() -> None:
    ir = json.loads(al("compile", LAYER).stdout)
    assert_declared_stand_in(ir["standIn"])
    assert_declared_stand_in(json.loads((LAYER / "agentic-layer.json").read_text())["standIn"])


def test_al_explain_of_own_layer_says_what_its_stand_in_may_do_alone() -> None:
    out = al("explain", LAYER).stdout
    flat = " ".join(out.split())
    assert "Who stands in for a person" in out
    assert ("A stand-in (auto) reads what a person would read when a run needs one. Alone it may: "
            "resume, restart, answer, integrate, settle and retry-job. It only proposes: land and "
            "promote. It never touches: nothing. At most 2 actions per run and 12 per hour; after "
            "that, and whenever it is unsure, the item goes to a person (the dashboard's Needs you "
            "list).") in flat


def test_committed_own_layer_equals_its_definition() -> None:
    compiled = al("compile", LAYER).stdout
    assert compiled == (LAYER / "agentic-layer.json").read_text(), \
        "agentic/agentic-layer.json is stale; regenerate it: tools/al gen agentic"
    # `al check` reads git status of this repository itself: inside a run its objects are in the
    # run's own store (SANDBOX_GIT); outside a run SANDBOX_GIT is empty.
    cp = al("check", LAYER, ok=False, env=SANDBOX_GIT)
    assert cp.returncode == 0 and cp.stdout.strip().endswith("check: ok"), cp.stdout


def test_own_layer_requires_the_podman_suites_on_the_host() -> None:
    """al's zte runs the podman suites on this machine for every runtime change, forbids their
    "podman is not installed" skip, and its zero-touch gate cannot pass without them."""
    ir = json.loads(al("compile", LAYER).stdout)
    cmd = next(c for c in ir["codebase"]["commands"] if c["name"] == "test-podman")
    for suite in ("tests/test_sandbox_podman.py", "tests/test_isolation_podman.py", "tests/test_nested_engine.py"):
        assert suite in cmd["run"]
    assert "-rs" in cmd["run"].split()
    assert "kit/runtime" in cmd["covers"]
    zte = next(a for a in ir["adws"] if a["name"] == "zte")
    check = next(s for s in zte["steps"] if s["kind"] == "check" and s.get("cmd") == "test-podman")
    environment = next(e for e in ir["environments"] if e["id"] == check["in"])
    assert environment["kind"] == "nix-devshell"
    assert check["forbidSkipReason"] == "podman is not installed"
    zero_touch = next(r for r in ir["reviews"] if r["id"] == "zero-touch")
    assert "test-podman" in zero_touch["requireChecks"]
