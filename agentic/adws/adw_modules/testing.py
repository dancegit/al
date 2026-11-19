"""How deep a run's checks go, and an honest account of what they ran.

Three depths of a check: `full` (the cmd's `#:run`), `targeted` (its `#:targeted` form filled with
the tests selection.py selects for the change) and `smoke` (its `#:smoke` form). The layer says
which in `(testing DEPTH …)` (IR key `testing`, present only when declared); a layer without the
clause runs every check in full, exactly as before this feature.

The depth of one check, first match wins:

1. the step's own `depth` (a promotion's or a batch's check on a stage head);
2. `agents/<run>/testing-request.json` — a person asked for full before it ships (gate only);
3. the run's `--testing` (state.testing["requested"]);
4. the local setting `agents/settings/testing.json` (light / standard / heavy; never committed);
5. the layer's policy: `#:per-run` for check steps and the gate, `#:review-rounds` for re-checks;
6. full, when the layer declares no `(testing …)`.

A narrower depth then turns full when the cmd has no narrower form, when the change escalates
(core paths, many lines or files, a lockfile, the layer, a work class, a covered path no strategy
maps), when the cmd's miss rate is above `#:max-miss-percent`, or when more tests are selected
than `#:max-targeted`. An empty selection runs smoke, else full: never nothing. Every decision
says why (`CheckPlan.why`) and every result says what ran (`accounting`, `accounting_text`).

Files under the layer directory (never committed):
- agents/settings/testing.json      {"version": 1, "level": "light|standard|heavy"}
- agents/<run>/testing-request.json {"depth": "full", "by", "at"}
- agents/testing/suites.json        {CMD: {"seconds", "tests", "commit", "at"}} — the last passing full run
- agents/testing/misses.jsonl       one line per batch failure bisected to a run that passed below full
- agents/testing/learned.json       {CMD: [{"files", "tests", "batch"}]} — what misses taught selection
- agents/testing/batches.jsonl      written by batch.py; read here for miss rates

Standard library only; imports `affected`, `selection`, `skips`, `delivery` (stages), never
`review`, `ship` or `steps`.
"""
from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import statistics
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import affected, delivery, selection, skips
from .errors import LayerError

LEVELS = {"light": "smoke", "standard": "targeted", "heavy": "full"}
DEPTHS = ("smoke", "targeted", "full")
SETTING_FILE = Path("agents") / "settings" / "testing.json"
TESTING_DIR = Path("agents") / "testing"
REQUEST_FILE = "testing-request.json"
MISS_WINDOW = 50          # newest targeted runs per cmd a miss rate is measured over
TYPICAL_RUNS = 20         # newest runs typical minutes are measured over
LOCKFILES = ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements*.txt", "pyproject.toml", "package.json",
             "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
             "Gemfile.lock", "flake.lock")
UNDECLARED_NOTE = ("testing: full (this layer has no (testing …) clause, so every check runs its full form; "
                   "add (testing targeted …) and #:targeted forms to test what a change touches)")
SOURCE_REQUEST = "a person asked for full before it ships"
SOURCE_FLAG = "--testing"
SOURCE_SETTING = "local setting"
SOURCE_POLICY = "layer policy"
SOURCE_UNDECLARED = "the layer declares no (testing …)"
SOURCE_STEP = "the step"

DEFAULTS: dict[str, Any] = {
    "depth": "targeted", "perRun": "inherit", "reviewRounds": "inherit", "onStage": "full",
    "batchEvery": 0, "batchAfterHours": 0, "escalatePaths": [], "escalateDiffLines": 1500, "escalateFiles": 40,
    "escalateOn": ["lockfile", "layer", "unmapped"], "escalateClasses": [], "onBatchFailure": ["bisect", "open-fix"],
    "fixAdw": "", "maxMissPercent": 10,
}
# without (testing …) every depth is full; the escalation defaults still guard a narrower depth a
# person asks for (--testing, the local setting) on such a layer
UNDECLARED: dict[str, Any] = {
    **DEFAULTS, "declared": False, "depth": "full", "perRun": "full", "reviewRounds": "full", "onStage": "full",
}


# ---------------------------------------------------------------- small helpers

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _layer_dir(layer: Any) -> Path:
    return Path(getattr(layer, "layer_dir", layer))


def _cmd_name(cmd: Any) -> str:
    return str(cmd.get("name", "")) if isinstance(cmd, dict) else str(cmd)


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False,
                                         prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, sort_keys=True) + "\n")


