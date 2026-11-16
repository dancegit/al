"""Promotion between stages: a stage's work moves into the next stage, accepted by people.

`al promote LAYER FROM TO` (the layer's `adws/promote.py run FROM TO`) starts a promotion run: an
ordinary run of a synthetic ADW (`promotion_adw`, like a single-command run) whose steps are kind
`promote`, so it has the run log, events, heartbeat, slots, summary, waiting/resume and dashboard
page of every run. Its steps:

    open      pin the stage head, list the changes (runs delivered into FROM, not yet in TO)
    checks    FROM's and TO's #:checks on the stage head
    pack      the presentation pack under artefacts/<id>/pack/ (index.html and its files)
    request   a pull request FROM → TO (forge) or promotions/<id>/approval.md (no forge)
    approval  decisions (al decide, the dashboard, PR comments and reviews, agent reviewers);
              refused changes are reverted out of FROM; waits (exit 75) until the approval exists
    version   the new version where the repository keeps it, and the changelog entry
    merge     FROM enters TO by merge, squash, fast-forward or cherry-pick
    tag       the tag on the promoted commit
    deploy    TO's #:deploy-cmd on the promoted tree
    back-merge  the result merged back into the working stages
    close     promotions/<id>/promotion.json and sign-off.md; the cycle closes

Decisions are files of the layer, committable and audited: promotions/<id>/acceptance.jsonl (one
JSON object per decision, append-only) rendered as sign-off.md. One promotion per FROM → TO is open
at a time (agents/promotions/<from>-<to>.json); `al promote` again continues it.

The application's working tree is never touched: merges, reverts and version commits happen in the
run's worktree, and application refs move only by compare-and-swap update-ref (or a push). A target
that is checked out gets refs/heads/al/promote/<branch> and a note instead.

Standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from . import delivery, events, gitsafe, ir, slots, state as state_module, storage
from .errors import LayerError, StepFailed, StepWaiting

STEPS = ("open", "checks", "pack", "request", "approval", "version", "merge", "tag", "deploy", "back-merge", "close")
REASONS = ("no-stages", "unknown-promotion", "not-open", "already-open", "agents-not-allowed", "tag-exists",
           "stage-missing", "ambiguous-branch", "nothing-to-promote", "checks-failed", "revert-conflict",
           "merge-conflict", "not-fast-forward", "deploy-failed", "back-merge-conflict", "agents-refused",
           "invalid-decision", "publish-failed")
DECISIONS = ("accept", "refuse", "approve")
THEN = ("revert", "patch")
KINDS = ("human", "agent", "forge")
WAITING_EXIT = 75
RECORDS = "promotions"              # promotions/<id>/{acceptance.jsonl, sign-off.md, approval.md, promotion.json}
ACCEPTANCE = "acceptance.jsonl"
MAX_BY = 80
MAX_COMMENT = 2000
CHANGE_ID = re.compile(r"^[0-9a-f]{8}$")
CONFLICT_MARKER = "<<<<<<<"
IDENTITY = ["-c", "user.name=adw", "-c", "user.email=adw@localhost"]
GIT_TIMEOUT = 300
VERSION_FILES = ("VERSION", "pyproject.toml", "package.json", "Cargo.toml")
CHANGELOG_FILES = ("CHANGELOG.md", "CHANGES.md", "HISTORY.md")
SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.+-]*)?")
SECTIONS = {"feature": "Added", "bug": "Fixed", "chore": "Changed", "patch": "Changed"}
IMAGE_TYPES = ("image",)
VIDEO_TYPES = ("video",)


class PromotionError(LayerError):
    """A promotion cannot start or go on, for a classified reason (see REASONS)."""

    def __init__(self, reason: str, message: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown promotion reason {reason!r}")
        self.reason = reason
        self.message = message
        super().__init__(f"promotion:{reason}: {message}")


@dataclass
class Change:
    """One run that delivered into the FROM branch and is not yet in TO."""
    id: str
    adw: str = ""
    prompt: str = ""
    klass: str = "chore"
    plan: str = ""                          # layer-relative
    criteria: list[str] = field(default_factory=list)
    checks: dict[str, int] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)       # {success, summary}
    risks: list[str] = field(default_factory=list)
    artefacts: list[dict[str, Any]] = field(default_factory=list)
    base: str = ""
    head: str = ""
    files: list[str] = field(default_factory=list)
    status: str = "open"                    # open | accepted | refused
    excluded: str = ""                      # "" | reverted | patch
    ended: str = ""
    worktree: str = ""
    by: str = ""
    at: str = ""
    comment: str = ""
    then: str = ""
    patch_run: dict[str, Any] = field(default_factory=dict)
    revert: str = ""

    @property
    def title(self) -> str:
        return first_line(self.prompt)


def first_line(text: str) -> str:
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _stamp(now: datetime | None = None) -> str:
    return _now(now).isoformat(timespec="seconds")


# ---------------------------------------------------------------- the declared promotion and its branches

def promotions(layer: Any) -> list[dict[str, Any]]:
    return [p for p in layer.ir.get("promotions") or [] if isinstance(p, dict)]


def pairs(layer: Any) -> str:
    return ", ".join(f"{p.get('from')} -> {p.get('to')}" for p in promotions(layer)) or "(none)"


def promotion_for(layer: Any, from_: str, to: str) -> dict[str, Any]:
    """The compiled promotion FROM → TO (the IR's `promotions`)."""
    if not delivery.stages(layer):
        raise PromotionError("no-stages", "the layer declares no stages, so nothing is promoted; add (stage …) "
                                          "clauses or a (delivery PRESET …) clause")
    for promo in promotions(layer):
        if promo.get("from") == from_ and promo.get("to") == to:
            return promo
    raise PromotionError("unknown-promotion", f"no promotion {from_} -> {to}; the layer's promotions: {pairs(layer)}"
                         + ("" if promotions(layer) else " (regenerate the layer with `al gen`)"))


def _app_git(app: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(app), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LayerError(f"git {' '.join(args)} failed: {exc}") from exc


def _ref(app: Path, ref: str) -> str:
    result = _app_git(app, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return result.stdout.strip() if result.returncode == 0 else ""


def _ensure_branch(layer: Any, branch: str, stage: str) -> None:
    app = Path(layer.app_root)
    if _ref(app, f"refs/heads/{branch}"):
        return
    remote = delivery._remote(layer)
    if remote and _app_git(app, ["fetch", "--quiet", remote, f"refs/heads/{branch}:refs/heads/{branch}"]).returncode == 0:
        return
    raise PromotionError("stage-missing", f"branch {branch} of stage {stage} does not exist in the application "
                                          f"repository{f' or on {remote}' if remote else ''}")


def _date_branches(layer: Any, pattern: str, to_branch: str) -> list[str]:
    app = Path(layer.app_root)
    prefix = pattern.split("{", 1)[0]
    wanted = re.compile("^" + re.escape(pattern).replace(re.escape("{date}"), r"\d{4}-\d{2}-\d{2}") + "$")
    listed = _app_git(app, ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}"]).stdout.split()
    found = []
    for name in sorted(listed):
        if not wanted.match(name):
            continue
        merged = _app_git(app, ["merge-base", "--is-ancestor", f"refs/heads/{name}", f"refs/heads/{to_branch}"])
        if merged.returncode != 0:
            found.append(name)
    return found


def resolve_branches(layer: Any, promo: Mapping[str, Any], branch: str | None = None,
                     now: datetime | None = None) -> tuple[str, str]:
    """(FROM branch, TO branch) of a promotion: TO's fixed (or open cycle's) branch; FROM from
    --branch, the open cycle ({cycle}/{version}) or the one local branch matching a {date} pattern
    that is not merged into TO yet."""
    cycle = delivery.load_cycle(layer)
    from_stage = delivery.stage_named(layer, str(promo["from"]))
    to_stage = delivery.stage_named(layer, str(promo["to"]))
    try:
        to_branch = delivery.expand_branch(to_stage, cycle, now)
    except delivery.DeliveryError as exc:
        raise PromotionError("stage-missing", exc.message) from exc
    _ensure_branch(layer, to_branch, to_stage.name)
    if branch:
        _ensure_branch(layer, branch, from_stage.name)
        return branch, to_branch
    pattern = from_stage.branch
    if "{cycle}" in pattern or "{version}" in pattern:
        try:
            from_branch = delivery.expand_branch(from_stage, cycle, now)
        except delivery.DeliveryError as exc:
            raise PromotionError("stage-missing", exc.message) from exc
    elif "{date}" in pattern:
        found = _date_branches(layer, pattern, to_branch)
        if not found:
            raise PromotionError("stage-missing", f"no branch {pattern} of stage {from_stage.name} waits to go into "
                                                  f"{to_branch}")
        if len(found) > 1:
            raise PromotionError("ambiguous-branch", f"{len(found)} branches of stage {from_stage.name} wait to go "
                                                     f"into {to_branch}; give --branch: {', '.join(found)}")
        from_branch = found[0]
    else:
        from_branch = pattern
    _ensure_branch(layer, from_branch, from_stage.name)
    return from_branch, to_branch


def base_adw(layer: Any, promo: Mapping[str, Any]) -> dict[str, Any]:
    adws = [a for a in layer.ir.get("adws") or [] if isinstance(a, dict) and not a.get("promotion")]
    if not adws:
        raise LayerError("the layer declares no workflow, so a promotion has no environment to run in")
    wanted = str(promo.get("adw") or "")
    return next((a for a in adws if a.get("name") == wanted), adws[0])


def promotion_steps(promo: Mapping[str, Any]) -> list[str]:
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
    return [step for step in STEPS if step not in skip]


def adw_name(promo: Mapping[str, Any]) -> str:
    return f"promote-{promo['from']}-{promo['to']}"


def promotion_adw(layer: Any, promo: Mapping[str, Any], from_branch: str, to_branch: str = "") -> dict[str, Any]:
    """The synthetic ADW of a promotion run (see module docstring): the environment, agent and review
    option of the promotion's base ADW, one `promote` step per step of the promotion."""
    base = base_adw(layer, promo)
    if not to_branch:
        to_branch = delivery.expand_branch(delivery.stage_named(layer, str(promo["to"])), delivery.load_cycle(layer))
    declared = {str(o.get("id")) for o in layer.ir.get("promptInputs") or [] if isinstance(o, dict)}
    return {"name": adw_name(promo), "description": f"promotion {promo['from']} -> {promo['to']}",
            "promotion": {"from": str(promo["from"]), "to": str(promo["to"]), "fromBranch": from_branch,
                          "toBranch": to_branch, "base": str(base.get("name"))},
            "deliverTo": str(promo["from"]), "agent": base.get("agent"), "review": base.get("review"),
            "environment": list(base.get("environment") or []), "input": [i for i in ["cli"] if i in declared],
            "classes": [], "trigger": [], "changes": "codebase",
            "steps": [{"kind": "promote", "name": step, "action": step} for step in promotion_steps(promo)]}


# ---------------------------------------------------------------- the changes

def _plan_path(layer: Any, plan: str) -> Path | None:
    if not plan:
        return None
    path = Path(plan)
    path = path if path.is_absolute() else Path(layer.layer_dir) / path
    return path if path.is_file() else None


def criteria(layer: Any, plan: str, prompt: str = "") -> list[str]:
    """The bullets under `## Acceptance Criteria` of the run's plan (up to the next `## `); without
    that heading, the request itself."""
    path = _plan_path(layer, plan)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path else []
    found: list[str] = []
    inside = False
    for line in lines:
        if re.match(r"^##\s+acceptance criteria\s*$", line.strip(), re.IGNORECASE):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            item = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)", line)
            if item:
                found.append(item.group(1).strip())
            elif found and line.startswith((" ", "\t")) and line.strip():
                found[-1] += " " + line.strip()
    return found or [f"The request: {first_line(prompt)}"]


def _what_was_done(layer: Any, plan: str) -> str:
    """The first paragraph of the run's plan after its title."""
    path = _plan_path(layer, plan)
    if path is None:
        return ""
    paragraph: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#"):
            if paragraph:
                break
            continue
        if not line.strip():
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    return " ".join(paragraph)


def _load_states(layer: Any) -> list[dict[str, Any]]:
    out = []
    for path in sorted((Path(layer.layer_dir) / "agents").glob("*/state.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and CHANGE_ID.match(str(data.get("adw_id") or "")):
            out.append(data)
    return out


def _is_ancestor(app: Path, commit: str, ref: str) -> bool:
    return bool(commit) and _app_git(app, ["merge-base", "--is-ancestor", commit, ref]).returncode == 0


def _layer_relative(layer: Any, path: str) -> str:
    if not path:
        return ""
    target = Path(path)
    if target.is_absolute():
        with contextlib.suppress(ValueError):
            return target.resolve().relative_to(Path(layer.layer_dir).resolve()).as_posix()
        return layer.display_path(target)
    return target.as_posix()


def _change_from_state(layer: Any, data: Mapping[str, Any]) -> Change:
    app = Path(layer.app_root)
    d = data.get("delivery") or {}
    verdict = (data.get("review") or {}).get("verdict") or {}
    risks = [f"{issue.get('severity')}: {issue.get('description')}" for issue in verdict.get("issues") or []
             if isinstance(issue, dict) and issue.get("severity") in ("tech_debt", "tech-debt", "skippable")]
    media = [dict(item) for item in data.get("artefacts") or []
             if isinstance(item, dict) and item.get("type") in IMAGE_TYPES + VIDEO_TYPES and item.get("path")
             and item.get("step") != "pack"]
    base, head = str(d.get("base") or ""), str(d.get("head") or "")
    files = []
    if base and head:
        files = _app_git(app, ["diff", "--name-only", base, head]).stdout.split()
    plan = _layer_relative(layer, str(data.get("plan") or ""))
    return Change(id=str(data["adw_id"]), adw=str(data.get("adw") or ""), prompt=str(data.get("prompt") or ""),
                  klass=str(data.get("klass") or "chore"), plan=plan,
                  criteria=criteria(layer, plan, str(data.get("prompt") or "")),
                  checks={name: int(c.get("exit", 1)) for name, c in (data.get("checks") or {}).items()
                          if isinstance(c, dict)},
                  verdict={"success": bool(verdict.get("success")), "summary": str(verdict.get("summary") or ""),
                           "mode": str(verdict.get("mode") or "")} if verdict else {},
                  risks=risks, artefacts=media, base=base, head=head, files=files, ended=str(data.get("ended") or ""),
                  worktree=str(data.get("worktree") or ""))


def changes(layer: Any, from_branch: str, to_branch: str, pinned: str = "") -> tuple[list[Change], list[Change]]:
    """(changes, handed over): the runs that delivered into `from_branch` (or whose pull request into it a
    person merged) and whose delivered head is not in `to_branch` yet (and, given `pinned`, is in the
    pinned stage head), ordered by when they ended; and the runs aimed at `from_branch` that were handed
    to a person instead."""
    app = Path(layer.app_root)
    to_ref = to_branch if to_branch.startswith("refs/") or re.fullmatch(r"[0-9a-f]{40}", to_branch) \
        else f"refs/heads/{to_branch}"
    from_ref = pinned or f"refs/heads/{from_branch}"
    found, handed = [], []
    for data in _load_states(layer):
        d = data.get("delivery") or {}
        ship = data.get("ship") or {}
        if d.get("branch") != from_branch:
            continue
        if ship.get("action") == "handoff":
            handed.append(_change_from_state(layer, data))
            continue
        if not d.get("delivered") and ship.get("action") == "pr" and ship.get("commit") and d.get("base") \
                and _is_ancestor(app, str(ship["commit"]), from_ref):
            # a pull request (or PR.md) into the stage that a person merged: the run's commit is the change
            data = {**data, "delivery": {**d, "head": str(ship["commit"])}}
            d = data["delivery"]
        elif not d.get("delivered") or not d.get("head"):
            continue
        head = str(d["head"])
        if _is_ancestor(app, head, to_ref):
            continue
        if pinned and not _is_ancestor(app, head, pinned):
            continue
        found.append(_change_from_state(layer, data))
    found.sort(key=lambda c: (c.ended, c.id))
    handed.sort(key=lambda c: (c.ended, c.id))
    return found, handed


def proof_for(layer: Any, change: Change | Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per acceptance criterion the proof that covers it: the checks named in it (else every check
    of the run), the pictures and recordings named in it (else all of them), and the reviewer
    verdict. A criterion with neither a passing check nor a picture or recording is `uncovered`."""
    c = change if isinstance(change, Change) else Change(**{k: v for k, v in dict(change).items()
                                                           if k in {f.name for f in dataclasses.fields(Change)}})
    del layer
    out = []
    for text in c.criteria:
        lowered = text.lower()
        named = {name: code for name, code in c.checks.items() if name.lower() in lowered}
        checks = named or dict(c.checks)
        media = [a for a in c.artefacts if Path(str(a.get("path"))).name.lower() in lowered]
        media = media or list(c.artefacts)
        items: list[dict[str, Any]] = [{"kind": "check", "label": f"{name} exit {code}", "ok": code == 0}
                                       for name, code in checks.items()]
        for item in media:
            kind = "video" if item.get("type") in VIDEO_TYPES else "image"
            items.append({"kind": kind, "label": Path(str(item["path"])).name, "path": str(item["path"]), "ok": True})
        if c.verdict:
            items.append({"kind": "verdict", "label": f"review {'pass' if c.verdict.get('success') else 'fail'}"
                          + (f": {c.verdict.get('summary')}" if c.verdict.get("summary") else ""),
                          "ok": bool(c.verdict.get("success"))})
        covered = any(i["kind"] == "check" and i["ok"] for i in items) or any(i["kind"] in ("image", "video")
                                                                                for i in items)
        out.append({"criterion": text, "proof": items, "uncovered": not covered})
    return out


# ---------------------------------------------------------------- decisions

def records_dir(layer: Any, promotion_id: str) -> Path:
    return Path(layer.layer_dir) / RECORDS / promotion_id


def decisions(layer: Any, promotion_id: str) -> list[dict[str, Any]]:
    path = records_dir(layer, promotion_id) / ACCEPTANCE
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        with contextlib.suppress(ValueError):
            value = json.loads(line)
            if isinstance(value, dict) and value.get("decision") in DECISIONS:
                out.append(value)
    return out


def _promotion_state(layer: Any, promotion_id: str) -> dict[str, Any]:
    path = state_module.run_dir(layer.layer_dir, promotion_id) / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _known_changes(layer: Any, promotion_id: str) -> list[dict[str, Any]]:
    return [c for c in (_promotion_state(layer, promotion_id).get("promotion") or {}).get("changes") or []
            if isinstance(c, dict)]


def record_decision(layer: Any, promotion_id: str, by: str, decision: str, scope: Iterable[str] = ("all",),
                    comment: str = "", then: str = "", kind: str = "human", source: str = "",
                    now: datetime | None = None, known: Iterable[str] | None = None) -> dict[str, Any]:
    """Append one decision to promotions/<id>/acceptance.jsonl (the audit trail: a later decision on
    the same scope by the same person replaces the earlier one when evaluated, the file keeps both)
    and re-render sign-off.md. Raises PromotionError("invalid-decision") on bad input."""
    who = str(by or "").strip()
    if not who or len(who) > MAX_BY or "\n" in who or "\r" in who:
        raise PromotionError("invalid-decision", f"--by names who decides: 1–{MAX_BY} characters on one line")
    if decision not in DECISIONS:
        raise PromotionError("invalid-decision", f"decision is one of {', '.join(DECISIONS)}")
    if kind not in KINDS:
        raise PromotionError("invalid-decision", f"kind is one of {', '.join(KINDS)}")
    scope = [str(s) for s in scope] or ["all"]
    ids = set(known) if known is not None else {str(c.get("id")) for c in _known_changes(layer, promotion_id)}
    for item in scope:
        if item != "all" and item not in ids:
            raise PromotionError("invalid-decision", f"{item} is not a change of promotion {promotion_id}; its "
                                                     f"changes: {', '.join(sorted(ids)) or '(none)'}")
    if "all" in scope and len(scope) > 1:
        raise PromotionError("invalid-decision", "decide on `all` or on changes, not both")
    if decision == "approve" and scope != ["all"]:
        raise PromotionError("invalid-decision", "approve is for the whole promotion; accept or refuse one change")
    if decision == "refuse":
        then = then or "revert"
        if then not in THEN:
            raise PromotionError("invalid-decision", f"--then is one of {', '.join(THEN)}")
    elif then:
        raise PromotionError("invalid-decision", "--then goes with refuse only")
    comment = str(comment or "")
    if len(comment) > MAX_COMMENT:
        raise PromotionError("invalid-decision", f"a comment has at most {MAX_COMMENT} characters")
    record = {"at": _stamp(now), "by": who, "kind": kind, "scope": scope, "decision": decision, "then": then,
              "comment": comment, "source": source}
    directory = records_dir(layer, promotion_id)
    directory.mkdir(parents=True, exist_ok=True)
    with slots.file_lock(directory / "acceptance.lock"):
        if source and any(d.get("source") == source for d in decisions(layer, promotion_id)):
            return {**record, "duplicate": True}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        handle = os.open(directory / ACCEPTANCE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(handle, line.encode("utf-8"))
        finally:
            os.close(handle)
        render_sign_off(layer, promotion_id)
    events.emit("promotion.decision", by=who, kind=kind, scope=scope, decision=decision, then=then,
                comment=comment[:500], source=source)
    return record


def effective(lines: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The decisions that count: per person and scope, the latest."""
    latest: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for line in lines:
        key = (str(line.get("by")), tuple(line.get("scope") or ["all"]))
        latest.pop(key, None)
        latest[key] = dict(line)
    return list(latest.values())


def _as_change(item: Change | Mapping[str, Any]) -> Change:
    if isinstance(item, Change):
        return item
    names = {f.name for f in dataclasses.fields(Change)}
    return Change(**{k: v for k, v in dict(item).items() if k in names})


def change_status(change: Change, current: list[dict[str, Any]], forge_approved: bool = False) -> dict[str, str]:
    """{status, by, at, comment, then} of one change under the effective decisions: a refusal of it (or
    of all) wins; else an acceptance of it, or an approval / acceptance of all."""
    covering = [d for d in current if change.id in (d.get("scope") or []) or (d.get("scope") or []) == ["all"]]
    refused = [d for d in covering if d.get("decision") == "refuse"]
    if refused:
        d = refused[-1]
        return {"status": "refused", "by": str(d["by"]), "at": str(d["at"]), "comment": str(d.get("comment") or ""),
                "then": str(d.get("then") or "revert")}
    accepted = [d for d in covering if d.get("decision") in ("accept", "approve")]
    if accepted:
        d = accepted[-1]
        return {"status": "accepted", "by": str(d["by"]), "at": str(d["at"]), "comment": str(d.get("comment") or ""),
                "then": ""}
    if forge_approved:
        return {"status": "accepted", "by": "forge review", "at": "", "comment": "", "then": ""}
    return {"status": "open", "by": "", "at": "", "comment": "", "then": ""}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def approval_status(layer: Any, promo: Mapping[str, Any], record_lines: Iterable[Mapping[str, Any]],
                    change_list: Iterable[Change | Mapping[str, Any]],
                    forge_approvals: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Whether the declared approval exists: {needed, have, approved_by, undecided, refused, ready, why}."""
    del layer
    current = effective(record_lines)
    forge_approvals = list(forge_approvals)
    approval = str(promo.get("approval") or "human")
    needed = 0 if approval == "auto" else max(1, int(promo.get("approvers") or 1))
    if approval == "agent":
        approved_by = sorted({str(d["by"]) for d in current if d.get("decision") == "approve" and d.get("kind") == "agent"})
    else:
        approved_by = sorted({str(d["by"]) for d in current if d.get("decision") == "approve"
                              and d.get("kind") in ("human", "forge")}
                             | {str(a.get("by")) for a in forge_approvals if a.get("by")})
    forge_approvals = list(forge_approvals)
    forge_approved = bool(forge_approvals)
    items = [_as_change(c) for c in change_list]
    undecided, refused = [], []
    for change in items:
        if change.excluded or change.status == "handed-over":
            continue
        status = change_status(change, current, forge_approved)["status"]
        if status == "refused":
            refused.append(change.id)
        elif status == "open":
            undecided.append(change.id)
    if "acceptance" not in (promo.get("requires") or []):
        undecided = []
    have = len(approved_by)
    missing = max(0, needed - have)
    parts = []
    if missing:
        who = "human" if approval == "human" else "agent"
        parts.append(f"{_plural(missing, f'{who} approval')} missing" + (f" ({have} of {needed} given)" if have else ""))
    if undecided:
        parts.append(f"{_plural(len(undecided), 'change')} undecided")
    ready = not parts
    return {"needed": needed, "have": have, "approved_by": approved_by, "undecided": undecided, "refused": refused,
            "ready": ready, "why": "; ".join(parts) if parts else "approved"}


def _stage_label(layer: Any, name: str) -> str:
    with contextlib.suppress(LayerError):
        if delivery.stage_named(layer, name).origin == "preset phase-gate" and name != "main":
            return f"phase {name}"
    return name


def _md_cell(text: str) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|") or "—"


def render_sign_off(layer: Any, promotion_id: str, info: Mapping[str, Any] | None = None) -> Path:
    """promotions/<id>/sign-off.md from acceptance.jsonl: one row per change (who, when, decision,
    comment), then every approval and decision in order."""
    info = dict(info or (_promotion_state(layer, promotion_id).get("promotion") or {}))
    lines = decisions(layer, promotion_id)
    current = effective(lines)
    from_, to = str(info.get("from") or "?"), str(info.get("to") or "?")
    title = f"Sign-off: {_stage_label(layer, from_)} → {to}"
    out = [f"# {title}", "",
           f"Promotion `{promotion_id}`: `{info.get('from_branch') or from_}` into `{info.get('to_branch') or to}`"
           f" ({info.get('method') or 'merge'}). Recorded in `{RECORDS}/{promotion_id}/{ACCEPTANCE}`; this page is "
           "rendered from it.", "", "## Changes", "",
           "| Change | Request | Decision | By | When | Comment |", "|---|---|---|---|---|---|"]
    items = [_as_change(c) for c in info.get("changes") or []]
    for change in items:
        s = change_status(change, current)
        decision = {"refused": f"refused ({s['then'] or 'revert'})", "accepted": "accepted"}.get(s["status"], "undecided")
        out.append(f"| `{change.id}` | {_md_cell(change.title)} | {decision} | {_md_cell(s['by'])} | "
                   f"{_md_cell(s['at'])} | {_md_cell(s['comment'])} |")
    if not items:
        out.append("| — | — | — | — | — | — |")
    approvals = [d for d in current if d.get("decision") == "approve"]
    out += ["", "## Approvals", ""]
    out += [f"- {d['by']} ({d.get('kind')}), {d['at']}" + (f": {d['comment']}" if d.get("comment") else "")
            for d in approvals] or ["- none yet"]
    out += ["", "## Every decision, in order", ""]
    out += [f"- {d['at']} {d['by']} ({d.get('kind')}): {d['decision']} {', '.join(d.get('scope') or [])}"
            + (f" then {d['then']}" if d.get("then") else "") + (f" — {d['comment']}" if d.get("comment") else "")
            + (f" [{d['source']}]" if d.get("source") else "") for d in lines] or ["- none"]
    path = records_dir(layer, promotion_id) / "sign-off.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- versions, changelog, tags

def _section_span(text: str, header: str) -> tuple[int, int] | None:
    match = re.search(rf"(?m)^\[{re.escape(header)}\]\s*$", text)
    if not match:
        return None
    nxt = re.search(r"(?m)^\[", text[match.end():])
    return match.end(), match.end() + nxt.start() if nxt else len(text)


def _toml_version(text: str, header: str) -> tuple[str, tuple[int, int]] | None:
    span = _section_span(text, header)
    if span is None:
        return None
    found = re.search(r'(?m)^version\s*=\s*"([^"]*)"', text[span[0]:span[1]])
    if not found:
        return None
    return found.group(1), (span[0] + found.start(1), span[0] + found.end(1))


def _json_top_version(text: str) -> tuple[str, tuple[int, int]] | None:
    """The top-level "version" string of a JSON document and its span (no re-serialisation)."""
    depth, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            key = text[i + 1:j]
            k = j + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if depth == 1 and key == "version" and k < n and text[k] == ":":
                k += 1
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k < n and text[k] == '"':
                    end = k + 1
                    while end < n and text[end] != '"':
                        end += 2 if text[end] == "\\" else 1
                    return text[k + 1:end], (k + 1, end)
            i = j + 1
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        i += 1
    return None


def _found_in(worktree: Path, name: str) -> tuple[str, tuple[int, int]] | None:
    path = worktree / name
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if name == "VERSION":
        match = re.search(r"\S+", text)
        return (match.group(0).lstrip("v"), (match.start() + (1 if match.group(0).startswith("v") else 0),
                                              match.end())) if match else None
    if name == "pyproject.toml":
        return _toml_version(text, "project") or _toml_version(text, "tool.poetry")
    if name == "package.json":
        return _json_top_version(text)
    if name == "Cargo.toml":
        return _toml_version(text, "package")
    return None


def _semver_key(text: str) -> tuple[int, int, int] | None:
    match = SEMVER.fullmatch(text.strip())
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def read_version(worktree: Path, to_ref: str = "HEAD", git: Any = None) -> tuple[str, str]:
    """(version, source): from VERSION, pyproject.toml ([project], then [tool.poetry]), package.json
    (top level) or Cargo.toml ([package]), else the highest vX.Y.Z tag reachable from `to_ref`
    (source `tag vX.Y.Z`), else ("0.0.0", "")."""
    worktree = Path(worktree)
    for name in VERSION_FILES:
        found = _found_in(worktree, name)
        if found and found[0]:
            return found[0], name
    run = git or (lambda args: _app_git(worktree, args))
    tags = run(["tag", "--merged", to_ref]).stdout.split()
    best = max(((key, tag) for tag in tags if (key := _semver_key(tag))), default=None)
    if best:
        return ".".join(str(n) for n in best[0]), f"tag {best[1]}"
    return "0.0.0", ""


def bump(version: str, level: str) -> str:
    """X.Y.Z raised at `level` (major resets minor and patch); a pre-release or build suffix is dropped."""
    match = SEMVER.match(str(version).strip())
    major, minor, patch = (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else (0, 0, 0)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def write_version(worktree: Path, source: str, version: str) -> bool:
    """Replace only the version string in `source` (a file of VERSION_FILES); False for a tag source."""
    if source not in VERSION_FILES:
        return False
    path = Path(worktree) / source
    found = _found_in(Path(worktree), source)
    if not found:
        return False
    text = path.read_text(encoding="utf-8")
    start, end = found[1]
    path.write_text(text[:start] + version + text[end:], encoding="utf-8")
    return True


def changelog_kind(worktree: Path) -> tuple[str, str]:
    """(kind, file): `keepachangelog` (an `## [Unreleased]` heading or a keepachangelog.com link),
    `plain` (a heading with a version number), or ("", "") without a changelog file."""
    for name in CHANGELOG_FILES:
        path = Path(worktree) / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?mi)^##\s*\[unreleased\]", text) or "keepachangelog.com" in text:
            return "keepachangelog", name
        if re.search(r"(?m)^#+\s.*\d+\.\d+\.\d+", text):
            return "plain", name
        return "plain", name
    return "", ""


def _entry_lines(change_list: Iterable[Change | Mapping[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for change in (_as_change(c) for c in change_list):
        grouped.setdefault(SECTIONS.get(change.klass, "Changed"), []).append(f"- {change.title}")
    return grouped


def changelog_entry(kind: str, version: str, date: str, change_list: Iterable[Change | Mapping[str, Any]],
                    heading: str = "") -> str:
    """The changelog entry `insert_changelog` writes: keepachangelog `## [X.Y.Z] - DATE` with
    ### Added / Fixed / Changed; plain: `heading` (the first version heading) with version and date
    replaced, then one line per change."""
    grouped = _entry_lines(change_list)
    if kind == "keepachangelog":
        parts = [f"## [{version}] - {date}"]
        for section in ("Added", "Fixed", "Changed"):
            if grouped.get(section):
                parts += ["", f"### {section}", "", *grouped[section]]
        return "\n".join(parts) + "\n"
    title = heading or f"## {version} - {date}"
    if heading:
        title = re.sub(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]*)?",
                       lambda m: ("v" if m.group(0).startswith("v") else "") + version, heading, count=1)
        title = re.sub(r"\d{4}-\d{2}-\d{2}", date, title, count=1)
    lines = [line for section in ("Added", "Fixed", "Changed") for line in grouped.get(section, [])]
    return "\n".join([title, "", *lines]) + "\n"


def _first_version_heading(text: str) -> str:
    match = re.search(r"(?m)^(#+\s.*\d+\.\d+\.\d+.*)$", text)
    return match.group(1) if match else ""


def insert_changelog(worktree: Path, name: str, kind: str, version: str, date: str,
                     change_list: Iterable[Change | Mapping[str, Any]]) -> str:
    """Write the entry into the changelog file `name`; returns the entry."""
    path = Path(worktree) / name
    text = path.read_text(encoding="utf-8")
    if kind == "keepachangelog":
        entry = changelog_entry(kind, version, date, change_list)
        unreleased = re.search(r"(?mi)^##\s*\[unreleased\].*$", text)
        if unreleased:
            nxt = re.search(r"(?m)^## ", text[unreleased.end():])
            at = unreleased.end() + nxt.start() if nxt else len(text)
        else:
            first = re.search(r"(?m)^## ", text)
            at = first.start() if first else len(text)
        before = text[:at].rstrip("\n") + "\n\n" if text[:at].strip() else text[:at]
        after = text[at:]
        path.write_text(before + entry + ("\n" + after if after else ""), encoding="utf-8")
        return entry
    heading = _first_version_heading(text)
    entry = changelog_entry("plain", version, date, change_list, heading or "")
    if heading:
        at = text.index(heading)
    else:
        first = re.search(r"(?m)^## ", text)
        at = first.start() if first else len(text)
    before = text[:at]
    if before and not before.endswith("\n\n"):
        before = before.rstrip("\n") + "\n\n"
    path.write_text(before + entry + "\n" + text[at:], encoding="utf-8")
    return entry


def fill_tag(tag: str, version: str = "", cycle: str = "", date: str = "") -> str:
    return str(tag).replace("{version}", version).replace("{cycle}", cycle).replace("{date}", date)


# ---------------------------------------------------------------- the open promotion

def marker_path(layer: Any, from_: str, to: str) -> Path:
    return Path(layer.layer_dir) / delivery.OPEN_PROMOTIONS / f"{from_}-{to}.json"


def open_promotion(layer: Any, from_: str, to: str) -> dict[str, Any] | None:
    path = marker_path(layer, from_, to)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") else None


def _mark_open(layer: Any, info: Mapping[str, Any]) -> None:
    path = marker_path(layer, str(info["from"]), str(info["to"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, prefix=".open.") as handle:
        json.dump(dict(info), handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(handle.name, path)


def _unmark(layer: Any, from_: str, to: str, promotion_id: str) -> None:
    current = open_promotion(layer, from_, to)
    if current and current.get("id") == promotion_id:
        marker_path(layer, from_, to).unlink(missing_ok=True)


# ---------------------------------------------------------------- the run's context and git

@dataclass
class _Run:
    layer: Any
    state: Any
    adw: dict[str, Any]
    env: Any
    promo: dict[str, Any]

    @property
    def p(self) -> dict[str, Any]:
        return self.state.promotion

    @property
    def pid(self) -> str:
        return str(self.state.adw_id)

    @property
    def app(self) -> Path:
        return Path(self.layer.app_root)

    @property
    def sandbox(self) -> Any:
        return getattr(self.env, "sandbox", None)

    @property
    def dry(self) -> bool:
        return bool(self.state.dry_run)

    def say(self, line: str) -> None:
        print(line, flush=True)
        self.p.setdefault("lines", []).append(line)

    def git(self, args: list[str], step: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """git in the run's worktree (sandboxed: through gitsafe)."""
        return delivery.tree_git(self.state, self.env, args, check=check, step=step)

    def app_git(self, args: list[str], step: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """git in the application repository: ref updates, tags, pushes. Never its working tree."""
        if self.sandbox is not None:
            return gitsafe.run_app(self.sandbox, args, step, check=check)
        result = _app_git(self.app, args)
        if check and result.returncode != 0:
            raise StepFailed(step, f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
        return result

    def head(self, step: str) -> str:
        return self.git(["rev-parse", "HEAD"], step, check=True).stdout.strip()

    def branch_head(self, branch: str, step: str) -> str:
        """The head of `branch`: fetched from the remote when there is one, else the local branch."""
        remote = delivery._remote(self.layer)
        if remote:
            fetched = self.git(["fetch", *(["--refmap="] if self.sandbox else []), remote, branch], step)
            if fetched.returncode == 0:
                return self.git(["rev-parse", "FETCH_HEAD"], step, check=True).stdout.strip()
        out = self.app_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], step)
        return out.stdout.strip() if out.returncode == 0 else ""

    @property
    def from_stage(self) -> delivery.Stage:
        return delivery.stage_named(self.layer, str(self.promo["from"]))

    @property
    def to_stage(self) -> delivery.Stage:
        return delivery.stage_named(self.layer, str(self.promo["to"]))

    def change_list(self) -> list[Change]:
        return [_as_change(c) for c in self.p.get("changes") or []]

    def save_changes(self, items: list[Change]) -> None:
        self.p["changes"] = [dataclasses.asdict(c) for c in items]
        self.state.save(self.layer.layer_dir)


def _forge_kind(layer: Any) -> str:
    return str((layer.ir.get("forge") or {}).get("kind") or "none")


def _client(run: _Run) -> Any:
    if _forge_kind(run.layer) == "none" or run.dry:
        return None
    from . import forge

    return forge.get_client(run.layer)


def _never_commit(run: _Run) -> list[str]:
    files: list[str] = []
    for option_id in run.adw.get("environment") or []:
        with contextlib.suppress(LayerError):
            option = run.layer.option("environments", option_id)
            if option.get("kind") == "worktree":
                files += list(option.get("copyFiles", []))
    return files


def _commit(run: _Run, message: str) -> str:
    from . import ship

    with gitsafe.session(run.sandbox) if run.sandbox is not None else contextlib.nullcontext():
        return ship.commit_pending(Path(run.state.worktree), run.state, _never_commit(run), run.sandbox, message=message)


def _checked_out(run: _Run, branch: str, step: str) -> bool:
    listing = run.app_git(["worktree", "list", "--porcelain"], step).stdout
    return any(line.strip() == f"branch refs/heads/{branch}" for line in listing.splitlines())


def _publish(run: _Run, branch: str, commit: str, old: str, step: str) -> tuple[str, bool]:
    """Put `commit` on `branch` of the application: pushed to the remote (forge or #:remote), else a
    compare-and-swap update-ref; a branch checked out in a working tree of the application gets
    refs/heads/al/promote/<branch> and a note instead. Returns (where, staged)."""
    from . import ship

    shared = getattr(ship, "merge_into", None)  # the shared rule once ship.py offers it (see docs/DESIGN.md)
    if shared is not None and not delivery._remote(run.layer):
        ok, where = shared(run.layer, run.state, Path(run.state.worktree), branch, commit, sandbox=run.sandbox)
        return where, not ok
    sandbox = run.sandbox
    if sandbox is not None:
        commit = gitsafe.import_objects(sandbox, commit, step)
    remote = delivery._remote(run.layer)
    if remote:
        if sandbox is not None:
            pushed = gitsafe.run_app(sandbox, ["push", remote, f"{commit}:refs/heads/{branch}"], step, check=False)
        else:
            pushed = run.git(["push", remote, f"{commit}:refs/heads/{branch}"], step)
        if pushed.returncode != 0:
            raise StepFailed(step, f"promotion:publish-failed: push to {remote}/{branch} refused: "
                                   f"{(pushed.stderr or pushed.stdout).strip()[:300]}")
        return f"{remote}/{branch}", False
    if _checked_out(run, branch, step):
        staging = f"al/promote/{branch}"
        if sandbox is not None:
            gitsafe.update_ref(sandbox, f"refs/heads/{staging}", commit, step, f"promotion {run.pid}")
        else:
            run.app_git(["update-ref", "-m", f"promotion {run.pid}", f"refs/heads/{staging}", commit], step, check=True)
        note = (f"{branch} is checked out in a working tree, so the result is on {staging}; run "
                f"`git merge --ff-only {staging}` in the app to take it")
        notes = run.p.setdefault("notes", [])
        if note not in notes:
            notes.append(note)
        run.say(f"note: {note}")
        return staging, True
    if sandbox is not None:
        gitsafe.update_ref(sandbox, f"refs/heads/{branch}", commit, step, f"promotion {run.pid}")
    else:
        moved = run.app_git(["update-ref", "-m", f"promotion {run.pid}", f"refs/heads/{branch}", commit, old or ""],
                            step)
        if moved.returncode != 0:
            raise StepFailed(step, f"promotion:publish-failed: {branch} moved meanwhile; run `al promote` again: "
                                   f"{(moved.stderr or moved.stdout).strip()[:300]}")
    return branch, False


def _publish_from(run: _Run, commit: str, step: str) -> None:
    """Bring the promotion's own commits (reverts, the version) onto the FROM branch. When FROM moved
    since the pin, its head is merged with them first; the promoted commit stays what was pinned."""
    if not run.from_stage.agents_may_merge and _forge_kind(run.layer) == "none":
        note = f"{run.p['from_branch']} only changes by people: the promotion's own commits go with the merge only"
        if note not in run.p.setdefault("notes", []):
            run.p["notes"].append(note)
        return
    branch = str(run.p["from_branch"])
    old = str(run.p.get("from_head") or run.p.get("pinned") or "")
    current = run.branch_head(branch, step)
    target = commit
    if current and current != old and not _is_ancestor_in(run, current, commit, step):
        run.git(["checkout", "--detach", current], step, check=True)
        merged = run.git([*IDENTITY, "merge", "--no-ff", "-m",
                          f"Merge promotion {run.pid} into {branch} [promotion {run.pid}]", commit], step)
        if merged.returncode != 0:
            run.git(["merge", "--abort"], step)
            run.git(["checkout", "--detach", commit], step)
            raise StepFailed(step, f"promotion:publish-failed: {branch} moved and does not merge with the "
                                   "promotion's commits; a person merges them")
        target = run.head(step)
        run.git(["checkout", "--detach", commit], step, check=True)
        old = current
    _publish(run, branch, target, old, step)
    run.p["from_head"] = target
    run.state.save(run.layer.layer_dir)


def _is_ancestor_in(run: _Run, a: str, b: str, step: str) -> bool:
    return run.git(["merge-base", "--is-ancestor", a, b], step).returncode == 0


def _review_step(run: _Run) -> dict[str, Any] | None:
    try:
        base = run.layer.adw(str(run.adw.get("promotion", {}).get("base") or run.promo.get("adw") or ""))
    except LayerError:
        return None
    return next((dict(s) for s in base.get("steps") or [] if s.get("kind") == "review"), None)


def _model_set(layer: Any, command: str) -> str:
    return str(layer.command(command).get("modelSet") or "base")


def _markers(worktree: Path, files: Iterable[str]) -> list[str]:
    out = []
    for name in files:
        path = worktree / name
        if path.is_file() and CONFLICT_MARKER in path.read_text(encoding="utf-8", errors="replace"):
            out.append(name)
    return out


def _unmerged(run: _Run, step: str) -> list[str]:
    out = run.git(["diff", "--name-only", "--diff-filter=U"], step).stdout
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def _agent_round(run: _Run, prefix: str, blocker: str, files: list[str], step: str, reason: str) -> None:
    """One patch round of the base ADW's review step (#:patch, then #:implement) on a conflict:
    steps <prefix>-patch / <prefix>-implement. No conflict marker may remain afterwards."""
    from . import agents

    review_step = _review_step(run)
    if review_step is None or not review_step.get("patch") or not review_step.get("implement"):
        raise StepFailed(step, f"promotion:{reason}: {', '.join(files) or 'the change'} conflict(s), and the review step "
                               "of the promotion's workflow has no #:patch/#:implement to resolve them; a person does")
    request = f"- {blocker}\n  Resolution: resolve every conflict marker; keep every other change intact"
    plan = agents.run_agent(run.layer, run.state, f"{prefix}-patch", str(review_step["patch"]),
                            [request, run.state.plan or ""], _model_set(run.layer, str(review_step["patch"])), run.env)
    run.state.outputs[f"{prefix}-patch"] = plan
    done = agents.run_agent(run.layer, run.state, f"{prefix}-implement", str(review_step["implement"]), [plan],
                            _model_set(run.layer, str(review_step["implement"])), run.env)
    run.state.outputs[f"{prefix}-implement"] = done
    worktree = Path(run.state.worktree)
    tracked = run.git(["ls-files"], step).stdout.split()
    remaining = sorted(set(_markers(worktree, set(files) | set(tracked))))
    if remaining:
        raise StepFailed(step, f"promotion:{reason}: {', '.join(remaining)} still hold conflict markers after the "
                               "agent round; a person resolves them")
    run.git(["add", "-A"], step)


# ---------------------------------------------------------------- the presentation pack

def _clean(run_or_layer: Any, text: str, worktrees: Iterable[str] = ()) -> str:
    """Text an agent wrote, without absolute paths of this machine (as on the pull request)."""
    from . import ship

    layer = run_or_layer.layer if isinstance(run_or_layer, _Run) else run_or_layer
    text = str(text or "")
    for tree in [*worktrees, ""]:
        text = ship._without_machine_paths(layer, SimpleNamespace(worktree=tree), text)
    return text


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry(layer: Any, path: Path, kind: str, artefact_id: str) -> dict[str, Any]:
    return {"id": artefact_id, "type": kind, "step": "pack", "path": path.relative_to(layer.layer_dir).as_posix(),
            "bytes": path.stat().st_size, "sha256": _sha(path), "url": ""}


def _since(layer: Any, promo: Mapping[str, Any]) -> str:
    """Since when the pack's changes collected: the last closed promotion of FROM → TO, else the cycle."""
    closed = []
    for path in (Path(layer.layer_dir) / RECORDS).glob("*/promotion.json"):
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("from") == promo.get("from") and data.get("to") == promo.get("to") and data.get("closed_at"):
                closed.append(data)
    if closed:
        last = max(closed, key=lambda d: str(d["closed_at"]))
        return f"since promotion {last.get('id')} ({str(last['closed_at'])[:10]})"
    cycle = delivery.load_cycle(layer) or {}
    if cycle.get("stage") == promo.get("from") and cycle.get("current"):
        return "since the cycle opened"
    return "not yet in the next stage"


def _release_notes(change_list: list[Change]) -> str:
    grouped = _entry_lines(change_list)
    parts = []
    for section in ("Added", "Fixed", "Changed"):
        if grouped.get(section):
            parts += [f"### {section}", "", *grouped[section], ""]
    return "\n".join(parts).strip() + "\n" if parts else "Nothing is released: no change was accepted.\n"


def _planned_version(run: _Run, included: list[Change]) -> dict[str, Any]:
    """What the version step will do (computed on the worktree, nothing written)."""
    version = run.promo.get("version") or None
    worktree = Path(run.state.worktree)
    kind, name = changelog_kind(worktree)
    out: dict[str, Any] = {"kind": kind, "changelog": name, "entry": ""}
    if not version:
        return out
    to_ref = str(run.p.get("to_head") or "HEAD")
    current, source = read_version(worktree, to_ref, git=lambda args: run.git(args, "pack"))
    new = bump(current, str(version.get("level") or "patch")) if version.get("how") == "bump" else current
    out.update({"from": current, "to": new, "source": source})
    if kind:
        heading = ""
        if kind == "plain":
            heading = _first_version_heading((worktree / name).read_text(encoding="utf-8", errors="replace"))
        out["entry"] = changelog_entry(kind, new, _now().strftime("%Y-%m-%d"), included, heading)
    return out


def _summary_md(run: _Run, included: list[Change], excluded: list[Change], handed: list[Change],
                checks: Mapping[str, int]) -> str:
    p, promo = run.p, run.promo
    lines = [f"# Promote {p['from_branch']} into {p['to_branch']}", "",
             f"Promotion `{run.pid}` of stage {promo['from']} into {promo['to']} ({promo.get('method', 'merge')}), "
             f"{_since(run.layer, promo)}: {_plural(len(included) + len(excluded), 'change')} delivered, "
             f"{len(excluded)} excluded, {len(handed)} handed to a person.", "",
             f"Stage head: `{str(p.get('promoted') or p.get('pinned'))[:12]}`. Checks: "
             + (", ".join(f"{name}={code}" for name, code in checks.items()) or "none") + ".", "", "## Changes", ""]
    for change in included:
        lines.append(f"- `{change.id}` ({change.klass}): {change.title}")
    if not included:
        lines.append("- none")
    if excluded or handed:
        lines += ["", "## Not included", ""]
        lines += [f"- `{c.id}`: {c.title} — refused by {c.by}" + (f": \"{c.comment}\"" if c.comment else "")
                  + f" ({c.excluded})" for c in excluded]
        lines += [f"- `{c.id}`: {c.title} — handed over to a person" for c in handed]
    lines += ["", "Decide: `al decide LAYER "
              f"{promo['from']} {promo['to']} accept|refuse|approve [CHANGE …] --by NAME [--comment TEXT]`, "
              "or comment `/accept ID`, `/refuse ID [revert|patch] why`, `/approve` on the pull request."]
    return _clean(run, "\n".join(lines) + "\n", [c.worktree for c in included + excluded])


def _h(text: Any) -> str:
    return html.escape("" if text is None else str(text), quote=True)


PACK_CSS = """
body{font:15px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1d2330;background:#fff}
h1{font-size:1.6rem}h2{border-bottom:1px solid #d5d9e0;padding-bottom:.2rem;margin-top:2rem}
.change{border:1px solid #d5d9e0;border-radius:8px;padding:1rem;margin:1rem 0}
.meta{color:#5b6475;font-size:.9rem}.ok{color:#17733a}.bad{color:#b3261e}.uncovered{color:#9a5b00}
table{border-collapse:collapse}td,th{border:1px solid #d5d9e0;padding:.3rem .6rem;text-align:left}
img,video{max-width:100%;border:1px solid #d5d9e0;border-radius:6px;margin:.4rem 0}
.strip{display:flex;flex-wrap:wrap;gap:.6rem}.strip img,.strip video{max-width:18rem}
pre{background:#f4f6f9;padding:.6rem;overflow:auto}code{background:#f4f6f9;padding:0 .2rem}
"""


def _media_html(item: Mapping[str, Any]) -> str:
    src = _h(item["src"])
    if item["kind"] == "video":
        return f'<video controls src="{src}" title="{_h(item["label"])}"></video>'
    return f'<img src="{src}" alt="{_h(item["label"])}">'


def _pack_html(run: _Run, included: list[Change], excluded: list[Change], handed: list[Change],
               checks: Mapping[str, Any], media: Mapping[str, list[dict[str, Any]]], notes: str, entry: str,
               proofs: Mapping[str, list[dict[str, Any]]]) -> str:
    """One self-contained page (inline CSS, no script, relative links only), sections in `present` order."""
    p, promo = run.p, run.promo
    present = list(promo.get("present") or ["summary", "changes", "proof", "demo", "checks", "notes", "excluded"])
    worktrees = [c.worktree for c in included + excluded + handed]
    clean = lambda text: _h(_clean(run, text, worktrees))  # one rule for every agent text
    sections: dict[str, str] = {}
    sections["summary"] = (
        f"<h2>Summary</h2><p>Stage <b>{_h(promo['from'])}</b> (<code>{_h(p['from_branch'])}</code>) into "
        f"<b>{_h(promo['to'])}</b> (<code>{_h(p['to_branch'])}</code>) by {_h(promo.get('method', 'merge'))}, "
        f"{_h(_since(run.layer, promo))}.</p><p>{len(included)} change(s) to decide or accepted, {len(excluded)} "
        f"refused, {len(handed)} handed over. Stage head <code>{_h(str(p.get('promoted') or p.get('pinned'))[:12])}"
        f"</code>.</p>")
    blocks = []
    for change in included:
        rows = []
        for item in proofs.get(change.id, []):
            proof = "; ".join(
                f'<span class="{"ok" if pr["ok"] else "bad"}">{_h(pr["label"])}</span>' for pr in item["proof"]) or "none"
            flag = ' <span class="uncovered">(uncovered: no passing check and no picture)</span>' if item["uncovered"] else ""
            rows.append(f"<li>{clean(item['criterion'])}{flag}<br><span class=\"meta\">proof: {proof}</span></li>")
        risks = "".join(f"<li>{clean(r)}</li>" for r in change.risks) or "<li>none noted by the reviewers</li>"
        files = ", ".join(f"<code>{_h(f)}</code>" for f in change.files) or "—"
        done = _what_was_done(run.layer, change.plan)
        blocks.append(
            f'<div class="change" id="change-{_h(change.id)}"><h3>{_h(change.title)}</h3>'
            f'<p class="meta">change <code>{_h(change.id)}</code> · {_h(change.klass)} · workflow {_h(change.adw)} · '
            f"{_h(change.status)}</p><p><b>Request:</b> {clean(change.prompt)}</p>"
            f"<p><b>What was done:</b> {clean(done) or 'see the plan'}"
            + (f" (plan <code>{_h(change.plan)}</code>)" if change.plan else "") + f"</p><p><b>Files:</b> {files}</p>"
            f"<p><b>Risk notes:</b></p><ul>{risks}</ul><p><b>Acceptance criteria:</b></p><ul>{''.join(rows)}</ul>"
            + "".join(_media_html(m) for m in media.get(change.id, [])) + "</div>")
    sections["changes"] = "<h2>Changes</h2>" + ("".join(blocks) or "<p>No change.</p>")
    proof_rows = []
    for change in included:
        items = media.get(change.id, [])
        if items:
            proof_rows.append(f"<h3>{_h(change.title)} (<code>{_h(change.id)}</code>)</h3>"
                              + "".join(f"<figure>{_media_html(m)}<figcaption>{_h(m['label'])}</figcaption></figure>"
                                        for m in items))
    sections["proof"] = "<h2>Proof</h2>" + ("".join(proof_rows) or "<p>No pictures or recordings were captured.</p>")
    strip = [m for change in included for m in media.get(change.id, [])]
    sections["demo"] = ("<h2>Show</h2><p>This page is the demo: every picture and recording, in order.</p>"
                        f'<div class="strip">{"".join(_media_html(m) for m in strip) or "<p>nothing to show</p>"}</div>')
    check_rows = "".join(f"<tr><td>{_h(name)}</td><td class=\"{'ok' if data['exit'] == 0 else 'bad'}\">{_h(data['exit'])}"
                         f"</td><td><code>{_h(data.get('log', ''))}</code></td></tr>" for name, data in checks.items())
    sections["checks"] = (f"<h2>Checks on the stage head</h2><table><tr><th>check</th><th>exit</th><th>log</th></tr>"
                          f"{check_rows or '<tr><td colspan=3>none declared</td></tr>'}</table>")
    sections["notes"] = (f"<h2>Release notes</h2><pre>{clean(notes)}</pre><h2>Changelog entry</h2>"
                         f"<pre>{clean(entry or 'no changelog detected')}</pre>")
    rows = [f"<li><code>{_h(c.id)}</code> {_h(c.title)} — refused by {_h(c.by)}"
            + (f": “{clean(c.comment)}”" if c.comment else "") + f" ({_h(c.excluded or 'to be reverted')})</li>"
            for c in excluded]
    rows += [f"<li><code>{_h(c.id)}</code> {_h(c.title)} — handed over to a person, not in this promotion</li>"
             for c in handed]
    if run.dry:
        rows += [f"<li><code>{_h(c.id)}</code> {_h(c.title)} — open: undecided in this dry run</li>"
                 for c in included if c.status == "open"]
    sections["excluded"] = "<h2>Not included</h2><ul>" + ("".join(rows) or "<li>nothing</li>") + "</ul>"
    body = "".join(sections[name] for name in present if name in sections)
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Promote {_h(p['from_branch'])} "
            f"into {_h(p['to_branch'])}</title><style>{PACK_CSS}</style></head><body><h1>Promote "
            f"{_h(p['from_branch'])} into {_h(p['to_branch'])}</h1><p class=\"meta\">promotion {_h(run.pid)}</p>"
            f"{body}</body></html>\n")


def _copy_media(run: _Run, pack: Path, change: Change) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Copies of a change's pictures, recordings and keyframes under pack/media/<id>/: (html items, artefacts)."""
    shown, entries = [], []
    target_dir = pack / "media" / change.id
    for item in change.artefacts:
        source = Path(run.layer.layer_dir) / str(item["path"])
        if not source.is_file():
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        shutil.copy2(source, target)
        kind = "video" if item.get("type") in VIDEO_TYPES else "image"
        shown.append({"kind": kind, "src": target.relative_to(pack).as_posix(), "label": source.name})
        entry = _entry(run.layer, target, kind, "pack-media")
        if kind == "video":
            frames = []
            for frame in item.get("keyframes") or []:
                frame_source = Path(run.layer.layer_dir) / str(frame.get("path") or "")
                if not frame_source.is_file():
                    continue
                frame_target = target_dir / frame_source.name
                shutil.copy2(frame_source, frame_target)
                shown.append({"kind": "image", "src": frame_target.relative_to(pack).as_posix(),
                              "label": f"{frame_source.name} (keyframe of {source.name})"})
                frames.append({"path": frame_target.relative_to(run.layer.layer_dir).as_posix(),
                               "bytes": frame_target.stat().st_size})
            entry["keyframes"] = frames
        entries.append(entry)
    return shown, entries


def build_pack(run: _Run, checks: Mapping[str, Any] | None = None) -> Path:
    """The presentation pack under <artefacts>/<id>/pack/: index.html, summary.md, release-notes.md,
    changelog-entry.md, acceptance.md and media/<change id>/…; every file is an artefact of the run."""
    layer, state = run.layer, run.state
    pack = storage.artefacts_root(layer) / run.pid / "pack"
    if pack.exists():
        shutil.rmtree(pack)
    pack.mkdir(parents=True)
    items = run.change_list()
    handed = [_as_change(c) for c in run.p.get("handed_over") or []]
    included = [c for c in items if not c.excluded]
    excluded = [c for c in items if c.excluded or c.status == "refused"]
    included = [c for c in included if c not in excluded]
    checks = dict(checks if checks is not None else run.p.get("check_logs") or {})
    media: dict[str, list[dict[str, Any]]] = {}
    entries: list[dict[str, Any]] = []
    for change in included:
        shown, copied = _copy_media(run, pack, change)
        media[change.id] = shown
        entries += copied
    proofs = {c.id: proof_for(layer, c) for c in included}
    run.p["proofs"] = proofs   # what the dashboard's promotion card shows per criterion
    accepted = [c for c in included if c.status != "refused"]
    planned = _planned_version(run, accepted)
    run.p["planned_version"] = {k: v for k, v in planned.items() if k != "entry"}
    notes = _release_notes(accepted)
    entry = planned.get("entry") or ""
    (pack / "summary.md").write_text(_summary_md(run, included, excluded, handed,
                                                 {k: v["exit"] for k, v in checks.items()}), encoding="utf-8")
    (pack / "release-notes.md").write_text(notes, encoding="utf-8")
    (pack / "changelog-entry.md").write_text(entry or "no changelog detected\n", encoding="utf-8")
    acceptance = [f"# Acceptance: {run.p['from_branch']} into {run.p['to_branch']}", ""]
    current = effective(decisions(layer, run.pid)) if not run.dry else []
    for change in included:
        s = change_status(change, current)
        decided = f"Decision: {s['status']}" + (f" by {s['by']} ({s['at']})" if s["by"] else "")
        acceptance += [f"## `{change.id}` {change.title}", "", decided, ""]
        for item in proofs[change.id]:
            proof = "; ".join(f"{pr['label']}{'' if pr['ok'] else ' (failed)'}" for pr in item["proof"]) or "none"
            acceptance.append(f"- {item['criterion']}{' (uncovered)' if item['uncovered'] else ''} — proof: {proof}")
        acceptance.append("")
    (pack / "acceptance.md").write_text(_clean(run, "\n".join(acceptance) + "\n", [c.worktree for c in included]),
                                        encoding="utf-8")
    (pack / "index.html").write_text(_pack_html(run, included, excluded, handed, checks, media, notes, entry, proofs),
                                     encoding="utf-8")
    state.artefacts = [a for a in state.artefacts if a.get("step") != "pack"]
    state.artefacts.append(_entry(layer, pack / "index.html", "html", "pack"))
    for name in ("summary.md", "release-notes.md", "changelog-entry.md", "acceptance.md"):
        state.artefacts.append(_entry(layer, pack / name, "file", "pack"))
    state.artefacts += entries
    run.p["pack"] = (pack / "index.html").relative_to(layer.layer_dir).as_posix()
    run.p["summary_md"] = (pack / "summary.md").relative_to(layer.layer_dir).as_posix()
    state.save(layer.layer_dir)
    return pack / "index.html"


# ---------------------------------------------------------------- the steps

def _approval_words(promo: Mapping[str, Any]) -> str:
    approval = str(promo.get("approval") or "human")
    if approval == "auto":
        return "no approval"
    return _plural(int(promo.get("approvers") or 1), f"{approval} approval")


def guard(layer: Any, promo: Mapping[str, Any]) -> None:
    """The compiler's agent rule, again at run time (an IR written by hand, or by an older compiler):
    agents never approve, and nothing merges unapproved, into a stage where agents may not merge."""
    to_stage = delivery.stage_named(layer, str(promo["to"]))
    if promo.get("approval") in ("agent", "auto") and not to_stage.agents_may_merge:
        raise PromotionError("agents-not-allowed", f"#:approval {promo['approval']} into stage {to_stage.name}, where "
                                                   "agents may not merge (#:agents-may-merge #f); a person approves")


def _open(run: _Run) -> None:
    layer, promo, state = run.layer, run.promo, run.state
    guard(layer, promo)
    definition = run.adw.get("promotion") or {}
    from_branch, to_branch = str(definition["fromBranch"]), str(definition["toBranch"])
    other = open_promotion(layer, str(promo["from"]), str(promo["to"]))
    if other and other.get("id") != run.pid and not run.dry:
        raise PromotionError("already-open", f"promotion {other['id']} of {promo['from']} -> {promo['to']} is open; "
                                             "continue it with `al promote` (one promotion per pair at a time)")
    pinned = run.head("open")
    to_head = run.branch_head(to_branch, "open")
    if not to_head:
        raise PromotionError("stage-missing", f"branch {to_branch} of stage {promo['to']} does not exist")
    items, handed = changes(layer, from_branch, to_head, pinned)
    if not items:
        raise PromotionError("nothing-to-promote", f"no change delivered into {from_branch} waits to go into "
                                                   f"{to_branch}; nothing was opened")
    state.promotion = {"id": run.pid, "from": str(promo["from"]), "to": str(promo["to"]), "from_branch": from_branch,
                       "to_branch": to_branch, "pinned": pinned, "promoted": pinned, "from_head": pinned,
                       "to_head": to_head, "method": str(promo.get("method") or "merge"),
                       "approval": str(promo.get("approval") or "human"), "approvers": int(promo.get("approvers") or 1),
                       "requires": list(promo.get("requires") or []), "opened_at": _stamp(),
                       "changes": [dataclasses.asdict(c) for c in items],
                       "handed_over": [dataclasses.asdict(c) for c in handed],
                       "decisions_file": f"{RECORDS}/{run.pid}/{ACCEPTANCE}", "dry_run": run.dry, "lines": []}
    tag = str(promo.get("tag") or "")
    if tag and "{version}" not in tag:
        _check_tag(run, _filled_tag(run, ""), "open")
    elif tag:
        planned = _planned_version(run, items)
        if planned.get("to"):
            _check_tag(run, _filled_tag(run, str(planned["to"])), "open")
    if not run.dry:
        _mark_open(layer, {"id": run.pid, "from": promo["from"], "to": promo["to"], "from_branch": from_branch,
                           "to_branch": to_branch, "opened_at": state.promotion["opened_at"]})
    state.save(layer.layer_dir)
    events.emit("promotion.opened", **{"id": run.pid, "from": promo["from"], "to": promo["to"],
                                       "from_branch": from_branch, "to_branch": to_branch, "pinned": pinned,
                                       "changes": [c.id for c in items], "dry_run": run.dry})
    run.say(f"promotion {run.pid}: {from_branch} -> {to_branch} ({state.promotion['method']}; {_approval_words(promo)})"
            + (" — dry run" if run.dry else ""))
    open_count = sum(1 for c in items if c.status == "open")
    run.say(f"changes: {len(items)} delivered {_since(layer, promo)} ({open_count} to decide)"
            + (f"; {len(handed)} handed over, not included" if handed else ""))
    state.outputs["open"] = f"{len(items)} change(s) pinned at {pinned[:7]}"


def _check_commands(run: _Run) -> list[str]:
    names: list[str] = []
    for cmd in [*run.from_stage.checks, *run.to_stage.checks]:
        if cmd not in names:
            names.append(cmd)
    if not names:
        with contextlib.suppress(LayerError):
            option = run.layer.option("reviews", str(run.adw.get("review") or ""))
            names = [str(c) for c in option.get("requireChecks") or []]
    return names


def _run_checks(run: _Run, step: str) -> dict[str, Any]:
    from . import steps

    results: dict[str, Any] = {}
    for cmd in _check_commands(run):
        name = f"check-{cmd}"
        code = steps.run_check(run.layer, run.state, {"name": name, "kind": "check", "cmd": cmd}, run.env)
        results[cmd] = {"exit": code, "log": run.state.checks[name]["log"]}
    run.p["check_logs"] = results
    run.state.save(run.layer.layer_dir)
    failed = [f"{cmd}={data['exit']} (log {data['log']})" for cmd, data in results.items() if data["exit"] != 0]
    if failed:
        raise StepFailed(step, f"promotion:checks-failed: on {run.p['from_branch']}: {', '.join(failed)}")
    return results


def _checks(run: _Run) -> None:
    results = _run_checks(run, "checks")
    shown = ", ".join(f"{cmd}={data['exit']}" for cmd, data in results.items()) or "none declared"
    run.say(f"checks on {run.p['from_branch']} at {str(run.p['promoted'])[:7]}: {shown}")
    run.state.outputs["checks"] = shown


def _pack(run: _Run) -> None:
    path = build_pack(run)
    shown = path.relative_to(run.layer.layer_dir).as_posix()
    run.say(f"pack: {shown}")
    run.state.outputs["pack"] = shown


def _dashboard_address(layer: Any) -> str:
    option = layer.ir.get("dashboard") or {}
    if not isinstance(option, dict) or option.get("kind") in (None, "none"):
        return ""
    return f"http://{option.get('bind') or '127.0.0.1'}:{option.get('port') or 8765}/"


def _decide_lines(run: _Run) -> list[str]:
    promo = run.promo
    return [f"decide: al decide LAYER {promo['from']} {promo['to']} accept|refuse|approve [CHANGE …] --by NAME "
            "[--comment TEXT]",
            f"then:   al promote LAYER {promo['from']} {promo['to']}"]


def _request(run: _Run) -> None:
    layer, p = run.layer, run.p
    if run.dry:
        run.say("request: dry run, so no pull request and no approval item")
        return
    summary = (Path(layer.layer_dir) / str(p["summary_md"])).read_text(encoding="utf-8") if p.get("summary_md") else ""
    client = _client(run)
    if client is not None:
        from . import ship

        remote = delivery._remote(layer)
        listed = run.git(["ls-remote", remote, f"refs/heads/{p['from_branch']}"], "request").stdout.split()
        if not listed or listed[0] != p["promoted"]:
            pushed = run.git(["push", remote, f"{p['promoted']}:refs/heads/{p['from_branch']}"], "request")
            if pushed.returncode != 0:
                raise StepFailed("request", f"promotion:publish-failed: push of {p['from_branch']} refused: "
                                            f"{(pushed.stderr or pushed.stdout).strip()[:300]}")
        pr = client.create_pr(p["from_branch"], p["to_branch"], f"Promote {p['from_branch']} into {p['to_branch']}",
                              summary)
        if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
            raise StepFailed("request", f"forge create_pr returned an unexpected value: {pr!r}")
        p["pr"] = pr
        run.state.save(layer.layer_dir)
        ship.post_proof(layer, run.state, client, pr)
        if getattr(client, "can_attach", False) and p.get("pack"):
            with contextlib.suppress(Exception):
                url = client.attach(pr["number"], Path(layer.layer_dir) / p["pack"])
                if url:
                    client.comment(pr["number"], f"The presentation pack of promotion `{run.pid}`: [index.html]({url})")
        run.say(f"pull request: #{pr['number']} {pr.get('url', '')}".rstrip())
        return
    path = records_dir(layer, run.pid) / "approval.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    address = _dashboard_address(layer)
    text = [f"# Approval needed: {p['from_branch']} into {p['to_branch']}", "",
            f"Promotion `{run.pid}` waits for {_approval_words(run.promo)}.",
            f"The presentation pack: `{p.get('pack', '')}` (open it in a browser).", "", "## How to decide", "",
            "```", *(line.split(": ", 1)[1].strip() for line in _decide_lines(run)), "```", ""]
    if address:
        text += [f"Or on the dashboard: {address}", ""]
    text += ["---", "", summary]
    path.write_text("\n".join(text), encoding="utf-8")
    run.say(f"approval item: {path.relative_to(layer.layer_dir).as_posix()}")


COMMAND = re.compile(r"^\s*/(accept|refuse|approve)\b(.*)$", re.MULTILINE)


def _forge_decisions(run: _Run, client: Any, number: int) -> list[dict[str, Any]]:
    """Record `/accept ID…`, `/refuse ID… [revert|patch] comment` and `/approve` comments once each."""
    known = {c.id for c in run.change_list()}
    try:
        comments = client.list_comments(number)
    except Exception as exc:  # noqa: BLE001 — the forge's own failure: decide from what is recorded
        print(f"[adw {run.pid}] warning: could not read the pull request's comments: {exc}")
        return []
    recorded = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if client.is_bot(body):
            continue
        for match in COMMAND.finditer(body):
            verb, rest = match.group(1), match.group(2).split()
            ids = [token for token in rest if CHANGE_ID.match(token) and token in known]
            words = [token for token in rest if token not in ids]
            then = ""
            if verb == "refuse" and words and words[0] in THEN:
                then = words.pop(0)
            scope = ["all"] if verb == "approve" or not ids else ids
            with contextlib.suppress(PromotionError):
                record = record_decision(run.layer, run.pid, str(comment.get("user") or "forge"), verb, scope,
                                         " ".join(words), then, kind="forge",
                                         source=f"comment:{comment.get('id')}", known=known)
                if not record.get("duplicate"):
                    recorded.append(record)
    return recorded


def _agent_approval(run: _Run) -> None:
    """Independent reviewer agents on the whole stage diff and the pack (review.run_review on a
    run-scoped copy of the IR whose review option is `agent` with #:approvers reviewers)."""
    from . import review

    if any(d.get("kind") == "agent" and d.get("decision") == "approve" for d in decisions(run.layer, run.pid)):
        return
    layer = run.layer
    base_option: dict[str, Any] = {}
    with contextlib.suppress(LayerError):
        base_option = layer.option("reviews", str(run.adw.get("review") or ""))
    ir_copy = copy.deepcopy(layer.ir)
    option = {"id": "promotion-agents", "kind": "agent", "status": "stable",
              "reviewers": int(run.promo.get("approvers") or 1),
              "command": str(base_option.get("command") or "review"), "agent": str(base_option.get("agent") or ""),
              "maxRetries": 0, "artefacts": []}
    ir_copy["reviews"] = [*ir_copy.get("reviews", []), option]
    for adw in ir_copy.get("adws", []):
        if adw.get("name") == run.state.adw:
            adw["review"] = "promotion-agents"
    scoped = dataclasses.replace(layer, ir=ir_copy, delivery={**(layer.delivery or {}), "branch": run.p["to_branch"]})
    if run.p.get("summary_md"):
        run.state.plan = str(Path(layer.layer_dir) / run.p["summary_md"])
    try:
        verdict = review.run_review(scoped, run.state, {"name": "approval", "kind": "review"}, run.env)
    except StepFailed as exc:
        issues = "; ".join(str(i.get("description")) for i in (run.state.review.get("verdict") or {}).get("issues") or [])
        raise StepFailed("approval", f"promotion:agents-refused: the reviewer agents do not approve: "
                                     f"{issues or exc.reason}") from exc
    if not verdict.get("success"):
        raise StepFailed("approval", "promotion:agents-refused: the reviewer agents do not approve: "
                                     + "; ".join(str(i.get("description")) for i in verdict.get("issues") or []))
    for index in range(1, int(option["reviewers"]) + 1):
        record_decision(layer, run.pid, f"agent:review-{index}", "approve", ["all"],
                        str(verdict.get("summary") or "")[:MAX_COMMENT], kind="agent", source=f"review-{index}",
                        known=[c.id for c in run.change_list()])


def _refresh(run: _Run, forge_approvals: list[dict[str, Any]]) -> list[Change]:
    current = effective(decisions(run.layer, run.pid))
    items = run.change_list()
    for change in items:
        s = change_status(change, current, bool(forge_approvals))
        change.status, change.by, change.at, change.comment = s["status"], s["by"], s["at"], s["comment"]
        change.then = s["then"] or change.then
    run.save_changes(items)
    return items


def _approval(run: _Run) -> None:
    layer, promo, p = run.layer, run.promo, run.p
    forge_approvals: list[dict[str, Any]] = []
    client = _client(run)
    if client is not None and p.get("pr"):
        number = int(p["pr"]["number"])
        _forge_decisions(run, client, number)
        try:
            forge_approvals = list(client.pr_approvals(number))
        except Exception as exc:  # noqa: BLE001
            print(f"[adw {run.pid}] warning: could not read the pull request's reviews: {exc}")
    if promo.get("approval") == "agent" and not run.dry:
        _agent_approval(run)
    items = _refresh(run, forge_approvals)
    if not run.dry:
        for change in items:
            if change.status == "refused" and not change.excluded:
                _exclude(run, change)
        items = run.change_list()
    status = approval_status(layer, promo, decisions(layer, run.pid), items, forge_approvals)
    p["approval_status"] = status
    run.state.save(layer.layer_dir)
    if run.dry:
        run.say(f"approval: {status['why']} (dry run: not waited for)")
        return
    if not status["ready"]:
        events.emit("promotion.waiting", why=status["why"], needed=status["needed"], have=status["have"],
                    undecided=status["undecided"])
        run.say(f"waiting: {status['why']}")
        for line in _decide_lines(run):
            run.say(line)
        raise StepWaiting("approval", WAITING_EXIT, status["why"])
    events.emit("promotion.approved", approved_by=status["approved_by"], refused=status["refused"])
    run.say(f"approved: by {', '.join(status['approved_by']) or 'no one needed (auto)'}"
            + (f"; {_plural(len(status['refused']), 'change')} refused" if status["refused"] else ""))
    run.state.outputs["approval"] = f"approved by {', '.join(status['approved_by']) or 'auto'}"


def _exclude(run: _Run, change: Change) -> None:
    """Revert a refused change out of the stage (an agent round when the revert does not apply),
    re-run the checks, publish to FROM, rebuild the pack; `then: patch` also starts a patch run."""
    step = "approval"
    worktree = Path(run.state.worktree)
    patch = state_module.run_dir(run.layer.layer_dir, run.pid) / f"revert-{change.id}.patch"
    run.git(["diff", "--binary", f"--output={patch}", change.base, change.head], step, check=True)
    applied = run.git(["apply", "--3way", "-R", str(patch)], step)
    if applied.returncode != 0:
        files = sorted(set(_unmerged(run, step)) | set(_markers(worktree, change.files)))
        if not files and not run.git(["status", "--porcelain"], step).stdout.strip():
            files = list(change.files)
        print(f"[adw {run.pid}] promotion: the revert of {change.id} does not apply cleanly ({', '.join(files)}); "
              "an agent round resolves it")
        _agent_round(run, f"revert-{change.id}", f"revert change {change.id} ({', '.join(change.files)}) out of "
                     f"{run.p['from_branch']}; keep every other change", files, step, "revert-conflict")
    sha = _commit(run, f"Revert {change.id} out of {run.p['from_branch']}: refused by {change.by} [promotion {run.pid}]")
    if sha == run.p.get("promoted"):
        print(f"[adw {run.pid}] promotion: reverting {change.id} changed nothing (already out of the stage)")
    attempt_checks = _check_commands(run) if "checks" in (run.promo.get("requires") or []) else []
    if attempt_checks:
        _run_checks(run, step)
    run.p["promoted"] = sha
    _publish_from(run, sha, step)
    change.excluded, change.revert = "reverted", sha
    items = [change if c.id == change.id else c for c in run.change_list()]
    run.save_changes(items)
    if change.then == "patch":
        change.excluded = "patch"
        change.patch_run = _start_patch_run(run.layer, change, change.comment, sha, run.pid, run.p["from_branch"])
        items = [change if c.id == change.id else c for c in run.change_list()]
        run.save_changes(items)
    build_pack(run)
    events.emit("promotion.excluded", change=change.id, by=change.by, comment=change.comment[:500],
                then=change.then or "revert", revert=sha, patch_run=change.patch_run or None)
    run.say(f"excluded: {change.id} reverted out of {run.p['from_branch']} (refused by {change.by}"
            + (f": \"{change.comment}\"" if change.comment else "") + ")"
            + (f"; patch run {change.patch_run.get('run_id')} started" if change.patch_run.get("run_id") else ""))


def _start_patch_run(layer: Any, change: Change, comment: str, revert_sha: str, promotion_id: str = "",
                     from_branch: str = "") -> dict[str, Any]:
    """A patch run of the refused change's workflow, with the person's comment."""
    from . import launch

    prompt = (f"Patch round for change {change.id} refused in promotion {promotion_id} by {change.by}: {comment}. "
              f"Original request: {first_line(change.prompt)}. The change was reverted out of {from_branch} by "
              f"{revert_sha}: start with `git revert {revert_sha}`, then address the comment.")
    try:
        result = launch.start(layer, launch.Target("workflow", change.adw, ""), prompt,
                              request_key=f"promotion-{promotion_id}-{change.id}", klass="patch")
    except Exception as exc:  # noqa: BLE001 — the revert stands; the patch run is a courtesy to the team
        return {"run_id": "", "status": "error", "error": str(exc)[:300]}
    return {"run_id": str(result.get("run_id") or ""), "status": str(result.get("status") or "")}


def _accepted(run: _Run) -> list[Change]:
    return [c for c in run.change_list() if not c.excluded and c.status != "refused"]


def _filled_tag(run: _Run, version: str) -> str:
    cycle = delivery.load_cycle(run.layer) or {}
    name = str((cycle.get("current") or {}).get("name") or "") if cycle.get("stage") == run.promo.get("from") else ""
    return fill_tag(str(run.promo.get("tag") or ""), version, name, _now().strftime("%Y-%m-%d"))


def _check_tag(run: _Run, tag: str, step: str) -> None:
    if not tag:
        return
    if run.app_git(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], step).returncode == 0:
        raise PromotionError("tag-exists", f"tag {tag} already exists; nothing was merged (raise the version or "
                                           "change #:tag)")


def _version(run: _Run) -> None:
    p, promo = run.p, run.promo
    accepted = _accepted(run)
    if not accepted:
        run.say("version: unchanged (no change was accepted)")
        return
    version = promo.get("version") or {}
    worktree = Path(run.state.worktree)
    current, source = read_version(worktree, str(p.get("to_head") or "HEAD"), git=lambda args: run.git(args, "version"))
    new = bump(current, str(version.get("level") or "patch")) if version.get("how") == "bump" else current
    kind, name = changelog_kind(worktree)
    date = _now().strftime("%Y-%m-%d")
    p["version"] = {"from": current, "to": new, "source": source, "changelog": name, "changelog_kind": kind}
    _check_tag(run, _filled_tag(run, new), "version")
    if run.dry:
        run.say(f"would set version {new} in {source if source in VERSION_FILES else 'a tag (no version file)'}")
        if kind:
            heading = _first_version_heading((worktree / name).read_text(encoding="utf-8")) if kind == "plain" else ""
            run.say(f"would add to {name}:\n{changelog_entry(kind, new, date, accepted, heading)}".rstrip())
        else:
            run.say("no changelog detected: nothing would be added")
        return
    wrote = write_version(worktree, source, new) if new != current else False
    entry = insert_changelog(worktree, name, kind, new, date, accepted) if kind else ""
    if wrote or entry:
        sha = _commit(run, f"Release {new} [promotion {run.pid}]")
        p["promoted"] = sha
        _publish_from(run, sha, "version")
    where = f"({source})" if source in VERSION_FILES else f"({source or 'no version file'}; the tag carries it)"
    cl = f"{name}: {kind} entry with {_plural(len(accepted), 'change')}" if kind else "no changelog"
    run.say(f"version: {current} -> {new} {where}; {cl}")
    run.state.outputs["version"] = f"{current} -> {new}"
    run.state.save(run.layer.layer_dir)


def _merge(run: _Run) -> None:
    p, promo, step = run.p, run.promo, "merge"
    method = str(promo.get("method") or "merge")
    accepted = _accepted(run)
    if not accepted:
        p["result"] = ""
        run.say(f"merged: nothing — every change was refused, {p['to_branch']} is unchanged")
        events.emit("promotion.merged", method=method, result="", to_branch=p["to_branch"], staged=False, nothing=True)
        return
    promoted = str(p["promoted"])
    if run.dry:
        run.say(f"would merge {p['from_branch']} ({promoted[:7]}) into {p['to_branch']} by {method}")
        return
    to_head = run.branch_head(str(p["to_branch"]), step)
    client = _client(run)
    title = f"Promote {p['from_branch']} into {p['to_branch']} [promotion {run.pid}]"
    staged = False
    if client is not None and p.get("pr") and method in ("merge", "squash"):
        # the pull request's head is FROM: it must carry the reverts and the version commit
        listed = run.git(["ls-remote", delivery._remote(run.layer), f"refs/heads/{p['from_branch']}"], step).stdout.split()
        if not listed or listed[0] != promoted:
            _publish(run, str(p["from_branch"]), promoted, "", step)
        client.merge_pr(int(p["pr"]["number"]), method)
        result = run.branch_head(str(p["to_branch"]), step)
    else:
        run.git(["checkout", "--detach", to_head], step, check=True)
        if method == "merge":
            done = run.git([*IDENTITY, "merge", "--no-ff", "-m", title, promoted], step)
            if done.returncode != 0:
                _resolve_merge(run, step, "merge", f"merge {p['from_branch']} into {p['to_branch']}")
        elif method == "squash":
            done = run.git([*IDENTITY, "merge", "--squash", promoted], step)
            if done.returncode != 0:
                _resolve_merge(run, step, "merge", f"squash {p['from_branch']} into {p['to_branch']}", commit=False)
            _commit(run, title)
        elif method == "fast-forward":
            if not _is_ancestor_in(run, to_head, promoted, step):
                raise PromotionError("not-fast-forward", f"{p['to_branch']} moved since {p['from_branch']} was cut; "
                                                         "it cannot be fast-forwarded; merge it or use #:method merge")
            run.git(["checkout", "--detach", promoted], step, check=True)
        elif method == "cherry-pick":
            picks = run.git(["rev-list", "--reverse", "--no-merges", f"{to_head}..{promoted}"], step,
                            check=True).stdout.split()
            for sha in picks:
                done = run.git([*IDENTITY, "cherry-pick", "-x", sha], step)
                if done.returncode != 0:
                    if "empty" in (done.stderr + done.stdout) and not _unmerged(run, step):
                        run.git(["cherry-pick", "--skip"], step)
                        continue
                    _resolve_merge(run, step, "merge", f"cherry-pick {sha[:12]} onto {p['to_branch']}")
        result = run.head(step)
        _where, staged = _publish(run, str(p["to_branch"]), result, to_head, step)
    p["result"] = result
    p["staged"] = staged
    run.state.save(run.layer.layer_dir)
    events.emit("promotion.merged", method=method, result=result, to_branch=p["to_branch"], staged=staged)
    run.say(f"merged: {p['from_branch']} into {p['to_branch']} by {method} at {result[:7]}"
            + (f" (staged on al/promote/{p['to_branch']})" if staged else ""))
    run.state.outputs["merge"] = f"{method} at {result[:7]}"


def _resolve_merge(run: _Run, step: str, prefix: str, what: str, commit: bool = True) -> None:
    files = _unmerged(run, step)
    _agent_round(run, prefix, f"{what}: resolve the conflicts in {', '.join(files)}", files, step, "merge-conflict")
    if commit:
        _commit(run, f"{what} [promotion {run.pid}]")


def _tag(run: _Run) -> None:
    p = run.p
    version = str((p.get("version") or {}).get("to") or (p.get("planned_version") or {}).get("to") or "")
    tag = _filled_tag(run, version)
    if not p.get("result") and not run.dry:
        run.say("tagged: nothing (nothing was merged)")
        return
    if run.dry:
        run.say(f"would tag {tag}")
        return
    _check_tag(run, tag, "tag")
    run.app_git([*IDENTITY, "tag", "-a", tag, str(p["result"]), "-m", f"{tag} [promotion {run.pid}]"], "tag",
                check=True)
    remote = delivery._remote(run.layer)
    if remote:
        pushed = run.app_git(["push", remote, f"refs/tags/{tag}"], "tag")
        if pushed.returncode != 0:
            print(f"[adw {run.pid}] warning: tag {tag} was not pushed: {(pushed.stderr or '').strip()[:200]}")
    p["tag"] = tag
    run.state.save(run.layer.layer_dir)
    events.emit("promotion.tagged", tag=tag, commit=p["result"])
    run.say(f"tagged: {tag}")


def _deploy(run: _Run) -> None:
    from . import steps

    p, to_stage = run.p, run.to_stage
    after = list(run.promo.get("after") or [])
    env_name = to_stage.environment_name or (after[1] if len(after) > 1 else "")
    if not p.get("result") and not run.dry:
        run.say("deploy: nothing (nothing was merged)")
        return
    if run.dry:
        run.say(f"would deploy {p['to_branch']} with {to_stage.deploy_cmd}" + (f" to {env_name}" if env_name else ""))
        return
    run.git(["checkout", "--detach", str(p["result"])], "deploy", check=True)
    code = steps.run_check(run.layer, run.state, {"name": "deploy", "kind": "check", "cmd": to_stage.deploy_cmd}, run.env)
    p["deploy"] = {"environment": env_name, "exit": code, "log": run.state.checks["deploy"]["log"]}
    run.state.save(run.layer.layer_dir)
    events.emit("promotion.deployed", environment=env_name, exit=code)
    if code != 0:
        raise StepFailed("deploy", f"promotion:deploy-failed: {to_stage.deploy_cmd} exited {code} (log "
                                   f"{p['deploy']['log']}); {p['to_branch']} is merged"
                                   + (f" and tagged {p.get('tag')}" if p.get("tag") else "")
                                   + "; a person deploys, then runs `al promote` again to finish")
    run.say(f"deployed: {p['to_branch']} to {env_name or 'its environment'} (exit 0)")


def _back_merge_branches(run: _Run, stage: delivery.Stage) -> list[str]:
    cycle = delivery.load_cycle(run.layer)
    if "{date}" in stage.branch and "{cycle}" not in stage.branch:
        return _date_branches(run.layer, stage.branch, str(run.p["to_branch"]))
    try:
        branch = delivery.expand_branch(stage, cycle)
    except delivery.DeliveryError:
        return []
    return [branch] if _ref(run.app, f"refs/heads/{branch}") or delivery._remote(run.layer) else []


def _back_merge(run: _Run) -> None:
    from . import steps

    p, step = run.p, "back-merge"
    result = str(p.get("result") or "")
    done: list[dict[str, Any]] = list(p.get("back_merges") or [])
    if not result and not run.dry:
        run.say("back-merge: nothing (nothing was merged)")
        return
    for name in run.promo.get("backMerge") or []:
        stage = delivery.stage_named(run.layer, str(name))
        branches = _back_merge_branches(run, stage)
        if not branches:
            note = f"back-merge into stage {stage.name} skipped: it has no open branch ({stage.branch})"
            p.setdefault("notes", []).append(note)
            run.say(note)
            continue
        for branch in branches:
            if any(d.get("branch") == branch for d in done):
                continue
            if run.dry:
                run.say(f"would back-merge {p['to_branch']} into {branch}")
                continue
            head = run.branch_head(branch, step)
            run.git(["checkout", "--detach", head], step, check=True)
            merged = run.git([*IDENTITY, "merge", "--no-ff", "-m",
                              f"Back-merge {p['to_branch']} into {branch} [promotion {run.pid}]", result], step)
            conflict = merged.returncode != 0
            if conflict:
                files = _unmerged(run, step)
                _agent_round(run, f"back-merge-{stage.name}", f"back-merge {p['to_branch']} into {branch}: resolve the "
                             f"conflicts in {', '.join(files)} keeping both sides' intent", files, step,
                             "back-merge-conflict")
                _commit(run, f"Back-merge {p['to_branch']} into {branch} [promotion {run.pid}]")
                for cmd in stage.checks:
                    code = steps.run_check(run.layer, run.state, {"name": f"back-merge-{stage.name}-{cmd}",
                                                                   "kind": "check", "cmd": cmd}, run.env)
                    if code != 0:
                        raise StepFailed(step, f"promotion:back-merge-conflict: check {cmd} exits {code} after the "
                                               f"back-merge into {branch}; a person finishes it")
            commit = run.head(step)
            if stage.agents_may_merge:
                where, _staged = _publish(run, branch, commit, head, step)
                published = where
            else:
                side = f"al/back-merge/{run.pid}-{stage.name}"
                where, _staged = _publish(run, side, commit, "", step)
                published = where
                client = _client(run)
                if client is not None:
                    with contextlib.suppress(Exception):
                        pr = client.create_pr(side, branch, f"Back-merge {p['to_branch']} into {branch}",
                                              f"Promotion `{run.pid}` merged {p['to_branch']}; this brings it back into "
                                              f"{branch}, which only changes by people.")
                        published = f"{where} (PR #{pr.get('number')})"
                else:
                    note = f"{branch} only changes by people: the back-merge is on {side}; merge it there"
                    p.setdefault("notes", []).append(note)
            record = {"stage": stage.name, "branch": branch, "conflict": conflict, "resolved": conflict,
                      "published": published, "commit": commit}
            done.append(record)
            p["back_merges"] = done
            run.state.save(run.layer.layer_dir)
            events.emit("promotion.back-merge", stage=stage.name, branch=branch, conflict=conflict, resolved=conflict,
                        published=published)
            run.say(f"back-merged: {p['to_branch']} into {branch}" + (" (conflict resolved by an agent round)"
                                                                       if conflict else "") + f" -> {published}")


def _close(run: _Run) -> None:
    layer, p, promo = run.layer, run.p, run.promo
    if run.dry:
        run.say("dry run: nothing was recorded, merged or tagged")
        return
    current = effective(decisions(layer, run.pid))
    closed_at = _stamp()
    items = run.change_list()
    record = {"id": run.pid, "from": promo["from"], "to": promo["to"],
              "branches": {"from": p["from_branch"], "to": p["to_branch"]}, "pinned": p.get("pinned"),
              "result": p.get("result") or "", "method": p.get("method"), "version": p.get("version") or None,
              "tag": p.get("tag") or "", "opened_at": p.get("opened_at"), "closed_at": closed_at,
              "changes": [{"id": c.id, "status": c.status, "excluded": c.excluded, "by": c.by, "at": c.at}
                          for c in items],
              "approvals": [{"by": d["by"], "kind": d.get("kind"), "at": d["at"]} for d in current
                            if d.get("decision") == "approve"],
              "deploy": p.get("deploy") or None, "back_merges": p.get("back_merges") or [], "pack": p.get("pack") or ""}
    directory = records_dir(layer, run.pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "promotion.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    p["closed_at"] = closed_at
    render_sign_off(layer, run.pid, p)
    _unmark(layer, str(promo["from"]), str(promo["to"]), run.pid)
    cycle = delivery.load_cycle(layer) or {}
    if cycle.get("stage") == promo["from"] and cycle.get("current") and p.get("result") and not p.get("staged"):
        with contextlib.suppress(LayerError):
            closed = delivery.cycle_close(layer)["closed"]
            p["cycle"] = {"name": closed.get("name"), "accepted": closed.get("accepted")}
            run.say(f"closed: cycle {promo['from']} {closed.get('name')} "
                    f"({'accepted' if closed.get('accepted') else 'not accepted'})")
    waiting = []
    for data in _load_states(layer):
        if data.get("status") == "waiting" and (data.get("delivery") or {}).get("stage") == promo["from"] \
                and data.get("adw_id") != run.pid:
            with contextlib.suppress(LayerError):
                from . import runner

                waiting.append({"id": data["adw_id"],
                                "resume": runner.resume_command(state_module.load(layer.layer_dir, data["adw_id"]))})
    p["waiting_runs"] = waiting
    for item in waiting:
        note = f"run {item['id']} waited to deliver into {promo['from']}; resume it: {item['resume']}"
        p.setdefault("notes", []).append(note)
        run.say(note)
    if "cycle" not in p:
        run.say(f"closed: promotion {run.pid}")
    run.state.save(layer.layer_dir)
    events.emit("promotion.closed", id=run.pid, result=record["result"], tag=record["tag"],
                changes={c.id: c.status for c in items})


ACTIONS = {"open": _open, "checks": _checks, "pack": _pack, "request": _request, "approval": _approval,
           "version": _version, "merge": _merge, "tag": _tag, "deploy": _deploy, "back-merge": _back_merge,
           "close": _close}


def run_step(layer: Any, state: Any, adw: Mapping[str, Any], step: Mapping[str, Any], env_stack: Any) -> None:
    """One step of a promotion run (steps._execute_step dispatches kind `promote` here)."""
    definition = adw.get("promotion") or {}
    promo = promotion_for(layer, str(definition.get("from")), str(definition.get("to")))
    action = str(step.get("action") or step.get("name"))
    if action not in ACTIONS:
        raise LayerError(f"unknown promotion step {action!r}; steps are {', '.join(STEPS)}")
    run = _Run(layer, state, dict(adw), env_stack, dict(promo))
    try:
        ACTIONS[action](run)
    except StepFailed as exc:
        state.promotion["stopped"] = {"step": action, "reason": exc.reason[:2000]}
        print(f"promotion stopped at {action}: {exc.reason}; a person looks at it, then runs `al promote` again to "
              "continue")
        raise
    finally:
        state.save(layer.layer_dir)


# ---------------------------------------------------------------- entry points

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="promote", description="Promote one stage into the next.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="start or continue the promotion FROM -> TO")
    run.add_argument("from_", metavar="FROM")
    run.add_argument("to", metavar="TO")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--branch", default="")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--adw-id", default="")
    run.add_argument("--detach", action="store_true")
    run.add_argument("--json", action="store_true")
    decide = sub.add_parser("decide", help="record a decision on the open promotion")
    decide.add_argument("from_", metavar="FROM")
    decide.add_argument("to", metavar="TO")
    decide.add_argument("decision", choices=DECISIONS)
    decide.add_argument("changes", nargs="*")
    decide.add_argument("--by", default="")
    decide.add_argument("--comment", default="")
    decide.add_argument("--then", choices=THEN, default="")
    decide.add_argument("--json", action="store_true")
    status = sub.add_parser("status", help="the open promotion's approval in words")
    status.add_argument("from_", metavar="FROM")
    status.add_argument("to", metavar="TO")
    status.add_argument("--json", action="store_true")
    return parser


def _spawn(argv: list[str], **popen: Any) -> subprocess.Popen:
    return subprocess.Popen(argv, **popen)


def run(layer_dir: Path, from_: str, to: str, *, dry_run: bool = False, branch: str = "", resume: bool = False,
        adw_id: str = "", detach: bool = False, as_json: bool = False, spawn: Any = _spawn) -> int:
    """Start (or continue) the promotion FROM -> TO as a run; returns its exit code (0 passed,
    1 failed, 75 waiting)."""
    from . import runner

    layer_dir = Path(layer_dir)
    layer = ir.load(layer_dir)
    promo = promotion_for(layer, from_, to)
    marker = open_promotion(layer, from_, to)
    definition: dict[str, Any] | None = None
    if marker and not resume and not dry_run:
        adw_id, resume = str(marker["id"]), True
        print(f"continuing promotion {adw_id} (opened {str(marker.get('opened_at') or '')[:10]})", flush=True)
    if resume:
        if not adw_id:
            raise PromotionError("not-open", f"--resume needs --adw-id; no promotion {from_} -> {to} is open")
        saved = _promotion_state(layer, adw_id)
        definition = saved.get("definition") if isinstance(saved.get("definition"), dict) else None
        if not definition:
            raise PromotionError("not-open", f"run {adw_id} is not a promotion run of {from_} -> {to}")
    else:
        from_branch, to_branch = resolve_branches(layer, promo, branch or None)
        definition = promotion_adw(layer, promo, from_branch, to_branch)
        if not adw_id:
            adw_id = state_module.new_adw_id()
            while state_module.run_dir(layer_dir, adw_id).exists():
                adw_id = state_module.new_adw_id()
    flags = [*(["--resume"] if resume else []), *(["--dry-run"] if dry_run else [])]
    if detach:
        script = layer_dir / "adws" / "promote.py"
        argv = [sys.executable, str(script), "run", from_, to, "--adw-id", adw_id, *flags]
        if branch and not resume:
            argv += ["--branch", branch]
        run_dir = state_module.run_dir(layer_dir, adw_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "launch.log").open("ab") as log:
            spawn(argv, cwd=str(layer_dir), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                  start_new_session=True)
        result = {"status": "started", "run_id": adw_id, "resumed": resume}
        print(json.dumps(result) if as_json else f"started promotion run {adw_id}; log agents/{adw_id}/launch.log")
        return 0
    prompt = f"promote {definition['promotion']['fromBranch']} -> {definition['promotion']['toBranch']}"
    code = runner.main(definition["name"], layer_dir, [prompt, "--class", "chore", "--adw-id", adw_id, *flags],
                       definition=definition)
    if as_json:
        saved = _promotion_state(layer, adw_id)
        print(json.dumps({"status": saved.get("status", ""), "run_id": adw_id, "exit": code}))
    return code


def _status_words(layer: Any, promo: Mapping[str, Any], promotion_id: str) -> dict[str, Any]:
    info = _promotion_state(layer, promotion_id).get("promotion") or {}
    status = approval_status(layer, promo, decisions(layer, promotion_id), info.get("changes") or [])
    return {"id": promotion_id, "from": promo["from"], "to": promo["to"], **status}


def decide(layer_dir: Path, from_: str, to: str, decision: str, change_ids: list[str], by: str, comment: str = "",
           then: str = "", as_json: bool = False) -> int:
    layer = ir.load(Path(layer_dir))
    promo = promotion_for(layer, from_, to)
    marker = open_promotion(layer, from_, to)
    if not marker:
        raise PromotionError("not-open", f"no promotion {from_} -> {to} is open; start one with "
                                         f"`al promote LAYER {from_} {to}`")
    # outside the run, so the decision is appended to the promotion run's own events here
    with events.recording(layer.layer_dir, str(marker["id"])):
        record = record_decision(layer, str(marker["id"]), by, decision, change_ids or ["all"], comment, then)
    status = _status_words(layer, promo, str(marker["id"]))
    if as_json:
        print(json.dumps({"status": "recorded", "promotion": marker["id"], "record": record, "approval": status}))
        return 0
    what = ", ".join(record["scope"])
    print(f"recorded: {record['by']} {record['decision']}s {what}" + (f" (then {record['then']})" if record["then"] else "")
          + (f": \"{record['comment']}\"" if record["comment"] else ""))
    if status["ready"]:
        print(f"ready: run `al promote LAYER {from_} {to}` to continue")
    else:
        print(f"waiting: {status['why']}")
    return 0


def main(layer_dir: Path, argv: list[str] | None = None, prog: str = "") -> int:
    """`adws/promote.py run|decide|status …` (and `al promote` / `al decide`, which pass prog="al ")."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    name = f"{prog}{'decide' if args.command == 'decide' else 'promote'}"
    try:
        if args.command == "run":
            return run(Path(layer_dir), args.from_, args.to, dry_run=args.dry_run, branch=args.branch,
                       resume=args.resume, adw_id=args.adw_id, detach=args.detach, as_json=args.json)
        if args.command == "decide":
            return decide(Path(layer_dir), args.from_, args.to, args.decision, list(args.changes), args.by,
                          args.comment, args.then, args.json)
        layer = ir.load(Path(layer_dir))
        promo = promotion_for(layer, args.from_, args.to)
        marker = open_promotion(layer, args.from_, args.to)
        if not marker:
            raise PromotionError("not-open", f"no promotion {args.from_} -> {args.to} is open")
        status = _status_words(layer, promo, str(marker["id"]))
        print(json.dumps(status) if args.json else
              f"promotion {marker['id']}: {status['why']}" + (f"; approved by {', '.join(status['approved_by'])}"
                                                              if status["approved_by"] else ""))
        return 0
    except PromotionError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "reason": exc.reason, "error": exc.message}))
        print(f"{name}: {exc.reason}: {exc.message}", file=sys.stderr)
        return 1
    except LayerError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "reason": "error", "error": str(exc)}))
        print(f"{name}: error: {exc}", file=sys.stderr)
        return 1
