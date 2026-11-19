"""Shared fixture for the targeted-run and batch tests: a small Python shop application with a real
pytest suite, and the IR of racket/al/tests/fixtures/targeted-testing.rkt adapted to it.

The cmd `unit` appends one line per run to a counter file outside the worktree (`full`, `smoke`, or
`targeted TESTS…`), so a test reads exactly which form ran with which tests. The 40 filler tests make
a full run visibly larger than a targeted one."""
from __future__ import annotations

import contextlib
import io
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from adw_modules import delivery, ir
from adw_modules.runner import main as run_main

DATA = Path(__file__).resolve().parent / "data"
SPRINT = "sprint/2026-s47"
FILLERS = 40
TEXT = {"description": "", "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}

SOURCES = {
    ".gitignore": "__pycache__/\n.pytest_cache/\ncov.json\n",
    "shop/__init__.py": "",
    "shop/cart.py": "def total(prices):\n    return sum(prices)\n",
    "shop/price.py": "from shop import cart\n\n\ndef with_tax(prices, rate=0.25):\n    return cart.total(prices) * (1 + rate)\n",
    "shop/plugins/__init__.py": "",
    "shop/plugins/discount.py": "def apply(amount):\n    return amount * 0.9\n",
    "tests/__init__.py": "",
    "tests/test_cart.py": "from shop.cart import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
    "tests/test_price.py": "from shop import price\n\n\ndef test_with_tax():\n    assert price.with_tax([4]) == 5\n",
    "tests/test_plugins.py": ("import importlib\n\n\ndef test_discount():\n"
                              "    plugin = importlib.import_module('shop.plugins.' + 'discount')\n"
                              "    assert plugin.apply(100) == 90\n"),
}
BROKEN_DISCOUNT = "def apply(amount):\n    return amount * 0.8\n"


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
                            capture_output=True, text=True, timeout=60)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def shop_app(tmp_path: Path) -> Path:
    """A git repository on main with the shop, its three real tests and FILLERS filler tests."""
    app = tmp_path / "shop"
    app.mkdir()
    git(app, "init", "-q", "-b", "main")
    files = dict(SOURCES)
    for n in range(FILLERS):
        files[f"tests/test_filler_{n:02d}.py"] = f"def test_filler_{n:02d}():\n    assert {n} == {n}\n"
    for name, content in files.items():
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        (app / name).write_text(content, encoding="utf-8")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "the shop")
    return app


def unit_cmd(counter: Path, **over: Any) -> dict[str, Any]:
    """The cmd `unit`: full, targeted and smoke forms of one pytest suite, each counted."""
    pytest = f"PYTHONDONTWRITEBYTECODE=1 {shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"
    count = shlex.quote(str(counter))
    return {"name": "unit", "run": f"echo full >> {count}; {pytest}", "timeout": 120, "description": "",
            "covers": ["shop", "tests"], "always": False, "reuse": True,
            "targeted": f"echo targeted {{tests}} >> {count}; {pytest} {{tests}}",
            "smoke": f"echo smoke >> {count}; {pytest} tests/test_cart.py",
            "select": ["changed-tests", "imports"], "selectCmd": "", "pathMap": [], "testRoot": "", "maxTargeted": 200,
            "recordCoverage": "none", "coverageRun": "", "coverageFile": "", **over}


def testing_ir(**over: Any) -> dict[str, Any]:
    """The fixture's (testing targeted #:batch ((every 5) (after-hours 6)) #:escalate (…) …), as the IR."""
    return {"depth": "targeted", "perRun": "inherit", "reviewRounds": "inherit", "onStage": "full",
            "batchEvery": 10, "batchAfterHours": 0, "escalatePaths": ["shop/core/**"], "escalateDiffLines": 1500,
            "escalateFiles": 40, "escalateOn": ["lockfile", "layer", "unmapped"], "escalateClasses": [],
            "onBatchFailure": ["bisect", "open-fix"], "fixAdw": "", "maxMissPercent": 10, **over}


def _check(name: str = "unit") -> dict[str, Any]:
    return {"name": name, "kind": "check", "cmd": "unit", "resolve": "", "retries": 0, "collect": [], "in": "",
            "maxSkipped": -1, "forbidSkipReason": "", "whenAffected": True}


