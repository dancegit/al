"""Subscription usage: the 5-hour and weekly windows of an agent CLI that runs on a subscription.

Everything here comes from two events Claude Code writes into a call's stream-json transcript
(`agents/<id>/<step>/raw.jsonl`); nothing else is read (no credential file, no network endpoint,
no private CLI state):

  * `{"type": "system", "subtype": "init", "apiKeySource": …}`: `"none"` on a subscription login;
    `"ANTHROPIC_API_KEY"`, `"apiKeyHelper"` or `"/login managed key"` for an API key. From it the
    billing mode of the call is derived: `subscription`, `api-key`, or `""` (not said: never guessed).
  * `{"type": "rate_limit_event", "rate_limit_info": {status, resetsAt, rateLimitType,
    unifiedWindows: {<window>: {utilization, resetsAt, status}}, …}}`: `status` is allowed /
    allowed_warning / rejected, `resetsAt` is in epoch seconds, `utilization` is the share used
    (0–1). The public reference documents a smaller shape (`status`, `resetsAt?`, `utilization?`,
    `errorCode?`) without `rateLimitType` and `unifiedWindows`; both are read. The event has no
    time of its own: "as of" is the time of the nearest transcript line that has one.

Derived from them:

  * a window record `{window, status, used: 0–1 | None, resets_at: ISO-8601 UTC | None}` per window
    (the window names are the CLI's own keys: `seven_day`, `five_hour`, …);
  * `limit.status` events of the run, and `state.usage` (see state.py);
  * the layer-level file `agents/subscription-usage.json`, the latest report per window over all
    runs of the layer (written under an flock on `agents/subscription-usage.lock`):

        {"version": 1, "billing": "subscription", "api_key_source": "none", "billing_at": "…",
         "billing_run": "1a2b3c4d",
         "windows": {"seven_day": {"window": "seven_day", "status": "allowed", "used": 0.57,
                                   "resets_at": "2026-11-19T14:00:00+00:00",
                                   "at": "2026-11-15T18:57:41.294Z", "run": "1a2b3c4d", "step": "build"}}}

  * a failed call whose transcript says `rejected` stops the run (LimitReached "limit:<window>: …"),
    never retried, handed to a person like any stopped run;
  * (budget limits #:limit-stop-percent N #:on-limit stop|wait #:limit-wait-minutes M): before an
    agent call, when the file says that N% or more of a window is used and its reset has not passed,
    the run stops (`stop`) or sleeps until the reset (`wait`, at most M minutes; else it stops).
    With `stop` a new run does not start at all (budget.refuse_start).

Usage is known only as of the last agent call of this layer; agents that report nothing (codex,
custom CLIs, a plain mock) record nothing.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import events
from .errors import StepFailed

FILE_NAME = "subscription-usage.json"
LOCK_NAME = "subscription-usage.lock"
SUBSCRIPTION = "subscription"
API_KEY = "api-key"
API_KEY_SOURCES = ("ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key")
LABELS = {"five_hour": "5-hour", "seven_day": "weekly"}
DEFAULT_WAIT_MINUTES = 300

# the clock, replaceable by tests: always called through the module attribute
now = time.time
sleep = time.sleep

_warned: set[str] = set()   # status files a warning was already printed for (one line per file)


class LimitReached(StepFailed):
    """A subscription usage limit stopped the run. Never retried."""

    def __init__(self, step: str, reason: str, window: str, cause: str, resets_at: str | None) -> None:
        self.window, self.cause, self.resets_at = window, cause, resets_at
        super().__init__(step, reason)


# ---------------------------------------------------------------- pure helpers

def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def billing_of(source: Any) -> str:
    """The billing mode an init event's `apiKeySource` names ("" when it names none we know)."""
    if source == "none":
        return SUBSCRIPTION
    if isinstance(source, str) and source in API_KEY_SOURCES:
        return API_KEY
    return ""


