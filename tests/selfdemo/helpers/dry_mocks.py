"""Scripted mock responses for a free rehearsal of selfdemo_live.sh (AL_SELFDEMO_DRY=1).

    python dry_mocks.py LAYER_DIR KIT_DIR OUT_DIR

Writes OUT_DIR/1/{plan,implement}.json (the DSL change: a task file and a poller for ui-change) and
OUT_DIR/2/{plan,implement}.json (the template refinement: an ejected plan-chore with a Rollback
section). They do what a good agent would do, so the rest of the script can be checked for free.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def plan(title: str, change: str) -> dict:
    text = (f"# Layer change: {title}\n\n## Request\n{title}\n\n## Summary\n{title} (rehearsal).\n\n"
            f"## What changes for you\n{change}\n\n## How\nScripted.\n\n## Step by Step Tasks\n### Change\n- scripted\n\n"
            "## Validation Commands\n- definition\n\n## Notes\nnone\n")
    return {"layer_files": {"specs/layer-change.md": text}, "output": "specs/layer-change.md"}


def main() -> int:
    layer, kit, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    source = (layer / "agentic.rkt").read_text()
    changed = source.replace("  (prompt-input issue #:labels () #:classify #f)\n  (trigger manual)\n",
                             "  (prompt-input issue #:labels () #:classify #f)\n  (prompt-input tasklist)\n"
                             "  (trigger manual)\n  (trigger cron #:interval-seconds 20)\n")
    changed = changed.replace('#:input (issue) #:trigger (manual) #:environment (worktree podman) #:review human',
                              '#:input (issue tasklist) #:trigger (manual cron) #:environment (worktree podman) #:review human')
    assert changed != source
    (out / "1").mkdir(parents=True, exist_ok=True)
    (out / "1" / "plan.json").write_text(json.dumps(plan("queue work in a text file", "- Write tasks in `agentic/tasks.md`.")))
    (out / "1" / "implement.json").write_text(json.dumps({"files": {"agentic/agentic.rkt": changed}}))
    template = (kit / "templates/commands/plan-chore.md").read_text()
    edited = template.replace("## Notes\n<tests added", "## Rollback\n<exactly how to undo the change>\n\n## Notes\n<tests added")
    assert edited != template
    sha = hashlib.sha256((kit / "templates/commands/plan-chore.md").read_bytes()).hexdigest()
    (out / "2").mkdir(parents=True, exist_ok=True)
    (out / "2" / "plan.json").write_text(json.dumps(plan("plans end with Rollback", "- Every plan ends with a Rollback section.")))
    (out / "2" / "implement.json").write_text(json.dumps({"raw_files": {
        "agentic/templates/commands/plan-chore.md": edited,
        "agentic/templates/.al-eject.json": json.dumps({"commands/plan-chore.md": sha})}}))
    print(f"dry_mocks: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
