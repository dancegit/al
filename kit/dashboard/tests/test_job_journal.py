"""The job journal: an agent step's raw stream condensed once, while the job runs, into
`steps/<name>.journal.jsonl` beside the step's full log `steps/<name>.log`; the job's view built from
them (`job_detail`: what each step is doing, the outcome, the author's report) and the read-only routes
that page through them (`/api/job/journal`, `/api/job/log`) or download them (`/api/job/file`). The
recorded streams are invented (data/job-streams/); the al tool is `fake_al`."""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

import dashboard as d
import workspace_fixtures as wf
from conftest import SSEReader
from test_actions import confirmed, job, post, wait_job

STREAMS = wf.STREAMS


def fed(name: str, short: d.Shortener | None = None, text: str | None = None) -> tuple[d.AgentJournal, list[dict]]:
    """Every journal event of the recorded stream `name`, fed line by line, with finish()."""
    journal = d.AgentJournal(short)
    events: list[dict] = []
    for line in (text if text is not None else (STREAMS / f"{name}.jsonl").read_text()).splitlines(keepends=True):
        events += journal.feed_line(line)
    return journal, events + journal.finish()


# --- the condenser ------------------------------------------------------------------------------------

def test_hook_and_system_events_are_hidden():
    raw = (STREAMS / "author-running.jsonl").read_text()
    _journal, events = fed("author-running")
    hidden = [e for e in events if e["kind"] == "hidden"]
    whats = [e["what"] for e in hidden]
    assert whats.count("hook") == 4 and "init" in whats and "rate" in whats
    for line in raw.splitlines():
        obj = json.loads(line)
        if obj.get("type") == "system" or obj.get("type") == "rate_limit_event":
            assert not any(e["kind"] != "hidden" and obj.get("hook_name", "\0") in e.get("text", "") for e in events)
    assert all(d.journal_line(e) is None for e in hidden)
    assert all(e["t"] and isinstance(e["text"], str) for e in events)
    journal_bytes = sum(len(json.dumps(e)) + 1 for e in events)
    assert journal_bytes < len(raw.encode()) / 10, (journal_bytes, len(raw))
    assert "fernhook-sentinel" not in "\n".join(filter(None, map(d.journal_line, events)))


def test_messages_tool_calls_and_results_are_condensed():
    clock = iter(range(1000, 2000))
    journal = d.AgentJournal(clock=lambda: float(next(clock)))
    events = []
    for line in (STREAMS / "author-running.jsonl").read_text().splitlines():
        events += journal.feed_line(line)
    messages = [e for e in events if e["kind"] == "message"]
    assert messages[0]["text"].startswith("I will read the codebase facts first")
    reads = [e for e in events if e["kind"] == "tool" and e["tool"] == "Read"]
    assert len(reads) == 2 and reads[1]["target"].endswith(".github/workflows/ci.yml")
    assert reads[1]["text"].startswith("read ") and reads[1]["id"] == "t2"
    done = next(e for e in events if e["kind"] == "tool.done" and e["id"] == "t2")
    assert done["bytes"] > 0 and done["lines"] == 8 and done["ms"] >= 0 and done["error"] is False
    assert d.journal_line(reads[1]).startswith("  · read ")
    assert d.journal_line(messages[0]) == "  I will read the codebase facts first, then the CI files, and write the definition for **fern**."
    assert d.journal_line(done) is None


def test_tool_errors_and_error_results_are_errors():
    _journal, events = fed("author-failed")
    failed_tools = [e for e in events if e["kind"] == "tool.done" and e["error"]]
    assert len(failed_tools) == 3
    errors = [e for e in events if e["kind"] == "error"]
    tool_errors = [e for e in errors if e["text"].startswith("agentic.rkt:14:5: cmd: unknown keyword")]
    assert len(tool_errors) == 3                                     # one per failed compile
    results = [e for e in events if e["kind"] == "result"]
    assert len(results) == 1 and results[0]["error"] is True and results[0]["turns"] == 30
    assert errors[-1]["text"].startswith("the agent ended with error_max_turns; its last error: agentic.rkt:14:5")
    assert d.journal_line(failed_tools[0]) == "  ! Bash failed"
    assert d.journal_line(errors[0]).startswith("  ! ")
    assert d.journal_line(results[0]).startswith("  = 30 turns, 5m 01s, $0.90")


