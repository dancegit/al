"""The delivery request: after every hands-off cycle, one request for what is to be delivered next.

`after_run(layer, state, adw)` is called once at the end of every run (runner._run). Promotion,
integration and verify-batch are ordinary runs of synthetic workflows, so this one hook covers a
zte run that merged into its stage, an integration run that landed, a batch that proved a stage
head and a promotion that moved a stage. Source and target come only from the declarations
(`delivery.stages`, `Stage.next`, `delivery.expand_branch`, `delivery.pull_requests`, the forge):

    a hands-off run that did not merge     its own request (ship.py's PR or PR.md), adopted
    a run / integration merged into S      S's branch into the next stage's branch
    a promotion that moved TO              TO's branch into TO's next stage
    a promotion waiting for approval       its own request (the `request` step's), adopted
    a batch on S                           S into its next stage, with the batch's results
    S is the final stage                   nothing (recorded as `none`)
    the source is not ahead                nothing new (`nothing-to-deliver`)
    a dry run                              skipped, and why

With a github or forgejo forge and pull requests, the request is a pull request found by head and
base: an open one carrying MARKER is updated in place, an open one without it is a person's and is
left alone, merged and closed ones are never touched; a new one is opened only when none is open.
Otherwise the same title and body go to agents/requests/<from>-<to>.md (and .json, the record the
dashboard reads). al never merges a request and never pushes a branch for one.

`describe(ir, adw)` is the sentence `al explain` says per workflow; it reads only IR dicts, so this
module imports its siblings inside the functions that need them (tools/explain.py loads it by path).
Standard library only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

RECORDS = "agents/requests"
MARKER = "<!-- al:delivery-request {key} -->"
FORGE_KINDS = ("github", "forgejo")


# ---------------------------------------------------------------- al explain

def _routes(ir: Mapping[str, Any], adw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stages a run of `adw` delivers into, one per distinct stage, in class order."""
    stages = [s for s in ir.get("stages") or [] if isinstance(s, dict)]
    if not stages:
        return []
    final = next((s for s in stages if not (s.get("into") or s.get("from"))), stages[-1])
    if adw.get("deliverTo"):
        return [next((s for s in stages if s.get("name") == adw["deliverTo"]), final)]
    found: list[dict[str, Any]] = []
    for klass in adw.get("classes") or ("chore", "bug", "feature", "patch"):
        stage = next((s for s in stages if klass in (s.get("accepts") or ())), final)
        if stage not in found:
            found.append(stage)
    return found


def _forge_requests(ir: Mapping[str, Any]) -> bool:
    forge = ir.get("forge") or {}
    return forge.get("kind") in FORGE_KINDS and forge.get("pullRequests", True) is not False


def _hands_off_ir(ir: Mapping[str, Any], adw: Mapping[str, Any]) -> bool:
    review = next((r for r in ir.get("reviews") or [] if isinstance(r, dict) and r.get("id") == adw.get("review")), {})
    return review.get("kind") == "auto" and review.get("ship") in ("merge", "deploy")


def describe(ir: Mapping[str, Any], adw: Mapping[str, Any]) -> str:
    """One sentence: what a run of `adw` leaves as its delivery request. "" without a ship step."""
    if not any(isinstance(s, dict) and s.get("kind") == "ship" for s in adw.get("steps") or []):
        return ""
    stages = {s.get("name"): s for s in ir.get("stages") or [] if isinstance(s, dict)}
    forge = _forge_requests(ir)
    run_medium = "a pull request on the forge" if forge else "`agents/<run id>/PR.md`"
    own = "its own pull request on the forge" if forge else "its own description file (`agents/<run id>/PR.md`)"
    if not stages:
        return f"Its delivery request: {own}; nothing further, the layer declares no stages."
    if not _hands_off_ir(ir, adw):
        targets = " or ".join(f"`{s.get('branch')}`" for s in _routes(ir, adw))
        return f"Its delivery request: {own}, aimed at {targets}, which a person decides."
    parts = []
    for stage in _routes(ir, adw):
        branch = str(stage.get("branch") or "")
        nxt = stages.get(stage.get("into") or stage.get("from") or "")
        text = f"when it does not merge, its branch into `{branch}` ({run_medium}); after it merged, "
        if nxt is None:
            text += f"nothing further, `{branch}` is the final stage"
        else:
            key = f"{stage.get('name')}-{nxt.get('name')}"
            medium = "a pull request on the forge" if forge else f"the file `{RECORDS}/{key}.md`"
            text += (f"`{branch}` into `{nxt.get('branch')}` as a promotion request ({medium}), opened or updated "
                     "and never merged by al")
        parts.append(text)
    return "Its delivery request: " + "; and ".join(parts) + "."


