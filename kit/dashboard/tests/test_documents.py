"""Run documents: the plan, PR.md, review patch plans and verdicts and other text a run wrote,
listed per run, viewed as JSON, downloaded as exact bytes or as one zip. Unit and HTTP tests, no browser."""
from __future__ import annotations

import io
import json
import os
import time
import urllib.parse
import zipfile
from pathlib import Path

import pytest

import dashboard as d
import fixtures
from conftest import SSEReader, start_server, stop_server

LAYER_DIR = fixtures.LAYER_DIR


def layer_of(fx) -> d.Layer:
    return d.load_layer(fx["layer"].resolve(), set())


def state_of(fx, run_id: str) -> dict:
    return json.loads((fx["agents"] / run_id / "state.json").read_text())


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def running_state(run_id: str, **extra) -> dict:
    base = {"adw_id": run_id, "adw": "zte", "prompt": "a feature", "status": "running", "klass": "feature",
            "started": "2026-11-01T12:00:00+00:00", "steps": [], "outputs": {}}
    base.update(extra)
    return base


def doc_url(kind: str, run: str, path: str) -> str:
    suffix = {"view": "", "download": "/download"}[kind]
    return f"/api/doc{suffix}?layer=demo-shop&run={run}&path={urllib.parse.quote(path, safe='')}"


# ------------------------------------------------------------------------------ the model

def test_document_path_maps_recorded_paths(fx):
    layer = layer_of(fx)
    assert d.document_path(layer, f"{LAYER_DIR}/specs/chore-a1000001.md") == "specs/chore-a1000001.md"
    assert d.document_path(layer, f"{layer.path}/agents/a1000001/PR.md") == "agents/a1000001/PR.md"
    assert d.document_path(layer, "specs/x.md") == "specs/x.md"
    for hostile in ("/etc/passwd", "/elsewhere/specs/x.md", "specs/../agents/x", "", "specs/a.md\nspecs/b.md",
                    "specs/x\x00.md", None, 42):
        assert d.document_path(layer, hostile) is None, hostile


def test_run_with_plan_and_pr_lists_both(fx):
    (fx["layer"] / "specs" / "chore-a1000002.md").write_text("# Chore: refunds\n")
    layer = layer_of(fx)
    docs = {entry["path"]: entry for entry in d.run_documents(layer, "a1000002", state_of(fx, "a1000002"))}
    plan, pr = docs["specs/chore-a1000002.md"], docs["agents/a1000002/PR.md"]
    assert plan["kind"] == "plan" and pr["kind"] == "pr"
    for entry in (plan, pr):
        assert entry["bytes"] > 0 and entry["mtime"] > 0 and isinstance(entry["step"], str) and entry["viewable"] is True
    assert pr["step"] == "test" and pr["type"] == "markdown" and pr["filename"] == "PR.md"


def test_plan_is_listed_while_build_runs(fx):
    spec = f"{LAYER_DIR}/specs/feature-a2000001-x.md"
    (fx["layer"] / "specs" / "feature-a2000001-x.md").write_text("# Feature: x\n\n## Steps\n\n- one\n")
    steps = [{"name": "classify", "kind": "run", "status": "passed", "started": "2026-11-01T12:00:00+00:00",
              "ended": "2026-11-01T12:00:10+00:00"},
             {"name": "plan-feature", "kind": "run", "status": "passed", "started": "2026-11-01T12:00:10+00:00",
              "ended": "2026-11-01T12:00:40+00:00", "detail": spec},
             {"name": "build", "kind": "run", "status": "running", "started": "2026-11-01T12:00:40+00:00", "ended": ""}]
    state = running_state("a2000001", plan=spec, outputs={"plan-feature": spec}, steps=steps)
    write_state(fx["agents"] / "a2000001" / "state.json", state)
    docs = d.run_documents(layer_of(fx), "a2000001", state)
    assert [(e["path"], e["step"], e["kind"]) for e in docs] == [("specs/feature-a2000001-x.md", "plan-feature", "plan")]
    model = d.Model([fx["layer"]], [])
    model.discover()
    detail = model.run_detail("demo-shop", "a2000001")
    assert [e["path"] for e in detail["documents"]] == ["specs/feature-a2000001-x.md"]
    assert detail["summary"]["documents"] == 1 and detail["summary"]["status"] == "running"


