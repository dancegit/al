"""The scheduler of a layer that declares `(placement auto #:hosts (…) …)`.

It runs on the home machine (the one that holds the layer), once per run that starts:

  1. `survey` asks every listed host for its load with one call (`adws/host.py load`), all hosts
     at once, and writes what it heard to agents/hosts.json (the dashboard and `al hosts LAYER
     status` read that file);
  2. `decide` picks a host by the declared rule and reserves it in the same file, under one lock,
     so runs that start together spread instead of all seeing the same empty host.

A workflow stays on `home` when the compiler pinned it (`pinnedHome`: a command that needs home, a
waiting script, a layer change, no worktree), when it is a single command, or when it is a fan-out
candidate (candidates run where their parent runs). Other hosts are skipped, each with its reason,
when they do not answer, have no usable login for the workflow's agent, are reserved for other
classes, or when the layer on this machine is not what the git source holds.
"""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import events, gitsafe, hostlink, ir, localenv, recordsync, selfrun, slots, state as state_module
from .errors import LayerError

HOME = hostlink.HOME
HOSTS_FILE = Path("agents") / "hosts.json"
LOCK = Path("agents") / ".placement.lock"
SURVEY_TIMEOUT = 10
PENDING_SECONDS = 60.0
NOT_AT_SOURCE = "the layer on this machine is not at the git source: commit and push it"


class Refused(Exception):
    """No host can take the run; the message says why, host by host."""


def declared(layer: Any) -> dict[str, Any] | None:
    """None without a (placement …), else {hosts, prefer, max_runs, reserve, source_env, lost_after}."""
    option = layer.ir.get("placement")
    if not isinstance(option, dict):
        return None
    reserve = option.get("reserve") if isinstance(option.get("reserve"), dict) else None
    return {"hosts": [str(h) for h in option.get("hosts") or []], "prefer": str(option.get("prefer") or "least-loaded"),
            "max_runs": int(option.get("maxRunsPerHost") or 0), "reserve": reserve,
            "source_env": str(option.get("sourceEnv") or "AL_GIT_SOURCE"),
            "lost_after": int(option.get("lostAfter") or 120)}


def pins(layer: Any, adw: dict[str, Any], state: Any = None) -> list[str]:
    """Why this workflow (or this run) always executes on `home`; empty when it may be placed."""
    reasons = [str(reason) for reason in adw.get("pinnedHome") or []]
    declared_names = {str(a.get("name")) for a in layer.ir.get("adws") or [] if isinstance(a, dict) and "pinnedHome" in a}
    if str(adw.get("name")) not in declared_names:
        reasons.append("it is not a workflow the layer declares (a single command, a promotion)")
    if state is not None and getattr(state, "fanout_parent", None):
        reasons.append("it is a fan-out candidate: candidates run where their parent runs")
    return reasons


# ---------------------------------------------------------------- the survey

def home_load(layer: Any) -> dict[str, Any]:
    """The facts `adws/host.py load` gives for a host, computed here for this machine."""
    limit = max(1, slots.limits(layer)["runs"])
    held = limit - slots.free_slots(layer, "runs")
    return selfrun.machine_facts(layer, held, Path(layer.layer_dir), localenv.load(layer))


def _ask(layer: Any, host_id: str, link: Callable[[Any, str], Any]) -> dict[str, Any]:
    try:
        answer = link(layer, host_id).tool(layer, host_id, "load", timeout=SURVEY_TIMEOUT)
    except hostlink.HostSilent as exc:
        return {"silent": exc.reason}
    except (LayerError, OSError) as exc:
        return {"silent": str(exc)}
    return answer


def _read(layer: Any) -> dict[str, Any]:
    data = selfrun._read_json(Path(layer.layer_dir) / HOSTS_FILE)
    return {"at": data.get("at") or "", "hosts": data.get("hosts") if isinstance(data.get("hosts"), dict) else {},
            "pending": [p for p in data.get("pending") or [] if isinstance(p, dict)]}


def _write(layer: Any, data: dict[str, Any]) -> None:
    recordsync.write_json(Path(layer.layer_dir) / HOSTS_FILE, data)


