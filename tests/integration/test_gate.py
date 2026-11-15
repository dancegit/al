"""Seeded defects against the zero-touch gate (flask, mock agent, offline podman sandbox).

The fixture copy keeps `#:ship merge` and the podman requirement of a zero-touch ADW, but its only
check is `fast` (every Python file under src/ parses) in a network-less container made from an
image that is already local. Each scenario is one mock `build` response plus the gate's decision.
"""
from __future__ import annotations

import pytest

import alit

pytestmark = pytest.mark.podman

FAST = "python3 -c \"import ast,sys,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('src').rglob('*.py')]\""
MAX_DIFF = 60
SHIP_REF = "al/ship/main"
GOOD = "\"\"\"Seeded by the al integration suite.\"\"\"\n\n\ndef answer() -> int:\n    return 42\n"


def _zte(form: str) -> str:
    lines = [line for line in form.splitlines() if not line.strip().startswith("(check ")]
    assert len(lines) == len(form.splitlines()) - 3, "the authored zte ADW no longer has three check steps"
    at = next(i for i, line in enumerate(lines) if line.strip().startswith("(compose plan-build)")) + 1
    lines.insert(at, "      (check fast #:cmd fast #:resolve resolve-test #:retries 1 #:collect (log))")
    form = "\n".join(lines)
    assert "(review #:patch plan-patch #:implement run-plan)" in form
    return form.replace("(review #:patch plan-patch #:implement run-plan)", "(review)")  # no patch loop


@pytest.fixture
def zte(layer):
    if alit.sh(["podman", "image", "exists", alit.PYTHON_IMAGE]).returncode != 0:
        pytest.skip(f"{alit.PYTHON_IMAGE} is not in local podman storage (podman pull it once to enable the gate tests)")
    lay = layer(
        "flask", *alit.mock_agent(),
        alit.add_cmd("fast", FAST),
        alit.replace_form("(environment podman", f'(environment podman #:base-image "{alit.PYTHON_IMAGE}" #:network none)'),
        alit.substitute("#:require-checks (lint typecheck test)", f"#:require-checks (fast) #:max-diff-lines {MAX_DIFF}"),
        alit.replace_form("(adw zte", _zte),
    )
    review = next(r for r in lay.ir["reviews"] if r["id"] == "zero-touch")
    assert (review["ship"], review["requireChecks"], review["maxDiffLines"]) == ("merge", ["fast"], MAX_DIFF)
    assert ".github" in lay.ir["codebase"]["paths"]["protected"]
    lay.ship_before = alit.heads(lay.app).get(SHIP_REF)
    return lay


def _run(lay, *args):
    run = lay.run_adw("zte", "Add an answer helper", *args, timeout=900)
    state = lay.only_state()
    return run, state


def _nothing_shipped(lay):
    assert alit.heads(lay.app).get(SHIP_REF) == lay.ship_before, f"{SHIP_REF} moved although nothing may ship"


def _falls_back_to_pr(lay, run, state, reason: str):
    assert run.returncode == 0, alit.describe(run)
    ship = state["ship"]
    assert state["status"] == "passed" and ship["action"] == "pr" and ship["shipped"] is False, ship
    assert any(r.startswith(reason) for r in ship["reasons"]), ship["reasons"]
    assert ship["gate"]["action"] == "pr" and ship["gate"]["reasons"] == ship["reasons"]
    assert (lay.dir / ship["target"]).is_file(), "without a forge the PR description is written under the layer"
    assert state["branch"] in alit.heads(lay.app), "the branch is kept for the human"
    _nothing_shipped(lay)