# ---------------------------------------------------------------- what the cycle requests

def hands_off(layer: Any, adw: Mapping[str, Any], state: Any) -> bool:
    from . import delivery

    if state.promotion or state.batch or state.integration:
        return True
    try:
        return delivery._ships_untouched(layer, adw)
    except Exception:  # noqa: BLE001 — an unknown review option: not a hands-off workflow
        return False


def _record_key(from_stage: str, to_stage: str) -> str:
    return f"{from_stage}-{to_stage}"


def _branch_of(layer: Any, name: str) -> str:
    from . import delivery

    stage = delivery.stage_named(layer, name)
    try:
        return delivery.expand_branch(stage, delivery.load_cycle(layer))
    except delivery.DeliveryError:
        return stage.branch


def _moved_stage(state: Any) -> str:
    promo = state.promotion or {}
    if promo:
        return str(promo.get("to") or "") if promo.get("result") and not promo.get("staged") else ""
    if state.batch:
        return str(state.batch.get("stage") or "")
    d = state.delivery or {}
    if d.get("stage") and (d.get("delivered") or (state.ship or {}).get("shipped")):
        return str(d["stage"])
    return ""


def next_request(layer: Any, adw: Mapping[str, Any], state: Any) -> dict[str, Any] | None:
    """What this cycle requests: {kind: run | promotion, source, target, …}, or None."""
    from . import delivery

    ship = state.ship or {}
    promo = state.promotion or {}
    if not promo and not state.batch and not ship.get("shipped") and (
            ship.get("pr") or str(ship.get("target") or "").endswith("PR.md")):
        out = {"kind": "run", "source": state.branch,
               "target": (state.delivery or {}).get("branch") or str(layer.default_branch),
               "state": "open", "action": "adopted", "medium": "forge" if ship.get("pr") else "file"}
        pr = ship.get("pr") if isinstance(ship.get("pr"), dict) else {}
        if pr.get("number"):
            out.update(number=int(pr["number"]), url=str(pr.get("url") or ""))
        else:
            out["path"] = str(ship.get("target") or "")
        return out
    if promo and not promo.get("result"):
        out = {"kind": "promotion", "from": promo.get("from"), "to": promo.get("to"),
               "source": promo.get("from_branch"), "target": promo.get("to_branch"), "state": "open",
               "action": "adopted"}
        pr = promo.get("pr") if isinstance(promo.get("pr"), dict) else {}
        if pr.get("number"):
            return {**out, "medium": "forge", "number": int(pr["number"]), "url": str(pr.get("url") or "")}
        approval = Path(layer.layer_dir) / "promotions" / str(state.adw_id) / "approval.md"
        if approval.is_file():
            return {**out, "medium": "file", "path": approval.relative_to(layer.layer_dir).as_posix()}
        return None
    moved = _moved_stage(state)
    if not moved:
        return None
    following = delivery.stage_named(layer, moved).next
    if not following:
        return {"none": f"{_branch_of(layer, moved)} is the final stage"}
    return {"kind": "promotion", "from": moved, "to": following,
            "source": _branch_of(layer, moved), "target": _branch_of(layer, following)}


def ahead(layer: Any, source: str, target: str) -> int:
    """How many commits `source` has that `target` lacks (on the remote when there is one); 0 on error.
    A remote's branches are fetched into refs/al/request/* (al's own refs, deleted afterwards)."""
    from . import delivery

    app = Path(layer.app_root)
    remote = delivery._remote(layer)
    refs = {"source": f"refs/heads/{source}", "target": f"refs/heads/{target}"}
    fetched = []
    try:
        if remote:
            for name, branch in (("source", source), ("target", target)):
                ref = f"refs/al/request/{name}"
                if delivery._git(app, ["fetch", "--quiet", "--no-tags", remote,
                                       f"+refs/heads/{branch}:{ref}"]).returncode == 0:
                    refs[name] = ref
                    fetched.append(ref)
        out = delivery._git(app, ["rev-list", "--count", f"{refs['target']}..{refs['source']}"])
        return int(out.stdout.strip() or 0) if out.returncode == 0 else 0
    except Exception:  # noqa: BLE001 — not knowing counts as not ahead
        return 0
    finally:
        for ref in fetched:
            delivery._git(app, ["update-ref", "-d", ref])