def survey(layer: Any, link: Callable[[Any, str], Any] = hostlink.link_for, hosts: list[str] | None = None) -> dict[str, Any]:
    """{host: load | {"silent": reason}} of every listed host (or of `hosts`), asked at once; the
    result replaces those hosts' entries in agents/hosts.json."""
    option = declared(layer)
    if option is None:
        raise LayerError("the layer declares no (placement …): every run executes on this machine")
    wanted = list(option["hosts"]) if hosts is None else list(hosts)
    found: dict[str, Any] = {}
    remote = [h for h in wanted if h != HOME]
    if remote:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(remote)) as pool:
            futures = {h: pool.submit(_ask, layer, h, link) for h in remote}
            for host_id, future in futures.items():
                found[host_id] = future.result()
    if HOME in wanted:
        found[HOME] = home_load(layer)
    found = {h: found[h] for h in wanted}
    data = _read(layer)
    kept = {h: v for h, v in data["hosts"].items() if h in option["hosts"] and h not in found}
    _write(layer, {"at": state_module.now(), "hosts": {**kept, **found}, "pending": _live_pending(layer, data["pending"])})
    return found


def _live_pending(layer: Any, pending: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    """Reservations that still count: younger than PENDING_SECONDS and of a run the host cannot
    have reported yet."""
    now = time.time() if now is None else now
    kept = []
    for entry in pending:
        if now - float(entry.get("at") or 0) >= PENDING_SECONDS:
            continue
        data = selfrun._read_json(state_module.run_dir(layer.layer_dir, str(entry.get("run"))) / "state.json")
        if data:
            remote = data.get("remote") if isinstance(data.get("remote"), dict) else {}
            if data.get("status") != "running" or remote.get("status") in ("running", *recordsync.REMOTE_ENDED):
                continue   # ended, or the host counts it itself by now
            if entry.get("host") == HOME and data.get("worktree"):
                continue   # it holds its slot here
        kept.append(entry)
    return kept


# ---------------------------------------------------------------- the decision

def _git(cwd: Path, args: list[str], secrets: tuple[str, ...] = ()) -> str | None:
    try:
        done = subprocess.run(["git", *gitsafe.HARDENING, *args], cwd=cwd, capture_output=True, text=True, timeout=60,
                              check=False, stdin=subprocess.DEVNULL, env={**gitsafe._base_env(), "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def source_problem(layer: Any) -> str:
    """"" when a host that clones the git source gets exactly the layer and base this machine
    runs; else why not."""
    option = declared(layer) or {}
    name = option.get("source_env") or "AL_GIT_SOURCE"
    url = localenv.lookup(name, localenv.load(layer)).strip()
    if not url:
        return f"{name} is not set on this machine: the git source runs on other hosts push to"
    app = Path(layer.app_root)
    branch = layer.default_branch
    local = (_git(app, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]) or "").strip()
    listed = _git(app, ["ls-remote", url, f"refs/heads/{branch}"], (url,))
    there = listed.split()[0] if listed and listed.strip() else ""
    if not local or local != there:
        return NOT_AT_SOURCE
    working = Path(layer.layer_dir) / ir.IR_FILE
    if layer.in_repo_dir:
        committed = _git(app, ["show", f"{local}:{layer.in_repo_dir}/{ir.IR_FILE}"])
    else:
        committed = _git(Path(layer.layer_dir), ["show", f"HEAD:{ir.IR_FILE}"])
    try:
        if committed is None or committed != working.read_text(encoding="utf-8"):
            return NOT_AT_SOURCE
    except OSError:
        return NOT_AT_SOURCE
    return ""


def layer_revision(layer: Any) -> str:
    """The commit of a layer that is its own repository ("" for an in-repo layer)."""
    if layer.in_repo_dir:
        return ""
    return (_git(Path(layer.layer_dir), ["rev-parse", "--verify", "--quiet", "HEAD"]) or "").strip()


def agent_kinds(layer: Any, adw: dict[str, Any]) -> list[str]:
    """The kinds of the agents the workflow runs on (its own, a step's, its reviewer's)."""
    ids = [str(adw.get("agent") or "")] + [str(s.get("agent")) for s in adw.get("steps") or [] if s.get("agent")]
    review = next((r for r in layer.ir.get("reviews") or [] if r.get("id") == adw.get("review")), {})
    if review.get("agent"):
        ids.append(str(review["agent"]))
    kinds = []
    for agent in layer.ir.get("agents") or []:
        if agent.get("id") in ids and agent.get("kind") not in kinds:
            kinds.append(str(agent.get("kind")))
    return kinds


def login_problem(layer: Any, adw: dict[str, Any], state: Any, host_id: str, load: dict[str, Any]) -> str:
    """The refusal sentence when the host cannot run the workflow's agent; "" when it can (a dry
    run and the mock agent need no login)."""
    from . import envs

    if getattr(state, "dry_run", False):
        return ""
    logins = load.get("logins") if isinstance(load.get("logins"), dict) else {}
    for kind in agent_kinds(layer, adw):
        credential = selfrun.AGENT_CREDENTIAL.get(kind)
        if credential is None:
            continue
        if str(logins.get(kind) or "none") == "none":
            variables = " or ".join(envs.CREDENTIALS[credential][1])
            return (f"host {host_id} has no usable login for {kind}: log in on it, or name a token ({variables}) "
                    f"in its env file; see: al host LAYER {host_id} doctor")
    return ""


def _reserved_for_others(option: dict[str, Any], host_id: str, state: Any) -> str:
    reserve = option.get("reserve")
    if not reserve or str(reserve.get("host")) != host_id:
        return ""
    classes = [str(c) for c in reserve.get("classes") or []]
    if getattr(state, "class_fixed", False) and getattr(state, "klass", "") in classes:
        return ""
    if not getattr(state, "class_fixed", False):
        return f"reserved for {', '.join(classes)}; this run's class is not fixed at start"
    return f"reserved for {', '.join(classes)}; this run is a {state.klass}"


def _order(prefer: str, usable: list[tuple[str, dict[str, Any], int, int]]) -> list[tuple[str, dict[str, Any], int, int]]:
    """Candidates (host, load, runs counted, declared index), best first."""
    if prefer == "first":
        free = [c for c in usable if c[2] < int(c[1].get("max_runs") or 1)]
        return free or usable
    def key(candidate: tuple[str, dict[str, Any], int, int]) -> tuple[float, float, int, int]:
        _host, load, runs, index = candidate
        return (runs / max(1, int(load.get("max_runs") or 1)), float(load.get("load1") or 0) / max(1, int(load.get("cpus") or 1)),
                0 if load.get("image") in (True, None) else 1, index)
    return sorted(usable, key=key)


def decide(layer: Any, adw: dict[str, Any], state: Any, on: str | None = None,
           link: Callable[[Any, str], Any] = hostlink.link_for, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    """{host, reasons, skipped: [{host, reason}]} for this run, and the reservation of that host in
    agents/hosts.json. `on` names the only host to consider. Refused when no host is left."""
    option = declared(layer)
    if option is None:
        raise LayerError("the layer declares no (placement …)")
    listed = option["hosts"]
    pinned = pins(layer, adw, state)
    skipped: list[dict[str, str]] = []
    if on is not None and on not in listed and not (on == HOME and pinned):
        raise Refused(f"{on} is not a host of (placement … #:hosts ({' '.join(listed)}))")
    if pinned:
        if on not in (None, HOME):
            raise Refused(f"workflow {adw.get('name')} runs on home only: {'; '.join(pinned)}")
        return _decided(layer, state, HOME, [f"runs on home only: {'; '.join(pinned)}"], skipped, reserve=False)
    candidates = [h for h in ([on] if on is not None else listed) if h not in exclude]
    with slots.file_lock(Path(layer.layer_dir) / LOCK):
        remote = [h for h in candidates if h != HOME]
        problem = source_problem(layer) if remote else ""
        if problem:
            skipped += [{"host": h, "reason": problem} for h in remote]
            candidates = [h for h in candidates if h == HOME]
        loads = survey(layer, link, candidates) if candidates else {}
        pending = _read(layer)["pending"]
        usable: list[tuple[str, dict[str, Any], int, int]] = []
        for index, host_id in enumerate(candidates):
            load = loads.get(host_id) or {"silent": "no answer"}
            if "silent" in load:
                skipped.append({"host": host_id, "reason": f"no answer: {load['silent']}"})
                continue
            reason = _reserved_for_others(option, host_id, state) if on is None else ""
            if not reason and host_id != HOME:
                reason = login_problem(layer, adw, state, host_id, load)
            if reason:
                skipped.append({"host": host_id, "reason": reason})
                continue
            waiting = sum(1 for p in pending if p.get("host") == host_id)
            usable.append((host_id, load, int(load.get("runs") or 0) + waiting, index))
        if not usable:
            details = "; ".join(s["reason"] if s["reason"].startswith(f"host {s['host']} ") else f"{s['host']}: {s['reason']}"
                                for s in skipped) or "no host is listed"
            raise Refused(details if on is not None and len(skipped) == 1 else f"no host can take the run: {details}")
        host_id, load, runs, _index = _order(option["prefer"], usable)[0]
        limit = int(load.get("max_runs") or 1)
        reasons = [f"{'named with --on' if on is not None else option['prefer']}: {runs} of {limit} runs in use"]
        if runs >= limit:
            reasons.append("every run slot there is in use: the run waits for one")
        return _decided(layer, state, host_id, reasons, skipped, reserve=True)


def _decided(layer: Any, state: Any, host_id: str, reasons: list[str], skipped: list[dict[str, str]],
             reserve: bool) -> dict[str, Any]:
    if reserve:
        data = _read(layer)
        pending = _live_pending(layer, data["pending"])
        pending.append({"host": host_id, "run": getattr(state, "adw_id", ""), "at": time.time()})
        _write(layer, {**data, "pending": pending})
    return {"host": host_id, "reasons": reasons, "skipped": skipped}


def emit_decision(decision: dict[str, Any]) -> None:
    events.emit("placement.decision", host=decision["host"], reasons=decision["reasons"], skipped=decision["skipped"])


# ---------------------------------------------------------------- what a person reads

def _size(kilobytes: Any) -> str:
    try:
        value = float(kilobytes) / (1024 * 1024)
    except (TypeError, ValueError):
        return "-"
    return f"{value:.1f} GB" if value < 100 else f"{value:.0f} GB"


COLUMNS = ("host", "runs", "cpus", "load", "memory free", "disk free", "image", "login", "state")


def table(found: dict[str, Any]) -> list[dict[str, str]]:
    """The rows of `al hosts LAYER status`: what the scheduler sees, host by host."""
    rows = []
    for host_id, load in found.items():
        if not isinstance(load, dict) or "silent" in load:
            reason = str((load or {}).get("silent") or "") if isinstance(load, dict) else ""
            rows.append({**{c: "-" for c in COLUMNS}, "host": host_id, "state": "no answer" + (f": {reason}" if reason else "")})
            continue
        logins = load.get("logins") if isinstance(load.get("logins"), dict) else {}
        shown = ", ".join(f"{kind}: {text}" for kind, text in logins.items() if text != "not needed") or "not needed"
        image = load.get("image")
        rows.append({"host": host_id, "runs": f"{load.get('runs', 0)}/{load.get('max_runs', '?')}",
                     "cpus": str(load.get("cpus", "-")), "load": f"{float(load.get('load1') or 0):.2f}",
                     "memory free": _size(load.get("mem_available_kb")), "disk free": _size(load.get("disk_free_kb")),
                     "image": "-" if image is None else ("yes" if image else "no"), "login": shown, "state": "ok"})
    return rows


def render(rows: list[dict[str, str]]) -> str:
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) if rows else len(c) for c in COLUMNS}
    lines = ["  ".join(c.ljust(widths[c]) for c in COLUMNS).rstrip()]
    lines += ["  ".join(r[c].ljust(widths[c]) for c in COLUMNS).rstrip() for r in rows]
    return "\n".join(lines)


def headline(layer: Any) -> str:
    option = declared(layer) or {}
    limit = max(1, slots.limits(layer)["runs"])
    return f"placement: auto, prefer {option.get('prefer')}, at most {limit} runs per host"
