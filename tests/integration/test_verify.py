"""`al verify`: the codebase cmds a layer declares are true, proven on a throwaway worktree."""
from __future__ import annotations

import json
import shutil

import pytest

import alit

# toolchain binaries each testbed's real cmds need on the host or (podman stacks) the container engine
REAL = {
    "flask": (["nix"], ["test"]),
    "ripgrep": (["podman"], ["test"]),
    "gin": (["go", "make"], ["test"]),
    "express": (["node", "npm"], ["install", "test"]),          # `install` is never implicit
    "jq": (["podman"], ["install", "build"]),                   # autoreconf + make live in the image
}


def _report(lay) -> dict[str, dict]:
    rows = json.loads((lay.dir / "agents" / "verify" / "report.json").read_text())
    assert all(set(row) == {"name", "cmd", "exit", "seconds", "log", "at"} for row in rows), rows
    return {row["name"]: row for row in rows}


def _nothing_left(lay, heads_before):
    assert lay.trees() == [], "verify must remove its worktree"
    assert alit.worktrees_under(lay.app, lay.dir) == []
    assert alit.heads(lay.app) == heads_before, "verify must not create or move a branch"


def test_verify_reports_true_and_false(layer):
    lay = layer("flask", cmds={
        "ok": "true",
        "bad": "echo first line; echo the reason >&2; false",
        "where": "pwd && git status --porcelain && echo scribble > SCRIBBLE && git status --porcelain",
        "install": "exit 7",  # skipped unless named
    })
    heads_before = alit.heads(lay.app)

    run = lay.al("verify", check=False)
    assert run.returncode == 1, alit.describe(run)
    report = _report(lay)
    assert list(report) == ["ok", "bad", "where"], "cmds run in declaration order; install is skipped"
    assert (report["ok"]["exit"], report["bad"]["exit"], report["where"]["exit"]) == (0, 1, 0)
    assert report["bad"]["cmd"].endswith("false") and report["bad"]["log"] == "agents/verify/bad.log"
    assert (lay.dir / "agents/verify/bad.log").read_text() == "first line\nthe reason\n"
    out = run.stdout
    assert "skip  install" in out and "FAIL  bad  exit 1" in out and "ok    ok  exit 0" in out, out
    assert "        the reason" in out, "the tail of a failing cmd's output is printed"

    # the cmd ran in a clean worktree under the layer, and could write there without touching the app
    where = (lay.dir / "agents/verify/where.log").read_text().splitlines()
    assert where[0].startswith(str(lay.dir / "trees" / "verify-")) and where[1:] == ["?? SCRIBBLE"], where
    assert not (lay.app / "SCRIBBLE").exists()
    _nothing_left(lay, heads_before)

    only = lay.al("verify", "--cmd", "ok", "--cmd", "install", "--json", check=False)
    assert only.returncode == 1, alit.describe(only)
    rows = json.loads(only.stdout)
    assert [(r["name"], r["exit"]) for r in rows] == [("ok", 0), ("install", 7)]
    report = _report(lay)                     # the latest result of every cmd: the earlier rows stay
    assert [report[n] for n in ("ok", "install")] == rows and report["bad"]["exit"] == 1 and "where" in report

    good = lay.al("verify", "--cmd", "ok", check=False)
    assert good.returncode == 0, alit.describe(good)
    unknown = lay.al("verify", "--cmd", "nope", check=False)
    assert unknown.returncode == 1 and "not a declared codebase cmd: nope" in unknown.stderr
    slow = lay.al("verify", "--cmd", "ok", "--timeout", "1", check=False)
    assert slow.returncode == 0
    _nothing_left(lay, heads_before)


def test_verify_timeout_kills_the_cmd(layer):
    lay = layer("flask", cmds={"hang": "sleep 60"})
    heads_before = alit.heads(lay.app)
    run = lay.al("verify", "--timeout", "2", check=False, timeout=60)
    assert run.returncode == 1, alit.describe(run)
    assert _report(lay)["hang"]["exit"] == 124 and _report(lay)["hang"]["seconds"] < 30
    _nothing_left(lay, heads_before)


@pytest.mark.slow
@pytest.mark.nix
@pytest.mark.network
def test_verify(layer):
    """The real flask layer: `typecheck` and `test` are true in the sdlc ADW's nix dev shell."""
    lay = layer("flask")
    heads_before = alit.heads(lay.app)
    run = lay.al("verify", "--cmd", "typecheck", "--cmd", "test", check=False, timeout=1800)
    report = _report(lay)
    tails = "\n".join(f"--- {n}\n" + "\n".join((lay.dir / r["log"]).read_text().splitlines()[-30:]) for n, r in report.items())
    assert run.returncode == 0, alit.describe(run) + "\n" + tails
    assert {name: row["exit"] for name, row in report.items()} == {"typecheck": 0, "test": 0}
    _nothing_left(lay, heads_before)


@pytest.mark.slow
@pytest.mark.network
@pytest.mark.parametrize("bed", list(REAL))
def test_real_checks(layer, bed):
    binaries, cmds = REAL[bed]
    missing = [b for b in binaries if not shutil.which(b)]
    if missing:
        pytest.skip(f"{bed}: toolchain not available ({', '.join(missing)} not on PATH)")
    lay = layer(bed)
    heads_before = alit.heads(lay.app)
    run = lay.al("verify", *[arg for cmd in cmds for arg in ("--cmd", cmd)], check=False, timeout=3600)
    report = _report(lay)
    assert run.returncode == 0, alit.describe(run)
    assert {name: row["exit"] for name, row in report.items()} == dict.fromkeys(cmds, 0)
    _nothing_left(lay, heads_before)
