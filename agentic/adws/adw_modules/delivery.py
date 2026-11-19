"""Where a run's work goes: stages, the cycle, the run's target branch.

A layer that declares stages (`(stage …)` / `(delivery PRESET …)`) has `stages`, `cycle` and
`delivery` in its IR. Each run then has a target: the stage it delivers into (its ADW's
`#:deliver-to`, else the first stage that accepts its class, else the final stage) and that
stage's concrete branch (placeholders filled from the open cycle). The runner gives every step a
run-scoped layer (`scope`) whose `default_branch` is that branch, so the worktree starts from it
and ship integrates, merges and opens its pull request against it, without any other module
knowing about stages. Before shipping, `before_ship` checks that the target still holds (class,
entry method, forge protection) and that the stage head merges cleanly; a conflict starts one
patch round against the stage head.

The current cycle is state of the layer, in agents/cycle.json, changed by `al cycle LAYER
status|start|close` (which imports this module). A layer without stages has no target: nothing
here runs and the run is exactly what it was before.

Standard library only.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import events, gitsafe, slots
from .errors import LayerError, StepFailed, StepWaiting

CYCLE_FILE = "agents/cycle.json"
CYCLE_LOCK = "agents/cycle.lock"
REASONS = ("no-open-cycle", "stage-missing", "forbidden-target", "class-moved", "entry-unsupported",
           "entry-forbidden", "conflict", "cycle-open", "cycle-not-open", "branch-exists", "source-missing")
CLASSES = ("chore", "bug", "feature", "patch")
FORGE_METHODS = ("merge", "squash", "rebase")
GIT_TIMEOUT = 300
IDENTITY = ["-c", "user.name=adw", "-c", "user.email=adw@localhost"]
CONFLICT_MARKER = "<<<<<<<"
OPEN_PROMOTIONS = "agents/promotions"   # one <from>-<to>.json per open promotion (see promotion.py)
FROZEN_EXIT = 75                         # a delivery into a frozen stage waits, like a waiting script step


class DeliveryError(LayerError):
    """A run cannot be delivered (or a cycle cannot change) for a classified reason (see REASONS)."""

    def __init__(self, reason: str, message: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown delivery reason {reason!r}")
        self.reason = reason
        self.message = message
        super().__init__(f"delivery:{reason}: {message}")


@dataclass(frozen=True)
class Stage:
    name: str
    branch: str
    from_: str = ""
    into: str = ""
    accepts: tuple[str, ...] = CLASSES
    agents_may_merge: bool = True
    entry: str = "merge"
    checks: tuple[str, ...] = ()
    deploy_cmd: str = ""
    environment_name: str = ""
    origin: str = "declared"
    frozen: tuple[str, ...] = ()
    frozen_while_promoting: bool = False

    @classmethod
    def from_ir(cls, data: Mapping[str, Any]) -> "Stage":
        return cls(name=str(data["name"]), branch=str(data["branch"]), from_=str(data.get("from") or ""),
                   into=str(data.get("into") or ""), accepts=tuple(data.get("accepts", CLASSES)),
                   agents_may_merge=bool(data.get("agentsMayMerge", True)), entry=str(data.get("entry") or "merge"),
                   checks=tuple(data.get("checks", ())), deploy_cmd=str(data.get("deployCmd") or ""),
                   environment_name=str(data.get("environmentName") or ""),
                   origin=str(data.get("origin") or "declared"), frozen=tuple(data.get("frozen") or ()),
                   frozen_while_promoting=bool(data.get("frozenWhilePromoting", False)))

    @property
    def next(self) -> str:
        """The stage this one is delivered into when accepted ("" for the final stage)."""
        return self.into or self.from_


@dataclass
class Target:
    adw: str
    stage: Stage
    branch: str
    source: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- routing

def stages(layer: Any) -> list[Stage]:
    return [Stage.from_ir(item) for item in (layer.ir.get("stages") or []) if isinstance(item, dict)]


def stage_named(layer: Any, name: str) -> Stage:
    for stage in stages(layer):
        if stage.name == name:
            return stage
    raise LayerError(f"no stage named {name!r} is declared")


def final_stage(all_stages: list[Stage]) -> Stage | None:
    return next((stage for stage in all_stages if not stage.next), None)


def route(layer: Any, adw: Mapping[str, Any], klass: str) -> Stage | None:
    """The stage a run of `adw` with class `klass` delivers into: #:deliver-to, else the first stage
    (in declaration order) that accepts the class, else the final stage. None without stages."""
    all_stages = stages(layer)
    if not all_stages:
        return None
    wanted = str(adw.get("deliverTo") or "")
    if wanted:
        return stage_named(layer, wanted)
    return next((stage for stage in all_stages if klass in stage.accepts), None) or final_stage(all_stages)


def cycle_option(layer: Any) -> dict[str, Any] | None:
    option = layer.ir.get("cycle")
    if isinstance(option, dict) and option.get("kind") not in (None, "none"):
        return option
    return None


def _utc(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def expand_branch(stage: Stage, cycle_state: Mapping[str, Any] | None, now: datetime | None = None) -> str:
    """The concrete branch of `stage`: {cycle} and {version} from the open cycle (only on the
    cycle's stage), {date} from the cycle's opening date on the cycle's stage, else today (UTC)."""
    current = (cycle_state or {}).get("current") if cycle_state else None
    on_cycle_stage = bool(cycle_state) and cycle_state.get("stage") == stage.name
    branch = stage.branch
    if "{cycle}" in branch or "{version}" in branch:
        if not on_cycle_stage or not current:
            raise DeliveryError("no-open-cycle", f"stage {stage.name} has no open cycle; run `al cycle LAYER start`")
        branch = branch.replace("{cycle}", str(current.get("name") or "")).replace(
            "{version}", str(current.get("version") or ""))
    if "{date}" in branch:
        if on_cycle_stage and current and current.get("opened_at"):
            day = str(current["opened_at"])[:10]
        else:
            day = _utc(now).strftime("%Y-%m-%d")
        branch = branch.replace("{date}", day)
    return branch


# ---------------------------------------------------------------- git helpers

def _git(cwd: Path, args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                                timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LayerError(f"git {' '.join(args)} failed: {exc}") from exc
    if check and result.returncode != 0:
        raise LayerError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result


def tree_git(state: Any, env_stack: Any, args: list[str], check: bool = False,
             step: str = "deliver") -> subprocess.CompletedProcess[str]:
    """git in the run's worktree; sandboxed: only through gitsafe. promotion.py uses it too."""
    sandbox = getattr(env_stack, "sandbox", None)
    if sandbox is not None:
        return gitsafe.run(sandbox, args, step, check=check, timeout=GIT_TIMEOUT)
    result = _git(Path(state.worktree), args)
    if check and result.returncode != 0:
        raise StepFailed(step, f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result


_tree_git = tree_git  # the name this module used before promotion.py needed it


def _branch_exists(app: Path, branch: str) -> bool:
    return _git(app, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0


def _remote(layer: Any) -> str:
    forge = layer.ir.get("forge") or {}
    if forge.get("kind") in ("github", "forgejo"):
        return "origin"
    return str(forge.get("remote") or "")


# ---------------------------------------------------------------- the run's target

def _first_class(adw: Mapping[str, Any], state: Any) -> str:
    return str(getattr(state, "klass", "") or (adw.get("classes") or ["chore"])[0])


def _ships_untouched(layer: Any, adw: Mapping[str, Any]) -> bool:
    review = layer.option("reviews", adw["review"]) if adw.get("review") else {}
    has_ship = any(step.get("kind") == "ship" for step in adw.get("steps", []))
    return has_ship and review.get("kind") == "auto" and review.get("ship") in ("merge", "deploy")


def _target_from_record(layer: Any, adw: Mapping[str, Any], data: Mapping[str, Any]) -> Target:
    stage = stage_named(layer, str(data["stage"]))
    return Target(adw=str(adw["name"]), stage=stage, branch=str(data["branch"]),
                  source=dict(data.get("source") or {}))


def for_run(layer: Any, adw: Mapping[str, Any], state: Any, env: Mapping[str, str] | None = None,
            now: datetime | None = None) -> Target | None:
    """The run's target, or None when the layer declares no stage. A resumed run keeps the target
    it started with; a fan-out candidate takes its parent's. Refuses (DeliveryError, and records
    {stage, branch, refused} in state.delivery) before any worktree exists."""
    if not stages(layer):
        return None
    env = os.environ if env is None else env
    recorded = getattr(state, "delivery", None) or {}
    if recorded.get("stage") and recorded.get("branch") and not recorded.get("refused"):
        return _target_from_record(layer, adw, recorded)
    stage: Stage | None = None
    branch = ""
    promotion = adw.get("promotion") if isinstance(adw.get("promotion"), dict) else None
    try:
        if promotion is not None:  # a promotion run starts at the head of the stage it promotes
            stage = stage_named(layer, str(promotion["from"]))
            branch = str(promotion.get("fromBranch") or "") or expand_branch(stage, load_cycle(layer), now)
        elif env.get("ADW_FANOUT_PARENT") and env.get("ADW_DELIVERY_STAGE"):
            stage = stage_named(layer, env["ADW_DELIVERY_STAGE"])
            branch = env.get("ADW_DELIVERY_BRANCH") or expand_branch(stage, load_cycle(layer), now)
        else:
            stage = route(layer, adw, _first_class(adw, state))
            assert stage is not None
            branch = expand_branch(stage, load_cycle(layer), now)
        if promotion is None and _ships_untouched(layer, adw) and not stage.agents_may_merge:
            raise DeliveryError("forbidden-target", f"workflow {adw['name']} ships without a person, but stage "
                                f"{stage.name} (branch {branch}) only changes by people; deliver to a stage with "
                                "#:agents-may-merge #t or use a human or agent review")
        app = Path(layer.app_root)
        if not _branch_exists(app, branch):
            remote = _remote(layer)
            fetched = bool(remote) and _git(app, ["fetch", "--quiet", remote,
                                                  f"refs/heads/{branch}:refs/heads/{branch}"]).returncode == 0
            if not fetched:
                raise DeliveryError("stage-missing", f"branch {branch} of stage {stage.name} does not exist in the "
                                    f"application repository{f' or on {remote}' if remote else ''}")
    except DeliveryError as exc:
        state.status = "failed"
        state.delivery = {"stage": stage.name if stage else "", "branch": branch, "refused": exc.reason,
                          "reason": exc.message}
        raise
    source: dict[str, str] = {}
    if stage.from_:
        source_stage = stage_named(layer, stage.from_)
        try:
            source = {"stage": source_stage.name, "branch": expand_branch(source_stage, load_cycle(layer), now)}
        except DeliveryError:
            source = {"stage": source_stage.name, "branch": source_stage.branch}
    return Target(adw=str(adw["name"]), stage=stage, branch=branch, source=source)


def record(target: Target) -> dict[str, Any]:
    stage = target.stage
    return {"stage": stage.name, "branch": target.branch, "entry": stage.entry,
            "agents_may_merge": stage.agents_may_merge, "environment_name": stage.environment_name,
            "source": dict(target.source), "origin": stage.origin}


def scope(layer: Any, target: Target) -> Any:
    """The run-scoped layer: default_branch is the target branch; in a copy of the IR the ADW's
    review option gets the stage's checks (added to #:require-checks) and, when it has none, the
    stage's deploy command. The original layer and its IR are not changed."""
    ir_copy = copy.deepcopy(layer.ir)
    adw = next((item for item in ir_copy.get("adws", []) if item.get("name") == target.adw), None)
    if adw is not None:
        for review in ir_copy.get("reviews", []):
            if review.get("id") != adw.get("review"):
                continue
            required = list(review.get("requireChecks") or [])
            for name in target.stage.checks:
                if name not in required:
                    required.append(name)
            if "requireChecks" in review or required:
                review["requireChecks"] = required
            if not review.get("deployCmd") and target.stage.deploy_cmd:
                review["deployCmd"] = target.stage.deploy_cmd
    return dataclasses.replace(layer, ir=ir_copy, delivery=record(target))


def _forge_kind(layer: Any) -> str:
    return str((layer.ir.get("forge") or {}).get("kind") or "none")


def entry_supported(layer: Any, target: Target | Mapping[str, Any]) -> bool:
    """Whether this runtime can bring a change into the stage the stage's way. Through a forge:
    merge, squash, rebase (the pull request's merge method). Without one: merge, plus whatever
    ship.ENTRY_METHODS lists once ship supports more."""
    entry = target.stage.entry if isinstance(target, Target) else str(target.get("entry") or "merge")
    if _forge_kind(layer) != "none":
        return entry in FORGE_METHODS
    if entry == "merge":
        return True
    from . import ship

    return entry in tuple(getattr(ship, "ENTRY_METHODS", ()))


# ---------------------------------------------------------------- freezes

def _day(now: datetime | None) -> str:
    return _utc(now).strftime("%Y-%m-%d")


def open_promotion_out_of(layer: Any, stage_name: str) -> dict[str, Any] | None:
    """The open promotion out of `stage_name` (its agents/promotions/<from>-<to>.json), or None."""
    directory = Path(layer.layer_dir) / OPEN_PROMOTIONS
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("from") == stage_name:
            return data
    return None


def frozen_reason(layer: Any, stage: Stage, now: datetime | None = None, resume: str = "") -> str:
    """Why `stage` takes no delivery now ("" when it does): a #:frozen period covering today (UTC, both
    days included), or, with #:frozen-while-promoting, an open promotion out of it. `resume` (how the
    waiting run continues) ends the sentence."""
    today = _day(now)
    tail = f": {resume}" if resume else ""
    for period in stage.frozen:
        first, _, last = str(period).partition("..")
        if first <= today <= last:
            return (f"stage {stage.name} is frozen from {first} to {last} (UTC); the run waits — resume it after "
                    f"the freeze{tail}")
    if stage.frozen_while_promoting:
        open_one = open_promotion_out_of(layer, stage.name)
        if open_one:
            return (f"stage {stage.name} is closed to deliveries while promotion {open_one.get('id', '?')} into "
                    f"{open_one.get('to', '?')} is open; it continues when the promotion closes{tail}")
    return ""


# ---------------------------------------------------------------- before and after ship

def _stage_head(layer: Any, state: Any, env_stack: Any) -> str:
    """The commit the stage branch is at now: fetched from the remote when there is one."""
    branch = layer.delivery["branch"]
    remote = _remote(layer) if not getattr(state, "dry_run", False) or _forge_kind(layer) == "none" else ""
    sandbox = getattr(env_stack, "sandbox", None)
    if remote:
        fetched = _tree_git(state, env_stack, ["fetch", *(["--refmap="] if sandbox else []), remote, branch])
        if fetched.returncode == 0:
            return _tree_git(state, env_stack, ["rev-parse", "FETCH_HEAD"], check=True).stdout.strip()
    if sandbox is not None:
        return gitsafe.run_app(sandbox, ["rev-parse", f"refs/heads/{branch}"], "deliver").stdout.strip()
    result = _git(Path(layer.app_root), ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    if result.returncode != 0:
        raise StepFailed("deliver", f"delivery:stage-missing: branch {branch} no longer exists")
    return result.stdout.strip()


def _stale(state: Any, env_stack: Any, head: str) -> int:
    out = _tree_git(state, env_stack, ["rev-list", "--count", f"HEAD..{head}"]).stdout.strip()
    return int(out) if out.isdigit() else 0


# replaceable in tests: whether `git merge-tree --write-tree` is known (git ≥ 2.38)
def _merge_tree(state: Any, env_stack: Any, head: str) -> subprocess.CompletedProcess[str]:
    return _tree_git(state, env_stack, ["merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", head])


def _conflicts(state: Any, env_stack: Any, head: str) -> list[str]:
    """Files that conflict when the stage head is merged into the run's HEAD (nothing is changed)."""
    probe = _merge_tree(state, env_stack, head)
    if probe.returncode == 0:
        return []
    if probe.returncode == 1:
        lines = [line for line in probe.stdout.splitlines()[1:] if line.strip()]
        return sorted(set(lines))
    # an older git (exit 129: unknown option): a trial merge in the worktree, then abort it
    trial = _tree_git(state, env_stack, [*IDENTITY, "merge", "--no-commit", "--no-ff", head])
    files = _unmerged(state, env_stack) if trial.returncode != 0 else []
    _tree_git(state, env_stack, ["merge", "--abort"])
    return files


def _unmerged(state: Any, env_stack: Any) -> list[str]:
    out = _tree_git(state, env_stack, ["diff", "--name-only", "--diff-filter=U"]).stdout
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def _nothing() -> contextlib.AbstractContextManager[None]:
    return contextlib.nullcontext()


def _never_commit(layer: Any, state: Any) -> list[str]:
    files: list[str] = []
    for option_id in layer.adw(state.adw).get("environment", []):
        option = layer.option("environments", option_id)
        if option["kind"] == "worktree":
            files += list(option.get("copyFiles", []))
    return files


def _review_step(adw: Mapping[str, Any]) -> dict[str, Any] | None:
    return next((dict(step) for step in adw.get("steps", []) if step.get("kind") == "review"), None)


def _model_set(layer: Any, command_name: str) -> str:
    """The model tier of a command (its #:model-set, else base)."""
    return str(layer.command(command_name).get("modelSet") or "base")


def _checks_that_ran(state: Any, adw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ADW's check steps that already ran in this run, in order."""
    ran = {record["name"] for record in state.steps if record.get("kind") == "check"
           and record.get("status") in {"passed", "failed"}}
    return [step for step in adw.get("steps", []) if step.get("kind") == "check" and step.get("name") in ran]


def _conflict_round(layer: Any, state: Any, adw: Mapping[str, Any], files: list[str], head: str,
                    env_stack: Any) -> None:
    """Merge the stage head into the worktree and let the review's patch and implement commands
    resolve the conflict (steps deliver-patch-1 / deliver-implement-1); then re-run the checks that
    ran, commit the merge and review the merged tree again. One round."""
    from . import agents, review, ship, steps

    branch = layer.delivery["branch"]
    listed = ", ".join(files)
    step = _review_step(layer.adw(state.adw))
    if step is None or not step.get("patch") or not step.get("implement"):
        events.emit("delivery.conflict", files=files, resolved=False)
        raise StepFailed("deliver", f"delivery:conflict: {listed} conflict with {branch}; "
                                 "the review step has no #:patch/#:implement")
    sandbox = getattr(env_stack, "sandbox", None)
    worktree = Path(state.worktree)
    print(f"[adw {state.adw_id}] delivery: {listed} conflict with {branch}; merging it and resolving in a patch round")
    _tree_git(state, env_stack, [*IDENTITY, "merge", "--no-ff", "--no-commit", head])
    request = (f"- merge conflict with {branch} in {listed}\n"
               "  Resolution: resolve the conflict markers keeping both intents; do not change unrelated code")
    patch_plan = agents.run_agent(layer, state, "deliver-patch-1", str(step["patch"]), [request, state.plan or ""],
                                  _model_set(layer, str(step["patch"])), env_stack)
    state.outputs["deliver-patch-1"] = patch_plan
    implemented = agents.run_agent(layer, state, "deliver-implement-1", str(step["implement"]), [patch_plan],
                                   _model_set(layer, str(step["implement"])), env_stack)
    state.outputs["deliver-implement-1"] = implemented
    marked = [name for name in files if (worktree / name).is_file()
              and CONFLICT_MARKER in (worktree / name).read_text(encoding="utf-8", errors="replace")]
    if not marked:
        _tree_git(state, env_stack, ["add", "--", *files])
    remaining = sorted(set(marked) | set(_unmerged(state, env_stack)))
    if remaining:
        _tree_git(state, env_stack, ["merge", "--abort"])
        events.emit("delivery.conflict", files=files, resolved=False)
        raise StepFailed("deliver", f"delivery:conflict: {', '.join(remaining)} still conflict with {branch} "
                                 "after the patch round")
    rerun = []
    for check in _checks_that_ran(state, layer.adw(state.adw)):
        steps.run_check_with_resolve(layer, state, check, env_stack)
        rerun.append(check["name"])
    ship.commit_pending(worktree, state, _never_commit(layer, state), sandbox,
                        message=f"merge {branch} into {state.branch} [adw {state.adw_id}]")
    events.emit("delivery.conflict", files=files, resolved=True)
    state.delivery["conflict"] = {"files": files, "resolved": True, "checks_rerun": rerun,
                                  "patch_plan": patch_plan, "implement_output": implemented[:2000]}
    earlier = list(state.review.get("rounds") or [])
    review.run_review(layer, state, step, env_stack)
    state.review["rounds"] = earlier + list(state.review.get("rounds") or [])
    state.save(layer.layer_dir)


def before_ship(layer: Any, state: Any, adw: Mapping[str, Any], step: Mapping[str, Any], env_stack: Any) -> None:
    """Checks before a ship step of a run with a delivery target, run as the step `deliver` (see
    steps.run_step). A failure raises StepFailed with `delivery:<reason>:`, which hands the run to
    a person (ship.handoff), its work on the run's branch."""
    del step
    target = layer.delivery
    stage, branch = str(target["stage"]), str(target["branch"])
    from . import runner  # the resume command a waiting run prints

    why = frozen_reason(layer, stage_named(layer, stage), resume=runner.resume_command(state))
    if why:
        events.emit("delivery.frozen", stage=stage, branch=branch, reason=why)
        state.delivery["frozen"] = why
        state.save(layer.layer_dir)
        raise StepWaiting("deliver", FROZEN_EXIT, why)
    state.delivery.pop("frozen", None)
    definition = layer.adw(state.adw)
    if not definition.get("deliverTo"):
        routed = route(layer, definition, state.klass)
        if routed is not None and routed.name != stage:
            raise StepFailed("deliver", f"delivery:class-moved: the run was classified {state.klass}, which goes to "
                                     f"stage {routed.name}, but it started from stage {stage} (branch {branch}); "
                                     f"run it again with --class {state.klass}, or deliver it by hand")
    if not entry_supported(layer, target):
        raise StepFailed("deliver", f"delivery:entry-unsupported: stage {stage} takes changes by {target['entry']}, which "
                                 f"this runtime cannot do {'through the forge' if _forge_kind(layer) != 'none' else 'without a forge'}; "
                                 "a person brings the change in")
    if _forge_kind(layer) != "none" and not state.dry_run:
        from . import forge

        try:
            protection = forge.get_client(layer).branch_protection(branch)
        except forge.ForgeError as exc:
            protection = {}
            print(f"[adw {state.adw_id}] delivery: branch protection of {branch} unknown: {exc}")
        state.delivery["protection"] = protection
        if target.get("entry") == "merge" and protection.get("linear_history") is True:
            raise StepFailed("deliver", f"delivery:entry-forbidden: stage {stage} takes changes by merge, but the "
                                     f"forge keeps {branch} linear (no merge commits); change the stage's #:entry")
    from . import ship

    # the run's work as ship would commit it, so the conflict check sees what will be delivered
    sandbox = getattr(env_stack, "sandbox", None)
    with gitsafe.session(sandbox) if sandbox is not None else _nothing():
        ship.commit_pending(Path(state.worktree), state, _never_commit(layer, state), sandbox)
    head = _stage_head(layer, state, env_stack)
    state.delivery["base"] = head  # the stage head this run's change is based on (a change = base..head)
    behind = _stale(state, env_stack, head)
    if behind > 0:
        events.say(f"[adw {state.adw_id}] delivery: {branch} moved {behind} commit(s) since the run started",
                   "delivery.stale", stage=stage, branch=branch, behind=behind)
        state.delivery["behind"] = behind
    files = _conflicts(state, env_stack, head)
    if files:
        _conflict_round(layer, state, adw, files, head, env_stack)
    state.save(layer.layer_dir)


def _delivered_head(layer: Any, branch: str) -> str:
    """The stage branch's head right after a delivery: the local branch, or the remote's (ls-remote)."""
    remote = _remote(layer)
    if remote:
        out = _git(Path(layer.app_root), ["ls-remote", remote, f"refs/heads/{branch}"]).stdout.split()
        return out[0] if out else ""
    result = _git(Path(layer.app_root), ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    return result.stdout.strip() if result.returncode == 0 else ""


def after_ship(layer: Any, state: Any) -> None:
    state.delivery["delivered"] = bool(state.ship.get("shipped"))
    state.delivery["target"] = str(state.ship.get("target") or "")
    if state.delivery["delivered"]:
        try:
            state.delivery["head"] = _delivered_head(layer, str(state.delivery.get("branch") or ""))
        except LayerError as exc:
            print(f"[adw {state.adw_id}] delivery: head of {state.delivery.get('branch')} unknown: {exc}")
    state.save(layer.layer_dir)
    events.emit("delivery.result", stage=state.delivery.get("stage"), branch=state.delivery.get("branch"),
                delivered=state.delivery["delivered"], target=state.delivery["target"])


# ---------------------------------------------------------------- the cycle (agents/cycle.json)

def load_cycle(layer: Any) -> dict[str, Any] | None:
    path = Path(layer.layer_dir) / CYCLE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LayerError(f"cannot read {CYCLE_FILE}: {exc}") from exc
    return data if isinstance(data, dict) else None


def _save_cycle(layer: Any, data: dict[str, Any]) -> None:
    target = Path(layer.layer_dir) / CYCLE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(target)


def _require_cycle(layer: Any) -> tuple[dict[str, Any], Stage]:
    option = cycle_option(layer)
    if option is None:
        raise LayerError("no (cycle …) is declared in agentic.rkt")
    return option, stage_named(layer, str(option["stage"]))


def _fill_name(pattern: str, now: datetime, n: int, version: str) -> str:
    year, week, _ = now.isocalendar()
    return (pattern.replace("{year}", str(year)).replace("{week}", f"{week:02d}").replace("{n}", str(n))
            .replace("{date}", now.strftime("%Y-%m-%d")).replace("{version}", version))


def _source(layer: Any, stage: Stage, now: datetime) -> dict[str, str]:
    if not stage.from_:
        return {"stage": "", "branch": ""}
    source = stage_named(layer, stage.from_)
    branch = source.branch.replace("{date}", now.strftime("%Y-%m-%d"))
    return {"stage": source.name, "branch": branch}


def _into(layer: Any, stage: Stage) -> dict[str, str]:
    if not stage.next:
        return {"stage": "", "branch": ""}
    into = stage_named(layer, stage.next)
    return {"stage": into.name, "branch": into.branch}


def _count(app: Path, spec: str) -> int | None:
    result = _git(app, ["rev-list", "--count", spec])
    return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip().isdigit() else None


def cycle_status(layer: Any, now: datetime | None = None) -> dict[str, Any]:
    option, stage = _require_cycle(layer)
    data = load_cycle(layer) or {}
    current = data.get("current")
    now = _utc(now)
    result: dict[str, Any] = {"kind": option["kind"], "stage": stage.name, "open": bool(current),
                              "source": _source(layer, stage, now), "into": _into(layer, stage),
                              "length_days": option.get("lengthDays") if option["kind"] == "sprint" else None,
                              "current": current, "history": len(data.get("history") or [])}
    if current:
        app = Path(layer.app_root)
        opened = datetime.fromisoformat(str(current["opened_at"]))
        if result["length_days"]:
            result["days_left"] = int(result["length_days"]) - (now.date() - opened.date()).days
        branch = str(current["branch"])
        into, source = result["into"]["branch"], result["source"]["branch"]
        result["ahead"] = _count(app, f"refs/heads/{into}..refs/heads/{branch}") if into else None
        result["behind"] = _count(app, f"refs/heads/{branch}..refs/heads/{source}") if source else None
    return result


def cycle_start(layer: Any, name: str | None = None, version: str | None = None, push: bool = False,
                now: datetime | None = None) -> dict[str, Any]:
    """Open the next cycle: close the open one (recorded, never merged), then create the stage's
    branch for the new cycle from its source stage's head with `git branch` (nothing is checked out)."""
    option, stage = _require_cycle(layer)
    now = _utc(now)
    if option["kind"] == "release" and not version:
        raise LayerError("a release cycle is started with --version V")
    app = Path(layer.app_root)
    with slots.file_lock(Path(layer.layer_dir) / CYCLE_LOCK):
        data = load_cycle(layer) or {"version": 1, "kind": option["kind"], "stage": stage.name,
                                     "current": None, "history": []}
        data["kind"], data["stage"] = option["kind"], stage.name
        previous = data.get("current")
        numbers = [int(item.get("n") or 0) for item in [*(data.get("history") or []), *([previous] if previous else [])]]
        n = max(numbers, default=0) + 1
        cycle_name = name or _fill_name(str(option.get("name") or "{n}"), now, n, version or "")
        opened = {"name": cycle_name, "version": version or "", "opened_at": now.isoformat(timespec="seconds"), "n": n}
        branch = expand_branch(stage, {"stage": stage.name, "current": opened}, now)
        source = _source(layer, stage, now)
        if not source["branch"] or not _branch_exists(app, source["branch"]):
            raise DeliveryError("source-missing", f"stage {stage.name} is cut from "
                                f"{source['branch'] or 'no stage'}, which does not exist in the application repository")
        if _branch_exists(app, branch):
            raise DeliveryError("branch-exists", f"branch {branch} already exists; give another --name or --version")
        closed = _close_current(layer, data, now) if previous else None
        commit = _git(app, ["rev-parse", f"refs/heads/{source['branch']}"], check=True).stdout.strip()
        _git(app, ["branch", branch, commit], check=True)
        pushed = ""
        if push:
            remote = _remote(layer)
            if not remote:
                raise LayerError("--push needs a forge or (forge none #:remote …) to push to")
            _git(app, ["push", remote, f"refs/heads/{branch}:refs/heads/{branch}"], check=True)
            pushed = remote
        data["current"] = {**opened, "branch": branch, "from_commit": commit}
        _save_cycle(layer, data)
    return {"current": data["current"], "closed": closed, "source": source, "pushed": pushed}


def _close_current(layer: Any, data: dict[str, Any], now: datetime) -> dict[str, Any]:
    _option, stage = _require_cycle(layer)
    current = dict(data["current"])
    into = _into(layer, stage)["branch"]
    app = Path(layer.app_root)
    accepted = bool(into) and _git(app, ["merge-base", "--is-ancestor", f"refs/heads/{current['branch']}",
                                         f"refs/heads/{into}"]).returncode == 0
    closed = {**current, "closed_at": now.isoformat(timespec="seconds"), "accepted": accepted}
    data.setdefault("history", []).append(closed)
    data["current"] = None
    return closed


def cycle_close(layer: Any, now: datetime | None = None) -> dict[str, Any]:
    """Close the open cycle: record when, and whether its branch is already in the stage it goes
    into. Never merges: accepting a cycle is a person's step; the result says what that step is."""
    _option, stage = _require_cycle(layer)
    now = _utc(now)
    with slots.file_lock(Path(layer.layer_dir) / CYCLE_LOCK):
        data = load_cycle(layer) or {}
        if not data.get("current"):
            raise DeliveryError("cycle-not-open", "no cycle is open; start one with `al cycle LAYER start`")
        closed = _close_current(layer, data, now)
        _save_cycle(layer, data)
    try:  # the closed cycle's branch is proven by a batch of the full suites (batch.py); never fails the close
        from . import batch

        batch.after_cycle_closed(layer, stage.name)
    except Exception as exc:  # noqa: BLE001
        print(f"batch: not started after the cycle closed: {type(exc).__name__}: {exc}")
    into = _into(layer, stage)["branch"]
    if closed["accepted"]:
        step = f"{closed['branch']} is already merged into {into}"
    elif _forge_kind(layer) != "none":
        step = f"open a pull request from {closed['branch']} into {into}; a person reviews and merges it"
    else:
        step = f"a person merges {closed['branch']} into {into} (git merge --no-ff {closed['branch']})"
    return {"closed": closed, "into": into, "next": step}


def status_lines(status: Mapping[str, Any]) -> list[str]:
    """`al cycle LAYER status` in words."""
    current = status.get("current")
    if not current:
        return [f"cycle: {status['kind']}: none open; start one with `al cycle LAYER start`"]
    opened = str(current["opened_at"])[:10]
    if status.get("length_days"):
        timing = f" ({status['length_days']} days, {status.get('days_left')} left)"
    else:
        timing = ""
    source, into = status["source"]["branch"], status["into"]["branch"]
    lines = [f"cycle: {status['kind']} {current['name']} open since {opened}{timing}",
             f"branch: {current['branch']} (from {source or '?'} at {str(current.get('from_commit', ''))[:7]})"]
    parts = []
    if into:
        parts.append(f"ahead of {into}: {status.get('ahead')} commits")
    if source:
        parts.append(f"behind {source}: {status.get('behind')} commits")
    if parts:
        lines.append(" · ".join(parts))
    return lines


