#!/usr/bin/env python3
"""A scripted stand-in for a coding agent, used by the dashboard's live demo: no LLM, no cost.

It is an `(agent custom …)`: it reads the rendered prompt on stdin, works in the run's worktree
(argv[1]) and writes Claude Code stream-json events to stdout, one line at a time with pauses,
so the dashboard has a transcript that grows while the step runs. The edits and the test run
it reports are real: it changes cart.py, adds a test and runs the test suite.

If the plan mentions "rounding" it makes a deliberate mistake, so the demo's test check fails
once and the (mock) resolve agent fixes it.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PAUSE = float(sys.argv[2]) if len(sys.argv) > 2 else 0.9
cwd = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
prompt = sys.stdin.read()
session = "replay-0000"
counter = {"tool": 0, "msg": 0}


def emit(event: dict, pause: float = PAUSE) -> None:
    if event.get("type") in {"assistant", "user"}:
        event["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    print(json.dumps(event), flush=True)
    time.sleep(pause)


def say(block: dict) -> None:
    counter["msg"] += 1
    emit({"type": "assistant", "message": {"id": f"msg_{counter['msg']}", "model": "replay-agent (no LLM)", "role": "assistant",
          "content": [block], "usage": {"input_tokens": 0, "output_tokens": 0}}, "session_id": session})


def tool(name: str, data: dict, run) -> None:
    counter["tool"] += 1
    tid = f"toolu_{counter['tool']:03d}"
    say({"type": "tool_use", "id": tid, "name": name, "input": data})
    result, error, meta = run()
    emit({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": error}]},
          "tool_use_result": meta, "session_id": session}, pause=PAUSE * 0.5)


def read(path: Path):
    def run():
        text = path.read_text() if path.is_file() else ""
        return "\n".join(f"{i}\t{line}" for i, line in enumerate(text.splitlines(), 1)), False, {"type": "text"}
    return run


started = time.time()
emit({"type": "system", "subtype": "init", "cwd": str(cwd), "session_id": session, "model": "replay-agent (no LLM)",
      "permissionMode": "acceptEdits", "tools": ["Read", "Edit", "Write", "Bash", "Grep"]}, pause=0.3)
emit({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 40, "session_id": session}, pause=0.2)
say({"type": "thinking", "thinking": ""})
say({"type": "text", "text": "I'll read the plan and the cart module, then make the change and run the tests."})

plan_match = re.search(r"(/\S+/specs/\S+\.md)", prompt)
plan = Path(plan_match.group(1)) if plan_match else None
plan_text = plan.read_text() if plan and plan.is_file() else prompt
if plan:
    tool("Read", {"file_path": str(plan)}, read(plan))
tool("Read", {"file_path": str(cwd / "cart.py")}, read(cwd / "cart.py"))
tool("Grep", {"pattern": "def total", "path": str(cwd)},
     lambda: ("cart.py:14:    def total(self) -> int:", False, {"mode": "content", "numLines": 1}))
say({"type": "thinking", "thinking": ""})

lower = plan_text.lower()
if "shipping" in lower:
    old = "    def total(self) -> int:\n"
    new = ("    def total_with_shipping(self) -> int:\n        \"\"\"Total plus 4.95 shipping below a 50.00 subtotal.\"\"\"\n"
           "        subtotal = self.total()\n        return subtotal if subtotal >= 5000 or not self.items else subtotal + 495\n\n"
           "    def total(self) -> int:\n")
    test_name, test_body = "test_shipping.py", ("from cart import Cart, Item\n\n\ndef test_free_shipping_from_50():\n"
                                               "    cart = Cart()\n    cart.add(Item('box', 5000))\n    assert cart.total_with_shipping() == 5000\n\n\n"
                                               "def test_shipping_below_50():\n    cart = Cart()\n    cart.add(Item('pen', 100))\n    assert cart.total_with_shipping() == 595\n")
elif "rounding" in lower:
    old = "        return sum(item.price for item in self.items)\n"
    new = "        return round(sum(item.price for item in self.items) / 100) * 100 + 1  # cents\n"   # deliberate mistake
    test_name, test_body = "test_rounding.py", ("from cart import Cart, Item\n\n\ndef test_total_is_whole_cents():\n"
                                               "    cart = Cart()\n    cart.add(Item('pen', 199))\n    assert cart.total() == 199\n")
else:
    old = "    def total(self) -> int:\n"
    new = ("    def discounted(self, percent: int) -> int:\n        \"\"\"Total after a percentage discount, in cents.\"\"\"\n"
           "        return self.total() * (100 - percent) // 100\n\n    def total(self) -> int:\n")
    test_name, test_body = "test_discount.py", ("from cart import Cart, Item\n\n\ndef test_ten_percent_off():\n"
                                               "    cart = Cart()\n    cart.add(Item('mug', 1000))\n    assert cart.discounted(10) == 900\n")


def edit():
    path = cwd / "cart.py"
    text = path.read_text()
    path.write_text(text.replace(old, new, 1))
    return f"The file {path} has been updated successfully.", False, {"filePath": str(path)}


tool("Edit", {"file_path": str(cwd / "cart.py"), "old_string": old, "new_string": new}, edit)


def write():
    path = cwd / "tests" / test_name
    path.write_text(test_body)
    return f"File created successfully at: {path}", False, {"filePath": str(path)}


tool("Write", {"file_path": str(cwd / "tests" / test_name), "content": test_body}, write)


def run_tests():
    done = subprocess.run([sys.executable, "tools/run_tests.py", "--fast", "--no-color"], cwd=cwd,
                          capture_output=True, text=True, timeout=120)
    output = (done.stdout + done.stderr).strip()
    return output, done.returncode != 0, {"stdout": done.stdout, "stderr": done.stderr, "interrupted": False}


tool("Bash", {"command": "python3 tools/run_tests.py --fast", "description": "Run the unit tests"}, run_tests)
say({"type": "text", "text": f"Changed cart.py and added tests/{test_name}."})
emit({"type": "result", "subtype": "success", "is_error": False, "result": f"Changed cart.py and added tests/{test_name}.",
      "num_turns": counter["tool"] + 2, "duration_ms": int((time.time() - started) * 1000), "total_cost_usd": 0.0,
      "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
      "session_id": session}, pause=0)
