"""The promotion on the dashboard (HTTP, no browser): a promotion run's page (its steps, the card with
changes, criteria, proof and decisions), POST /api/promotion behind the same guard as every write
(decide, start, continue through the layer's own adws/promote.py), the pack page served inline and
sandboxed, and the layer page's promotions."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import Running, start_server, stop_server
from promotion_fixtures import build_promotion_site, script


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build_promotion_site(tmp_path_factory.mktemp("promotion"))


@pytest.fixture()
def site(built, tmp_path):
    """A private copy of the built site (tests write decisions and start runs)."""
    import shutil

    root = tmp_path / "copy"
    shutil.copytree(built["root"], root, symlinks=True)
    # the copied layer names the copied app; worktrees of the copy are re-registered by git on demand
    ir_path = root / "shop.al" / "agentic-layer.json"
    data = json.loads(ir_path.read_text())
    data["codebase"]["root"] = str(root / "app")
    ir_path.write_text(json.dumps(data, indent=1))
    import subprocess

    subprocess.run(["git", "-C", str(root / "app"), "worktree", "repair", *[str(p) for p in (root / "shop.al" / "trees").glob("*")]],
                   capture_output=True, timeout=60)
    return {**built, "root": root, "app": root / "app", "layer": root / "shop.al"}


def headers(running: Running, **changes: str | None) -> dict[str, str]:
    out = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{running.port}",
           "Host": f"127.0.0.1:{running.port}", "X-AL-Dashboard": "1"}
    for name, value in changes.items():
        name = name.replace("_", "-")
        if value is None:
            out.pop(name, None)
        else:
            out[name] = value
    return out


def post(running: Running, body: Any, path: str = "/api/promotion", **changes: str | None) -> tuple[int, dict]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    status, _headers, data = running.get(path, headers(running, **changes), method="POST", body=raw)
    try:
        return status, json.loads(data) if data else {}
    except ValueError:
        return status, {"raw": data.decode("utf-8", "replace")}


def serve(site, *flags: str) -> Running:
    return start_server(["--layer", str(site["layer"]), *flags])


def decide_body(**extra: Any) -> dict:
    return {"layer": "shop", "action": "decide", "from": "sprint", "to": "main", "decision": "accept",
            "changes": ["0000c001"], "by": "Ada Example", "comment": "looks right", **extra}


def lines(site) -> list[dict]:
    path = site["layer"] / "promotions" / site["run"] / "acceptance.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_promotion_run_page_has_its_steps_and_card(site):
    done = script(site["layer"], "promote.py", "decide", "sprint", "main", "refuse", "0000c002", "--by", "Grace Example",
                  "--comment", "not yet")
    assert done.returncode == 0, done.stderr
    running = serve(site)
    try:
        detail = running.json(f"/api/run?layer=shop&run={site['run']}")
    finally:
        stop_server(running)
    types = {node["type"] for node in detail["graph"]["nodes"]}
    assert types == {"promote"}
    assert {node["step"] for node in detail["graph"]["nodes"]} >= {"open", "checks", "pack", "request", "approval",
                                                                    "merge", "close"}
    assert next(n for n in detail["graph"]["nodes"] if n["step"] == "approval")["sub"] == "people decide"
    card = detail["promotion"]
    assert card["from_branch"] == "sprint/2026-s47" and card["to_branch"] == "main" and card["open"] is True
    assert [c["id"] for c in card["changes"]] == ["0000c001", "0000c002", "0000c003"]
    cart = card["changes"][0]
    assert cart["title"] == "Add a cart" and [c["criterion"] for c in cart["criteria"]] == ["The cart holds items",
                                                                                              "test passes"]
    assert any(p["kind"] == "image" and p["label"] == "cart.png" for p in cart["criteria"][0]["proof"])
    assert any(p["kind"] == "check" and p["ok"] for p in cart["criteria"][1]["proof"])
    assert card["decisions"] == [{"at": card["decisions"][0]["at"], "by": "Grace Example", "kind": "human",
                                  "scope": ["0000c002"], "decision": "refuse", "then": "revert", "comment": "not yet",
                                  "source": ""}]
    assert card["pack"] == f"artefacts/{site['run']}/pack/index.html"
    assert card["records"]["approval.md"] == f"promotions/{site['run']}/approval.md"
    summary = detail["summary"]
    assert summary["status"] == "waiting" and summary["waiting_step"]["name"] == "approval"
    assert summary["resume"] == f"uv run adws/promote.py run sprint main --resume --adw-id {site['run']}"
    assert summary["promotion"]["from"] == "sprint" and summary["promotion"]["changes"] == 3


def test_decide_records_through_the_layer_script(site):
    running = serve(site)
    try:
        status, data = post(running, decide_body())
    finally:
        stop_server(running)
    assert status == 200, data
    assert data["status"] == "recorded" and data["promotion"] == site["run"]
    record = lines(site)[-1]
    assert record["by"] == "Ada Example" and record["comment"] == "looks right" and record["scope"] == ["0000c001"]
    assert (site["layer"] / "promotions" / site["run"] / "sign-off.md").is_file()


def _wait_not_running(layer: Path, run_id: str, timeout: float = 240) -> dict:
    deadline = time.monotonic() + timeout
    state: dict = {}
    while time.monotonic() < deadline:
        try:
            state = json.loads((layer / "agents" / run_id / "state.json").read_text())
        except (OSError, ValueError):
            state = {}
        if state.get("status") in ("passed", "failed", "waiting") and state.get("ended"):
            return state
        time.sleep(0.3)
    log = layer / "agents" / run_id / "launch.log"
    raise AssertionError(f"run {run_id} did not end: {state.get('status')}\n"
                         + (log.read_text()[-3000:] if log.exists() else "no launch.log"))


def test_start_and_continue_spawn_the_layer_script(site):
    running = serve(site)
    try:
        status, started = post(running, {"layer": "shop", "action": "start", "from": "sprint", "to": "main",
                                         "dry_run": True})
        assert status == 202, started
        assert started["status"] == "started" and started["run_id"] != site["run"]
        assert started["url"] == f"#/run/shop/{started['run_id']}"
        assert _wait_not_running(site["layer"], started["run_id"])["status"] == "passed"
        status, continued = post(running, {"layer": "shop", "action": "continue", "from": "sprint", "to": "main"})
        assert status == 202, continued
        assert continued["run_id"] == site["run"] and continued["resumed"] is True
        state = _wait_not_running(site["layer"], site["run"])
        assert state["status"] == "waiting"
    finally:
        stop_server(running)


def _old_layer(running, site):
    (site["layer"] / "adws" / "promote.py").unlink()
    return post(running, decide_body())


REFUSALS = [
    ("wrong origin", [], lambda r, s: post(r, decide_body(), Origin="http://evil.example"), 403),
    ("no X-AL-Dashboard", [], lambda r, s: post(r, decide_body(), X_AL_Dashboard=None), 403),
    ("token in the query string", ["--token", "t0k"],
     lambda r, s: post(r, decide_body(), path="/api/promotion?token=t0k"), 401),
    ("form content type", [], lambda r, s: post(r, b"layer=shop", Content_Type="application/x-www-form-urlencoded"), 415),
    ("body over 8 KiB", [], lambda r, s: post(r, decide_body(comment="x" * (8 * 1024))), 413),
    ("--read-only", ["--read-only"], lambda r, s: post(r, decide_body()), 403),
    ("--no-start for start", ["--no-start"],
     lambda r, s: post(r, {"layer": "shop", "action": "start", "from": "sprint", "to": "main"}), 403),
    ("unknown action", [], lambda r, s: post(r, decide_body(action="merge")), 400),
    ("unknown key", [], lambda r, s: post(r, {**decide_body(), "shell": "rm -rf /"}), 400),
    ("bad change id", [], lambda r, s: post(r, decide_body(changes=["../../x"])), 400),
    ("by with a newline", [], lambda r, s: post(r, decide_body(by="Ada\nExample")), 400),
    ("comment over 2000 characters", [], lambda r, s: post(r, decide_body(comment="x" * 2001)), 400),
    ("bad decision", [], lambda r, s: post(r, decide_body(decision="maybe")), 400),
    ("bad then", [], lambda r, s: post(r, decide_body(decision="refuse", then="delete")), 400),
    ("unknown layer", [], lambda r, s: post(r, decide_body(layer="elsewhere")), 404),
    ("old layer without adws/promote.py", [], _old_layer, 409),
    ("GET", [], lambda r, s: (r.get("/api/promotion")[0], {}), 405),
]


@pytest.mark.parametrize("why, flags, call, code", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_promotion_refusals(site, why, flags, call, code):
    before = lines(site)
    running = serve(site, *flags)
    try:
        status, data = call(running, site)
    finally:
        stop_server(running)
    assert status == code, (why, data)
    assert lines(site) == before, why
    if code in (400, 403, 404, 409) and why != "GET":
        assert data.get("error"), data
    if why == "old layer without adws/promote.py":
        assert "run `al gen shop`" in data["error"]


def test_pack_page_is_served_inline_and_sandboxed(site):
    running = serve(site)
    try:
        status, response_headers, body = running.get(f"/files/shop/artefacts/{site['run']}/pack/index.html")
        assert status == 200
        assert response_headers["content-type"] == "text/html; charset=utf-8"
        policy = response_headers["content-security-policy"]
        assert policy.startswith("sandbox") and "allow-scripts" not in policy and "script-src" not in policy
        assert "default-src 'none'" in policy
        assert b"Add a cart" in body and b'<img src="media/0000c001/cart.png"' in body
        status, _h, _b = running.get(f"/files/shop/artefacts/{site['run']}/pack/media/0000c001/cart.png")
        assert status == 200
        for bad in ("artefacts/../agentic-layer.json", f"artefacts/{site['run']}/pack/../../../../app/VERSION",
                    "promotions/../agents/cycle.json"):
            assert running.get(f"/files/shop/{bad}")[0] in (403, 404), bad
        status, _h, body = running.get(f"/files/shop/promotions/{site['run']}/approval.md")
        assert status == 200 and b"Approval needed" in body
    finally:
        stop_server(running)


def test_layer_page_lists_promotions(site):
    running = serve(site)
    try:
        data = running.json("/api/layer?layer=shop")
    finally:
        stop_server(running)
    assert [(p["from"], p["to"], p["approval"]) for p in data["promotions"]] == [("sprint", "main", "human")]
    assert data["promotions"][0]["open"]["id"] == site["run"]
    assert data["open_promotions"][0]["id"] == site["run"]
    assert data["promote"]["available"] is True and data["promote"]["start"]["enabled"] is True
