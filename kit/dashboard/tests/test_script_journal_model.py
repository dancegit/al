"""A script step with a waiting exit and a journal of its own: the dashboard shows the run as
`waiting` with the command that resumes it, and the script's inner steps (mirrored from its
journal as `script.event` events) under the script step, with their tokens, cost and documents.
Two runs in a layer built in tmp_path; the event shapes follow docs/DESIGN.md ("Run events")."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import dashboard as d
from conftest import start_server, stop_server
from fixtures import iso, rec, state

NAME = "suite-shop"
WAITING, PASSED = "5a170001", "5a170002"
IR = {
    "schemaVersion": 1, "name": NAME, "description": "A layer that runs a hand-built suite",
    "codebase": {"root": "../orbit", "defaultBranch": "main",
                 "commands": [{"name": "legacy-watch", "run": "uv run adws/adw_orbit.py watch"}]},
    "agents": [{"id": "mock", "kind": "mock"}],
    "reviews": [{"id": "human", "kind": "human"}],
    "adws": [{"name": "legacy-watch", "description": "PROPOSAL (al adopt): runs adws/adw_orbit.py watch",
              "agent": "mock", "classes": [], "environment": ["worktree"], "input": ["cli"], "review": "human",
              "trigger": ["manual"],
              "steps": [{"kind": "script", "name": "run", "cmd": "legacy-watch", "args": ["--adw-id", "$adw_id"],
                         "timeout": 0, "waitingExit": [75], "journal": ".work/adw/{adw_id}"},
                        {"kind": "review", "name": "review", "patch": "", "implement": ""},
                        {"kind": "ship", "name": "ship", "message": ""}]}],
}
T0 = 1_000.0


def _event(seq: int, at: float, type_: str, **fields) -> str:
    return json.dumps({"ts": iso(at).replace("+00:00", ".000+00:00"), "seq": seq, "type": type_, **fields})


def _foreign(at: float, kind: str, subject: str, **fields) -> dict:
    return {"time": iso(at).replace("+00:00", "Z"), "kind": kind, "subject": subject, **fields, "prev": "", "hash": "h"}


def _write_run(layer: Path, run_id: str, status: str, foreign: list[dict], odd: list[str] | None = None,
               documents: list[str] | None = None) -> None:
    run = layer / "agents" / run_id
    run.mkdir(parents=True)
    lines = [_event(1, T0, "run.start", adw="legacy-watch", adw_id=run_id, klass="chore", dry_run=False, resumed=False),
             _event(2, T0 + 1, "step.start", step="run", kind="script", attempt=1),
             _event(3, T0 + 1.5, "script.start", step="run", owner="", cmd="legacy-watch", log=f"agents/{run_id}/scripts/run.1.log")]
    for n, obj in enumerate(foreign, start=1):
        lines.append(_event(len(lines) + 1, T0 + 2 + n, "script.event", step="run", owner="run", line=n, foreign=obj))
    lines += odd or []
    end = T0 + 30
    step_status = "waiting" if status == "waiting" else "passed"
    lines += [_event(len(lines) + 1, end - 1, "script.end", step="run", owner="", cmd="legacy-watch", exit=75 if status == "waiting" else 0,
                     log=f"agents/{run_id}/scripts/run.1.log", seconds=27.5),
              _event(len(lines) + 2, end - 0.5, "step.end", step="run", kind="script", status=step_status, detail="", seconds=28.5),
              _event(len(lines) + 3, end, "run.end", status=status, exit=75 if status == "waiting" else 0)]
    (run / "events.jsonl").write_text("\n".join(lines) + "\n")
    docs = []
    for rel in documents or []:
        inner, file = rel.split("/")
        target = run / "scripts" / "run" / "journal" / inner / file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {inner} {file}\n" if file.endswith(".md") else '{"ok": true}\n')
        docs.append({"path": f"agents/{run_id}/scripts/run/journal/{inner}/{file}", "inner": inner, "file": file})
    records = [rec("run", "script", step_status, T0 + 1, end - 0.5,
                   "run: script legacy-watch exited 75 (declared as waiting); log x" if status == "waiting" else "exit 0",
                   attempts=1)]
    data = state(run_id, "legacy-watch", "watch the review queue", status, T0, end, records,
                 scripts={"run": {"cmd": "uv run adws/adw_orbit.py watch", "args": ["--adw-id", run_id],
                                  "exit": 75 if status == "waiting" else 0, "log": f"agents/{run_id}/scripts/run.1.log",
                                  "waiting": status == "waiting",
                                  "journal": {"path": f".work/adw/{run_id}", "found": True, "lines": len(foreign),
                                              "malformed": 0, "refused": ""},
                                  "documents": docs}})
    (run / "state.json").write_text(json.dumps(data))
    (run / "run.log").write_text("")


@pytest.fixture
def layer(tmp_path: Path) -> Path:
    layer = tmp_path / f"{NAME}.al"
    layer.mkdir()
    (layer / "agentic-layer.json").write_text(json.dumps(IR))
    _write_run(layer, WAITING, "waiting", [_foreign(T0 + 3, "watch.check", "review-queue", ready=False)])
    _write_run(layer, PASSED, "passed", [
        _foreign(T0 + 3, "watch.check", "review-queue", ready=True),
        _foreign(T0 + 4, "agent.start", "orbit-round-review", step="round-review"),
        _foreign(T0 + 9, "agent.end", "orbit-round-review", step="round-review",
                 usage={"input_tokens": 1200, "output_tokens": 340, "cache_read_input_tokens": 10}, cost_usd=0.02),
        {"type": "note", "step": "done", "tokens": {"in": 5, "out": 7}, "cost": 0.01}],
        documents=["round-review/prompt.md", "round-review/output.json"])
    return layer


def _detail(layer: Path, run_id: str) -> dict:
    model = d.Model([layer], [])
    model.discover()
    return model.run_detail(NAME, run_id)


def test_a_waiting_run_has_status_waiting_and_a_resume_hint(layer):
    detail = _detail(layer, WAITING)
    summary = detail["summary"]
    assert summary["status"] == "waiting"
    assert summary["waiting_step"]["name"] == "run" and summary["waiting_step"]["status"] == "waiting"
    assert summary["resume"] == f"uv run adws/adw_legacy-watch.py --resume --adw-id {WAITING}"
    node = next(n for n in detail["graph"]["nodes"] if n["step"] == "run")
    assert node["status"] == "waiting"
    assert "waiting_step" not in _detail(layer, PASSED)["summary"]


def test_script_events_become_inner_steps_under_the_script_step(layer):
    detail = _detail(layer, PASSED)
    inner = detail["scripts"]["run"]["inner"]
    assert [(i["kind"], i["subject"]) for i in inner] == [
        ("watch.check", "review-queue"), ("agent.start", "orbit-round-review"), ("agent.end", "orbit-round-review"),
        ("note", "done")]
    assert inner[0]["at"] == pytest.approx(T0 + 3) and inner[0]["fields"]["ready"] is True
    assert "time" not in inner[0]["fields"] and "kind" not in inner[0]["fields"]
    bars = [b for b in detail["timeline"]["bars"] if b["kind"] == "inner"]
    assert all(b["step"] == "run" and b["level"] == 1 for b in bars)
    ids = [b["id"] for b in bars]
    assert "inner:run:2" in ids and "inner:run:3" not in ids          # agent.start … agent.end is one bar
    pair = next(b for b in bars if b["id"] == "inner:run:2")
    assert pair["start"] == pytest.approx(T0 + 4) and pair["end"] == pytest.approx(T0 + 9)
    point = next(b for b in bars if b["id"] == "inner:run:1")
    assert point["start"] == point["end"]


def test_inner_usage_and_cost_are_shown_when_the_journal_has_them(layer):
    script = _detail(layer, PASSED)["scripts"]["run"]
    end = next(i for i in script["inner"] if i["kind"] == "agent.end")
    assert end["usage"] == {"in": 1200, "out": 340, "cache_read": 10, "cache_write": 0, "cost": 0.02}
    note = next(i for i in script["inner"] if i["kind"] == "note")
    assert note["usage"] == {"in": 5, "out": 7, "cache_read": 0, "cache_write": 0, "cost": 0.01}
    assert next(i for i in script["inner"] if i["kind"] == "watch.check")["usage"] is None
    assert script["usage"] == {"in": 1205, "out": 347, "cache_read": 10, "cache_write": 0, "cost": 0.03}


def test_inner_documents_are_listed_and_served(layer):
    loaded = d.load_layer(layer.resolve(), set())
    state = json.loads((layer / "agents" / PASSED / "state.json").read_text())
    docs = [e for e in d.run_documents(loaded, PASSED, state) if e["kind"] == "inner"]
    base = f"agents/{PASSED}/scripts/run/journal/round-review"
    assert [(e["path"], e["name"], e["step"]) for e in docs] == [
        (f"{base}/prompt.md", "round-review/prompt.md", "run"), (f"{base}/output.json", "round-review/output.json", "run")]
    entry, path = d.run_document(loaded, PASSED, f"{base}/prompt.md")
    assert entry["kind"] == "inner" and path.read_text() == "# round-review prompt.md\n"
    # a recorded document that points outside the run is not listed
    state["scripts"]["run"]["documents"].append({"path": "../../etc/passwd", "inner": "x", "file": "passwd"})
    assert not any("passwd" in e["path"] for e in d.run_documents(loaded, PASSED, state))


def test_unknown_foreign_fields_and_odd_lines_do_not_break_the_model(tmp_path):
    layer = tmp_path / f"{NAME}.al"
    layer.mkdir()
    (layer / "agentic-layer.json").write_text(json.dumps(IR))
    odd = [_event(90, T0 + 20, "script.event", step="run", owner="run", line=7, foreign=["not", "a", "dict"]),
           _event(91, T0 + 21, "script.event", step="run", owner="run", line=8, foreign={"what": {"deep": [1, 2]}}),
           _event(92, T0 + 22, "script.event", step="run", owner="run", line=9),
           _event(93, T0 + 23, "script.event", line=10, foreign={"kind": "lost"})]
    _write_run(layer, WAITING, "waiting", [{"kind": "x", "time": "not a time", "usage": "lots", "cost_usd": "free"}], odd=odd)
    detail = _detail(layer, WAITING)
    inner = detail["scripts"]["run"]["inner"]
    assert inner[0]["kind"] == "x" and inner[0]["at"] is not None and inner[0]["usage"] is None
    keyed = next(i for i in inner if i["line"] == 8)
    assert keyed["at"] == pytest.approx(T0 + 21) and keyed["kind"] == "" and "what" in keyed["fields"]
    assert all(i["line"] not in (7, 9) for i in inner)                    # not an object: skipped
    assert detail["summary"]["status"] == "waiting"


def test_the_run_api_exposes_inner_steps_and_waiting(layer):
    running = start_server(["--layer", str(layer)])
    try:
        waiting = running.json(f"/api/run?layer={NAME}&run={WAITING}")
        assert waiting["summary"]["status"] == "waiting" and waiting["summary"]["resume"].endswith(f"--adw-id {WAITING}")
        passed = running.json(f"/api/run?layer={NAME}&run={PASSED}")
        assert [i["kind"] for i in passed["scripts"]["run"]["inner"]][:2] == ["watch.check", "agent.start"]
        assert any(doc["kind"] == "inner" for doc in passed["documents"])
    finally:
        stop_server(running)
