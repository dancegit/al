from __future__ import annotations

import json
import os
import re
import secrets
import socket
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import LayerError

HEARTBEAT_SECONDS = 5.0   # state.json's heartbeat is refreshed this often while the run is alive
_SAVE_LOCK = threading.RLock()  # the heartbeat thread and the run never interleave a save
# steps a resume does not run again; a rejected review returned normally and its verdict stays in state.review
FINISHED_STEP_STATUSES = ("passed", "rejected")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ADWState:
    adw_id: str
    adw: str
    prompt: str = ""
    source: str = "manual"
    klass: str = "chore"
    class_fixed: bool = False
    dry_run: bool = False
    model_set: str = ""
    branch: str = ""
    worktree: str = ""
    ports: list[int] = field(default_factory=list)
    plan: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    scripts: dict[str, dict[str, Any]] = field(default_factory=dict)  # (script …) steps: {cmd, args, exit, log}
    artefacts: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    ship: dict[str, Any] = field(default_factory=dict)
    fanout: dict[str, dict[str, Any]] = field(default_factory=dict)  # (fan-out NAME …) → candidates, picked, pick
    status: str = "running"
    started: str = field(default_factory=now)
    ended: str = ""
    # liveness: the process running the run, and when it was last seen alive (see Heartbeat)
    pid: int = 0
    host: str = ""
    heartbeat: str = ""
    fanout_parent: dict[str, Any] = field(default_factory=dict)  # {run, step, index} of a fan-out candidate
    # --model-set / --model / --max-cost and their environment variables: {tier, models, max_cost, from}
    overrides: dict[str, Any] = field(default_factory=dict)
    # {tier: {model, source}} of the ADW's agent, decided when the run started (local settings read then)
    models: dict[str, Any] = field(default_factory=dict)
    # every agent call: {step, owner, agent, agent_kind, tier, tier_from, model, model_from, effort, status, cost, billing}
    agent_calls: list[dict[str, Any]] = field(default_factory=list)
    # what the agent calls cost: {total, calls, unreported: ["id (kind)" of agents that report no cost],
    # billing: subscription | api-key | mixed (when a transcript said so), api_equivalent: dollars of subscription calls}
    cost: dict[str, Any] = field(default_factory=dict)
    # spending limits of this run: {limits: {run, step, day, run_from}, stopped: {limit, spent, of, step, reason}}
    budget: dict[str, Any] = field(default_factory=dict)
    # subscription usage seen by this run: {billing, windows: {name: {window, status, used, resets_at, at, step}}, stopped: {window, cause, status, used, percent, resets_at, step, reason}, waits: [{step, window, seconds, until}]}
    usage: dict[str, Any] = field(default_factory=dict)
    # notifications already attempted by this run: {notifier id: [event, …]}; at most one attempt per notifier and event, also over --resume
    notified: dict[str, list[str]] = field(default_factory=dict)
    # the synthetic ADW of a single-command run (launch.single_command_adw), so a reader can draw it
    # without the compiled layer knowing it; empty for every run of a declared workflow
    definition: dict[str, Any] = field(default_factory=dict)
    # where the run delivers when the layer declares stages (delivery.record + delivered/target, or
    # {stage, branch, refused}); empty, and then left out of state.json, for a layer without stages
    delivery: dict[str, Any] = field(default_factory=dict)
    # the sandbox image this run uses (al-<layer>:<recipe hash>), recorded by the preflight; "" without podman
    image: str = ""
    # a promotion run (promotion.py): {from, to, from_branch, to_branch, pinned, to_head, method, approval,
    # approvers, changes, decisions_file, …}; empty, and then left out of state.json, for every other run
    promotion: dict[str, Any] = field(default_factory=dict)
    # every check result and decision of the run, in order (affected.record): {check, step, phase:
    # check | recheck | gate, outcome: passed | failed | not-affected | reused, tree, definition, exit,
    # log, why, paths, paths_total, from: {step, phase, log} | null, at}; left out of state.json while empty
    check_results: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-fA-F]{8}", self.adw_id):
            raise LayerError("adw_id must contain exactly 8 hexadecimal characters")

    def step_record(self, name: str, kind: str) -> dict[str, Any]:
        """Return the mutable record of step `name`, creating a pending one on first use."""
        for record in self.steps:
            if record.get("name") == name:
                return record
        record = {"name": name, "kind": kind, "status": "pending", "attempts": 0,
                  "started": "", "ended": "", "detail": ""}
        self.steps.append(record)
        return record

    @property
    def finished_steps(self) -> set[str]:
        return {str(step["name"]) for step in self.steps if step.get("status") in FINISHED_STEP_STATUSES}

    def mark_alive(self) -> None:
        self.pid, self.host, self.heartbeat = os.getpid(), socket.gethostname(), now()

    def save(self, layer_dir: Path) -> Path:
        target = run_dir(layer_dir, self.adw_id) / "state.json"
        with _SAVE_LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(self)
            if not data.get("delivery"):
                data.pop("delivery", None)  # a layer without stages writes the state.json of before
            if not data.get("promotion"):
                data.pop("promotion", None)  # only a promotion run has one
            if not data.get("check_results"):
                data.pop("check_results", None)  # a run without a check writes the state.json of before
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                temporary = Path(handle.name)
            temporary.replace(target)
        return target


class Heartbeat:
    """Refreshes state.heartbeat (and saves state.json) every HEARTBEAT_SECONDS from one daemon
    thread, so a reader can tell a run that is still working from one whose process died. The
    thread only writes what is in memory, under the same lock as every other save; a save that
    races with the run changing its state is skipped and retried on the next beat."""

    def __init__(self, state: "ADWState", layer_dir: Path) -> None:
        self.state, self.layer_dir = state, Path(layer_dir)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, name=f"heartbeat-{state.adw_id}", daemon=True)

    def start(self) -> "Heartbeat":
        self.state.mark_alive()
        self.thread.start()
        return self

    def _loop(self) -> None:
        while not self.stopped.wait(HEARTBEAT_SECONDS):
            try:
                with _SAVE_LOCK:
                    if self.stopped.is_set():
                        return
                    self.state.heartbeat = now()
                    self.state.save(self.layer_dir)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue  # e.g. the run mutated a dict while it was serialised: the next beat saves

    def stop(self) -> None:
        with _SAVE_LOCK:
            self.stopped.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)


def run_dir(layer_dir: Path, adw_id: str) -> Path:
    return Path(layer_dir) / "agents" / adw_id


def new_adw_id() -> str:
    return secrets.token_hex(4)


def load(layer_dir: Path, adw_id: str) -> ADWState:
    path = run_dir(layer_dir, adw_id) / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ADWState(**data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise LayerError(f"Cannot load state for {adw_id}: {exc}") from exc
