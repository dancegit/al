#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""ADW `reviewed` — Everything sdlc does, then two independent reviewer agents must both approve before a human gets the branch and PR.md; never merges

Steps: classify → plan-chore → plan-bug → plan-feature → build → test-compiler → test-runtime → test-tools → test-dashboard → test-integration → document → commit → review → ship
PITER: input=['cli', 'tasklist'] trigger=['manual', 'cron'] environment=['worktree'] review=two-reviewers
Generated from agentic.rkt — edit that file and run `al gen`, not this one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adw_modules.runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(adw="reviewed", layer_dir=Path(__file__).resolve().parent.parent))
