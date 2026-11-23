"""The evidence pack and the reviewer-free gap rounds (step `evidence`, before an agent or auto review).

For each row of the plan's Proof plan (proofplan.py) the pack records what proves it for the exact
tree the reviewers will read (a row narrowed to some tests: the narrowed command's own run and log): the command's result (exit, tree, passed/failed/skipped counts, the
named test ids found in its log) and the files it wrote (collected artefacts, run-scoped proof).
Each row gets a state: `proven`, `missing`, `failed`, `stale`, `skipped-only`, `not-run` or
`files-missing`. A narrowed row whose own log shows no passed result for the tests it names is
`not-run`.

A row that is not proven is a gap. A gap only an agent can close (`fixable`) goes to a patch round
with that one line, without calling a reviewer: agents `evidence-patch-N` and
`evidence-implement-N` (the review step's #:patch and #:implement), then the checks that ran
again. A gap that survives the first patch gets one different-approach patch with the first
attempt's diff (convergence.patch_request); one that survives that stops the run
(`evidence:no-progress`). A gap that needs an environment, a declaration or a person stops at once.

A check pass the run carried forward as not affected (`carried` on its record) proves a row when
none of the changed paths is a file the row names; a stale row whose check would be skipped again
has its check run once in full in the step instead of a patch.

Before the pack, a change under a protected path that no `edit:PATH` approval covers stops the run
for an approver. The pack, the approvals and the round scope reach the reviewers through the
artefact manifest (convergence.manifest_entries).
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath
from typing import Any

from . import affected, agents, approvals, artefacts, delivery, events, proofplan, skips, state as state_module
from . import steps, storage, testing
from .errors import LayerError, StepFailed

STEP = "evidence"
ENV_WORDS = ("not installed", "not found", "no podman", "no chrome", "network", "unavailable")
STATES = ("proven", "missing", "failed", "stale", "skipped-only", "not-run", "files-missing")
KIND_PARTY = {"needs-environment": "operator", "needs-declaration": "layer author", "needs-person": "approver"}
MAX_PATCHES = 2
DIFF_MAX = 20_000
CONTEXT_DIR = "review-context"


def context_dir(layer: Any, state: Any) -> Path:
    return storage.artefacts_root(layer) / state.adw_id / CONTEXT_DIR


# ---------------------------------------------------------------- the pack

def _read(layer: Any, relative: str) -> str:
    if not relative:
        return ""
    try:
        return (Path(layer.layer_dir) / relative).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _test_seen(log: str, test_id: str) -> str:
    """`named in log`, `skipped in log` or `not shown`."""
    names = [test_id] + ([test_id.rsplit("::", 1)[-1]] if "::" in test_id else [])
    lines = [line for line in log.splitlines() if any(name in line for name in names)]
    if not lines:
        return "not shown"
    if all(re.search(r"\bSKIP", line, re.IGNORECASE) for line in lines):
        return "skipped in log"
    return "named in log"


def _run_cmd(layer: Any, state: Any, stack: Any, cmd: dict[str, Any], *, label: str = "") -> dict[str, Any]:
    """Run a cmd no check step runs, once per tree (a pass on the same committed tree is reused);
    returns its ledger entry."""
    name = str(cmd.get("name"))
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label or name) or "cmd"
    log_path = Path(layer.layer_dir) / "agents" / state.adw_id / "checks" / f"evidence-{safe}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    steps.run_logged(layer, state, stack, cmd, log_path, step=STEP, phase=STEP)
    entry = next((e for e in reversed(getattr(state, "check_results", []) or [])
                  if e.get("check") == name and e.get("phase") == STEP), {})
    return entry


def _artefact_relative(item: dict[str, Any]) -> str:
    path = str(item.get("path") or "")
    marker = f"/{item.get('id')}/"
    return path.split(marker, 1)[1] if marker in path else PurePosixPath(path).name


def _matches(relative: str, glob: str) -> bool:
    if fnmatch.fnmatchcase(relative, glob):
        return True
    try:
        return PurePosixPath(relative).match(glob)
    except ValueError:
        return False


def _files(state: Any, row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(the collected files of the row, the globs that found none)."""
    found: list[str] = []
    empty: list[str] = []
    collected = [item for item in state.artefacts if not str(item.get("id", "")).startswith("proof-")]
    for item in row.get("writes") or []:
        glob = str(item.get("glob") or "")
        hits = [str(a.get("path")) for a in collected if _matches(_artefact_relative(a), glob)]
        if not hits:
            empty.append(glob)
        found += hits
    proof_items = [item for item in state.artefacts if str(item.get("id", "")).startswith("proof-")]
    by_relative = {_artefact_relative(item): str(item.get("path")) for item in proof_items}
    for item in row.get("proof") or []:
        glob = str(item.get("glob") or "")
        hits = [path for rel, path in by_relative.items() if _matches(rel, glob)]
        if not hits:
            empty.append(f"proof:{glob}")
        found += hits
    return list(dict.fromkeys(found)), empty


