"""The flask layer with the mock agent declared in the DSL: tasklist + cron, concurrency slots, killed runs."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime

import pytest

import alit

PROMPT = "Add a note to the README about running the tests"
WAITING = re.compile(r"^\[slots\] .*waiting for a free runs slot", re.M)


def test_tasklist_cron(layer):
    lay = layer("flask", *alit.mock_agent())
    tasks = lay.dir / "tasks.md"
    with tasks.open("a") as handle:
        handle.write("\n## readme-note\n"
                     f"- [ ] {PROMPT} {{plan-build}}\n"
                     "- [>] Mention tox in that note {plan-build}\n")

    first = lay.cron_once()
    assert first.returncode == 0, alit.describe(first)
    text = tasks.read_text()
    assert re.search(r"^- \[x\] Add a note .*adw:[0-9a-f]{8}", text, re.M), text
    assert re.search(r"^- \[>\] Mention tox", text, re.M), "the blocked task must still be waiting:\n" + text
    assert len(lay.states()) == 1

    second = lay.cron_once()
    assert second.returncode == 0, alit.describe(second)
    text = tasks.read_text()
    assert re.search(r"^- \[x\] Mention tox .*adw:[0-9a-f]{8}", text, re.M), text
    states = lay.states()
    assert len(states) == 2 and {s["status"] for s in states.values()} == {"passed"}
    assert all(s["dry_run"] is False for s in states.values()), "the mock agent comes from the DSL, not from --dry-run"
    ids = set(re.findall(r"adw:([0-9a-f]{8})", text))
    assert ids == set(states), "the ids written into tasks.md are the runs recorded under agents/"


def _held(state: dict) -> tuple[datetime, datetime]:
    """The span in which a run certainly held its `runs` slot: first step started -> last step ended."""
    stamps = [datetime.fromisoformat(s[key]) for s in state["steps"] for key in ("started", "ended") if s[key]]
    return min(stamps), max(stamps)


def _max_overlap(spans: list[tuple[datetime, datetime]]) -> int:
    events = sorted([(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans], key=lambda e: (e[0], e[1]))
    level = peak = 0
    for _, delta in events:
        level += delta
        peak = max(peak, level)
    return peak


def test_concurrency_soak(layer):
    runs, limit = 8, 2
    lay = layer("flask", *alit.mock_agent(), alit.concurrency(max_runs=limit))
    assert lay.ir["concurrency"]["maxRuns"] == limit

    procs = [lay.popen_adw("plan-build", f"{PROMPT} ({n})") for n in range(runs)]
    outputs = [p.communicate(timeout=600)[0] for p in procs]
    for proc, output in zip(procs, outputs):
        assert proc.returncode == 0, alit.describe(output=output)

    states = lay.states()
    assert len(states) == runs and {s["status"] for s in states.values()} == {"passed"}
    peak = _max_overlap([_held(s) for s in states.values()])
    waited = sum(bool(WAITING.search(output)) for output in outputs)
    assert peak <= limit, f"{peak} runs held a slot at the same time, the limit is {limit}"
    # the limit was actually exercised: with 8 simultaneous starts and 2 slots somebody had to queue
    assert waited >= 1, "no run ever waited for a slot: the soak did not create contention"

    ports = lay.dir / "agents" / ".ports.json"
    assert not ports.exists() or json.loads(ports.read_text()) == {}, ports.read_text()
    assert lay.trees() == [] and alit.worktrees_under(lay.app, lay.dir) == []
    left = {s["branch"] for s in states.values()} & set(alit.heads(lay.app))
    assert not left, f"branches with nothing unshipped must be deleted with their worktree: {sorted(left)}"


def _wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _kill_mid_check(layer):
    """A layer with one slot per pool, and a run SIGKILLed while its check sleeps. -> (layer, dead state)"""
    # one slot per pool: a slot leaked by the dead run would make every later run queue
    lay = layer("flask", *alit.mock_agent(), alit.concurrency(max_runs=1, max_checks=1, wait_seconds=25),
                alit.add_cmd("nap", "sleep 30"), alit.add_cmd("quick", "true"),
                alit.append_clause("(adw nap #:input (cli) #:trigger (manual) #:environment (worktree) #:review human "
                                   "#:agent mock (steps (check nap #:cmd nap)))"),
                alit.append_clause("(adw quick #:input (cli) #:trigger (manual) #:environment (worktree) #:review human "
                                   "#:agent mock (steps (check quick #:cmd quick)))"))
    victim = lay.popen_adw("nap", "sleep through the check")
    try:
        def napping() -> dict | None:
            for state in lay.states().values():
                if any(s["name"] == "nap" and s["status"] == "running" for s in state["steps"]):
                    return state
            assert victim.poll() is None, "the nap run ended before its check started:\n" + (victim.stdout.read() or "")
            return None
        dead = _wait_for(napping, 60, "the nap check to start")
        time.sleep(0.5)  # be inside the sleep, holding one runs slot and one checks slot
        os.killpg(victim.pid, signal.SIGKILL)
        victim.wait(timeout=30)
    finally:
        if victim.poll() is None:
            os.killpg(victim.pid, signal.SIGKILL)
    assert victim.returncode == -signal.SIGKILL
    return lay, dead


def test_killed_run_leaks_nothing_permanent(layer):
    lay, dead = _kill_mid_check(layer)

    # what the kill leaves behind is recoverable state, not held resources
    assert lay.states()[dead["adw_id"]]["status"] == "running"  # nobody was alive to record the failure
    assert lay.trees() == [dead["adw_id"]]

    started = time.monotonic()
    normal = lay.run_adw("plan-build", PROMPT, timeout=120)
    took = time.monotonic() - started
    assert normal.returncode == 0, alit.describe(normal)
    assert "waiting for a free" not in normal.stdout, "the dead run's runs slot was not released:\n" + normal.stdout
    assert took < 20, f"the run after the kill took {took:.1f}s"

    check = lay.run_adw("quick", "run one check", timeout=120)  # needs the checks slot the dead run held
    assert check.returncode == 0, alit.describe(check)
    assert "waiting for a free" not in check.stdout, "the dead run's checks slot was not released:\n" + check.stdout

    passed = [s for s in lay.states().values() if s["status"] == "passed"]
    assert sorted(s["adw"] for s in passed) == ["plan-build", "quick"]
    assert lay.trees() == [dead["adw_id"]], "only the killed run's worktree is left (for --resume or gc)"
    # the fixture teardown removes that worktree, its branch and the orphaned `sleep`, then checks the testbed


@pytest.mark.xfail(strict=True, reason="kit/runtime envs.lease_ports: a SIGKILLed run's port block stays in "
                   "agents/.ports.json for as long as its agents/<id>/state.json exists (i.e. forever: the state "
                   "also stays `running`); leases are not tied to a live process the way slots are")
def test_killed_run_port_lease_is_reclaimed(layer):
    lay, dead = _kill_mid_check(layer)
    assert dead["ports"], "the nap run leased a port block"
    normal = lay.run_adw("plan-build", PROMPT, timeout=120)  # the next lease is the chance to drop dead ones
    assert normal.returncode == 0, alit.describe(normal)
    leases = json.loads((lay.dir / "agents" / ".ports.json").read_text())
    assert dead["adw_id"] not in leases, f"dead run {dead['adw_id']} still holds ports {leases[dead['adw_id']]}"
