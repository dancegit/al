"""Helpers for the stand-in's dashboard tests: runs written to a layer by hand (a stopped run with or
without a suggestion, its decisions.jsonl, the layer's log), the mock decider's answers, and waiting
for what the dashboard publishes. The record's format is the runtime's (standin.record)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from conftest import Running
from test_start import headers, post  # noqa: F401 — the write helpers every stand-in test uses

HOSTILE = '<img src=x onerror="window.__pwned=1"> settle <b>now</b>'


def iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def ago(seconds: float) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def entry(run: str, **fields: Any) -> dict[str, Any]:
    """One line of the stand-in's record."""
    base = {"id": fields.pop("id", f"e{abs(hash((run, json.dumps(fields, sort_keys=True, default=str)))) % 16**7:07x}"),
            "at": ago(30), "type": "decision", "who": "stand-in", "via": "loop", "run": run, "mode": "suggest",
            "situation": "stopped", "kind": "resume", "action": "resume", "summary": "The check looks flaky; run it again.",
            "why": "Nothing here needs a person.", "confidence": 0.9, "read": ["why it stopped", "run.log (last 40 lines)"],
            "status": "suggested", "auto": False}
    return {**base, **fields}


def append(path: Path, *entries: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for item in entries:
            handle.write(json.dumps(item) + "\n")


def record(layer: Path, *entries: dict[str, Any]) -> None:
    """Write entries the way standin.record does: the layer's log and each run's decisions.jsonl."""
    for item in entries:
        append(layer / "agents" / "stand-in" / "log.jsonl", item)
        if item.get("run"):
            append(layer / "agents" / item["run"] / "decisions.jsonl", item)


def write_run(layer: Path, run_id: str, *, adw: str = "stops", status: str = "failed", ended: str | None = None,
              **state: Any) -> Path:
    """A finished run of the layer: failed at the check `ready` and handed over with PR.md, unless told otherwise."""
    directory = layer / "agents" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    ended = ended if ended is not None else ago(60)
    started = iso(datetime.fromisoformat(ended) - timedelta(seconds=40)) if ended else ago(100)
    steps = [{"name": "plan", "kind": "run", "status": "passed", "attempts": 1, "started": started, "ended": started, "detail": "ok"},
             {"name": "ready", "kind": "check", "status": "failed" if status == "failed" else "passed", "attempts": 1,
              "started": started, "ended": ended, "detail": "ready: check ready exited 1 after 1 attempt(s)"}]
    data = {"adw_id": run_id, "adw": adw, "prompt": "Add a note about the orchard.", "source": "cli", "klass": "chore",
            "status": status, "started": started, "ended": ended, "branch": f"chore/{run_id}-add-a-note", "worktree": "",
            "steps": steps, "checks": {"ready": {"exit": 1, "log": f"agents/{run_id}/checks/ready.1.log"}},
            "ship": {"action": "handoff", "stopped_at": "ready", "target": f"agents/{run_id}/PR.md", "shipped": False,
                     "reasons": ["run-stopped:ready: check ready exited 1"]} if status == "failed" else {},
            "agent_calls": [], "cost": {}, "pid": 0, "host": "", "heartbeat": ended}
    data.update(state)
    (directory / "state.json").write_text(json.dumps(data, indent=1))
    (directory / "run.log").write_text(f"[adw {run_id}] run failed: ready: check ready exited 1\n")
    (directory / "PR.md").write_text("# [needs human] Add a note\n\n## Why this stopped\n\nThe check `ready` failed.\n")
    return directory


def suggestion(run_id: str, **fields: Any) -> dict[str, Any]:
    """A state.decision that waits for a person's answer."""
    return {"mode": "suggest", "mode_from": "local setting", "action": "resume", "kind": "resume", "situation": "stopped",
            "from_step": "ready", "stopped_at": "ready", "summary": "The check looks flaky; run it again.",
            "explanation": "The check failed on a missing marker, not on the change.", "instructions": "",
            "confidence": 0.9, "read": ["why it stopped", "run.log (last 40 lines)"], "by": "stand-in",
            "code": "Abc_123-xyz9", "rounds": 0, "answers": [], "at": ago(30), "status": "suggested", **fields}


def with_suggestion(layer: Path, run_id: str, **fields: Any) -> dict[str, Any]:
    """Give a written run a suggestion: in state.json and, as the runtime does, in the record."""
    path = layer / "agents" / run_id / "state.json"
    state = json.loads(path.read_text())
    state["decision"] = suggestion(run_id, **fields)
    path.write_text(json.dumps(state, indent=1))
    d = state["decision"]
    stop = f"{state['status']}:{state.get('ended') or ''}:" + next(
        (s["name"] for s in state.get("steps") or [] if s.get("status") == "failed"), "")
    record(layer, entry(run_id, kind=d["kind"], action=d["action"], summary=d["summary"], why=d["explanation"],
                        confidence=d["confidence"], stop=stop,
                        **{k: d[k] for k in ("settle_as", "from_step", "promote") if d.get(k)}))
    return d


def mock_decision(layer: Path, action: str = "resume", **fields: Any) -> None:
    """What the mock agent answers when the stand-in asks it (mock/decide.json)."""
    answer = {"action": action, "summary": f"The stand-in proposes {action}.", "explanation": "Mock: nothing needs a person.",
              "instructions": "", "from_step": "", "confidence": 0.8, **fields}
    (layer / "mock").mkdir(exist_ok=True)
    (layer / "mock" / "decide.json").write_text(json.dumps({"output": "```json\n" + json.dumps(answer) + "\n```"}))


def declare(layer: Path, kind: str = "auto", **fields: Any) -> None:
    """Add a (stand-in KIND …) declaration to the layer's compiled IR."""
    path = layer / "agentic-layer.json"
    data = json.loads(path.read_text())
    data["standIn"] = {"id": kind, "kind": kind, "status": "stable", "modelSet": "heavy", "may": ["resume"], "never": [],
                       "maxActionsPerRun": 3, "maxPerHour": 12, "escalateTo": ["dashboard"], "systemd": False, **fields}
    path.write_text(json.dumps(data, indent=1))


def stops(layer: Path) -> Path:
    """Give the layer the workflow `stops` (start_layer.add_stops) → the file that lets its check pass."""
    import start_layer

    return start_layer.add_stops(layer)


def state_of(layer: Path, run_id: str) -> dict[str, Any]:
    return json.loads((layer / "agents" / run_id / "state.json").read_text())


def wait_for(what: Callable[[], Any], timeout: float = 120.0, every: float = 0.3, why: str = "") -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = what()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"never happened within {timeout:.0f} s: {why or what} (last: {last!r})")


def run_row(running: Running, layer: str, run_id: str) -> dict[str, Any] | None:
    return next((r for r in running.json("/api/snapshot")["runs"] if r["layer"] == layer and r["id"] == run_id), None)


def standin_of(running: Running, layer: str) -> dict[str, Any]:
    return next(l for l in running.json("/api/snapshot")["layers"] if l["key"] == layer)["standin"]
