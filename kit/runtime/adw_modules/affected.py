"""Which checks a change needs, and which results can be used again.

A codebase cmd says what it covers (`#:covers`), whether it runs for every change (`#:always`) and
whether a pass may be reused (`#:reuse`). This module decides, for one check at one moment, between

- `run`: run the command;
- `reuse`: the same command already passed in this run on a tree with exactly this content;
- `not-affected`: affected checks were asked for (`#:when-affected #t` on the step, `#:require
  affected` at the gate) and no changed path is under what the command covers.

Everything here fails safe: when the changed paths or the tree cannot be computed, when the change
touches the layer, a path no check covers or a file several selective checks declare, the answer
is `run`. A check that is not run is recorded as such, with the paths compared, never as passed.

`steps.py` is the only caller: the review and the gate (`review.py`, `ship.py`) reach this module
through `steps.run_check_with_resolve` and `steps.run_logged`. Standard library only.
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import gitsafe
from .errors import LayerError, StepFailed

GIT_TIMEOUT = 300
MAX_PATHS = 50          # compared paths kept per ledger entry and event (paths_total has the real count)
MAX_REPORT_PATHS = 20   # compared paths printed in the published report
PASSED, FAILED, NOT_AFFECTED, REUSED = "passed", "failed", "not-affected", "reused"
RUN, REUSE = "run", "reuse"
REPORT_HEADING = "## What the checks proved"

_warned: set[str] = set()


# ---------------------------------------------------------------- coverage matching

def _entry(text: str) -> str:
    """A `#:covers` entry in repository-relative form; "" means the whole repository."""
    value = str(text).strip()
    if value in {"", ".", "./"}:
        return ""
    value = posixpath.normpath(value)
    while value.startswith("./"):
        value = value[2:]
    return "" if value == "." else value


def matches(path: str, patterns: list[str]) -> bool:
    """True when `path` (relative to the repository root) is under one of `patterns`: a directory
    (`kit/dashboard`), a file, or a glob (`*.md`, `docs/**`). Case-sensitive."""
    target = posixpath.normpath(str(path))
    parts = target.split("/")
    prefixes = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    for raw in patterns:
        entry = _entry(raw)
        if entry == "":
            return True
        if any(ch in entry for ch in "*?["):
            pattern = entry[:-3] if entry.endswith("/**") else entry
            if any(fnmatch.fnmatchcase(prefix, pattern) for prefix in prefixes):
                return True
            if "/" not in pattern and fnmatch.fnmatchcase(parts[-1], pattern):
                return True
        elif target == entry or target.startswith(entry + "/"):
            return True
    return False


def _check(layer: Any, name: str) -> dict[str, Any]:
    try:
        cmd = layer.cmd(name)
    except (LayerError, KeyError, AttributeError):
        cmd = {}
    # an IR written before this feature has none of the three keys: the check runs for every change
    return {"name": str(name), "covers": [str(item) for item in cmd.get("covers") or []],
            "always": cmd.get("always", True) is not False, "reuse": cmd.get("reuse", True) is not False}


def _review_option(layer: Any, adw: dict[str, Any]) -> dict[str, Any]:
    try:
        return layer.option("reviews", adw["review"]) if adw.get("review") else {}
    except (LayerError, KeyError, AttributeError):
        return {}


def workflow_checks(layer: Any, adw: dict[str, Any]) -> list[dict[str, Any]]:
    """The checks of one workflow: the cmds of its check steps, then its review option's
    required checks, each once: [{name, covers, always, reuse}]."""
    names: list[str] = []
    for step in adw.get("steps", []):
        if step.get("kind") == "check" and step.get("cmd"):
            names.append(str(step["cmd"]))
    names += [str(name) for name in _review_option(layer, adw).get("requireChecks") or []]
    return [_check(layer, name) for name in dict.fromkeys(names)]


# ---------------------------------------------------------------- selection (pure)

def select(checks: list[dict[str, Any]], changed: list[str] | None, *, layer_dir: str = "",
           changes_layer: bool = False) -> dict[str, Any]:
    """Which of `checks` a change of the paths `changed` needs:
    {"everything": why or "", "run": {name: why}, "skip": {name: why}}. First match wins:

    1. the changed paths are unknown                               → everything
    2. a layer-change workflow, or a path under the layer directory → everything
    3. a path no check covers                                       → everything
    4. a path two or more `always #f` checks declare                → everything
    5. otherwise: `always` checks, and checks whose `covers` matches a changed path."""
    def everything(why: str) -> dict[str, Any]:
        return {"everything": why, "run": {check["name"]: why for check in checks}, "skip": {}}

    if changed is None:
        return everything("the changed paths could not be computed")
    root = _entry(layer_dir) if layer_dir else ""
    if changes_layer:
        return everything("the change touches the agentic layer (a layer-change workflow)")
    if root:
        for path in changed:
            if path == root or path.startswith(root + "/"):
                return everything(f"the change touches the agentic layer ({path})")
    for path in changed:
        if not any(matches(path, check["covers"]) for check in checks):
            return everything(f"{path} is covered by no check")
    for path in changed:
        selective = [check["name"] for check in checks if not check["always"] and matches(path, check["covers"])]
        if len(selective) > 1:
            return everything(f"{path} is declared by several checks ({', '.join(selective)})")
    result: dict[str, Any] = {"everything": "", "run": {}, "skip": {}}
    for check in checks:
        hits = [path for path in changed if matches(path, check["covers"])]
        if check["always"]:
            result["run"][check["name"]] = "runs for every change"
        elif hits:
            more = f" and {len(hits) - 1} more" if len(hits) > 1 else ""
            result["run"][check["name"]] = f"covers {hits[0]}{more}"
        else:
            result["skip"][check["name"]] = "no covered path changed"
    return result


# ---------------------------------------------------------------- git: tree, base, changed paths

class _GitFailed(Exception):
    pass


def _sandbox(env_stack: Any) -> Any:
    return getattr(env_stack, "sandbox", None)


def _git(state: Any, env_stack: Any, args: list[str], index: Path | None = None) -> str:
    """git in the run's worktree; a sandboxed run goes through gitsafe (frozen container, verified
    metadata, pinned locations). `index` replaces the index file, so the real one is never written."""
    sandbox = _sandbox(env_stack)
    extra = {"GIT_INDEX_FILE": str(index)} if index is not None else {}
    try:
        if sandbox is not None:
            with gitsafe.session(sandbox):
                result = subprocess.run(["git", *gitsafe.HARDENING, *args],
                                        env={**gitsafe.worktree_env(sandbox), **extra}, cwd=sandbox.worktree,
                                        capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
        else:
            worktree = str(getattr(state, "worktree", "") or "")
            if not worktree or not Path(worktree).is_dir():
                raise _GitFailed("the run has no worktree")
            result = subprocess.run(["git", "-C", worktree, *args], env={**os.environ, **extra},
                                    capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError, StepFailed) as exc:
        raise _GitFailed(f"git {' '.join(args)}: {exc}") from exc
    if result.returncode != 0:
        raise _GitFailed(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def _warn(state: Any, what: str, exc: BaseException) -> None:
    key = f"{getattr(state, 'adw_id', '')}:{what}"
    if key not in _warned:
        _warned.add(key)
        print(f"[adw {getattr(state, 'adw_id', '?')}] checks: {what} could not be computed, so every check runs "
              f"and no result is reused: {str(exc)[:300]}")


def _copy_files(layer: Any, state: Any) -> list[str]:
    """`#:copy-files` of the ADW's worktree option (e.g. .env): never part of what ships."""
    files: list[str] = []
    try:
        for option_id in layer.adw(state.adw).get("environment", []):
            option = layer.option("environments", option_id)
            if option.get("kind") == "worktree":
                files += [str(item) for item in option.get("copyFiles", [])]
    except LayerError:  # a run of something the layer does not declare (a promotion) copies no files
        return []
    return files


