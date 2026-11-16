"""`al promote` and `al decide` on a generated layer with (delivery sprint) and a declared
(promotion sprint -> main …): changes come from the generated workflow's own runs (dry runs with the
mock agent, each pull request merged into the sprint by a person), and the promotion waits for a
person, reverts what they refuse, then merges, versions and tags. The output lines are the ones
docs/GUIDE.md quotes."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from conftest import LAYER_TEMPLATE, al, commit, git, sh, status

PROMOTION = '  (promotion sprint -> main #:version (bump minor) #:tag "v{{version}}")\n'
SPRINT_LAYER = LAYER_TEMPLATE.replace(
    "  {placement}\n", "  {placement}\n  (delivery sprint)\n"
    '  (stage sprint #:branch "sprint/{{cycle}}" #:from main #:checks (test))\n' + PROMOTION)
PLAIN_LAYER = LAYER_TEMPLATE
CHANGELOG = ("# Changelog\n\nThe format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).\n\n"
             "## [Unreleased]\n\n## [0.1.0] - 2026-01-02\n\n### Added\n\n- The first release\n")


def _week_name() -> str:
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    return f"{year}-s{week:02d}"


def _layer(app: Path, text: str = SPRINT_LAYER) -> Path:
    (app / "VERSION").write_text("0.1.0\n")
    (app / "CHANGELOG.md").write_text(CHANGELOG)
    (app / ".gitignore").write_text("__pycache__/\n")   # test.sh imports app.py; its bytecode is not a change
    commit(app, "release 0.1.0")
    layer = app.parent / "notes.al"
    layer.mkdir(exist_ok=True)
    (layer / "agentic.rkt").write_text(text.format(root="../notes", placement="(layer sibling)"))
    al("gen", layer)
    return layer


def _deliver(layer: Path, app: Path, sprint: str, adw_id: str, name: str, prompt: str) -> None:
    """One run of the generated `change` workflow (mock agent), its pull request merged by a person."""
    (layer / "mock" / "change").mkdir(parents=True, exist_ok=True)
    (layer / "mock" / "change" / "build.json").write_text(json.dumps({"files": {name: f"{name.upper()} = 1\n"}}))
    run = sh(["uv", "run", "--quiet", layer / "adws" / "adw_change.py", prompt, "--class", "feature",
              "--adw-id", adw_id, "--dry-run"], cwd=layer)
    assert run.returncode == 0, run.stdout + run.stderr
    state = json.loads((layer / "agents" / adw_id / "state.json").read_text())
    assert state["delivery"]["branch"] == sprint and state["ship"]["action"] == "pr"
    tree = app.parent / f"merge-{adw_id}"
    git(app, "worktree", "add", "-q", str(tree), sprint)
    git(tree, "-c", "user.name=Ada Example", "-c", "user.email=ada@example.org", "merge", "--no-ff", "-q", "-m",
        f"Merge run {adw_id}", state["ship"]["commit"])
    git(app, "worktree", "remove", str(tree))


def _sprint_with_three(app: Path) -> tuple[Path, str]:
    layer = _layer(app)
    al("cycle", layer, "start")
    sprint = f"sprint/{_week_name()}"
    git(app, "checkout", "-q", "-b", "work")   # neither main nor the sprint is checked out
    _deliver(layer, app, sprint, "0000aaa1", "cart.py", "Add a cart")
    _deliver(layer, app, sprint, "0000aaa2", "search.py", "Add search")
    _deliver(layer, app, sprint, "0000aaa3", "login.py", "Add login")
    return layer, sprint


def test_al_promote_sprint_to_main_on_a_fixture(app):
    layer, sprint = _sprint_with_three(app)
    first = al("promote", layer, "sprint", "main", ok=False)
    assert first.returncode == 75, first.stdout + first.stderr
    out = first.stdout
    pid = json.loads((layer / "agents" / "promotions" / "sprint-main.json").read_text())["id"]
    expected = [rf"promotion {pid}: {re.escape(sprint)} -> main \(merge; 1 human approval\)",
                r"changes: 3 delivered since the cycle opened \(3 to decide\)",
                rf"checks on {re.escape(sprint)} at [0-9a-f]{{7}}: test=0",
                rf"pack: artefacts/{pid}/pack/index\.html",
                rf"approval item: promotions/{pid}/approval\.md",
                r"waiting: 1 human approval missing; 3 changes undecided",
                r"decide: al decide LAYER sprint main accept\|refuse\|approve \[CHANGE …\] --by NAME \[--comment TEXT\]",
                r"then:   al promote LAYER sprint main"]
    for pattern in expected:
        assert re.search(rf"^{pattern}$", out, re.M), (pattern, out)
    page = (layer / "artefacts" / pid / "pack" / "index.html").read_text()
    assert all(request in page for request in ("Add a cart", "Add search", "Add login"))
    assert "<td>test</td>" in page and "Checks on the stage head" in page
    refused = al("decide", layer, "sprint", "main", "refuse", "0000aaa2", "--then", "revert", "--by", "Ada Example",
                 "--comment", "not this sprint")
    assert 'recorded: Ada Example refuses 0000aaa2 (then revert): "not this sprint"' in refused.stdout
    approved = al("decide", layer, "sprint", "main", "approve", "--by", "Ada Example")
    assert "ready: run `al promote LAYER sprint main` to continue" in approved.stdout
    second = al("promote", layer, "sprint", "main")
    out = second.stdout
    expected = [rf"continuing promotion {pid} \(opened \d{{4}}-\d\d-\d\d\)",
                rf'excluded: 0000aaa2 reverted out of {re.escape(sprint)} \(refused by Ada Example: "not this sprint"\)',
                r"version: 0\.1\.0 -> 0\.2\.0 \(VERSION\); CHANGELOG\.md: keepachangelog entry with 2 changes",
                rf"merged: {re.escape(sprint)} into main by merge at [0-9a-f]{{7}}",
                r"tagged: v0\.2\.0",
                rf"closed: cycle sprint {_week_name()} \(accepted\)"]
    for pattern in expected:
        assert re.search(rf"^{pattern}$", out, re.M), (pattern, out)
    head = git(app, "rev-parse", "refs/heads/main")
    assert len(git(app, "rev-list", "--parents", "-n", "1", head).split()) == 3
    assert git(app, "rev-parse", "v0.2.0^{commit}") == head
    assert "## [0.2.0]" in git(app, "show", f"{head}:CHANGELOG.md")
    names = git(app, "ls-tree", "--name-only", head).split()
    assert "cart.py" in names and "login.py" in names and "search.py" not in names
    sign_off = (layer / "promotions" / pid / "sign-off.md").read_text()
    assert "Ada Example" in sign_off and "not this sprint" in sign_off
    assert git(app, "symbolic-ref", "HEAD") == "refs/heads/work" and status(app) == ""


def test_al_promote_dry_run_changes_nothing(app):
    layer, _sprint = _sprint_with_three(app)
    refs = git(app, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags")
    cp = al("promote", layer, "sprint", "main", "--dry-run")
    assert "would merge" in cp.stdout and "would set version 0.2.0 in VERSION" in cp.stdout
    assert "would tag v0.2.0" in cp.stdout
    assert git(app, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags") == refs
    assert status(app) == ""
    assert not (layer / "promotions").exists() and not (layer / "agents" / "promotions" / "sprint-main.json").exists()


def test_al_promote_without_stages_says_so(app):
    layer = _layer(app, PLAIN_LAYER)
    cp = al("promote", layer, "sprint", "main", ok=False)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    assert "al promote: " in cp.stderr


def test_al_promote_unknown_pair_lists_the_promotions(app):
    layer = _layer(app)
    cp = al("promote", layer, "main", "sprint", ok=False)
    assert cp.returncode == 1
    assert "al promote: unknown-promotion: no promotion main -> sprint; the layer's promotions: sprint -> main" in cp.stderr


def test_al_decide_without_an_open_promotion(app):
    layer = _layer(app)
    cp = al("decide", layer, "sprint", "main", "approve", "--by", "Ada Example", ok=False)
    assert cp.returncode == 1
    assert "al decide: not-open: no promotion sprint -> main is open" in cp.stderr


def test_generated_layer_has_promote_script_only_with_stages(app, tmp_path):
    layer = _layer(app)
    assert (layer / "adws" / "promote.py").is_file()
    ignored = (layer / ".gitignore").read_text().splitlines()
    assert not any(line.strip("/").startswith("promotions") for line in ignored)
    assert "`promotions/` (acceptance and sign-off records)" in (layer / "README.md").read_text()
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "agentic.rkt").write_text(PLAIN_LAYER.format(root=str(app), placement="(layer sibling)"))
    al("gen", plain)
    assert not (plain / "adws" / "promote.py").exists()