def _run_testing(state: Any) -> dict[str, Any]:
    """state.testing (the run's depth decision); created on the state when it has none yet."""
    data = getattr(state, "testing", None)
    if isinstance(data, dict):
        return data
    data = {}
    try:
        state.testing = data
    except (AttributeError, TypeError):
        pass
    return data


# ---------------------------------------------------------------- depths, levels, policy

def normalise(value: Any) -> str:
    """A depth from a level (light / standard / heavy) or a depth (smoke / targeted / full)."""
    text = str(value if value is not None else "").strip().lower()
    if text in LEVELS:
        return LEVELS[text]
    if text in DEPTHS:
        return text
    raise ValueError(f"unknown testing level {value!r}; levels are light, standard, heavy "
                     "(or the depths smoke, targeted, full)")


def level_of(depth: str) -> str:
    """The level name of a depth (or level)."""
    depth = normalise(depth)
    return next(level for level, value in LEVELS.items() if value == depth)


def policy(layer: Any) -> dict[str, Any]:
    """The layer's `(testing …)` with defaults filled and `inherit` resolved; without the clause,
    full everywhere and `"declared": False`."""
    raw = (getattr(layer, "ir", None) or {}).get("testing")
    if not isinstance(raw, dict):
        return {key: (list(value) if isinstance(value, list) else value) for key, value in UNDECLARED.items()}
    result = {**{key: (list(value) if isinstance(value, list) else value) for key, value in DEFAULTS.items()},
              **raw, "declared": True}
    depth = str(result.get("depth") or "targeted")
    result["depth"] = depth if depth in DEPTHS else "targeted"
    for key in ("perRun", "reviewRounds"):
        value = str(result.get(key) or "inherit")
        result[key] = result["depth"] if value == "inherit" or value not in DEPTHS else value
    if str(result.get("onStage") or "") not in DEPTHS:
        result["onStage"] = "full"
    for key in ("batchEvery", "batchAfterHours", "escalateDiffLines", "escalateFiles", "maxMissPercent"):
        try:
            result[key] = int(result.get(key) or 0)
        except (TypeError, ValueError):
            result[key] = int(DEFAULTS[key])
    return result


def on_stage(layer: Any) -> str:
    """The depth of a batch and of a promotion's checks on a stage head (`#:on-stage`)."""
    return str(policy(layer)["onStage"])


# ---------------------------------------------------------------- the local setting

def render_setting(level: str) -> str:
    """The one format of agents/settings/testing.json (dashboard.py writes it byte for byte alike)."""
    return json.dumps({"version": 1, "level": level}, indent=2, sort_keys=True) + "\n"


def read_setting(layer_dir: Path | str) -> str | None:
    """The layer's local testing level (light / standard / heavy); None when there is none.
    A file that is not the setting raises LayerError with how to fix it."""
    path = Path(layer_dir) / SETTING_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LayerError(f"{path}: cannot read it ({exc.strerror}); fix or remove it (al testing LAYER --reset)") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LayerError(f"{path}: not JSON ({exc.msg}); fix or remove it (al testing LAYER --reset)") from exc
    level = data.get("level") if isinstance(data, dict) else None
    if level not in LEVELS:
        raise LayerError(f'{path}: expected {{"version": 1, "level": "light|standard|heavy"}}; '
                         "fix or remove it (al testing LAYER --reset)")
    return str(level)


def write_setting(layer_dir: Path | str, level: str | None) -> Path:
    """Write the local setting atomically (a depth name is stored as its level); None removes it."""
    path = Path(layer_dir) / SETTING_FILE
    if level is None:
        path.unlink(missing_ok=True)
        return path
    name = level_of(level)  # ValueError for an unknown level
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False,
                                         prefix=".testing.", suffix=".tmp")
    try:
        with handle:
            handle.write(render_setting(name))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def _review(layer: Any, adw: dict[str, Any]) -> dict[str, Any]:
    try:
        return layer.option("reviews", adw["review"]) if adw.get("review") else {}
    except (LayerError, KeyError, AttributeError):
        return {}


