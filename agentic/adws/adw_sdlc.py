#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""ADW `sdlc` — Plan, build, run the repository's test suites, document, commit; a human reviews the branch and PR.md

Steps: classify → plan-chore → plan-bug → plan-feature → build → test-compiler → test-runtime → test-tools → test-dashboard → test-integration → document → commit → review → ship
PITER: input=['cli', 'tasklist'] trigger=['manual', 'cron'] environment=['worktree'] review=human
Generated from agentic.rkt — edit that file and run `al gen`, not this one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adw_modules.runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(adw="sdlc", layer_dir=Path(__file__).resolve().parent.parent))