def label(window: str) -> str:
    return LABELS.get(window) or str(window).replace("_", " ")


def iso(epoch_seconds: Any) -> str | None:
    """Epoch seconds as ISO-8601 UTC (None for anything that is not a usable number)."""
    if not _number(epoch_seconds):
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def epoch(stamp: Any) -> float | None:
    """An ISO-8601 time as epoch seconds (`Z` accepted; None when it cannot be read)."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def when(stamp: str | None) -> str:
    seconds = epoch(stamp)
    if seconds is None:
        return "the CLI reported no reset time"
    return "resets " + datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def windows(info: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The window records of one `rate_limit_info`."""
    out: list[dict[str, Any]] = []
    unified = info.get("unifiedWindows")
    unified = unified if isinstance(unified, dict) else {}
    limit_type = info.get("rateLimitType")

    def finish(record: dict[str, Any]) -> dict[str, Any]:
        if record["status"] == "rejected" and "errorCode" in info:
            record["error_code"] = info.get("errorCode")
        return record

    for name, window in unified.items():
        if not isinstance(window, dict):
            continue
        resets = (window.get("resetsAt") or info.get("resetsAt")) if name == limit_type else window.get("resetsAt")
        out.append(finish({"window": str(name), "status": str(window.get("status") or info.get("status") or ""),
                           "used": float(window["utilization"]) if _number(window.get("utilization")) else None,
                           "resets_at": iso(resets)}))
    if not unified or limit_type not in unified:
        out.append(finish({"window": str(limit_type or "unknown"), "status": str(info.get("status") or ""),
                           "used": float(info["utilization"]) if _number(info.get("utilization")) else None,
                           "resets_at": iso(info.get("resetsAt"))}))
    return out


def scan(raw_path: Path) -> dict[str, Any]:
    """{"billing", "api_key_source", "reports": [{"at": ISO | None, "info": dict}]} of one transcript.

    Only lines that can matter are parsed. Never raises: an unreadable file or line is skipped."""
    found: dict[str, Any] = {"billing": "", "api_key_source": None, "reports": []}
    try:
        data = Path(raw_path).read_bytes()
    except OSError:
        return found
    seen_init = False
    last_time: str | None = None
    waiting: list[dict[str, Any]] = []   # reports before the first line that has a time
    for line in data.splitlines():
        if b'"rate_limit_event"' not in line and b'"init"' not in line and b'"timestamp"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        stamp = obj.get("timestamp")
        if isinstance(stamp, str) and stamp:
            last_time = stamp
            for report in waiting:
                report["at"] = stamp
            waiting = []
        if obj.get("type") == "system" and obj.get("subtype") == "init" and not seen_init:
            seen_init = True
            source = obj.get("apiKeySource")
            found["billing"] = billing_of(source)
            found["api_key_source"] = source if isinstance(source, str) else None
        elif obj.get("type") == "rate_limit_event" and isinstance(obj.get("rate_limit_info"), dict):
            report = {"at": last_time, "info": obj["rate_limit_info"]}
            found["reports"].append(report)
            if last_time is None:
                waiting.append(report)
    return found


def rejection(found: Mapping[str, Any]) -> dict[str, Any] | None:
    """The last window record that says `rejected` (the CLI refused or cut off the call), or None."""
    hit: dict[str, Any] | None = None
    for report in found.get("reports") or []:
        info = report["info"]
        records = windows(info)
        rejected = [record for record in records if record["status"] == "rejected"]
        if rejected:
            hit = rejected[-1]
        elif info.get("status") == "rejected":
            name = str(info.get("rateLimitType") or "unknown")
            base = next((record for record in records if record["window"] == name),
                        {"window": name, "used": None, "resets_at": iso(info.get("resetsAt"))})
            hit = {**base, "status": "rejected"}
            if "errorCode" in info:
                hit["error_code"] = info.get("errorCode")
    return hit


# ---------------------------------------------------------------- the layer-level status file

