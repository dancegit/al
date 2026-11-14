"""Fixtures of the al integration suite. See tests/README.md.

  testbed(name)            session: testbeds/<name> at its pinned commit (cloned only when missing)
  layer(name, *edits)      function: a fresh generated layer in a tmp dir; teardown removes every
                           worktree, branch, container, volume and image the test made and then
                           asserts that the testbed is exactly as it was
"""
from __future__ import annotations

import secrets
import shutil
import socket
from pathlib import Path
from typing import Callable

import pytest

import alit


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--update-golden", action="store_true", default=False,
                     help="rewrite tests/integration/golden/ from the freshly generated layers")


_ONLINE: bool | None = None


def online() -> bool:
    global _ONLINE
    if _ONLINE is None:
        try:
            socket.create_connection(("github.com", 443), timeout=4).close()
            _ONLINE = True
        except OSError:
            _ONLINE = False
    return _ONLINE


def pytest_runtest_setup(item: pytest.Item) -> None:
    for marker, binary in (("podman", "podman"), ("nix", "nix")):
        if item.get_closest_marker(marker) and not alit.have(binary):
            pytest.skip(f"marked `{marker}` and `{binary}` is not on PATH")
    if item.get_closest_marker("network") and not online():
        pytest.skip("marked `network` and github.com:443 is unreachable")


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


# ------------------------------------------------------------------------------ testbeds

def _clone(name: str, target: Path) -> None:
    spec = alit.TESTBEDS[name]
    if not online():
        pytest.skip(f"testbed `{name}` is not cloned at {target} and github.com is unreachable: "
                    f"needs network once to fetch {spec['url']} @ {spec['commit'][:7]}")
    target.mkdir(parents=True)
    try:
        alit.sh(["git", "init", "-q", "-b", spec["branch"], target], check=True)
        alit.git(target, "remote", "add", "origin", spec["url"])
        alit.sh(["git", "-C", target, "fetch", "-q", "--depth", "1", "origin", spec["commit"]], check=True, timeout=900)
        alit.git(target, "checkout", "-q", "-B", spec["branch"], "FETCH_HEAD")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)  # a half-made clone would fail the pin check forever
        raise


def _check_pin(name: str, target: Path) -> None:
    spec = alit.TESTBEDS[name]
    problems = []
    head = alit.git(target, "rev-parse", "HEAD", check=False)
    if head != spec["commit"]:
        problems.append(f"HEAD is {head[:12] or 'unreadable'}, the pin in testbeds.toml is {spec['commit'][:12]}")
    status = alit.git(target, "status", "--porcelain", check=False)
    if status:
        problems.append("the working tree is not clean:\n" + status)
    if spec["branch"] not in alit.heads(target):
        problems.append(f"local branch `{spec['branch']}` (the layer's default branch) does not exist")
    if problems:
        pytest.fail(
            f"testbed {target} is not usable:\n  - " + "\n  - ".join(problems) + "\n"
            "The suite never resets a checkout on its own. Either restore it yourself\n"
            f"  git -C {target} stash -u   # if you have changes to keep\n"
            f"  git -C {target} checkout {spec['branch']} && git -C {target} reset --hard {spec['commit']}\n"
            f"or move it away and let the suite clone a fresh one (rm -rf {target}).", pytrace=False)


@pytest.fixture(scope="session")
def testbed() -> Callable[[str], Path]:
    """testbed(name) -> path of testbeds/<name>, guaranteed to be at the pinned commit and clean."""
    ready: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name not in ready:
            target = alit.TESTBED_DIR / name
            if not (target / ".git").exists():
                if target.exists() and any(target.iterdir()):
                    pytest.fail(f"{target} exists but is not a git checkout; remove it so the suite can clone it", pytrace=False)
                if target.exists():
                    target.rmdir()
                _clone(name, target)
            _check_pin(name, target)
            ready[name] = target
        return ready[name]

    return get


# ------------------------------------------------------------------------------ layers

