"""`al models`: a layer's local model setting (agents/settings/models.json) from the command line,
the `al check` rule that keeps it out of git, the same rules in the dashboard's Models panel, and
runs that use what either of them wrote."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import KIT, al, git, sh, status

SETTINGS = Path("agents") / "settings" / "models.json"


def runtime_models():
    if str(KIT / "runtime") not in sys.path:
        sys.path.insert(0, str(KIT / "runtime"))
    from adw_modules import models
    assert Path(models.__file__).resolve() == (KIT / "runtime" / "adw_modules" / "models.py").resolve()
    return models


def dashboard_module():
    spec = importlib.util.spec_from_file_location("al_dashboard_under_test", KIT / "dashboard" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rows(out: str) -> dict[str, tuple[str, str]]:
    found = {}
    for line in out.splitlines():
        match = re.fullmatch(r"(\S+)\s+(\S+)\s+\((.+)\)", line)
        if match and not line.startswith(("note:", "settings:")):
            found[match.group(1)] = (match.group(2), match.group(3))
    return found


def dry_run(layer: Path, app: Path) -> tuple[str, dict]:
    env = {k: v for k, v in os.environ.items() if k not in ("AL_MODELS", "AL_MODEL_SET", "AL_MAX_COST")}
    cp = sh(["uv", "run", "--quiet", layer / "adws" / "adw_change.py", "add a note", "--dry-run"], cwd=app, env=env)
    state_line = next(l for l in cp.stdout.splitlines() if l.startswith("state:"))
    return cp.stdout, json.loads(Path(state_line.split(None, 1)[1]).read_text())


def test_al_models_prints_every_tier_with_its_source(in_repo) -> None:
    out = al("models", in_repo).stdout
    print(out)
    table = rows(out)
    assert set(table) == {"base", "heavy"}
    assert table["base"] == ("claude-opus-5-5", "built-in default")
    assert {source for _model, source in table.values()} <= {"layer", "built-in default"}
    assert (f"settings: {in_repo / SETTINGS} (local to this machine, never committed); "
            "applies to runs started from now on") in out


def test_unknown_tier_and_bad_model_id_are_errors(in_repo) -> None:
    for item, message in (("nope=x", "no agent of this layer has a tier `nope`"),
                          ("heavy=", "expected TIER=MODEL_ID"),
                          ("heavy=a b", "model id must be a non-empty string without whitespace")):
        cp = al("models", in_repo, "--set", item, ok=False)
        assert cp.returncode == 1 and message in cp.stderr, cp.stderr
        assert not (in_repo / SETTINGS).exists()
    cp = al("models", in_repo, "--set", "heavy=my-model")
    assert "note: my-model is not a known model id" in cp.stdout
    assert rows(cp.stdout)["heavy"] == ("my-model", "local setting")
    assert al("models", in_repo, "--reset", "nope", ok=False).returncode == 1
    assert al("models", in_repo, "--set", "heavy=x", "--reset", ok=False).returncode == 2   # one or the other


def test_al_check_fails_when_the_settings_file_is_tracked(in_repo, app) -> None:
    al("models", in_repo, "--set", "heavy=claude-fable-5-2")
    assert al("check", in_repo).returncode == 0            # ignored: a local setting is fine
    git(app, "add", "-f", "agentic/agents/settings/models.json")
    cp = al("check", in_repo, ok=False)
    assert cp.returncode == 1
    assert ("PROBLEM: agents/settings/models.json is a local setting and must not be committed: "
            "git rm --cached agentic/agents/settings/models.json") in cp.stdout
    git(app, "rm", "-q", "--cached", "agentic/agents/settings/models.json")
    assert al("check", in_repo).stdout.strip().endswith("check: ok")


def test_set_and_reset_change_what_a_dry_run_uses(in_repo, app) -> None:
    ir_heavy = next(m["model"] for m in json.loads((in_repo / "agentic-layer.json").read_text())["agents"][0]["models"]
                    if m["tier"] == "heavy")
    al("models", in_repo, "--set", "heavy=claude-fable-5-2")
    out, state = dry_run(in_repo, app)
    assert "heavy=claude-fable-5-2 (local setting)" in next(l for l in out.splitlines() if l.startswith("tiers:"))
    assert state["models"]["heavy"] == {"model": "claude-fable-5-2", "source": "local setting"}
    al("models", in_repo, "--reset")
    assert not (in_repo / SETTINGS).exists()
    out, state = dry_run(in_repo, app)
    assert state["models"]["heavy"]["model"] == ir_heavy and state["models"]["heavy"]["source"] != "local setting"
    assert f"heavy={ir_heavy} (" in out
    assert status(app) == ""


def test_cli_and_dashboard_rules_are_the_same(tmp_path) -> None:
    models, dash = runtime_models(), dashboard_module()
    assert models.KNOWN_MODELS == dash.KNOWN_MODELS
    assert models.BUILTIN_TIERS == dash.BUILTIN_TIERS
    assert models.SETTINGS_FILE.as_posix() == dash.SETTINGS_FILE
    assert models.MODEL_ID_RX.pattern == dash.MODEL_ID_RX.pattern and models.TIER_RX.pattern == dash.TIER_RX.pattern

    def outcome(check, *args):
        try:
            return ("ok", check(*args))
        except ValueError as exc:
            return ("error", str(exc))
    known = ["base", "heavy", "fast"]
    ids = ["claude-fable-5-2", "gpt-5.4", "my-model", "", "a b", "x\n", "x\ty", "x\x00", "x" * 200, "x" * 201, "ü-model"]
    for tier in ("base", "heavy", "fast", "Heavy", "nope", "", "heavy\n"):
        for model in ids:
            assert outcome(models.check_setting, tier, model, known) == outcome(dash.check_setting, tier, model, known)
    for model in ids:
        assert outcome(models.check_model_id, model) == outcome(dash.check_model_id, model)
    ir = {"agents": [{"id": "c", "kind": "claude-code", "models": [{"tier": "base", "model": "a"},
                                                                   {"tier": "fast", "model": "b"}]},
                     {"id": "x", "kind": "codex"}, {"id": "m", "kind": "mock"}]}
    assert models.declared_tiers(SimpleNamespace(ir=ir)) == dash.declared_tiers(ir) == ["base", "fast", "heavy"]
    mapping = {"heavy": "claude-fable-5-2", "base": "my-model"}
    assert models.render_settings(mapping) == dash.render_settings(mapping)
    one, two = models.write_settings(tmp_path / "a", mapping), dash.write_settings(tmp_path / "b", mapping)
    assert one.read_bytes() == two.read_bytes()


def test_al_dashboard_read_only_is_passed_on(tmp_path) -> None:
    env = {"AL_DASHBOARD_DRY_RUN": "1"}
    argv = json.loads(al("dashboard", tmp_path, "--read-only", env=env).stdout)["argv"]
    assert "--read-only" in argv and argv[1].endswith("dashboard.py")
    argv = json.loads(al("dashboard", tmp_path, env=env).stdout)["argv"]
    assert "--read-only" not in argv
    argv = json.loads(al("dashboard", tmp_path, "--podman", "--read-only", env=env).stdout)["argv"]
    assert argv[0].endswith("run-in-podman.sh") and "--read-only" in argv


def test_a_run_started_after_a_dashboard_change_uses_it(in_repo, app) -> None:
    dash = dashboard_module()
    server = dash.make_server(dash.parse_args(["--layer", str(in_repo), "--port", "0", "--no-podman"]))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    try:
        port = server.server_address[1]
        key = server.model.layer_list()[0]["key"]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("POST", "/api/models", body=json.dumps({"layer": key, "tier": "heavy", "model": "claude-fable-5-2"}),
                           headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}",
                                    "Host": f"127.0.0.1:{port}", "X-AL-Dashboard": "1"})
        response = connection.getresponse()
        assert response.status == 200, response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
    written = (in_repo / SETTINGS).read_bytes()
    al("models", in_repo, "--reset")
    al("models", in_repo, "--set", "heavy=claude-fable-5-2")
    assert (in_repo / SETTINGS).read_bytes() == written          # the CLI writes the same bytes
    _out, state = dry_run(in_repo, app)
    assert state["models"]["heavy"] == {"model": "claude-fable-5-2", "source": "local setting"}
    assert status(app) == ""


@pytest.mark.parametrize("bad", ["{oops", '{"models": []}'])
def test_a_broken_settings_file_is_reported_and_a_bare_reset_removes_it(in_repo, bad) -> None:
    (in_repo / SETTINGS).parent.mkdir(parents=True, exist_ok=True)
    (in_repo / SETTINGS).write_text(bad)
    cp = al("models", in_repo, ok=False)
    assert cp.returncode == 1 and "al models LAYER --reset" in cp.stderr
    al("models", in_repo, "--reset")
    assert not (in_repo / SETTINGS).exists()