def _named_files(row: dict[str, Any]) -> list[str]:
    """The files a row names: its Writes globs and the files of its test ids (`proof:` globs are
    git-ignored and never change)."""
    named = [str(item.get("glob") or "") for item in row.get("writes") or []]
    named += [str(test).split("::", 1)[0] for test in row.get("tests") or []]
    return [name for name in dict.fromkeys(named) if name]


def _carried(record: dict[str, Any], row: dict[str, Any], current: str) -> tuple[dict[str, Any] | None, list[str]]:
    """(the record's carried pass when it carries to the current tree, the changed paths that are
    files the row names). Any hit means the carried pass does not prove the row."""
    carried = record.get("carried")
    if not isinstance(carried, dict) or not current or carried.get("tree") != current:
        return None, []
    outcome = record.get("outcome")
    if not ((outcome == affected.PASSED and carried.get("from_tree") == record.get("tree"))
            or outcome == affected.NOT_AFFECTED):
        return None, []
    # test ids are relative to where the command runs, so a path also matches by suffix; a doubtful
    # match falls to stale (and a forced re-run), never to a false proof
    named = _named_files(row)
    hits = [str(path) for path in carried.get("changed") or []
            if any(_matches(str(path), name) or str(path) == name or str(path).endswith("/" + name)
                   for name in named)]
    return carried, hits


def _carried_note(carried: dict[str, Any]) -> str:
    changed = [str(path) for path in carried.get("changed") or []]
    total = int(carried.get("changed_total") or len(changed))
    more = f" and {total - len(changed)} more" if total > len(changed) else ""
    return f"carried from {str(carried.get('from_tree') or '')[:8]} (only {', '.join(changed)}{more} changed)"


