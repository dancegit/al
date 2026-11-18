"""The one description of what a layer can run: its declared workflows, the workflows the kit gives
every layer (`integrate`, promotions, `improve`), the stand-in decider, its commands and hops as
single-command runs, and the codebase commands its `script` steps run.

`build(ir, layer_dir)` returns it from the compiled layer (a plain dict) plus the KIT table below.
The dashboard (Layers view, start form, `/NAME` menu, run page), `al run`, `al explain`, the launcher
(`launch.catalog_names`, `launch.resolve_target`) and the decider's resume all read it, so a kit
workflow is declared in one place, KIT, and none of them keeps a list of its own.

Standard library only, and no import of another runtime module: the dashboard and tools/explain.py
load this file by path. It reads the layer's two setting files and looks whether a script exists;
it never reads the application and never runs git. Three rules are repeated here by value because
of that (the names and settings file of integrate.py, the names and steps of promotion.py, the
script lookup of inputs.adw_script); kit/runtime/tests/test_registry.py keeps them equal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

VERSION = 1
COMMAND_PREFIX = "command:"                                  # launch.COMMAND_PREFIX
INTEGRATE = "integrate"                                      # integrate.ADW_NAME
INTEGRATE_STEP_NAMES = ("prepare", "resolve", "approve")     # integrate.STEP_NAMES
INTEGRATE_SETTINGS = "templates/integrate.json"              # integrate.SETTINGS_SHOWN
INTEGRATE_KEYS = ("version", "enabled", "approval", "decider", "generated")   # integrate.DEFAULTS
INTEGRATE_APPROVALS = ("none", "person")                     # integrate.APPROVALS
PROMOTE = "promote"
PROMOTION_STEPS = ("open", "checks", "pack", "request", "approval", "version", "merge", "tag", "deploy",
                   "back-merge", "close")                    # promotion.STEPS
DECIDER_SETTINGS = "agents/settings/decider.json"            # decide.SETTINGS_FILE
DECIDER_MODES = ("off", "suggest", "auto")                   # decide.MODES
IMPROVE_STEPS = ("plan", "implement", "definition", "regenerate", "scope", "layer-check", "dry-runs", "verify",
                 "commit", "review", "ship")                 # the (steps …) of kit/self/agentic.rkt.in
TRIGGER_SCRIPTS = {"cron": "trigger_cron.py", "webhook": "trigger_webhook.py", "file-watch": "trigger_watch.py",
                   "voice": "trigger_voice.py"}              # tools/al Generator.adws
VOICE_SCRIPTS = ("listen.py", "say.py")                      # kit/runtime/voice
ENTRY_KEYS = ("name", "kind", "source", "purpose", "steps", "definition", "input", "start", "ships", "setting",
              "script", "available", "off", "adw", "used_by")
NO_STAGES = "declare stages, e.g. `(delivery sprint)` in agentic.rkt, then `al gen`"

# The only place a kit workflow is declared. `name` is the row; `integrate` gives one entry,
# `promote` one per declared promotion (or one that is off), `improve` and `decider` one each.
KIT: list[dict[str, Any]] = [
    {"name": INTEGRATE, "kind": "kit-workflow", "script": "adws/integrate.py",
     "purpose": "brings a handed-over run onto its target branch with an agent, when its review passed but the "
                "merge conflicted or the target moved",
     "input": {"kind": "run", "what": "a handed-over run"},
     "start": {"by_hand": True, "how": "form", "command": "al integrate LAYER RUN", "why": ""},
     "ships": "as the run's own workflow",
     "setting": {"where": INTEGRATE_SETTINGS, "key": "enabled", "turn_on": "al integrate LAYER --set enabled=true"}},
    {"name": PROMOTE, "kind": "kit-workflow", "script": "adws/promote.py",
     "purpose": "moves a stage into the next one once people accept it: checks, pack, approval, merge",
     "input": {"kind": "stages", "what": "two stages"},
     "start": {"by_hand": True, "how": "form", "command": "al promote LAYER FROM TO", "why": ""},
     "ships": "by the promotion's method",
     "setting": {"where": "agentic.rkt", "key": "stages", "turn_on": NO_STAGES}},
    {"name": "improve", "kind": "kit-workflow", "script": "",
     "purpose": "changes the layer itself from a request: the definition, its prompts and what is generated",
     "input": {"kind": "request", "what": "a request"},
     "start": {"by_hand": False, "how": "cli", "command": 'al improve LAYER "…"',
               "why": 'run `al improve LAYER "…"`: it works in a copy of the repository that holds the layer, and '
                      "a person always approves"},
     "ships": "a pull request a person merges", "setting": None},
    {"name": "decider", "kind": "decider", "script": "",
     "purpose": "stands in for a person when a run stops: resumes it, resumes it with an instruction, abandons "
                "it or asks a person",
     "input": {"kind": "run", "what": "a stopped run"},
     "start": {"by_hand": False, "how": "", "command": "al answer LAYER RUN …",
               "why": "it runs when a run stops; a person answers with `al answer LAYER RUN …`"},
     "ships": "never: a resume goes through every check, the review and the gate again",
     "setting": {"where": DECIDER_SETTINGS, "key": "mode", "turn_on": "al decider LAYER --set suggest|auto"}},
]


# ---------------------------------------------------------------- reading the compiled layer

def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []


def _adws(ir: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [a for a in _dicts(ir.get("adws")) if isinstance(a.get("name"), str) and a["name"]]


def _steps(adw: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _dicts(adw.get("steps"))


def _review(ir: Mapping[str, Any], review_id: Any) -> dict[str, Any]:
    return next((r for r in _dicts(ir.get("reviews")) if r.get("id") == review_id), {})


def ships_unattended(ir: Mapping[str, Any], adw: Mapping[str, Any]) -> str:
    """The rule of launch.ships_unattended: "merge" / "deploy" when the workflow ships without a
    person (an auto review and a ship step), else ""."""
    if not any(s.get("kind") == "ship" for s in _steps(adw)):
        return ""
    review = _review(ir, adw.get("review"))
    return str(review["ship"]) if review.get("kind") == "auto" and review.get("ship") in ("merge", "deploy") else ""


def _read(layer_dir: Path | None, relative: str) -> tuple[Any, str]:
    """(the parsed JSON file or None when there is none, why it cannot be read)."""
    if layer_dir is None:
        return None, ""
    path = Path(layer_dir) / relative
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"{relative}: cannot read it ({exc.strerror})"
    try:
        return json.loads(text), ""
    except ValueError:
        return None, f"{relative}: not JSON"


def integrate_setting(layer_dir: Path | None) -> tuple[dict[str, Any], str]:
    """({enabled, approval} of templates/integrate.json, why it cannot be used). The rules of
    integrate.settings: a missing file is on; a broken one is a problem that is reported."""
    out = {"enabled": True, "approval": "none"}
    data, problem = _read(layer_dir, INTEGRATE_SETTINGS)
    if problem:
        return out, problem
    if data is None:
        return out, ""
    if not isinstance(data, dict):
        return out, f"{INTEGRATE_SETTINGS}: expected a JSON object"
    unknown = sorted(set(data) - set(INTEGRATE_KEYS))
    if unknown:
        return out, f"{INTEGRATE_SETTINGS}: unknown key {', '.join(unknown)}"
    enabled, approval = data.get("enabled", True), data.get("approval", "none")
    if not isinstance(enabled, bool) or approval not in INTEGRATE_APPROVALS:
        return out, f"{INTEGRATE_SETTINGS}: enabled is true or false, approval is none or person"
    return {"enabled": enabled, "approval": str(approval)}, ""


def decider_mode(layer_dir: Path | None) -> tuple[str, str]:
    """(the layer's local decider mode, off when there is no file; why the file cannot be used)."""
    data, problem = _read(layer_dir, DECIDER_SETTINGS)
    if problem:
        return "off", problem
    if data is None:
        return "off", ""
    mode = data.get("mode") if isinstance(data, dict) else None
    if mode not in DECIDER_MODES:
        return "off", f"{DECIDER_SETTINGS}: mode is one of {', '.join(DECIDER_MODES)}"
    return str(mode), ""


def adw_script(layer_dir: Path, name: str) -> Path:
    """The lookup rule of inputs.adw_script."""
    adws = Path(layer_dir) / "adws"
    for candidate in (adws / f"adw_{name}.py", adws / f"adw_{name.replace('-', '_')}.py"):
        if candidate.exists():
            return candidate
    return adws / f"adw_{name}.py"


def promotion_name(promo: Mapping[str, Any]) -> str:
    """The rule of promotion.adw_name."""
    return f"{PROMOTE}-{promo.get('from')}-{promo.get('to')}"


def promotion_steps(promo: Mapping[str, Any]) -> list[str]:
    """The rule of promotion.promotion_steps."""
    skip = set()
    if "checks" not in (promo.get("requires") or []):
        skip.add("checks")
    if not promo.get("version"):
        skip.add("version")
    if not promo.get("tag"):
        skip.add("tag")
    if not promo.get("after"):
        skip.add("deploy")
    if not promo.get("backMerge"):
        skip.add("back-merge")
    return [step for step in PROMOTION_STEPS if step not in skip]


def integrate_serves(ir: Mapping[str, Any]) -> list[str]:
    """The declared workflows whose runs can end handed over because of a merge (the rule of
    tools/explain.py integrate_text): a ship step, an auto review, and it does not change the layer."""
    return [a["name"] for a in _adws(ir)
            if any(s.get("kind") == "ship" for s in _steps(a)) and str(a.get("changes") or "codebase") != "layer"
            and _review(ir, a.get("review")).get("kind") == "auto"]


# ---------------------------------------------------------------- entries

def _entry(**fields: Any) -> dict[str, Any]:
    entry = {"name": "", "kind": "", "source": "", "purpose": "", "steps": [], "definition": None,
             "input": {"kind": "none"}, "start": {"by_hand": False, "how": "", "command": "", "why": ""},
             "ships": "", "setting": None, "script": "", "available": True, "off": False, "adw": "", "used_by": []}
    entry.update(fields)
    return entry


def _exists(layer_dir: Path | None, script: str) -> bool:
    return True if layer_dir is None or not script else (Path(layer_dir) / script).is_file()


def _missing(script: str) -> str:
    return f"run `al gen` to add {script}"


def _declared(ir: Mapping[str, Any], layer_dir: Path | None) -> list[dict[str, Any]]:
    out = []
    for adw in _adws(ir):
        name = adw["name"]
        script = f"adws/adw_{name}.py"
        if layer_dir is not None:
            script = adw_script(Path(layer_dir), name).relative_to(Path(layer_dir)).as_posix()
        available = _exists(layer_dir, script)
        out.append(_entry(
            name=name, kind="workflow", source="declared", purpose=str(adw.get("description") or ""),
            steps=[str(s.get("name")) for s in _steps(adw)], definition=adw, input={"kind": "request"},
            start={"by_hand": True, "how": "form", "command": f"al run LAYER /{name} …",
                   "why": "" if available else _missing(script)},
            ships=ships_unattended(ir, adw), script=script, available=available, off=not available, adw=name))
    return out


def _integrate_row(row: Mapping[str, Any], ir: Mapping[str, Any], layer_dir: Path | None,
                   problems: list[str]) -> list[dict[str, Any]]:
    data, problem = integrate_setting(layer_dir)
    if problem:
        problems.append(problem)
    serves = integrate_serves(ir)
    available = _exists(layer_dir, row["script"])
    steps = ["prepare", "resolve", "checks", "review", *(["approve"] if data["approval"] == "person" else []), "ship"]
    kinds = {"prepare": "integrate", "resolve": "integrate", "approve": "integrate", "checks": "check",
             "review": "review", "ship": "ship"}
    definition = {"name": row["name"], "description": row["purpose"], "integration": {},
                  "steps": [{"kind": kinds[s], "name": s, **({"action": s} if kinds[s] == "integrate" else {})}
                            for s in steps]}
    on = data["enabled"] and not problem
    start = dict(row["start"])
    if not available:
        start["why"] = _missing(row["script"])
    elif problem:
        start["why"] = problem
    elif not data["enabled"]:
        start["why"] = f"switched off in {INTEGRATE_SETTINGS}"
    elif not serves:
        start.update(by_hand=False, why="never offered in this layer: no workflow merges by itself (a ship step "
                                        "with a `(review auto …)`), so no run ends handed over because of a merge")
    purpose = row["purpose"] + (f"; serves runs of {', '.join(serves)}" if serves else "")
    return [_entry(name=row["name"], kind=row["kind"], source="kit", purpose=purpose, steps=steps,
                   definition=definition, input=dict(row["input"]), start=start, ships=row["ships"],
                   setting={**row["setting"], "on": on}, script=row["script"], available=available,
                   off=not available or not on, adw=row["name"])]


def _promote_row(row: Mapping[str, Any], ir: Mapping[str, Any], layer_dir: Path | None,
                 problems: list[str]) -> list[dict[str, Any]]:
    del problems
    promotions = [p for p in _dicts(ir.get("promotions")) if p.get("from") and p.get("to")]
    if not promotions:
        return [_entry(name=row["name"], kind=row["kind"], source="kit", purpose=row["purpose"],
                       steps=list(PROMOTION_STEPS), input={"kind": "stages", "what": "two stages", "from": "", "to": ""},
                       start={**row["start"], "by_hand": False, "why": NO_STAGES}, ships=row["ships"],
                       setting={**row["setting"], "on": False}, script=row["script"], available=False, off=True)]
    available = _exists(layer_dir, row["script"])
    out = []
    for promo in promotions:
        name, steps = promotion_name(promo), promotion_steps(promo)
        from_, to = str(promo["from"]), str(promo["to"])
        definition = {"name": name, "description": f"promotion {from_} -> {to}",
                      "promotion": {"from": from_, "to": to},
                      "steps": [{"kind": "promote", "name": step, "action": step} for step in steps]}
        out.append(_entry(
            name=name, kind=row["kind"], source="kit", purpose=f"{row['purpose']} ({from_} → {to})", steps=steps,
            definition=definition, input={"kind": "stages", "what": "two stages", "from": from_, "to": to},
            start={**row["start"], "command": f"al promote LAYER {from_} {to}",
                   "why": "" if available else _missing(row["script"])},
            ships=str(promo.get("method") or row["ships"]), setting={**row["setting"], "on": True},
            script=row["script"], available=available, off=not available, adw=name))
    return out


def _improve_row(row: Mapping[str, Any], ir: Mapping[str, Any], layer_dir: Path | None,
                 problems: list[str]) -> list[dict[str, Any]]:
    del ir, layer_dir, problems
    return [_entry(name=row["name"], kind=row["kind"], source="kit", purpose=row["purpose"],
                   steps=list(IMPROVE_STEPS), input=dict(row["input"]), start=dict(row["start"]), ships=row["ships"])]


def _decider_row(row: Mapping[str, Any], ir: Mapping[str, Any], layer_dir: Path | None,
                 problems: list[str]) -> list[dict[str, Any]]:
    del ir
    mode, problem = decider_mode(layer_dir)
    if problem:
        problems.append(problem)
    on = mode != "off"
    start = dict(row["start"])
    if not on:
        start["why"] = (problem or f"off: turn it on with `{row['setting']['turn_on']}`") + f"; {start['why']}"
    return [_entry(name=row["name"], kind=row["kind"], source="kit", purpose=row["purpose"], input=dict(row["input"]),
                   start=start, ships=row["ships"], setting={**row["setting"], "on": on, "value": mode}, off=not on)]


_ROWS = {INTEGRATE: _integrate_row, PROMOTE: _promote_row, "improve": _improve_row, "decider": _decider_row}


def _kit(ir: Mapping[str, Any], layer_dir: Path | None, taken: set[str], problems: list[str]) -> list[dict[str, Any]]:
    out = []
    for row in KIT:
        expand = _ROWS.get(str(row.get("name")), _improve_row)
        # a declared workflow keeps a name the kit does not reserve (kit/self declares `improve` itself)
        out += [entry for entry in expand(row, ir, layer_dir, problems) if entry["name"] not in taken]
    return out


def command_definition(ir: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """The definition of a single-command run of `name` by the rule of launch.single_command_adw:
    one `run` step in the environment, agent and review option of the layer's first workflow."""
    adws = _adws(ir)
    if not adws:
        return None
    first = adws[0]
    declared = {str(o.get("id")) for o in _dicts(ir.get("promptInputs"))}
    return {"name": COMMAND_PREFIX + name, "description": f"single command /{name}", "agent": first.get("agent"),
            "review": first.get("review"), "environment": list(first.get("environment") or []),
            "input": [i for i in ["cli"] if i in declared], "classes": [], "trigger": [], "changes": "codebase",
            "steps": [{"kind": "run", "name": name, "command": name, "args": ["$prompt"], "modelSet": "inherit",
                       "agent": "", "retries": 0, "when": "always"}]}


def _commands(ir: Mapping[str, Any], layer_dir: Path | None) -> list[dict[str, Any]]:
    script = "adws/dispatch.py"
    available = _exists(layer_dir, script)
    out, seen = [], set()
    for key, kind in (("commands", "command"), ("hops", "hop")):
        for item in _dicts(ir.get(key)):
            name = item.get("name")
            if not isinstance(name, str) or not name or name in seen:
                continue
            seen.add(name)
            definition = command_definition(ir, name)
            why = "" if available else _missing(script)
            if definition is None:
                why = "the layer declares no workflow, so there is no environment to run a single command in"
            out.append(_entry(
                name=name, kind=kind, source="declared", purpose=str(item.get("description") or ""), steps=[name],
                definition=definition, input={"kind": "request"},
                start={"by_hand": definition is not None, "how": "form", "command": f"al run LAYER /{name} …",
                       "why": why},
                ships="", script=script, available=available, off=not available, adw=COMMAND_PREFIX + name))
    return out


def _suite(ir: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each codebase command a `script` step of a declared workflow runs."""
    codebase = ir.get("codebase") if isinstance(ir.get("codebase"), dict) else {}
    described = {str(c.get("name")): c for c in _dicts(codebase.get("commands"))}
    used: dict[str, list[dict[str, str]]] = {}
    for adw in _adws(ir):
        for step in _steps(adw):
            if step.get("kind") == "script" and step.get("cmd"):
                used.setdefault(str(step["cmd"]), []).append({"workflow": adw["name"], "step": str(step.get("name"))})
    out = []
    for name, where in used.items():
        command = described.get(name, {})
        first = where[0]
        why = f"runs as step {first['step']} of workflow {first['workflow']}" + (
            f" (and {len(where) - 1} more)" if len(where) > 1 else "")
        out.append(_entry(name=name, kind="suite-command", source="suite",
                          purpose=str(command.get("description") or command.get("run") or ""), steps=[name],
                          start={"by_hand": False, "how": "", "command": "", "why": why}, used_by=where))
    return out


def _scripts(ir: Mapping[str, Any], entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    roles = {"workflow": "workflow", "kit-workflow": "kit-workflow", "command": "launcher", "hop": "launcher"}
    scripts: dict[str, dict[str, Any]] = {"adws/dispatch.py": {"role": "launcher", "entries": []}}
    for entry in entries:
        role = roles.get(entry["kind"])
        if not role or not entry["script"] or (entry["source"] == "kit" and not entry["adw"]):
            continue   # a kit row without a script in this layer (no stages: no adws/promote.py)
        scripts.setdefault(entry["script"], {"role": role, "entries": []})["entries"].append(entry["name"])
    used = {str(t.get("kind")) for t in _dicts(ir.get("triggers"))}
    for kind, script in TRIGGER_SCRIPTS.items():
        if kind in used:
            scripts[f"adws/triggers/{script}"] = {"role": "trigger", "entries": []}
    if ir.get("voice"):
        for script in VOICE_SCRIPTS:
            scripts[f"adws/voice/{script}"] = {"role": "voice", "entries": []}
    dashboard = ir.get("dashboard")
    if isinstance(dashboard, dict) and str(dashboard.get("kind") or "none") != "none":
        scripts["adws/dashboard.py"] = {"role": "dashboard", "entries": []}
    return scripts


def build(ir: Mapping[str, Any], layer_dir: Path | None = None) -> dict[str, Any]:
    """{"version": 1, "entries", "scripts", "reserved", "problems"} for one compiled layer. Entries,
    in this order: the declared workflows, the kit rows, the commands and hops as single-command
    runs, the suite commands. With `layer_dir` the settings files are read and a script that is not
    there makes its entry `off`; without it every script counts as present."""
    ir = ir if isinstance(ir, Mapping) else {}
    layer_dir = Path(layer_dir) if layer_dir is not None else None
    problems: list[str] = []
    declared_ = _declared(ir, layer_dir)
    kit_ = _kit(ir, layer_dir, {e["name"] for e in declared_}, problems)
    entries = [*declared_, *kit_, *_commands(ir, layer_dir), *_suite(ir)]
    # the names a run of a kit workflow records: a declared workflow may not take one
    reserved = [name for row in KIT for name in _reserved(row, ir)]
    return {"version": VERSION, "entries": entries, "scripts": _scripts(ir, entries),
            "reserved": list(dict.fromkeys(reserved)), "problems": list(dict.fromkeys(problems))}


def _reserved(row: Mapping[str, Any], ir: Mapping[str, Any]) -> list[str]:
    if row.get("name") == INTEGRATE:
        return [INTEGRATE]
    if row.get("name") == PROMOTE:
        return [promotion_name(p) for p in _dicts(ir.get("promotions")) if p.get("from") and p.get("to")]
    return []


def fallback(ir: Mapping[str, Any], problem: str) -> dict[str, Any]:
    """A registry of the declared entries only, for a reader that could not load this module's
    newer copy: same shape, one problem line."""
    ir = ir if isinstance(ir, Mapping) else {}
    entries = [*_declared(ir, None), *_commands(ir, None)]
    return {"version": VERSION, "entries": entries, "scripts": _scripts(ir, entries), "reserved": [],
            "problems": [problem]}


# ---------------------------------------------------------------- what the readers ask

def entry(reg: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """The entry called `name`, or the one whose runs record `name` (command:NAME); a workflow wins
    over a kit row, a kit row over a command of the same name."""
    entries = list(reg.get("entries") or [])
    if isinstance(name, str) and name.startswith(COMMAND_PREFIX):
        return next((e for e in entries if e["adw"] == name), None)
    order = {"workflow": 0, "kit-workflow": 1, "decider": 1, "command": 2, "hop": 2, "suite-command": 3}
    found = sorted((e for e in entries if e["name"] == name), key=lambda e: order.get(e["kind"], 9))
    return found[0] if found else None


def declared(reg: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [e for e in reg.get("entries") or [] if e["kind"] == "workflow"]


def kit(reg: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [e for e in reg.get("entries") or [] if e["source"] == "kit"]


def commands(reg: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [e for e in reg.get("entries") or [] if e["kind"] in ("command", "hop")]


def suite(reg: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [e for e in reg.get("entries") or [] if e["kind"] == "suite-command"]


def start_answer(reg: Mapping[str, Any], name: str, where: str = "cli") -> str:
    """The sentence for a name that is not started through launch.start ("" for one that is, or an
    unknown name). `where` is "cli" (name the command) or "form" (the dashboard's start form)."""
    found = entry(reg, name)
    if found is None or found["source"] != "kit":
        return ""
    takes = {"run": found["input"].get("what") or "a run", "stages": "two stages",
             "request": "a request"}.get(found["input"].get("kind"), "")
    if found["kind"] == "decider":
        return f"{name} is the stand-in for a person and is not started by hand: {found['start']['why']}"
    if where == "form" and found["start"]["by_hand"]:
        how = "choose it in the form" if found["input"].get("kind") == "run" else "choose it in the form or on the layer's page"
        return f"{name} takes {takes}: {how}"
    if not found["start"]["by_hand"] and found["start"]["why"] and found["start"]["how"] != "cli":
        return f"{name} is a kit workflow that takes {takes}: {found['start']['why']}"
    return f"{name} is a kit workflow that takes {takes}: {found['start']['command']}"


def known_names(reg: Mapping[str, Any]) -> list[str]:
    """What a "known:" list names: `/NAME` per declared workflow and command, then the kit entries, each with `(kit)`."""
    workflows = [e["name"] for e in declared(reg)]
    names = [*workflows, *[e["name"] for e in commands(reg) if e["name"] not in workflows]]
    return [f"/{n}" for n in names] + [f"/{e['name']} (kit)" for e in kit(reg)]


def is_kit_run(reg: Mapping[str, Any], adw_name: str, definition: Any = None) -> bool:
    """True for a run of a kit workflow: its recorded definition says so, or its name is a kit entry's."""
    if isinstance(definition, dict) and (isinstance(definition.get("integration"), dict)
                                         or isinstance(definition.get("promotion"), dict)):
        return True
    found = entry(reg, adw_name)
    return bool(found and found["source"] == "kit")


def resume_argv(layer_dir: Path, adw_name: str, definition: Any, adw_id: str, *, decider: str = "",
                dry_run: bool = False) -> list[str] | None:
    """The command line (the script first, no interpreter) that continues run `adw_id` of `adw_name`,
    or None when no script of the layer does:

      a promotion      adws/promote.py run FROM TO --resume --adw-id ID
      an integration   adws/integrate.py run ORIGINAL --resume --adw-id ID
      command:NAME     adws/dispatch.py --command NAME --resume --adw-id ID
      a workflow       adws/adw_<name>.py --resume --adw-id ID   (the lookup of inputs.adw_script)

    `decider` (a mode) and `dry_run` add the flags the script takes: the runner's `--decider MODE`
    for a workflow and a single command (promote.py and integrate.py have no such flag), `--dry-run`
    for all four. None only for a kit run that recorded no definition (its stages or its original
    run are not known); a workflow's script is named as before, whether or not it exists."""
    layer_dir = Path(layer_dir)
    definition = definition if isinstance(definition, dict) else {}
    adws = layer_dir / "adws"
    dry = ["--dry-run"] if dry_run else []
    runner_flags = [*(["--decider", decider] if decider else []), *dry]
    promotion = definition.get("promotion")
    if isinstance(promotion, dict) and promotion.get("from") and promotion.get("to"):
        return [str(adws / "promote.py"), "run", str(promotion["from"]), str(promotion["to"]), "--resume",
                "--adw-id", adw_id, *dry]
    integration = definition.get("integration")
    if isinstance(integration, dict) and integration.get("run"):
        return [str(adws / "integrate.py"), "run", str(integration["run"]), "--resume", "--adw-id", adw_id, *dry]
    if adw_name == INTEGRATE and not (adws / f"adw_{INTEGRATE}.py").exists():
        return None   # an integration run that recorded no definition: integrate.py cannot continue it
    if adw_name.startswith(f"{PROMOTE}-") and not adw_script(layer_dir, adw_name).exists():
        return None   # a promotion run that recorded no definition: its stages are not known
    if adw_name.startswith(COMMAND_PREFIX):
        steps = _steps(definition)
        name = str(steps[0].get("command") or "") if steps else ""
        return [str(adws / "dispatch.py"), "--command", name or adw_name[len(COMMAND_PREFIX):], "--resume",
                "--adw-id", adw_id, *runner_flags]
    return [str(adw_script(layer_dir, adw_name)), "--resume", "--adw-id", adw_id, *runner_flags]


def resume_words(layer_dir: Path, adw_name: str, definition: Any, adw_id: str) -> str:
    """The same command as a person types it in the layer directory: `uv run adws/… --resume …`; ""
    when no script continues the run."""
    argv = resume_argv(layer_dir, adw_name, definition, adw_id)
    if argv is None:
        return ""
    try:
        argv[0] = Path(argv[0]).relative_to(Path(layer_dir)).as_posix()
    except ValueError:
        pass
    return "uv run " + " ".join(argv)