def path(layer_dir: Any) -> Path:
    return Path(layer_dir) / "agents" / FILE_NAME


def load(layer_dir: Any) -> dict[str, Any]:
    """The status file ({} when it is missing, unreadable or not a JSON object)."""
    try:
        data = json.loads(path(layer_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stamp() -> str:
    return datetime.fromtimestamp(now(), timezone.utc).isoformat(timespec="seconds")


def _update_file(layer_dir: Any, found: Mapping[str, Any], latest: Mapping[str, dict[str, Any]], adw_id: str) -> None:
    target = path(layer_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(target.with_name(LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        data = load(layer_dir)
        data["version"] = 1
        if found.get("billing"):
            data.update(billing=found["billing"], api_key_source=found.get("api_key_source"),
                        billing_at=_stamp(), billing_run=adw_id)
        kept = data.get("windows")
        kept = kept if isinstance(kept, dict) else {}
        kept.update(latest)
        data["windows"] = kept
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, prefix=".usage-",
                                             delete=False) as out:
                temporary = out.name
                json.dump(data, out, indent=2, sort_keys=True)
                out.write("\n")
            os.replace(temporary, target)
        except BaseException:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass  # the temp file is already gone; the original error is the one to report
            raise
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def record(layer: Any, state: Any, step: str, owner: str, raw_path: Path) -> dict[str, Any]:
    """Record what a finished call's transcript says about billing and usage windows: `limit.status`
    events, state.usage and the layer-level file. Returns the scan. Never raises."""
    found = scan(raw_path)
    try:
        emitted: dict[str, tuple[Any, Any, Any]] = {}
        latest: dict[str, dict[str, Any]] = {}
        for report in found["reports"]:
            at = report["at"] or events.timestamp()
            for item in windows(report["info"]):
                name = item["window"]
                key = (item["status"], item["used"], item["resets_at"])
                if name not in emitted or emitted[name] != key:
                    emitted[name] = key
                    events.emit("limit.status", step=step, owner=owner, window=name, status=item["status"],
                                used=item["used"], resets_at=item["resets_at"], at=report["at"],
                                billing=found["billing"], raw=report["info"])
                state.usage.setdefault("windows", {})[name] = {**item, "at": at, "step": step}
                latest[name] = {"window": name, "status": item["status"], "used": item["used"],
                                "resets_at": item["resets_at"], "at": at, "run": state.adw_id, "step": step}
        if found["billing"]:
            state.usage["billing"] = found["billing"]
        if found["billing"] or latest:
            _update_file(layer.layer_dir, found, latest, state.adw_id)
    except (OSError, ValueError, TypeError) as exc:
        target = str(path(layer.layer_dir))
        if target not in _warned:
            _warned.add(target)
            print(f"[usage] warning: cannot record subscription usage in {target}: {type(exc).__name__}: {exc}; "
                  "continuing without it", file=sys.stderr, flush=True)
    return found


# ---------------------------------------------------------------- stopping and waiting

def _share(used: Any) -> str:
    return f"{round(float(used) * 100, 2):g}"


def _threshold_reason(item: Mapping[str, Any], percent: int, extra: str = "") -> str:
    return (f"limit:{item['window']}: {_share(item['used'])}% of the {label(item['window'])} subscription limit is "
            f"used (threshold {percent}%, #:limit-stop-percent); {when(item.get('resets_at'))}{extra}")


def reached(state: Any, step: str, item: Mapping[str, Any], cause: str = "reached", percent: int | None = None,
            extra: str = "") -> LimitReached:
    """Record the stop (state.usage["stopped"], a `limit.stop` event) and return the exception to raise."""
    window = str(item["window"])
    if cause == "threshold":
        reason = _threshold_reason(item, int(percent or 0), extra)
    else:
        reason = (f"limit:{window}: the {label(window)} subscription limit is reached (the CLI refused the call); "
                  f"{when(item.get('resets_at'))}"
                  + ("; usage credits are required" if item.get("error_code") == "credits_required" else "") + extra)
    stopped = {"window": window, "cause": cause, "status": str(item.get("status") or ""), "used": item.get("used"),
               "percent": percent, "resets_at": item.get("resets_at"), "step": step, "reason": reason}
    state.usage["stopped"] = stopped
    events.emit("limit.stop", **stopped)
    return LimitReached(step, reason, window, cause, item.get("resets_at"))


def over(layer_dir: Any, percent: int) -> list[dict[str, Any]]:
    """The windows of the status file at or above `percent` whose reset has not passed, by name."""
    if percent <= 0:
        return []
    data = load(layer_dir)
    listed = data.get("windows")
    if data.get("billing") == API_KEY or not isinstance(listed, dict) or not listed:
        return []
    hits = []
    for name in sorted(listed, key=str):
        item = listed[name]
        if not isinstance(item, dict) or not _number(item.get("used")):
            continue
        if round(float(item["used"]) * 100, 2) < percent:
            continue
        resets = item.get("resets_at") if isinstance(item.get("resets_at"), str) else None
        ends = epoch(resets)
        if ends is not None and ends <= now():
            continue   # that window has reset since the report: it says nothing about now
        hits.append({"window": str(name), "status": str(item.get("status") or ""), "used": float(item["used"]),
                     "resets_at": resets if ends is not None else None})
    return hits


def _worst(hits: list[dict[str, Any]]) -> dict[str, Any]:
    """The hit that resets last (no reset time counts as last; then the last by window name)."""
    def key(item: dict[str, Any]) -> tuple[float, str]:
        ends = epoch(item.get("resets_at"))
        return (float("inf") if ends is None else ends, item["window"])
    return max(hits, key=key)


def before_call(layer: Any, state: Any, step: str) -> None:
    """Stop (or wait) before an agent call when a usage window is at the layer's threshold."""
    lim = state.budget.get("limits") or {}
    percent = int(lim.get("limit_percent") or 0)
    hits = over(layer.layer_dir, percent)
    if not hits:
        return
    worst = _worst(hits)
    if lim.get("on_limit") != "wait":
        raise reached(state, step, worst, cause="threshold", percent=percent)
    minutes = int(lim.get("wait_minutes") or DEFAULT_WAIT_MINUTES)
    ends = epoch(worst.get("resets_at"))
    if ends is None:
        raise reached(state, step, worst, cause="threshold", percent=percent,
                      extra="; without a reset time there is nothing to wait for")
    if ends - now() > minutes * 60:
        raise reached(state, step, worst, cause="threshold", percent=percent,
                      extra=f"; that is more than the {minutes} minutes #:on-limit wait may wait")
    seconds = max(0.0, ends - now())
    events.say(f"[adw {state.adw_id}] step {step}: {_share(worst['used'])}% of the {label(worst['window'])} "
               f"subscription limit is used (threshold {percent}%); waiting {round(seconds)}s for its reset at "
               f"{when(worst['resets_at']).removeprefix('resets ')}", "limit.wait", step=step, window=worst["window"],
               used=worst["used"], percent=percent, resets_at=worst["resets_at"], seconds=round(seconds, 3))
    state.usage.setdefault("waits", []).append({"step": step, "window": worst["window"], "seconds": round(seconds, 3),
                                                "until": worst["resets_at"]})
    state.save(layer.layer_dir)
    sleep(seconds)


def refuse_start(layer: Any, lim: Mapping[str, Any]) -> str | None:
    """The reason a run may not start (a usage window is at the threshold, with #:on-limit stop), or None."""
    if lim.get("on_limit") == "wait":
        return None
    percent = int(lim.get("limit_percent") or 0)
    hits = over(layer.layer_dir, percent)
    if not hits:
        return None
    return (_threshold_reason(_worst(hits), percent)
            + "; no run starts until then, or raise #:limit-stop-percent in (budget limits …)")
