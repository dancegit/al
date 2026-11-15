"""Spending limits: (budget limits #:run-usd #:step-usd #:day-usd) and --max-cost / AL_MAX_COST.

Cost is what each agent call's transcript reports (Claude Code's `total_cost_usd` in its final
`result` event). Agents that report nothing (codex, custom CLIs, a mock without a scripted cost)
count as $0, and the run summary says so.

  * before every agent call: today's spending in this layer (local date, every run's state.json,
    or its events for a run from before this module) must be below the day limit, else the call
    does not start (BudgetExceeded "budget:day: …").
  * after every agent call: a call that cost more than the step limit, or a run whose calls
    together cost more than the run limit, stops the run (BudgetExceeded "budget:step: …" /
    "budget:run: …"). The call has finished by then: nothing is cut off in the middle of a write.
    The runner then hands the work to a person through the usual handoff path.
  * a run does not start at all when the day limit is already reached (exit code EXIT_REFUSED).

Claude Code also gets a native cap (--max-budget-usd) of what is left under the tightest limit,
so a single call stops early instead of only being judged afterwards.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .errors import StepFailed

EXIT_REFUSED = 3          # the run did not start: the day limit is reached
LIMITS = ("run", "step", "day")


class BudgetExceeded(StepFailed):
    """A spending limit stopped the run. Never retried."""

    def __init__(self, step: str, limit: str, spent: float, of: float) -> None:
        self.limit, self.spent, self.of = limit, spent, of
        super().__init__(step, reason(limit, spent, of))


def reason(limit: str, spent: float, of: float) -> str:
    return f"budget:{limit}: spent {usd(spent)} of {usd(of)}"


def usd(value: float | None) -> str:
    return "none" if value is None else f"${value:.2f}"


def limits(layer: Any, over: Mapping[str, Any] | None) -> dict[str, Any]:
    """{"run", "step", "day": float | None, "run_from": "layer" | "--max-cost" | "AL_MAX_COST" | ""}."""
    declared = layer.ir.get("budget") if isinstance(layer.ir.get("budget"), dict) else {}

    def number(key: str) -> float | None:
        value = declared.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    out = {"run": number("runUsd"), "step": number("stepUsd"), "day": number("dayUsd"), "run_from": ""}
    if out["run"] is not None:
        out["run_from"] = "layer"
    over = over or {}
    if over.get("max_cost"):
        out["run"] = float(over["max_cost"])
        out["run_from"] = str((over.get("from") or {}).get("max_cost") or "--max-cost")
    return out


def any_limit(lim: Mapping[str, Any]) -> bool:
    return any(lim.get(k) is not None for k in LIMITS)


def local_day(stamp: str) -> str:
    """The local calendar date of an ISO timestamp ("" when it cannot be read)."""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().date().isoformat()


def today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _events_cost(run_dir: Path) -> float:
    total = 0.0
    try:
        data = (run_dir / "events.jsonl").read_bytes()
    except OSError:
        return 0.0
    for line in data.splitlines():
        if b'"agent.end"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        cost = event.get("cost") if isinstance(event, dict) else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
    return total


def run_cost(state: Mapping[str, Any], run_dir: Path) -> float:
    """What one run spent: its state's cost record, else (older runs) the sum of its agent.end events."""
    cost = state.get("cost")
    if isinstance(cost, Mapping) and isinstance(cost.get("total"), (int, float)):
        return float(cost["total"])
    return _events_cost(run_dir)


