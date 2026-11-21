"""Fixtures of the al integration suite. See tests/README.md.

  testbed(name)            session: a private clone of the testbed at its pinned commit, in the
                           session's tmp dir. testbeds/<name> is only read (optional object cache)
                           and asserted unchanged when the session ends
  layer(name, *edits)      function: a fresh generated layer in a tmp dir, rooted at that private
                           clone; teardown removes every worktree, branch, container, volume and
                           image the test made and then asserts that the clone is exactly as it was
"""
from __future__ import annotations

import secrets
import shutil
import socket
from pathlib import Path
from typing import Callable, Iterator

import pytest

import alit


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Every skip prints its reason (as -rs would), so a log says why a test did not run.
    if "s" not in (config.option.reportchars or ""):
        config.option.reportchars = (config.option.reportchars or "") + "s"


@pytest.fixture(scope="session", autouse=True)
def _al_labelled_podman():
    """Every image and container this session's tests make is labelled al.test (a podman shim first on
    PATH, kit/runtime/adw_modules/diskuse.py) and removed when the session ends; leftovers older than six
    hours go when it starts. Nothing happens without that file or without podman."""
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "kit" / "runtime" / "adw_modules" / "diskuse.py"
    if not path.is_file():
        yield ""
        return
    spec = importlib.util.spec_from_file_location("al_diskuse", path)
    diskuse = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diskuse)
    with diskuse.labelled_session() as session:
        yield session


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

def _fetch(name: str, target: Path) -> None:
    """Make `target` a shallow checkout of the pinned commit, fetched from the pinned URL."""
    spec = alit.TESTBEDS[name]
    if not online():
        pytest.skip(f"testbed `{name}`: no local cache at {alit.TESTBED_DIR / name} that contains the pin and "
                    f"github.com is unreachable: needs network to fetch {spec['url']} @ {spec['commit'][:7]}")
    target.mkdir(parents=True)
    alit.sh(["git", "init", "-q", "-b", spec["branch"], target], check=True)
    alit.git(target, "remote", "add", "origin", spec["url"])
    alit.sh(["git", "-C", target, "fetch", "-q", "--depth", "1", "origin", spec["commit"]], check=True, timeout=900)


def _copy(name: str, cache: Path, target: Path) -> bool:
    """Clone the local cache into `target` (objects copied, never linked). False when git cannot."""
    cp = alit.sh(["git", "clone", "-q", "--no-hardlinks", "--no-checkout", cache, target], timeout=900)
    if cp.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        return False
    # The clone's `origin` would be the user's checkout: a `git push origin` of the runtime must never
    # be able to reach it. Point origin at the pinned URL instead (this also drops the tracking refs).
    alit.git(target, "remote", "remove", "origin")
    alit.git(target, "remote", "add", "origin", alit.TESTBEDS[name]["url"])
    return True


def _private_clone(name: str, cache: Path, target: Path) -> None:
    spec = alit.TESTBEDS[name]
    try:
        if not (alit.cache_has(cache, spec["commit"]) and _copy(name, cache, target)):
            _fetch(name, target)
        alit.git(target, "checkout", "-q", "-B", spec["branch"], spec["commit"])
        for other in set(alit.heads(target)) - {spec["branch"]}:  # the branch the cache had checked out
            alit.git(target, "branch", "-q", "-D", other)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    problems = []
    if alit.git(target, "rev-parse", "HEAD", check=False) != spec["commit"]:
        problems.append("HEAD is not the pinned commit")
    if alit.git(target, "branch", "--show-current", check=False) != spec["branch"]:
        problems.append(f"`{spec['branch']}` is not checked out")
    if alit.git(target, "status", "--porcelain", check=False):
        problems.append("the working tree is not clean")
    assert not problems, f"private clone of testbed `{name}` at {target} is unusable: {problems}"


@pytest.fixture(scope="session")
def testbed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Callable[[str], Path]]:
    """testbed(name) -> a clone of the testbed that belongs to this session only: default branch
    checked out at the pinned commit, clean. `testbeds/<name>` is at most read (as an object cache)."""
    home = tmp_path_factory.mktemp("testbeds")
    ready: dict[str, Path] = {}
    caches: dict[Path, dict[str, object]] = {}

    def get(name: str) -> Path:
        if name not in ready:
            cache = alit.TESTBED_DIR / name
            if cache not in caches and (cache / ".git").exists():
                caches[cache] = alit.cache_fingerprint(cache)
            _private_clone(name, cache, home / name)
            ready[name] = home / name
        return ready[name]

    yield get

    changed = []
    for cache, snapshot in caches.items():
        after = alit.cache_fingerprint(cache)
        diff = {key: (snapshot[key], after[key]) for key in snapshot if snapshot[key] != after[key]}
        if diff:
            changed.append(f"{cache}: (at session start, at session end) {diff}")
    assert not changed, ("a local testbed checkout changed while the suite ran. The suite only reads these "
                         "(git clone / cat-file / status without index refresh), so either something else "
                         "worked in it at the same time or a test escaped its private clone:\n  "
                         + "\n  ".join(changed))


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
    alit.podman_cleanup(podman_before, [handle.name for handle in made])
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
            leaks.append(f"{app.name}: the private testbed clone is not as the test found it (before, after): {diff}")
        left = [str(tree) for handle in made if handle.app == app for tree in alit.worktrees_under(app, handle.dir)]
        if left:
            leaks.append(f"{app.name}: worktrees still registered: {left}")
    assert not leaks, "teardown found leaks:\n  " + "\n  ".join(leaks)


# A run's sandbox points git at the run's own object store through environment variables. Tests build
# their own repositories, so those variables must not reach them: clear them once, before any test.
import os as _os
for _name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
              "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_QUARANTINE_PATH", "GIT_CEILING_DIRECTORIES"):
    _os.environ.pop(_name, None)
