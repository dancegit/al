#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""ADW `plan-build` — Classify the request, plan it by class, implement the plan; nothing else

Steps: classify → plan-chore → plan-bug → plan-feature → build
PITER: input=['cli', 'tasklist'] trigger=['manual', 'cron'] environment=['worktree'] review=human
Generated from agentic.rkt — edit that file and run `al gen`, not this one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adw_modules.runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(adw="plan-build", layer_dir=Path(__file__).resolve().parent.parent))
