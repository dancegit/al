"""Sanitised fixture layers for the dashboard tests, written as code so every byte is reviewable.

    python fixtures.py OUTDIR      # writes OUTDIR/demo-shop.al, OUTDIR/repo/…, OUTDIR/outside-secret.txt

The shapes mirror what the al runtime writes (kit/runtime/adw_modules/state.py, steps.py, agents.py,
review.py, ship.py, fanout.py, artefacts.py, slots.py, envs.py) and what Claude Code's stream-json
output contains. Paths are invented (/work/…); no real run data is copied.

Runs in demo-shop.al (ADW, what it covers):
  a1000001 zte      passed, merged; Claude transcript with every event type; artefacts (log, image, video)
  a1000002 zte      failed: test check red after two resolve agents, handed off (PR.md)
  a1000003 zte      passed after a resolve loop on lint (lint.1 red, lint-resolve-1, lint.2 green)
  a1000004 zte      two review rounds: a blocker, patch-1 + implement-1, re-checks, round 2 approves
  a1000005 race     fan-out of three candidate runs (c1000001..3), one of them failed; best picked
  a1000006 zte      still running (build step, transcript growing, last line partial); holds slots
  a1000007 zte      hostile content: HTML/script in request, transcript, tool output, review issues
  a1000008 zte      transcript with corrupt and truncated JSON lines; failed at classify
  a1000009 -        no state.json (only run.log and a prompt)
  a100000a ask      plain-text agent (codex-style output)
  a100000b zte      dry run on the mock agent (classify only)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data"
T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
LAYER_DIR = "/work/demo-shop.al"   # what the runtime would have recorded as absolute paths


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iso(seconds: float) -> str:
    return at(seconds).isoformat()


def write(path: Path, content: str | bytes, when: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    if when is not None:
        stamp = at(when).timestamp()
        os.utime(path, (stamp, stamp))
    return path


# ------------------------------------------------------------------------------ the layer IR

def step_run(name: str, command: str, when: str = "always", args: list[str] | None = None, agent: str = "") -> dict:
    return {"agent": agent, "args": args or ["$prompt"], "command": command, "kind": "run", "modelSet": "inherit",
            "name": name, "retries": 0, "when": when}


def step_check(name: str, retries: int = 2) -> dict:
    return {"cmd": name, "collect": ["log"], "kind": "check", "name": name, "resolve": "resolve-test", "retries": retries}


PLAN_STEPS = [step_run("classify", "classify"), step_run("plan-chore", "chore", "class-chore"),
              step_run("plan-bug", "bug", "class-bug"), step_run("plan-feature", "feature", "class-feature"),
              step_run("build", "implement", args=["$plan"])]

IR: dict[str, Any] = {
    "schemaVersion": 1, "name": "demo-shop", "description": "Fixture layer for the dashboard tests",
    "codebase": {"root": "../demo-shop", "defaultBranch": "main",
                 "commands": [{"name": "lint", "run": "ruff check ."}, {"name": "test", "run": "pytest -q"}]},
    "agents": [{"id": "claude", "kind": "claude-code"}, {"id": "codex", "kind": "codex"}, {"id": "mock", "kind": "mock"}],
    "reviews": [{"id": "human", "kind": "human"},
                {"id": "zero-touch", "kind": "auto", "reviewers": 2, "maxRetries": 1, "ship": "merge",
                 "requireChecks": ["lint", "test"], "allowedClasses": ["chore", "bug"], "maxDiffLines": 300,
                 "artefacts": ["log", "screenshots", "recording"]}],
    "concurrency": {"id": "slots", "kind": "slots", "maxRuns": 2, "maxAgents": 1, "maxChecks": 1, "waitSeconds": 3600},
    "adws": [
        {"name": "zte", "description": "Zero-touch: plan, build, lint, test, commit, two reviewers, gate, merge",
         "agent": "claude", "classes": ["chore", "bug"], "environment": ["worktree", "podman"], "input": ["cli"],
         "review": "zero-touch", "trigger": ["manual"],
         "steps": [*PLAN_STEPS, step_check("lint", 1), step_check("test", 2), step_run("commit", "commit", args=[]),
                   {"implement": "implement", "kind": "review", "name": "review", "patch": "plan-patch"},
                   {"kind": "ship", "message": "", "name": "ship"}]},
        {"name": "candidate", "description": "One candidate of a race", "agent": "claude", "classes": [],
         "environment": ["worktree"], "input": ["cli"], "review": "human", "trigger": ["manual"],
         "steps": [step_run("plan", "chore"), step_run("build", "implement", args=["$plan"]), step_check("test", 1),
                   step_run("commit", "commit", args=[])]},
        {"name": "race", "description": "Three candidates race; a human reviews the best", "agent": "claude",
         "classes": [], "environment": ["worktree"], "input": ["cli"], "review": "human", "trigger": ["manual"],
         "steps": [{"adw": "candidate", "count": 3, "kind": "fan-out", "maxParallel": 2, "name": "race", "pick": "best"},
                   step_check("test", 1), {"implement": "", "kind": "review", "name": "review", "patch": ""},
                   {"kind": "ship", "message": "", "name": "ship"}]},
        {"name": "ask", "description": "Answer a question about the codebase", "agent": "codex", "classes": [],
         "environment": ["worktree"], "input": ["cli"], "review": "human", "trigger": ["manual"],
         "steps": [step_run("answer", "question")]},
    ],
}


# ------------------------------------------------------------------------------ transcripts

class Claude:
    """Builds a Claude Code stream-json transcript (the event shapes found in real runs)."""

    def __init__(self, start: float, cwd: str) -> None:
        self.t = start
        self.lines: list[str] = []
        self.session = "00000000-0000-4000-8000-000000000001"
        self.n = 0
        self.message = 0
        self.add({"type": "system", "subtype": "init", "cwd": cwd, "session_id": self.session,
                  "tools": ["Bash", "Edit", "Glob", "Grep", "Read", "Write", "Task", "TodoWrite"], "mcp_servers": [],
                  "model": "claude-opus-5-5", "permissionMode": "acceptEdits", "apiKeySource": "none",
                  "uuid": "u-init"}, stamp=False)
        self.add({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day",
                  "unifiedWindows": {"seven_day": {"utilization": 0.12, "status": "allowed"}}},
                  "uuid": "u-rl", "session_id": self.session}, stamp=False)

    def add(self, event: dict, stamp: bool = True, advance: float = 1.0) -> None:
        self.t += advance
        if stamp:
            event["timestamp"] = at(self.t).isoformat().replace("+00:00", "Z")
        self.lines.append(json.dumps(event))

    def _assistant(self, block: dict, out: int = 20) -> None:
        self.message += 1
        self.add({"type": "assistant", "message": {"model": "claude-opus-5-5", "id": f"msg_{self.message:04d}",
                  "type": "message", "role": "assistant", "content": [block], "stop_reason": None,
                  "usage": {"input_tokens": 3, "output_tokens": out, "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 200}},
                  "parent_tool_use_id": None, "session_id": self.session, "uuid": f"u-a{self.message}"})

    def thinking(self) -> None:
        self.add({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50,
                  "estimated_tokens_delta": 50, "session_id": self.session, "uuid": "u-tt"}, stamp=False, advance=0.5)
        self._assistant({"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl"})

    def text(self, text: str) -> None:
        self._assistant({"type": "text", "text": text})

    def tool(self, name: str, data: dict, result: Any, *, error: bool = False, meta: dict | None = None,
             task: bool = False) -> str:
        self.n += 1
        tid = f"toolu_{self.n:04d}"
        self._assistant({"type": "tool_use", "id": tid, "name": name, "input": data, "caller": {"type": "direct"}})
        if task:
            self.add({"type": "system", "subtype": "task_started", "task_id": f"t{self.n}", "tool_use_id": tid,
                      "description": data.get("command", ""), "task_type": "local_bash", "session_id": self.session,
                      "uuid": f"u-ts{self.n}"}, stamp=False, advance=2)
            self.add({"type": "system", "subtype": "task_notification", "task_id": f"t{self.n}", "tool_use_id": tid,
                      "status": "completed", "summary": data.get("command", ""), "session_id": self.session,
                      "uuid": f"u-tn{self.n}"}, stamp=False, advance=2)
            self.add({"type": "system", "subtype": "ui_invalidate", "event": "ui.render", "session_id": self.session,
                      "uuid": f"u-ui{self.n}"}, stamp=False, advance=0)
        self.add({"type": "user", "message": {"role": "user", "content": [
            {"tool_use_id": tid, "type": "tool_result", "content": result, "is_error": error}]},
            "parent_tool_use_id": None, "session_id": self.session, "uuid": f"u-r{self.n}",
            "tool_use_result": meta or {}})
        return tid

    def final(self, result: str, cost: float, turns: int, duration_ms: int, *, error: bool = False,
              subtype: str = "success") -> None:
        self.add({"type": "result", "subtype": subtype, "is_error": error, "duration_ms": duration_ms,
                  "duration_api_ms": duration_ms - 500, "num_turns": turns, "result": result,
                  "stop_reason": "end_turn", "session_id": self.session, "total_cost_usd": cost,
                  "usage": {"input_tokens": 40, "output_tokens": 900, "cache_read_input_tokens": 60000,
                            "cache_creation_input_tokens": 5000},
                  "modelUsage": {"claude-opus-5-5": {"inputTokens": 40, "outputTokens": 900, "costUSD": cost}},
                  "uuid": "u-result"}, stamp=False)

    def body(self) -> str:
        return "\n".join(self.lines) + "\n"


def mock_raw(output: str) -> str:
    return json.dumps({"synthesised": True, "output": output}) + "\n"


# ------------------------------------------------------------------------------ runs

def state(run_id: str, adw: str, prompt: str, status: str, start: float, end: float | None, steps: list[dict],
          **extra: Any) -> dict:
    base = {"adw_id": run_id, "adw": adw, "prompt": prompt, "source": "cli", "klass": "chore", "class_fixed": False,
            "dry_run": False, "model_set": "", "branch": f"chore/{run_id}-demo", "worktree": f"{LAYER_DIR}/trees/{run_id}",
            "ports": [], "plan": f"{LAYER_DIR}/specs/chore-{run_id}.md", "outputs": {}, "steps": steps, "checks": {},
            "artefacts": [], "review": {}, "ship": {}, "fanout": {}, "status": status, "started": iso(start),
            "ended": iso(end) if end is not None else ""}
    base.update(extra)
    return base


def rec(name: str, kind: str, status: str, start: float | None, end: float | None, detail: str = "", attempts: int = 1) -> dict:
    return {"name": name, "kind": kind, "status": status, "attempts": attempts if status != "skipped" else 0,
            "started": iso(start) if start is not None else "", "ended": iso(end) if end is not None else "",
            "detail": detail}


def plan_records(t: float, klass: str = "chore") -> tuple[list[dict], float]:
    out = [rec("classify", "run", "passed", t, t + 10, '{"class":"chore"}')]
    t += 10
    for name, when in (("plan-chore", "chore"), ("plan-bug", "bug"), ("plan-feature", "feature")):
        if when == klass:
            out.append(rec(name, "run", "passed", t, t + 30, f"{LAYER_DIR}/specs/chore.md"))
            t += 30
        else:
            out.append(rec(name, "run", "skipped", t, t, f"when=class-{when}, class={klass}"))
    out.append(rec("build", "run", "passed", t, t + 50, "Added the test."))
    return out, t + 50


def agent(run: Path, name: str, start: float, end: float | None, raw: str, prompt: str = "", output: str | None = "ok") -> None:
    directory = run / name
    write(directory / "prompt.md", prompt or f"---\ndescription: {name}\n---\n# {name}\n\nDo the {name} step.\n", start)
    write(directory / "raw.jsonl", raw, end if end is not None else start + 1)
    if output is not None and end is not None:
        write(directory / "output.txt", output, end)


def simple_claude(start: float, end: float, cwd: str, text: str, cost: float, turns: int = 3) -> str:
    claude = Claude(start, cwd)
    claude.thinking()
    claude.text(text)
    claude.final(text, cost, turns, int((end - start) * 1000))
    return claude.body()


def build_transcript(start: float, end: float, cwd: str) -> str:
    """The showcase transcript: every event type found in real runs."""
    claude = Claude(start, cwd)
    claude.thinking()
    claude.text("I'll read the plan first, then add the test.")
    claude.tool("Read", {"file_path": f"{LAYER_DIR}/specs/chore-a1000001.md"},
                "1\t# Chore: test the cart total\n2\t\n3\tAdd a test for Cart.total().\n",
                meta={"type": "text", "file": {"filePath": f"{LAYER_DIR}/specs/chore-a1000001.md", "numLines": 3}})
    claude.tool("Grep", {"pattern": "def total", "path": f"{cwd}/shop", "output_mode": "content"},
                "shop/cart.py:12:    def total(self) -> int:")
    claude.tool("Glob", {"pattern": "tests/test_*.py"}, [{"type": "text", "text": "tests/test_cart.py\ntests/test_api.py"}])
    claude.tool("Edit", {"file_path": f"{cwd}/tests/test_cart.py", "old_string": "def test_empty():\n    assert Cart().total() == 0\n",
                         "new_string": "def test_empty():\n    assert Cart().total() == 0\n\n\ndef test_two_items():\n    cart = Cart()\n    cart.add(Item(3))\n    cart.add(Item(4))\n    assert cart.total() == 7\n"},
                f"The file {cwd}/tests/test_cart.py has been updated successfully.",
                meta={"filePath": f"{cwd}/tests/test_cart.py", "structuredPatch": [
                    {"oldStart": 8, "oldLines": 2, "newStart": 8, "newLines": 9,
                     "lines": [" def test_empty():", "     assert Cart().total() == 0", "+", "+",
                               "+def test_two_items():", "+    cart = Cart()", "+    cart.add(Item(3))",
                               "+    cart.add(Item(4))", "+    assert cart.total() == 7"]}]})
    claude.tool("Write", {"file_path": f"{cwd}/tests/conftest.py", "content": "import pytest\n\n\n@pytest.fixture\ndef cart():\n    return Cart()\n"},
                f"File created successfully at: {cwd}/tests/conftest.py")
    claude.tool("Bash", {"command": "uv run --locked pytest -q tests/test_cart.py 2>&1 | tail -5", "description": "Run the cart tests"},
                "", error=True, meta={"stdout": "", "stderr": "E   NameError: name 'Item' is not defined", "interrupted": False},
                task=True)
    claude.thinking()
    claude.tool("Edit", {"file_path": f"{cwd}/tests/test_cart.py", "old_string": "from shop.cart import Cart\n",
                         "new_string": "from shop.cart import Cart, Item\n"}, "ok")
    claude.tool("Bash", {"command": "uv run --locked pytest -q tests/test_cart.py", "description": "Run the cart tests again"},
                "..\n2 passed in 0.03s", meta={"stdout": "..\n2 passed in 0.03s", "stderr": ""}, task=True)
    claude.tool("TodoWrite", {"todos": [{"content": "Add the test", "status": "completed"},
                                        {"content": "Run the suite", "status": "completed"}]}, "Todos updated")
    claude.add({"type": "user", "message": {"role": "user", "content": "Continue."}, "session_id": claude.session,
                "uuid": "u-plain"})
    claude.text("Added `test_two_items` to tests/test_cart.py; the cart tests pass.")
    claude.final("Added `test_two_items` to tests/test_cart.py; the cart tests pass.", 0.1834, 14,
                 int((end - start) * 1000))
    return claude.body()


def build(root: Path) -> dict[str, Path]:
    """Write every fixture below `root`; returns the interesting paths."""
    root = Path(root)
    layer = root / "demo-shop.al"
    if layer.exists():
        shutil.rmtree(layer)
    write(layer / "agentic-layer.json", json.dumps(IR, indent=2))
    write(layer / "agentic.rkt", "#lang al\n;; fixture: the IR in agentic-layer.json is hand-written\n")
    write(layer / "EXPLAIN.md", "# demo-shop, in plain words\n\nAgents plan, build and test changes to a toy shop.\n")
    write(layer / "specs" / "chore-a1000001.md", "# Chore: test the cart total\n")
    write(layer / "commands" / "secret-template.md", "not served: commands/ is outside agents/, artefacts/, specs/\n")
    agents_dir = layer / "agents"
    write(agents_dir / "trigger-logs" / "20261101-100000-plan-build.log", "a trigger log, not a run\n")
    cwd = lambda rid: f"{LAYER_DIR}/trees/{rid}"  # noqa: E731

    # ---- a1000001: passed, merged, full transcript, artefacts
    rid, run = "a1000001", agents_dir / "a1000001"
    records, t = plan_records(0)
    agent(run, "classify", 0, 10, simple_claude(0, 10, cwd(rid), '```json\n{"class":"chore"}\n```', 0.0412), output='{"class":"chore"}')
    agent(run, "plan-chore", 10, 40, simple_claude(10, 40, cwd(rid), f"{LAYER_DIR}/specs/chore-a1000001.md", 0.1500, 6),
          output=f"{LAYER_DIR}/specs/chore-a1000001.md")
    agent(run, "build", 40, 90, build_transcript(40, 90, cwd(rid)), output="Added test_two_items.")
    write(run / "checks" / "lint.1.log", "\x1b[1mAll checks passed!\x1b[0m\n", 94)
    write(run / "checks" / "test.1.log", "tests/test_cart.py \x1b[32m..\x1b[0m\n\x1b[32m\x1b[1m2 passed\x1b[0m in 0.03s\n", 100)
    agent(run, "commit", 100, 110, simple_claude(100, 110, cwd(rid), "Committed: test the cart total", 0.0300), output="Committed.")
    agent(run, "review-1", 110, 130, simple_claude(110, 130, cwd(rid), '{"success": true, "summary": "Matches the plan.", "issues": []}', 0.2000))
    agent(run, "review-2", 130, 150, simple_claude(130, 150, cwd(rid), '{"success": true, "summary": "Looks right.", "issues": [{"severity": "skippable", "description": "Name could be clearer", "resolution": "rename"}]}', 0.1800))
    write(run / "checks" / "gate-lint.log", "All checks passed!\n", 153)
    write(run / "checks" / "gate-test.log", "2 passed\n", 157)
    records += [rec("lint", "check", "passed", 90, 94, f"exit 0, log agents/{rid}/checks/lint.1.log"),
                rec("test", "check", "passed", 94, 100, f"exit 0, log agents/{rid}/checks/test.1.log"),
                rec("commit", "run", "passed", 100, 110, "Committed."), rec("review", "review", "passed", 110, 150, "ok"),
                rec("ship", "ship", "passed", 150, 160, "ok")]
    arts = layer / "artefacts" / rid
    write(arts / "log" / "test.1.log", "2 passed\n", 150)
    write(arts / "screenshots" / "e2e" / "01-cart.png", (DATA / "tiny.png").read_bytes(), 150)
    write(arts / "recording" / "e2e" / "session.webm", (DATA / "tiny.webm").read_bytes(), 150)
    write(arts / "recording" / "e2e" / "session.keyframes" / "session-01.png", (DATA / "tiny.png").read_bytes(), 150)
    artefacts = [
        {"id": "log", "type": "log", "step": "review", "path": f"artefacts/{rid}/log/test.1.log", "bytes": 9, "sha256": "0" * 64,
         "url": f"file://{LAYER_DIR}/artefacts/{rid}/log/test.1.log", "tail": "2 passed"},
        {"id": "screenshots", "type": "image", "step": "review", "path": f"artefacts/{rid}/screenshots/e2e/01-cart.png",
         "bytes": 525, "sha256": "0" * 64, "url": ""},
        {"id": "recording", "type": "video", "step": "review", "path": f"artefacts/{rid}/recording/e2e/session.webm",
         "bytes": 3235, "sha256": "0" * 64, "url": "",
         "keyframes": [{"path": f"artefacts/{rid}/recording/e2e/session.keyframes/session-01.png", "bytes": 525, "sha256": "0" * 64, "url": ""}]},
    ]
    write(arts / "manifest.json", json.dumps(artefacts, indent=2), 150)
    review = {"mode": "auto", "verdict": {"success": True, "summary": "Matches the plan. | Looks right.", "issues": [
        {"severity": "skippable", "description": "Name could be clearer", "resolution": "rename"}], "mode": "auto"},
        "rounds": [{"round": 1, "reviewers": [
            {"success": True, "summary": "Matches the plan.", "issues": []},
            {"success": True, "summary": "Looks right.", "issues": [{"severity": "skippable", "description": "Name could be clearer", "resolution": "rename"}]}],
            "verdict": {"success": True}}]}
    ship = {"action": "merge", "shipped": True, "target": "al/ship/main", "commit": "1" * 40, "pr": None, "reasons": [],
            "gate": {"action": "merge", "ship": True, "reasons": [], "diff_lines": 12, "changed_files": ["tests/test_cart.py", "tests/conftest.py"]}}
    write(run / "state.json", json.dumps(state(rid, "zte", "Add a test for the cart total with two items", "passed", 0, 160, records,
                                               checks={"lint": {"cmd": "ruff check .", "exit": 0, "log": f"agents/{rid}/checks/lint.1.log"},
                                                       "test": {"cmd": "pytest -q", "exit": 0, "log": f"agents/{rid}/checks/test.1.log"}},
                                               artefacts=artefacts, review=review, ship=ship, ports=[9100, 9101]), indent=2), 160)
    write(run / "run.log", "\n".join([
        f"[adw {rid}] zte start: class=chore dry_run=False", f"[adw {rid}] step classify (run) start",
        f"[adw {rid}] container started: al-demo-shop-{rid}", f"[adw {rid}] step classify (run) passed: ok",
        f"[adw {rid}] gate: lint exit 0", f"[adw {rid}] gate: test exit 0", f"[adw {rid}] gate: action merge",
        f"[adw {rid}] container removed: al-demo-shop-{rid}", "=== ADW summary ===", "status:    passed"]) + "\n", 160)

    # ---- a1000002: failed, handed off
    rid, run = "a1000002", agents_dir / "a1000002"
    base = 1000
    records, t = plan_records(base)
    for name, a, b in (("classify", base, base + 10), ("plan-chore", base + 10, base + 40), ("build", base + 40, base + 90)):
        agent(run, name, a, b, simple_claude(a, b, cwd(rid), f"{name} done", 0.05))
    write(run / "checks" / "lint.1.log", "All checks passed!\n", base + 93)
    write(run / "checks" / "test.1.log", "\x1b[31mFAILED\x1b[0m tests/test_pay.py::test_refund - assert 0 == 5\n1 failed\n", base + 100)
    agent(run, "test-resolve-1", base + 100, base + 130, simple_claude(base + 100, base + 130, cwd(rid), "Needs a migration; REQUIRES HUMAN.", 0.11))
    write(run / "checks" / "test.2.log", "\x1b[31mFAILED\x1b[0m tests/test_pay.py::test_refund\n1 failed\n", base + 136)
    agent(run, "test-resolve-2", base + 136, base + 160, simple_claude(base + 136, base + 160, cwd(rid), "Still blocked on the migration.", 0.09))
    write(run / "checks" / "test.3.log", "\x1b[31mFAILED\x1b[0m tests/test_pay.py::test_refund\n1 failed\n", base + 166)
    records += [rec("lint", "check", "passed", base + 90, base + 93, "exit 0"),
                rec("test", "check", "failed", base + 93, base + 166, f"test: check test exited 1 after 3 attempt(s); log agents/{rid}/checks/test.3.log")]
    write(run / "PR.md", "# [needs human] Refund support\n\n## Why this stopped\n\nThe check `test` failed after 3 attempts.\n\n"
                         "## What the agent asks a human to do\n\nAdd the migration `0007_refunds.py`.\n\n## Summary\n\n…\n", base + 170)
    write(run / "state.json", json.dumps(state(rid, "zte", "Support refunds on paid orders", "failed", base, base + 172, records,
                                               checks={"lint": {"cmd": "ruff check .", "exit": 0, "log": f"agents/{rid}/checks/lint.1.log"},
                                                       "test": {"cmd": "pytest -q", "exit": 1, "log": f"agents/{rid}/checks/test.3.log"}},
                                               ship={"action": "handoff", "shipped": False, "stopped_at": "test", "target": f"agents/{rid}/PR.md",
                                                     "reasons": [f"run-stopped:test: check test exited 1 after 3 attempt(s); log agents/{rid}/checks/test.3.log"],
                                                     "branch": f"chore/{rid}-demo", "commit": "2" * 40, "pr": None}), indent=2), base + 172)
    write(run / "run.log", f"[adw {rid}] zte start: class=chore dry_run=False\n[adw {rid}] run failed: test\n", base + 172)

    # ---- a1000003: resolve loop that passes
    rid, run = "a1000003", agents_dir / "a1000003"
    base = 2000
    records, t = plan_records(base)
    for name, a, b in (("classify", base, base + 10), ("plan-chore", base + 10, base + 40), ("build", base + 40, base + 90)):
        agent(run, name, a, b, simple_claude(a, b, cwd(rid), f"{name} done", 0.04))
    write(run / "checks" / "lint.1.log", "shop/cart.py:3:1: F401 `os` imported but unused\nFound 1 error.\n", base + 94)
    agent(run, "lint-resolve-1", base + 94, base + 110, simple_claude(base + 94, base + 110, cwd(rid), "Removed the unused import.", 0.03))
    write(run / "checks" / "lint.2.log", "All checks passed!\n", base + 113)
    write(run / "checks" / "test.1.log", "3 passed\n", base + 120)
    agent(run, "commit", base + 120, base + 128, simple_claude(base + 120, base + 128, cwd(rid), "Committed.", 0.02))
    agent(run, "review-1", base + 128, base + 140, simple_claude(base + 128, base + 140, cwd(rid), "ok", 0.10))
    agent(run, "review-2", base + 140, base + 152, simple_claude(base + 140, base + 152, cwd(rid), "ok", 0.10))
    records += [rec("lint", "check", "passed", base + 90, base + 113, "exit 0"), rec("test", "check", "passed", base + 113, base + 120, "exit 0"),
                rec("commit", "run", "passed", base + 120, base + 128), rec("review", "review", "passed", base + 128, base + 152),
                rec("ship", "ship", "passed", base + 152, base + 158)]
    write(run / "state.json", json.dumps(state(rid, "zte", "Remove unused imports in the cart module", "passed", base - 12, base + 158, records,
                                               checks={"lint": {"cmd": "ruff check .", "exit": 0, "log": f"agents/{rid}/checks/lint.2.log"},
                                                       "test": {"cmd": "pytest -q", "exit": 0, "log": f"agents/{rid}/checks/test.1.log"}},
                                               review={"mode": "auto", "verdict": {"success": True, "summary": "ok | ok", "issues": []},
                                                       "rounds": [{"round": 1, "reviewers": [{"success": True, "summary": "ok", "issues": []}] * 2, "verdict": {"success": True}}]},
                                               ship={"action": "merge", "shipped": True, "target": "al/ship/main", "reasons": []}), indent=2), base + 158)
    write(run / "run.log", f"[adw {rid}] zte start: class=chore dry_run=False\n"
                           f"[slots] adw {rid}: waiting for a free runs slot (2 in use)\n"
                           f"[adw {rid}] step classify (run) start\n", base + 158)

    # ---- a1000004: two review rounds
    rid, run = "a1000004", agents_dir / "a1000004"
    base = 3000
    records, t = plan_records(base)
    for name, a, b in (("classify", base, base + 10), ("plan-chore", base + 10, base + 40), ("build", base + 40, base + 90)):
        agent(run, name, a, b, simple_claude(a, b, cwd(rid), f"{name} done", 0.05))
    write(run / "checks" / "lint.1.log", "All checks passed!\n", base + 93)
    write(run / "checks" / "test.1.log", "4 passed\n", base + 100)
    agent(run, "commit", base + 100, base + 108, simple_claude(base + 100, base + 108, cwd(rid), "Committed.", 0.02))
    blocker = {"severity": "blocker", "description": "Expired coupons are still accepted", "resolution": "check the expiry date"}
    agent(run, "review-1", base + 108, base + 130, simple_claude(base + 108, base + 130, cwd(rid), "blocker found", 0.21))
    agent(run, "review-2", base + 130, base + 150, simple_claude(base + 130, base + 150, cwd(rid), "approve", 0.19))
    agent(run, "patch-1", base + 150, base + 170, simple_claude(base + 150, base + 170, cwd(rid), f"{LAYER_DIR}/specs/patch-1.md", 0.12))
    agent(run, "implement-1", base + 170, base + 200, simple_claude(base + 170, base + 200, cwd(rid), "Expiry is checked now.", 0.15))
    write(run / "checks" / "lint.2.log", "All checks passed!\n", base + 203)
    write(run / "checks" / "test.2.log", "5 passed\n", base + 210)
    agent(run, "review-2-1", base + 210, base + 230, simple_claude(base + 210, base + 230, cwd(rid), "approve", 0.2))
    agent(run, "review-2-2", base + 230, base + 250, simple_claude(base + 230, base + 250, cwd(rid), "approve", 0.2))
    records += [rec("lint", "check", "passed", base + 90, base + 93), rec("test", "check", "passed", base + 93, base + 100),
                rec("commit", "run", "passed", base + 100, base + 108), rec("review", "review", "passed", base + 108, base + 250),
                rec("ship", "ship", "passed", base + 250, base + 260)]
    rounds = [{"round": 1, "reviewers": [{"success": False, "summary": "Coupon expiry is not enforced.", "issues": [blocker,
                {"severity": "tech_debt", "description": "Duplicate date parsing", "resolution": "share a helper"}]},
               {"success": True, "summary": "Looks fine.", "issues": []}],
               "verdict": {"success": False}, "patch": {"patch_plan": f"{LAYER_DIR}/specs/patch-1.md",
                                                        "implement_output": "Expiry is checked now.", "checks_rerun": ["lint", "test"]}},
              {"round": 2, "reviewers": [{"success": True, "summary": "Fixed.", "issues": []},
                                         {"success": True, "summary": "Fixed and tested.", "issues": []}], "verdict": {"success": True}}]
    write(run / "state.json", json.dumps(state(rid, "zte", "Reject expired coupons at checkout", "passed", base, base + 260, records,
                                               klass="bug",
                                               checks={"lint": {"cmd": "ruff check .", "exit": 0, "log": f"agents/{rid}/checks/lint.2.log"},
                                                       "test": {"cmd": "pytest -q", "exit": 0, "log": f"agents/{rid}/checks/test.2.log"}},
                                               review={"mode": "auto", "verdict": {"success": True, "summary": "Fixed. | Fixed and tested.", "issues": []},
                                                       "rounds": rounds},
                                               ship={"action": "merge", "shipped": True, "target": "main via PR #7",
                                                     "pr": {"number": 7, "url": "http://forge.invalid/demo/shop/pulls/7"},
                                                     "url": "http://forge.invalid/demo/shop/pulls/7", "reasons": []}), indent=2), base + 260)
    write(run / "run.log", f"[adw {rid}] zte start: class=bug dry_run=False\n", base + 260)

    # ---- a1000005: fan-out with candidates c1000001..3
    rid, run = "a1000005", agents_dir / "a1000005"
    base = 4000
    candidates = []
    for index, (cid, status, seconds) in enumerate((("c1000001", "passed", 80), ("c1000002", "failed", 60), ("c1000003", "passed", 95)), start=1):
        crun = agents_dir / cid
        start = base + 1 + (index - 1) * 5
        crecords = [rec("plan", "run", "passed", start, start + 20), rec("build", "run", "passed", start + 20, start + 50),
                    rec("test", "check", status, start + 50, start + seconds), rec("commit", "run", "passed" if status == "passed" else "pending", start + seconds, start + seconds + 5) if status == "passed" else rec("commit", "run", "pending", None, None)]
        agent(crun, "plan", start, start + 20, simple_claude(start, start + 20, cwd(cid), "plan", 0.06))
        agent(crun, "build", start + 20, start + 50, simple_claude(start + 20, start + 50, cwd(cid), "build", 0.08))
        write(crun / "checks" / "test.1.log", "ok\n" if status == "passed" else "1 failed\n", start + seconds)
        write(crun / "state.json", json.dumps(state(cid, "candidate", "Speed up the product list", status, start, start + seconds + 5, crecords,
                                                    checks={"test": {"cmd": "pytest -q", "exit": 0 if status == "passed" else 1,
                                                                     "log": f"agents/{cid}/checks/test.1.log"}}), indent=2), start + seconds + 5)
        write(crun / "run.log", f"[adw {cid}] candidate start: class=chore dry_run=False\n", start + seconds + 5)
        candidates.append({"id": cid, "index": index, "status": status, "branch": f"chore/{cid}-demo", "head": "", "diff_lines": 20 + index,
                           "resolve_attempts": 0, "exit": 0 if status == "passed" else 1, "seconds": seconds, "completed": index,
                           "log": f"agents/{rid}/fanout/race/{cid}.log", **({"reason": "step test failed"} if status == "failed" else {})})
        write(run / "fanout" / "race" / f"{cid}.log", f"candidate {cid}\n", start + seconds)
    write(run / "checks" / "test.1.log", "ok\n", base + 120)
    records = [rec("race", "fan-out", "passed", base, base + 110, "picked c1000001"), rec("test", "check", "passed", base + 110, base + 120),
               rec("review", "review", "passed", base + 120, base + 121), rec("ship", "ship", "passed", base + 121, base + 125)]
    write(run / "state.json", json.dumps(state(rid, "race", "Speed up the product list", "passed", base, base + 125, records,
                                               checks={"test": {"cmd": "pytest -q", "exit": 0, "log": f"agents/{rid}/checks/test.1.log"}},
                                               fanout={"race": {"candidates": candidates, "picked": ["c1000001"], "pick": "best", "adw": "candidate"}},
                                               review={"mode": "human", "verdict": {"success": True, "summary": "awaiting human review", "issues": [], "mode": "human"}, "rounds": []},
                                               ship={"action": "pr", "shipped": False, "target": "PR.md", "reasons": ["review-human: a human merges"]}), indent=2), base + 125)
    write(run / "run.log", f"[adw {rid}] race start: class=chore dry_run=False\n", base + 125)

    # ---- a1000006: in progress (build running); holds the runs and agents slots and a port lease
    rid, run = "a1000006", agents_dir / "a1000006"
    base = 5000
    records, _t = plan_records(base)
    records[-1] = rec("build", "run", "running", base + 40, None)
    agent(run, "classify", base, base + 10, simple_claude(base, base + 10, cwd(rid), '{"class":"chore"}', 0.04), output='{"class":"chore"}')
    agent(run, "plan-chore", base + 10, base + 40, simple_claude(base + 10, base + 40, cwd(rid), "plan", 0.12))
    live = Claude(base + 40, cwd(rid))
    live.thinking()
    live.tool("Read", {"file_path": f"{cwd(rid)}/shop/catalog.py"}, "1\tclass Catalog: ...")
    body = live.body()
    agent(run, "build", base + 40, None, body + PARTIAL_EDIT[:PARTIAL_SPLIT], output=None)
    write(run / "state.json", json.dumps(state(rid, "zte", "Cache the catalog for 60 seconds", "running", base, None, records,
                                               ports=[9110, 9111]), indent=2), base + 41)
    write(run / "run.log", f"[adw {rid}] zte start: class=chore dry_run=False\n[adw {rid}] container started: al-demo-shop-{rid}\n"
                           f"[adw {rid}] step build (run) start\n", base + 41)
    write(agents_dir / ".slots" / "runs.0.lock", f"4242 adw {rid}\n")
    write(agents_dir / ".slots" / "agents.0.lock", f"4242 adw {rid} step build\n")
    write(agents_dir / ".ports.json", json.dumps({rid: {"ports": [9110, 9111], "pid": 0}}))

    # ---- a1000007: hostile content
    rid, run = "a1000007", agents_dir / "a1000007"
    base = 6000
    hostile = '<script>window.__pwned=1</script><img src=x onerror="window.__pwned=2">'
    records, _t = plan_records(base)
    agent(run, "classify", base, base + 10, simple_claude(base, base + 10, cwd(rid), f"classify {hostile}", 0.01))
    agent(run, "plan-chore", base + 10, base + 40, simple_claude(base + 10, base + 40, cwd(rid), "plan", 0.01),
          prompt=f"# Plan\n\n{hostile}\n\n<b>bold?</b>\n")
    evil = Claude(base + 40, cwd(rid))
    evil.text(f"Here is HTML: {hostile} and <a href=\"javascript:window.__pwned=3\">a link</a>")
    evil.tool("Bash", {"command": f"echo '{hostile}'"}, f"{hostile}\n<style>body{{display:none}}</style>")
    evil.tool("Edit", {"file_path": f"{cwd(rid)}/templates/<img src=x onerror=alert(1)>.html", "old_string": "<p>old</p>",
                       "new_string": hostile}, "ok")
    evil.final(f"done {hostile}", 0.02, 3, 5000)
    agent(run, "build", base + 40, base + 90, evil.body(), output=hostile)
    write(run / "checks" / "lint.1.log", f"\x1b[31m{hostile}\x1b[0m\n", base + 95)
    records += [rec("lint", "check", "failed", base + 90, base + 95, f"lint failed {hostile}")]
    write(run / "state.json", json.dumps(state(rid, "zte", f"Render {hostile} safely", "failed", base, base + 96, records,
                                               checks={"lint": {"cmd": "ruff check .", "exit": 1, "log": f"agents/{rid}/checks/lint.1.log"}},
                                               review={"mode": "auto", "verdict": {"success": False, "summary": hostile, "issues": []},
                                                       "rounds": [{"round": 1, "reviewers": [{"success": False, "summary": hostile, "issues": [
                                                           {"severity": "blocker", "description": hostile, "resolution": hostile}]}], "verdict": {"success": False}}]}), indent=2), base + 96)
    write(run / "run.log", f"[adw {rid}] zte start: class=chore dry_run=False\n{hostile}\n", base + 96)

    # ---- a1000008: corrupt and truncated JSON lines
    rid, run = "a1000008", agents_dir / "a1000008"
    base = 7000
    broken = Claude(base, cwd(rid))
    broken.text("Starting.")
    lines = broken.body().splitlines()
    lines.insert(2, '{"type": "assistant", "message": {"content": [')
    lines.insert(3, "not json at all, a stray diagnostic line")
    lines.append('{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":30,"total_cost_usd":0.5,"usage":{"input_tokens":1,"output_tokens":2}}')
    agent(run, "classify", base, base + 20, "\n".join(lines) + "\n{\"type\": \"user\", \"mess", output=None)
    write(run / "state.json", json.dumps(state(rid, "zte", "Tidy the README", "failed", base, base + 21,
                                               [rec("classify", "run", "failed", base, base + 20, "classify: claude-code reported is_error")]), indent=2), base + 21)
    write(run / "run.log", f"[adw {rid}] zte start: class=chore dry_run=False\n", base + 21)

    # ---- a1000009: no state.json
    rid, run = "a1000009", agents_dir / "a1000009"
    write(run / "run.log", f"[adw {rid}] zte start: class=bug dry_run=True\n[adw {rid}] step classify (run) start\n", 8000)
    write(run / "classify" / "prompt.md", "# classify\n", 8000)

    # ---- a100000a: plain-text agent
    rid, run = "a100000a", agents_dir / "a100000a"
    base = 9000
    agent(run, "answer", base, base + 30, "Reading shop/cart.py\nThe cart total is computed in Cart.total() by summing item prices.\n"
                                           "\x1b[2mtokens used: 1234\x1b[0m\n", output="The cart total is computed in Cart.total().")
    write(run / "state.json", json.dumps(state(rid, "ask", "Where is the cart total computed?", "passed", base, base + 31,
                                               [rec("answer", "run", "passed", base, base + 30, "The cart total is computed in Cart.total().")]), indent=2), base + 31)
    write(run / "run.log", f"[adw {rid}] ask start: class=chore dry_run=False\n", base + 31)

    # ---- a100000b: dry run on the mock agent
    rid, run = "a100000b", agents_dir / "a100000b"
    base = 9500
    agent(run, "classify", base, base + 1, mock_raw('{"class": "chore", "adw": "", "model_set": "base", "slug": "mock-task"}'))
    write(run / "state.json", json.dumps(state(rid, "zte", "Dry run", "failed", base, base + 2,
                                               [rec("classify", "run", "passed", base, base + 1), rec("plan-chore", "run", "failed", base + 1, base + 2, "mock failure")],
                                               dry_run=True), indent=2), base + 2)
    write(run / "run.log", f"[adw {rid}] zte start: class=chore dry_run=True\n", base + 2)

    # ---- escapes for the safety tests
    write(root / "outside-secret.txt", "SECRET outside every layer\n")
    escape = agents_dir / "a1000001" / "escape.txt"
    if not escape.exists():
        os.symlink(os.path.relpath(root / "outside-secret.txt", escape.parent), escape)
    link_dir = layer / "artefacts" / "a1000001" / "linked"
    if not link_dir.exists():
        os.symlink(os.path.relpath(root, link_dir.parent), link_dir)

    # ---- a second, in-repo style layer and a pruned one, for --scan
    repo = root / "repo"
    other = dict(IR, name="other-layer", adws=[IR["adws"][3]])
    write(repo / "layers" / "other" / LAYER_FILE_NAME, json.dumps(other))
    write(repo / "node_modules" / "pkg" / LAYER_FILE_NAME, json.dumps(dict(IR, name="pruned")))
    return {"layer": layer, "agents": agents_dir, "repo": repo, "secret": root / "outside-secret.txt"}


LAYER_FILE_NAME = "agentic-layer.json"
# The running run's transcript ends in the first PARTIAL_SPLIT characters of this event; tests append the rest.
PARTIAL_EDIT = json.dumps({"type": "assistant", "message": {"id": "msg_9999", "content": [
    {"type": "tool_use", "id": "toolu_9", "name": "Edit",
     "input": {"file_path": f"{LAYER_DIR}/trees/a1000006/shop/catalog.py", "old_string": "a", "new_string": "b"}}]}})
PARTIAL_SPLIT = 60


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    paths = build(Path(sys.argv[1]))
    print(json.dumps({k: str(v) for k, v in paths.items()}, indent=2))