def tree(layer: Any, state: Any, env_stack: Any) -> str:
    """The tree hash of the worktree's content as it would be committed: everything staged into a
    temporary copy of the index (the real index is not touched), the worktree option's copied
    files left out. Equals `HEAD^{tree}` once everything is committed. "" when git fails."""
    sandbox = _sandbox(env_stack)
    if sandbox is None and not str(getattr(state, "worktree", "") or ""):
        return ""  # a run without a worktree has no tree to hash: its checks simply run
    try:
        with tempfile.TemporaryDirectory(prefix="al-tree-") as scratch:
            with gitsafe.session(sandbox) if sandbox is not None else contextlib.nullcontext():
                if sandbox is not None:
                    real = Path(sandbox.admin) / "index"
                else:
                    real = Path(_git(state, env_stack, ["rev-parse", "--git-path", "index"]).strip())
                    if not real.is_absolute():
                        real = Path(state.worktree) / real
                index = Path(scratch) / "index"
                if real.is_file():  # a missing index starts empty; copy2 keeps its mtime, so git still
                    shutil.copy2(real, index)  # re-hashes racily clean entries (a same-size edit in the same second)
                _git(state, env_stack, ["add", "-A"], index)
                for relative in _copy_files(layer, state):
                    with contextlib.suppress(_GitFailed):
                        _git(state, env_stack, ["reset", "-q", "--", relative], index)
                return _git(state, env_stack, ["write-tree"], index).strip()
    except Exception as exc:  # noqa: BLE001 — a hash that cannot be computed means "run the check", never a failure
        _warn(state, "the worktree's tree hash", exc)
        return ""