def _pack_row(layer: Any, state: Any, adw: dict[str, Any], stack: Any, row: dict[str, Any],
              current: str) -> dict[str, Any]:
    check, cmd = proofplan.resolve_command(layer, adw, str(row.get("command") or ""))
    out: dict[str, Any] = {"id": row.get("id"), "criterion": row.get("criterion"), "command": row.get("command"),
                           "check": "", "exit": None, "tree": "none", "passed": 0, "failed": 0, "skipped": 0,
                           "reasons": [], "tests": {}, "files": [], "log": "", "state": "missing", "note": ""}
    log_text = ""
    narrowed = False
    if cmd is None:
        out["note"] = "the command is no cmd or check of the layer"
    elif check is not None and not cmd.get("selection"):
        name = str(check.get("name"))
        out["check"] = name
        record = state.checks.get(name)
        carried, hits = _carried(record, row, current) if record else (None, [])
        if carried is not None and not hits:
            if record.get("outcome") == affected.NOT_AFFECTED:
                out["exit"] = 0
                out["log"] = str(carried.get("log") or "")
                log_text = _read(layer, out["log"])
                report = skips.from_log(log_text)
                if report is not None:
                    found = report.as_dict()
                    out.update(passed=int(found["passed"]), failed=int(found["failed"]),
                               skipped=int(found["skipped"]), reasons=list(found["reasons"]))
            else:
                out["exit"] = record.get("exit")
                found = record.get("skips") or {}
                out.update(passed=int(found.get("passed") or 0), failed=int(found.get("failed") or 0),
                           skipped=int(found.get("skipped") or 0), reasons=list(found.get("reasons") or []))
                source = record.get("from") or {}
                out["log"] = str(source.get("log") or record.get("log") or "")
                log_text = _read(layer, out["log"])
            out["tree"] = "carried"
            out["carried"] = {"from_tree": carried.get("from_tree"), "changed": list(carried.get("changed") or []),
                              "changed_total": carried.get("changed_total"), "log": carried.get("log")}
            out["note"] = _carried_note(carried)
        elif not record or record.get("outcome") == affected.NOT_AFFECTED:
            out["state"] = "not-run"
            out["note"] = "the check did not run" if not record else f"not affected: {record.get('why')}"
        else:
            out["exit"] = record.get("exit")
            tree = str(record.get("tree") or "")
            out["tree"] = "current" if tree and tree == current else ("older" if tree else "none")
            found = record.get("skips") or {}
            out.update(passed=int(found.get("passed") or 0), failed=int(found.get("failed") or 0),
                       skipped=int(found.get("skipped") or 0), reasons=list(found.get("reasons") or []))
            source = record.get("from") or {}
            out["log"] = str(source.get("log") or record.get("log") or "")
            log_text = _read(layer, out["log"])
        if hits:
            out["note"] = f"a file this criterion names changed: {', '.join(hits)}"
    else:
        # a row narrowed to some tests is proven by its own run, whose log names them (-rA, -v)
        narrowed = bool(cmd.get("selection"))
        out["check"] = f"narrowed {cmd.get('name')}" if narrowed else f"cmd {cmd.get('name')}"
        entry = _run_cmd(layer, state, stack, cmd, label=f"{cmd.get('name')}-{row.get('id')}" if narrowed else "")
        out["exit"] = entry.get("exit")
        tree = str(entry.get("tree") or "")
        out["tree"] = "current" if tree and tree == current else ("older" if tree else "none")
        source = entry.get("from") or {}
        out["log"] = str(source.get("log") or entry.get("log") or "")
        log_text = _read(layer, out["log"])
        report = skips.from_log(log_text)
        if report is not None:
            found = report.as_dict()
            out.update(passed=int(found["passed"]), failed=int(found["failed"]), skipped=int(found["skipped"]),
                       reasons=list(found["reasons"]))
    out["tests"] = {str(test): _test_seen(log_text, str(test)) for test in row.get("tests") or []}
    out["files"], empty = _files(state, row)
    too_many = (state.proof_plan.get("too_many") or {}).get(str(row.get("id")))
    if cmd is None:
        out["state"] = "missing"
    elif out["state"] == "not-run":
        pass
    elif out["exit"] not in (0, None) or out["failed"]:
        out["state"] = "failed"
    elif out["tree"] == "older":
        out["state"] = "stale"
    elif (out["passed"] == 0 and out["skipped"] > 0) or (
            out["tests"] and any(seen == "skipped in log" for seen in out["tests"].values())):
        out["state"] = "skipped-only"
    elif narrowed and (out["passed"] == 0 or any(seen == "not shown" for seen in out["tests"].values())):
        # a narrowed row is proven by its own run: a log with no passed test, or without a test the row names, proves nothing
        out["state"] = "not-run"
        missing = [test for test, seen in out["tests"].items() if seen == "not shown"]
        out["note"] = ("the narrowed run's log shows no result for: " + ", ".join(missing) if missing
                       else "the narrowed run's log shows no passed test")
    elif empty or too_many:
        out["state"] = "files-missing"
        out["note"] = (f"too many proof files ({too_many})" if too_many
                       else f"no file for {', '.join(empty)}")
    else:
        out["state"] = "proven"
    out["empty"] = empty
    return out


def _cell(text: Any) -> str:
    return " ".join(str(text if text is not None else "-").split()).replace("|", "\\|") or "-"


