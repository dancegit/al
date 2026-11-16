"""Pick the ADW for a prompt input and run it; or start work the way the dashboard does.

  dispatch.py [PROMPT] [--source KIND:REF] [--adw NAME] [--class C] [--dry-run]
  dispatch.py --start (--adw NAME | --command NAME) --request-key KEY [--class C] [--model-set T]
              [--model T=ID …] [--issue none|start|trigger] [--confirm-duplicate] [--confirm-ships]
              [--dry-run] [--json]   < prompt
  dispatch.py --command NAME [runner flags: --prompt-file F --adw-id ID --class C --dry-run …]

The first form is used by CI workflows and triggers that do not know which ADW applies. --start is
the layer's one start path (adw_modules/launch.py): it reads the prompt from stdin, starts the run
detached and prints the result (with --json: one JSON line); exit 0 started or issue created,
2 invalid, 3 refused (day limit or usage threshold), 4 needs a confirmation, 1 failed.
--command alone runs one command or hop in the foreground as a recorded run (what a single-command
start executes). The (PEP 723) script header is prepended by `al gen`.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

LAYER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
MAX_PROMPT = 64 * 1024


def start(a: argparse.Namespace) -> int:
    from adw_modules import ir, launch
    from adw_modules.errors import LayerError

    def done(result: dict) -> int:
        if a.json:
            print(json.dumps(result), flush=True)
        else:
            text = result.get("message") or result.get("error") or ""
            if result["status"] == "started":
                text = f"run {result['run_id']} started" + (f"; {result['queued']}" if result.get("queued") else "")
            print(f"dispatch: {result['status']}" + (f": {text}" if text else ""), flush=True)
        return launch.EXIT.get(result["status"], 1)

    if bool(a.adw) == bool(a.command):
        return done({"status": "invalid", "error": "give one of --adw NAME or --command NAME"})
    data = sys.stdin.buffer.read(MAX_PROMPT + 1)
    if len(data) > MAX_PROMPT:
        return done({"status": "invalid", "error": f"the prompt is over {MAX_PROMPT} bytes"})
    try:
        prompt = data.decode("utf-8")
        layer = ir.load(LAYER)
    except UnicodeDecodeError:
        return done({"status": "invalid", "error": "the prompt is not UTF-8"})
    except LayerError as exc:
        return done({"status": "invalid", "error": str(exc)})
    target = launch.Target("workflow", a.adw) if a.adw else launch.Target("command", a.command)
    return done(launch.start(layer, target, prompt, request_key=a.request_key or "", klass=a.klass,
                             model_set=a.model_set, models=a.model, issue=a.issue,
                             confirm_duplicate=a.confirm_duplicate, confirm_ships=a.confirm_ships,
                             dry_run=a.dry_run))


def main() -> int:
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--command")
    pre.add_argument("--start", action="store_true")
    known, rest = pre.parse_known_args()
    if known.command and not known.start:   # one command in the foreground; every other flag is the runner's
        from adw_modules import launch

        return launch.run_command(LAYER, known.command, rest)

    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--source")
    ap.add_argument("--adw")
    ap.add_argument("--class", dest="klass")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--start", action="store_true", help="start --adw or --command with the prompt on stdin")
    ap.add_argument("--command", help="a command or hop of the layer: one recorded run of it")
    ap.add_argument("--request-key", help="with --start: the same key never starts a second run")
    ap.add_argument("--model-set", metavar="TIER")
    ap.add_argument("--model", action="append", default=[], metavar="TIER=MODEL_ID")
    ap.add_argument("--issue", choices=("none", "start", "trigger"), default="none",
                    help="with --start: also log the request as a forge issue")
    ap.add_argument("--confirm-duplicate", action="store_true")
    ap.add_argument("--confirm-ships", action="store_true")
    ap.add_argument("--json", action="store_true", help="with --start: print the result as one JSON line")
    a = ap.parse_args()
    if a.start:
        return start(a)

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