# ---------------------------------------------------------------- where the request goes

def mark(body: str, key: str) -> str:
    """`body` ending with al's marker for request `key` (once)."""
    marker = MARKER.format(key=key.replace("-", "->", 1))
    return body if marker in body else body.rstrip() + "\n\n" + marker + "\n"


def open_or_update(layer: Any, client: Any, head: str, base: str, title: str, body: str, key: str) -> dict[str, Any]:
    """Update al's open request head → base in place, leave a person's alone, else open one.
    {number, url, state: open, action: updated | left-person | opened}. Never merges, comments or closes."""
    body = mark(body, key)
    marker = MARKER.format(key=key.replace("-", "->", 1))
    found = client.find_prs(head, base)
    open_ = [p for p in found if p.get("state") == "open"]
    ours = next((p for p in open_ if marker in str(p.get("body") or "")), None)
    if ours is not None:
        client.update_pr(int(ours["number"]), title, body)
        return {"number": int(ours["number"]), "url": str(ours.get("url") or ""), "state": "open", "action": "updated"}
    if open_:
        person = open_[0]
        return {"number": int(person["number"]), "url": str(person.get("url") or ""), "state": "open",
                "action": "left-person"}
    pr = client.create_pr(head, base, title, body)
    return {"number": int(pr.get("number") or 0), "url": str(pr.get("url") or ""), "state": "open", "action": "opened"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _records(layer: Any) -> Path:
    return Path(layer.layer_dir) / RECORDS


def write_record(layer: Any, key: str, record: Mapping[str, Any]) -> Path:
    """agents/requests/<key>.json: the request's record (what the dashboard reads)."""
    path = _records(layer) / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in dict(record).items() if k != "body"}
    data.setdefault("updated_at", _now())
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_file(layer: Any, key: str, title: str, body: str, record: dict[str, Any]) -> Path:
    """agents/requests/<key>.md (title, body, marker) and its .json; `record` gets path, medium, state,
    action (written when new, updated when present) and updated_at. Never deletes either file."""
    path = _records(layer) / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    action = "updated" if path.exists() else "written"
    path.write_text(f"# {title}\n\n{mark(body, key)}", encoding="utf-8")
    record.update(path=path.relative_to(layer.layer_dir).as_posix(), medium="file", state="open", action=action,
                  title=title, updated_at=_now())
    write_record(layer, key, record)
    return path


# ---------------------------------------------------------------- the end of a run

def _promotion_request(layer: Any, state: Any, req: dict[str, Any], built: dict[str, str]) -> dict[str, Any]:
    from . import delivery, promotion, ship

    key = _record_key(str(req["from"]), str(req["to"]))
    record = dict(req)
    if ahead(layer, str(req["source"]), str(req["target"])) == 0:
        record["action"] = "nothing-to-deliver"
        return record
    current = promotion.open_promotion(layer, str(req["from"]), str(req["to"]))
    if current:
        data = promotion._promotion_state(layer, str(current["id"]))
        p = data.get("promotion") or {}
        approval = Path(layer.layer_dir) / promotion.RECORDS / str(current["id"]) / "approval.md"
        if isinstance(p.get("pr"), dict) and p["pr"].get("number"):
            record.update(medium="forge", number=int(p["pr"]["number"]), url=str(p["pr"].get("url") or ""),
                          state="open", action="adopted", promotion=str(current["id"]))
            write_record(layer, key, {**record, "updated_at": _now()})
            return record
        if approval.is_file():
            record.update(medium="file", path=approval.relative_to(layer.layer_dir).as_posix(), state="open",
                          action="adopted", promotion=str(current["id"]))
            write_record(layer, key, {**record, "updated_at": _now()})
            return record
    title, body = promotion.request_summary(layer, str(req["from"]), str(req["to"]), str(req["source"]),
                                            str(req["target"]), by_run=str(state.adw_id))
    built.update(title=title, body=body, key=key)
    record["title"] = title
    client = ship._forge_client(delivery.ship_layer(layer)) if delivery.pull_requests(layer) else None
    if client is not None:
        remote = delivery._remote(layer) or "origin"
        listed = delivery._git(Path(layer.app_root), ["ls-remote", remote, f"refs/heads/{req['source']}"])
        if listed.returncode != 0 or not listed.stdout.strip():
            record["note"] = f"{req['source']} is not on the forge; nothing is pushed"
            write_file(layer, key, title, body, record)
            return record
        record.update(open_or_update(layer, client, str(req["source"]), str(req["target"]), title, body, key),
                      medium="forge", updated_at=_now())
        write_record(layer, key, record)
        return record
    forge = (layer.ir.get("forge") or {}).get("kind") or "none"
    record["note"] = "no forge" if forge not in FORGE_KINDS else "no pull requests"
    write_file(layer, key, title, body, record)
    return record


