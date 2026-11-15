#!/usr/bin/env python3
"""Print a short, human-readable summary of a saved review verdict file (used by the demo scenes).

  show_verdicts.py FILE          reviewers' decisions, blockers (first lines), and the gate decision if present
"""
import json
import sys
import textwrap

data = json.load(open(sys.argv[1]))
reviewers = data.get("reviewers") or []
for i, r in enumerate(reviewers, 1):
    blockers = [x for x in r.get("issues", []) if x.get("severity") == "blocker"]
    print(f"reviewer {i}: {'APPROVED' if r.get('success') else 'REJECTED'}   blockers: {len(blockers)}   "
          f"artefacts examined: {len(r.get('artefacts_reviewed', []))}")
    for b in blockers[:2]:
        text = " ".join(str(b.get("description", "")).split())
        for line in textwrap.wrap(text, 100)[:3]:
            print("   | " + line)
gate = data.get("gate")
if gate:
    reasons = gate.get("reasons") or []
    print(f"gate: {gate.get('action')}   changed lines: {gate.get('diff_lines')}   "
          f"reasons against: {', '.join(reasons) if reasons else 'none'}")
if data.get("target"):
    print("shipped to:", data["target"])