MINIMAL = """#lang al
;; Fixture layer of the al integration suite (not authored from an LLM.txt).
(agentic-layer #:name "{name}"
  (codebase #:root "{root}" #:languages (python) #:default-branch "{branch}"
{cmds})
  (agent mock)
  (command plan #:template plan-chore #:output path)
  (hop implement #:template run-plan #:takes plan)
  (adw plan-build #:agent mock
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (run build #:command implement #:args ("$plan"))
      (check {first} #:cmd {first}))))
"""


@pytest.fixture
def layer(testbed: Callable[[str], Path], tmp_path: Path):
    """layer(name, *edits, cmds=None) -> alit.Layer, generated fresh from tests/authored/<name>.

    `edits` are functions str -> str applied to the authored agentic.rkt (see alit.py). With
    `cmds={"ok": "true", …}` the layer is a minimal mock-agent one with exactly those codebase cmds.
    """
    made: list[alit.Layer] = []
    before: dict[Path, dict] = {}
    heads_before: dict[Path, dict[str, str]] = {}
    podman_before = alit.podman_snapshot()

    def make(name: str, *edits: alit.Edit, cmds: dict[str, str] | None = None) -> alit.Layer:
        app = testbed(name)
        if app not in before:
            before[app] = alit.fingerprint(app)
            heads_before[app] = alit.heads(app)
        unique = f"it-{name}-{secrets.token_hex(3)}"
        directory = tmp_path / unique
        directory.mkdir()
        if cmds is None:
            source = (alit.AUTHORED / name / "agentic.rkt").read_text()
            shutil.copy(alit.AUTHORED / name / "LLM.txt", directory / "LLM.txt")
            source = alit.substitute(f'#:root "../../testbeds/{name}"', f'#:root "{app}"')(source)
            source = alit.substitute(f'#:name "{name}"', f'#:name "{unique}"', 1)(source)
            for edit in edits:
                source = edit(source)
        else:
            assert not edits, "edits apply to authored layers only"
            lines = "\n".join("    (cmd {} \"{}\")".format(n, s.replace("\\", "\\\\").replace('"', '\\"')) for n, s in cmds.items())
            source = MINIMAL.format(name=unique, root=app, branch=alit.TESTBEDS[name]["branch"], cmds=lines, first=next(iter(cmds)))
        (directory / "agentic.rkt").write_text(source)
        handle = alit.Layer(bed=name, name=unique, dir=directory, app=app)
        made.append(handle)  # registered before gen so a failing gen is still torn down
        handle.al("gen")
        return handle

    yield make

    leaks: list[str] = []
    for handle in made:
        alit.kill_processes_under(handle.dir)
    alit.podman_cleanup(podman_before)
    for app, snapshot in before.items():
        for handle in made:
            if handle.app != app:
                continue
            for tree in alit.worktrees_under(app, handle.dir):
                alit.sh(["git", "-C", app, "worktree", "remove", "--force", tree], timeout=120)
        alit.sh(["git", "-C", app, "worktree", "prune"], timeout=120)
        now = alit.heads(app)
        for branch in sorted(set(now) - set(heads_before[app])):
            cp = alit.sh(["git", "-C", app, "branch", "-D", branch], timeout=120)
            if cp.returncode != 0:
                leaks.append(f"{app.name}: cannot delete branch {branch}: {cp.stderr.strip()}")
        shipped = {state.get("ship", {}).get("commit") for handle in made if handle.app == app
                   for state in handle.states().values()}
        for branch, sha in heads_before[app].items():
            # al/ship/<default> is the runtime's staging ref: a test may advance one that already existed.
            # It is put back only when it points at a commit one of this test's runs shipped.
            if branch.startswith("al/ship/") and now.get(branch) != sha and now.get(branch) in shipped:
                alit.git(app, "update-ref", f"refs/heads/{branch}", sha)
        after = alit.fingerprint(app)
        if after != snapshot:
            diff = {key: (snapshot[key], after[key]) for key in snapshot if snapshot[key] != after[key]}
            leaks.append(f"{app.name}: the testbed is not as the test found it (before, after): {diff}")
        left = [str(tree) for handle in made if handle.app == app for tree in alit.worktrees_under(app, handle.dir)]
        if left:
            leaks.append(f"{app.name}: worktrees still registered: {left}")
    assert not leaks, "teardown found leaks:\n  " + "\n  ".join(leaks)
