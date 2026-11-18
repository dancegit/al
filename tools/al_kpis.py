"""`al kpis LAYER [--since DATE] [--json|--md]`: numbers from a layer's run records.

Read-only, and without a network: the runs are the dashboard's own model (al_runs.rows_of, which
reads agents/<id> and asks git, never a forge, whether a handed-over run's work is on its target
branch), plus each run's agents/<id>/state.json for the step records. --since keeps the runs that
started on or after that day (UTC).

What is counted (the same text is in the --md output):

  outcome       every run is one of passed, handed over, failed (failed or died), waiting, running
                (anything else is `other`); "landed afterwards" are the handed-over runs whose work
                is on its target branch, or that a person settled as landed.
  why           a fixed category, never the recorded text. Handed over: each recorded reason by its
                leading keyword (review-rejected, required-check, class-not-allowed, change-too-large,
                protected-path, no-container, other). Failed: the kind of the failed step (run, check,
                review, ship, setup), or step-discipline, budget, usage-limit, died when the record
                says so; anything else is other.
  attempts      per step name of the layer definition, from the step records: mean, max, and the
                share of records that passed on the first attempt.
  review rounds per run that was reviewed: mean, max, runs with more than one round.
  a person      one action for each of: the run was handed over, it waited at an approval step, it
                has a settlement record, it was started again by hand. Zero-touch runs passed and
                were shipped by the gate.
  time and cost per workflow and class: median and total of the finished runs' duration and of the
                real runs' cost. Dry runs are their own rows and have no cost; a real run without a
                recorded cost is counted under "without cost".
  default branch  runs whose work is on the layer's default branch: shipped by the gate, or handed
                over and landed afterwards.

The output holds counts, step names, workflow names and classes. It never holds a run id, a title, a
prompt, a branch, a URL, a path or a reason as it was written.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTCOMES = ("passed", "handed_over", "failed", "waiting", "running", "other")
HANDOVER_WHY = ("review-rejected", "required-check", "class-not-allowed", "change-too-large", "protected-path",
                "no-container", "other")
FAILED_WHY = ("run", "check", "review", "ship", "setup", "step-discipline", "budget", "usage-limit", "died", "other")
STEP_KINDS = ("run", "check", "review", "ship", "setup")
ACTIONS = ("handed_over", "waited_at_approval", "settled", "started_again")
SAMPLE_LINE = ("> **SAMPLE — generated from test fixtures, not from real runs.** Replace it with "
               "`tools/al kpis LAYER --md > docs/KPIS.md`.")
DEFINITIONS = (
    ("Outcome", "every run is passed, handed over, failed (failed or died), waiting or running; landed afterwards "
                "are the handed-over runs whose work is on its target branch (read from git, never fetched) or that "
                "a person settled as landed."),
    ("Why", "a fixed category, never the recorded text: for a hand-over the leading keyword of each recorded reason, "
            "for a failure the kind of the failed step, or step-discipline, budget, usage-limit, died."),
    ("Attempts", "per step of the layer definition, from the step records; first attempt is the share of records "
                 "that passed without a second attempt."),
    ("Review rounds", "counted for the runs that were reviewed."),
    ("A person acted", "one action for each of: the run was handed over, it waited at an approval step, it has a "
                       "settlement record, it was started again by hand. Zero-touch runs passed and were shipped by "
                       "the gate."),
    ("Time and cost", "median and total per workflow and class, over finished runs; dry runs are their own rows "
                      "and have no cost; a real run without a recorded cost is counted under without cost."),
    ("Default branch", "runs whose work is on the layer's default branch: shipped by the gate, or handed over and "
                       "landed afterwards."),
)


_NUMBER = re.compile(r"^[\d.$%hms -]*$")   # a cell of a column that is printed right-aligned


class Usage(Exception):
    pass


# ---------------------------------------------------------------- reading

def parse_since(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError as exc:
        raise Usage(f"--since takes a date as YYYY-MM-DD, not {text!r}") from exc


def _state(layer_path: Path, run_id: str) -> dict[str, Any]:
    try:
        data = json.loads((layer_path / "agents" / run_id / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def outcome_of(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "")
    if status in ("failed", "died"):
        return "failed"
    return {"passed": "passed", "handed-over": "handed_over", "waiting": "waiting", "running": "running"}.get(status, "other")


def landed_afterwards(row: dict[str, Any]) -> bool:
    settlement = row.get("settlement") or row.get("settled") or {}
    return outcome_of(row) == "handed_over" and (row.get("bucket") == "landed" or
                                                 (isinstance(settlement, dict) and settlement.get("as") == "landed"))


def handover_category(reason: str) -> str:
    key = reason.strip().lower()
    if key.startswith(("review-rejected", "review-failed")):
        return "review-rejected"
    if key.startswith(("check-failed", "require-check-failed", "required-check")):
        return "required-check"
    if key.startswith("class-not-allowed"):
        return "class-not-allowed"
    if key.startswith("diff-too-large"):
        return "change-too-large"
    if key.startswith(("protected-path", "layer-change")):
        return "protected-path"
    if key.startswith(("no-container", "container")):
        return "no-container"
    return "other"


def failed_category(row: dict[str, Any], state: dict[str, Any]) -> str:
    if row.get("status") == "died":
        return "died"
    record = next((r for r in state.get("steps") or [] if isinstance(r, dict) and r.get("status") == "failed"), {})
    detail = str(record.get("detail") or "").strip().lower()
    for marker, category in (("step-discipline", "step-discipline"), ("budget", "budget"), ("usage-limit", "usage-limit"),
                             ("usage limit", "usage-limit"), ("died", "died")):
        if detail.startswith(marker):
            return category
    kind = str(record.get("kind") or "")
    return kind if kind in STEP_KINDS else "other"


def person_actions(row: dict[str, Any], state: dict[str, Any]) -> dict[str, int]:
    waited = row.get("status") == "waiting" or any(
        isinstance(r, dict) and r.get("status") == "waiting" for r in state.get("steps") or [])
    return {"handed_over": int(outcome_of(row) == "handed_over"), "waited_at_approval": int(waited),
            "settled": int(bool(row.get("settlement"))), "started_again": int(bool(row.get("replaces")))}


def _cost(row: dict[str, Any], state: dict[str, Any]) -> float | None:
    if isinstance(row.get("cost"), (int, float)):
        return float(row["cost"])
    record = state.get("cost") if isinstance(state.get("cost"), dict) else {}
    total = record.get("total")
    return float(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else None


def _stats(values: list[float], digits: int) -> dict[str, Any]:
    return {"runs": len(values), "median": round(statistics.median(values), digits) if values else None,
            "total": round(sum(values), digits) if values else 0}


def collect(layer_dir: Path, since: float | None = None, since_text: str = "") -> dict[str, Any]:
    """Every number of the report, as plain data."""
    import al_runs
    layer = al_runs._load(layer_dir)
    codebase = layer.ir.get("codebase") if isinstance(layer.ir.get("codebase"), dict) else {}
    default = str(codebase.get("defaultBranch") or "main")
    rows = [r for r in al_runs.rows_of(layer, forge=False)
            if since is None or (isinstance(r.get("started"), (int, float)) and r["started"] >= since)]
    runs: dict[str, Any] = {"total": len(rows), **{name: 0 for name in OUTCOMES}, "landed_afterwards": 0, "dry_runs": 0,
                            "why": {"handed_over": {}, "failed": {}}}
    steps: dict[str, list[tuple[int, bool]]] = {}
    rounds: list[int] = []
    actions = {name: 0 for name in ACTIONS}
    needing = zero_touch = 0
    groups: dict[tuple[str, str, bool], dict[str, list[float] | int]] = {}
    on_default = {"changes": 0, "shipped_by_the_gate": 0, "landed_afterwards": 0}
    for row in rows:
        state = _state(layer.path, str(row["id"]))
        outcome = outcome_of(row)
        shipped = outcome == "passed" and (row.get("outcome") or {}).get("shipped") is True
        landed = landed_afterwards(row)
        runs[outcome] += 1
        runs["landed_afterwards"] += int(landed)
        runs["dry_runs"] += int(bool(row.get("dry_run")))
        if outcome == "handed_over":
            why = runs["why"]["handed_over"]
            for category in sorted({handover_category(str(r)) for r in (row.get("outcome") or {}).get("handover") or []}) or ["other"]:
                why[category] = why.get(category, 0) + 1
        elif outcome == "failed":
            category = failed_category(row, state)
            runs["why"]["failed"][category] = runs["why"]["failed"].get(category, 0) + 1
        for record in state.get("steps") or []:
            if isinstance(record, dict) and isinstance(record.get("attempts"), int) and record["attempts"] >= 1 \
                    and record.get("status") in ("passed", "failed"):
                steps.setdefault(str(record.get("name") or "?"), []).append(
                    (record["attempts"], record["status"] == "passed" and record["attempts"] == 1))
        if (row.get("outcome") or {}).get("rounds"):
            rounds.append(int(row["outcome"]["rounds"]))
        acted = person_actions(row, state)
        for name, count in acted.items():
            actions[name] += count
        needing += int(any(acted.values()))
        zero_touch += int(shipped)
        key = (str(row.get("adw") or "?"), str(row.get("klass") or "?"), bool(row.get("dry_run")))
        group = groups.setdefault(key, {"runs": 0, "durations": [], "costs": [], "without_cost": 0})
        group["runs"] += 1
        if outcome != "running" and isinstance(row.get("duration"), (int, float)):
            group["durations"].append(float(row["duration"]))
        if not key[2]:
            cost = _cost(row, state)
            if cost is None:
                group["without_cost"] += 1
            else:
                group["costs"].append(cost)
        if str(row.get("target_branch") or "") == default and (shipped or landed):
            on_default["changes"] += 1
            on_default["shipped_by_the_gate" if shipped else "landed_afterwards"] += 1
    attempts = {name: {"records": len(items), "mean": round(sum(a for a, _ in items) / len(items), 2),
                       "max": max(a for a, _ in items),
                       "first_attempt_share": round(sum(1 for _, first in items if first) / len(items), 3)}
                for name, items in sorted(steps.items())}
    return {
        "since": since_text or None,
        "runs": runs,
        "attempts": attempts,
        "review": {"runs_reviewed": len(rounds), "mean": round(sum(rounds) / len(rounds), 2) if rounds else None,
                   "max": max(rounds) if rounds else None, "runs_with_more_than_one": sum(1 for r in rounds if r > 1)},
        "person": {"runs_needing_a_person": needing, "actions": sum(actions.values()), "by_action": actions,
                   "zero_touch_runs": zero_touch},
        "time_and_cost": [{"workflow": adw, "class": klass, "dry_run": dry, "runs": group["runs"],
                           "duration_seconds": _stats(group["durations"], 1),
                           "cost_usd": None if dry else {**_stats(group["costs"], 4), "without_cost": group["without_cost"]}}
                          for (adw, klass, dry), group in sorted(groups.items())],
        "default_branch": on_default,
    }


# ---------------------------------------------------------------- rendering

def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "-"


def _seconds(value: Any) -> str:
    if value is None:
        return "-"
    value = int(round(float(value)))
    hours, rest = divmod(value, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m" if hours else (f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s")


def _usd(value: Any) -> str:
    return "-" if value is None else f"${float(value):.2f}"


def _why(histogram: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in sorted(histogram.items(), key=lambda kv: (-kv[1], kv[0]))) or "-"


def _tables(data: dict[str, Any]) -> list[tuple[str, list[str], list[list[str]]]]:
    """(title, header, rows) of every table: the text and the Markdown output print the same cells."""
    runs, person, review, branch = data["runs"], data["person"], data["review"], data["default_branch"]
    total = runs["total"]
    outcomes = [["passed", str(runs["passed"]), _share(runs["passed"], total), ""],
                ["handed over", str(runs["handed_over"]), _share(runs["handed_over"], total), _why(runs["why"]["handed_over"])],
                ["  landed afterwards", str(runs["landed_afterwards"]), _share(runs["landed_afterwards"], total), ""],
                ["failed", str(runs["failed"]), _share(runs["failed"], total), _why(runs["why"]["failed"])],
                ["waiting", str(runs["waiting"]), _share(runs["waiting"], total), ""],
                ["running", str(runs["running"]), _share(runs["running"], total), ""]]
    if runs["other"]:
        outcomes.append(["other", str(runs["other"]), _share(runs["other"], total), ""])
    outcomes.append(["all", str(total), "", f"{runs['dry_runs']} dry"])
    attempts = [[name, str(a["records"]), f"{a['mean']:.2f}", str(a["max"]), _share(round(a["first_attempt_share"] * 1000), 1000)]
                for name, a in data["attempts"].items()]
    reviews = [[str(review["runs_reviewed"]), "-" if review["mean"] is None else f"{review['mean']:.2f}",
                "-" if review["max"] is None else str(review["max"]), str(review["runs_with_more_than_one"])]]
    acted = person["by_action"]
    people = [["runs needing a person", str(person["runs_needing_a_person"])],
              ["actions by a person", str(person["actions"])],
              ["  handed over", str(acted["handed_over"])],
              ["  waited at an approval", str(acted["waited_at_approval"])],
              ["  settled by a person", str(acted["settled"])],
              ["  started again by hand", str(acted["started_again"])],
              ["zero-touch runs", str(person["zero_touch_runs"])]]
    money = []
    for group in data["time_and_cost"]:
        duration, cost = group["duration_seconds"], group["cost_usd"]
        money.append([group["workflow"], group["class"], "dry" if group["dry_run"] else "real", str(group["runs"]),
                      _seconds(duration["median"]), _seconds(duration["total"]) if duration["runs"] else "-",
                      _usd(cost["median"]) if cost else "-", _usd(cost["total"]) if cost and cost["runs"] else "-",
                      str(cost["without_cost"]) if cost else "-"])
    default = [["changes on the default branch", str(branch["changes"])],
               ["  shipped by the gate", str(branch["shipped_by_the_gate"])],
               ["  handed over, landed afterwards", str(branch["landed_afterwards"])]]
    return [("Runs by outcome", ["outcome", "runs", "share", "why"], outcomes),
            ("Attempts per step", ["step", "records", "mean", "max", "first attempt"], attempts),
            ("Review rounds", ["runs reviewed", "mean", "max", "more than one round"], reviews),
            ("How often a person acted", ["", "count"], people),
            ("Time and cost by workflow and class",
             ["workflow", "class", "agent", "runs", "median time", "total time", "median cost", "total cost", "without cost"],
             money),
            ("Changes on the default branch", ["", "count"], default)]


def _scope(data: dict[str, Any]) -> str:
    count = data["runs"]["total"]
    return f"{count} run{'' if count == 1 else 's'}" + (f" started on or after {data['since']}" if data["since"] else "")


def render_text(data: dict[str, Any]) -> str:
    out = [f"Numbers from the run records: {_scope(data)}"]
    for title, header, rows in _tables(data):
        out += ["", title]
        if not rows:
            out.append("  (none)")
            continue
        widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(len(header))]
        right = [i > 0 and all(_NUMBER.match(row[i]) for row in rows) for i in range(len(header))]
        for line in ([header] if any(header) else []) + rows:
            out.append("  " + "  ".join(cell.rjust(widths[i]) if right[i] else cell.ljust(widths[i])
                                       for i, cell in enumerate(line)).rstrip())
    return "\n".join(out) + "\n"


def render_md(data: dict[str, Any], sample: bool = False, today: str = "") -> str:
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = ["# Numbers"]
    if sample:
        out.append(SAMPLE_LINE)
    out += ["", f"Generated by `tools/al kpis` on {today}: {_scope(data)}.", ""]
    for title, header, rows in _tables(data):
        out += [f"## {title}", ""]
        if not rows:
            out += ["None.", ""]
            continue
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "|".join("---" for _ in header) + "|")
        for row in rows:
            first = ("&nbsp;&nbsp;" + row[0].strip()) if row[0].startswith("  ") else row[0]
            out.append("| " + " | ".join([first, *row[1:]]) + " |")
        out.append("")
    out += ["## What is counted", ""]
    out += [f"- **{name}**: {text}" for name, text in DEFINITIONS]
    out += ["", "No run id, title, prompt, branch, address or path appears in this file."]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- tools/al

def main(a: Any, al: Any) -> int:
    import al_runs
    al_runs.AL = al
    if a.json and a.md:
        print("usage: al kpis LAYER [--since YYYY-MM-DD] [--json | --md]", file=sys.stderr)
        return 2
    try:
        since = parse_since(a.since)
    except Usage as exc:
        print(f"al kpis: {exc}\nusage: al kpis LAYER [--since YYYY-MM-DD] [--json | --md]", file=sys.stderr)
        return 2
    data = collect(a.layer.resolve(), since, a.since or "")
    if a.json:
        print(json.dumps(data, indent=1, sort_keys=True))
    elif a.md:
        sys.stdout.write(render_md(data, sample=bool(getattr(a, "sample", False))))
    else:
        sys.stdout.write(render_text(data))
    return 0
