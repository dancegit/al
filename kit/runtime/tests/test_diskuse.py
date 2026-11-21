"""diskuse.py: a settled run's worktree given back, the podman label shim of the test suites, the
removal of labelled test objects (this session's at its end, anyone's older than six hours at the
start, never an unlabelled or kept one) and the `disk:` lines of `al doctor`. Podman is a double
(FakePodman); git and the filesystem are real, the free space is a double."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import diskuse

NOW = 1_800_000_000.0
HOUR = 3600.0
GIB = 1024 ** 3


class FakePodman:
    """A callable `run(argv) -> CompletedProcess` over in-memory containers and images. It honours
    `ps -a --filter label=K=V --format json`, `images [--filter label=K=V] --format json`,
    `rm -f -t 0 ID…` and `rmi REF…` (a name untags; the last name or the id removes the image),
    and records every argv in `calls`."""

    def __init__(self) -> None:
        self.containers: list[dict] = []
        self.images: list[dict] = []
        self.calls: list[list[str]] = []

    def container(self, ident: str, name: str, *, labels: dict | None = None, running: bool = False,
                  created: float = NOW) -> None:
        self.containers.append({"Id": ident, "Names": [name], "State": "running" if running else "exited",
                                "Labels": dict(labels or {}), "Created": int(created)})

    def image(self, ident: str, names: list[str], *, labels: dict | None = None, created: float = NOW,
              size: int = 1000) -> None:
        self.images.append({"Id": ident, "Names": list(names), "Labels": dict(labels or {}),
                            "Created": int(created), "Size": size})

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        command, rest = argv[0], argv[1:]
        wanted = [rest[i + 1].removeprefix("label=").partition("=") for i, x in enumerate(rest)
                  if x == "--filter" and i + 1 < len(rest) and rest[i + 1].startswith("label=")]

        def match(labels: dict) -> bool:
            return all((labels or {}).get(k) == v for k, _, v in wanted)

        if command == "ps":
            return self._done(json.dumps([c for c in self.containers if match(c["Labels"])]))
        if command == "images":
            return self._done(json.dumps([i for i in self.images if match(i["Labels"])]))
        if command == "rm":
            at = 0
            while at < len(rest) and rest[at].startswith("-"):
                at += 2 if rest[at] == "-t" else 1
            gone = set(rest[at:])
            self.containers = [c for c in self.containers if c["Id"] not in gone]
            return self._done("")
        if command == "rmi":
            code = 0
            for ref in (x for x in rest if not x.startswith("-")):
                image = next((i for i in self.images if ref in i["Names"] or i["Id"] == ref), None)
                if image is None:
                    code = 1
                    continue
                if ref in image["Names"] and len(image["Names"]) > 1:
                    image["Names"].remove(ref)
                else:
                    self.images.remove(image)
            return self._done("", code)
        return self._done("", 125)

    @staticmethod
    def _done(out: str, code: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], code, out, "")

    def removals(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] in ("rm", "rmi")]


def mine(session: str) -> dict[str, str]:
    return {diskuse.TEST_LABEL: "1", diskuse.SESSION_LABEL: session}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


# ------------------------------------------------------------------------------ worktrees

def test_release_worktree_removes_only_that_runs_worktree(app_repo, tmp_path):
    layer = tmp_path / "layer"
    for run in ("aaaa0001", "aaaa0002"):
        git(app_repo, "worktree", "add", "-q", "-b", f"al/{run}", str(layer / "trees" / run))
    (layer / "agents" / "aaaa0001").mkdir(parents=True)
    (layer / "agents" / "aaaa0001" / "state.json").write_text("{}\n")
    assert diskuse.release_worktree(layer, app_repo, "aaaa0001") == ("trees/aaaa0001", "")
    assert not (layer / "trees" / "aaaa0001").exists()
    listed = git(app_repo, "worktree", "list", "--porcelain")
    assert "aaaa0001" not in listed and "aaaa0002" in listed
    assert (layer / "trees" / "aaaa0002").is_dir() and (layer / "agents" / "aaaa0001" / "state.json").is_file()
    git(app_repo, "rev-parse", "--verify", "-q", "refs/heads/al/aaaa0001")


def test_release_worktree_without_a_worktree_is_a_no_op(app_repo, tmp_path):
    layer = tmp_path / "layer"
    (layer / "trees").mkdir(parents=True)
    assert diskuse.release_worktree(layer, app_repo, "aaaa0003") == ("", "")
    outside = layer / "x"
    outside.mkdir()
    (outside / "keep.txt").write_text("mine\n")
    assert diskuse.release_worktree(layer, app_repo, "../x") == ("", "not a run id")
    assert (outside / "keep.txt").read_text() == "mine\n"


# ------------------------------------------------------------------------------ the label shim

def test_label_shim_labels_build_run_and_create_only(tmp_path):
    real = tmp_path / "real" / "podman"
    real.parent.mkdir()
    real.write_text('#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done > "$ARGV_OUT"\n')
    real.chmod(0o755)
    shim = diskuse.write_label_shim(tmp_path / "bin", str(real), "4242-abcdef")
    assert shim == tmp_path / "bin" / "podman" and os.access(shim, os.X_OK)
    out = tmp_path / "argv.txt"
    labels = ["--label", "al.test=1", "--label", "al.test.session=4242-abcdef"]

    def argv_of(*args: str) -> list[str]:
        done = subprocess.run([str(shim), *args], env={**os.environ, "ARGV_OUT": str(out)},
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        return out.read_text().splitlines()

    assert argv_of("build", "-t", "al-x", ".") == ["build", *labels, "-t", "al-x", "."]
    assert argv_of("run", "-d", "img") == ["run", *labels, "-d", "img"]
    assert argv_of("create", "img") == ["create", *labels, "img"]
    assert argv_of("image", "build", ".") == ["image", "build", *labels, "."]
    assert argv_of("container", "run", "img") == ["container", "run", *labels, "img"]
    assert argv_of("container", "create", "img") == ["container", "create", *labels, "img"]
    assert argv_of("--log-level=error", "run", "img") == ["--log-level=error", "run", *labels, "img"]
    assert argv_of("run", "img", "sh", "-c", "echo 'a b'") == ["run", *labels, "img", "sh", "-c", "echo 'a b'"]
    for unchanged in (["ps"], ["rmi", "x"], ["images"], ["--version"], ["image", "ls"], ["container", "ps"]):
        assert argv_of(*unchanged) == unchanged


# ------------------------------------------------------------------------------ the session

def test_session_end_removes_its_own_labelled_objects_only(tmp_path):
    fake = FakePodman()
    fake.container("other1", "theirs", labels=mine("1-other"), running=True, created=NOW - 60)
    fake.image("plain1", ["localhost/al-orchard:latest"], created=NOW - 60)
    environ = {"PATH": "/usr/bin"}
    with diskuse.labelled_session(which=lambda name: "/opt/podman/bin/podman", run=fake, environ=environ,
                                  now=lambda: NOW) as session:
        assert session
        shim_dir = Path(environ["PATH"].split(os.pathsep)[0])
        assert (shim_dir / "podman").is_file() and environ["PATH"].endswith(os.pathsep + "/usr/bin")
        assert "/opt/podman/bin/podman" in (shim_dir / "podman").read_text()
        assert session in (shim_dir / "podman").read_text()
        fake.container("mine1", "ours", labels=mine(session), running=True)
        fake.image("mine2", ["localhost/al-sandbox-test:v2"], labels=mine(session))
        assert fake.removals() == []                       # nothing of this session is old yet
    assert environ == {"PATH": "/usr/bin"} and not shim_dir.exists()
    assert [c["Id"] for c in fake.containers] == ["other1"]
    assert [i["Id"] for i in fake.images] == ["plain1"]
    removed = " ".join(" ".join(argv) for argv in fake.removals())
    assert "mine1" in removed and "al-sandbox-test:v2" in removed
    assert "other1" not in removed and "al-orchard" not in removed and "plain1" not in removed


def test_session_start_removes_leftovers_older_than_six_hours():
    fake = FakePodman()
    fake.container("old1", "old", labels=mine("1-old"), created=NOW - 7 * HOUR)
    fake.image("old2", ["localhost/al-test-layer:1"], labels=mine("1-old"), created=NOW - 7 * HOUR)
    fake.container("new1", "recent", labels=mine("2-recent"), running=True, created=NOW - HOUR)
    fake.image("new2", ["localhost/al-test-layer:2"], labels=mine("2-recent"), created=NOW - HOUR)
    environ = {"PATH": "/usr/bin"}
    with diskuse.labelled_session(which=lambda name: "/usr/bin/podman", run=fake, environ=environ,
                                  now=lambda: NOW):
        assert [c["Id"] for c in fake.containers] == ["new1"]
        assert [i["Id"] for i in fake.images] == ["new2"]
    assert [c["Id"] for c in fake.containers] == ["new1"] and [i["Id"] for i in fake.images] == ["new2"]


def test_session_cleans_up_when_the_body_raises():
    fake = FakePodman()
    environ = {"PATH": "/usr/bin"}
    with pytest.raises(RuntimeError):
        with diskuse.labelled_session(which=lambda name: "/usr/bin/podman", run=fake, environ=environ,
                                      now=lambda: NOW) as session:
            fake.container("mine1", "ours", labels=mine(session), running=True)
            fake.image("mine2", ["localhost/al-prep-x:latest"], labels=mine(session))
            raise RuntimeError("a test session broke")
    assert environ == {"PATH": "/usr/bin"}
    assert fake.containers == [] and fake.images == []


def test_session_without_podman_changes_nothing():
    fake = FakePodman()
    environ = {"PATH": "/usr/bin"}
    with diskuse.labelled_session(which=lambda name: None, run=fake, environ=environ, now=lambda: NOW) as session:
        assert session == "" and environ == {"PATH": "/usr/bin"}
    assert environ == {"PATH": "/usr/bin"} and fake.calls == []


def test_remove_labelled_never_touches_a_kept_reference():
    fake = FakePodman()
    fake.image("kept1", ["localhost/al-orchard:latest"], labels=mine("1-a"))
    fake.image("gone1", ["localhost/al-x:1", "localhost/al-x:latest"], labels=mine("1-a"))
    fake.image("gone2", [], labels=mine("1-a"))
    removed = diskuse.remove_labelled(fake, keep={"al-orchard:latest"}, now=lambda: NOW)
    assert removed == ["localhost/al-x:1", "localhost/al-x:latest", "gone2"]
    assert [i["Id"] for i in fake.images] == ["kept1"]
    assert all(argv[0] != "rmi" or not any(x in ("--force", "-f") for x in argv) for argv in fake.calls)
    assert not any("al-orchard" in " ".join(argv) for argv in fake.removals())


# ------------------------------------------------------------------------------ al doctor's lines

def disk(total: float, free: float):
    return lambda path: SimpleNamespace(total=int(total), used=int(total - free), free=int(free))


def orchard(tmp_path: Path) -> tuple[Path, dict]:
    layer = tmp_path / "orchard.al"
    for run in ("aaaa0001", "aaaa0002"):
        (layer / "trees" / run).mkdir(parents=True)
        (layer / "trees" / run / "trees.py").write_text("TREES = []\n" * 400)
        (layer / "agents" / run).mkdir(parents=True)
        (layer / "agents" / run / "state.json").write_text("{}\n")
    (layer / "agents" / "aaaa0001" / "settlement.json").write_text(json.dumps({"as": "abandoned"}))
    (layer / "agents" / "stand-in").mkdir()                     # not a run record: no state.json
    ir = {"name": "orchard", "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees"}]}
    return layer, ir


def test_doctor_lines_report_every_category(tmp_path):
    layer, ir = orchard(tmp_path)
    fake = FakePodman()
    fake.image("i1", ["localhost/al-orchard:latest"], size=3 * GIB)
    fake.image("i2", ["localhost/al-orchard:0a0a0a0a0a0a"], size=2 * GIB)
    fake.image("i3", ["localhost/al-sandbox-test:v2"], labels=mine("1-a"), size=GIB)
    fake.image("i4", ["localhost/al-other:latest"], size=GIB)
    fake.image("i5", ["docker.io/library/python:3.12-slim"], size=GIB)
    fake.container("c1", "ours", labels=mine("1-a"), running=True)
    tmp = tmp_path / "tmp"
    (tmp / "pytest-of-ada" / "pytest-1").mkdir(parents=True)
    (tmp / "pytest-of-ada" / "pytest-1" / "x.txt").write_text("x" * 5000)
    report = diskuse.report(layer, ir, run=fake, disk_usage=disk(100 * GIB, 50 * GIB), tmp_dir=tmp, user="ada")
    lines = diskuse.lines(report)
    assert lines and all(line.startswith("disk: ") for line in lines) and "" not in lines
    text = "\n".join(lines)
    assert "disk: worktrees 2 (" in text and "1 of settled runs" in text
    assert "disk: run records 2 (" in text
    assert "layer 1 (3.0G)" in text and "old 1 (2.0G)" in text and "test 1 (1.0G)" in text and "other al 1 (1.0G)" in text
    assert "disk: test leftovers 1 containers (1 running), 1 images" in text
    assert "al sandbox LAYER prune --tests removes them" in text
    assert f"disk: pytest temp {tmp / 'pytest-of-ada'} (" in text
    assert "disk: free 50.0G of 100G (50%) on " in text
    assert "warning" not in text
    assert report["worktrees"] == {"count": 2, "bytes": report["worktrees"]["bytes"], "settled": 1}
    assert report["worktrees"]["bytes"] > 0 and report["pytest"]["bytes"] >= 5000
    assert report["images"]["layer"] == {"count": 1, "bytes": 3 * GIB}
    assert report["free"]["total"] == 100 * GIB and report["warning"] == ""


def test_doctor_warns_below_ten_percent_free(tmp_path):
    layer, ir = orchard(tmp_path)
    low = diskuse.lines(diskuse.report(layer, ir, disk_usage=disk(100 * GIB, 9 * GIB), tmp_dir=tmp_path, user="ada"))
    assert low[-1].startswith("disk: warning: only 9% free"), low
    edge = diskuse.report(layer, ir, disk_usage=disk(100 * GIB, 10 * GIB), tmp_dir=tmp_path, user="ada")
    assert edge["warning"] == "" and not any("warning" in line for line in diskuse.lines(edge))


def test_doctor_without_podman_says_so(tmp_path):
    layer, ir = orchard(tmp_path)
    report = diskuse.report(layer, ir, run=None, disk_usage=disk(100 * GIB, 50 * GIB), tmp_dir=tmp_path, user="ada")
    assert report["images"] is None and report["leftovers"] is None
    text = "\n".join(diskuse.lines(report))
    assert "disk: images: podman not found" in text
    for start in ("disk: worktrees 2", "disk: run records 2", "disk: pytest temp ", "disk: free 50.0G of 100G"):
        assert start in text, text
