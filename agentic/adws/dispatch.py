#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pick the ADW for a prompt input and run it.

  dispatch.py [PROMPT] [--source KIND:REF] [--adw NAME] [--class C] [--dry-run]

Used by CI workflows and triggers that do not know which ADW applies. The (PEP 723) script
header is prepended by `al gen`.
"""
import argparse
import subprocess
import sys
from pathlib import Path

LAYER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--source")
    ap.add_argument("--adw")
    ap.add_argument("--class", dest="klass")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    from adw_modules import inputs, ir

    layer = ir.load(LAYER)
    name = a.adw
    if not name:
        if a.source:
            request = inputs.resolve(layer, a.source, a.prompt)
            name = inputs.pick_adw(layer, request)
        else:
            name = layer.ir["adws"][0]["name"]
    script = LAYER / "adws" / f"adw_{name}.py"
    if not script.exists():
        print(f"dispatch: no ADW named {name}", file=sys.stderr)
        return 2
    cmd = ["uv", "run", str(script)]
    if a.prompt:
        cmd.append(a.prompt)
    if a.source:
        cmd += ["--source", a.source]
    if a.klass:
        cmd += ["--class", a.klass]
    if a.dry_run:
        cmd.append("--dry-run")
    print(f"dispatch: {name} <- {a.source or 'cli'}", flush=True)
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