def test_doing_names_ci_reads_and_compile_attempts():
    _journal, events = fed("author-finished")
    doing = [e["doing"] for e in events if e["kind"] == "tool"]
    assert doing[0] == "reading the codebase facts (LLM.txt)"
    assert doing.index("reading the CI files") < doing.index("compiling, attempt 1") < doing.index("compiling, attempt 2")
    assert "writing agentic.rkt" in doing


def test_plain_and_corrupt_lines():
    text = 'the agent says hello\nsecond plain line\n{"type": "assistant", "message": {"content": [{"type": "te\n'
    journal, events = fed("", text=text)
    kinds = [e["kind"] for e in events]
    assert kinds[:2] == ["message", "message"] and events[0]["text"] == "the agent says hello"
    assert kinds[2] == "error" and "unreadable" in events[2]["text"]
    assert kinds[-1] == "report" and events[-1]["text"] == "second plain line"
    assert journal.finish() == []                                    # a report is made once
    _journal, finished = fed("author-finished")
    assert [e["kind"] for e in finished].count("report") == 1        # a result's report: finish() adds none


def test_home_paths_are_shown_as_tilde(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    stream = wf.job_stream("author-finished", workspace)
    short = d.Shortener([(str(workspace.resolve() / "fern.al"), "ws/fern.al"), (str(workspace.resolve() / "fern"), "ws/fern")])
    _journal, events = fed("", short=short, text=stream.read_text())
    blob = json.dumps(events)
    assert str(Path.home()) not in blob and "~/al/tools/al compile" in blob
    assert str(workspace.resolve()) not in blob and "ws/fern.al" in blob


def test_report_sections_are_parsed():
    report = (STREAMS / "author-finished.jsonl").read_text().splitlines()[-1]
    parsed = d.parse_report(json.loads(report)["result"])
    assert len(parsed["decisions"]) == 3 and parsed["decisions"][0].startswith("The test command is")
    assert len(parsed["left_out"]) == 2 and parsed["left_out"][1].startswith("UNVERIFIED")
    bold = "Done.\n\n**Decisions:**\n1. one\n2. two\n   continued\n\n**Left out**\n* skipped this\n\nnext: al gen x\n"
    assert d.parse_report(bold) == {"decisions": ["one", "two continued"], "left_out": ["skipped this"]}
    plain = "Key decisions:\n- a\n- b\nWhat was left out:\n- c\n## Other\n- not this\n"
    assert d.parse_report(plain) == {"decisions": ["a", "b"], "left_out": ["c"]}
    assert d.parse_report("All done, nothing to say.\n- a bullet without a heading\n") == {"decisions": [], "left_out": []}
    assert all(len(item) <= 600 for item in d.parse_report("### Decisions\n- " + "x" * 2000 + "\n")["decisions"])


# --- the job writer -----------------------------------------------------------------------------------

@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    wf.build(root)
    return root


def create_job(running, al: Path) -> str:
    return confirmed(running, "fern", "create")


def lines_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_runner_writes_step_logs_and_an_agent_journal(ws, tmp_path, workspace_server):
    stream = wf.job_stream("author-finished", ws)
    al = wf.fake_al(tmp_path / "bin" / "al", out="plain step output", streams={"author": stream})
    running = workspace_server(ws, al=al)
    job_id = create_job(running, al)
    assert wait_job(running, job_id)["status"] == "passed"
    steps = tmp_path / "jobs" / job_id / "steps"
    assert sorted(p.name for p in steps.iterdir()) == ["author.journal.jsonl", "author.log", "check.log", "gen.log", "init.log"]
    assert (steps / "author.log").read_bytes() == stream.read_bytes()
    assert (steps / "init.log").read_text() == "plain step output\n"
    events = lines_of(steps / "author.journal.jsonl")
    kinds = {e["kind"] for e in events}
    assert {"message", "tool", "tool.done", "report", "result", "hidden"} <= kinds
    assert all({"t", "kind", "text"} <= set(e) for e in events)
    assert oct((steps).stat().st_mode & 0o777) == oct(0o700)
    record = json.loads((tmp_path / "jobs" / job_id / "job.json").read_text())
    assert [s.get("stream") for s in record["steps"]] == ["", "agent", "", ""]
    assert record["steps"][1]["argv"][-1] == "--raw"
    assert str(Path.home()) not in (steps / "author.journal.jsonl").read_text()


def test_output_log_holds_condensed_lines_not_the_stream(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", out="plain step output",
                    streams={"author": wf.job_stream("author-finished", ws)})
    running = workspace_server(ws, al=al)
    job_id = create_job(running, al)
    wait_job(running, job_id)
    output = (tmp_path / "jobs" / job_id / "output.log").read_text()
    assert not any(line.lstrip().startswith('{"type":') or '"type":' in line for line in output.splitlines())
    assert "  · read " in output and "plain step output" in output
    assert "  = 14 turns, 3m 04s, $0.42" in output and "fernhook-sentinel" not in output
    assert output.count("plain step output") == 3                     # init, gen and check
    assert len(output) < 20_000


def test_a_retried_step_starts_its_files_afresh(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", streams={"author": wf.job_stream("author-failed", ws)},
                    fail={"author": (1, "al: author: the agent exited 1")})
    running = workspace_server(ws, al=al)
    job_id = create_job(running, al)
    assert wait_job(running, job_id)["status"] == "failed"
    steps = tmp_path / "jobs" / job_id / "steps"
    first_log = (steps / "author.log").read_bytes()
    assert b"error_max_turns" in first_log
    errors = [e for e in lines_of(steps / "author.journal.jsonl") if e["kind"] == "error"]
    assert "al: author: the agent exited 1" in [e["text"] for e in errors]   # an `al: …` stderr line is an error too
    wf.fake_al(al, streams={"author": wf.job_stream("author-finished", ws)})
    status, body = post(running, {"job": job_id, "from": "author"}, "/api/job/retry")
    assert status == 202, body
    assert wait_job(running, job_id)["status"] == "passed"
    assert b"error_max_turns" not in (steps / "author.log").read_bytes()
    assert "error_max_turns" not in (steps / "author.journal.jsonl").read_text()
    assert (steps / "author.log").read_bytes() == wf.job_stream("author-finished", ws).read_bytes()
    output = (tmp_path / "jobs" / job_id / "output.log").read_text()
    assert "--- retry from author" in output
    assert "the agent ended with error_max_turns" in output and "  = 14 turns" in output   # both attempts


# --- the job's view and its routes --------------------------------------------------------------------

def stop_runner(tmp_path: Path, job_id: str) -> None:
    record = json.loads((tmp_path / "jobs" / job_id / "job.json").read_text())
    if record.get("pid"):
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except OSError:
            pass


def finished_job(ws: Path, tmp_path: Path, workspace_server, name: str = "author-finished", **fail) -> tuple:
    al = wf.fake_al(tmp_path / "bin" / "al", out="plain step output", streams={"author": wf.job_stream(name, ws)}, **fail)
    running = workspace_server(ws, al=al)
    job_id = create_job(running, al)
    return running, job_id, wait_job(running, job_id)


def test_job_detail_running_step_says_what_it_does(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", streams={"author": wf.job_stream("author-running", ws)}, hold={"author": 30})
    running = workspace_server(ws, al=al)
    events = SSEReader(running.port, "/api/events")
    try:
        job_id = create_job(running, al)
        view = wait_job(running, job_id, until=lambda j: j["steps"][1]["doing"] == "writing agentic.rkt")
        author = view["steps"][1]
        assert author["status"] == "running" and author["stream"] == "agent" and author["activity"] == "journal"
        assert author["raw"] is True and author["log_bytes"] > 50_000
        assert view["steps"][0]["status"] == "passed" and view["steps"][0]["doing"] == ""
        assert view["outcome"] is None and view["has_steps_dir"] is True
        data, _ = events.wait_for("job", lambda raw: json.loads(raw).get("doing") == "writing agentic.rkt", timeout=15)
        live = json.loads(data)
        assert live["id"] == job_id and live["steps"][1]["doing"] == "writing agentic.rkt"
        assert str(tmp_path) not in json.dumps(view) and str(Path.home()) not in json.dumps(view)
    finally:
        events.close()
        stop_runner(tmp_path, job_id)


def test_job_detail_outcome_of_a_finished_job(ws, tmp_path, workspace_server):
    running, job_id, view = finished_job(ws, tmp_path, workspace_server)
    assert view["status"] == "passed"
    outcome = view["outcome"]
    assert outcome["status"] == "passed" and outcome["created"]["layer"] == "ws/fern.al"
    assert outcome["unverified"] == [i for i in view["report"]["left_out"] if "UNVERIFIED" in i] and len(outcome["unverified"]) == 1
    assert outcome["next"] == "al gen ws/fern.al"
    assert len(view["report"]["decisions"]) == 3 and len(view["report"]["left_out"]) == 2
    assert view["needs_person"] == 2 and view["output_bytes"] > 0
    assert [s["activity"] for s in view["steps"]] == ["log", "journal", "log", "log"]
    assert [s["raw"] for s in view["steps"]] == [False, True, False, False]
    assert str(tmp_path) not in json.dumps(view) and str(Path.home()) not in json.dumps(view)


def test_job_detail_outcome_of_a_failed_job(ws, tmp_path, workspace_server):
    _running, _job_id, view = finished_job(ws, tmp_path, workspace_server, "author-failed", fail={"author": (1, "")})
    assert view["status"] == "failed"
    outcome = view["outcome"]
    assert outcome["step"] == "author" and outcome["retry_from"] == "author" and outcome["log"] == "author.log"
    assert view["steps"][1]["reason"] == "author exited 1; see the output"
    assert outcome["reason"].startswith("the agent ended with error_max_turns; its last error: agentic.rkt:14:5")
    assert "exited 1" not in outcome["reason"] and "\n" not in outcome["reason"]
    assert 0 < len(outcome["lines"]) <= 12 and any("unknown keyword" in line for line in outcome["lines"])
    assert view["report"] is None and view["needs_person"] == 0


def test_journal_and_log_routes_page_with_from(ws, tmp_path, workspace_server, monkeypatch):
    monkeypatch.setattr(d, "JOURNAL_PAGE", 7)
    monkeypatch.setattr(d, "LOG_PAGE_LINES", 2)
    home_line = f"wrote {Path.home()}/notes/x.txt"
    al = wf.fake_al(tmp_path / "bin" / "al", lines=["\x1b[32mgreen\x1b[0m", home_line, "third", "fourth", "fifth"],
                    streams={"author": wf.job_stream("author-finished", ws)})
    running = workspace_server(ws, al=al)
    job_id = create_job(running, al)
    wait_job(running, job_id)
    steps = tmp_path / "jobs" / job_id / "steps"
    seen, start = [], 0
    while True:
        page = running.json(f"/api/job/journal?id={job_id}&step=author&from={start}")
        assert len(page["events"]) <= 7
        seen += page["events"]
        if page["next_from"] == start:
            break
        start = page["next_from"]
    assert seen == lines_of(steps / "author.journal.jsonl")
    seen_lines, start = [], 0
    while True:
        page = running.json(f"/api/job/log?id={job_id}&step=init&from={start}")
        assert len(page["lines"]) <= 2 and page["total"] == 5
        seen_lines += page["lines"]
        if page["next_from"] == start:
            break
        start = page["next_from"]
    assert seen_lines == ["green", "wrote ~/notes/x.txt", "third", "fourth", "fifth"]
    tail = running.json(f"/api/job/log?id={job_id}&step=init&tail=2")
    assert tail["from"] == 3 and tail["lines"] == ["fourth", "fifth"] and tail["next_from"] == 5
    assert running.get(f"/api/job/journal?id={job_id}&step=init&from=0")[0] == 404     # a plain step has no journal
    assert running.json(f"/api/job/log?id={job_id}&step=author&from=0")["total"] == len((steps / "author.log").read_text().splitlines())


def test_job_file_route_refusals(ws, tmp_path, workspace_server):
    running, job_id, _view = finished_job(ws, tmp_path, workspace_server)
    other = start_scout(running)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n")
    steps = tmp_path / "jobs" / job_id / "steps"
    (steps / "gen.log").unlink()
    os.symlink(secret, steps / "gen.log")
    for name in ("../job.json", "job.json", "..%2Fjob.json", "steps/..%2F..%2Fjob.json", "/etc/passwd", f"{steps}/author.log",
                 "steps/author.log%00", "%00", "", "steps/nope.log", "steps/gen.log", "steps/author.txt",
                 f"../{other}/output.log", "docs/../job.json", "steps", "steps/"):
        status, _headers, body = running.get(f"/api/job/file?id={job_id}&name={name}")
        assert status == 404 and b"secret" not in body, name
    for route in ("journal", "log"):
        for step in ("nope", "../author", "gen%00", ""):
            assert running.get(f"/api/job/{route}?id={job_id}&step={step}&from=0")[0] == 404, (route, step)
        assert running.get(f"/api/job/{route}?id=../{job_id}&step=author")[0] == 404
        assert running.get(f"/api/job/{route}?id=0000ffff&step=author")[0] == 404
    assert running.get(f"/api/job/log?id={job_id}&step=gen")[0] == 404              # the symlink
    assert running.get(f"/api/job/file?id=../{job_id}&name=output.log")[0] == 404
    assert running.get(f"/api/job/file?id={other}&name=steps/author.log")[0] == 404  # another job's step
    status, headers, body = running.get(f"/api/job/file?id={job_id}&name=steps/author.log")
    assert status == 200 and body == wf.job_stream("author-finished", ws).read_bytes()
    assert headers["content-disposition"].startswith("attachment;") and "sandbox" in headers["content-security-policy"]
    status, headers, body = running.get(f"/api/job/file?id={job_id}&name=output.log")
    assert status == 200 and headers["content-disposition"].startswith("attachment;") and b"plain step output" in body
    assert running.get(f"/api/job/file?id={job_id}&name=steps/author.journal.jsonl")[0] == 200


def start_scout(running) -> str:
    status, body = post(running, {"repo": "quill", "action": "scout"})
    assert status == 202, body
    wait_job(running, body["job"])
    return body["job"]


def test_a_job_without_step_files_still_shows(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al")
    running = workspace_server(ws, al=al)
    directory = tmp_path / "jobs" / "0123abcd"
    (directory / "docs").mkdir(parents=True)
    (directory / "output.log").write_text("$ al init ws/fern\nal: init: something went wrong\n")
    old = {"version": 1, "id": "0123abcd", "repo": "fern", "repo_name": "fern", "repo_path": str(ws / "fern"),
           "action": "create", "status": "failed", "created": "2026-01-01T00:00:00Z", "started": "2026-01-01T00:00:00Z",
           "ended": "2026-01-01T00:01:00Z", "exit": 1, "reason": "init: something went wrong. More text here.",
           "layer_display": "ws/fern.al", "steps": [
               {"name": "init", "argv": [str(al), "init"], "display": "al init ws/fern", "status": "failed", "exit": 1,
                "reason": "something went wrong. More text here."},
               {"name": "author", "argv": [str(al), "author"], "display": "al author ws/fern.al", "status": "skipped"}]}
    (directory / "job.json").write_text(json.dumps(old))
    view = job(running, "0123abcd")
    assert view["has_steps_dir"] is False and view["report"] is None and view["needs_person"] == 0
    assert [s["activity"] for s in view["steps"]] == ["", ""] and [s["stream"] for s in view["steps"]] == ["", ""]
    assert view["outcome"]["step"] == "init" and view["outcome"]["reason"] == "something went wrong."
    assert view["outcome"]["log"] == "" and view["outcome"]["lines"] == []
    assert running.get("/api/job/log?id=0123abcd&step=init")[0] == 404


def test_job_output_tail_shows_home_as_tilde(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", lines=[f"wrote {Path.home()}/notes/x.txt"])
    running = workspace_server(ws, al=al)
    job_id = start_scout(running)
    reader = SSEReader(running.port, f"/api/tail?job={job_id}&what=job")
    try:
        seen = ""
        deadline = time.monotonic() + 10
        while "x.txt" not in seen and time.monotonic() < deadline:
            data, _ = reader.wait_for("text")
            seen += "".join(segment[0] for segment in json.loads(data)["segments"])
        assert "wrote ~/notes/x.txt" in seen and str(Path.home()) not in seen
    finally:
        reader.close()
