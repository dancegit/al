"""The Models view and the dashboard's one write, POST /api/models: it writes the layer's local
model setting (agents/settings/models.json) in the runtime's format, behind every guard."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

import dashboard as d
from conftest import Running, start_server, stop_server

TIERS = [{"tier": "base", "model": "claude-opus-5-5", "effort": "default"},
         {"tier": "heavy", "model": "claude-opus-5-5", "effort": "default"},
         {"tier": "fast", "model": "claude-haiku-5-5", "effort": "default"}]


def _with_tiers(fx: dict[str, Path]) -> Path:
    path = fx["layer"] / "agentic-layer.json"
    ir = json.loads(path.read_text())
    for agent in ir["agents"]:
        if agent["id"] == "claude":
            agent["models"] = TIERS
    path.write_text(json.dumps(ir, indent=2))
    return fx["layer"]


def _start(fx: dict[str, Path], *extra: str) -> Running:
    return start_server(["--layer", str(_with_tiers(fx)), *extra])


@pytest.fixture()
def models_server(fx: dict[str, Path]) -> Iterator[Running]:
    running = _start(fx)
    try:
        yield running
    finally:
        stop_server(running)


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


def post(running: Running, body: object, path: str = "/api/models", **changes: str | None) -> tuple[int, dict]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    status, _headers, data = running.get(path, headers(running, **changes), method="POST", body=raw)
    return status, json.loads(data) if data else {}


def view(running: Running) -> dict:
    return running.json("/api/snapshot")["layers"][0]["models"]


def tier(models_view: dict, name: str) -> dict:
    return next(t for t in models_view["tiers"] if t["tier"] == name)


def settings_file(fx: dict[str, Path]) -> Path:
    return fx["layer"] / d.SETTINGS_FILE


def test_snapshot_lists_each_tier_with_model_source_and_writability(models_server, fx):
    models = view(models_server)
    assert [t["tier"] for t in models["tiers"]] == ["base", "heavy", "fast"]
    assert {k: tier(models, "heavy")[k] for k in ("model", "source", "local", "agents")} == {
        "model": "claude-opus-5-5", "source": "built-in default", "local": None, "agents": ["claude"]}
    assert (tier(models, "fast")["model"], tier(models, "fast")["source"]) == ("claude-haiku-5-5", "layer")
    assert models["known"] == list(d.KNOWN_MODELS)
    assert (models["writable"], models["why"], models["error"]) == (True, "", "")
    # a malformed file is reported and not used
    settings_file(fx).parent.mkdir(parents=True, exist_ok=True)
    settings_file(fx).write_text("{oops")
    layer = models_server.server.model.layers["demo-shop"]
    broken = layer.models_view(False)
    assert broken["error"] and "al models LAYER --reset" in broken["error"]
    assert tier(broken, "heavy")["source"] == "built-in default"


def test_valid_change_writes_the_file_and_reports_the_new_source(models_server, fx):
    status, data = post(models_server, {"layer": "demo-shop", "tier": "heavy", "model": "claude-fable-5-2"})
    assert status == 200 and data["ok"] is True and data["note"] is None
    assert settings_file(fx).read_text() == d.render_settings({"heavy": "claude-fable-5-2"})
    assert (tier(data["models"], "heavy")["model"], tier(data["models"], "heavy")["source"]) == (
        "claude-fable-5-2", "local setting")
    heavy = tier(view(models_server), "heavy")
    assert (heavy["model"], heavy["source"], heavy["local"]) == ("claude-fable-5-2", "local setting", "claude-fable-5-2")
    assert not list(settings_file(fx).parent.glob(".models.*"))


def test_custom_model_id_is_accepted_with_a_note(models_server, fx):
    status, data = post(models_server, {"layer": "demo-shop", "tier": "base", "model": "my-custom-model"})
    assert status == 200 and "my-custom-model is not a known model id" in data["note"]
    assert json.loads(settings_file(fx).read_text())["models"] == {"base": "my-custom-model"}


def test_reset_one_tier_and_reset_all(models_server, fx):
    for name, model in (("heavy", "claude-fable-5-2"), ("base", "claude-sonnet-5-5")):
        assert post(models_server, {"layer": "demo-shop", "tier": name, "model": model})[0] == 200
    status, data = post(models_server, {"layer": "demo-shop", "tier": "heavy", "reset": True})
    assert status == 200 and json.loads(settings_file(fx).read_text())["models"] == {"base": "claude-sonnet-5-5"}
    assert tier(data["models"], "heavy")["source"] == "built-in default"
    assert post(models_server, {"layer": "demo-shop", "tier": None, "reset": True})[0] == 200
    assert not settings_file(fx).exists()


def test_unknown_tier_is_400(models_server, fx):
    status, data = post(models_server, {"layer": "demo-shop", "tier": "turbo", "model": "claude-fable-5-2"})
    assert status == 400 and "no agent of this layer has a tier `turbo`" in data["error"]
    assert not settings_file(fx).exists()


@pytest.mark.parametrize("model", ["", "a b", "x\n", "x" * 201])
def test_bad_model_id_is_400(models_server, fx, model):
    status, data = post(models_server, {"layer": "demo-shop", "tier": "heavy", "model": model})
    assert status == 400 and "model id must be" in data["error"]
    assert not settings_file(fx).exists()


def test_malformed_bodies_are_400(models_server):
    for body in (b"{oops", json.dumps([1]).encode(), json.dumps({"layer": "demo-shop", "tier": "heavy"}).encode(),
                 json.dumps({"layer": "demo-shop", "tier": "heavy", "model": 3}).encode()):
        assert post(models_server, body)[0] == 400


def test_wrong_origin_is_403(models_server, fx):
    status, data = post(models_server, {"layer": "demo-shop", "tier": "heavy", "model": "claude-fable-5-2"},
                        Origin="http://evil.example")
    assert status == 403 and data["error"] == "cross-origin request refused"
    assert not settings_file(fx).exists()


def test_missing_origin_is_403(models_server, fx):
    status, _ = post(models_server, {"layer": "demo-shop", "tier": "heavy", "model": "claude-fable-5-2"}, Origin=None)
    assert status == 403 and not settings_file(fx).exists()


def test_missing_custom_header_is_403(models_server, fx):
    status, data = post(models_server, {"layer": "demo-shop", "tier": "heavy", "model": "claude-fable-5-2"},
                        X_AL_Dashboard=None)
    assert status == 403 and data["error"] == "missing X-AL-Dashboard header"
    assert not settings_file(fx).exists()


def test_form_post_is_refused(models_server, fx):
    status, _ = post(models_server, b"layer=demo-shop&tier=heavy&model=claude-fable-5-2",
                     Content_Type="application/x-www-form-urlencoded")
    assert status == 415 and not settings_file(fx).exists()


def test_oversized_body_is_413(models_server, fx):
    body = json.dumps({"layer": "demo-shop", "tier": "heavy", "model": "claude-fable-5-2", "pad": "x" * 5000}).encode()
    assert post(models_server, body)[0] == 413
    assert not settings_file(fx).exists()


def test_other_post_paths_stay_refused(models_server):
    assert post(models_server, {"layer": "demo-shop"}, path="/api/snapshot")[0] == 405


def test_token_server_requires_the_token_for_the_write(fx):
    running = _start(fx, "--token", "t0k")
    try:
        body = {"layer": "demo-shop", "tier": "heavy", "model": "claude-fable-5-2"}
        assert post(running, body)[0] == 401
        assert post(running, body, path="/api/models?token=t0k")[0] == 401
        assert not settings_file(fx).exists()
        assert post(running, body, Authorization="Bearer t0k")[0] == 200
        assert post(running, body, Cookie="al_dashboard_token=t0k")[0] == 200
    finally:
        stop_server(running)


def test_read_only_mode_refuses_and_says_so(fx):
    running = _start(fx, "--read-only")
    try:
        status, data = post(running, {"layer": "demo-shop", "tier": "heavy", "model": "claude-fable-5-2"})
        assert status == 403 and data["error"] == "the dashboard was started with --read-only"
        models = view(running)
        assert models["writable"] is False and models["why"] == "the dashboard was started with --read-only"
        assert not settings_file(fx).exists()
    finally:
        stop_server(running)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes anywhere")
def test_unwritable_settings_directory_says_why(models_server, fx):
    directory = settings_file(fx).parent
    directory.mkdir(parents=True)
    directory.chmod(0o500)
    try:
        models = view(models_server)
        assert models["writable"] is False and "agents/settings is not writable here" in models["why"]
        status, data = post(models_server, {"layer": "demo-shop", "tier": "heavy", "model": "claude-fable-5-2"})
        assert status == 409 and "al models LAYER --set TIER=ID" in data["error"]
        assert not settings_file(fx).exists()
    finally:
        directory.chmod(0o700)


def test_unknown_layer_is_404(models_server):
    assert post(models_server, {"layer": "nope", "tier": "heavy", "model": "claude-fable-5-2"})[0] == 404
