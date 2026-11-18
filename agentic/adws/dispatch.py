#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pick the ADW for a prompt input and run it; or start work the way the dashboard does.

  dispatch.py [PROMPT] [--source KIND:REF] [--adw NAME] [--class C] [--dry-run]
  dispatch.py --start (--adw NAME | --command NAME) --request-key KEY [--class C] [--model-set T]
              [--model T=ID …] [--issue none|start|trigger] [--confirm-duplicate] [--confirm-ships]
              [--dry-run] [--replaces OLD] [--decider off|suggest|auto] [--json]   < prompt
  dispatch.py --pr-state N [--json]
  dispatch.py --command NAME [runner flags: --prompt-file F --adw-id ID --class C --dry-run …]
  dispatch.py --resume ID [--answer] [--json]   [< answer]
  dispatch.py --stop ID [--json]
  dispatch.py --helper [--json]   < prompt
  dispatch.py --registry [--json]

The first form is used by CI workflows and triggers that do not know which ADW applies. --start is
the layer's one start path (adw_modules/launch.py): it reads the prompt from stdin, starts the run
detached and prints the result (with --json: one JSON line); exit 0 started or issue created,
2 invalid, 3 refused (day limit or usage threshold), 4 needs a confirmation, 1 failed.
--replaces OLD names the run the start begins again ("Start again"); --decider MODE is the stand-in's
mode for that run (the run's own --decider; without it the layer's mode applies). --command alone runs one
command or hop in the foreground as a recorded run (what a single-command start executes).
--registry prints what the layer can run (adw_modules/registry.py: its workflows, the kit's
workflows, its commands), read only; with --json the whole registry as one JSON line.
--pr-state N asks the layer's forge, read only, whether pull request N is merged and prints one JSON
line {"state": open|merged|closed|unknown, "commit", "at"}; exit 0, or 1 with {"state": "unknown",
"error"} when the forge fails (the dashboard's landed check uses it, so credentials stay here).

--resume continues a waiting, failed or handed-over run detached (with --answer, a person's answer to
a waiting run is read from stdin, at most 8 KiB, and given to its script steps as ADW_ANSWER_FILE);
--stop ends a run running on this host (SIGTERM; the run ends failed, "stopped by a person"). Both
print the result (with --json: one JSON line {"status": resumed|stopping|invalid|refused|error, …});
exit 0 done, 2 invalid, 3 refused (the run is running), 1 failed.
--helper answers the dashboard's voice helper: the prompt (at most 64 KiB) on stdin, one answer of the
layer's helper agent with no tools (adw_modules/helper.py); prints one JSON line {"status": "ok",
"reply", "cost", "model"} or {"status": unavailable|failed|invalid, "error"}; exit 0 ok, 2 invalid,
1 otherwise. The (PEP 723) script header is prepended by `al gen`.
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
                             dry_run=a.dry_run, replaces=a.replaces or "", decider=a.decider or ""))


def pr_state(a: argparse.Namespace) -> int:
    from adw_modules import forge, ir

    try:
        result, code = dict(forge.get_client(ir.load(LAYER)).pr_state(a.pr_state)), 0
    except Exception as exc:  # the forge's own failure (ForgeError, HTTP, a missing gh): the answer is unknown
        result, code = {"state": "unknown", "error": f"{type(exc).__name__}: {exc}"[:500]}, 1
    if a.json:
        print(json.dumps(result), flush=True)
    else:
        print(f"dispatch: pull request #{a.pr_state} {result.get('state')}"
              + (f": {result['error']}" if result.get("error") else ""), flush=True)
    return code


def _emit(a: argparse.Namespace, result: dict, text: str = "") -> None:
    if a.json:
        print(json.dumps(result), flush=True)
    else:
        text = text or result.get("message") or result.get("error") or ""
        print(f"dispatch: {result['status']}" + (f": {text}" if text else ""), flush=True)


def _read_stdin(limit: int, what: str) -> tuple[str | None, str]:
    """(text, "") or (None, why not): at most `limit` bytes of UTF-8 from stdin."""
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        return None, f"the {what} is over {limit} bytes"
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, f"the {what} is not UTF-8"


