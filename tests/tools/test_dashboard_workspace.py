"""The dashboard's workspace end to end, with the real al tool, real git and the mock author agent (no LLM):
started for one repository's layer, it lists the repositories beside it with their al state, scouts the
one with a hand-built suite, adopts it (a dry-run diff first, then a new layer with the proposed
workflows), creates a layer for the empty one (init, scout, adopt, author, gen, check, result: the card names
its forge), runs check / explain / gen / verify on a layer, and never overwrites a definition. No
application checkout changes.

    uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_dashboard_workspace.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import suite_setup
from conftest import KIT, LAYER_TEMPLATE, al, commit, git, status
from test_adopt_suite import SUITE_ADWS

# the dashboard and its test helpers (start_server, stop_server), loaded by path
_spec = importlib.util.spec_from_file_location("al_dashboard_test_helpers", KIT / "dashboard" / "tests" / "conftest.py")
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)  # type: ignore[union-attr]
dashboard = helpers.dashboard

# the mock author: what a real one is told to do, including the two include lines when adopt ran before it
DEFINITION = (LAYER_TEMPLATE.format(root="${root}", placement="(layer sibling)\n  ${includes}")
              .replace('#:name "notes"', '#:name "${name}"')
              .replace('#:protected ("test.sh"))', '#:protected ("test.sh"))\n    ${codebase_includes}'))


def repo(root: Path, name: str, files: dict[str, str]) -> Path:
    path = root / name
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    commit(path, f"{name}: first commit")
    return path


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "code"
    pantry = repo(root, "pantry", {"app.py": "NOTES = []\n", "test.sh": "#!/bin/sh\npython3 -c 'import app'\n"})
    (pantry / "test.sh").chmod(0o755)
    commit(pantry, "executable test")
    al("init", pantry, "--in-repo")
    (pantry / "agentic" / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="..", placement='(layer in-repo #:dir "agentic")'))
    al("gen", pantry / "agentic")
    commit(pantry, "the agentic layer", "agentic")
    assert status(pantry) == ""
    repo(root, "quill", {"CLAUDE.md": "# Rules\n\n- Never commit generated files.\n",
                         ".claude/commands/review.md": "---\ndescription: Review the change\n---\n\nReview $ARGUMENTS.\n",
                         "quill.py": "print('quill')\n"})
    suite_setup.build(root)
    repo(root, "fern", {"README.md": "# Fern\n\nGrows slowly.\n", "fern.py": "LEAVES = 3\n"})
    return root


@pytest.fixture
def server(ws: Path, tmp_path: Path):
    mock = tmp_path / "author-mock.json"
    mock.write_text(json.dumps({"files": {"agentic.rkt": DEFINITION}, "output": "wrote agentic.rkt"}))
    running = helpers.start_server(["--layer", str(ws / "pantry" / "agentic"), "--jobs-dir", str(tmp_path / "jobs"),
                                    "--author-mock", str(mock)])
    try:
        yield running
    finally:
        helpers.stop_server(running)


def post(running, body: dict) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{running.port}",
               "Host": f"127.0.0.1:{running.port}", "X-AL-Dashboard": "1"}
    code, _headers, data = running.get("/api/action", headers, method="POST", body=json.dumps(body).encode())
    return code, json.loads(data)


def wait(predicate, timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def finished(running, job_id: str, timeout: float = 600) -> dict:
    job = wait(lambda: (j := running.json(f"/api/job?id={job_id}"))["status"] != "running" and j, timeout, f"job {job_id}")
    if job["status"] != "passed":
        raise AssertionError(f"job {job_id} {job['status']}: {job['steps']}\n{output(job_id, running)}")
    return job


def output(job_id: str, running) -> str:
    return (Path(running.server.model.jobs.root) / job_id / "output.log").read_text()


def doc(running, job_id: str, name: str) -> str:
    return running.json(f"/api/job/doc?id={job_id}&name={name}")["text"]


def run(running, repo_key: str, action: str) -> dict:
    code, body = post(running, {"repo": repo_key, "action": action})
    if code == 200:   # a writing action: confirm its preview
        if body.get("job"):
            finished(running, body["job"])
        code, body = post(running, {"repo": repo_key, "action": action, "confirm": body["nonce"]})
    assert code == 202, body
    return finished(running, body["job"])


def states(running) -> dict[str, str]:
    return {r["name"]: r["state"] for r in running.json("/api/workspace")["repos"]}


def layer_of(running, name: str) -> dict:
    key = next(r["layer_key"] for r in running.json("/api/workspace")["repos"] if r["name"] == name)
    return next(l for l in running.json("/api/snapshot")["layers"] if l["key"] == key)


def create_fern(running, ws: Path) -> dict:
    job = run(running, "fern", "create")
    wait(lambda: states(running)["fern"] == "layer" and layer_of(running, "fern"), 60, "fern to become a watched layer")
    return job


def test_started_for_one_repository_lists_its_siblings_with_states(server):
    view = server.json("/api/workspace")
    assert view["roots"] == ["code"] and view["actions"]["enabled"] is True
    assert states(server) == {"pantry": "layer", "quill": "setup", "orbit": "setup", "fern": "none"}


def test_scout_shows_the_suite_summary(server, ws):
    job = run(server, "orbit", "scout")
    assert job["steps"][0]["display"] == "al scout code/orbit"
    assert "### Existing ADW suite" in doc(server, job["id"], "LLM.txt")
    assert status(ws / "orbit") == ""


def test_adopt_preview_lists_the_files_then_the_new_layer_has_the_proposed_workflows(server, ws):
    code, preview = post(server, {"repo": "orbit", "action": "adopt"})
    assert code == 200 and preview["job"]
    finished(server, preview["job"])
    diff = doc(server, preview["job"], "preview.diff.txt")
    assert "would write:" in diff and "+++ b/adopted/REPORT.md" in diff and "--- /dev/null" in diff
    choices = json.loads(doc(server, preview["job"], "choices.json"))      # the start form's detected choices
    assert choices["forge"]["value"] == "none" and choices["review"]["fact"] == "default: facts are silent"
    assert not (ws / "orbit.al").exists()
    code, body = post(server, {"repo": "orbit", "action": "adopt", "confirm": preview["nonce"]})
    assert code == 202, body
    job = finished(server, body["job"])
    assert [(s["name"], s["exit"]) for s in job["steps"]] == [("init", 0), ("scout", 0), ("adopt", 0), ("author", 0),
                                                              ("gen", 0), ("check", 0), ("result", 0)]
    assert {"path": "orbit.al/agentic.rkt", "change": "added"} in job["changed"]
    assert job["repo_unchanged"] is True
    wait(lambda: states(server)["orbit"] == "layer" and layer_of(server, "orbit"), 60, "orbit to become a watched layer")
    assert set(SUITE_ADWS) <= set(layer_of(server, "orbit")["adws"])
    assert status(ws / "orbit") == ""


def test_create_a_layer_for_the_empty_repository_end_to_end(server, ws):
    job = create_fern(server, ws)
    assert [(s["name"], s["status"]) for s in job["steps"]] == [
        (name, "passed") for name in ("init", "scout", "adopt", "author", "gen", "check", "result")]
    log = output(job["id"], server)
    assert "agentic.rkt compiles" in log and "nothing to adopt: no existing agent setup found" in log
    card = json.loads(doc(server, job["id"], "result.json"))
    assert card["forge"]["clause"] == "(forge none)" and card["layer"].endswith("fern.al")
    assert card["next"][0].startswith("al verify ")
    # both agent steps ran with --raw through the job runner; the author's journal and the page's view of it
    assert next(s for s in job["steps"] if s["name"] == "scout")["stream"] == "agent"
    author = next(s for s in job["steps"] if s["name"] == "author")
    assert author["stream"] == "agent" and author["activity"] == "journal" and author["raw"] is True
    journal = server.json(f"/api/job/journal?id={job['id']}&step=author&from=0")
    messages = [e["text"] for e in journal["events"] if e["kind"] == "message"]
    assert messages and any("agentic.rkt compiles" in text for text in messages), journal
    assert any(e["kind"] == "report" for e in journal["events"])
    outcome = job["outcome"]
    assert outcome["status"] == "passed" and outcome["created"]["layer"].endswith("fern.al")
    assert outcome["created"]["added"] >= 1 and outcome["next"].startswith("al verify ")
    assert str(Path.home()) not in json.dumps(job) and str(ws) not in json.dumps(job)
    al("compile", ws / "fern.al")
    assert "change" in layer_of(server, "fern")["adws"]
    assert status(ws / "fern") == ""


def test_gen_check_explain_verify_on_a_layer(server, ws):
    check = run(server, "pantry", "check")
    assert "check: ok" in output(check["id"], server)
    explain = run(server, "pantry", "explain")
    assert "EXPLAIN.md" in [d["name"] for d in server.json(f"/api/job?id={explain['id']}")["docs"]]
    run(server, "pantry", "gen")
    verify = run(server, "pantry", "verify")
    report = json.loads(doc(server, verify["id"], "verify.json"))
    assert any(row["name"] == "test" and row["exit"] == 0 for row in report)


def test_a_second_create_never_overwrites(server, ws):
    create_fern(server, ws)
    before = (ws / "fern.al" / "agentic.rkt").read_bytes()
    code, body = post(server, {"repo": "fern", "action": "create"})
    assert code == 409, body
    entry = server.server.model.workspace.entry("fern")
    entry.state = "none"   # as if the scan had not seen the layer yet: the definition guard still refuses
    code, body = post(server, {"repo": "fern", "action": "create"})
    assert code == 409 and "fern.al/agentic.rkt already exists; it is never overwritten" in body["error"]
    assert (ws / "fern.al" / "agentic.rkt").read_bytes() == before
