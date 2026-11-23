"""Pick the ADW for a prompt input and run it; or start work the way the dashboard does.

  dispatch.py [PROMPT] [--source KIND:REF] [--adw NAME] [--class C] [--dry-run]
  dispatch.py --start (--adw NAME | --command NAME) --request-key KEY [--class C] [--model-set T]
              [--testing LEVEL] [--model T=ID …] [--issue none|start|trigger] [--confirm-duplicate] [--confirm-ships]
              [--dry-run] [--replaces OLD] [--decider off|suggest|auto] [--approve WHAT …] [--who WHO] [--json]   < prompt
  dispatch.py --grant RUN --approve WHAT [--approve WHAT …] [--who WHO] [--json]
  dispatch.py --approvable RUN [RUN …] [--json]
  dispatch.py --approve-resume RUN [--item ID …] [--token T] --who NAME [--note TEXT] [--json]
  dispatch.py --approve-ship RUN [--token T] --who NAME [--note TEXT] [--json]
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
mode for that run (the run's own --decider; without it the layer's mode applies). --approve WHAT pre-approves a
decision only a person may take for the run (approvals.py), given by --who. --grant RUN approves
WHAT for a run that stopped for an approver and resumes it; exit 0 resumed, 1 refused, 2 invalid.
--approvable RUN … derives, for each run, whether it waits only for a person's approval, of which items,
and where it resumes (adw_modules/approvable.py), stores that as agents/RUN/approvable.json (where the
dashboard reads it) and prints it (with --json: one line {"runs": {RUN: record}}); exit 0.
--approve-resume RUN approves the items the run waits for (--item ID, repeatable; none means all of them;
--token T is the token of the derivation that was shown) as --who, with an optional one-line --note, and
resumes the run; exit 0 resumed, 1 refused. --approve-ship RUN approves a gate stop that only needs a
person's yes (kind ship of approvable.json: the diff is too large, the class is not one the gate ships, the
ship failed) as --who, with --token and --note as above; the runtime records the approval, then
fast-forwards the stage branch or settles the run as shipped to its pull request or branch
(adw_modules/shipapproval.py); exit 0 merged or shipped, 1 refused. --command alone runs one
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
1 otherwise. In a layer with `(placement …)` the launched workflow script decides where the run
executes and, when that is another host, stays as the run's proxy (adw_modules/remoterun.py); nothing
here changes for that. The (PEP 723) script header is prepended by `al gen`.
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
                             model_set=a.model_set, models=a.model, issue=a.issue, testing=a.testing,
                             confirm_duplicate=a.confirm_duplicate, confirm_ships=a.confirm_ships,
                             dry_run=a.dry_run, replaces=a.replaces or "", decider=a.decider or "",
                             approvals=a.approve,
                             approved_by=a.who or ""))


def grant(a: argparse.Namespace) -> int:
    from adw_modules import approvals

    def done(result: dict, code: int) -> int:
        if a.json:
            print(json.dumps(result), flush=True)
        else:
            print(f"dispatch: {result['status']}: {result.get('message') or ''}", flush=True)
        return code

    if not a.approve:
        return done({"status": "invalid", "reason": "bad-approval", "message": "--grant needs --approve WHAT"}, 2)
    try:
        for what in a.approve:
            approvals.parse(what)
    except ValueError as exc:
        return done({"status": "invalid", "reason": "bad-approval", "message": str(exc)}, 2)
    result = approvals.grant_and_resume(LAYER, a.grant, a.approve, a.who or "dashboard", "dashboard")
    return done(result, 0 if result["status"] == "resumed" else 1)


def approvable_cli(a: argparse.Namespace) -> int:
    """--approvable RUN [RUN …]: what each run waits for, stored for the dashboard and printed."""
    from adw_modules import approvable

    records = {}
    for run_id in a.approvable:
        try:
            records[run_id] = approvable.write(LAYER, run_id)
        except Exception as exc:  # one run that cannot be derived does not hide the others
            records[run_id] = {"version": 1, "run": run_id, "basis": "", "waiting": False, "kind": "", "items": [],
                               "from_step": "", "after": "", "token": "", "at": "",
                               "why": f"not derived: {type(exc).__name__}: {exc}"[:300]}
    if a.json:
        print(json.dumps({"runs": records}), flush=True)
    else:
        for record in records.values():
            print("\n".join(approvable.lines(record)), flush=True)
    return 0


def approve_resume_cli(a: argparse.Namespace) -> int:
    """--approve-resume RUN [--item ID …] [--token T] --who NAME [--note TEXT]: approve and resume."""
    from adw_modules import approvable

    result = approvable.approve_resume(LAYER, a.approve_resume, list(a.item) or None, a.who or "", "dashboard",
                                       note=a.note or "", token=a.token or "")
    if a.json:
        print(json.dumps(result), flush=True)
    else:
        print(f"dispatch: {result['status']}: {result.get('message') or ''}", flush=True)
    return 0 if result["status"] == "resumed" else 1


def approve_ship_cli(a: argparse.Namespace) -> int:
    """--approve-ship RUN [--token T] --who NAME [--note TEXT]: approve a gate stop and merge or ship."""
    from adw_modules import approvable

    result = approvable.approve_ship(LAYER, a.approve_ship, a.who or "", "dashboard", note=a.note or "",
                                     token=a.token or "")
    if a.json:
        print(json.dumps(result), flush=True)
    else:
        print(f"dispatch: {result['status']}: {result.get('message') or ''}", flush=True)
    return 0 if result["status"] in ("merged", "shipped") else 1


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
    ap.add_argument("--testing", metavar="LEVEL", help="with --start: the depth of the run's checks "
                    "(light|standard|heavy or smoke|targeted|full)")
    ap.add_argument("--issue", choices=("none", "start", "trigger"), default="none",
                    help="with --start: also log the request as a forge issue")
    ap.add_argument("--confirm-duplicate", action="store_true")
    ap.add_argument("--confirm-ships", action="store_true")
    ap.add_argument("--replaces", metavar="OLD", help="with --start: the run this start begins again")
    ap.add_argument("--decider", choices=("off", "suggest", "auto"),
                    help="with --start: the stand-in's mode for this run")
    ap.add_argument("--approve", action="append", default=[], metavar="WHAT",
                    help="with --start or --grant: an approval for the run (repeatable)")
    ap.add_argument("--who", metavar="WHO", help="with --approve, --approve-resume or --approve-ship: who approves")
    ap.add_argument("--grant", metavar="RUN", help="approve --approve WHAT for stopped run RUN and resume it")
    ap.add_argument("--approvable", nargs="+", metavar="RUN",
                    help="what each stopped run waits for a person's approval of (stored as approvable.json)")
    ap.add_argument("--approve-resume", metavar="RUN", help="approve what stopped run RUN waits for and resume it")
    ap.add_argument("--item", action="append", default=[], metavar="ID",
                    help="with --approve-resume: an item to approve (repeatable; none means all of them)")
    ap.add_argument("--approve-ship", metavar="RUN",
                    help="approve a gate stop of run RUN that only needs a person's yes, then merge or ship it")
    ap.add_argument("--token", metavar="T",
                    help="with --approve-resume or --approve-ship: the token of what was shown")
    ap.add_argument("--note", metavar="TEXT",
                    help="with --approve-resume or --approve-ship: one line kept with the approval")
    ap.add_argument("--pr-state", type=int, metavar="N", help="is pull request N merged? (read only)")
    ap.add_argument("--registry", action="store_true", help="print what the layer can run (read only)")
    ap.add_argument("--json", action="store_true",
                    help="with --start, --grant, --approvable, --approve-resume, --approve-ship, --pr-state, --resume, --stop, "
                         "--helper or --registry: print one JSON line")
    ap.add_argument("--resume", metavar="ID", help="continue a waiting, failed or handed-over run, detached")
    ap.add_argument("--answer", action="store_true", help="with --resume: a person's answer to a waiting run on stdin")
    ap.add_argument("--stop", metavar="ID", help="stop a run running on this host")
    ap.add_argument("--helper", action="store_true", help="one answer of the voice helper's agent (prompt on stdin)")
    a = ap.parse_args()
    if a.registry:
        return registry_cli(a)
    if a.grant:
        return grant(a)
    if a.approvable:
        return approvable_cli(a)
    if a.approve_resume:
        return approve_resume_cli(a)
    if a.approve_ship:
        return approve_ship_cli(a)
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