def test_clean_small_change_merges(zte):
    zte.mock("zte", "build", output="added src/flask/_al_it_answer.py", files={"src/flask/_al_it_answer.py": GOOD})
    run, state = _run(zte)
    assert run.returncode == 0, alit.describe(run)
    ship = state["ship"]
    assert state["status"] == "passed" and state["checks"]["fast"]["exit"] == 0
    assert ship["action"] == "merge" and ship["reasons"] == [] and ship["gate"]["action"] == "merge", ship
    assert ship["gate"]["changed_files"] == ["src/flask/_al_it_answer.py"]
    # flask's default branch is checked out in the testbed, so the result is staged, never checked out there
    assert ship["target"] == SHIP_REF and ship["shipped"] is False
    assert alit.heads(zte.app)[SHIP_REF] == ship["commit"] != zte.ship_before
    assert alit.git(zte.app, "show", f"{SHIP_REF}:src/flask/_al_it_answer.py") == GOOD.rstrip("\n")
    assert alit.git(zte.app, "rev-parse", "main") == alit.TESTBEDS["flask"]["commit"]


def test_protected_path_falls_back_to_pr(zte):
    zte.mock("zte", "build", output="added a workflow", files={".github/workflows/x.yml": "name: x\non: push\njobs: {}\n"})
    run, state = _run(zte)
    _falls_back_to_pr(zte, run, state, "protected-path")
    assert "protected-path:.github/workflows/x.yml" in state["ship"]["reasons"]


def test_large_diff_falls_back_to_pr(zte):
    body = "".join(f"VALUE_{n} = {n}\n" for n in range(MAX_DIFF * 3))
    zte.mock("zte", "build", output="added a big module", files={"src/flask/_al_it_big.py": body})
    run, state = _run(zte)
    _falls_back_to_pr(zte, run, state, "diff-too-large")
    assert f"diff-too-large:{MAX_DIFF * 3}>{MAX_DIFF}" in state["ship"]["reasons"]


def test_failing_check_fails_the_run(zte):
    zte.mock("zte", "build", output="added a broken module", files={"src/flask/_al_it_broken.py": "def broken(:\n"})
    run, state = _run(zte)  # no mock script for fast-resolve-1: the resolve agent changes nothing
    assert run.returncode == 1, alit.describe(run)
    assert state["status"] == "failed" and state["checks"]["fast"]["exit"] != 0
    steps = {s["name"]: s["status"] for s in state["steps"]}
    assert steps["fast"] == "failed" and "review" not in steps and "ship" not in steps, steps
    assert "fast-resolve-1" in state["outputs"], "the resolve loop ran once (#:retries 1)"
    # a stopped run hands its work to a human instead of dropping it: nothing ships, the reason
    # is recorded, and (no forge here) the handoff text is written to PR.md with the branch kept
    ship = state["ship"]
    assert ship["action"] == "handoff" and ship["shipped"] is False, ship
    assert any(reason.startswith("run-stopped:fast") for reason in ship["reasons"]), ship
    handoff = zte.dir / "agents" / state["adw_id"] / "PR.md"
    assert "Why this stopped" in handoff.read_text()
    assert "SyntaxError" in (zte.dir / state["checks"]["fast"]["log"]).read_text()
    _nothing_shipped(zte)


def test_review_blocker_falls_back_to_pr(zte):
    zte.mock("zte", "build", output="added src/flask/_al_it_answer.py", files={"src/flask/_al_it_answer.py": GOOD})
    verdict = {"success": False, "summary": "the helper has no test", "artefacts_reviewed": [],
               "issues": [{"severity": "blocker", "description": "answer() is not covered by any test",
                           "resolution": "add tests/test_al_it_answer.py"}]}
    import json
    zte.mock("zte", "review-1", output="```json\n" + json.dumps(verdict) + "\n```")
    run, state = _run(zte)
    _falls_back_to_pr(zte, run, state, "review-failed")
    assert state["review"]["verdict"]["success"] is False and len(state["review"]["rounds"]) == 1
    assert state["checks"]["fast"]["exit"] == 0, "the deterministic checks were green: only the verdict blocks"


def test_class_not_allowed_falls_back_to_pr(zte):
    zte.mock("zte", "build", output="added src/flask/_al_it_answer.py", files={"src/flask/_al_it_answer.py": GOOD})
    run, state = _run(zte, "--class", "feature")
    _falls_back_to_pr(zte, run, state, "class-not-allowed")
    assert state["klass"] == "feature" and "class-not-allowed:feature" in state["ship"]["reasons"]