def render_pack(pack: dict[str, Any]) -> str:
    lines = ["# Evidence pack", "", f"Tree: `{pack.get('tree') or 'unknown'}`", "",
             "| # | Criterion | State | Command | Exit | Tree | Passed | Failed | Skipped | Tests | Files |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in pack.get("rows") or []:
        tests = "; ".join(f"{k}: {v}" for k, v in (row.get("tests") or {}).items()) or "-"
        lines.append("| " + " | ".join(_cell(v) for v in (
            row.get("id"), row.get("criterion"), row.get("state") + (f" ({row['note']})" if row.get("note") else ""),
            row.get("command"), row.get("exit"), row.get("tree"), row.get("passed"), row.get("failed"),
            row.get("skipped"), tests, ", ".join(row.get("files") or []) or "-")) + " |")
    return "\n".join(lines) + "\n"


def build_pack(layer: Any, state: Any, adw: dict[str, Any], stack: Any) -> dict[str, Any]:
    """One row per Proof plan row (see the module docstring); written to
    artefacts/<id>/review-context/evidence.md and stored in state.proof_plan["pack"]."""
    current = affected.tree(layer, state, stack)
    rows = [_pack_row(layer, state, adw, stack, row, current)
            for row in (state.proof_plan or {}).get("rows") or []]
    pack = {"tree": current, "rows": rows, "at": state_module.now()}
    folder = context_dir(layer, state)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "evidence.md").write_text(render_pack(pack), encoding="utf-8")
    pack["path"] = (folder / "evidence.md").relative_to(layer.layer_dir).as_posix()
    state.proof_plan["pack"] = pack
    states = {str(row["id"]): row["state"] for row in rows}
    for row in state.proof_plan.get("rows") or []:
        if str(row.get("id")) in states:
            row["state"] = states[str(row.get("id"))]
    events.emit("evidence.pack", rows={str(row["id"]): row["state"] for row in rows})
    state.save(layer.layer_dir)
    return pack


# ---------------------------------------------------------------- gaps

def classify_gap(row: dict[str, Any], layer: Any, adw: dict[str, Any] | None = None) -> str:
    """fixable | needs-environment | needs-declaration of one pack row that is not proven."""
    state = row.get("state")
    if state == "skipped-only":
        reasons = " ".join(str(reason[0] if isinstance(reason, (list, tuple)) else reason)
                           for reason in row.get("reasons") or []).lower()
        return "needs-environment" if any(word in reasons for word in ENV_WORDS) else "fixable"
    if state == "files-missing" and adw is not None:
        writes = [{"glob": glob, "count": 1} for glob in row.get("empty") or [] if not glob.startswith("proof:")]
        if writes and proofplan._write_problems(layer, adw, {"writes": writes}):
            return "needs-declaration"
    return "fixable"