def after_run(layer: Any, state: Any, adw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Open, update or adopt the cycle's delivery request; record it in state.delivery_request.
    Never raises: a failure is recorded in `error` and the file is written instead."""
    from . import delivery

    record: dict[str, Any] | None = None
    req: dict[str, Any] | None = None
    built: dict[str, str] = {}
    try:
        if not delivery.stages(layer):
            return None  # a layer without stages: the state.json of before
        if state.dry_run:
            record = {"skipped": "dry run"}
        elif (state.remote or {}).get("phase") == "remote":
            record = {"skipped": "a run on another host"}
        elif not hands_off(layer, adw, state):
            record = {"skipped": "not a hands-off cycle"}
        else:
            req = next_request(layer, adw, state)
            if req is None:
                return None
            if "none" in req or req.get("kind") == "run" or req.get("action") == "adopted":
                record = dict(req)
            else:
                record = _promotion_request(layer, state, req, built)
    except Exception as exc:  # noqa: BLE001 — the run's result stands whatever the forge does
        record = {**(req or {}), "error": f"{type(exc).__name__}: {exc}"[:300]}
        if req and req.get("kind") == "promotion" and built.get("title"):
            try:
                record["note"] = "the forge failed"
                write_file(layer, built["key"], built["title"], built["body"], record)
            except Exception as again:  # noqa: BLE001
                record["error"] += f"; the file failed too: {again}"[:200]
    if record is None:
        return None
    record.setdefault("updated_at", _now())
    try:
        state.delivery_request = record
        state.save(layer.layer_dir)
        from . import events

        events.emit("delivery.request", **{k: v for k, v in record.items() if k != "body"})
        print(f"[adw {state.adw_id}] request: {summary_line(state)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[adw {state.adw_id}] warning: recording the delivery request failed: {exc}", flush=True)
    return record


def summary_line(state: Any) -> str:
    """The run summary's `request:` line."""
    r = state.delivery_request or {}
    if r.get("skipped"):
        return f"skipped: {r['skipped']}"
    if r.get("none"):
        return f"none: {r['none']}"
    error = f" (error: {str(r['error'])[:120]})" if r.get("error") else ""
    pair = f"{r.get('source')} → {r.get('target')}"
    if r.get("action") == "nothing-to-deliver":
        return f"nothing to deliver: {r.get('source')} is not ahead of {r.get('target')}{error}"
    if r.get("action") == "adopted":
        what = f"PR #{r['number']}" if r.get("number") else str(r.get("path") or "")
        whose = (f"run branch into {r.get('target')}" if r.get("kind") == "run"
                 else f"promotion {r.get('promotion') or ''} {pair}".replace("  ", " "))
        return f"adopted {what} ({whose}){error}"
    if r.get("action") == "left-person":
        return f"PR #{r.get('number')} {pair} is a person's, left alone {r.get('url') or ''}".rstrip() + error
    if r.get("medium") == "forge" and r.get("number"):
        return f"PR #{r['number']} {pair} ({r.get('action')}) {r.get('url') or ''}".rstrip() + error
    if r.get("path"):
        return f"written to {r['path']} ({r.get('note') or 'no forge'}){error}"
    return f"{pair}: {r.get('action') or 'not made'}{error}"
