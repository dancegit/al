from __future__ import annotations

import json
from pathlib import Path

import pytest

from adw_modules.errors import LayerError
from adw_modules.ir import load


def test_layer_lookups_and_full_ir(make_layer: object) -> None:
    layer_path = make_layer({"adws": [{"name": "demo"}], "hops": [{"name": "work", "template": "run-plan"}]})
    layer = load(layer_path)
    assert layer.adw("demo")["name"] == "demo"
    assert layer.command("review")["isHop"] is False
    assert layer.command("work")["isHop"] is True
    assert layer.option("agent", "mock-agent")["kind"] == "mock"
    full = load(Path(__file__).with_name("full.layer.json"))
    assert full.adw("sdlc")["name"] == "sdlc"
    assert full.command("implement")["isHop"] is True
    assert full.cmd("test")["timeout"] == 1800


def test_planned_option_is_refused(make_layer: object) -> None:
    layer = load(make_layer({"agents": [{"id": "later", "kind": "mock", "scriptDir": "mock", "status": "planned", "timeout": 30}]}))
    with pytest.raises(LayerError, match="planned"):
        layer.option("agents", "later")


def test_unknown_family_and_missing_items_are_clear(make_layer: object) -> None:
    layer = load(make_layer())
    with pytest.raises(LayerError, match="Unknown option family"):
        layer.option("mystery", "x")
    with pytest.raises(LayerError, match="Unknown command"):
        layer.command("missing")