def _gaps(layer: Any, adw: dict[str, Any], pack: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []
    for row in pack.get("rows") or []:
        if row.get("state") == "proven":
            continue
        kind = classify_gap(row, layer, adw)
        gaps.append({"id": row.get("id"), "state": row.get("state"), "kind": kind, "criterion": row.get("criterion"),
                     "command": row.get("command"), "note": row.get("note") or "",
                     "fingerprint": f"{row.get('id')}|{kind}"})
    return gaps


def _issue(gap: dict[str, Any]) -> dict[str, Any]:
    return {"severity": "blocker", "criterion": str(gap["id"]), "kind": gap["kind"],
            "description": f"{gap['id']}: {gap['state']}: {gap['criterion']}",
            "resolution": f"make `{gap['command']}` prove it for this tree"}


def _stop(layer: Any, state: Any, outcome: str, party: str, text: str, wanted: list[str] | None = None,
          question: str = "") -> None:
    state.proof_plan["stopped"] = {"at": STEP, "outcome": outcome, "party": party, "reason": text,
                                   "approvals": list(wanted or []), "question": question}
    from . import convergence

    convergence.record_summary(state)
    state.save(layer.layer_dir)
    raise StepFailed(STEP, text)


# ---------------------------------------------------------------- protected paths

def changed_files(layer: Any, state: Any, stack: Any) -> list[str]:
    """Files the run changed: committed since the target branch, plus pending and untracked ones."""
    found: set[str] = set()
    for args in (["diff", "--name-only", "--no-renames", f"{layer.default_branch}...HEAD"],
                 ["diff", "--name-only", "--no-renames", "HEAD"],
                 ["ls-files", "--others", "--exclude-standard"]):
        result = delivery.tree_git(state, stack, args, step=STEP)
        if result.returncode == 0:
            found |= {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return sorted(found)


def protected_unapproved(layer: Any, state: Any, adw: dict[str, Any], stack: Any) -> list[str]:
    from . import review  # read only: its protected-path rule

    protected = list(layer.ir.get("codebase", {}).get("paths", {}).get("protected", []) or [])
    in_repo = str(getattr(layer, "in_repo_dir", "") or "")
    if in_repo and str(adw.get("changes") or "codebase") != "layer":
        protected.append(in_repo)
    if not protected or not state.worktree:
        return []
    return [path for path in changed_files(layer, state, stack)
            if review.is_protected(path, protected) and approvals.covered(state, f"edit:{path}") is None]


# ---------------------------------------------------------------- the step

def _archive_review(state: Any) -> None:
    if (state.review or {}).get("rounds"):
        state.proof_plan.setdefault("earlier_review", []).append(dict(state.review))
        state.review = {}


def _collect(layer: Any, state: Any, adw: dict[str, Any]) -> None:
    option = proofplan._review_option(layer, adw)
    ids = [str(item) for item in option.get("artefacts") or []]
    if ids and state.worktree:
        try:
            artefacts.collect(layer, state, ids, STEP)  # it collects the run-scoped proof too
            return
        except (LayerError, StepFailed) as exc:
            print(f"[adw {state.adw_id}] evidence: artefacts not collected: {exc}")
    proofplan.collect_proof(layer, state, STEP)


def prepare_context(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], stack: Any) -> None:
    """What the step does without a table or an approval: nothing, so a run without them is
    exactly as before (no earlier review is archived, no proof is collected)."""
    del step
    if not proofplan.has_table(state) and not state.approvals:
        return
    _archive_review(state)
    if proofplan.has_table(state):
        _collect(layer, state, adw)
        build_pack(layer, state, adw, stack)
    artefacts.write_manifest(layer, state)


def _patch(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], stack: Any, number: int,
           gaps: list[dict[str, Any]], first_diff: str) -> dict[str, Any]:
    from . import convergence

    issues = [_issue(gap) for gap in gaps]
    request = convergence.patch_request(issues, first_diff=first_diff)
    patch_step, implement_step = f"evidence-patch-{number}", f"evidence-implement-{number}"
    patch_plan = agents.run_agent(layer, state, patch_step, str(step["patch"]), [request, state.plan or ""],
                                  delivery._model_set(layer, str(step["patch"])), stack)
    state.outputs[patch_step] = patch_plan
    implemented = agents.run_agent(layer, state, implement_step, str(step["implement"]), [patch_plan],
                                   delivery._model_set(layer, str(step["implement"])), stack)
    state.outputs[implement_step] = implemented
    rerun, failed = [], []
    for check in delivery._checks_that_ran(state, adw):
        print(f"[adw {state.adw_id}] evidence patch {number}: re-running check {check['name']}")
        try:
            steps.run_check_with_resolve(layer, state, check, stack)
        except StepFailed as exc:
            failed.append(str(exc.reason)[:300])
        rerun.append(check["name"])
    return {"request": request, "patch_plan": patch_plan[:2000], "checks_rerun": rerun, "checks_failed": failed}


def _force_stale(layer: Any, state: Any, adw: dict[str, Any], stack: Any, pack: dict[str, Any],
                 forced: set[str]) -> list[str]:
    """Run the check of each stale row once in full, at most once per check in this step: the
    patch path re-decides the check and would skip it again. A failure is not raised; it shows as
    `failed` in the next pack. Returns the names it ran."""
    ran: list[str] = []
    for row in pack.get("rows") or []:
        if row.get("state") != "stale":
            continue
        check, _cmd = proofplan.resolve_command(layer, adw, str(row.get("command") or ""))
        if check is None or str(check.get("name")) in forced:
            continue
        name = str(check.get("name"))
        forced.add(name)
        print(f"[adw {state.adw_id}] evidence: check {name} is stale and would be skipped; running it once in full")
        plan = testing.CheckPlan("full", "full", dict(layer.cmd(check["cmd"])), "forced: stale in evidence", None,
                                 "tests", "")
        steps.run_check(layer, state, check, stack, plan=plan)
        ran.append(name)
    return ran


