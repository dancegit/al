"""Invented repositories with a forge behind them, for `al create` / `al adopt-all` and the forge facts
of `al scout`. Each is a git repository with one commit on `main`; every name is invented.

  forgejo_repo  remote git@git.example.org:acme/larder.git (a self-hosted Forgejo whose host name does not
                say so), .forgejo/workflows/ci.yml, a Makefile whose `all` and `test` echo; with `setup` the
                agent setup of existing_setup.py, with `suite` the hand-built ADW suite of suite_setup.py
  github_repo   remote https://github.com/acme/notes.git, .github/workflows/ci.yml
  bare_repo     no remote; with `gitlab`, a remote on gitlab.example.org and a .gitlab-ci.yml
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import existing_setup
import suite_setup

MAKEFILE = "all:\n\t@echo built\n\ntest:\n\t@echo tested\n"
FORGEJO_CI = ("name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: docker\n    steps:\n"
              "      - uses: actions/checkout@v4\n      - run: make test\n")
GITHUB_CI = "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make test\n"
README = "# {name}\n\n{name} is a small invented application used by the tests of al; it keeps a list and prints it.\n"


MAKE_STAND_IN = '''#!{python}
"""A stand-in for make where none is installed: runs the recipe lines of the named (else the first) target."""
import re, subprocess, sys
targets = [a for a in sys.argv[1:] if not a.startswith("-")]
text = open("Makefile").read()
names = re.findall(r"^([A-Za-z0-9_.-]+)\\s*:", text, re.M)
target = targets[0] if targets else names[0]
block = re.search(r"^" + re.escape(target) + r"\\s*:.*\\n((?:\\t.*\\n?)*)", text, re.M)
if block is None:
    sys.exit(f"make: *** No rule to make target '{{target}}'.")
for line in block.group(1).splitlines():
    code = subprocess.call(["sh", "-c", line.strip().lstrip("@")])
    if code:
        sys.exit(2)
'''


def make_env(tmp: Path) -> dict[str, str]:
    """The environment the Makefile fixtures' commands run in: the real make when there is one, else a
    small stand-in for it on PATH."""
    if shutil.which("make"):
        return {}
    folder = Path(tmp) / "make-bin"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "make").write_text(MAKE_STAND_IN.format(python=sys.executable))
    (folder / "make").chmod(0o755)
    return {"PATH": f"{folder}{os.pathsep}{os.environ.get('PATH', '')}"}


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0, f"git {' '.join(args)}: {cp.stderr}"
    return cp.stdout


def _repo(root: Path, name: str, files: dict[str, str], remote: str = "", executable: tuple[str, ...] = ()) -> Path:
    app = Path(root) / name
    app.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for rel in executable:
        if (app / rel).is_file():
            (app / rel).chmod(0o755)
    _git(app, "init", "-q", "-b", "main")
    if remote:
        _git(app, "remote", "add", "origin", remote)
    _git(app, "add", "-A")
    _git(app, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"{name}: first commit")
    return app


def forgejo_repo(root: Path, setup: bool = True, suite: bool = True, extra: dict[str, str] | None = None) -> Path:
    """root/larder on a self-hosted Forgejo (git.example.org); `extra` files are written over the rest."""
    files: dict[str, str] = {}
    executable: tuple[str, ...] = ()
    if setup:
        files.update(existing_setup.FILES)
        executable += ("adws/adw_plan_build.py", "adws/adw_test.py")
    if suite:
        files.update(suite_setup.FILES)
        executable += suite_setup.EXECUTABLE
    files.update({"README.md": README.format(name="Larder"), "Makefile": MAKEFILE,
                  ".forgejo/workflows/ci.yml": FORGEJO_CI, "larder.py": "ITEMS = []\n"})
    files.update(extra or {})
    return _repo(root, "larder", files, "git@git.example.org:acme/larder.git", executable)


def github_repo(root: Path) -> Path:
    """root/notes on github.com."""
    return _repo(root, "notes", {"README.md": README.format(name="Notes"), "Makefile": MAKEFILE,
                                 ".github/workflows/ci.yml": GITHUB_CI, "notes.py": "NOTES = []\n"},
                 "https://github.com/acme/notes.git")


def bare_repo(root: Path, gitlab: bool = False) -> Path:
    """root/fern without a remote (with `gitlab`: a gitlab.example.org remote and a .gitlab-ci.yml)."""
    files = {"README.md": README.format(name="Fern"), "fern.py": "LEAVES = 3\n"}
    if gitlab:
        files[".gitlab-ci.yml"] = "test:\n  script:\n    - python3 fern.py\n"
    return _repo(root, "fern", files, "git@gitlab.example.org:acme/fern.git" if gitlab else "")
