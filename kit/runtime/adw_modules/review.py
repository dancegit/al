from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import agents, artefacts, steps
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
    verdicts = []
    for index in range(1, max(1, int(option.get("reviewers", 1))) + 1):
        name = f"review-{index}" if round_number == 1 else f"review-{round_number}-{index}"
        raw = agents.run_agent(layer, state, name, command, [state.plan or "", manifest],
                               _model_set(layer, command), env_stack)
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
        state.save(layer.layer_dir)
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

def decide(option: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """Pure ship decision. `ship` is True only when the change ships without a human."""
    kind = option.get("kind")
    if kind in {"human", "agent"}:
        return {"action": "pr", "ship": False, "reasons": [f"review-{kind}: a human merges"]}
    if kind != "auto":
        raise LayerError(f"Unknown review kind: {kind!r}")
    reasons: list[str] = []
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
    if reasons:
        return {"action": "pr", "ship": False, "reasons": reasons}
    return {"action": str(option.get("ship") or "pr"), "ship": True, "reasons": []}


def is_protected(path: str, protected: list[str]) -> bool:
    for entry in protected:
        root = entry.strip().rstrip("/")
        if root and (path == root or path.startswith(root + "/")):
            return True
    return False


def parse_numstat(text: str) -> tuple[int, list[str]]:
    """(added+deleted lines, files) from `git diff --numstat --no-renames`; binary '-' counts 0."""
    total, files = 0, []
    for row in text.splitlines():
        if not row.strip():
            continue
        fields = row.split("\t", 2)
        if len(fields) != 3:
            raise ValueError(f"unexpected git numstat row: {row!r}")
        total += sum(0 if value == "-" else int(value) for value in fields[:2])
        files.append(fields[2])
    return total, files


def _git_stdout(worktree: str, args: list[str]) -> str:
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
        code, output = steps.run_command(env_stack.wrap(str(command["run"])), env_stack.cwd,
                                         env_stack.process_env(), int(command.get("timeout", 900)))
        (log_dir / f"gate-{name}.log").write_text(output, encoding="utf-8")
        print(f"[adw {state.adw_id}] gate: {name} exit {code}")
        exits[name] = code
    return exits


def gather_facts(layer: Any, state: Any, option: dict[str, Any], env_stack: Any) -> dict[str, Any]:
    """Facts for `decide`. Counts committed (<default>...HEAD) plus uncommitted tracked changes."""
    if not state.worktree:
        raise StepFailed("gate", "the gate needs a worktree")
    committed_lines, committed = parse_numstat(
        _git_stdout(state.worktree, ["diff", "--numstat", "--no-renames", f"{layer.default_branch}...HEAD"]))
    pending_lines, pending = parse_numstat(
        _git_stdout(state.worktree, ["diff", "--numstat", "--no-renames", "HEAD"]))
    return {
        "check_exits": {name: int(record["exit"]) for name, record in state.checks.items()},
        "require_exits": _rerun_required(layer, state, option, env_stack) if option["kind"] == "auto" else {},
        "verdict": state.review.get("verdict", {}),
        "klass": state.klass,
        "diff_lines": committed_lines + pending_lines,
        "changed_files": sorted(set(committed + pending)),
        "protected": list(layer.ir["codebase"].get("paths", {}).get("protected", [])),
    }


def gate(layer: Any, state: Any, env_stack: Any) -> dict[str, Any]:
    option = layer.option("reviews", layer.adw(state.adw)["review"])
    facts = gather_facts(layer, state, option, env_stack)
    decision = decide(option, facts)
    state.ship["gate"] = {**decision, "diff_lines": facts["diff_lines"], "changed_files": facts["changed_files"]}
    print(f"[adw {state.adw_id}] gate: action {decision['action']}"
          + (f" (fallback: {', '.join(decision['reasons'])})" if decision["reasons"] and option["kind"] == "auto" else ""))
    return decision
