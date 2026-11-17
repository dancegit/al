"""The stand-in in the generated layer: `al gen` writes adws/standin.py (the one entry of the
dashboard, `al decider` and the loop) and, with #:systemd #t, a unit that runs the loop only;
`al explain` says what the stand-in may do alone; and the dashboard, which never imports a layer's
runtime, holds the same decision kinds and writes the same settlement as the runtime does.
Run with -s to see the commands' output (docs/GUIDE.md quotes it)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from conftest import KIT, LAYER_TEMPLATE, al, sh

EXAMPLE = ("(stand-in auto #:model-set heavy #:may (resume restart answer integrate settle)\n"
           "            #:never (land promote) #:max-actions-per-run 3 #:max-per-hour 12\n"
           "            #:escalate-to (dashboard notify))\n  (notify email)")


def show(cp) -> str:
    print(f"$ al {' '.join(str(a) for a in cp.args[1:])}\n{cp.stdout}{cp.stderr}", end="")
    return cp.stdout


def layer_with(app: Path, clause: str, name: str = "notes.al") -> Path:
    layer = app.parent / name
    layer.mkdir()
    text = LAYER_TEMPLATE.format(root="../notes", placement=f"(layer sibling)\n  {clause}")
    (layer / "agentic.rkt").write_text(text)
    al("gen", layer)
    return layer


def test_al_gen_writes_the_standin_script_and_al_check_needs_it(sibling) -> None:
    script = sibling / "adws" / "standin.py"
    assert script.is_file() and script.stat().st_mode & 0o111
    assert script.read_text().startswith("#!/usr/bin/env -S uv run --quiet --script\n")
    assert (sibling / "adws" / "adw_modules" / "standin.py").is_file()
    assert al("check", sibling).stdout.strip().endswith("check: ok")
    cp = sh(["uv", "run", "--quiet", script, "status", "--json"], cwd=sibling)
    assert cp.returncode == 0, cp.stderr
    view = json.loads(cp.stdout.splitlines()[-1])
    assert (view["mode"], view["source"], view["may"], view["declared"]) == ("off", "default", ["resume"], None)
    assert view["counts"] == {"today": 0, "hour": 0, "waiting": 0} and view["log"] == []
    cp = sh(["uv", "run", "--quiet", script, "set", "--mode", "suggest", "--via", "dashboard", "--json"], cwd=sibling)
    assert cp.returncode == 0 and json.loads(cp.stdout.splitlines()[-1])["mode"] == "suggest"
    assert json.loads((sibling / "agents" / "settings" / "decider.json").read_text()) == {"mode": "suggest", "version": 1}
    bad = sh(["uv", "run", "--quiet", script, "set", "--may", "land", "--json"], cwd=sibling)
    assert bad.returncode == 2 and "land is not in the layer's (stand-in … #:may)" in json.loads(bad.stdout.splitlines()[-1])["error"]
    script.unlink()
    cp = al("check", sibling, ok=False)
    assert cp.returncode == 1 and "PROBLEM: missing adws/standin.py" in cp.stdout


def test_systemd_unit_runs_the_loop_only(app, sibling) -> None:
    assert not (sibling / "systemd" / "al-notes-stand-in.service").exists()      # not declared: no unit
    layer = layer_with(app, "(stand-in suggest #:systemd #t)", name="loop.al")
    unit = (layer / "systemd" / "al-notes-stand-in.service").read_text()
    print(unit)
    assert f"ExecStart=/usr/bin/env uv run {layer}/adws/dashboard.py --stand-in-only\n" in unit
    assert "Description=Stand-in of the notes agentic layer" in unit and "Restart=on-failure" in unit
    assert "systemctl --user enable --now al-notes-stand-in" in unit.splitlines()[0]
    plain = layer_with(app, "(stand-in suggest)", name="plain.al")
    assert not (plain / "systemd" / "al-notes-stand-in.service").exists()


def test_al_explain_says_what_the_stand_in_may_do_alone(app, sibling) -> None:
    layer = layer_with(app, EXAMPLE, name="declared.al")
    out = show(al("explain", layer))
    assert "Who stands in for a person" in out
    flat = " ".join(out.split())
    assert ("A stand-in (auto) reads what a person would read when a run needs one. Alone it may: resume, restart, "
            "answer, integrate and settle. It only proposes: retry-job. It never touches: land and promote. At most 3 "
            "actions per run and 12 per hour; after that, and whenever it is unsure, the item goes to a person "
            "(the dashboard's Needs you list and a notification).") in flat
    for needle in ("never overrides a rejected review", "never approves its own work", "only ask the gate again"):
        assert needle in flat, needle
    plain = al("explain", sibling).stdout
    assert "No stand-in is declared" not in plain and "Who stands in for a person" not in plain
    # the paragraph of a layer without a declaration, for whoever asks the function
    spec = importlib.util.spec_from_file_location("al_explain_standin", KIT.parent / "tools" / "explain.py")
    explain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(explain)
    title, text, bullets = explain.standin_text({"standIn": None})
    assert title == "Who stands in for a person" and bullets == []
    assert text == ("No stand-in is declared. A person can switch one on for this machine (al decider, the dashboard); "
                    "it may then only resume a stopped run.")


def test_dashboard_kinds_and_settlement_format_equal_the_runtime_s(sibling) -> None:
    spec = importlib.util.spec_from_file_location("al_dashboard_for_standin", KIT / "dashboard" / "dashboard.py")
    dashboard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dashboard
    spec.loader.exec_module(dashboard)
    for name in [n for n in sys.modules if n == "adw_modules" or n.startswith("adw_modules.")]:
        del sys.modules[name]
    sys.path.insert(0, str(sibling / "adws"))
    try:
        from adw_modules import decide, ir, standin, state as state_module
    finally:
        sys.path.remove(str(sibling / "adws"))
    assert tuple(dashboard.STANDIN_KINDS) == tuple(standin.KINDS)
    assert tuple(dashboard.DECIDER_MODES) == tuple(decide.MODES)
    assert tuple(dashboard.SETTLE_AS) == tuple(decide.SETTLE_AS)
    assert dashboard.STANDIN_SCRIPT == "adws/standin.py" and (sibling / dashboard.STANDIN_SCRIPT).is_file()
    for run in ("5e771e01", "5e771e02"):
        state_module.ADWState(adw_id=run, adw="change", prompt="p", status="failed", ended=state_module.now()).save(sibling)
    layer = ir.load(sibling)
    by_runtime = standin.settle(layer, state_module.load(sibling, "5e771e01"), "abandoned", note="nobody needs it")
    by_page = dashboard.write_settlement(sibling, "5e771e02", as_="abandoned", note="nobody needs it", who="stand-in",
                                         via="stand-in", status="failed")
    read_runtime = dashboard.read_settlement(sibling, "5e771e01")
    read_page = dashboard.read_settlement(sibling, "5e771e02")
    assert read_runtime == by_runtime and set(read_runtime) == set(read_page) == {"as", "by", "note", "who", "at", "via"}
    assert {k: v for k, v in read_runtime.items() if k != "at"} == {k: v for k, v in read_page.items() if k != "at"}
    assert read_runtime["at"].endswith("Z") and len(read_runtime["at"]) == len(read_page["at"]) == len(by_page["at"])
    # a second settlement by the runtime keeps the first under `previous`, as the dashboard's does
    standin.settle(layer, state_module.load(sibling, "5e771e01"), "superseded", by="5e771e02")
    dashboard.write_settlement(sibling, "5e771e02", as_="superseded", by="5e771e01", who="stand-in", via="stand-in", status="failed")
    again_runtime, again_page = (dashboard.read_settlement(sibling, run) for run in ("5e771e01", "5e771e02"))
    assert set(again_runtime) == set(again_page) and set(again_runtime["previous"]) == set(again_page["previous"])
    assert (again_runtime["as"], again_runtime["by"], again_runtime["previous"]["as"]) == ("superseded", "5e771e02", "abandoned")
