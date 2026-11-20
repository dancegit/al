"""Review rounds that must make progress: blocker fingerprints, round comparison, debt, the
reviewers' context (evidence pack, approvals, round scope) and the per-run numbers for `al kpis`.

A blocker gets a stable fingerprint `ANCHOR|KIND`: the anchor is the criterion it names (`C2`),
else the first file in its description (line number dropped), else a hash of its first words;
the kind is the reviewer's `kind`, else `classify`. After a review (`after_review`, never raises)
the runtime records per round what was resolved, what is still open and what is new, how long
each round took, why the rounds stopped (or would have stopped: `after_round`), and files debt:
`tech_debt` issues and new non-safety blockers of round two on become tasks linked to the run.

`after_round` and `patch_request` are the in-loop decision for review.py (protected; the hooks a
person adds are described in the plan): stop when a blocker needs an environment, a declaration
or a person; ask once for a different approach when a fixable blocker survives a patch; stop when
it survives that too.

Only a run with a Proof plan table or an approval is touched: every other run is as before.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import approvals, events, state as state_module, storage

KINDS = ("fixable", "needs-environment", "needs-declaration", "needs-person")
PARTIES = ("agent", "layer author", "operator", "approver", "person")
SAFETY = ("secret", "credential", "personal path", "protected path", "data loss", "security", "injection")
KIND_PARTY = {"needs-environment": "operator", "needs-declaration": "layer author", "needs-person": "approver",
              "fixable": "agent"}
KEYWORDS = (
    ("needs-person", ("approval", "approve", "protected path", "vocabulary", "a person", "human decision",
                      "requires human", "only a person")),
    ("needs-declaration", ("declare", "declaration", "artefact glob", "#:", "max-files", "layer change",
                           "agentic.rkt", "not handed to the reviewers")),
    ("needs-environment", ("not installed", "no podman", "no chrome", "no network", "unavailable",
                           "cannot run here", "missing tool", "environment lacks")),
)
ROUND_RULE = ("From round 2 on, check only that each earlier blocker is resolved and that the patch broke "
              "nothing. A new finding outside the patch is tech_debt unless it is on the safety list.")
DEBT_PREFIX = "debt from run "
CONTEXT_DIR = "review-context"
_CRITERION = re.compile(r"^C\d+$")
_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8})(?::\d+(?::\d+)?)?")


# ---------------------------------------------------------------- fingerprints

def classify(issue: dict[str, Any]) -> str:
    text = f"{issue.get('description', '')} {issue.get('resolution', '')}".lower()
    for kind, words in KEYWORDS:
        if any(word in text for word in words):
            return kind
    return "fixable"


def kind_of(issue: dict[str, Any]) -> str:
    kind = str(issue.get("kind") or "")
    return kind if kind in KINDS else classify(issue)


def anchor(issue: dict[str, Any]) -> str:
    criterion = str(issue.get("criterion") or "").strip()
    if _CRITERION.fullmatch(criterion):
        return criterion
    match = _PATH.search(str(issue.get("description") or ""))
    if match and not re.fullmatch(r"\d+(\.\d+)+", match.group(1)):
        return match.group(1)
    words = re.sub(r"[^a-z0-9 ]+", " ", str(issue.get("description") or "").lower()).split()[:12]
    return "text:" + hashlib.sha1(" ".join(words).encode("utf-8")).hexdigest()[:12]


def fingerprint(issue: dict[str, Any]) -> str:
    return f"{anchor(issue)}|{kind_of(issue)}"


def blockers(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    return [issue for issue in (verdict or {}).get("issues") or [] if issue.get("severity") == "blocker"]


def compare(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, list[str]]:
    """{resolved, open, new} fingerprints between two rounds' blockers."""
    before = list(dict.fromkeys(fingerprint(issue) for issue in previous))
    after = list(dict.fromkeys(fingerprint(issue) for issue in current))
    return {"resolved": [fp for fp in before if fp not in after], "open": [fp for fp in after if fp in before],
            "new": [fp for fp in after if fp not in before]}


def is_safety(issue: dict[str, Any]) -> bool:
    text = f"{issue.get('description', '')} {issue.get('resolution', '')}".lower()
    return any(word in text for word in SAFETY)