def test_review_patch_plan_and_verdicts_are_listed(fx):
    (fx["layer"] / "specs" / "patch-1.md").write_text("# Patch: check the expiry\n")
    docs = d.run_documents(layer_of(fx), "a1000004", state_of(fx, "a1000004"))
    by_path = {e["path"]: e for e in docs}
    assert by_path["specs/patch-1.md"]["step"] == "review" and by_path["specs/patch-1.md"]["kind"] == "patch"
    run = fx["agents"] / "a1000004"
    expected = sorted(p.name for p in run.iterdir() if p.name.startswith("review-") and (p / "output.txt").is_file())
    assert expected == ["review-1", "review-2", "review-2-1", "review-2-2"]
    for name in expected:
        entry = by_path[f"agents/a1000004/{name}/output.txt"]
        assert entry["kind"] == "verdict" and entry["name"] == f"{name} verdict" and entry["step"] == "review"
        assert entry["filename"] == f"{name}-output.txt"


def test_missing_and_unsafe_references_are_not_listed(fx):
    os.symlink(fx["secret"], fx["layer"] / "specs" / "leak.md")
    layer = layer_of(fx)
    for plan in (f"{LAYER_DIR}/specs/missing-plan.md", "/etc/passwd"):
        state = {**state_of(fx, "a1000001"), "plan": plan,
                 "outputs": {"plan-chore": f"{LAYER_DIR}/specs/leak.md", "build": "/etc/passwd"}}
        paths = [e["path"] for e in d.run_documents(layer, "a1000001", state)]
        assert "specs/missing-plan.md" not in paths and "specs/leak.md" not in paths
        assert not any("passwd" in p for p in paths)
        assert "agents/a1000001/escape.txt" not in paths     # in the run folder, but a symlink out of the layer
        assert all(p.startswith(("specs/", "agents/a1000001/", "artefacts/a1000001/")) for p in paths)


# ------------------------------------------------------------------------------ HTTP

def test_view_returns_json_with_escaped_text(server, fx):
    status, headers, body = server.get(doc_url("view", "a1000001", "specs/chore-a1000001.md"))
    assert status == 200 and headers["content-type"].startswith("application/json")
    doc = json.loads(body)
    assert doc["text"] == (fx["layer"] / "specs" / "chore-a1000001.md").read_text()
    assert doc["type"] == "markdown" and doc["too_large"] is False and doc["kind"] == "plan"


def test_download_returns_exact_bytes_with_attachment_headers(server, fx):
    url = doc_url("download", "a1000002", "agents/a1000002/PR.md")
    status, headers, body = server.get(url)
    assert status == 200 and body == (fx["agents"] / "a1000002" / "PR.md").read_bytes()
    assert headers["content-disposition"] == 'attachment; filename="PR.md"'
    assert headers["content-type"].startswith("text/markdown")
    assert headers["x-content-type-options"] == "nosniff" and "sandbox" in headers["content-security-policy"]
    status, head_headers, head_body = server.get(url, method="HEAD")
    assert status == 200 and head_body == b"" and head_headers["content-disposition"] == headers["content-disposition"]
    assert head_headers["content-length"] == str(len(body))


def test_markdown_with_script_is_inert_in_the_json(server, fx):
    hostile = "# Plan\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n"
    (fx["layer"] / "specs" / "feature-a2000003-x.md").write_text(hostile)
    write_state(fx["agents"] / "a2000003" / "state.json",
                running_state("a2000003", status="passed", plan=f"{LAYER_DIR}/specs/feature-a2000003-x.md"))
    status, _headers, body = server.get(doc_url("view", "a2000003", "specs/feature-a2000003-x.md"))
    assert status == 200
    assert b"<script" not in body.lower() and b"<img" not in body.lower() and b"\\u003c" in body
    assert json.loads(body)["text"] == hostile


def test_too_large_is_download_only(server, fx):
    big = b"x" * (d.DOC_VIEW_CAP + 1)
    (fx["layer"] / "specs" / "feature-a2000004-big.md").write_bytes(big)
    write_state(fx["agents"] / "a2000004" / "state.json",
                running_state("a2000004", status="passed", plan=f"{LAYER_DIR}/specs/feature-a2000004-big.md"))
    doc = server.json(doc_url("view", "a2000004", "specs/feature-a2000004-big.md"))
    assert doc["too_large"] is True and doc["text"] == "" and doc["viewable"] is False
    status, _headers, body = server.get(doc_url("download", "a2000004", "specs/feature-a2000004-big.md"))
    assert status == 200 and body == big


def test_non_text_document_is_download_only(server, fx):
    specs = fx["layer"] / "specs"
    (specs / "notes.txt").write_text("plain notes\n")
    (specs / "result.json").write_text('{"ok": true}')
    (specs / "x.html").write_text("<html><script>alert(1)</script></html>")
    state = running_state("a2000005", status="passed", plan="",
                          outputs={"notes": "specs/notes.txt", "result": f"{LAYER_DIR}/specs/result.json",
                                   "page": f"{LAYER_DIR}/specs/x.html"})
    write_state(fx["agents"] / "a2000005" / "state.json", state)
    assert server.json(doc_url("view", "a2000005", "specs/notes.txt"))["type"] == "text"
    assert server.json(doc_url("view", "a2000005", "specs/result.json"))["type"] == "json"
    assert "specs/x.html" not in [e["path"] for e in d.run_documents(layer_of(fx), "a2000005", state)]
    for kind in ("view", "download"):
        assert server.get(doc_url(kind, "a2000005", "specs/x.html"))[0] == 404