def head_tree(layer: Any, state: Any, env_stack: Any) -> str:
    """The tree of the worktree's HEAD commit; "" when git fails."""
    del layer
    try:
        return _git(state, env_stack, ["rev-parse", "--verify", "HEAD^{tree}"]).strip()
    except Exception as exc:  # noqa: BLE001
        _warn(state, "HEAD's tree", exc)
        return ""


def base(layer: Any, state: Any, env_stack: Any) -> str:
    """The commit the run's change is measured against: the merge base of HEAD and the branch the
    run delivers into; "" when git fails."""
    try:
        return _git(state, env_stack, ["merge-base", "HEAD", f"refs/heads/{layer.default_branch}"]).strip()
    except Exception as exc:  # noqa: BLE001
        _warn(state, "the run's base", exc)
        return ""


def changed(layer: Any, state: Any, env_stack: Any, since: str, tree_hash: str) -> list[str] | None:
    """Paths that differ between `since` (a commit or tree) and `tree_hash`, sorted. A rename counts
    as its old and its new path, a deleted file counts. None when either is unknown or git fails."""
    del layer
    if not since or not tree_hash:
        return None
    try:
        output = _git(state, env_stack, ["diff-tree", "-r", "--name-only", "-z", "--no-renames", since, tree_hash])
    except Exception as exc:  # noqa: BLE001
        _warn(state, "the changed paths", exc)
        return None
    return sorted({path for path in output.split("\0") if path})


def definition(layer: Any, state: Any, cmd: dict[str, Any], step: dict[str, Any] | None) -> str:
    """What a result belongs to besides the tree: the command line, where it runs, its skip rules
    and the sandbox image. A pass is only reused for the same definition."""
    step = step or {}
    try:
        environment = str(step.get("in") or "") or list(layer.adw(state.adw).get("environment", []))
    except (LayerError, KeyError, AttributeError):
        environment = str(step.get("in") or "")
    data = {"run": str(cmd.get("run", "")), "env": environment, "maxSkipped": int(step.get("maxSkipped", -1)),
            "forbidSkipReason": str(step.get("forbidSkipReason", "")), "image": str(getattr(state, "image", "") or "")}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the ledger (state.check_results)

def _ledger(state: Any) -> list[dict[str, Any]]:
    ledger = getattr(state, "check_results", None)
    if ledger is None:
        ledger = []
        with contextlib.suppress(AttributeError, TypeError):
            state.check_results = ledger
    return ledger


