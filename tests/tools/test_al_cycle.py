"""`al cycle LAYER status|start|close` on a generated layer with (delivery sprint): it opens the next
sprint branch from main with `git branch` (nothing checked out), closes the previous one and never
merges. The output lines are the ones docs/GUIDE.md quotes."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from conftest import LAYER_TEMPLATE, al, git, sh, status

SPRINT_LAYER = (LAYER_TEMPLATE.replace("  {placement}\n", "  {placement}\n  (delivery sprint)\n"))
NO_CYCLE_LAYER = (LAYER_TEMPLATE.replace("  {placement}\n", "  {placement}\n  (delivery trunk)\n"))


def _layer(app: Path, text: str = SPRINT_LAYER) -> Path:
    layer = app.parent / "notes.al"
    layer.mkdir(exist_ok=True)
    (layer / "agentic.rkt").write_text(text.format(root="../notes", placement="(layer sibling)"))
    al("gen", layer)
    return layer


def _week_name() -> str:
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    return f"{year}-s{week:02d}"


def test_cycle_start_status_close_on_a_generated_layer(app):
    layer = _layer(app)
    head = git(app, "rev-parse", "HEAD")
    name = _week_name()
    started = al("cycle", layer, "start").stdout
    assert re.search(rf"^cycle: sprint {name} opened: branch sprint/{name} from main at [0-9a-f]{{7}}$", started, re.M)
    assert git(app, "rev-parse", f"refs/heads/sprint/{name}") == head
    assert git(app, "symbolic-ref", "HEAD") == "refs/heads/main" and status(app) == ""

    lines = al("cycle", layer, "status").stdout.splitlines()
    assert re.fullmatch(rf"cycle: sprint {name} open since \d{{4}}-\d\d-\d\d \(14 days, 14 left\)", lines[0])
    assert re.fullmatch(rf"branch: sprint/{name} \(from main at [0-9a-f]{{7}}\)", lines[1])
    assert lines[2] == "ahead of main: 0 commits · behind main: 0 commits"
    data = json.loads(al("cycle", layer, "status", "--json").stdout)
    assert data["open"] and data["current"]["branch"] == f"sprint/{name}"

    second = al("cycle", layer, "start", "--name", "next").stdout
    assert f"cycle: sprint {name} closed (not merged into main" in second or f"cycle: sprint {name} closed (already in main" in second
    assert "cycle: sprint next opened: branch sprint/next" in second
    closed = al("cycle", layer, "close").stdout
    assert "cycle: sprint next closed" in closed and "next:" in closed
    assert git(app, "rev-parse", "refs/heads/main") == head
    again = al("cycle", layer, "close", ok=False)
    assert again.returncode == 1 and "al cycle: cycle-not-open: no cycle is open" in again.stderr


def test_cycle_on_a_layer_without_a_cycle_says_so(app):
    layer = _layer(app, NO_CYCLE_LAYER)
    cp = al("cycle", layer, "status", ok=False)
    assert cp.returncode == 1
    assert "al cycle: no (cycle …) is declared in agentic.rkt" in cp.stderr


def test_cycle_start_without_push_publishes_nothing(app, tmp_path):
    origin = tmp_path / "origin.git"
    assert sh(["git", "init", "-q", "--bare", origin]).returncode == 0
    git(app, "remote", "add", "origin", str(origin))
    git(app, "push", "-q", "origin", "main")
    before = git(origin, "for-each-ref", "--format=%(refname)")
    layer = _layer(app)
    al("cycle", layer, "start")
    assert git(origin, "for-each-ref", "--format=%(refname)") == before
