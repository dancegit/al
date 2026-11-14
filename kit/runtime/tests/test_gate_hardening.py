"""Regressions for the independent review of the zero-touch gate."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adw_modules import review


@pytest.mark.parametrize("path,protected,expected", [
    (".github/workflows/dé.yml", [".github"], True),          # non-ASCII path under a protected dir
    (".github/workflows/ci.yml", ["./.github"], True),        # ./ prefix
    (".github/workflows/ci.yml", [".github/"], True),         # trailing slash
    (".github/workflows/ci.yml", ["/.github"], True),         # leading slash
    (".GitHub/workflows/ci.yml", [".github"], True),          # case cannot dodge
    ("cicd/run.sh", ["ci"], False),                            # prefix collision
    ("ci/run.sh", ["ci"], True),
    ("Cargo.lock", ["*.lock"], True),                          # glob on a file name
    ("crates/core/Cargo.lock", ["*.lock"], True),
    ("ci/a/b.sh", ["ci/**"], True),                            # recursive glob
    ("src/app.py", ["."], True),                               # "." protects everything
    ("src/app.py", [""], True),
    ("src/app.py", ["docs", "ci"], False),
])
def test_is_protected(path: str, protected: list[str], expected: bool) -> None:
    assert review.is_protected(path, protected) is expected


def test_numstat_z_keeps_raw_non_ascii_paths(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              check=True, capture_output=True, text=True).stdout
    git("init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    git("add", "-A"); git("commit", "-qm", "base")
    git("checkout", "-qb", "work")
    target = tmp_path / ".github" / "workflows"
    target.mkdir(parents=True)
    (target / "dé.yml").write_text("on: push\n")
    git("add", "-A"); git("commit", "-qm", "sneaky")
    quoted = git("diff", "--numstat", "--no-renames", "main...HEAD")
    assert quoted.split("\t")[2].startswith('"')              # the hazard: git quotes the path
    lines, files = review.parse_numstat(git("diff", "--numstat", "-z", "--no-renames", "main...HEAD"))
    assert (lines, files) == (1, [".github/workflows/dé.yml"])
    assert review.is_protected(files[0], [".github"])


def test_extra_reasons_force_a_human_pr() -> None:
    option = {"kind": "auto", "ship": "merge", "allowedClasses": ["chore"], "maxDiffLines": 100,
              "blockProtectedPaths": True}
    facts = {"check_exits": {"test": 0}, "require_exits": {"test": 0}, "klass": "chore", "diff_lines": 3,
             "changed_files": ["src/a.py"], "protected": [], "verdict": {"success": True, "issues": []}}
    assert review.decide(option, facts)["ship"] is True
    assert review.decide(option, {**facts, "check_exits": {"test": 0, "e2e": -1}})["reasons"] == ["check-failed:e2e"]