def unbatched_trunk_adws(layer: Any, pol: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    """(adw, branch) of each zero-touch ADW (review auto, ship merge or deploy) that can land on
    the final stage (no stages: the default branch) while no batch rhythm is declared."""
    pol = pol if pol is not None else policy(layer)
    if int(pol.get("batchEvery") or 0) > 0 or int(pol.get("batchAfterHours") or 0) > 0:
        return []
    result: list[tuple[str, str]] = []
    try:
        all_stages = delivery.stages(layer)
    except Exception:  # noqa: BLE001 — a stage list that cannot be read: treat as none
        all_stages = []
    final = delivery.final_stage(all_stages) if all_stages else None
    for adw in (getattr(layer, "ir", None) or {}).get("adws", []):
        review = _review(layer, adw)
        if review.get("kind") != "auto" or review.get("ship") not in ("merge", "deploy"):
            continue
        if not all_stages:
            result.append((str(adw.get("name")), str(layer.codebase_default_branch)))
            continue
        classes = list(adw.get("classes") or review.get("allowedClasses") or delivery.CLASSES)
        for klass in classes:
            try:
                stage = delivery.route(layer, adw, str(klass))
            except Exception:  # noqa: BLE001
                continue
            if stage is not None and final is not None and stage.name == final.name:
                result.append((str(adw.get("name")), str(final.branch)))
                break
    return result


def zero_touch_message(adw: str, branch: str, depth: str) -> str:
    """The compiler's wording of the zero-touch rule."""
    return (f"(adw {adw}) ships to {branch} without a person at testing depth {depth}, but no batch "
            "verification is declared: add #:batch ((every N)) or ((after-hours H)) to (testing …), or #:per-run full")


def setting_allowed(layer: Any, level: str) -> tuple[bool, str]:
    """Whether the local setting may be `level`: a depth below full is refused while a zero-touch
    ADW lands on the final stage and the layer declares no batch rhythm (the compiler's rule)."""
    try:
        depth = normalise(level)
    except ValueError as exc:
        return False, str(exc)
    if depth == "full":
        return True, ""
    offenders = unbatched_trunk_adws(layer)
    if offenders:
        adw, branch = offenders[0]
        return False, zero_touch_message(adw, branch, depth)
    return True, ""


# ---------------------------------------------------------------- "full before it ships"

def request_full(layer_dir: Path | str, adw_id: str, by: str) -> dict[str, Any]:
    """Ask that run `adw_id` ships only after its gate ran the full suites."""
    data = {"depth": "full", "by": str(by), "at": _now()}
    _write_json(Path(layer_dir) / "agents" / str(adw_id) / REQUEST_FILE, data)
    return data


def requested_full(layer_dir: Path | str, adw_id: str) -> dict[str, Any] | None:
    data = _read_json(Path(layer_dir) / "agents" / str(adw_id) / REQUEST_FILE, None)
    return data if isinstance(data, dict) and data.get("depth") == "full" else None


# ---------------------------------------------------------------- the run's depth

def run_depth(layer: Any, state: Any, phase: str) -> tuple[str, str]:
    """(depth, source) of a run's checks in `phase` (check, gate, recheck); see the module docstring."""
    layer_dir = _layer_dir(layer)
    if phase == "gate" and requested_full(layer_dir, getattr(state, "adw_id", "")):
        return "full", SOURCE_REQUEST
    requested = _run_testing(state).get("requested")
    if requested:
        try:
            return normalise(requested), SOURCE_FLAG
        except ValueError:
            pass
    try:
        level = read_setting(layer_dir)
    except LayerError as exc:
        print(f"[adw {getattr(state, 'adw_id', '?')}] testing: the local setting is ignored: {exc}")
        level = None
    if level:
        return LEVELS[level], SOURCE_SETTING
    pol = policy(layer)
    if not pol["declared"]:
        return "full", SOURCE_UNDECLARED
    return str(pol["reviewRounds"] if phase == "recheck" else pol["perRun"]), SOURCE_POLICY


# ---------------------------------------------------------------- escalation

def _lines_changed(numstat: Any) -> int:
    """Total changed lines from a numstat: an int, {path: lines or (added, deleted)}, or a list
    of (added, deleted, path) rows / `git diff --numstat` text."""
    def number(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0  # binary files show "-"

    if numstat is None:
        return 0
    if isinstance(numstat, int):
        return numstat
    if isinstance(numstat, str):
        rows = [line.split("\t") for line in numstat.splitlines() if line.strip()]
        return sum(number(row[0]) + number(row[1]) for row in rows if len(row) >= 2)
    if isinstance(numstat, dict):
        values = numstat.values()
    else:
        values = numstat
    total = 0
    for value in values:
        if isinstance(value, (list, tuple)):
            total += number(value[0]) + (number(value[1]) if len(value) > 1 else 0)
        else:
            total += number(value)
    return total


def _is_lockfile(path: str) -> bool:
    name = posixpath.basename(path)
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in LOCKFILES)


def escalation(layer: Any, state: Any, env_stack: Any, changed: list[str] | None, numstat: Any,
               tree: str = "") -> list[str]:
    """Why this change runs at full depth, in order (core path, lines, files, lockfile, layer,
    class); [] when it need not. With `tree`, computed once per tree and cached in
    state.testing["escalation"][tree]. `unmapped` is decided per cmd in plan_check."""
    del env_stack
    cache = _run_testing(state).setdefault("escalation", {}) if tree else None
    if cache is not None and tree in cache:
        return list(cache[tree])
    pol = policy(layer)
    reasons: list[str] = []
    paths = [str(path) for path in changed or []]
    for pattern in pol.get("escalatePaths") or []:
        hit = next((path for path in paths if affected.matches(path, [str(pattern)])), None)
        if hit:
            reasons.append(f"core path {hit} ({pattern})")
            break
    lines, limit = _lines_changed(numstat), int(pol.get("escalateDiffLines") or 0)
    if limit > 0 and lines > limit:
        reasons.append(f"{lines} changed lines > {limit}")
    limit = int(pol.get("escalateFiles") or 0)
    if limit > 0 and len(paths) > limit:
        reasons.append(f"{len(paths)} changed files > {limit}")
    escalate_on = list(pol.get("escalateOn") or [])
    if "lockfile" in escalate_on:
        hit = next((path for path in paths if _is_lockfile(path)), None)
        if hit:
            reasons.append(f"lockfile {hit}")
    if "layer" in escalate_on:
        in_repo = str(getattr(layer, "in_repo_dir", "") or "")
        hit = next((path for path in paths if in_repo and (path == in_repo or path.startswith(in_repo + "/"))), None)
        if hit:
            reasons.append(f"the agentic layer changed ({hit})")
        else:
            try:
                if layer.changes_layer(state.adw):
                    reasons.append("the agentic layer changed (a layer-change workflow)")
            except (LayerError, AttributeError, KeyError):
                pass
    klass = str(getattr(state, "klass", "") or "")
    if klass and klass in [str(item) for item in pol.get("escalateClasses") or []]:
        reasons.append(f"class {klass}")
    if cache is not None:
        cache[tree] = list(reasons)
    return reasons


# ---------------------------------------------------------------- the plan of one check

@dataclass
class CheckPlan:
    depth: str                                   # what runs: smoke | targeted | full
    asked: str                                   # the depth before escalation and fallbacks
    command: dict[str, Any]                      # the cmd with `run` replaced by the form that runs
    why: str = ""                                # escalation / fallback / empty-selection text
    selection: selection.Selection | None = None
    units: str = "tests"                         # tests | test files | packages | files
    source: str = ""                             # where `asked` came from


def _root(layer: Any, state: Any, env_stack: Any) -> Path:
    worktree = str(getattr(state, "worktree", "") or "")
    if worktree and Path(worktree).is_dir():
        return Path(worktree)
    sandbox = getattr(env_stack, "sandbox", None)
    if sandbox is not None and getattr(sandbox, "worktree", None):
        return Path(sandbox.worktree)
    return Path(layer.app_root)


def _numstat(state: Any, env_stack: Any, reference: str, tree: str) -> str:
    try:
        result = delivery.tree_git(state, env_stack, ["diff", "--numstat", "--no-renames", reference, tree, "--"],
                                   step="checks")
    except Exception:  # noqa: BLE001 — no line count: the line limit does not escalate
        return ""
    return result.stdout if result.returncode == 0 else ""


def _units(template: str, chosen: selection.Selection) -> tuple[str, int]:
    if "{tests}" in template:
        return ("tests" if any("::" in test for test in chosen.tests) else "test files"), len(chosen.tests)
    if "{packages}" in template:
        return "packages", len(chosen.packages)
    return "files", len(chosen.files)


def _pct(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def plan_check(layer: Any, state: Any, env_stack: Any, step: dict[str, Any], phase: str, reference: str,
               tree: str) -> CheckPlan:
    """What one check runs now and why. `reference` is what the change is measured against (the
    run's base; in a review round the cmd's last passing tree), `tree` the tree it runs on."""
    cmd = dict(layer.cmd(step["cmd"]))
    name = _cmd_name(cmd) or str(step["cmd"])
    if step.get("depth"):
        asked, source = normalise(step["depth"]), SOURCE_STEP
    else:
        asked, source = run_depth(layer, state, phase)

    def full(why: str = "") -> CheckPlan:
        return CheckPlan("full", asked, cmd, why, None, "tests", source)

    targeted_form, smoke_form = str(cmd.get("targeted") or ""), str(cmd.get("smoke") or "")
    if asked == "full":
        return full()
    if not targeted_form and not smoke_form:
        return full("no narrower form")
    pol = policy(layer)
    changed = affected.changed(layer, state, env_stack, reference, tree) if reference and tree else None
    if not step.get("depth") and phase in ("check", "gate"):
        record = _run_testing(state)
        record["depth"], record["source"] = asked, source
    if changed is not None:
        reasons = escalation(layer, state, env_stack, changed, _numstat(state, env_stack, reference, tree), tree=tree)
        if reasons:
            _run_testing(state).setdefault("escalated", {})[name] = reasons
            return full("escalated: " + "; ".join(reasons))
    rates = miss_rates(layer).get(name) or {}
    if rates.get("fallback"):
        return full(f"miss rate {_pct(float(rates['rate']))}% over the last {rates['targeted']} targeted runs "
                    f"is above {rates['threshold']}%: running full")

    def smoke(why: str = "") -> CheckPlan:
        if not smoke_form:
            return full(why)
        return CheckPlan("smoke", asked, {**cmd, "run": smoke_form}, why, None, "tests", source)

    if asked == "smoke":
        return smoke()
    if not targeted_form:
        return smoke()
    if changed is None:
        return full("the changed paths could not be computed")
    chosen = selection.select(layer, cmd, changed, root=_root(layer, state, env_stack), base=reference,
                              env_stack=env_stack, state=state)
    if "unmapped" in (pol.get("escalateOn") or []) and chosen.unmapped:
        return full(f"escalated: unmapped path {chosen.unmapped[0]}: no selection strategy maps it to a test")
    units, count = _units(targeted_form, chosen)
    limit = int(cmd.get("maxTargeted") or 200)
    if count > limit:
        return full(f"{count} selected > max-targeted {limit}")
    if count == 0:
        return smoke("no test selected for a covered change")
    return CheckPlan("targeted", asked, {**cmd, "run": selection.fill(targeted_form, chosen)}, "", chosen, units,
                     source)


# ---------------------------------------------------------------- accounting and suite timings

def _suites_path(layer: Any) -> Path:
    return _layer_dir(layer) / TESTING_DIR / "suites.json"


def suites(layer: Any) -> dict[str, Any]:
    data = _read_json(_suites_path(layer), {})
    return data if isinstance(data, dict) else {}


def _ran(log_text: str) -> int | None:
    report = skips.from_log(log_text or "")
    if report is None:
        return None
    total = report.passed + report.failed + report.skipped
    return total if total or not report.sources else 0


def note_full_run(layer: Any, cmd: Any, seconds: float, log_text: str, commit: str) -> None:
    """Remember a passing full run of `cmd`: how long it took and how many tests it ran."""
    data = suites(layer)
    previous = data.get(_cmd_name(cmd)) or {}
    tests = _ran(log_text)
    data[_cmd_name(cmd)] = {"seconds": round(float(seconds), 1) if float(seconds) % 1 else int(seconds),
                            "tests": tests if tests else previous.get("tests"),
                            "commit": str(commit or ""), "at": _now()}
    _write_json(_suites_path(layer), data)


def accounting(plan: CheckPlan, seconds: float, log_text: str, cmd_name: str, layer: Any) -> dict[str, Any]:
    """What one check ran, for the record: {depth, asked, why, selected, units, strategies, ran,
    total, seconds, full_seconds}; `ran` from the log when a parser reads it, else `selected`."""
    chosen = plan.selection
    selected = None
    if chosen is not None:
        selected = _units(str(plan.command.get("targeted") or "{tests}"), chosen)[1] if plan.command else chosen.count
    ran = _ran(log_text)
    suite = suites(layer).get(cmd_name) or {}
    return {"depth": plan.depth, "asked": plan.asked, "why": plan.why, "selected": selected,
            "units": plan.units, "strategies": dict(chosen.by_strategy) if chosen is not None else {},
            "ran": ran if ran is not None else selected, "ran_from": "log" if ran is not None else "selection",
            "total": suite.get("tests"), "seconds": round(float(seconds or 0), 1),
            "full_seconds": suite.get("seconds")}


def _duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def accounting_text(acc: dict[str, Any] | None) -> str:
    """One check's depth and what it ran, in words; never says "passed" (that is the outcome's)."""
    acc = acc or {}
    depth, why = str(acc.get("depth") or "full"), str(acc.get("why") or "")
    if depth == "full":
        return f"full ({why})" if why else "full"
    ran, total, units = acc.get("ran"), acc.get("total"), str(acc.get("units") or "tests")
    from_log = acc.get("ran_from", "log") == "log"
    strategies = ", ".join(f"{name} {count}" for name, count in (acc.get("strategies") or {}).items())
    if depth == "smoke":
        head = f"smoke ({why})" if why else "smoke"
        body = f"ran {ran} tests" if ran is not None and from_log else "ran the smoke form"
        text = f"{head}: {body}"
    else:
        if from_log and total:
            body, inner = f"ran {ran} of {total} tests", strategies
        elif from_log:
            body = f"ran {ran} tests"
            inner = "; ".join(item for item in ("full suite size not known yet", strategies) if item)
        else:
            body = f"ran the {ran} selected {units}"
            inner = "; ".join(item for item in (strategies, f"the full suite has {total} tests" if total
                                                  else "full suite size not known yet") if item)
        text = f"targeted: {body}" + (f" ({inner})" if inner else "")
    if acc.get("seconds") is not None:
        text += f": {_duration(float(acc['seconds']))}"
        if acc.get("full_seconds"):
            text += f" instead of about {_duration(float(acc['full_seconds']))}"
    return text


# ---------------------------------------------------------------- misses and what they taught

def _misses_path(layer: Any) -> Path:
    return _layer_dir(layer) / TESTING_DIR / "misses.jsonl"


def _learned_path(layer: Any) -> Path:
    return _layer_dir(layer) / TESTING_DIR / "learned.json"


def record_miss(layer: Any, *, batch: str, run: str, cmd: str, strategies: list[str] | dict[str, Any],
                tests: list[str], files: list[str]) -> dict[str, Any]:
    """A batch failure bisected to `run`, whose check of `cmd` passed below full depth."""
    item = {"batch": str(batch), "run": str(run), "cmd": str(cmd), "strategies": [str(name) for name in strategies],
            "tests": [str(test) for test in tests], "files": [str(path) for path in files], "at": _now()}
    _append_jsonl(_misses_path(layer), item)
    return item


def misses(layer: Any) -> list[dict[str, Any]]:
    return _read_jsonl(_misses_path(layer))


def learn(layer: Any, cmd: str, files: list[str], tests: list[str], batch: str) -> None:
    """Teach selection that a change of `files` concerns `tests` (each lesson once)."""
    data = _read_json(_learned_path(layer), {})
    data = data if isinstance(data, dict) else {}
    entries = data.setdefault(str(cmd), [])
    lesson = {"files": sorted({str(path) for path in files}), "tests": list(dict.fromkeys(str(t) for t in tests)),
              "batch": str(batch)}
    if any(entry.get("files") == lesson["files"] and entry.get("tests") == lesson["tests"] for entry in entries):
        return
    entries.append(lesson)
    _write_json(_learned_path(layer), data)


def learned(layer: Any, cmd: str) -> list[dict[str, Any]]:
    data = _read_json(_learned_path(layer), {})
    entries = data.get(str(cmd)) if isinstance(data, dict) else None
    return [entry for entry in entries or [] if isinstance(entry, dict)]


def _run_state(layer: Any, run: str) -> dict[str, Any]:
    data = _read_json(_layer_dir(layer) / "agents" / str(run) / "state.json", {})
    return data if isinstance(data, dict) else {}


def _below_full_pass(entry: dict[str, Any]) -> bool:
    depth = str(entry.get("depth") or (entry.get("testing") or {}).get("depth") or "full")
    return entry.get("outcome") == affected.PASSED and depth != "full"


def miss_rates(layer: Any) -> dict[str, dict[str, Any]]:
    """Per cmd, over the newest MISS_WINDOW runs delivered in batches that ended (green or red)
    whose check of the cmd passed below full depth: {targeted, misses, rate (percent, None
    without runs), threshold, fallback, by_strategy: {strategy: {targeted, misses, rate}}}."""
    batches = sorted(_read_jsonl(_layer_dir(layer) / TESTING_DIR / "batches.jsonl"),
                     key=lambda item: str(item.get("at") or ""), reverse=True)
    threshold = int(policy(layer).get("maxMissPercent") or 0)
    runs: list[str] = []
    for batch in batches:
        if batch.get("status") not in ("green", "red"):
            continue
        for run in reversed([str(item) for item in batch.get("delivered") or []]):
            if run not in runs:
                runs.append(run)
    per_cmd: dict[str, list[tuple[str, list[str]]]] = {}
    for run in runs:
        seen: set[str] = set()
        for entry in reversed(_run_state(layer, run).get("check_results") or []):
            cmd = str(entry.get("check") or "")
            if not cmd or cmd in seen or not _below_full_pass(entry):
                continue
            seen.add(cmd)
            if len(per_cmd.setdefault(cmd, [])) < MISS_WINDOW:
                per_cmd[cmd].append((run, list(((entry.get("testing") or {}).get("strategies") or {}).keys())))
    all_misses = misses(layer)
    result: dict[str, dict[str, Any]] = {}
    for cmd, counted in per_cmd.items():
        run_ids = {run for run, _ in counted}
        mine = [miss for miss in all_misses if miss.get("cmd") == cmd and miss.get("run") in run_ids]
        missed_runs = {str(miss.get("run")) for miss in mine}
        rate = 100.0 * len(missed_runs) / len(counted) if counted else None
        by_strategy: dict[str, dict[str, Any]] = {}
        for strategy in dict.fromkeys(name for _, names in counted for name in names):
            targeted = sum(1 for _, names in counted if strategy in names)
            missed = len({str(miss.get("run")) for miss in mine if strategy in (miss.get("strategies") or [])})
            by_strategy[strategy] = {"targeted": targeted, "misses": missed,
                                     "rate": round(100.0 * missed / targeted, 1) if targeted else None}
        result[cmd] = {"targeted": len(counted), "misses": len(missed_runs),
                       "rate": round(rate, 1) if rate is not None else None, "threshold": threshold,
                       "fallback": bool(threshold > 0 and rate is not None and rate > threshold),
                       "by_strategy": by_strategy}
    return result


def typical_minutes(layer: Any) -> dict[str, float | None]:
    """Typical check minutes of a run per level, from the newest runs' accounting (the median of
    each run's summed check seconds at that depth); heavy from the suite timings when no full
    run is known. None without data."""
    agents = _layer_dir(layer) / "agents"
    states: list[dict[str, Any]] = []
    try:
        candidates = [path / "state.json" for path in agents.iterdir() if path.is_dir()]
    except OSError:
        candidates = []
    for path in candidates:
        data = _read_json(path, None)
        if isinstance(data, dict) and any(isinstance(entry.get("testing"), dict)
                                          for entry in data.get("check_results") or []):
            states.append(data)
    states.sort(key=lambda data: str(data.get("started") or ""), reverse=True)
    per_depth: dict[str, list[float]] = {depth: [] for depth in DEPTHS}
    for data in states[:TYPICAL_RUNS]:
        sums: dict[str, float] = {}
        for entry in data.get("check_results") or []:
            acc = entry.get("testing")
            if isinstance(acc, dict) and acc.get("seconds") is not None:
                depth = str(acc.get("depth") or "full")
                sums[depth] = sums.get(depth, 0.0) + float(acc["seconds"])
        for depth, total in sums.items():
            if depth in per_depth:
                per_depth[depth].append(total)
    if not per_depth["full"]:
        timings = [float(item.get("seconds") or 0) for item in suites(layer).values() if isinstance(item, dict)]
        if any(timings):
            per_depth["full"].append(sum(timings))
    return {level: (round(statistics.median(per_depth[depth]) / 60, 1) if per_depth[depth] else None)
            for level, depth in LEVELS.items()}


# ---------------------------------------------------------------- what al explain / al verify say

def _check_cmds(layer: Any) -> list[dict[str, Any]]:
    """The cmds that run as checks: check steps, review required checks, stage checks, each once."""
    ir = getattr(layer, "ir", None) or {}
    names: list[str] = []
    for adw in ir.get("adws", []):
        names += [str(step["cmd"]) for step in adw.get("steps", []) if step.get("kind") == "check" and step.get("cmd")]
    for option in ir.get("reviews", []):
        names += [str(name) for name in option.get("requireChecks") or []]
    for stage in ir.get("stages") or []:
        names += [str(name) for name in stage.get("checks") or []]
    result = []
    for name in dict.fromkeys(names):
        try:
            result.append(layer.cmd(name))
        except (LayerError, KeyError, AttributeError):
            continue
    return result


def _setting_line(layer: Any) -> str:
    try:
        level = read_setting(_layer_dir(layer))
    except LayerError as exc:
        return f"testing: the local setting is ignored: {exc}"
    if not level:
        return ""
    return (f"testing: local setting {level} ({LEVELS[level]}) in {SETTING_FILE.as_posix()} "
            "overrides the layer's depth on this machine")


def _rhythm(pol: dict[str, Any]) -> str:
    parts = []
    if pol.get("batchEvery"):
        parts.append(f"every {pol['batchEvery']} landed changes")
    if pol.get("batchAfterHours"):
        parts.append(f"when a change landed and the last batch is older than {pol['batchAfterHours']} hours")
    if not parts:
        return ("testing: no batch rhythm is declared: the full suites run per batch only when a cycle closes, "
                "before a promotion, or when started (al batch)")
    return f"testing: a batch runs the full suites ({pol['onStage']}) " + " or ".join(parts)


def _escalation_line(pol: dict[str, Any]) -> str:
    parts = []
    if pol.get("escalatePaths"):
        parts.append("core paths " + ", ".join(str(item) for item in pol["escalatePaths"]))
    if pol.get("escalateDiffLines"):
        parts.append(f"more than {pol['escalateDiffLines']} changed lines")
    if pol.get("escalateFiles"):
        parts.append(f"more than {pol['escalateFiles']} changed files")
    parts += [{"lockfile": "a lockfile or dependency manifest", "layer": "the agentic layer",
               "unmapped": "a covered path no strategy maps to a test"}.get(str(item), str(item))
              for item in pol.get("escalateOn") or []]
    if pol.get("escalateClasses"):
        parts.append("class " + ", ".join(str(item) for item in pol["escalateClasses"]))
    return ("testing: a run goes full on " + "; ".join(parts)) if parts else "testing: no escalation is declared"


def _cmd_lines(layer: Any) -> list[str]:
    lines = []
    for cmd in _check_cmds(layer):
        name = _cmd_name(cmd)
        if not cmd.get("targeted") and not cmd.get("smoke"):
            lines.append(f"testing: cmd {name} has no #:targeted or #:smoke form and always runs its full form")
        if "coverage" in (cmd.get("select") or []) and _coverage_missing(layer, name):
            lines.append(f"testing: cmd {name} selects by coverage, but no coverage map is recorded yet "
                         "(a passing full run with #:coverage-run records it)")
    return lines


def _coverage_missing(layer: Any, name: str) -> bool:
    from . import coverage_map

    return coverage_map.load(layer, name) is None


def verify_lines(layer: Any) -> list[str]:
    """`testing:` lines for `al verify` / `al sandbox doctor`."""
    pol = policy(layer)
    setting = _setting_line(layer)
    if not pol["declared"]:
        return [UNDECLARED_NOTE] + ([setting] if setting else [])
    lines = [f"testing: {pol['perRun']} per run, {pol['reviewRounds']} in review rounds, {pol['onStage']} per batch "
             f"(layer policy, depth {pol['depth']})"]
    if setting:
        lines.append(setting)
    lines.append(_rhythm(pol))
    lines += _cmd_lines(layer)
    return lines


def explain_lines(layer: Any) -> list[str]:
    """`testing:` lines for `al explain`: the verify lines plus escalation and batch failure handling."""
    pol = policy(layer)
    lines = verify_lines(layer)
    if not pol["declared"]:
        return lines
    lines.insert(len(lines) - len(_cmd_lines(layer)), _escalation_line(pol))
    failure = {"bisect": "bisect to the run that broke it", "open-fix": "open a fix run",
               "revert": "prepare a revert branch"}
    handling = [failure.get(str(item), str(item)) for item in pol.get("onBatchFailure") or []]
    lines.append("testing: when a batch fails: " + (", ".join(handling) if handling else "nothing is started")
                 + (f"; misses above {pol['maxMissPercent']}% make a cmd run full" if pol.get("maxMissPercent") else ""))
    return lines