@pytest.mark.parametrize("path", [
    "../outside-secret.txt",
    "specs/../../outside-secret.txt",
    "%2Fetc%2Fpasswd",
    "/etc/passwd",
    "agents/a1000001/escape.txt",                       # symlink to a file outside the layer
    "artefacts/a1000001/linked/outside-secret.txt",     # symlinked directory
    "agents/a1000002/PR.md",                            # another run's file
    "specs/missing.md",
    "commands/secret-template.md",
    "agents/a1000001/state.json",
    "agents/a1000001/build/raw.jsonl",
    "agents\\..\\..\\outside-secret.txt",
    "specs/chore-a1000001.md\x00.png",
])
def test_document_refusals_are_404(server, fx, path):
    for kind in ("view", "download"):
        status, _headers, body = server.get(doc_url(kind, "a1000001", path))
        assert status == 404, (kind, path, status)
        assert b"SECRET" not in body and b"root:" not in body


def test_unknown_run_or_layer_is_404(server):
    for url in ("/api/doc?layer=demo-shop&run=zzzzzzzz&path=specs%2Fchore-a1000001.md",
                "/api/doc?layer=demo-shop&run=a0000000&path=specs%2Fchore-a1000001.md",
                "/api/doc/download?layer=nope&run=a1000001&path=specs%2Fchore-a1000001.md",
                "/api/doc/zip?layer=nope&run=a1000001", "/api/doc/zip?layer=demo-shop&run=a0000000"):
        status, _headers, body = server.get(url)
        assert status == 404, url
        assert b"SECRET" not in body


def test_download_all_is_a_zip_of_the_run_documents(server, fx):
    status, headers, body = server.get("/api/doc/zip?layer=demo-shop&run=a1000002")
    assert status == 200 and headers["content-type"] == "application/zip"
    assert headers["content-disposition"] == 'attachment; filename="demo-shop-a1000002-documents.zip"'
    assert "sandbox" in headers["content-security-policy"]
    docs = d.run_documents(layer_of(fx), "a1000002", state_of(fx, "a1000002"))
    assert docs
    archive = zipfile.ZipFile(io.BytesIO(body))
    assert archive.namelist() == [e["path"] for e in docs]
    for name in archive.namelist():
        assert ".." not in name and not name.startswith("/")
        assert archive.read(name) == (fx["layer"] / name).read_bytes()


def test_document_endpoints_need_the_token(fx):
    running = start_server(["--layer", str(fx["layer"]), "--token", "t0k"])
    try:
        urls = (doc_url("view", "a1000002", "agents/a1000002/PR.md"),
                doc_url("download", "a1000002", "agents/a1000002/PR.md"),
                "/api/doc/zip?layer=demo-shop&run=a1000002")
        deadline = time.monotonic() + 10
        while not running.server.model.layers and time.monotonic() < deadline:
            time.sleep(0.05)
        for url in urls:
            assert running.get(url)[0] == 401, url
            assert running.get(url, {"Authorization": "Bearer t0k"})[0] == 200, url
    finally:
        stop_server(running)


def test_new_document_arrives_on_the_event_stream(server, fx):
    reader = SSEReader(server.port, "/api/events")
    try:
        reader.wait_for("snapshot")
        state_path = fx["agents"] / "a2000002" / "state.json"
        state = running_state("a2000002", plan="")
        write_state(state_path, state)
        data, _ = reader.wait_for("run", lambda raw: json.loads(raw)["id"] == "a2000002")
        assert json.loads(data)["documents"] == 0
        (fx["layer"] / "specs" / "feature-a2000002-x.md").write_text("# Feature: x\n")
        write_state(state_path, {**state, "plan": f"{LAYER_DIR}/specs/feature-a2000002-x.md"})
        reader.wait_for("run", lambda raw: json.loads(raw)["id"] == "a2000002" and json.loads(raw)["documents"] == 1)
        detail = server.json("/api/run?layer=demo-shop&run=a2000002")
        assert [e["path"] for e in detail["documents"]] == ["specs/feature-a2000002-x.md"]
        (fx["agents"] / "a2000002" / "PR.md").write_text("# PR\n")
        reader.wait_for("run", lambda raw: json.loads(raw)["id"] == "a2000002" and json.loads(raw)["documents"] == 2)
    finally:
        reader.close()