def run_control(a: argparse.Namespace) -> int:
    """--resume ID [--answer] / --stop ID."""
    from adw_modules import ir, launch
    from adw_modules.errors import LayerError

    def done(result: dict) -> int:
        text = ""
        if result["status"] == "resumed":
            text = f"run {result['run_id']} resumed"
        elif result["status"] == "stopping":
            text = f"run {result['run_id']} is stopping"
        _emit(a, result, text)
        return launch.EXIT.get(result["status"], 1)

    run_id = a.resume or a.stop
    if a.resume and a.stop:
        return done({"status": "invalid", "error": "give one of --resume ID or --stop ID", "run_id": run_id})
    if a.answer and not a.resume:
        return done({"status": "invalid", "error": "--answer goes with --resume", "run_id": run_id})
    answer = None
    if a.answer:
        answer, why = _read_stdin(launch.MAX_ANSWER, "answer")
        if answer is None:
            return done({"status": "invalid", "error": why, "run_id": run_id})
    try:
        layer = ir.load(LAYER)
    except LayerError as exc:
        return done({"status": "invalid", "error": str(exc), "run_id": run_id})
    if a.stop:
        return done(launch.stop(layer, a.stop))
    return done(launch.resume(layer, a.resume, answer=answer))


def helper(a: argparse.Namespace) -> int:
    """--helper: one answer of the voice helper's agent to the prompt on stdin."""
    from adw_modules import helper as helper_module, ir
    from adw_modules.errors import LayerError

    def done(result: dict) -> int:
        if a.json:
            print(json.dumps(result), flush=True)
        elif result["status"] == "ok":
            print(result["reply"], flush=True)
        else:
            _emit(a, result)
        return {"ok": 0, "invalid": 2}.get(result["status"], 1)

    prompt, why = _read_stdin(MAX_PROMPT, "prompt")
    if prompt is None:
        return done({"status": "invalid", "error": why})
    if not prompt.strip():
        return done({"status": "invalid", "error": "the prompt is empty"})
    try:
        layer = ir.load(LAYER)
    except LayerError as exc:
        return done({"status": "invalid", "error": str(exc)})
    return done(helper_module.reply(layer, prompt))




def registry_cli(a: argparse.Namespace) -> int:
    from adw_modules import registry

    try:
        ir = json.loads((LAYER / "agentic-layer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"dispatch: cannot read agentic-layer.json: {exc}", file=sys.stderr)
        return 1
    reg = registry.build(ir, LAYER)
    if a.json:
        print(json.dumps(reg), flush=True)
        return 0
    for entry in reg["entries"]:
        state = "off" if entry["off"] else "by hand" if entry["start"]["by_hand"] else "not by hand"
        how = entry["start"]["command"] or entry["start"]["why"]
        print(f"{entry['name']:<28} {entry['kind']:<14} {entry['source']:<9} {state:<12} {how}")
    for problem in reg["problems"]:
        print(f"problem: {problem}")
    return 0


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
    ap.add_argument("--replaces", metavar="OLD", help="with --start: the run this start begins again")
    ap.add_argument("--decider", choices=("off", "suggest", "auto"),
                    help="with --start: the stand-in's mode for this run")
    ap.add_argument("--pr-state", type=int, metavar="N", help="is pull request N merged? (read only)")
    ap.add_argument("--registry", action="store_true", help="print what the layer can run (read only)")
    ap.add_argument("--json", action="store_true",
                    help="with --start, --pr-state, --resume, --stop, --helper or --registry: print one JSON line")
    ap.add_argument("--resume", metavar="ID", help="continue a waiting, failed or handed-over run, detached")
    ap.add_argument("--answer", action="store_true", help="with --resume: a person's answer to a waiting run on stdin")
    ap.add_argument("--stop", metavar="ID", help="stop a run running on this host")
    ap.add_argument("--helper", action="store_true", help="one answer of the voice helper's agent (prompt on stdin)")
    a = ap.parse_args()
    if a.registry:
        return registry_cli(a)
    if a.pr_state is not None:
        return pr_state(a)
    if a.helper:
        return helper(a)
    if a.resume or a.stop or a.answer:
        return run_control(a)
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