def _tree(state: Any, stack: Any) -> str:
    from . import review

    try:
        return review.tree_fingerprint(state.worktree, getattr(stack, "sandbox", None)) if state.worktree else ""
    except StepFailed:
        return ""


def _diff(state: Any, stack: Any, before: str, after: str) -> str:
    if not before or not after or before == after:
        return ""
    result = delivery.tree_git(state, stack, ["diff", before, after], step=STEP)
    return (result.stdout or "")[:DIFF_MAX] if result.returncode == 0 else ""


def run(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], stack: Any) -> dict[str, Any]:
    """The step `evidence` (see the module docstring). Raises StepFailed("evidence", …) on a stop."""
    state.proof_plan.pop("stopped", None)
    _archive_review(state)
    if not proofplan.has_table(state):
        artefacts.write_manifest(layer, state)
        state.save(layer.layer_dir)
        return {}
    hits = protected_unapproved(layer, state, adw, stack)
    if hits:
        wanted = [f"edit:{path}" for path in hits]
        question = f"may this run {', '.join(wanted)}? answer: al approve LAYER {state.adw_id} {' '.join(wanted)}"
        _stop(layer, state, "needs-person", "approver",
              f"evidence:needs-person: the run changed {len(hits)} protected path(s) without an approval: "
              f"{', '.join(hits)}; party: approver\n{question}", wanted, question)
    _collect(layer, state, adw)
    pack = build_pack(layer, state, adw, stack)
    can_patch = bool(step.get("patch")) and bool(step.get("implement"))
    history: list[dict[str, Any]] = []
    state.proof_plan["gaps"] = history
    seen: dict[str, int] = {}
    forced: set[str] = set()
    first_diff = ""
    number = 0
    while True:
        gaps = _gaps(layer, adw, pack)
        history.append({"round": number, "gaps": gaps, "tree": pack.get("tree", "")})
        state.save(layer.layer_dir)
        if not gaps:
            break
        lines = "; ".join(f"{gap['id']} {gap['state']}" + (f" ({gap['note']})" if gap.get("note") else "")
                          for gap in gaps)
        hard = [gap for gap in gaps if gap["kind"] != "fixable"]
        if hard:
            kind = hard[0]["kind"]
            party = KIND_PARTY.get(kind, "person")
            _stop(layer, state, kind, party, f"evidence:{kind}: "
                  + "; ".join(f"{gap['id']} {gap['state']}: {gap['criterion']}" for gap in hard)
                  + f"; party: {party}")
        ran = _force_stale(layer, state, adw, stack, pack, forced)
        if ran:
            history[-1]["forced"] = ran
            _collect(layer, state, adw)
            pack = build_pack(layer, state, adw, stack)
            continue
        if not can_patch:
            _stop(layer, state, "fixable", "person",
                  f"evidence:fixable: {lines}; the review step has no #:patch/#:implement; party: person")
        for gap in gaps:
            seen[gap["fingerprint"]] = seen.get(gap["fingerprint"], 0) + 1
        if number >= MAX_PATCHES:
            survivor = next((gap for gap in gaps if seen.get(gap["fingerprint"], 0) > MAX_PATCHES), gaps[0])
            _stop(layer, state, "no-progress", "person",
                  f"evidence:no-progress: {survivor['id']} still {survivor['state']} after {MAX_PATCHES} patches; "
                  "party: person")
        number += 1
        before = _tree(state, stack)
        repeat = [gap for gap in gaps if seen.get(gap["fingerprint"], 0) > 1]
        done = _patch(layer, state, adw, step, stack, number, gaps, first_diff if repeat else "")
        after = _tree(state, stack)
        if number == 1:
            first_diff = _diff(state, stack, before, after)
        history[-1]["patch"] = {"number": number, **done, "different_approach": bool(repeat)}
        _collect(layer, state, adw)
        pack = build_pack(layer, state, adw, stack)
    from . import convergence

    convergence.record_summary(state)
    artefacts.write_manifest(layer, state)
    state.save(layer.layer_dir)
    return pack