def _covered(state: Any, issue: dict[str, Any]) -> bool:
    """A needs-person blocker an approval already covers (its file, its criterion's approvals, or
    an approval it names)."""
    if not getattr(state, "approvals", None):
        return False
    point = anchor(issue)
    if not point.startswith("text:") and not _CRITERION.fullmatch(point) and approvals.covered(state, f"edit:{point}"):
        return True
    if _CRITERION.fullmatch(point):
        rows = [row for row in (state.proof_plan or {}).get("rows") or [] if row.get("id") == point]
        needed = [need[len("approval:"):] for row in rows for need in row.get("needs") or []
                  if str(need).startswith("approval:")]
        if needed and all(approvals.covered(state, need) for need in needed):
            return True
    text = str(issue.get("description") or "")
    return any(str(item.get("what")) in text for item in state.approvals)


# ---------------------------------------------------------------- the in-loop decision

def after_round(layer: Any, state: Any, rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """What review.py's loop does after a round: {action: continue | different-approach | stop,
    reason, party, request}."""
    del layer
    if not rounds:
        return {"action": "continue", "reason": "", "party": "", "request": ""}
    current = blockers(rounds[-1].get("verdict") or {})
    for issue in current:
        kind = kind_of(issue)
        if kind == "needs-person" and _covered(state, issue):
            continue
        if kind != "fixable":
            return {"action": "stop", "reason": f"{kind}: {fingerprint(issue)}: {issue.get('description', '')}"[:500],
                    "party": KIND_PARTY[kind], "request": ""}
    if len(rounds) < 2:
        return {"action": "continue", "reason": "", "party": "", "request": ""}
    progress = compare(blockers(rounds[-2].get("verdict") or {}), current)
    still = [issue for issue in current if fingerprint(issue) in progress["open"]]
    if not still:
        return {"action": "continue", "reason": "", "party": "", "request": ""}
    asked = any((item.get("patch") or {}).get("different_approach") or item.get("different_approach")
                for item in rounds[:-1])
    if not asked:
        first_diff = str((rounds[-2].get("patch") or {}).get("diff") or "")
        return {"action": "different-approach", "party": "agent",
                "reason": f"{len(still)} blocker(s) still open after a patch: {', '.join(progress['open'])}",
                "request": patch_request(still, first_diff=first_diff, different=True)}
    return {"action": "stop", "party": "person",
            "reason": f"no-progress: {', '.join(progress['open'])} still open after a different approach"}


def patch_request(blockers_: list[dict[str, Any]], first_diff: str = "", different: bool | None = None) -> str:
    """The request review._patch_round builds; with a first attempt (its diff), the request for a
    different approach."""
    request = "\n".join(f"- {issue['description']}\n  Resolution: {issue['resolution']}" for issue in blockers_)
    if different or (different is None and first_diff):
        request += "\n\nA first attempt did not resolve this; take a different approach. Its diff:\n"
        request += f"\n```diff\n{first_diff.rstrip()}\n```\n" if first_diff.strip() else "\n(the first attempt changed nothing)\n"
    return request


def is_debt(state: Any, issue: dict[str, Any], round_number: int | None = None) -> bool:
    """A blocker the review should file as debt instead of blocking: new in round two or later
    (no earlier round raised it) and not on the safety list."""
    rounds = list((getattr(state, "review", None) or {}).get("rounds") or [])
    number = round_number if round_number is not None else len(rounds) + 1
    if number < 2 or is_safety(issue):
        return False
    earlier = {fingerprint(item) for r in rounds[:number - 1] for item in blockers(r.get("verdict") or {})}
    return fingerprint(issue) not in earlier


# ---------------------------------------------------------------- the reviewers' context

def involved(state: Any) -> bool:
    plan = getattr(state, "proof_plan", None) or {}
    return plan.get("table") in ("parsed", "invalid") or bool(getattr(state, "approvals", None))


def _context_dir(layer: Any, state: Any) -> Path:
    return storage.artefacts_root(layer) / state.adw_id / CONTEXT_DIR


def _relative(layer: Any, path: Path) -> str:
    try:
        return path.relative_to(layer.layer_dir).as_posix()
    except ValueError:
        return str(path)


def manifest_entries(layer: Any, state: Any) -> list[dict[str, Any]]:
    """The `evidence`, `approvals` and `round` entries of the reviewers' manifest, each written as a
    file under artefacts/<id>/review-context/; [] for a run without a table or an approval."""
    if not involved(state):
        return []
    folder = _context_dir(layer, state)
    folder.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    plan = state.proof_plan or {}
    if plan.get("table") == "parsed":
        from . import evidence

        pack = plan.get("pack") or {}
        path = folder / "evidence.md"
        if pack:
            path.write_text(evidence.render_pack(pack), encoding="utf-8")
        else:
            path.write_text("# Evidence pack\n\nNot built yet: every criterion is only planned.\n", encoding="utf-8")
        rows = pack.get("rows") or [{"id": row.get("id"), "criterion": row.get("criterion"),
                                     "command": row.get("command"), "state": row.get("state", "planned"),
                                     "files": []} for row in plan.get("rows") or []]
        entries.append({"id": "evidence", "type": "evidence", "step": "evidence", "path": _relative(layer, path),
                        "tree": pack.get("tree", ""),
                        "table": [{"id": row.get("id"), "criterion": row.get("criterion"),
                                   "command": row.get("command"), "state": row.get("state"),
                                   "files": list(row.get("files") or [])} for row in rows],
                        "states": {str(row.get("id")): row.get("state") for row in rows}})
    given = [dict(item) for item in state.approvals]
    path = folder / "approvals.md"
    path.write_text("# Approvals for this run\n\n" + ("\n".join(
        f"- `{item.get('what')}` by {item.get('who')} at {item.get('at')} ({item.get('via')})" for item in given)
        or "None: a change that needs a person's decision is one blocker of kind needs-person.") + "\n",
        encoding="utf-8")
    entries.append({"id": "approvals", "type": "approvals", "step": "evidence", "path": _relative(layer, path),
                    "approvals": given})
    rounds = list((state.review or {}).get("rounds") or [])
    number = len(rounds) + 1
    earlier = [{"fingerprint": fingerprint(issue), "description": issue.get("description", ""),
                "resolution": issue.get("resolution", "")}
               for issue in (blockers(rounds[-1].get("verdict") or {}) if rounds else [])]
    patch = (rounds[-1].get("patch") or {}) if rounds else {}
    patch_plan = str(patch.get("patch_plan") or "").strip()
    patch_plan = patch_plan.splitlines()[-1].strip() if patch_plan else ""
    path = folder / "round.md"
    lines = [f"# Review round {number}", "", ROUND_RULE if number > 1 else "Round 1: review the whole change.", ""]
    if earlier:
        lines += ["## Earlier blockers", ""] + [f"- `{item['fingerprint']}`: {item['description']} — "
                                               f"{item['resolution']}" for item in earlier] + [""]
    if patch_plan:
        lines += [f"Patch plan: {patch_plan}", ""]
    lines += ["## Safety list (always a blocker)", "", *[f"- {word}" for word in SAFETY]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    entries.append({"id": "round", "type": "round", "step": "review", "path": _relative(layer, path),
                    "round": number, "earlier_blockers": earlier, "patch_plan": patch_plan,
                    "safety": list(SAFETY), "rule": ROUND_RULE if number > 1 else "review the whole change"})
    return entries


# ---------------------------------------------------------------- after the review

def _stamp(text: str) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def round_seconds(layer: Any, state: Any, step_name: str) -> list[float]:
    """Seconds of each review round, from the run's `review.round` events since the review step started."""
    record = next((r for r in reversed(state.steps) if r.get("name") == step_name), {})
    start = _stamp(record.get("started") or "")
    times = [_stamp(event.get("ts", "")) for event in events.read(events.path_for(layer.layer_dir, state.adw_id))
             if event.get("type") == "review.round" and event.get("step") == step_name]
    times = [t for t in times if t is not None and (start is None or t >= start)]
    out, previous = [], start
    for when in times:
        out.append(round(max(0.0, when - previous), 3) if previous is not None else 0.0)
        previous = when
    return out


def _progress(rounds: list[dict[str, Any]], seconds: list[float]) -> list[dict[str, Any]]:
    out = []
    for index, item in enumerate(rounds):
        verdict = item.get("verdict") or {}
        now = blockers(verdict)
        moved = compare(blockers(rounds[index - 1].get("verdict") or {}) if index else [], now)
        out.append({"round": int(item.get("round") or index + 1), "success": bool(verdict.get("success")),
                    "blockers": [fingerprint(issue) for issue in now], "resolved": moved["resolved"],
                    "open": moved["open"], "new": moved["new"],
                    "seconds": seconds[index] if index < len(seconds) else 0.0})
    return out


def _tasklist_option(layer: Any, state: Any) -> dict[str, Any] | None:
    try:
        wanted = list((state.definition or layer.adw(state.adw)).get("input") or [])
    except Exception:  # noqa: BLE001 — a run of something the layer does not declare has no task list
        wanted = []
    for option in layer.ir.get("promptInputs") or []:
        if isinstance(option, dict) and option.get("kind") == "tasklist" and option.get("id") in wanted:
            return option
    return None


_DEBT_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.[A-Za-z0-9]+)(?::\d+)?")


def _debt_checks(layer: Any, state: Any, issue: dict[str, Any]) -> list[str]:
    """The check steps of the run's workflow that cover the files a debt issue names, in step order."""
    try:
        from . import affected

        adw = state.definition or layer.adw(state.adw)
        paths = list(dict.fromkeys(_DEBT_PATH.findall(str(issue.get("description") or ""))))
        if not paths:
            return []
        chosen = affected.select(affected.workflow_checks(layer, adw), paths)["run"]
        names = [str(step.get("name")) for step in adw.get("steps") or []
                 if step.get("kind") == "check" and str(step.get("cmd")) in chosen]
        return list(dict.fromkeys(names))
    except Exception:  # noqa: BLE001 — a filed debt must never fail the review
        return []


def _file_debt(layer: Any, state: Any, text: str) -> str:
    """Put one debt task where the layer takes work: its task list, else its forge, else debt.md."""
    from . import tasklist

    option = _tasklist_option(layer, state)
    if option is not None:
        path = tasklist.path_for(layer.layer_dir, option)
        line = tasklist.append(path, "Debt", text, tags=(), option=option)
        return f"tasklist:{_relative(layer, path)}:{line}"
    if str((layer.ir.get("forge") or {}).get("kind") or "none") != "none" and not state.dry_run:
        from . import forge

        created = forge.get_client(layer).create_issue(text[:80], text, [])
        return f"issue:{created.get('number') or created.get('url') or '?'}"
    path = state_module.run_dir(layer.layer_dir, state.adw_id) / "debt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        if handle.tell() == 0:
            handle.write(f"# Debt from run {state.adw_id}\n\n")
        handle.write(f"- [ ] {text}\n")
    return f"file:{_relative(layer, path)}"


def debt_items(state: Any, rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tech_debt issues of every round and the new blockers of round two on that are not on the safety list."""
    found: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, item in enumerate(rounds):
        verdict = item.get("verdict") or {}
        for issue in verdict.get("issues") or []:
            fp = fingerprint(issue)
            if issue.get("severity") == "tech_debt":
                found.setdefault(fp, issue)
            elif issue.get("severity") == "blocker" and index >= 1 and fp not in seen and not is_safety(issue):
                found.setdefault(fp, issue)
        seen |= {fingerprint(issue) for issue in blockers(verdict)}
    return list(found.values())


def summarise(state: Any) -> dict[str, Any]:
    """The per-run numbers `al kpis` and the dashboard sum (state.proof_plan["summary"])."""
    plan = state.proof_plan or {}
    progress = list(plan.get("rounds") or [])
    seen: dict[str, int] = {}
    for item in progress:
        for fp in item.get("blockers") or []:
            seen[fp] = seen.get(fp, 0) + 1
    repeated = [{"fingerprint": fp, "cause": fp.rsplit("|", 1)[-1]} for fp, count in seen.items() if count > 1]
    idle = sum(float(item.get("seconds") or 0) for index, item in enumerate(progress)
               if index and item.get("success") == progress[index - 1].get("success"))
    stopped = plan.get("stopped") or {}
    rows = list(plan.get("rows") or [])
    declarations = [str(row.get("fix")) for row in rows if row.get("outcome") == "needs-declaration" and row.get("fix")]
    if stopped.get("outcome") == "needs-declaration" and stopped.get("at") == "evidence":
        declarations.append(str(stopped.get("reason") or "").splitlines()[0])
    if stopped:
        why = f"{stopped.get('at')}:{stopped.get('outcome')}"
    elif plan.get("why_stopped"):
        why = str(plan["why_stopped"])
    else:
        why = ""
    return {"criteria": len(rows), "proven": sum(1 for row in rows if row.get("state") == "proven"),
            "rounds": len(progress), "repeated": repeated, "idle_seconds": round(idle, 3),
            "seconds": round(sum(float(item.get("seconds") or 0) for item in progress), 3),
            "stopped": why, "party": str(stopped.get("party") or ""),
            "would_have_stopped": plan.get("would_have_stopped") or 0,
            "evidence_patches": sum(1 for item in plan.get("gaps") or [] if item.get("patch")),
            "declarations": declarations, "debt": len(plan.get("debt") or [])}


def record_summary(state: Any) -> None:
    if involved(state):
        state.proof_plan["summary"] = summarise(state)


def after_review(layer: Any, state: Any, step: dict[str, Any]) -> None:
    """Record the rounds' progress, why they stopped, and file debt (see the module docstring).
    Never raises."""
    try:
        _after_review(layer, state, step)
    except Exception as exc:  # noqa: BLE001 — the record never fails a review
        print(f"[adw {state.adw_id}] convergence: not recorded: {type(exc).__name__}: {exc}")


def _after_review(layer: Any, state: Any, step: dict[str, Any]) -> None:
    if not involved(state):
        return
    review = state.review or {}
    rounds = list(review.get("rounds") or [])
    if not rounds:
        return
    name = str(step.get("name") or "review")
    plan = state.proof_plan
    plan["rounds"] = _progress(rounds, round_seconds(layer, state, name))
    would = 0
    for count in range(1, len(rounds) + 1):
        if after_round(layer, state, rounds[:count])["action"] == "stop":
            would = count
            break
    plan["would_have_stopped"] = would
    verdict = review.get("verdict") or {}
    try:
        option = layer.option("reviews", (state.definition or layer.adw(state.adw))["review"])
    except Exception:  # noqa: BLE001
        option = {}
    if verdict.get("success"):
        plan["why_stopped"] = "passed"
    elif would and would < len(rounds):
        plan["why_stopped"] = f"no-progress (would have stopped after round {would})"
    elif len(rounds) > int(option.get("maxRetries", 0) or 0) or not (step.get("patch") and step.get("implement")):
        plan["why_stopped"] = "max retries"
    else:
        plan["why_stopped"] = "failed"
    if verdict.get("success") and not str(state.prompt or "").lstrip().startswith(DEBT_PREFIX):
        filed = list(plan.get("debt") or [])
        known = {item.get("fingerprint") for item in filed}
        for issue in debt_items(state, rounds):
            fp = fingerprint(issue)
            if fp in known:
                continue
            text = f"{DEBT_PREFIX}{state.adw_id}: {issue.get('description', '')} — {issue.get('resolution', '')}"
            text = " ".join(text.split())
            names = _debt_checks(layer, state, issue)
            proof = (" Proof: prove it only with the check(s) " + ", ".join(f"`{name}`" for name in names)
                     + " of this layer, optionally narrowed to test ids; name no other command.") if names else ""
            try:
                where = _file_debt(layer, state, text + proof)
            except Exception as exc:  # noqa: BLE001 — a forge or a task list that fails keeps the item in state
                where = f"not filed: {type(exc).__name__}: {exc}"[:300]
            filed.append({"fingerprint": fp, "text": text, "where": where})
            known.add(fp)
        plan["debt"] = filed
    record_summary(state)
    events.emit("review.progress", rounds=[{k: item[k] for k in ("round", "resolved", "open", "new")}
                                           for item in plan["rounds"]], why=plan.get("why_stopped", ""))
    state.save(layer.layer_dir)