def layer_ir(app: Path, counter: Path, *, testing: dict[str, Any] | None = None, declared: bool = True,
             stages: bool = False, cmd: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    """The IR: cmd unit; zte (plan, build, check, commit, auto review, ship merge) for chore and
    feature; fix (human review) for bug; `testing` unless `declared` is false; the sprint preset's
    stages with `stages`."""
    data: dict[str, Any] = {
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": n, "template": t, "modelSet": "base", "output": o, **TEXT} for n, t, o in (
            ("plan", "plan", "path"), ("build", "build", "text"), ("commit", "commit", "text"),
            ("patch", "patch", "path"), ("implement", "implement", "text"), ("review", "review", "json"))],
        "codebase": {"root": str(app), "defaultBranch": "main", "commands": [cmd or unit_cmd(counter)],
                     "paths": {"source": ["shop"], "tests": ["tests"], "protected": []}},
        "reviews": [{"id": "zero-touch", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 1, "artefacts": [], "allowedClasses": ["chore", "bug", "feature"],
                     "blockProtectedPaths": True, "maxDiffLines": 2000, "requireChecks": ["unit"],
                     "require": "affected", "ship": "merge", "deployCmd": ""},
                    {"id": "person", "kind": "human", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["bug"], "blockProtectedPaths": True,
                     "maxDiffLines": 2000, "requireChecks": [], "ship": "pr", "deployCmd": ""}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27700,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "concurrency": {"maxRuns": 8, "maxAgents": 4, "maxChecks": 4, "waitSeconds": 60},
        "adws": [{"name": "zte", "agent": "mock", "review": "zero-touch", "environment": ["worktree"],
                  "classes": ["chore", "feature"], "steps": [
                      {"name": "plan", "kind": "run", "command": "plan"},
                      {"name": "build", "kind": "run", "command": "build"},
                      _check(),
                      {"name": "commit", "kind": "run", "command": "commit"},
                      {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"},
                      {"name": "ship", "kind": "ship"}]},
                 {"name": "fix", "agent": "mock", "review": "person", "environment": ["worktree"],
                  "classes": ["bug"], "steps": [
                      {"name": "plan", "kind": "run", "command": "plan"},
                      {"name": "build", "kind": "run", "command": "build"},
                      _check(),
                      {"name": "commit", "kind": "run", "command": "commit"},
                      {"name": "review", "kind": "review"},
                      {"name": "ship", "kind": "ship"}]}],
    }
    if declared:
        data["testing"] = testing_ir(**(testing or {}))
    if stages:
        sprint = json.loads((DATA / "delivery-sprint.json").read_text(encoding="utf-8"))
        data.update({"stages": sprint["stages"], "cycle": sprint["cycle"], "delivery": sprint["delivery"]})
    data.update(over)
    return data


def make_shop_layer(make_layer, tmp_path: Path, **over: Any) -> tuple[Path, Path, Path]:
    """(layer, app, counter). The app's own checkout leaves main (and the sprint) free, so a merge
    really lands; with stages the sprint cycle 2026-s47 is open."""
    app = shop_app(tmp_path)
    counter = tmp_path / "ran.log"
    layer = make_layer(layer_ir(app, counter, **over))
    if over.get("stages"):
        from datetime import datetime, timezone

        delivery.cycle_start(ir.load(layer), name="2026-s47", now=datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc))
    git(app, "checkout", "-q", "-b", "work")
    return layer, app, counter


def script(layer: Path, adw: str, step: str, data: dict[str, Any]) -> None:
    path = layer / "mock" / adw / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def change(layer: Path, adw_id: str, files: dict[str, str], *, adw: str = "zte", klass: str = "chore",
           argv: tuple[str, ...] = (), expect: int = 0, prompt: str = "") -> str:
    """One whole run whose build writes `files`; returns its output."""
    script(layer, adw, "build", {"files": files})
    script(layer, adw, "commit", {"commit": True})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run_main(adw, layer, [prompt or f"Change {', '.join(files)}", "--class", klass, "--adw-id", adw_id, *argv])
    assert code == expect, out.getvalue()
    return out.getvalue()


def ran(counter: Path) -> list[str]:
    return counter.read_text(encoding="utf-8").splitlines() if counter.exists() else []


def reset(counter: Path) -> None:
    counter.unlink(missing_ok=True)


def state_of(layer: Path, adw_id: str) -> dict[str, Any]:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text(encoding="utf-8"))


def commit_on(app: Path, branch: str, files: dict[str, str], message: str = "") -> str:
    """A commit on `branch` made in a throwaway worktree (the app's checkout is not touched)."""
    tree = Path(tempfile.mkdtemp(prefix="helper-", dir=app.parent))
    tree.rmdir()
    git(app, "worktree", "add", "-q", str(tree), branch)
    for name, content in files.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(content, encoding="utf-8")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", message or f"{branch}: {', '.join(files)}")
    commit = git(tree, "rev-parse", "HEAD")
    git(app, "worktree", "remove", "--force", str(tree))
    return commit


def merge_into(app: Path, branch: str, source: str) -> str:
    """A person merges `source` into `branch` (in a throwaway worktree)."""
    tree = Path(tempfile.mkdtemp(prefix="helper-", dir=app.parent))
    tree.rmdir()
    git(app, "worktree", "add", "-q", str(tree), branch)
    git(tree, "merge", "-q", "--no-ff", "--no-edit", source)
    commit = git(tree, "rev-parse", "HEAD")
    git(app, "worktree", "remove", "--force", str(tree))
    return commit
