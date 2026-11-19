"""POST /api/action: an allow-listed al action on a scanned workspace repository, behind the write checks
of POST /api/models, recorded as a job (job.json, output.log, docs/) that a detached runner executes
holding the repository's lock. The al tool is `fake_al` here, which records its argv."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import dashboard as d
import workspace_fixtures as wf
from conftest import Running, SSEReader, stop_server


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    wf.build(root)
    return root


@pytest.fixture()
def al(tmp_path: Path) -> Path:
    return wf.fake_al(tmp_path / "bin" / "al", out="# LLM.txt draft")


def real(path: Path) -> str:
    return os.path.realpath(path)


def headers(running: Running, **changes: str | None) -> dict[str, str]:
    out = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{running.port}",
           "Host": f"127.0.0.1:{running.port}", "X-AL-Dashboard": "1"}
    for key, value in changes.items():
        key = key.replace("_", "-")
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def post(running: Running, body: Any, path: str = "/api/action", **changes: str | None) -> tuple[int, dict]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    status, _headers, data = running.get(path, headers(running, **changes), method="POST", body=raw)
    return status, json.loads(data) if data else {}


def job(running: Running, job_id: str) -> dict:
    return running.json(f"/api/job?id={job_id}")


def wait_job(running: Running, job_id: str, until=lambda j: j["status"] != "running", timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = job(running, job_id)
        if until(current):
            return current
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} never got there: {job(running, job_id)}")


def job_file(jobs: Path, job_id: str) -> dict:
    return json.loads((jobs / job_id / "job.json").read_text())


def start(running: Running, repo: str, action: str) -> str:
    status, body = post(running, {"repo": repo, "action": action})
    assert status == 202, body
    return body["job"]


def confirmed(running: Running, repo: str, action: str, choices: dict | None = None) -> str:
    status, preview = post(running, {"repo": repo, "action": action})
    assert status == 200 and preview["nonce"], preview
    if preview.get("job"):          # create and adopt: the preview job (choices, dry run) ends first
        wait_job(running, preview["job"])
    status, body = post(running, {"repo": repo, "action": action, "confirm": preview["nonce"],
                                  **({"choices": choices} if choices is not None else {})})
    assert status == 202, body
    return body["job"]


def test_scout_runs_the_allowed_argv_and_records_a_job(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    job_id = start(running, "fern", "scout")
    view = wait_job(running, job_id)
    assert view["status"] == "passed"
    assert wf.calls(al) == [["scout", real(ws / "fern")]]
    record = job_file(tmp_path / "jobs", job_id)
    assert record["status"] == "passed" and len(record["steps"]) == 1
    assert record["steps"][0]["exit"] == 0 and record["steps"][0]["display"] == "al scout ws/fern"
    assert record["steps"][0]["argv"] == [real(al), "scout", real(ws / "fern")]
    assert [doc["name"] for doc in view["docs"]] == ["LLM.txt"]
    assert running.json(f"/api/job/doc?id={job_id}&name=LLM.txt")["text"] == "# LLM.txt draft\n"
    assert "argv" not in json.dumps(view) and str(tmp_path) not in json.dumps(view)


@pytest.mark.parametrize("action", ["delete", "rm", ""])
def test_unknown_action_is_refused(ws, al, workspace_server, action):
    running = workspace_server(ws, al=al)
    assert post(running, {"repo": "fern", "action": action}) == (400, {"error": "unknown action"})
    assert wf.calls(al) == []


def test_action_not_allowed_in_this_state_is_refused(ws, al, workspace_server):
    running = workspace_server(ws, al=al)
    status, body = post(running, {"repo": "fern", "action": "gen"})
    assert status == 409 and "its state is none" in body["error"]
    status, body = post(running, {"repo": "pantry", "action": "create"})
    assert status == 409 and "its state is layer" in body["error"]
    assert wf.calls(al) == []


def test_repository_outside_the_workspace_is_refused(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    for repo in ("../x", str(ws / "fern"), "/etc", "nope"):
        status, _body = post(running, {"repo": repo, "action": "scout"})
        assert status == 404, repo
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "fern").rename(outside / "fern")
    os.symlink(outside / "fern", ws / "fern")
    status, body = post(running, {"repo": "fern", "action": "scout"})
    assert status == 409 and "no longer a git repository inside the workspace" in body["error"]
    assert wf.calls(al) == []


@pytest.mark.parametrize("body", [
    {"repo": "fern", "action": "scout", "args": "--json"},
    {"repo": "fern", "action": "scout", "flags": ["--force"]},
    {"repo": "fern", "action": "scout --json"},
    {"repo": "fern --out /tmp/x", "action": "scout"},
    {"repo": "fern", "action": "scout", "confirm": ["x"]},
    {"repo": "fern"},
    ["fern", "scout"],
])
def test_extra_fields_and_flags_are_refused(ws, al, workspace_server, body):
    running = workspace_server(ws, al=al)
    status, _answer = post(running, body)
    assert status == 400
    assert wf.calls(al) == []


GUARD_CASES = {
    "wrong origin": ({"Origin": "http://evil.example"}, 403),
    "missing origin": ({"Origin": None}, 403),
    "missing header": ({"X_AL_Dashboard": None}, 403),
    "form content type": ({"Content_Type": "application/x-www-form-urlencoded"}, 415),
    "bad host": ({"Host": "evil.example:80", "Origin": "http://evil.example:80"}, 403),
}


@pytest.mark.parametrize("case", [*GUARD_CASES, "body over 4 KiB", "token in the query string only"])
def test_write_guard_is_the_same_as_for_models(ws, al, workspace_server, case):
    token = case == "token in the query string only"
    running = workspace_server(ws, *(["--token", "s3cret"] if token else []), al=al)
    body: Any = {"repo": "fern", "action": "scout"}
    changes: dict[str, str | None] = {}
    suffix = ""
    if case == "body over 4 KiB":
        body, expected = {"repo": "fern", "action": "scout", "confirm": "x" * 5000}, 413
    elif token:
        suffix, expected = "?token=s3cret", 401
    else:
        changes, expected = GUARD_CASES[case]
    action = post(running, body, "/api/action" + suffix, **changes)
    models = post(running, body, "/api/models" + suffix, **changes)
    assert action[0] == expected and action == models
    assert wf.calls(al) == []


def test_read_only_refuses_actions_and_says_so(ws, al, workspace_server):
    running = workspace_server(ws, "--read-only", al=al)
    assert post(running, {"repo": "fern", "action": "scout"}) == (403, {"error": "the dashboard was started with --read-only"})
    actions = running.json("/api/repo?repo=fern")["actions"]
    assert all(not a["available"] and a["why"] == "the dashboard was started with --read-only" for a in actions)
    assert wf.calls(al) == []


def test_no_actions_flag_refuses_actions_but_keeps_models(ws, al, workspace_server):
    running = workspace_server(ws, "--no-actions", al=al)
    assert post(running, {"repo": "fern", "action": "scout"}) == (403, {"error": "actions are off (--no-actions)"})
    assert wf.calls(al) == []
    layer = running.json("/api/snapshot")["layers"][0]["key"]
    status, body = post(running, {"layer": layer, "tier": "base", "model": "claude-sonnet-5-5"}, "/api/models")
    assert status == 200 and body["ok"] is True
    assert running.json("/api/snapshot")["actions"] == {"enabled": False, "why": "actions are off (--no-actions)"}


def test_write_action_needs_a_confirmed_preview(ws, al, workspace_server):
    running = workspace_server(ws, al=al)
    status, preview = post(running, {"repo": "pantry", "action": "gen"})
    assert status == 200 and preview["preview"] is True and preview["nonce"]
    assert preview["commands"] == [f"al gen ws/pantry/agentic"] and preview["writes"] == ["ws/pantry/agentic"]
    assert wf.calls(al) == []
    assert post(running, {"repo": "pantry", "action": "gen", "confirm": "wrong"})[0] == 409
    assert post(running, {"repo": "pantry", "action": "check", "confirm": preview["nonce"]})[0] == 202   # read-only: no nonce needed
    wait_job(running, running.json("/api/repo?repo=pantry")["jobs"][0]["id"])
    status, body = post(running, {"repo": "pantry", "action": "gen", "confirm": preview["nonce"]})
    assert status == 202
    wait_job(running, body["job"])
    assert wf.calls(al)[-1] == ["gen", real(ws / "pantry" / "agentic")]
    status, body = post(running, {"repo": "pantry", "action": "gen", "confirm": preview["nonce"]})
    assert status == 409 and "unknown, used or expired" in body["error"]
    status, preview = post(running, {"repo": "pantry", "action": "gen"})
    (ws / "pantry" / "pantry.py").write_text("ITEMS = ['tea']\n")
    wf.commit(ws / "pantry", "tea")
    status, body = post(running, {"repo": "pantry", "action": "gen", "confirm": preview["nonce"]})
    assert status == 409 and "changed since the preview" in body["error"]
    assert len(wf.calls(al)) == 2


def test_adopt_preview_is_a_read_only_dry_run_job(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    status, preview = post(running, {"repo": "comet", "action": "adopt"})
    assert status == 200 and preview["nonce"] and preview["job"]
    view = wait_job(running, preview["job"])
    assert view["status"] == "passed" and view["preview"] is True
    record = job_file(tmp_path / "jobs", preview["job"])
    assert [s["argv"][1:] for s in record["steps"]] == [
        ["adopt", real(ws) + "/comet.al", "--app", real(ws / "comet"), "--dry-run", "--diff"],
        ["create", real(ws / "comet"), "--choices", "--json"]]
    assert sorted(doc["name"] for doc in view["docs"]) == ["choices.json", "preview.diff.txt"]
    assert not (ws / "comet.al").exists()
    status, body = post(running, {"repo": "comet", "action": "adopt", "confirm": preview["nonce"]})
    assert status == 202
    steps = [s["name"] for s in wait_job(running, body["job"])["steps"]]
    assert steps == ["init", "scout", "adopt", "author", "gen", "check", "result"]


def test_one_action_per_repository_at_a_time(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", sleep=3)
    running = workspace_server(ws, al=al)
    first = start(running, "fern", "scout")
    status, body = post(running, {"repo": "fern", "action": "scout"})
    assert status == 409 and body["error"] == f"an action is already running on fern (job {first})"
    other = start(running, "quill", "scout")
    wait_job(running, first)
    wait_job(running, other)
    start(running, "fern", "scout")

    jobs = d.Jobs(tmp_path / "direct-jobs")
    directories = []
    for _ in range(2):
        job_id, directory = jobs.allocate()
        record = {"id": job_id, "repo": "fern", "repo_name": "fern", "repo_path": real(ws / "fern"), "status": "running",
                  "layer_dir": real(ws) + "/fern.al", "steps": [{"name": "scout", "argv": [str(al), "scout", "x"],
                                                                 "display": "al scout x", "doc": None, "timeout": 60}]}
        d.write_json_atomic(directory / "job.json", record)
        directories.append(directory)
    script = str(Path(d.__file__).resolve())
    one = subprocess.Popen([sys.executable, "-I", script, "--run-job", str(directories[0])])
    deadline = time.monotonic() + 10
    while json.loads((directories[0] / "job.json").read_text()).get("pid") is None and time.monotonic() < deadline:
        time.sleep(0.05)
    two = subprocess.run([sys.executable, "-I", script, "--run-job", str(directories[1])], timeout=30)
    assert two.returncode == 1
    second = json.loads((directories[1] / "job.json").read_text())
    assert second["status"] == "refused" and "already running on fern" in second["reason"]
    assert one.wait(timeout=30) == 0
    assert json.loads((directories[0] / "job.json").read_text())["status"] == "passed"


def test_job_survives_a_dashboard_restart(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", sleep=3)
    running = workspace_server(ws, al=al)
    job_id = start(running, "fern", "scout")
    wait_job(running, job_id, until=lambda j: j["steps"][0]["status"] == "running")
    stop_server(running)
    again = workspace_server(ws, al=al)
    assert job(again, job_id)["status"] == "running"
    view = wait_job(again, job_id)
    assert view["status"] == "passed" and view["exit"] == 0 and view["steps"][0]["exit"] == 0
    assert len(wf.calls(al)) == 1


def test_job_whose_runner_died_is_interrupted_never_alive(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", sleep=60)
    jobs = tmp_path / "jobs"
    running = workspace_server(ws, al=al)
    job_id = start(running, "fern", "scout")
    wait_job(running, job_id, until=lambda j: j["steps"][0]["status"] == "running")
    deadline = time.monotonic() + 10
    while "pid" not in job_file(jobs, job_id)["steps"][0] and time.monotonic() < deadline:
        time.sleep(0.05)
    record = job_file(jobs, job_id)
    os.kill(record["pid"], signal.SIGKILL)
    os.killpg(record["steps"][0]["pid"], signal.SIGKILL)
    stop_server(running)
    again = workspace_server(ws, al=al)
    view = wait_job(again, job_id, timeout=10)
    assert view["status"] == "interrupted" and view["ended"]
    assert job_file(jobs, job_id)["status"] == "interrupted" and job_file(jobs, job_id)["ended"]
    assert d.lock_held(d.lock_file(jobs, real(ws / "fern"))) is False
    wf.fake_al(al, out="again")
    wait_job(again, start(again, "fern", "scout"))


def test_job_output_streams_on_the_tail(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", lines=["\x1b[31mred line\x1b[0m", "second line", "third line"],
                    line_sleep=0.4)
    running = workspace_server(ws, al=al)
    job_id = start(running, "fern", "scout")
    reader = SSEReader(running.port, f"/api/tail?job={job_id}&what=job")
    try:
        assert reader.status == 200
        seen, last_id, colours = "", None, set()
        deadline = time.monotonic() + 15
        while "red line" not in seen and time.monotonic() < deadline:
            data, last_id = reader.wait_for("text")
            for segment in json.loads(data)["segments"]:
                seen += segment[0]
                if segment[0].strip() == "red line":
                    colours.add(segment[1])
        # the prerequisite check (al doctor), then the step
        assert seen.startswith("$ al doctor fern.al --for scout\nok\n$ al scout ws/fern\n"), seen
        assert "red line" in seen and colours - {None, ""}
    finally:
        reader.close()
    wait_job(running, job_id)
    resumed = SSEReader(running.port, f"/api/tail?job={job_id}&what=job", {"Last-Event-ID": last_id})
    try:
        data, _ = resumed.wait_for("text")
        text = "".join(segment[0] for segment in json.loads(data)["segments"])
        assert "red line" not in text and "third line" in text
    finally:
        resumed.close()
    assert running.get(f"/api/tail?job=nothere&what=job")[0] == 404


def test_job_records_changed_files(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", writes={"init": {real(ws) + "/fern.al/LLM.txt": "draft\n"}})
    running = workspace_server(ws, al=al)
    view = wait_job(running, confirmed(running, "fern", "create"))
    assert view["status"] == "passed"
    assert view["changed"] == [{"path": "fern.al/LLM.txt", "change": "added"}]
    assert view["repo_unchanged"] is True and view["repo_changes"] == []
    assert [c[0] for c in wf.calls(al)] == ["create", "init", "scout", "adopt", "author", "gen", "check", "result"]


def test_job_documents_are_confined(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    first = start(running, "fern", "scout")
    second = start(running, "quill", "scout")
    wait_job(running, first)
    wait_job(running, second)
    assert running.get(f"/api/job/doc?id={first}&name=LLM.txt")[0] == 200
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n")
    os.symlink(secret, tmp_path / "jobs" / first / "docs" / "link.txt")
    (tmp_path / "jobs" / second / "docs" / "only-second.txt").write_text("mine\n")
    for name in ("../job.json", "job.json", "output.log", "link.txt", "only-second.txt", "..%2Fjob.json", "", "%00"):
        status, _headers, body = running.get(f"/api/job/doc?id={first}&name={name}")
        assert status == 404 and b"secret" not in body, name
    assert running.get(f"/api/job/doc?id=../{first}&name=LLM.txt")[0] == 404
    assert "link.txt" not in [doc["name"] for doc in job(running, first)["docs"]]


def test_existing_layer_or_definition_is_never_overwritten(ws, al, workspace_server):
    running = workspace_server(ws, al=al)
    status, preview = post(running, {"repo": "fern", "action": "create"})
    assert status == 200
    wait_job(running, preview["job"])                # the choices preview reads, never writes
    (ws / "fern.al").mkdir()
    (ws / "fern.al" / "agentic.rkt").write_text("#lang al\n")
    status, body = post(running, {"repo": "fern", "action": "create", "confirm": preview["nonce"]})
    assert status == 409 and "fern.al/agentic.rkt already exists; it is never overwritten" in body["error"]
    (ws / "quill" / "agentic").mkdir()
    (ws / "quill" / "agentic" / "agentic.rkt").write_text("#lang al\n")
    status, body = post(running, {"repo": "quill", "action": "create"})
    assert status == 409 and "quill/agentic/agentic.rkt has uncommitted changes" in body["error"]
    assert wf.calls(al) == [["create", real(ws / "fern"), "--choices", "--json"]]


def test_actions_say_when_al_is_missing(ws, tmp_path, monkeypatch, workspace_server):
    monkeypatch.setattr(d, "KIT_AL", tmp_path / "nowhere" / "al")
    bin_dir = tmp_path / "only-git"
    bin_dir.mkdir()
    os.symlink(subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip(),
               bin_dir / "git")
    monkeypatch.setenv("PATH", str(bin_dir))
    running = workspace_server(ws, al="/nonexistent")
    status, body = post(running, {"repo": "fern", "action": "scout"})
    assert status == 409 and body["error"].startswith("this action needs the al tool; looked for: /nonexistent, ")


# --- failure reasons, the prerequisite check and "Retry from STEP" --------------------------------------

RACKET_MISSING = "al: author: racket is missing: al looked at AL_RACKET (not set), PATH; set AL_RACKET."


def adopt_job(running: Running) -> str:
    """A confirmed adopt of quill (init, scout, adopt, author, gen, check, result), after its preview job has ended."""
    status, preview = post(running, {"repo": "quill", "action": "adopt"})
    assert status == 200 and preview["nonce"], preview
    wait_job(running, preview["job"])
    status, body = post(running, {"repo": "quill", "action": "adopt", "confirm": preview["nonce"]})
    assert status == 202, body
    return body["job"]


def test_a_missing_tool_is_the_failure_reason_of_its_step(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", fail={"author": (4, RACKET_MISSING)})
    running = workspace_server(ws, al=al)
    view = wait_job(running, adopt_job(running))
    assert view["status"] == "failed"
    steps = {s["name"]: s for s in view["steps"]}
    assert steps["init"]["status"] == "passed" and steps["author"]["status"] == "failed"
    assert steps["author"]["reason"].startswith("racket is missing: al looked at AL_RACKET")
    assert [steps[n]["status"] for n in ("scout", "adopt")] == ["passed"] * 2
    assert [steps[n]["status"] for n in ("gen", "check", "result")] == ["skipped"] * 3
    assert view["reason"].startswith("author: racket is missing") and view["retry_from"] == "author"
    assert "Errno" not in json.dumps(view)
    # the preview's dry run and choices, then the job
    assert [c[0] for c in wf.calls(al)] == ["adopt", "create", "init", "scout", "adopt", "author"]


def test_preflight_stops_the_job_before_any_step(ws, tmp_path, workspace_server):
    sentence = "racket is missing: al looked at AL_RACKET (not set), PATH; install Racket."
    al = wf.fake_al(tmp_path / "bin" / "al", fail={"doctor": (4, sentence)})
    running = workspace_server(ws, al=al)
    began = time.monotonic()
    job_id = adopt_job(running)
    view = wait_job(running, job_id)
    record = job_file(tmp_path / "jobs", job_id)
    assert view["status"] == "failed" and record["ended"]
    assert time.monotonic() - began < 2 + 1      # the confirmation round trip aside
    assert wf.calls(al) == []                       # no step ran: neither the preview's dry run nor init
    doctor = wf.calls(al, preflight=True)[-1:]
    assert all(c[0] == "doctor" for c in wf.calls(al, preflight=True))
    assert doctor[0][1] == real(ws) + "/quill.al"                   # the layer that does not exist yet
    assert doctor[0][doctor[0].index("--for") + 1] == "init,scout,adopt,author,gen,check,result"
    steps = {s["name"]: s for s in view["steps"]}
    assert steps["init"]["status"] == "failed" and steps["init"]["reason"] == sentence
    assert all(steps[n]["status"] == "skipped" for n in ("scout", "adopt", "author", "gen", "check", "result"))
    assert view["reason"] == f"init: {sentence}"


def test_retry_from_the_failed_step_runs_only_the_remaining_steps(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", fail={"author": (4, RACKET_MISSING)})
    running = workspace_server(ws, al=al)
    job_id = adopt_job(running)
    first = wait_job(running, job_id)
    assert first["status"] == "failed" and first["retry_from"] == "author"
    init_before = job_file(tmp_path / "jobs", job_id)["steps"][0]
    seen = len(wf.calls(al, preflight=True))
    wf.fake_al(al)                                                   # the tool is there now
    status, body = post(running, {"job": job_id, "from": "author"}, "/api/job/retry")
    assert (status, body) == (202, {"job": job_id, "from": "author"})
    view = wait_job(running, job_id)
    assert view["status"] == "passed", view
    after = wf.calls(al, preflight=True)[seen:]
    assert [c[0] for c in after] == ["doctor", "author", "gen", "check", "result"]
    assert after[1][-2:] == ["--raw", "--accept-existing"]                # the agent's event stream, then the retry flag
    record = job_file(tmp_path / "jobs", job_id)
    assert record["steps"][0]["started"] == init_before["started"] and record["steps"][0]["ended"] == init_before["ended"]
    assert len(record["retries"]) == 1 and record["retries"][0]["from"] == "author"
    assert all(s["status"] == "passed" for s in record["steps"]) and view["retry_from"] == "" and view["reason"] == ""
    assert "--- retry from author" in (tmp_path / "jobs" / job_id / "output.log").read_text()
    assert record["steps"][3]["name"] == "author" and record["steps"][3]["display"].endswith("--accept-existing")


def test_retry_is_refused_unless_it_starts_at_the_first_step_that_did_not_pass(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", fail={"author": (4, RACKET_MISSING)})
    running = workspace_server(ws, al=al)
    job_id = adopt_job(running)
    wait_job(running, job_id)
    retry = lambda body: post(running, body, "/api/job/retry")  # noqa: E731
    status, body = retry({"job": job_id, "from": "init"})
    assert status == 409 and "first step that did not pass: author" in body["error"]
    assert retry({"job": job_id, "from": "gen"})[0] == 409
    assert retry({"job": "0000ffff", "from": "author"})[0] == 404
    for bad in ({"job": job_id, "from": "author", "extra": "x"}, {"job": job_id}, {"job": job_id, "from": ["author"]},
                {"job": job_id, "from": "author now"}, ["x"]):
        assert retry(bad)[0] == 400, bad
    status, preview = post(running, {"repo": "comet", "action": "adopt"})        # a preview job
    wait_job(running, preview["job"])
    status, body = retry({"job": preview["job"], "from": "adopt-dry-run"})
    assert status == 409 and "only a failed or interrupted job" in body["error"]
    wf.fake_al(al, sleep=3)
    assert retry({"job": job_id, "from": "author"})[0] == 202
    status, body = retry({"job": job_id, "from": "author"})            # it is running now
    assert status == 409
    assert wait_job(running, job_id)["status"] == "passed"


def test_retry_obeys_read_only_and_no_actions(ws, al, workspace_server):
    for flag, error in (("--read-only", "the dashboard was started with --read-only"), ("--no-actions", "actions are off (--no-actions)")):
        running = workspace_server(ws, flag, al=al)
        assert post(running, {"job": "0000ffff", "from": "author"}, "/api/job/retry") == (403, {"error": error})


def test_repo_detail_lists_the_tools(ws, al, workspace_server):
    running = workspace_server(ws, al=al)
    tools = running.json("/api/repo?repo=fern")["al"]["tools"]
    rows = {row["tool"]: row for row in tools}
    assert {"racket", "uv", "git"} <= set(rows)
    assert all({"found", "looked", "path", "version", "sentence"} <= set(row) for row in tools)
    assert rows["git"]["found"] is True and rows["git"]["looked"]


def test_jobs_run_with_the_tool_folders_on_path(ws, al, tmp_path, workspace_server, monkeypatch):
    real_uv = subprocess.run(["sh", "-c", "command -v uv"], capture_output=True, text=True).stdout.strip()
    tools = tmp_path / "tool-folder"
    tools.mkdir()
    os.symlink(real_uv, tools / "uv")
    monkeypatch.setenv("AL_UV", str(tools / "uv"))
    running = workspace_server(ws, al=al)
    job_id = start(running, "fern", "scout")
    wait_job(running, job_id)
    record = job_file(tmp_path / "jobs", job_id)
    assert record["path"].startswith(str(tools) + os.pathsep) and record["path"].endswith(os.environ["PATH"])
    assert record["status"] == "passed"

    monkeypatch.setenv("AL_UV", str(tmp_path / "no-uv-here"))      # what the finder sees: no uv
    calls = len(wf.calls(al, preflight=True))
    nowhere = workspace_server(ws, al=al)
    job_id = start(nowhere, "fern", "scout")
    record = job_file(tmp_path / "jobs", job_id)                     # failed at once, no runner
    assert record["status"] == "failed" and record["pid"] is None and record["ended"]
    assert record["steps"][0]["reason"].startswith("uv is missing: AL_UV names")
    assert record["reason"].startswith("scout: uv is missing") and job(nowhere, job_id)["retry_from"] == "scout"
    time.sleep(0.5)
    assert len(wf.calls(al, preflight=True)) == calls


# --- the start form's choices and the result card ---------------------------------------------------------

def test_create_preview_detects_choices(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    status, preview = post(running, {"repo": "fern", "action": "create"})
    assert status == 200 and preview["job"]
    view = wait_job(running, preview["job"])
    assert view["status"] == "passed" and view["preview"] is True
    assert [(s["name"], s["doc"]) for s in view["steps"]] == [("choices", "choices.json")]
    assert job_file(tmp_path / "jobs", preview["job"])["steps"][0]["argv"][1:] == [
        "create", real(ws / "fern"), "--choices", "--json"]
    choices = json.loads(running.json(f"/api/job/doc?id={preview['job']}&name=choices.json")["text"])
    assert set(choices) == set(d.CHOICE_RULES) and choices["forge"]["value"] == "none"
    assert not (ws / "fern.al").exists()


def test_confirm_with_choices_reaches_the_author_step(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    job_id = confirmed(running, "fern", "create", {"repo": "acme/notes", "forge": "github"})
    assert wait_job(running, job_id)["status"] == "passed"
    record = job_file(tmp_path / "jobs", job_id)
    author = next(s for s in record["steps"] if s["name"] == "author")
    assert author["argv"][-4:] == ["--choice", "forge=github", "--choice", "repo=acme/notes"]
    assert record["choices"] == {"repo": "acme/notes", "forge": "github"}
    assert next(c for c in wf.calls(al) if c[0] == "author")[-4:] == author["argv"][-4:]


@pytest.mark.parametrize("action, choices", [
    ("create", {"colour": "red"}),
    ("create", {"forge": "gitlab"}),
    ("create", {"review": True}),
    ("create", {"trigger": ["manual"]}),
    ("gen", {"forge": "none"}),
    ("create", {"host": "https://git.example.org --rm"}),
    ("create", {"host": "https://git example.org"}),
    ("create", ["forge=none"]),
])
def test_choices_are_validated(ws, al, tmp_path, workspace_server, action, choices):
    running = workspace_server(ws, al=al)
    repo = "pantry" if action == "gen" else "fern"
    status, _answer = post(running, {"repo": repo, "action": action, "confirm": "x", "choices": choices})
    assert status == 400
    assert wf.calls(al) == [] and not list((tmp_path / "jobs").glob("*/job.json"))


def test_create_and_adopt_jobs_run_the_new_order(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    for repo, action in (("fern", "create"), ("comet", "adopt")):
        job_id = confirmed(running, repo, action)
        view = wait_job(running, job_id)
        assert [s["name"] for s in view["steps"]] == ["init", "scout", "adopt", "author", "gen", "check", "result"]
        steps = {s["name"]: s["argv"][1:] for s in job_file(tmp_path / "jobs", job_id)["steps"]}
        layer = real(ws) + f"/{repo}.al"
        assert steps["adopt"] == ["adopt", layer, "--app", real(ws / repo)]
        assert steps["scout"][:4] == ["scout", real(ws / repo), "--into", layer]
        assert steps["result"] == ["result", layer, "--json"]


def test_result_card_doc_is_served(ws, al, workspace_server):
    running = workspace_server(ws, al=al)
    job_id = confirmed(running, "fern", "create")
    view = wait_job(running, job_id)
    assert view["status"] == "passed"
    assert next(s for s in view["steps"] if s["name"] == "result")["doc"] == "result.json"
    card = json.loads(running.json(f"/api/job/doc?id={job_id}&name=result.json")["text"])
    assert card["forge"]["kind"] == "forgejo" and card["next"][0].startswith("set FORGEJO_TOKEN")
    assert card["layer"] == real(ws) + "/fern.al"
