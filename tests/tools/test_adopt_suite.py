"""A hand-built ADW suite (the `orbit` fixture, suite_setup.py): `al scout` describes it as one unit,
read statically, in text and JSON; `al adopt` proposes runnable workflows around its subcommands
and scripts that keep the suite's run id equal to al's, wait on exit 75 and follow its journal; and
a proposed workflow really runs, waits, resumes and passes in a run with real git (no LLM)."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

import existing_setup
import suite_setup
from conftest import ROOT, al, sh, status

sys.path.insert(0, str(ROOT / "tools"))
import al_suite  # noqa: E402

BASE = """#lang al
(agentic-layer #:name "orbit" #:description "Agentic layer of Orbit"
  (codebase #:root "../orbit" #:languages (python)
    (cmd test "uv run pytest -q" #:description "the tests pass"))
  (command plan #:template plan-chore #:model-set heavy #:output path #:validation (test))
  (command commit #:template commit)
  (hop implement #:template run-plan #:takes plan)
  ;; ADWS
  (adw change
    (steps (run plan #:command plan #:args ("$prompt")) (run build #:command implement #:args ("$plan"))
           (check test #:cmd test) (run commit #:command commit) (review) (ship))))
"""
SUITE_ADWS = ["legacy-adw-orbit-plan", "legacy-adw-orbit-build", "legacy-adw-orbit-review", "legacy-adw-orbit-watch",
              "legacy-adw-orbit-status", "legacy-adw-orbit-misc", "legacy-adw-orbit-sync"]
RESUME = '${ADW_RESUME:+--resume "$ADW_ID"}'


@pytest.fixture
def orbit(tmp_path: Path) -> Path:
    return suite_setup.build(tmp_path)


@pytest.fixture
def orbit_layer(orbit: Path) -> Path:
    layer = orbit.with_name("orbit.al")
    al("init", orbit)
    (layer / "agentic.rkt").write_text(BASE)
    return layer


def tree(directory: Path) -> dict[str, str]:
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file() and ".git" not in p.parts}


def section(text: str) -> str:
    start = text.index("### Existing ADW suite")
    return text[start:text.index("\n## ", start)].rstrip("\n")


def scripts_of(suite: dict) -> dict[str, dict]:
    return {s["path"]: s for s in suite["scripts"]}


# ------------------------------------------------------------------------------------------ scout

def test_scout_describes_the_suite_as_one_unit(orbit):
    suite = json.loads(al("scout", orbit, "--json").stdout)["existing_adw_suite"]
    assert suite["root"] == "adws/"
    assert suite["runtime_package"]["path"] == "adws/adw_modules/"
    assert {"agent.py", "state.py", "guards.py"} <= set(suite["runtime_package"]["modules"])
    assert suite["runtime_package"]["guards"] == ["adws/adw_modules/guards.py:guard_clean_tree"]
    entry = suite["entry"]
    assert entry["path"] == "adws/adw_orbit.py" and entry["usage"] == ["adws/adw_orbit.py COMMAND [ARGS]"]
    assert [(s["name"], s["arguments"], s["description"]) for s in entry["subcommands"]] == [
        ("plan", "REQUEST [--adw-id ID]", "plan a change from a one-line request"),
        ("build", "SPEC [--adw-id ID]", "build a planned change"),
        ("review", "ADW_ID", "review the change of a run"),
        ("watch", "[--adw-id ID] [--resume ADW_ID] [--no-wait]", "wait for the review queue, then review"),
        ("status", "[ADW_ID]", "show the state of a run")]
    assert {s["name"]: s["script"] for s in entry["subcommands"]}["watch"] == "adws/adw_orbit_watch.py"
    journal = suite["journal"]
    assert journal["root"] == ".work/adw" and journal["constant"] == "JOURNAL_ROOT"
    assert journal["module"] == "adws/adw_modules/state.py" and journal["outside_git"] is True
    assert {"state.json", "events.jsonl", "prompt.md"} <= set(journal["files"]) and journal["how"].startswith("ast")
    assert suite["routing"]["tiers"] == {"deep": "claude-opus-5-5", "quick": "claude-haiku-5-5"}
    assert suite["routing"]["steps"] == {"orbit-plan": "deep", "orbit-round-review": "deep"}
    assert suite["routing"]["default"] == "quick" and suite["routing"]["problems"] == []
    assert suite["wrappers"] == [{"path": "adws/bin/orbit", "calls": ["adws/adw_orbit.py"], "how": "text scan"}]
    assert [u["path"] for u in suite["units"]] == ["ops/systemd/orbit-watch.service", "ops/systemd/orbit-watch.timer"]
    assert suite["units"][0]["calls"] == ["adws/bin/orbit"]

    scripts = scripts_of(suite)
    assert set(scripts) == {"adws/adw_orbit.py", "adws/adw_orbit_plan.py", "adws/adw_orbit_watch.py",
                            "adws/adw_orbit_sync.sh", "adws/adw_orbit_misc.py"}
    plan = scripts["adws/adw_orbit_plan.py"]
    assert plan["title"] == "Plan a change to orbit from a one-line request." and plan["how"]["title"] == "docstring"
    assert plan["usage"] == ["adws/adw_orbit_plan.py REQUEST [--adw-id ID]", "adws/adw_orbit_plan.py --resume ADW_ID"]
    assert plan["options"] == {"adw_id": True, "resume": "with-id", "wait": []}
    assert [(s["number"], s["kind"], s["prompt"]) for s in plan["steps"]] == [
        (1, "code", ""), (2, "llm", "orbit-plan"), (3, "llm", "orbit-summarise"), (4, "code", "")]
    assert plan["prompts"] == [{"name": "orbit-plan", "file": ".claude/commands/orbit-plan.md",
                                "how": "ast: string literal '/orbit-plan'"}]
    assert [p["name"] for p in plan["unresolved_prompts"]] == ["orbit-summarise"]
    assert plan["schemas"][0]["file"] == "adws/schemas/orbit-plan.schema.json" and plan["schemas"][0]["exists"] is True
    watch = scripts["adws/adw_orbit_watch.py"]
    assert watch["options"] == {"adw_id": True, "resume": "with-id", "wait": ["--no-wait"]}
    assert watch["prompts"][0]["name"] == "orbit-round-review"                 # named without the leading /
    assert watch["schemas"][0]["schema"] == "orbit-review.schema.json" and watch["schemas"][0]["exists"] is True
    codes = {c["code"]: c for c in watch["exit_codes"]}
    assert set(codes) == {0, 75}
    assert codes[75]["meaning"] == "still waiting for the review queue; run again with --resume"
    assert "ast" in codes[75]["how"] and "docstring" in codes[75]["how"]
    sync = scripts["adws/adw_orbit_sync.sh"]
    assert sync["language"] == "shell" and sync["title"] == "Sync the orbit cache." and sync["how"]["title"] == "comment"
    assert sync["usage"] == ["adws/adw_orbit_sync.sh [--adw-id ID]"] and sync["options"]["adw_id"] is True
    assert [c["code"] for c in sync["exit_codes"]] == [0, 3] and sync["exit_codes"][1]["how"] == "text scan: exit N"
    misc = scripts["adws/adw_orbit_misc.py"]
    assert misc["title"] == "" and "title" in misc["facts_unknown"]
    assert suite["unknown"] == ["adws/adw_orbit_misc.py: no docstring or leading comment, so its purpose and usage are unknown"]
    assert status(orbit) == ""


def test_scout_json_equals_the_text(orbit):
    data = json.loads(al("scout", orbit, "--json").stdout)
    text = al("scout", orbit).stdout
    assert al_suite.render_suite(data["existing_adw_suite"]) == section(text)
    setup = text[text.index("## Existing agent setup"):text.index("## Evidence")]
    assert "### Existing ADW suite" in setup
    assert "- prompt `orbit-summarise`: UNRESOLVED" in setup
    assert "75 still waiting for the review queue; run again with --resume" in setup


def test_scout_executes_and_imports_nothing(orbit):
    before = set(sys.modules)
    al("scout", orbit)
    al("scout", orbit, "--json")
    assert not (orbit / "misc-ran.txt").exists() and not (orbit / ".work").exists()
    assert not (orbit / "adws" / "adw_modules" / "__pycache__").exists()
    assert status(orbit) == ""
    assert not {m for m in set(sys.modules) - before if m.startswith(("adw_modules", "adw_orbit"))}


VARIANTS = {
    "no routing file": ([ "adws/model-routing.json"], {}, ["model routing: no routing file found in adws/"]),
    "no entry script": (["adws/adw_orbit.py"], {}, [
        "entry script: no script in adws/ lists subcommands (a `Commands:` block or argparse subparsers)"]),
    "no journal constant": ([], {"adws/adw_modules/state.py": suite_setup.FILES["adws/adw_modules/state.py"].replace(
        'JOURNAL_ROOT = ".work/adw"\n', "").replace("Path(JOURNAL_ROOT)", 'Path(".work") / "adw"')},
        ["journal root: no module constant names the folder the suite writes its run journal to"]),
    "routing not JSON": ([], {"adws/model-routing.json": "tiers: deep\n"},
                         ["model routing: adws/model-routing.json does not parse as JSON"]),
    "a script with a syntax error": ([], {"adws/adw_orbit_misc.py": '"""Tidy up."""\nimport adw_modules\ndef broken(:\n'}, []),
}


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_scout_degrades_cleanly_when_parts_are_missing(tmp_path, variant):
    drop, replace, extra = VARIANTS[variant]
    app = suite_setup.build_variant(tmp_path, drop, replace)
    cp = al("scout", app, "--json")
    suite = json.loads(cp.stdout)["existing_adw_suite"]
    assert suite is not None
    misc = "adws/adw_orbit_misc.py: no docstring or leading comment, so its purpose and usage are unknown"
    if variant == "a script with a syntax error":
        parsed = scripts_of(suite)["adws/adw_orbit_misc.py"]
        assert parsed["parse_error"].startswith("line 3") and parsed["title"] == "Tidy up."
        assert suite["unknown"] == [f"adws/adw_orbit_misc.py: does not parse as Python ({parsed['parse_error']}); "
                                    "its facts come from a text scan"]
    else:
        assert suite["unknown"] == [misc, *extra]
    assert "### Existing ADW suite" in al("scout", app).stdout


def test_no_suite_without_a_shared_runtime_package(tmp_path):
    larder = existing_setup.build(tmp_path)
    assert json.loads(al("scout", larder, "--json").stdout)["existing_adw_suite"] is None
    assert "### Existing ADW suite" not in al("scout", larder).stdout


# ------------------------------------------------------------------------------------------ adopt

def _ir(layer: Path) -> dict:
    return json.loads(al("compile", layer).stdout)


def test_adopt_proposes_runnable_workflows_and_compiles(orbit, orbit_layer):
    out = al("adopt", orbit_layer).stdout
    assert "suite:     7 workflow(s) of the hand-built suite in adws/ → PROPOSED, runnable; 2 prompt(s) left in place" in out
    ir = _ir(orbit_layer)
    names = [a["name"] for a in ir["adws"] if a["name"].startswith("legacy-")]
    assert sorted(names) == sorted(SUITE_ADWS)
    assert "legacy-adw-orbit-plan-x" not in names and "legacy-adw-orbit-watch-x" not in names
    runs = {c["name"]: c["run"] for c in ir["codebase"]["commands"]}
    assert runs["legacy-adw-orbit-watch"] == f"uv run adws/adw_orbit.py watch {RESUME}"
    assert runs["legacy-adw-orbit-plan"] == "uv run adws/adw_orbit.py plan"
    assert runs["legacy-adw-orbit-sync"] == "bash adws/adw_orbit_sync.sh"
    assert runs["legacy-adw-orbit-misc"] == "python3 adws/adw_orbit_misc.py"
    steps = {a["name"]: a["steps"] for a in ir["adws"]}
    watch = steps["legacy-adw-orbit-watch"][0]
    assert watch["kind"] == "script" and watch["waitingExit"] == [75] and watch["journal"] == ".work/adw/{adw_id}"
    assert watch["args"] == ["--adw-id", "$adw_id"]
    plan = steps["legacy-adw-orbit-plan"][0]
    assert plan["args"] == ["$prompt", "--adw-id", "$adw_id"] and plan["waitingExit"] == []
    assert plan["journal"] == ".work/adw/{adw_id}"
    assert steps["legacy-adw-orbit-review"][0]["args"] == ["$adw_id"]
    assert steps["legacy-adw-orbit-sync"][0]["journal"] == ""
    assert [s["kind"] for s in steps["legacy-adw-orbit-watch"]] == ["script", "review", "ship"]
    description = next(a for a in ir["adws"] if a["name"] == "legacy-adw-orbit-watch")["description"]
    assert description == "PROPOSAL (al adopt): runs adws/adw_orbit.py watch of the hand-built suite; a person reviews the result"
    al("gen", orbit_layer)
    assert "check: ok" in al("check", orbit_layer).stdout
    assert status(orbit) == ""


def test_adopting_the_suite_twice_changes_nothing(orbit, orbit_layer):
    al("adopt", orbit_layer)
    first = tree(orbit_layer)
    assert "nothing changed: the layer already has all of it" in al("adopt", orbit_layer).stdout
    assert tree(orbit_layer) == first


def test_suite_only_prompts_stay_in_place_and_previously_adopted_ones_keep_working(orbit, orbit_layer):
    al("adopt", orbit_layer)
    custom = orbit_layer / "templates" / "custom"
    assert sorted(p.name for p in custom.iterdir()) == ["orbit-notes.md"]
    report = (orbit_layer / "adopted" / "REPORT.md").read_text()
    assert ("`.claude/commands/orbit-plan.md`: left in place: used only by the suite's own runtime; "
            "copying it would create a second copy") in report

    # an earlier `al adopt` (before suites were recognised) copied orbit-plan and the layer uses it
    record_path = orbit_layer / "adopted" / ".al-adopt.json"
    record = json.loads(record_path.read_text())
    text = "<!-- adopted earlier -->\nWrite a plan for: $ARGUMENTS\n"
    (custom / "orbit-plan.md").write_text(text)
    record["files"]["templates/custom/orbit-plan.md"] = hashlib.sha256(text.encode()).hexdigest()
    record_path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    out = al("adopt", orbit_layer, "--force").stdout       # --force: the file above stands in for the earlier copy
    assert "1 prompt(s) left in place" in out
    assert (custom / "orbit-plan.md").is_file()
    assert "kept: adopted before" in (orbit_layer / "adopted" / "REPORT.md").read_text()
    definition = (orbit_layer / "agentic.rkt").read_text()
    (orbit_layer / "agentic.rkt").write_text(definition.replace(
        ";; ADWS", '(adw ask-plan (steps (run plan #:command orbit-plan #:args ("$prompt"))))'))
    ir = _ir(orbit_layer)
    assert next(c for c in ir["commands"] if c["name"] == "orbit-plan")["file"] == "templates/custom/orbit-plan.md"
    assert any(a["name"] == "ask-plan" for a in ir["adws"])


def test_report_explains_how_to_run_each_workflow_and_the_model_routing(orbit, orbit_layer):
    al("adopt", orbit_layer)
    report = (orbit_layer / "adopted" / "REPORT.md").read_text()
    run = report[report.index("## Run an existing workflow"):report.index("## Model routing")]
    for name in SUITE_ADWS:
        assert f'uv run adws/adw_{name}.py "<request>"' in run
        assert f"uv run adws/adw_{name}.py --resume --adw-id <id>" in run
    assert "### `legacy-adw-orbit-watch`: adws/adw_orbit.py watch" in run
    assert "it runs `uv run adws/adw_orbit.py watch --adw-id <id>`" in run
    assert "it may wait: exit 75 ends the run `waiting`" in run
    assert "unattended: yes (it has --no-wait)" in run
    assert "needs arguments: REQUEST (passed as $prompt)" in run
    assert "unattended: unknown" in run
    assert "not managed by al" in run
    assert "`adws/adw_modules/guards.py:guard_clean_tree`" in run
    assert "`ops/systemd/orbit-watch.timer`" in run and "`ops/systemd/orbit-watch.service`" in run
    assert "`.work/adw`" in run
    routing = report[report.index("## Model routing"):]
    assert "| suite | `deep` | `claude-opus-5-5` |" in routing and "| suite | `quick` | `claude-haiku-5-5` |" in routing
    assert "| layer | `base` |" in routing and "| layer | `heavy` |" in routing
    assert "the suite's tier `quick` uses `claude-haiku-5-5`, which no tier of the layer uses" in routing
    assert "al does not change it" in routing


def _run(layer: Path, adw: str, *args: str) -> tuple[int, dict, str]:
    cp = sh(["uv", "run", "--quiet", layer / "adws" / f"adw_{adw}.py", *args], cwd=layer, timeout=600)
    run_id = args[args.index("--adw-id") + 1]
    return cp.returncode, json.loads((layer / "agents" / run_id / "state.json").read_text()), cp.stdout + cp.stderr


def _events(layer: Path, run_id: str) -> list[dict]:
    path = layer / "agents" / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_proposed_workflow_mirrors_the_journal_waits_and_passes_after_resume(orbit, orbit_layer):
    al("adopt", orbit_layer)
    al("gen", orbit_layer)
    run_id = "0b170001"
    code, state, out = _run(orbit_layer, "legacy-adw-orbit-watch", "watch the review queue", "--adw-id", run_id)
    assert code == 75, out
    assert state["status"] == "waiting"
    mirrored = [e for e in _events(orbit_layer, run_id) if e["type"] == "script.event"]
    assert mirrored and all(e["foreign"]["kind"] == "watch.check" for e in mirrored)
    assert f"--resume --adw-id {run_id}" in out
    (Path(state["worktree"]) / "orbit.ready").write_text("ready\n")
    code, state, out = _run(orbit_layer, "legacy-adw-orbit-watch", "--resume", "--adw-id", run_id)
    assert code == 0, out
    assert state["status"] == "passed"
    documents = state["scripts"]["run"]["documents"]
    assert {(d["inner"], d["file"]) for d in documents} == {("round-review", "prompt.md"), ("round-review", "output.json")}
    prompt = (orbit_layer / next(d["path"] for d in documents if d["file"] == "prompt.md")).read_text()
    assert "Review the change of " + run_id in prompt
    ends = [e["foreign"] for e in _events(orbit_layer, run_id)
            if e["type"] == "script.event" and e["foreign"].get("kind") == "agent.end"]
    assert ends and ends[0]["usage"] == {"input_tokens": 1200, "output_tokens": 340} and ends[0]["cost_usd"] == 0.02
    assert status(orbit) == ""


def test_a_proposed_plan_workflow_records_inner_documents(orbit, orbit_layer):
    al("adopt", orbit_layer)
    al("gen", orbit_layer)
    run_id = "0b170002"
    code, state, out = _run(orbit_layer, "legacy-adw-orbit-plan", "add a moon", "--adw-id", run_id)
    assert code == 0, out
    assert state["status"] == "passed"
    subjects = [(e["foreign"]["kind"], e["foreign"]["subject"]) for e in _events(orbit_layer, run_id)
                if e["type"] == "script.event"]
    assert ("step.start", "read-request") in subjects and ("step.end", "plan") in subjects
    assert ("agent.end", "orbit-plan") in subjects
    files = {(d["inner"], d["file"]) for d in state["scripts"]["run"]["documents"]}
    assert files == {("plan", "prompt.md"), ("plan", "output.json")}
    assert "Write a plan for: add a moon" in (orbit_layer / f"agents/{run_id}/scripts/run/journal/plan/prompt.md").read_text()
    assert status(orbit) == ""
