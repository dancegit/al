"""The record of a run on another host, pulled to the home machine: what a delta carries, how it
is applied exactly once, and what the home machine refuses to write."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from adw_modules import events, ir, recordsync, state as state_module

RUN = "7a7a0001"
OTHER = "7a7a0002"
IR = {"name": "notes", "codebase": {"root": "..", "defaultBranch": "main"},
      "storage": {"id": "local", "kind": "local", "dir": "artefacts"}}


def _layer(root: Path) -> ir.Layer:
    (root / "layer").mkdir(parents=True, exist_ok=True)
    (root / "app").mkdir(exist_ok=True)
    return ir.Layer(IR, root / "layer", root / "app")


@pytest.fixture
def sides(tmp_path):
    """(the host's layer with a run in flight, the home layer with the run's own start state)."""
    host, home = _layer(tmp_path / "host"), _layer(tmp_path / "home")
    theirs = state_module.ADWState(adw_id=RUN, adw="change", prompt="tidy the notes")
    theirs.worktree = str(host.layer_dir / "trees" / RUN)
    theirs.plan = str(host.layer_dir / "specs" / f"plan-{RUN}-plan.md")
    theirs.artefacts = [{"id": "log", "url": (host.layer_dir / "artefacts" / RUN / "log.txt").as_uri()}]
    theirs.outputs = {"build": f"wrote {host.app_root}/app.py"}
    theirs.save(host.layer_dir)
    ours = state_module.ADWState(adw_id=RUN, adw="change", prompt="tidy the notes")
    ours.remote = {"host": "build-1", "phase": "remote"}
    ours.save(home.layer_dir)
    run = host.layer_dir / "agents" / RUN
    with events.recording(host.layer_dir, RUN):
        events.emit("run.start", adw="change")
        events.emit("step.start", step="plan")
    (run / "run.log").write_text("line one\n")
    (run / "plan").mkdir()
    (run / "plan" / "prompt.md").write_text("# plan\n")
    (run / "plan" / "raw.jsonl").write_text('{"n":1}\n')
    (run / "checks").mkdir()
    (run / "checks" / "test.1.log").write_text("ok\n")
    (host.layer_dir / "specs").mkdir()
    (host.layer_dir / "specs" / f"plan-{RUN}-plan.md").write_text("# Plan\n")
    (host.layer_dir / "specs" / "plan-of-someone-else.md").write_text("# not ours\n")
    (host.layer_dir / "artefacts" / RUN).mkdir(parents=True)
    (host.layer_dir / "artefacts" / RUN / "log.txt").write_text("artefact\n")
    return host, home


def _header(host: ir.Layer, status: str = "running", alive: bool = True) -> dict:
    return {"run": RUN, "layer_dir": str(host.layer_dir), "app_root": str(host.app_root), "status": status, "alive": alive}


def _pull(host: ir.Layer, home: ir.Layer, **header) -> dict:
    frame = recordsync.delta(recordsync.run_roots(host, RUN), recordsync.load_cursor(home, RUN), _header(host, **header))
    return recordsync.apply(home, RUN, frame)


def _paths(frame: bytes) -> dict[str, dict]:
    return {entry["path"]: entry for entry, _data in recordsync.parse(frame)[1]}


def test_first_delta_carries_everything_and_the_second_only_what_changed(sides):
    host, home = sides
    first = _pull(host, home)
    assert sorted(first["changed"]) == sorted([
        f"agents/{RUN}/state.json", f"agents/{RUN}/events.jsonl", f"agents/{RUN}/run.log", f"agents/{RUN}/plan/prompt.md",
        f"agents/{RUN}/plan/raw.jsonl", f"agents/{RUN}/checks/test.1.log", f"artefacts/{RUN}/log.txt",
        f"specs/plan-{RUN}-plan.md"])
    assert (home.layer_dir / "specs" / f"plan-{RUN}-plan.md").read_text() == "# Plan\n"
    assert not (home.layer_dir / "specs" / "plan-of-someone-else.md").exists()
    assert (home.layer_dir / "artefacts" / RUN / "log.txt").read_text() == "artefact\n"
    assert _pull(host, home)["changed"] == []
    with (host.layer_dir / "agents" / RUN / "run.log").open("a") as log:
        log.write("line two\n")
    (host.layer_dir / "agents" / RUN / "plan" / "output.md").write_text("done\n")
    second = _pull(host, home)
    assert sorted(second["changed"]) == [f"agents/{RUN}/plan/output.md", f"agents/{RUN}/run.log"]
    assert second["bytes"] == len("line two\n") + len("done\n")
    assert (home.layer_dir / "agents" / RUN / "run.log").read_text() == "line one\nline two\n"


def test_append_only_files_travel_as_ranges_and_are_never_duplicated(sides):
    host, home = sides
    path = f"agents/{RUN}/events.jsonl"
    cursor = recordsync.load_cursor(home, RUN)
    first = recordsync.delta(recordsync.run_roots(host, RUN), cursor, _header(host))
    assert _paths(first)[path]["mode"] == "append" and _paths(first)[path]["offset"] == 0
    recordsync.apply(home, RUN, first)
    recordsync.apply(home, RUN, first)            # the same answer twice
    with events.recording(host.layer_dir, RUN):
        events.emit("step.end", step="plan", status="passed")
    stale = recordsync.load_cursor(home, RUN)
    lost = recordsync.delta(recordsync.run_roots(host, RUN), stale, _header(host))   # an answer that never arrived …
    assert _paths(lost)[path]["offset"] > 0
    with events.recording(host.layer_dir, RUN):
        events.emit("step.start", step="build")
    again = recordsync.delta(recordsync.run_roots(host, RUN), stale, _header(host))  # … so the same cursor asks again
    recordsync.apply(home, RUN, again)
    recordsync.apply(home, RUN, lost)             # and the late answer turns up after all
    _pull(host, home)
    ours = (home.layer_dir / "agents" / RUN / "events.jsonl").read_bytes()
    assert ours == (host.layer_dir / "agents" / RUN / "events.jsonl").read_bytes()
    seqs = [event["seq"] for event in events.read(home.layer_dir / "agents" / RUN / "events.jsonl")]
    assert seqs == [1, 2, 3, 4]


@pytest.mark.parametrize("home_bytes", [b"line", b"line one\nand something else\n"])
def test_a_range_is_applied_only_at_its_offset(sides, home_bytes):
    host, home = sides
    _pull(host, home)
    log = home.layer_dir / "agents" / RUN / "run.log"
    log.write_bytes(home_bytes)                   # the home file is not where the cursor says it is
    with (host.layer_dir / "agents" / RUN / "run.log").open("a") as handle:
        handle.write("line two\n")
    assert f"agents/{RUN}/run.log" not in _pull(host, home)["changed"]
    assert log.read_bytes() == home_bytes         # the range was not written …
    assert recordsync.load_cursor(home, RUN)[f"agents/{RUN}/run.log"]["size"] == len(home_bytes)   # … and the cursor asks from here
    if len(home_bytes) < len("line one\nline two\n"):
        _pull(host, home)
        assert log.read_text() == "line one\nline two\n"


def test_whole_files_replace_atomically_and_a_half_written_state_is_never_seen(sides, monkeypatch):
    host, home = sides
    _pull(host, home)
    target = home.layer_dir / "agents" / RUN / "state.json"
    before = target.read_text()
    replaced: list[tuple[str, str]] = []
    real = os.replace

    def watching(source, destination):
        if str(destination) == str(target):
            assert target.read_text() == before          # whole until the moment it is replaced
            assert Path(source).parent == target.parent  # a temporary file in the same directory
            json.loads(Path(source).read_text())
            replaced.append((str(source), str(destination)))
        return real(source, destination)

    monkeypatch.setattr(os, "replace", watching)
    theirs = state_module.load(host.layer_dir, RUN)
    theirs.klass = "bug"
    theirs.save(host.layer_dir)
    _pull(host, home)
    assert replaced and json.loads(target.read_text())["klass"] == "bug"
    assert [p.name for p in target.parent.iterdir() if p.name.startswith(".sync.")] == []


def test_credentials_and_objects_are_never_in_a_delta(sides):
    host, home = sides
    run = host.layer_dir / "agents" / RUN
    for folder, name in (("credentials", "claude.json"), ("objects/ab", "cdef"), ("incoming.git", "HEAD"), (".remote", "cursor.json")):
        (run / folder).mkdir(parents=True)
        (run / folder / name).write_text("secret\n")
    frame = recordsync.delta(recordsync.run_roots(host, RUN), {}, _header(host))
    assert b"secret" not in frame
    assert not [p for p in _paths(frame) if "credentials" in p or "objects" in p or "incoming.git" in p or ".remote/" in p]
    recordsync.apply(home, RUN, frame)
    assert not (home.layer_dir / "agents" / RUN / "credentials").exists()
    assert not (home.layer_dir / "agents" / RUN / "objects").exists()
    # and a host that sends them anyway is refused
    for path in (f"agents/{RUN}/credentials/claude.json", f"agents/{RUN}/objects/ab/cdef"):
        forged = json.dumps({"protocol": recordsync.PROTOCOL, **_header(host), "files": [
            {"path": path, "mode": "whole", "offset": 0, "bytes": 1}]}).encode() + b"\nx"
        with pytest.raises(recordsync.Refused):
            recordsync.apply(home, RUN, forged)


def _forged(host: ir.Layer, path: str, data: bytes = b"x") -> bytes:
    head = {"protocol": recordsync.PROTOCOL, **_header(host),
            "files": [{"path": path, "mode": "whole", "offset": 0, "bytes": len(data)}]}
    return json.dumps(head).encode() + b"\n" + data


@pytest.mark.parametrize("path", [f"agents/{RUN}/../../../escape.txt", "/etc/al-escape", f"agents/{OTHER}/state.json",
                                  "specs/plan-of-someone-else.md", f"specs/sub/{RUN}.md", "adws/adw_modules/runner.py",
                                  f"agents/{RUN}", "agentic-layer.json", f"artefacts/{OTHER}/log.txt"])
def test_paths_outside_the_run_are_refused(sides, tmp_path, path):
    host, home = sides
    with pytest.raises(recordsync.Refused):
        recordsync.apply(home, RUN, _forged(host, path))
    assert not (tmp_path / "escape.txt").exists() and not (home.layer_dir / "agents" / OTHER).exists()
    assert not (home.layer_dir / "adws").exists()


def test_a_symlink_on_the_host_or_at_home_is_never_followed(sides, tmp_path):
    host, home = sides
    secret = tmp_path / "secret.txt"
    secret.write_text("the host's private file\n")
    (host.layer_dir / "agents" / RUN / "link.txt").symlink_to(secret)
    (host.layer_dir / "agents" / RUN / "linked").symlink_to(tmp_path)
    frame = recordsync.delta(recordsync.run_roots(host, RUN), {}, _header(host))
    assert b"private file" not in frame and not [p for p in _paths(frame) if "link" in p]
    # at home: a directory of the run that is a link out of it is not written through
    outside = tmp_path / "outside"
    outside.mkdir()
    (home.layer_dir / "agents" / RUN / "plan").symlink_to(outside)
    for path in (f"agents/{RUN}/plan/prompt.md", f"agents/{RUN}/plan/deeper/down/prompt.md"):
        with pytest.raises(recordsync.Refused):
            recordsync.apply(home, RUN, _forged(host, path))
    assert list(outside.iterdir()) == []                      # not a file, not even a directory
    # and one bad path refuses the whole answer: the good file next to it is not written either
    head = {"protocol": recordsync.PROTOCOL, **_header(host), "files": [
        {"path": f"agents/{RUN}/review/prompt.md", "mode": "whole", "offset": 0, "bytes": 1},
        {"path": "../outside.txt", "mode": "whole", "offset": 0, "bytes": 1}]}
    with pytest.raises(recordsync.Refused):
        recordsync.apply(home, RUN, json.dumps(head).encode() + b"\nxy")
    assert not (home.layer_dir / "agents" / RUN / "review").exists()


def test_state_paths_are_rewritten_to_this_machine(sides):
    host, home = sides
    _pull(host, home)
    ours = json.loads((home.layer_dir / "agents" / RUN / "state.json").read_text())
    assert ours["worktree"] == str(home.layer_dir / "trees" / RUN)
    assert ours["plan"] == str(home.layer_dir / "specs" / f"plan-{RUN}-plan.md")
    assert ours["artefacts"][0]["url"] == (home.layer_dir / "artefacts" / RUN / "log.txt").as_uri()
    assert ours["outputs"]["build"] == f"wrote {home.app_root}/app.py"
    assert str(host.layer_dir) not in json.dumps(ours) and str(host.app_root) not in json.dumps(ours)
    # the events are the host's bytes, whatever paths they name
    with events.recording(host.layer_dir, RUN):
        events.emit("step.end", step="plan", detail=str(host.layer_dir / "specs"))
    _pull(host, home)
    assert ((home.layer_dir / "agents" / RUN / "events.jsonl").read_bytes()
            == (host.layer_dir / "agents" / RUN / "events.jsonl").read_bytes())


def test_home_keeps_its_own_remote_block_and_running_status(sides):
    host, home = sides
    theirs = state_module.load(host.layer_dir, RUN)
    theirs.remote = {"host": "somewhere-else", "base": "0" * 40, "delivered": {"sha": "1" * 40, "branch": f"chore/{RUN}-x"}}
    theirs.status, theirs.pid = "delivered", 4242
    theirs.save(host.layer_dir)
    result = _pull(host, home, status="delivered", alive=False)
    assert result["status"] == "delivered" and result["alive"] is False
    ours = json.loads((home.layer_dir / "agents" / RUN / "state.json").read_text())
    assert ours["status"] == "running" and ours["ended"] == ""
    assert ours["remote"]["host"] == "build-1" and ours["remote"]["phase"] == "gate"
    assert ours["remote"]["status"] == "delivered" and ours["remote"]["delivered"]["sha"] == "1" * 40
    assert ours["pid"] == os.getpid() and ours["remote"]["last_seen"]
    state_module.load(home.layer_dir, RUN)     # still a run state


def test_a_run_that_pushed_three_times_restarts_from_the_third(sides):
    from adw_modules import remoterun

    host, home = sides
    branch = f"chore/{RUN}-tidy"
    for i, sha in enumerate(("a" * 40, "b" * 40, "c" * 40), start=1):
        theirs = state_module.load(host.layer_dir, RUN)
        theirs.remote = {**theirs.remote, "branch": branch,
                         "pushed": {"sha": sha, "step": f"s{i}", "at": f"2026-01-0{i}T00:00:00+00:00"}}
        theirs.save(host.layer_dir)
        _pull(host, home)
    ours = state_module.load(home.layer_dir, RUN)       # then the host stops answering
    ours.status = "lost"
    ours.remote = {**ours.remote, "lost": {"at": state_module.now(), "reason": "no answer for 120 s"}}
    ours.save(home.layer_dir)
    kept = json.loads((home.layer_dir / "agents" / RUN / "state.json").read_text())
    assert kept["remote"]["pushed"]["sha"] == "c" * 40
    assert kept["remote"]["host"] == "build-1"          # what only the home machine decides stays its own
    run_dir = home.layer_dir / "agents" / RUN
    (run_dir / "pushed-state.json").write_text(json.dumps({"adw_id": RUN, "steps": []}))
    assert remoterun._pushed(home, state_module.load(home.layer_dir, RUN))["sha"] == "c" * 40


def test_a_state_of_another_run_or_workflow_is_refused(sides):
    host, home = sides
    before = (home.layer_dir / "agents" / RUN / "state.json").read_text()
    other_run = state_module.ADWState(adw_id=OTHER, adw="change")
    other_adw = state_module.ADWState(adw_id=RUN, adw="deploy-everything")
    for forged in (other_run, other_adw):
        data = json.dumps({**forged.__dict__}).encode()
        with pytest.raises(recordsync.Refused):
            recordsync.apply(home, RUN, _forged(host, f"agents/{RUN}/state.json", data))
    for data in (b"not json", b"[]", json.dumps({"adw_id": RUN, "adw": "change", "surprise": 1}).encode()):
        with pytest.raises(recordsync.Refused):
            recordsync.apply(home, RUN, _forged(host, f"agents/{RUN}/state.json", data))
    wrong_run = json.dumps({"protocol": recordsync.PROTOCOL, **{**_header(host), "run": OTHER}, "files": []}).encode() + b"\n"
    with pytest.raises(recordsync.Refused):
        recordsync.apply(home, RUN, wrong_run)
    assert (home.layer_dir / "agents" / RUN / "state.json").read_text() == before


def test_a_reply_is_capped_and_the_rest_comes_with_the_next_pull(sides):
    host, home = sides
    big = host.layer_dir / "agents" / RUN / "plan" / "raw.jsonl"
    big.write_bytes(b"x" * 5000)
    cursor: dict = {}
    frame = recordsync.delta(recordsync.run_roots(host, RUN), cursor, _header(host), limit=2048)
    header, files = recordsync.parse(frame)
    assert header["more"] is True and sum(len(data) for _entry, data in files) <= 2048
    assert files[0][0]["path"] == f"agents/{RUN}/state.json"     # what the run is doing now is never held back
    for _ in range(10):
        if not _pull_limited(host, home, 2048)["more"]:
            break
    assert (home.layer_dir / "agents" / RUN / "plan" / "raw.jsonl").read_bytes() == big.read_bytes()


def _pull_limited(host: ir.Layer, home: ir.Layer, limit: int) -> dict:
    frame = recordsync.delta(recordsync.run_roots(host, RUN), recordsync.load_cursor(home, RUN), _header(host), limit=limit)
    return recordsync.apply(home, RUN, frame)


def test_a_log_that_was_written_anew_on_the_host_is_sent_whole(sides):
    host, home = sides
    _pull(host, home)
    (host.layer_dir / "agents" / RUN / "checks" / "test.1.log").write_text("a different, longer log\n")
    _pull(host, home)
    assert (home.layer_dir / "agents" / RUN / "checks" / "test.1.log").read_text() == "a different, longer log\n"
