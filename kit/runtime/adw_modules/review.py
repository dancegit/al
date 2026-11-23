from __future__ import annotations

import contextlib
import fnmatch
import posixpath
import subprocess
from pathlib import Path
from typing import Any

from . import agents, artefacts, events, gitsafe, reviewpolicy, steps
from .errors import LayerError, StepFailed

GIT_TIMEOUT = 120
HUMAN_VERDICT = {"success": True, "summary": "awaiting human review", "issues": [], "mode": "human"}


# ---------------------------------------------------------------- review rounds

def combine_verdicts(verdicts: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    """Passes only when every reviewer succeeded and nobody raised a blocker."""
    issues = [issue for verdict in verdicts for issue in verdict["issues"]]
    blockers = [issue for issue in issues if issue["severity"] == "blocker"]
    return {
        "success": bool(verdicts) and all(verdict["success"] for verdict in verdicts) and not blockers,
        "summary": " | ".join(verdict["summary"] for verdict in verdicts),
        "issues": issues,
        "mode": mode,
    }


def _model_set(layer: Any, command_name: str) -> str:
    return str(layer.command(command_name).get("modelSet") or "base")


def _run_reviewers(layer: Any, state: Any, option: dict[str, Any], round_number: int,
                   env_stack: Any) -> list[dict[str, Any]]:
    command = str(option["command"])
    manifest = str(artefacts.manifest_path(layer, state.adw_id))
    # `#:agent` on the review option: the reviewers run on that agent (another vendor or model)
    on = {"agent_id": str(option["agent"])} if option.get("agent") else {}
    verdicts = []
    for index in range(1, max(1, int(option.get("reviewers", 1))) + 1):
        name = f"review-{index}" if round_number == 1 else f"review-{round_number}-{index}"
        raw = agents.run_agent(layer, state, name, command, [state.plan or "", manifest],
                               _model_set(layer, command), env_stack, **on)
        verdict = agents.extract_json(raw, name)
        agents.validate_review_verdict(verdict, name)  # also enforced for non-`review` templates
        state.outputs[name] = raw
        verdicts.append(verdict)
    return verdicts


def _checks_that_ran(state: Any, adw: dict[str, Any]) -> list[dict[str, Any]]:
    ran = {record["name"] for record in state.steps if record.get("kind") == "check"
           and record.get("status") in {"passed", "failed"}}
    return [step for step in adw.get("steps", []) if step.get("kind") == "check" and step.get("name") in ran]


def _patch_round(layer: Any, state: Any, step: dict[str, Any], blockers: list[dict[str, Any]],
                 round_number: int, env_stack: Any) -> dict[str, Any]:
    """Plan a patch for the blockers, implement it and re-run every check that already ran."""
    request = "\n".join(f"- {issue['description']}\n  Resolution: {issue['resolution']}" for issue in blockers)
    patch_step, implement_step = f"patch-{round_number}", f"implement-{round_number}"
    patch_plan = agents.run_agent(layer, state, patch_step, str(step["patch"]), [request, state.plan or ""],
                                  _model_set(layer, str(step["patch"])), env_stack)
    state.outputs[patch_step] = patch_plan
    implemented = agents.run_agent(layer, state, implement_step, str(step["implement"]), [patch_plan],
                                   _model_set(layer, str(step["implement"])), env_stack)
    state.outputs[implement_step] = implemented
    rerun = []
    for check in _checks_that_ran(state, layer.adw(state.adw)):
        print(f"[adw {state.adw_id}] review round {round_number}: re-running check {check['name']}")
        steps.run_check_with_resolve(layer, state, check, env_stack)
        rerun.append(check["name"])
    return {"patch_plan": patch_plan, "implement_output": implemented[:2000], "checks_rerun": rerun}


def _has_later_ship(adw: dict[str, Any], step_name: str) -> bool:
    names = [step.get("name") for step in adw.get("steps", [])]
    later = adw.get("steps", [])[names.index(step_name) + 1:] if step_name in names else []
    return any(step.get("kind") == "ship" for step in later)


def run_review(layer: Any, state: Any, step: dict[str, Any], env_stack: Any) -> dict[str, Any]:
    """Run the ADW's review option. A failed verdict raises unless a later ship step will route it to a PR."""
    adw = layer.adw(state.adw)
    option = layer.option("reviews", adw["review"])
    kind = option["kind"]
    if kind == "human":
        state.review = {"verdict": dict(HUMAN_VERDICT), "rounds": [], "mode": kind}
        return state.review["verdict"]
    if kind not in {"agent", "auto"}:
        raise LayerError(f"Unknown review kind: {kind!r}")
    step_name = str(step["name"])
    artefacts.collect(layer, state, list(option.get("artefacts", [])), step_name)
    max_retries = max(0, int(option.get("maxRetries", 0)))
    can_patch = bool(step.get("patch")) and bool(step.get("implement"))
    rounds: list[dict[str, Any]] = []
    round_number = 1
    while True:
        verdicts = _run_reviewers(layer, state, option, round_number, env_stack)
        verdict = combine_verdicts(verdicts, kind)
        rounds.append({"round": round_number, "reviewers": verdicts, "verdict": verdict})
        state.review = {"verdict": verdict, "rounds": rounds, "mode": kind}
        if state.worktree:
            state.review["tree"] = tree_fingerprint(state.worktree, getattr(env_stack, "sandbox", None))
        state.save(layer.layer_dir)
        events.emit("review.round", step=step_name, round=round_number, success=verdict["success"],
                    reviewers=len(verdicts), summary=str(verdict["summary"])[:500],
                    issues={severity: sum(1 for issue in verdict["issues"] if issue["severity"] == severity)
                            for severity in agents.SEVERITIES})
        blockers = [issue for issue in verdict["issues"] if issue["severity"] == "blocker"]
        if verdict["success"] or not blockers or not can_patch or round_number > max_retries:
            break
        rounds[-1]["patch"] = _patch_round(layer, state, step, blockers, round_number, env_stack)
        artefacts.collect(layer, state, list(option.get("artefacts", [])), step_name)
        round_number += 1
    print(f"[adw {state.adw_id}] review verdict: {'pass' if verdict['success'] else 'fail'} "
          f"after {len(rounds)} round(s): {verdict['summary'][:200]}")
    if not verdict["success"] and not _has_later_ship(adw, step_name):
        raise StepFailed(step_name, f"review failed: {verdict['summary'][:500]}")
    return verdict


# ---------------------------------------------------------------- gate

LAYER_CHANGED = "layer-changed: this run changed the agentic layer itself; layer changes are made with `al improve` and always pass a person"
LAYER_CHANGE_NEEDS_A_PERSON = "layer-change: a change to how agents work always passes a person"


def layer_reasons(facts: dict[str, Any]) -> list[str]:
    """Implicit protection of an in-repo layer: `protected-path:` for every changed file under the
    layer directory, whatever the review option says (block-protected-paths cannot turn it off)."""
    hits = [path for path in facts.get("changed_files", []) if is_protected(path, facts.get("layer_protected") or [])]
    return [f"protected-path:{path}" for path in hits] + ([LAYER_CHANGED] if hits else [])


def decide(option: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """Pure ship decision. `ship` is True only when the change ships without a human."""
    kind = option.get("kind")
    implicit = layer_reasons(facts)
    if kind in {"human", "agent"}:
        return {"action": "pr", "ship": False, "reasons": [f"review-{kind}: a human merges", *implicit]}
    if kind != "auto":
        raise LayerError(f"Unknown review kind: {kind!r}")
    reasons: list[str] = []
    if facts.get("changes") == "layer":  # the compiler refuses this; the runtime refuses it again
        reasons.append(LAYER_CHANGE_NEEDS_A_PERSON)
    reasons += [f"check-failed:{name}" for name, code in facts["check_exits"].items() if code != 0]
    reasons += [f"require-check-failed:{name}" for name, code in facts["require_exits"].items() if code != 0]
    verdict = facts.get("verdict") or {}
    blocked = any(issue.get("severity") == "blocker" for issue in verdict.get("issues", []))
    if verdict.get("success") is not True or blocked:
        reasons.append("review-failed")
    if facts["klass"] not in option.get("allowedClasses", []):
        reasons.append(f"class-not-allowed:{facts['klass']}")
    limit = int(option.get("maxDiffLines", 0))
    if int(facts["diff_lines"]) > limit:
        reasons.append(f"diff-too-large:{facts['diff_lines']}>{limit}")
    if option.get("blockProtectedPaths"):
        reasons += [f"protected-path:{path}" for path in facts["changed_files"]
                    if is_protected(path, facts["protected"])]
    reasons += [reason for reason in implicit if reason not in reasons]
    if reasons:
        return {"action": "pr", "ship": False, "reasons": reasons}
    return {"action": str(option.get("ship") or "pr"), "ship": True, "reasons": []}


def _normalise_protected(entry: str) -> str:
    """Repo-relative form of a protected entry; "" means the whole repository."""
    text = entry.strip()
    if text in {"", ".", "/", "./"}:
        return ""
    text = posixpath.normpath(text).lstrip("/")
    return "" if text == "." else text


def is_protected(path: str, protected: list[str]) -> bool:
    """True when `path` equals or lies under a protected entry. Entries may be written as
    `dir`, `dir/`, `./dir`, `/dir` or as a glob (`*.lock`, `ci/**`); matching is case-insensitive
    so `.GitHub/` cannot dodge `.github`."""
    target = posixpath.normpath(path).lower()
    parts = target.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    for entry in protected:
        root = _normalise_protected(entry).lower()
        if root == "":
            return True
        if any(ch in root for ch in "*?["):
            pattern = root[:-3] if root.endswith("/**") else root
            if any(fnmatch.fnmatchcase(prefix, pattern) for prefix in prefixes) or fnmatch.fnmatchcase(parts[-1], pattern):
                return True
        elif root in prefixes:
            return True
    return False


def parse_numstat(text: str) -> tuple[int, list[str]]:
    """(added+deleted lines, files) from `git diff --numstat -z --no-renames`; binary '-' counts 0.

    NUL-separated records carry raw paths: without -z git quotes and escapes any path with
    non-ASCII or special characters, which would hide it from protected-path matching."""
    total, files = 0, []
    rows = text.split("\0") if "\0" in text else text.splitlines()
    for row in rows:
        if not row.strip():
            continue
        fields = row.split("\t", 2)
        if len(fields) != 3:
            raise ValueError(f"unexpected git numstat row: {row!r}")
        total += sum(0 if value == "-" else int(value) for value in fields[:2])
        files.append(fields[2])
    return total, files


def _git_stdout(worktree: str, args: list[str], sandbox: gitsafe.Sandbox | None = None) -> str:
    if sandbox is not None:
        return gitsafe.run(sandbox, args, "gate", timeout=GIT_TIMEOUT).stdout
    try:
        result = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True,
                                timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed("gate", f"git {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        raise StepFailed("gate", f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _rerun_required(layer: Any, state: Any, option: dict[str, Any], env_stack: Any) -> dict[str, int]:
    exits: dict[str, int] = {}
    log_dir = Path(layer.layer_dir) / "agents" / state.adw_id / "checks"
    log_dir.mkdir(parents=True, exist_ok=True)
    for name in option.get("requireChecks", []):
        command = layer.cmd(name)
        code = steps.run_logged(layer, state, env_stack, command, log_dir / f"gate-{name}.log", "gate", "gate")
        print(f"[adw {state.adw_id}] gate: {name} exit {code}")
        exits[name] = code
    return exits


def tree_fingerprint(worktree: str, sandbox: gitsafe.Sandbox | None = None) -> str:
    """Tree hash of everything currently in the worktree (staged via `add -A`, nothing committed)."""
    with gitsafe.session(sandbox) if sandbox is not None else contextlib.nullcontext():
        _git_stdout(worktree, ["add", "-A"], sandbox)
        return _git_stdout(worktree, ["write-tree"], sandbox).strip()


def gather_facts(layer: Any, state: Any, option: dict[str, Any], env_stack: Any, base: str | None = None,
                 head: str | None = None) -> dict[str, Any]:
    """Facts for `decide`, taken from the tree that will actually ship: `base` is the ref the
    result lands on (already merged into HEAD by ship), so the diff is exactly what shipping adds.

    `head` (sandboxed runs) is the exact commit ship will publish, already imported into the
    app's object store: the diff is then computed in the app repository, from objects the
    container cannot alter, and nothing uncommitted can ship so there is no pending diff."""
    if not state.worktree:
        raise StepFailed("gate", "the gate needs a worktree")
    sandbox = getattr(env_stack, "sandbox", None)
    if head is not None and sandbox is not None:
        target = base or f"refs/heads/{layer.default_branch}"
        committed_lines, committed = parse_numstat(gitsafe.run_app(
            sandbox, ["diff", "--numstat", "-z", "--no-renames", f"{target}...{head}"], "gate").stdout)
        pending_lines, pending = 0, []
    else:
        committed_lines, committed = parse_numstat(
            _git_stdout(state.worktree, ["diff", "--numstat", "-z", "--no-renames", f"{base or layer.default_branch}...HEAD"]))
        pending_lines, pending = parse_numstat(
            _git_stdout(state.worktree, ["diff", "--numstat", "-z", "--no-renames", "HEAD"]))
    check_exits = {name: int(record["exit"]) for name, record in state.checks.items()}
    for step in layer.adw(state.adw)["steps"]:
        # a declared check with no recorded result never ran: that is not a pass
        if step["kind"] == "check" and step["name"] not in check_exits:
            check_exits[step["name"]] = -1
    adw = layer.adw(state.adw)
    changes = str(adw.get("changes") or "codebase")
    in_repo = getattr(layer, "in_repo_dir", "")
    return {
        "changes": changes,
        # an in-repo layer is protected from every ADW that is not a layer-change workflow
        "layer_protected": [in_repo] if in_repo and changes != "layer" else [],
        "check_exits": check_exits,
        "require_exits": _rerun_required(layer, state, option, env_stack) if option["kind"] == "auto" else {},
        "verdict": state.review.get("verdict", {}),
        "klass": state.klass,
        "diff_lines": committed_lines + pending_lines,
        "changed_files": sorted(set(committed + pending)),
        "protected": list(layer.ir["codebase"].get("paths", {}).get("protected", [])),
    }


READS_LOCAL_LIMIT = True   # the gate applies agents/settings/review.json (reviewpolicy.py)


def gate(layer: Any, state: Any, env_stack: Any, base: str | None = None,
         extra_reasons: list[str] | None = None, head: str | None = None) -> dict[str, Any]:
    option = layer.option("reviews", layer.adw(state.adw)["review"])
    option, limit = reviewpolicy.apply(layer.layer_dir, option)   # the local ship-without-review limit, else the declared one
    facts = gather_facts(layer, state, option, env_stack, base, head)
    decision = decide(option, facts)
    if extra_reasons and option["kind"] == "auto":
        decision = {"action": "pr", "ship": False, "reasons": [*decision["reasons"], *extra_reasons]}
    state.ship["gate"] = {**decision, "diff_lines": facts["diff_lines"], "changed_files": facts["changed_files"],
                          **({"limit": limit} if limit else {})}
    if limit:
        events.say(f"[adw {state.adw_id}] {reviewpolicy.gate_line(limit)}", "gate.limit", **limit)
    events.say(f"[adw {state.adw_id}] gate: action {decision['action']}"
               + (f" (fallback: {', '.join(decision['reasons'])})" if decision["reasons"] and option["kind"] == "auto" else ""),
               "gate.decision", action=decision["action"], ship=bool(decision.get("ship")),
               reasons=list(decision["reasons"]))
    return decision
