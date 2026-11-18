"""The registry (adw_modules/registry.py): what a layer can run, built from the compiled full example
plus the kit. It may not import the modules that own three of its rules, so these tests keep it
equal to them: integrate.py (name, step names, settings file), promotion.py (names and steps),
launch.py (single-command names, who ships) and inputs.py (the script of a workflow). They also
check the two runtime readers: launch.resolve_target and the decider's resume."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from adw_modules import decide, inputs, integrate, ir, launch, promotion, registry
from adw_modules.errors import LayerError
from adw_modules.state import ADWState

FULL = json.loads((Path(__file__).resolve().parent / "full.layer.json").read_text(encoding="utf-8"))
ADW_ID = "ab12cd34"


@pytest.fixture
def reg() -> dict:
    return registry.build(FULL)


def names(entries: list[dict]) -> list[str]:
    return [e["name"] for e in entries]


# ------------------------------------------------------------------------------ what is in it

def test_the_module_imports_nothing_of_the_runtime():
    """The dashboard and tools/explain.py load it by path: standard library only, no relative import."""
    tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
    imported = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(isinstance(node, ast.Import) or node.level == 0 for node in imported)
    modules = {alias.name.split(".")[0] for node in imported if isinstance(node, ast.Import) for alias in node.names}
    modules |= {str(node.module).split(".")[0] for node in imported if isinstance(node, ast.ImportFrom)}
    assert modules <= {"__future__", "json", "pathlib", "typing"}, modules


def test_full_example_lists_everything_it_can_run(reg):
    assert reg["version"] == 1 and reg["problems"] == []
    assert names(registry.declared(reg)) == [a["name"] for a in FULL["adws"]]
    assert names(registry.commands(reg)) == [c["name"] for c in FULL["commands"]] + [h["name"] for h in FULL["hops"]]
    assert names(registry.kit(reg)) == ["integrate", "promote-sprint-main", "promote-hotfix-main", "improve", "decider"]
    assert names(registry.suite(reg)) == ["changelog"]
    # the order: declared workflows, kit rows, commands and hops, suite commands
    kinds = [e["kind"] for e in reg["entries"]]
    assert kinds == sorted(kinds, key=["workflow", "kit-workflow", "decider", "command", "hop", "suite-command"].index)
    for entry in reg["entries"]:
        assert tuple(entry) == registry.ENTRY_KEYS, entry["name"]
        assert set(entry["start"]) == {"by_hand", "how", "command", "why"}, entry["name"]
        assert entry["input"]["kind"] in ("request", "run", "stages", "none"), entry["name"]
        assert entry["setting"] is None or {"where", "key", "on", "turn_on"} <= set(entry["setting"]), entry["name"]
        assert entry["available"] is True, "without a layer directory every script counts as present"
    json.dumps(reg)   # dispatch.py --registry --json prints it


def test_declared_workflow_entry(reg, make_layer):
    zte = registry.entry(reg, "zte")
    declared = next(a for a in FULL["adws"] if a["name"] == "zte")
    assert zte["kind"] == "workflow" and zte["source"] == "declared" and zte["definition"] == declared
    assert zte["steps"] == [s["name"] for s in declared["steps"]] and zte["script"] == "adws/adw_zte.py"
    assert zte["input"] == {"kind": "request"} and zte["start"]["by_hand"] is True and zte["start"]["how"] == "form"
    layer = ir.load(make_layer(FULL))
    for adw in FULL["adws"]:
        assert registry.entry(reg, adw["name"])["ships"] == launch.ships_unattended(layer, adw), adw["name"]
    assert {registry.entry(reg, a["name"])["ships"] for a in FULL["adws"]} == {"", "deploy"}
    assert registry.start_answer(reg, "zte") == "", "a declared workflow is started through launch.start"


def test_integrate_agrees_with_the_integrate_module(reg):
    entry = registry.entry(reg, integrate.ADW_NAME)
    assert registry.INTEGRATE == integrate.ADW_NAME and entry["adw"] == integrate.ADW_NAME
    assert registry.INTEGRATE_STEP_NAMES == integrate.STEP_NAMES
    assert registry.INTEGRATE_SETTINGS == integrate.SETTINGS_SHOWN == entry["setting"]["where"]
    assert set(registry.INTEGRATE_KEYS) == set(integrate.DEFAULTS)
    assert registry.INTEGRATE_APPROVALS == integrate.APPROVALS
    assert entry["script"] == integrate.SCRIPT.as_posix()
    assert entry["kind"] == "kit-workflow" and entry["source"] == "kit" and entry["input"]["kind"] == "run"
    assert entry["steps"] == ["prepare", "resolve", "checks", "review", "ship"], "approve only with approval person"
    assert set(entry["steps"]) & set(integrate.STEP_NAMES) == {"prepare", "resolve"}
    assert entry["start"] == {"by_hand": True, "how": "form", "command": "al integrate LAYER RUN", "why": ""}
    assert entry["setting"]["key"] == "enabled" and entry["setting"]["on"] is True and entry["off"] is False
    assert "zte" in entry["purpose"], "the workflows whose runs it serves"


def test_promotions_agree_with_the_promotion_module(reg):
    assert registry.PROMOTION_STEPS == promotion.STEPS
    assert [promotion.adw_name(p) for p in FULL["promotions"]] == ["promote-sprint-main", "promote-hotfix-main"]
    for promo in FULL["promotions"]:
        entry = registry.entry(reg, promotion.adw_name(promo))
        assert entry["name"] == entry["adw"] == registry.promotion_name(promo) == promotion.adw_name(promo)
        assert entry["steps"] == registry.promotion_steps(promo) == promotion.promotion_steps(promo)
        assert [s["name"] for s in entry["definition"]["steps"]] == entry["steps"]
        assert entry["definition"]["promotion"] == {"from": promo["from"], "to": promo["to"]}
        assert entry["input"] == {"kind": "stages", "what": "two stages", "from": promo["from"], "to": promo["to"]}
        assert entry["start"]["command"] == f"al promote LAYER {promo['from']} {promo['to']}"
        assert entry["script"] == "adws/promote.py" and entry["ships"] == promo["method"] and entry["off"] is False
    for changed in ({"requires": []}, {"version": {"how": "bump"}, "tag": "v1"}, {"after": ["x"], "backMerge": ["y"]}):
        promo = {**FULL["promotions"][0], **changed}
        assert registry.promotion_steps(promo) == promotion.promotion_steps(promo), changed


def test_a_layer_without_stages_lists_promote_as_off():
    plain = {k: v for k, v in FULL.items() if k not in ("promotions", "stages")}
    reg = registry.build(plain)
    entry = registry.entry(reg, "promote")
    assert entry["off"] is True and entry["available"] is False and entry["start"]["by_hand"] is False
    assert "(delivery sprint)" in entry["start"]["why"] and "al gen" in entry["start"]["why"]
    assert entry["setting"]["on"] is False and entry["setting"]["turn_on"] == entry["start"]["why"]
    assert "adws/promote.py" not in reg["scripts"] and reg["reserved"] == ["integrate"]


def test_single_commands_agree_with_launch(reg, make_layer):
    layer = ir.load(make_layer(FULL))
    for entry in registry.commands(reg):
        assert entry["adw"] == launch.COMMAND_PREFIX + entry["name"]
        assert entry["steps"] == [entry["name"]] and entry["ships"] == "" and entry["script"] == "adws/dispatch.py"
        assert entry["definition"] == launch.single_command_adw(layer, entry["name"]), entry["name"]
        assert registry.entry(reg, entry["adw"]) is entry
    assert registry.COMMAND_PREFIX == launch.COMMAND_PREFIX
    assert registry.entry(reg, "implement")["kind"] == "hop" and registry.entry(reg, "classify")["kind"] == "command"
    assert launch.catalog_names(layer) == (names(registry.declared(reg)), names(registry.commands(reg)))


def test_improve_decider_and_suite_commands(reg):
    improve = registry.entry(reg, "improve")
    assert improve["steps"] == list(registry.IMPROVE_STEPS) and improve["script"] == "" and improve["setting"] is None
    assert improve["start"]["by_hand"] is False and improve["start"]["how"] == "cli"
    assert improve["start"]["command"] == 'al improve LAYER "…"' and "a person always approves" in improve["start"]["why"]
    assert improve["ships"] == "a pull request a person merges"
    decider = registry.entry(reg, "decider")
    assert registry.DECIDER_SETTINGS == decide.SETTINGS_FILE.as_posix() == decider["setting"]["where"]
    assert registry.DECIDER_MODES == decide.MODES
    assert decider["kind"] == "decider" and decider["off"] is True and decider["setting"]["on"] is False
    assert decider["setting"]["turn_on"] == "al decider LAYER --set suggest|auto" and decider["input"]["kind"] == "run"
    assert "al answer LAYER RUN" in decider["start"]["why"] and decider["start"]["by_hand"] is False
    changelog = registry.entry(reg, "changelog")
    assert changelog["kind"] == "suite-command" and changelog["source"] == "suite" and changelog["input"] == {"kind": "none"}
    assert changelog["used_by"] == [{"workflow": "sdlc", "step": "changelog"}]
    assert changelog["start"] == {"by_hand": False, "how": "", "command": "", "why": "runs as step changelog of workflow sdlc"}


def test_scripts_and_reserved_names(reg):
    scripts = reg["scripts"]
    assert {path: s["role"] for path, s in scripts.items()} == {
        **{f"adws/adw_{a['name']}.py": "workflow" for a in FULL["adws"]},
        "adws/dispatch.py": "launcher", "adws/integrate.py": "kit-workflow", "adws/promote.py": "kit-workflow",
        "adws/triggers/trigger_cron.py": "trigger", "adws/triggers/trigger_webhook.py": "trigger",
        "adws/triggers/trigger_voice.py": "trigger", "adws/voice/listen.py": "voice", "adws/voice/say.py": "voice",
        "adws/dashboard.py": "dashboard"}
    assert scripts["adws/promote.py"]["entries"] == ["promote-sprint-main", "promote-hotfix-main"]
    assert scripts["adws/integrate.py"]["entries"] == ["integrate"] and scripts["adws/adw_zte.py"]["entries"] == ["zte"]
    assert scripts["adws/dispatch.py"]["entries"] == names(registry.commands(reg))
    assert sorted(registry.VOICE_SCRIPTS) == sorted(
        p.name for p in (Path(registry.__file__).resolve().parents[1] / "voice").glob("*.py"))
    assert set(registry.TRIGGER_SCRIPTS.values()) == {
        p.name for p in (Path(registry.__file__).resolve().parents[1] / "triggers").glob("*.py")}
    assert reg["reserved"] == ["integrate", "promote-sprint-main", "promote-hotfix-main"]
    assert "improve" not in reg["reserved"] and "decider" not in reg["reserved"]


def test_a_declared_workflow_keeps_a_name_the_kit_does_not_reserve():
    """kit/self/agentic.rkt.in declares `improve` itself: the declared workflow is the entry."""
    own = {**FULL, "adws": [*FULL["adws"], {**FULL["adws"][0], "name": "improve"}]}
    reg = registry.build(own)
    assert [e["source"] for e in reg["entries"] if e["name"] == "improve"] == ["declared"]
    assert registry.start_answer(reg, "improve") == ""


# ------------------------------------------------------------------------------ settings and scripts

def test_settings_file_that_says_off_gives_off_with_the_reason(make_layer):
    layer_dir = make_layer(FULL)
    (layer_dir / "adws").mkdir()
    for script in ("integrate.py", "promote.py", "dispatch.py"):
        (layer_dir / "adws" / script).write_text("")
    on = registry.entry(registry.build(FULL, layer_dir), "integrate")
    assert on["off"] is False and on["available"] is True and on["setting"]["on"] is True
    settings = layer_dir / integrate.SETTINGS_FILE
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"version": 1, "enabled": False}))
    assert integrate.settings(layer_dir)["enabled"] is False
    reg = registry.build(FULL, layer_dir)
    off = registry.entry(reg, "integrate")
    assert off["off"] is True and off["setting"]["on"] is False and reg["problems"] == []
    assert off["start"]["why"] == "switched off in templates/integrate.json"
    settings.write_text(json.dumps({"version": 1, "approval": "person"}))
    assert registry.entry(registry.build(FULL, layer_dir), "integrate")["steps"] == [
        "prepare", "resolve", "checks", "review", "approve", "ship"]
    # a file the runtime refuses is a reported problem, never a crash
    for text, words in (("{broken", "not JSON"), (json.dumps({"colour": "red"}), "unknown key colour"),
                        (json.dumps({"enabled": "yes"}), "enabled is true or false")):
        settings.write_text(text)
        with pytest.raises(LayerError):
            integrate.settings(layer_dir)
        reg = registry.build(FULL, layer_dir)
        broken = registry.entry(reg, "integrate")
        assert broken["off"] is True and words in broken["start"]["why"] and reg["problems"] == [broken["start"]["why"]]
        assert broken["start"]["why"].startswith("templates/integrate.json")
    settings.unlink()
    (layer_dir / "adws" / "integrate.py").unlink()
    missing = registry.entry(registry.build(FULL, layer_dir), "integrate")
    assert missing["off"] is True and missing["available"] is False
    assert missing["start"]["why"] == "run `al gen` to add adws/integrate.py"


def test_decider_setting_is_read(make_layer):
    layer_dir = make_layer(FULL)
    assert registry.entry(registry.build(FULL, layer_dir), "decider")["off"] is True
    decide.write_mode(layer_dir, "suggest")
    entry = registry.entry(registry.build(FULL, layer_dir), "decider")
    assert entry["off"] is False and entry["setting"]["on"] is True and entry["setting"]["value"] == "suggest"
    (layer_dir / decide.SETTINGS_FILE).write_text("{broken")
    reg = registry.build(FULL, layer_dir)
    assert registry.entry(reg, "decider")["off"] is True and reg["problems"] == ["agents/settings/decider.json: not JSON"]


def test_integrate_is_never_offered_without_a_workflow_that_merges():
    reviews = [{**r, "kind": "human"} for r in FULL["reviews"]]
    entry = registry.entry(registry.build({**FULL, "reviews": reviews}), "integrate")
    assert entry["start"]["by_hand"] is False and entry["start"]["why"].startswith("never offered in this layer")


# ------------------------------------------------------------------------------ the runtime's readers

def test_resolve_target_answers_a_kit_name_with_the_registry_s_sentence(make_layer, reg):
    layer = ir.load(make_layer(FULL))
    with pytest.raises(LayerError) as exc:
        launch.resolve_target(layer, "/integrate x")
    assert str(exc.value) == registry.start_answer(reg, "integrate")
    assert str(exc.value) == "integrate is a kit workflow that takes a handed-over run: al integrate LAYER RUN"
    with pytest.raises(LayerError) as exc:
        launch.resolve_target(layer, "/promote-sprint-main")
    assert str(exc.value) == "promote-sprint-main is a kit workflow that takes two stages: al promote LAYER sprint main"
    with pytest.raises(LayerError, match=r'improve is a kit workflow that takes a request: al improve LAYER "…"'):
        launch.resolve_target(layer, "/improve make it faster")
    with pytest.raises(LayerError, match="decider is the stand-in for a person"):
        launch.resolve_target(layer, "/decider")
    with pytest.raises(LayerError) as exc:
        launch.resolve_target(layer, "/nope x")
    known = str(exc.value).split("known: ", 1)[1]
    assert known == ", ".join(registry.known_names(reg))
    assert known.startswith("/plan-build, /sdlc, /zte, ") and known.endswith(
        "/integrate (kit), /promote-sprint-main (kit), /promote-hotfix-main (kit), /improve (kit), /decider (kit)")
    assert launch.resolve_target(layer, "/zte ship it") == launch.Target("workflow", "zte", "ship it")
    assert launch.resolve_target(layer, "/classify this") == launch.Target("command", "classify", "this")
    result = launch.start(layer, launch.Target("workflow", "integrate"), "x", request_key="key-0001-abcdef")
    assert result["status"] == "invalid" and result["error"] == registry.start_answer(reg, "integrate")


def _state(adw: str, definition: dict | None = None, mode: str = "auto", dry_run: bool = False) -> ADWState:
    state = ADWState(adw_id=ADW_ID, adw=adw, prompt="p", dry_run=dry_run, definition=definition or {})
    state.decision = {"mode": mode, "status": "resuming"}
    return state


def test_the_decider_resumes_each_kind_of_run_through_a_script_that_exists(make_layer):
    layer_dir = make_layer(FULL)
    layer = ir.load(layer_dir)
    adws = layer_dir / "adws"
    # a declared workflow: unchanged
    argv = decide.resume_argv(layer, _state("zte"))
    assert argv == [sys.executable, str(inputs.adw_script(layer_dir, "zte")), "--resume", "--adw-id", ADW_ID,
                    "--decider", "auto"]
    assert decide.resume_argv(layer, _state("zte", mode="suggest", dry_run=True))[-3:] == ["--decider", "suggest", "--dry-run"]
    adws.mkdir()
    (adws / "adw_plan_build.py").write_text("")          # the second spelling inputs.adw_script looks for
    assert decide.resume_argv(layer, _state("plan-build"))[1] == str(inputs.adw_script(layer_dir, "plan-build"))
    assert decide.resume_argv(layer, _state("plan-build"))[1].endswith("adw_plan_build.py")
    # a single-command run: the launcher's --command form (dispatch.py), never adws/adw_command:NAME.py
    definition = launch.single_command_adw(layer, "classify")
    argv = decide.resume_argv(layer, _state(definition["name"], definition))
    assert argv == [sys.executable, str(adws / "dispatch.py"), "--command", "classify", "--resume", "--adw-id", ADW_ID,
                    "--decider", "auto"]
    assert "--command NAME [runner flags" in (Path(registry.__file__).resolve().parents[1] / "dispatch.py").read_text()
    # a promotion run: promote.py's own `run FROM TO --resume --adw-id ID` (it has no --decider flag)
    promo = FULL["promotions"][0]
    definition = {"name": promotion.adw_name(promo), "promotion": {"from": "sprint", "to": "main"}, "steps": []}
    argv = decide.resume_argv(layer, _state(definition["name"], definition, dry_run=True))
    assert argv == [sys.executable, str(adws / "promote.py"), "run", "sprint", "main", "--resume", "--adw-id", ADW_ID,
                    "--dry-run"]
    parsed = promotion._parser().parse_args(argv[2:])
    assert (parsed.from_, parsed.to, parsed.resume, parsed.adw_id, parsed.dry_run) == ("sprint", "main", True, ADW_ID, True)
    # an integration run: integrate.py's `run ORIGINAL --resume --adw-id ID`
    definition = {"name": "integrate", "integration": {"run": "f0000001"}, "steps": []}
    argv = decide.resume_argv(layer, _state("integrate", definition))
    assert argv == [sys.executable, str(adws / "integrate.py"), "run", "f0000001", "--resume", "--adw-id", ADW_ID]
    parsed = integrate._parser().parse_args(argv[2:])
    assert (parsed.command, parsed.run, parsed.resume, parsed.adw_id) == ("run", "f0000001", True, ADW_ID)
    # a kit run that recorded no definition: no script continues it
    assert decide.resume_argv(layer, _state("integrate")) is None
    assert decide.resume_argv(layer, _state("promote-sprint-main")) is None
    assert registry.resume_words(layer_dir, "zte", {}, ADW_ID) == f"uv run adws/adw_zte.py --resume --adw-id {ADW_ID}"
    assert registry.resume_words(layer_dir, "integrate", {}, ADW_ID) == ""


def test_after_run_says_why_a_kit_run_is_not_resumed(make_layer, monkeypatch, capsys):
    layer = ir.load(make_layer(FULL))
    started: list = []
    monkeypatch.setattr(decide, "spawn_resume", lambda layer, state: started.append(state.adw))
    decide.after_run(layer, _state("integrate"))
    out = capsys.readouterr().out
    assert started == [] and "is not resumed by the decider" in out and "integrate" in out
    assert "stays with a person" in out


def test_dispatch_prints_the_registry(make_layer, tmp_path):
    import shutil
    import subprocess

    layer_dir = make_layer(FULL)
    runtime = Path(registry.__file__).resolve().parents[1]
    (layer_dir / "adws").mkdir()
    shutil.copytree(runtime / "adw_modules", layer_dir / "adws" / "adw_modules",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(runtime / "dispatch.py", layer_dir / "adws" / "dispatch.py")
    before = sorted(str(p) for p in layer_dir.rglob("*") if "__pycache__" not in str(p))
    done = subprocess.run([sys.executable, str(layer_dir / "adws" / "dispatch.py"), "--registry", "--json"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert len(done.stdout.strip().splitlines()) == 1
    printed = json.loads(done.stdout)
    assert printed == json.loads(json.dumps(registry.build(FULL, layer_dir)))
    assert "integrate" in [e["name"] for e in printed["entries"]]
    words = subprocess.run([sys.executable, str(layer_dir / "adws" / "dispatch.py"), "--registry"],
                           capture_output=True, text=True, timeout=60)
    assert words.returncode == 0 and "integrate" in words.stdout and "kit-workflow" in words.stdout
    assert sorted(str(p) for p in layer_dir.rglob("*") if "__pycache__" not in str(p)) == before, "read only"