def day_spent(layer_dir: Path, day: str | None = None, exclude: str = "") -> float:
    """The sum over every run of this layer that started on `day` (local date; default today)."""
    day = day or today()
    total = 0.0
    try:
        entries = list(os.scandir(Path(layer_dir) / "agents"))
    except OSError:
        return 0.0
    for entry in entries:
        if entry.name == exclude or len(entry.name) != 8 or not entry.is_dir():
            continue
        try:
            state = json.loads((Path(entry.path) / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(state, dict) and local_day(state.get("started", "")) == day:
            total += run_cost(state, Path(entry.path))
    return round(total, 6)


def spent(state: Any) -> float:
    return float((state.cost or {}).get("total") or 0.0)


def refuse_start(layer: Any, lim: Mapping[str, Any], adw_id: str = "") -> str | None:
    """The reason a run may not start (day limit reached), or None."""
    if lim.get("day") is None:
        return None
    used = day_spent(layer.layer_dir, exclude=adw_id)
    if used >= float(lim["day"]):
        return (f"{reason('day', used, float(lim['day']))} today ({today()}) in this layer; no run starts until "
                "tomorrow, or raise #:day-usd in (budget limits …)")
    return None


def before_call(layer: Any, state: Any, step: str) -> float | None:
    """Raise when today's spending has reached the day limit; else the native cap for the call
    (what is left under the tightest limit), or None when there is no limit."""
    lim = state.budget.get("limits") or {}
    caps: list[float] = []
    if lim.get("day") is not None:
        used = round(day_spent(layer.layer_dir, exclude=state.adw_id) + spent(state), 6)
        if used >= float(lim["day"]):
            raise _stop(state, step, "day", used, float(lim["day"]))
        caps.append(float(lim["day"]) - used)
    if lim.get("run") is not None:
        caps.append(max(0.0, float(lim["run"]) - spent(state)))
    if lim.get("step") is not None:
        caps.append(float(lim["step"]))
    return round(min(caps), 4) if caps else None


def after_call(state: Any, step: str, agent: Mapping[str, Any], cost: float | None, failed: bool = False,
               native_stop: bool = False) -> None:
    """Add the call's cost to the run and stop the run when a limit is crossed."""
    record = state.cost if isinstance(state.cost, dict) else {}
    record["total"] = round(float(record.get("total") or 0.0) + float(cost or 0.0), 6)
    record["calls"] = int(record.get("calls") or 0) + 1
    if cost is None:
        unreported = list(record.get("unreported") or [])
        name = f"{agent.get('id')} ({agent.get('kind')})"
        if name not in unreported:
            unreported.append(name)
        record["unreported"] = unreported
    state.cost = record
    lim = state.budget.get("limits") or {}
    if lim.get("step") is not None and (cost or 0.0) > float(lim["step"]):
        raise _stop(state, step, "step", float(cost or 0.0), float(lim["step"]))
    if lim.get("run") is not None and spent(state) > float(lim["run"]):
        raise _stop(state, step, "run", spent(state), float(lim["run"]))
    if native_stop:  # the CLI stopped the call at the cap it was given: the tightest limit was reached
        which = state.budget.get("cap_from") or "step"
        of = float(lim.get(which) or 0.0)
        used = float(cost or 0.0) if which == "step" else spent(state)
        raise _stop(state, step, which, used, of)


def _stop(state: Any, step: str, limit: str, used: float, of: float) -> BudgetExceeded:
    state.budget["stopped"] = {"limit": limit, "spent": round(used, 6), "of": of, "step": step,
                               "reason": reason(limit, used, of)}
    from . import events  # late: events imports nothing from here, but keep the module light

    events.emit("budget.stop", step=step, limit=limit, spent=round(used, 6), of=of, reason=reason(limit, used, of))
    return BudgetExceeded(step, limit, used, of)


def cap_source(state: Any, layer: Any) -> str:
    """Which limit a native cap came from (the tightest one), for naming a CLI-side stop."""
    lim = state.budget.get("limits") or {}
    candidates = []
    if lim.get("step") is not None:
        candidates.append((float(lim["step"]), "step"))
    if lim.get("run") is not None:
        candidates.append((float(lim["run"]) - spent(state), "run"))
    if lim.get("day") is not None:
        candidates.append((float(lim["day"]) - day_spent(layer.layer_dir, exclude=state.adw_id) - spent(state), "day"))
    return min(candidates)[1] if candidates else ""


def summary(state: Any) -> str:
    """One line for the run summary."""
    record = state.cost or {}
    lim = state.budget.get("limits") or {}
    text = usd(spent(state))
    if lim.get("run") is not None:
        text += f" of run limit {usd(lim['run'])}" + (f" ({lim.get('run_from')})" if lim.get("run_from") not in ("", "layer") else "")
    extra = [f"step limit {usd(lim['step'])}"] if lim.get("step") is not None else []
    if lim.get("day") is not None:
        extra.append(f"day limit {usd(lim['day'])}")
    if extra:
        text += f" ({', '.join(extra)})"
    if record.get("unreported"):
        text += f"; reports no cost, counted as $0: {', '.join(record['unreported'])}"
    stopped = state.budget.get("stopped")
    if stopped:
        text += f"; STOPPED by {stopped['reason']}"
    return text
