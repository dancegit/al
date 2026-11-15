"""Render the planted defect (scenario C) into a mock-agent script, validated against the pristine app.

    python render.py APP_DIR PATCH OUT_JSON KIT_RUNTIME_DIR

planted.patch is a unified diff against tests/rotademo/app. This applies it to a copy of APP_DIR
(`git apply`, which refuses a patch that no longer fits, so the planted change cannot silently drift
from the app), and writes OUT_JSON in the mock agent's format (kit/runtime/adw_modules/agents.py):
{"output": ..., "files": {path: full new content}, "commit": true}. The mock agent writes the files
into the run's worktree and commits them. Files are passed through the runtime's own template
renderer to prove that no `$1` / `${name}` in them would be substituted on the way.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The step's report. Deliberately plain: the reviewers run in a container that can see the run's own
# records, and a giveaway here would spoil the experiment. It is still literally true. Everything a
# HUMAN reads (script output, README, docs/demo/rotademo/) labels this change as a planted defect.
OUTPUT = ("- Workers page: an Expired badge next to each worker whose registration has expired (app.py, "
          "templates/workers.html, static/style.css).\n"
          "- tests/test_journeys.py: test_workers_page_marks_expired_registrations.\n"
          "- e2e/scenario.py: a step and screenshot of the badge on the workers page.")


def main() -> int:
    app, patch, out, runtime = (Path(arg).resolve() for arg in sys.argv[1:5])
    sys.path.insert(0, str(runtime))
    from adw_modules.templates import render  # the renderer the mock agent uses

    paths = re.findall(r"^\+\+\+ b/(\S+)$", patch.read_text(encoding="utf-8"), re.MULTILINE)
    if not paths:
        print("render: the patch names no files", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as directory:
        tree = Path(directory) / "app"
        shutil.copytree(app, tree, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "e2e-results", "*.db"))
        result = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=tree,
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"render: planted.patch no longer applies to the app:\n{result.stderr}", file=sys.stderr)
            return 1
        files = {path: (tree / path).read_text(encoding="utf-8") for path in paths}
    for path, content in files.items():
        if render(content, {"adw_id": "x", "plan": "x", "prompt": "x", "class": "x"}, ["ARG"]) != content:
            print(f"render: {path} contains template tokens the mock agent would substitute", file=sys.stderr)
            return 1
    out.write_text(json.dumps({"output": OUTPUT, "files": files, "commit": True}, indent=1) + "\n", encoding="utf-8")
    print(f"render: {len(files)} file(s): {', '.join(paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