def record(state: Any, **entry: Any) -> dict[str, Any]:
    """Append one decision or result to the run's ledger and return it."""
    paths = [str(path) for path in entry.get("paths") or []]
    source = entry.get("from") or None
    item = {
        "check": str(entry.get("check", "")), "step": str(entry.get("step", "")),
        "phase": str(entry.get("phase", "")), "outcome": str(entry.get("outcome", "")),
        "tree": str(entry.get("tree") or ""), "definition": str(entry.get("definition") or ""),
        "exit": entry.get("exit"), "log": str(entry.get("log") or ""), "why": str(entry.get("why") or ""),
        "paths": paths[:MAX_PATHS], "paths_total": int(entry.get("paths_total") or len(paths)),
        "from": ({"step": str(source.get("step", "")), "phase": str(source.get("phase", "")),
                  "log": str(source.get("log", ""))} if source else None),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    # the depth and its accounting (testing.accounting), only where the run tracks depths: an
    # entry of a layer without (testing …) is exactly what it was before
    if entry.get("depth"):
        item["depth"] = str(entry["depth"])
    if isinstance(entry.get("testing"), dict):
        item["testing"] = dict(entry["testing"])
    _ledger(state).append(item)
    return item


def passing(state: Any, check: str, definition_hash: str, tree_hash: str) -> dict[str, Any] | None:
    """The latest real pass of `check` with this definition on exactly this tree. A result
    without a tree (the command changed the worktree, or git failed) is never found."""
    if not tree_hash:
        return None
    for entry in reversed(_ledger(state)):
        if (entry.get("check") == check and entry.get("outcome") == PASSED and entry.get("tree") == tree_hash
                and entry.get("definition") == definition_hash):
            return entry
    return None


def last_pass(state: Any, check: str, definition_hash: str) -> dict[str, Any] | None:
    """The latest real pass of `check` with this definition that knows its tree."""
    for entry in reversed(_ledger(state)):
        if (entry.get("check") == check and entry.get("outcome") == PASSED and entry.get("tree")
                and entry.get("definition") == definition_hash):
            return entry
    return None


# ---------------------------------------------------------------- decisions

@dataclass
class Decision:
    action: str = RUN                    # run | reuse | not-affected
    tree: str = ""
    definition: str = ""
    why: str = ""
    paths: list[str] = field(default_factory=list)
    paths_total: int = 0
    source: dict[str, Any] | None = None  # the ledger entry a `reuse` takes its pass from
    reference: str = ""                   # what the tree was compared with (a commit or a tree)


def _selection(layer: Any, state: Any, env_stack: Any, name: str, reference: str, tree_hash: str,
               decision: Decision) -> Decision:
    paths = changed(layer, state, env_stack, reference, tree_hash)
    selection = select(workflow_checks(layer, layer.adw(state.adw)), paths,
                       layer_dir=str(getattr(layer, "in_repo_dir", "") or ""),
                       changes_layer=bool(layer.changes_layer(state.adw)))
    decision.reference = reference
    decision.paths, decision.paths_total = list(paths or [])[:MAX_PATHS], len(paths or [])
    if name in selection["skip"]:
        decision.action, decision.why = NOT_AFFECTED, str(selection["skip"][name])
    else:
        decision.why = str(selection["run"].get(name, ""))
    return decision


def reference(layer: Any, state: Any, env_stack: Any, step: dict[str, Any], tree_hash: str = "",
              definition_hash: str | None = None) -> str:
    """What a check's change is measured against: the tree of the cmd's latest pass in this run
    (with `definition_hash`: of that definition, as `decide` compares; without: of any depth, on
    another tree than `tree_hash`, so a review round selects from what its patch touched), else
    the run's base. "" when git fails."""
    try:
        name = str(layer.cmd(step["cmd"]).get("name") or step["cmd"])
    except (LayerError, KeyError, AttributeError):
        name = str(step.get("cmd") or "")
    if definition_hash is not None:
        previous = last_pass(state, name, definition_hash)
    else:
        previous = next((entry for entry in reversed(_ledger(state))
                         if entry.get("check") == name and entry.get("outcome") == PASSED and entry.get("tree")
                         and entry.get("tree") != tree_hash), None)
    return str(previous["tree"]) if previous else base(layer, state, env_stack)


def decide(layer: Any, state: Any, env_stack: Any, step: dict[str, Any],
           command: dict[str, Any] | None = None) -> Decision:
    """What a check step (or a review round's re-check of it) does now. `command` is the form the
    depth chose (testing.plan_check); its line is part of the definition, so a pass at one depth is
    never reused for another."""
    decision = Decision()
    try:
        cmd = layer.cmd(step["cmd"])
        name = str(cmd.get("name") or step["cmd"])
        decision.tree = tree(layer, state, env_stack)
        decision.definition = definition(layer, state, command if command is not None else cmd, step)
        if cmd.get("reuse", True) is not False:
            source = passing(state, name, decision.definition, decision.tree)
            if source is not None:
                decision.action, decision.source, decision.why = REUSE, source, "the same tree already passed"
                return decision
        if step.get("whenAffected"):
            since = reference(layer, state, env_stack, step, decision.tree, decision.definition)
            return _selection(layer, state, env_stack, name, since, decision.tree, decision)
    except Exception as exc:  # noqa: BLE001 — a decision that cannot be made means "run the check"
        _warn(state, f"the decision for check {step.get('name')}", exc)
        decision.action, decision.source = RUN, None
    return decision


def decide_gate(layer: Any, state: Any, env_stack: Any, cmd: dict[str, Any], step: dict[str, Any] | None,
                option: dict[str, Any], command: dict[str, Any] | None = None) -> Decision:
    """What the gate does with one required cmd. Only a tree that is exactly what ships (the
    worktree's content equals HEAD's tree) can reuse a pass or be judged not affected; the
    comparison for `#:require affected` is always against the run's base, never a later pass.
    `command` is the form the gate's depth chose (part of the definition, as in `decide`)."""
    decision = Decision()
    try:
        name = str(cmd.get("name", ""))
        decision.definition = definition(layer, state, command if command is not None else cmd, step)
        current = tree(layer, state, env_stack)
        if not current or current != head_tree(layer, state, env_stack):
            return decision  # uncommitted changes, or unknown: the gate runs the cmd, its result has no tree
        decision.tree = current
        if cmd.get("reuse", True) is not False:
            source = passing(state, name, decision.definition, current)
            if source is not None:
                decision.action, decision.source, decision.why = REUSE, source, "the same tree already passed"
                return decision
        if str(option.get("require") or "all") == "affected":
            return _selection(layer, state, env_stack, name, base(layer, state, env_stack), current, decision)
    except Exception as exc:  # noqa: BLE001
        _warn(state, f"the gate's decision for {cmd.get('name')}", exc)
        decision.action, decision.source = RUN, None
    return decision


# ---------------------------------------------------------------- what a reader sees

def _depth_text(entry: dict[str, Any]) -> str:
    """The accounting of a result that carries one (testing.accounting_text); "" otherwise."""
    acc = entry.get("testing")
    if not isinstance(acc, dict):
        return ""
    from . import testing  # testing imports this module

    return testing.accounting_text(acc)


def summary_text(entry: dict[str, Any]) -> str:
    """One result in words: `passed`, `failed (exit N)`, `not affected (WHY; N paths compared)`,
    `reused from STEP, same tree HASH8`. A result that carries its depth (a layer that declares
    (testing …)) adds it: `passed (targeted: ran 14 of 812 tests …)`, `passed (full)`."""
    outcome = str(entry.get("outcome") or "")
    if outcome == NOT_AFFECTED:
        return f"not affected ({entry.get('why') or 'no reason recorded'}; {int(entry.get('paths_total') or 0)} paths compared)"
    if outcome == REUSED:
        source = entry.get("from") or {}
        return f"reused from {source.get('step') or '?'}, same tree {str(entry.get('tree') or '')[:8]}"
    depth = _depth_text(entry)
    depth = f" ({depth})" if depth else ""
    if outcome == PASSED or (not outcome and entry.get("exit") == 0):
        return "passed" + depth
    return f"failed (exit {entry.get('exit')})" + depth


def depth_cell(entry: dict[str, Any]) -> str:
    """The short depth of a result for a table: `targeted 14/812`, `targeted 14`, `full`, `smoke`;
    "" for a result without one (not run, or a layer without (testing …))."""
    acc = entry.get("testing") if isinstance(entry.get("testing"), dict) else {}
    depth = str(entry.get("depth") or acc.get("depth") or "")
    if not depth:
        return ""
    if depth == "targeted":
        ran, total = acc.get("ran"), acc.get("total")
        if ran is not None and total:
            return f"targeted {ran}/{total}"
        return f"targeted {ran}" if ran is not None else "targeted"
    return depth


def rows(layer: Any, state: Any) -> list[dict[str, Any]]:
    """Per check, in the order the run first met it: its latest result in a check step or a review
    round, then its latest result at the gate. A row carries `depth` and `testing` when its result
    has them (a reused result: those of the pass it reuses)."""
    del layer
    ledger = _ledger(state)
    result: list[dict[str, Any]] = []
    for check in dict.fromkeys(str(entry.get("check")) for entry in ledger):
        mine = [entry for entry in ledger if entry.get("check") == check]
        step_entry = next((e for e in reversed(mine) if e.get("phase") in ("check", "recheck")), None)
        gate_entry = next((e for e in reversed(mine) if e.get("phase") == "gate"), None)
        for entry in (step_entry, gate_entry):
            if entry is None:
                continue
            where = ("gate" if entry.get("phase") == "gate"
                     else f"{entry.get('step')} (review round)" if entry.get("phase") == "recheck"
                     else str(entry.get("step")))
            row = {"check": check, "where": where,
                   **{key: entry.get(key) for key in ("outcome", "why", "tree", "exit", "log", "paths",
                                                      "paths_total", "from")}}
            for key in ("depth", "testing"):
                if entry.get(key):
                    row[key] = entry[key]
            result.append(row)
    return result


def _evidence(layer: Any, row: dict[str, Any]) -> str:
    def shown(log: Any) -> str:
        return f"`{layer.display_path(str(log))}`" if log else ""

    outcome = row.get("outcome")
    if outcome == NOT_AFFECTED:
        return f"{int(row.get('paths_total') or 0)} paths compared, tree `{str(row.get('tree') or '')[:8]}`"
    if outcome == REUSED:
        return f"log {shown((row.get('from') or {}).get('log'))}".strip()
    tree_text = f", tree `{str(row.get('tree'))[:8]}`" if row.get("tree") else ""
    return f"log {shown(row.get('log'))}{tree_text}".strip()


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def report_markdown(layer: Any, state: Any) -> str:
    """The published account of the run's checks; "" for an empty ledger. No machine paths."""
    table = rows(layer, state)
    if not table:
        return ""
    # a Depth column only where depths are tracked: a layer without (testing …) keeps its report
    depths = any(depth_cell(row) for row in table)
    if depths:
        lines = [REPORT_HEADING, "", "| Check | Where | Depth | Result | Evidence |",
                 "| --- | --- | --- | --- | --- |"]
    else:
        lines = [REPORT_HEADING, "", "| Check | Where | Result | Evidence |", "| --- | --- | --- | --- |"]
    for row in table:
        depth = f" {_cell(depth_cell(row) or '—')} |" if depths else ""
        lines.append(f"| `{_cell(row['check'])}` | {_cell(row['where'])} |{depth} {_cell(summary_text(row))} | "
                     f"{_cell(_evidence(layer, row))} |")
    compared = list(dict.fromkeys(path for row in table for path in row.get("paths") or []))
    total = max([len(compared)] + [int(row.get("paths_total") or 0) for row in table])
    if compared:
        lines += ["", "Paths compared:", ""]
        lines += [f"- `{path}`" for path in compared[:MAX_REPORT_PATHS]]
        if total > MAX_REPORT_PATHS:
            lines.append(f"- … and {total - MAX_REPORT_PATHS} more")
    return "\n".join(lines) + "\n"


def publish(layer: Any, state: Any) -> None:
    """Put the report where the pull request text is: a comment on the run's pull request, or the
    end of its PR.md. Never raises; what happened is in state.ship["checks_report"]."""
    try:
        report = report_markdown(layer, state)
        if not report or not state.ship.get("action"):  # no check ran, or the run neither shipped nor was handed over
            return
        result: dict[str, Any] = {"posted": False, "where": "", "warning": ""}
        ship = state.ship
        pr = ship.get("pr") if isinstance(ship.get("pr"), dict) else None
        target = str(ship.get("target") or "")
        try:
            if (pr and isinstance(pr.get("number"), int) and layer.singleton("forge")["kind"] != "none"
                    and not state.dry_run):
                from . import forge  # imported only when a forge is used, as ship does

                forge.get_client(layer).comment(pr["number"], report)
                result.update(posted=True, where=f"PR #{pr['number']}")
            elif target.endswith("PR.md") and (Path(layer.layer_dir) / target).is_file():
                path = Path(layer.layer_dir) / target
                text = path.read_text(encoding="utf-8")
                if REPORT_HEADING in text:  # a resumed run replaces its earlier report
                    text = text[:text.index(REPORT_HEADING)]
                path.write_text(text.rstrip("\n") + "\n\n" + report, encoding="utf-8")
                result.update(posted=True, where=target)
        except Exception as exc:  # noqa: BLE001 — the report is a courtesy; shipping already happened
            result["warning"] = f"{type(exc).__name__}: {exc}"[:500]
            print(f"[adw {state.adw_id}] warning: the checks report was not published: {result['warning']}")
        ship["checks_report"] = result
        state.save(layer.layer_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[adw {getattr(state, 'adw_id', '?')}] warning: the checks report failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- coverage report (al verify, al sandbox doctor)

def coverage_report(layer: Any, adw: str | None = None) -> dict[str, Any]:
    """What the layer's checks declare: {"declared": any check has #:covers, "undeclared": checks
    without, "uncovered": source paths no check covers}. Checks are the cmds of check steps (of
    workflow `adw`, or of every workflow), every review's required checks and every stage's checks."""
    names: list[str] = []
    for entry in layer.ir.get("adws", []):
        if adw and entry.get("name") != adw:
            continue
        names += [str(step["cmd"]) for step in entry.get("steps", [])
                  if step.get("kind") == "check" and step.get("cmd")]
    for option in layer.ir.get("reviews", []):
        names += [str(name) for name in option.get("requireChecks") or []]
    for stage in layer.ir.get("stages") or []:
        names += [str(name) for name in stage.get("checks") or []]
    checks = [_check(layer, name) for name in dict.fromkeys(names)]
    covers = [entry for check in checks for entry in check["covers"]]
    source = [str(path) for path in (layer.ir.get("codebase", {}).get("paths") or {}).get("source") or []]
    return {"declared": bool(covers),
            "undeclared": [check["name"] for check in checks if not check["covers"]],
            "uncovered": [path for path in source if not matches(path, covers)]}


def coverage_lines(report: dict[str, Any]) -> list[str]:
    if not report.get("declared"):
        return ["coverage: no check declares #:covers, so every check runs for every change"]
    lines: list[str] = []
    undeclared = list(report.get("undeclared") or [])
    if undeclared:
        lines.append(f"coverage: {len(undeclared)} check{'s declare' if len(undeclared) != 1 else ' declares'} "
                     f"no #:covers and run{'' if len(undeclared) != 1 else 's'} for every change: {', '.join(undeclared)}")
    lines += [f"coverage: source path {path} is covered by no check: a change there runs every check"
              for path in report.get("uncovered") or []]
    return lines or ["coverage: every check declares #:covers and every source path is covered"]
