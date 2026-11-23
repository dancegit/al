"""The "Ship without review" policy on a real server (tests/review_limit_fixtures.py): the snapshot and
the layer carry it; POST /api/review-limit writes and resets the layer's local limit
(agents/settings/review.json) behind every write guard, refuses a value outside 1 to 50000, an option
that is not an auto review option, an unknown layer, and a layer whose gate does not read the limit."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

import dashboard as d
import review_limit_fixtures as rf
from conftest import Running, start_server, stop_server
from test_model_settings import post

PATH = "/api/review-limit"
SET = {"layer": rf.NAME, "option": rf.OPTION, "limit": 3000, "who": "Ada"}
RESET = {"layer": rf.NAME, "option": rf.OPTION, "reset": True}


@pytest.fixture()
def site(tmp_path: Path) -> dict:
    return rf.build_review_site(tmp_path / "site")


@pytest.fixture()
def server(site) -> Iterator[Running]:
    running = start_server(["--layer", str(site["layer"])])
    try:
        yield running
    finally:
        stop_server(running)


def setting(site: dict) -> Path:
    return site["layer"] / d.REVIEW_FILE


def limit_of(view: dict) -> dict:
    return next(o for o in view["options"] if o["id"] == rf.OPTION)["maxDiffLines"]


def policy(running: Running) -> dict:
    return running.json("/api/snapshot")["layers"][0]["review_policy"]


def nothing_written(site: dict) -> bool:
    return not setting(site).exists() and not list(setting(site).parent.glob(".review.*"))


def test_snapshot_and_layer_carry_the_policy(server, site):
    for view in (policy(server), server.json(f"/api/layer?layer={rf.NAME}")["layer"]["review_policy"]):
        assert [o["id"] for o in view["options"]] == [rf.OPTION]
        option = view["options"][0]
        assert option["ship"] == {"value": "merge", "source": "layer definition"}
        assert option["allowedClasses"] == {"value": ["chore", "bug"], "source": "al's default"}
        assert option["requireChecks"] == {"value": ["lint", "test"], "source": "layer definition"}
        assert (limit_of(view)["value"], limit_of(view)["source"], limit_of(view)["local"]) == (
            300, "layer definition", None)
        assert (view["min"], view["max"], view["writable"], view["gate_reads"], view["error"]) == (
            1, 50000, True, True, "")


def test_write_then_reset_the_override(server, site):
    status, data = post(server, SET, path=PATH)
    assert status == 200 and data["ok"] is True
    stored = json.loads(setting(site).read_text())
    assert stored["version"] == 1 and stored["maxDiffLines"] == {"zero-touch": 3000}
    stamp = stored["set"]["zero-touch"]
    assert (stamp["who"], stamp["via"]) == ("Ada", "dashboard")
    assert len(stamp["at"]) == 20 and stamp["at"].endswith("Z")
    for view in (data["review_policy"], policy(server)):
        limit = limit_of(view)
        assert (limit["value"], limit["source"], limit["declared"], limit["local"]) == (3000, "local override", 300, 3000)
        assert limit["set"] == stamp
    assert not list(setting(site).parent.glob(".review.*"))

    status, data = post(server, RESET, path=PATH)
    assert status == 200 and data["ok"] is True and not setting(site).exists()
    for view in (data["review_policy"], policy(server)):
        assert (limit_of(view)["value"], limit_of(view)["source"], limit_of(view)["local"]) == (
            300, "layer definition", None)
    assert nothing_written(site)
    assert post(server, RESET, path=PATH)[0] == 200   # resetting what is not set is no error


def test_write_without_the_guard_is_403(server, site):
    assert post(server, SET, path=PATH, X_AL_Dashboard=None)[0] == 403
    assert post(server, SET, path=PATH, Origin="http://evil.example")[0] == 403
    assert server.get(PATH)[0] == 405   # GET is refused like the other writing paths
    assert nothing_written(site)
    running = start_server(["--layer", str(site["layer"]), "--read-only"])
    try:
        status, data = post(running, SET, path=PATH)
        assert status == 403 and data["error"] == "the dashboard was started with --read-only"
        assert policy(running)["writable"] is False
    finally:
        stop_server(running)
    running = start_server(["--layer", str(site["layer"]), "--token", "t0k"])
    try:
        assert post(running, SET, path=PATH)[0] == 401
        assert post(running, SET, path=PATH, Authorization="Bearer t0k",
                    Content_Type="application/x-www-form-urlencoded")[0] == 415
        assert post(running, {**SET, "who": "x" * 5000}, path=PATH, Authorization="Bearer t0k")[0] == 413
    finally:
        stop_server(running)
    assert nothing_written(site)


@pytest.mark.parametrize("body", [
    {**SET, "limit": 0}, {**SET, "limit": -5}, {**SET, "limit": 50001}, {**SET, "limit": "3000"},
    {**SET, "limit": 2.5}, {**SET, "limit": True}, {"layer": rf.NAME, "option": rf.OPTION, "who": "Ada"},
    {"layer": rf.NAME, "option": rf.OPTION, "limit": 3000}, {**SET, "who": ""}, {**SET, "who": "x" * 81},
    {**SET, "who": 7}, {**RESET, "limit": 3000}, {"layer": rf.NAME, "option": rf.OPTION, "reset": False},
    {**SET, "extra": 1}, {"layer": rf.NAME, "limit": 3000, "who": "Ada"}, {**SET, "option": 3}, [1], b"{not json",
    {**SET, "option": "human"}, {**SET, "option": "nope"}, {**RESET, "option": "nope"},
])
def test_invalid_values_are_400(server, site, body):
    status, data = post(server, body, path=PATH)
    assert status == 400 and data["error"]
    assert nothing_written(site)


def test_invalid_values_say_what_is_wrong(server, site):
    assert "whole number from 1 to 50000" in post(server, {**SET, "limit": 50001}, path=PATH)[1]["error"]
    assert post(server, {**SET, "who": ""}, path=PATH)[1]["error"] == "who: a name of 1 to 80 characters"
    assert post(server, {**SET, "option": "human"}, path=PATH)[1]["error"] == (
        "no auto review option named 'human'; this layer has: zero-touch")


def test_unknown_layer_is_404(server, site):
    assert post(server, {**SET, "layer": "nope"}, path=PATH)[0] == 404
    assert post(server, {**RESET, "layer": "nope"}, path=PATH)[0] == 404
    assert nothing_written(site)


def test_gate_that_does_not_read_the_limit_is_409(tmp_path):
    old = rf.build_review_site(tmp_path / "old", gate_reads=False)
    running = start_server(["--layer", str(old["layer"])])
    try:
        status, data = post(running, SET, path=PATH)
        assert status == 409
        assert data["error"] == "this layer's gate does not read the local limit: run `al gen LAYER`"
        assert post(running, RESET, path=PATH)[0] == 409
        assert nothing_written(old)
        view = policy(running)
        assert (view["writable"], view["gate_reads"]) == (False, False)
    finally:
        stop_server(running)
