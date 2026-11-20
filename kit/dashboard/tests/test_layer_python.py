"""The interpreter that runs a layer's scripts: the dashboard is a long-lived service, and the Python it
started with can disappear under it (a pruned or rebuilt uv cache). Every layer script then runs under
`uv run --quiet --script`, else `python3` from PATH; with neither, the error names the missing
interpreter and says to restart the dashboard instead of "failed (exit -1)". On the scenario of
assign_fixtures, through the in-process model."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

import assign_fixtures as af
import dashboard as d
import integrate_fixtures as fx


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("python") / "s"
    af.build_scenario(root)
    return root


@pytest.fixture()
def layer(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("AL_APP_ROOT", raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return root / "pantry.al"


def model_for(layer: Path, ticks: int = 3) -> d.Model:
    model = d.Model([layer], [])
    model.discover()
    for _ in range(ticks):
        model.tick()
    return model


def test_assign_and_integrate_previews_fall_back_when_the_interpreter_is_gone(
        layer: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uv = shutil.which("uv")
    assert uv, "this test needs uv on PATH"
    model = model_for(layer)
    monkeypatch.setattr(d.sys, "executable", str(tmp_path / "gone" / "bin" / "python"))
    assert d.layer_python()[:4] == [uv, "run", "--quiet", "--script"]
    status, body = model.assign_preview("pantry", af.REJECTED)
    assert status == 200, body
    assert body.get("kind")
    status, body = model.integrate_preview("pantry", fx.ACCEPTANCE)
    assert status == 200, body
    assert "eligible" in body


def test_python3_is_the_last_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = shutil.which("python3")
    assert real, "this test needs python3 on PATH"
    only = tmp_path / "bin"
    only.mkdir()
    (only / "python3").symlink_to(real)
    monkeypatch.setenv("PATH", str(only))
    monkeypatch.setattr(d.sys, "executable", str(tmp_path / "gone" / "bin" / "python"))
    assert d.layer_python() == [str(only / "python3")]
    assert d.layer_python(isolated=True)[-1] == "-I"


def test_no_interpreter_left_names_it_and_says_restart(layer: Path, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    model = model_for(layer)
    gone = str(tmp_path / "gone" / "bin" / "python")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(d.sys, "executable", gone)
    for status, body in (model.assign_preview("pantry", af.REJECTED),
                         model.integrate_preview("pantry", fx.ACCEPTANCE)):
        assert status == 502, body
        assert gone in body["error"]
        assert "restart the dashboard" in body["error"]
        assert "exit -1" not in body["error"]


def test_no_layer_script_runs_under_sys_executable_directly() -> None:
    source = Path(d.__file__).read_text(encoding="utf-8")
    assert re.findall(r"\[\s*sys\.executable\s*,", source) == []
    start = source.index("def layer_python(")
    end = source.index("\ndef ", start + 1)
    outside = [m.start() for m in re.finditer(r"sys\.executable", source) if not start <= m.start() < end]
    assert outside == []
