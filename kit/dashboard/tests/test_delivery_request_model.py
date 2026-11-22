"""The delivery request on the dashboard (model and HTTP, no browser): the run page's request (a pull
request's link and state, or a file request's layer-relative path), the request of each promotion on
the layer page and in the promotion card (agents/requests/<from>-<to>.json), the forge state refreshed
through the layer's own runtime and kept PR_STATE_SECONDS."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import dashboard as d
from conftest import start_server, stop_server
from promotion_fixtures import SPRINT, build_promotion_site

URL = "https://forge.example.org/acme/webshop/pulls/3"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build_promotion_site(tmp_path_factory.mktemp("requests"))


@pytest.fixture()
def site(built, tmp_path):
    root = tmp_path / "copy"
    shutil.copytree(built["root"], root, symlinks=True)
    ir_path = root / "shop.al" / "agentic-layer.json"
    data = json.loads(ir_path.read_text())
    data["codebase"]["root"] = str(root / "app")
    ir_path.write_text(json.dumps(data, indent=1))
    subprocess.run(["git", "-C", str(root / "app"), "worktree", "repair",
                    *[str(p) for p in (root / "shop.al" / "trees").glob("*")]], capture_output=True, timeout=60)
    return {**built, "root": root, "app": root / "app", "layer": root / "shop.al"}


def model_of(site, **kwargs) -> d.Model:
    model = d.Model([site["layer"]], [], **kwargs)
    model.discover()
    model.tick()
    return model


def set_request(layer: Path, run_id: str, record: dict) -> None:
    path = layer / "agents" / run_id / "state.json"
    state = json.loads(path.read_text())
    state["delivery_request"] = record
    path.write_text(json.dumps(state, indent=2))


def forge_record(**extra) -> dict:
    return {"kind": "promotion", "from": "sprint", "to": "main", "source": SPRINT, "target": "main",
            "medium": "forge", "number": 3, "url": URL, "state": "open", "action": "updated",
            "title": f"Promote {SPRINT} into main: 3 changes", "updated_at": "2026-11-16T10:00:00+00:00", **extra}


def use_forgejo(layer: Path) -> None:
    path = layer / "agentic-layer.json"
    data = json.loads(path.read_text())
    data["forge"] = {"id": "forgejo", "kind": "forgejo", "status": "stable", "host": "https://forge.example.org",
                     "repo": "acme/webshop", "tokenEnv": "FORGEJO_TOKEN", "botMarker": "[ADW-BOT]"}
    path.write_text(json.dumps(data, indent=1))


def test_run_detail_carries_the_delivery_request(site):
    set_request(site["layer"], "0000c001", forge_record())
    detail = model_of(site).run_detail("shop", "0000c001")
    req = detail["delivery_request"]
    assert req["url"] == URL and req["number"] == 3 and req["state"] == "open"
    assert req["source"] == SPRINT and req["target"] == "main" and req["action"] == "updated"
    assert "body" not in req


def test_file_request_shows_a_layer_relative_path(site):
    # the last delivered run of the built site wrote the stage's file request itself (forge none)
    detail = model_of(site).run_detail("shop", "0000c003")
    req = detail["delivery_request"]
    assert req["path"] == "agents/requests/sprint-main.md" and req["medium"] == "file"
    assert req["state"] == "open" and req["action"] == "updated"
    text = json.dumps(req)
    assert str(site["root"]) not in text and str(Path.home()) not in text
    set_request(site["layer"], "0000c002", {**forge_record(), "medium": "file", "number": None, "url": "",
                                            "path": str(site["layer"] / "agents" / "requests" / "sprint-main.md")})
    shown = model_of(site).run_detail("shop", "0000c002")["delivery_request"]
    assert shown["path"] == "" and str(site["root"]) not in json.dumps(shown)


def test_layer_promotions_carry_their_request(site):
    record = site["layer"] / "agents" / "requests" / "sprint-main.json"
    data = json.loads(record.read_text())
    assert data["path"] == "agents/requests/sprint-main.md"
    data["updated_at"] = "2026-11-16T11:00:00+00:00"
    record.write_text(json.dumps(data))
    model = model_of(site)
    promotions = model.layer_detail("shop")["promotions"]
    req = promotions[0]["request"]
    assert (promotions[0]["from"], promotions[0]["to"]) == ("sprint", "main")
    assert req["path"] == "agents/requests/sprint-main.md" and req["state"] == "open"
    assert req["updated_at"] == "2026-11-16T11:00:00+00:00"
    card = model.run_detail("shop", site["run"])["promotion"]
    assert card["request"]["path"] == "agents/requests/sprint-main.md"
    assert card["request"]["updated_at"] == "2026-11-16T11:00:00+00:00"


def test_request_state_is_refreshed_from_the_forge_and_cached(site, monkeypatch):
    use_forgejo(site["layer"])
    path = site["layer"] / "agents" / "requests" / "sprint-main.json"
    path.write_text(json.dumps({**forge_record(), "key": "sprint-main"}))
    asked = []

    def answer(layer, number, timeout=15.0):
        asked.append(number)
        return {"state": "merged", "commit": "abc", "at": "2026-11-16T12:00:00Z"}

    monkeypatch.setattr(d, "pr_state_via_layer", answer)
    model = model_of(site)
    first = model.layer_detail("shop")["promotions"][0]["request"]
    assert first["state"] == "merged" and first["url"] == URL
    again = model.layer_detail("shop")["promotions"][0]["request"]
    assert again["state"] == "merged"
    card = model.run_detail("shop", site["run"])["promotion"]["request"]
    assert card["state"] == "merged"
    assert asked == [3]   # one forge question within PR_STATE_SECONDS
    # a file request is never asked about
    path.write_text(json.dumps({**forge_record(), "medium": "file", "number": None, "url": "",
                                "path": "agents/requests/sprint-main.md"}))
    assert model.layer_detail("shop")["promotions"][0]["request"]["state"] == "open" and asked == [3]


def test_http_run_and_layer_endpoints_return_the_request(site):
    set_request(site["layer"], "0000c001", forge_record())
    running = start_server(["--layer", str(site["layer"])])
    try:
        run = running.json("/api/run?layer=shop&run=0000c001")
        layer = running.json("/api/layer?layer=shop")
        card = running.json(f"/api/run?layer=shop&run={site['run']}")
    finally:
        stop_server(running)
    assert run["delivery_request"]["url"] == URL and run["delivery_request"]["state"] == "open"
    assert layer["promotions"][0]["request"]["path"] == "agents/requests/sprint-main.md"
    assert card["promotion"]["request"]["path"] == "agents/requests/sprint-main.md"
