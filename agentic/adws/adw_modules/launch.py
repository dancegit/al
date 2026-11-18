"""The one way to start work in a layer from outside it: the dashboard's "Start work" panel (through
`adws/dispatch.py --start`) and `al run LAYER /NAME …` both call `start()`.

A start names a target: a workflow the compiled layer declares, or one command or hop of it (a
*single-command run*: one `run` step of that command in the layer's normal environment, recorded
like any run and never shipping; see single_command_adw). `start()`:

  1. checks the request key, the target against the compiled layer, the class and the tier overrides;
  2. returns the run of a request key it has seen before instead of starting a second one;
  3. asks for a confirmation when the same request started less than DUPLICATE_SECONDS ago, or when
     the workflow ships (merges or deploys) without a person;
  4. refuses when the layer's day limit or usage threshold is reached;
  5. optionally logs the request as a forge issue: "trigger" leaves the start to the layer's own
     issue trigger, "start" opens the issue with a claim marker so no trigger starts it again;
  6. reserves the run id, writes the prompt to agents/<id>/request.md (never onto a command line),
     records the request key in agents/starts.json and launches the layer's own script.

A start may name the run it starts again (`replaces`, "Start again" on the dashboard, `al run
--replaces`): that run must exist, a recent duplicate of it needs no confirmation, and the new run
records `replaces` (in its skeleton state and through the runner's --replaces), so the old run reads
as replaced by the new one.

Steps 1-6 hold agents/.starts.lock, so two starts with one key (a double click, a retried request)
start one run. The launched process is detached (its own session) unless the caller attaches.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from . import budget, inputs, ir, models, slots, state as state_module
from .agents import CLASSES
from .errors import LayerError

DUPLICATE_SECONDS = 300            # the same target and prompt again within this window asks first
LEDGER_DAYS = 2                    # request keys are remembered this long
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{8}$")
LEDGER = Path("agents") / "starts.json"
LEDGER_LOCK = Path("agents") / ".starts.lock"
REQUEST_FILE = "request.md"
LAUNCH_LOG = "launch.log"
COMMAND_PREFIX = "command:"
SLASH_RE = re.compile(r"^\s*/(\S+)(?:\s+(.*))?$", re.DOTALL)   # /NAME, then the prompt
ISSUE_MODES = ("none", "start", "trigger")
DECIDER_MODES = ("off", "suggest", "auto")   # decide.MODES: the stand-in's mode for one run (--decider)
TITLE_CHARS = 80
EXIT = {"started": 0, "issue-created": 0, "error": 1, "invalid": 2, "refused": 3,
        "confirm-duplicate": 4, "confirm-ships": 4}


@dataclass(frozen=True)
class Target:
    kind: str          # workflow | command
    name: str
    prompt: str = ""

    @property
    def label(self) -> str:
        return f"{self.kind} {self.name}"

    @property
    def adw(self) -> str:
        """The ADW the run records: the workflow, or command:NAME for a single-command run."""
        return self.name if self.kind == "workflow" else COMMAND_PREFIX + self.name


# ---------------------------------------------------------------- what can be started

def catalog_names(layer: Any) -> tuple[list[str], list[str]]:
    """(workflow names, command and hop names) of the compiled layer, in declaration order."""
    workflows = [str(a.get("name")) for a in layer.ir.get("adws") or [] if isinstance(a, dict) and a.get("name")]
    commands = [str(c.get("name")) for key in ("commands", "hops") for c in layer.ir.get(key) or []
                if isinstance(c, dict) and c.get("name")]
    return workflows, list(dict.fromkeys(commands))


def resolve_target(layer: Any, text: str, kind: str | None = None) -> Target:
    """`/NAME rest…` → the workflow NAME when the layer declares one, else its command or hop NAME;
    `/command:NAME rest…` is always the command. `kind` (workflow | command) forces one of the two."""
    match = SLASH_RE.match(text or "")
    if not match:
        raise LayerError("start with /NAME (a workflow, command or hop of the layer), e.g. /plan-chore tidy the readme")
    head, rest = match.group(1), (match.group(2) or "").strip()
    workflows, commands = catalog_names(layer)
    if head.startswith(COMMAND_PREFIX):
        head, kind = head[len(COMMAND_PREFIX):], "command"
    if kind not in (None, "workflow", "command"):
        raise LayerError(f"unknown kind {kind!r}: workflow or command")
    if kind in (None, "workflow") and head in workflows:
        return Target("workflow", head, rest)
    if kind in (None, "command") and head in commands:
        return Target("command", head, rest)
    known = ", ".join(f"/{n}" for n in [*workflows, *[c for c in commands if c not in workflows]]) or "(none)"
    what = {"workflow": "workflow", "command": "command or hop"}.get(kind or "", "workflow or command")
    raise LayerError(f"no {what} named {head or '(empty)'}; known: {known}")


def single_command_adw(layer: Any, name: str) -> dict[str, Any]:
    """The ADW of a single-command run of `name`: one `run` step of that command or hop with the
    prompt, in the environment, agent and review option of the layer's first workflow (the default
    dispatch.py uses as well). It has no review and no ship step, so it can never ship."""
    layer.command(name)   # LayerError when the layer has no such command or hop
    adws = [a for a in layer.ir.get("adws") or [] if isinstance(a, dict)]
    if not adws:
        raise LayerError("the layer declares no workflow, so there is no environment to run a single command in")
    synthetic = COMMAND_PREFIX + name
    if any(a.get("name") == synthetic for a in adws):
        raise LayerError(f"the layer declares a workflow named {synthetic}; rename it to start /{name} as one command")
    first = adws[0]
    declared = {str(o.get("id")) for o in layer.ir.get("promptInputs") or [] if isinstance(o, dict)}
    return {"name": synthetic, "description": f"single command /{name}", "agent": first.get("agent"),
            "review": first.get("review"), "environment": list(first.get("environment") or []),
            "input": [i for i in ["cli"] if i in declared], "classes": [], "trigger": [], "changes": "codebase",
            "steps": [{"kind": "run", "name": name, "command": name, "args": ["$prompt"], "modelSet": "inherit",
                       "agent": "", "retries": 0, "when": "always"}]}


def run_command(layer_dir: Path, name: str, argv: list[str] | None = None) -> int:
    """Run one command in the foreground as a recorded run (what a detached single-command start executes)."""
    from . import runner  # the runner imports most of the runtime; only a run needs it

    layer = ir.load(Path(layer_dir))
    definition = single_command_adw(layer, name)
    return runner.main(adw=definition["name"], layer_dir=Path(layer_dir), argv=list(argv or []), definition=definition)


def ships_unattended(layer: Any, adw: dict[str, Any]) -> str:
    """"merge" / "deploy" when the workflow ships without a person (an auto review and a ship step), else ""."""
    if not any(isinstance(s, dict) and s.get("kind") == "ship" for s in adw.get("steps") or []):
        return ""
    try:
        review = layer.option("reviews", adw.get("review"))
    except LayerError:
        return ""
    if review.get("kind") == "auto" and review.get("ship") in ("merge", "deploy"):
        return str(review["ship"])
    return ""


def trigger_started(layer: Any, adw: dict[str, Any]) -> bool:
    """True when a cron or webhook trigger of the layer starts this workflow."""
    kinds = {str(t.get("id")): str(t.get("kind")) for t in layer.ir.get("triggers") or [] if isinstance(t, dict)}
    return any(kinds.get(str(t)) in ("cron", "webhook") for t in adw.get("trigger") or [])


# ---------------------------------------------------------------- the request-key ledger

def _ledger_lock(layer: Any):
    return slots.file_lock(Path(layer.layer_dir) / LEDGER_LOCK)


def _load_ledger(layer: Any) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((Path(layer.layer_dir) / LEDGER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    starts = data.get("starts") if isinstance(data, dict) else None
    return {str(k): v for k, v in (starts or {}).items() if isinstance(v, dict)}


def _save_ledger(layer: Any, starts: dict[str, dict[str, Any]], now: float | None = None) -> None:
    now = time.time() if now is None else now
    keep = {k: v for k, v in starts.items() if now - float(v.get("at") or 0) < LEDGER_DAYS * 86400}
    path = Path(layer.layer_dir) / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, prefix=".starts.") as handle:
        json.dump({"version": 1, "starts": keep}, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(handle.name, path)


def prompt_sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- start

def _spawn(argv: list[str], **popen: Any) -> subprocess.Popen:
    return subprocess.Popen(argv, **popen)


def issue_title(prompt: str) -> str:
    line = next((x.strip() for x in prompt.splitlines() if x.strip()), "work request")
    return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS - 1].rstrip() + "…"


def _result(status: str, **fields: Any) -> dict[str, Any]:
    return {"status": status, **fields}


def _check(layer: Any, target: Target, prompt: str, request_key: str, klass: str | None, model_set: str | None,
           model_list: list[str], issue: str, decider: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """(the ADW to run, the overrides) or LayerError / ValueError with the reason."""
    if not isinstance(request_key, str) or not KEY_RE.fullmatch(request_key):
        raise ValueError("request key: 8 to 64 letters, digits, - or _")
    workflows, commands = catalog_names(layer)
    if target.kind == "workflow":
        if target.name not in workflows:
            raise LayerError(f"no workflow named {target.name}; known: {', '.join(workflows) or '(none)'}")
        adw = layer.validate_adw(target.name)
    elif target.kind == "command":
        if target.name not in commands:
            raise LayerError(f"no command or hop named {target.name}; known: {', '.join(commands) or '(none)'}")
        adw = single_command_adw(layer, target.name)
    else:
        raise ValueError(f"unknown kind {target.kind!r}: workflow or command")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("a prompt is required")
    if klass is not None and klass not in CLASSES:
        raise ValueError(f"unknown class {klass!r}; one of {', '.join(CLASSES)}")
    if issue not in ISSUE_MODES:
        raise ValueError(f"issue: one of {', '.join(ISSUE_MODES)}")
    if decider and decider not in DECIDER_MODES:
        raise ValueError(f"decider: one of {', '.join(DECIDER_MODES)}")
    if issue != "none" and str((layer.ir.get("forge") or {}).get("kind") or "none") == "none":
        raise ValueError("the layer has no forge (forge none): the request cannot be logged as an issue")
    if issue == "trigger":
        if target.kind != "workflow":
            raise ValueError("a trigger starts workflows only: start a single command now, without an issue")
        if inputs.issue_input_for(layer.ir, target.name) is None or not trigger_started(layer, adw):
            raise ValueError(f"no cron or webhook trigger starts {target.name} from an issue; start it now instead")
    over = models.overrides(model_set, model_list, None)
    for model in over["models"].values():
        models.check_model_id(model)
    models.check_overrides(layer, adw, over)
    return adw, over


def start(layer: Any, target: Target, prompt: str, *, request_key: str, klass: str | None = None,
          model_set: str | None = None, models: Iterable[str] = (), issue: str = "none",
          confirm_duplicate: bool = False, confirm_ships: bool = False, dry_run: bool = False,
          detach: bool = True, spawn: Callable[..., Any] = _spawn, forge: Any = None,
          replaces: str = "", decider: str = "") -> dict[str, Any]:
    """Start `target` with `prompt` once per `request_key`; see the module docstring. `models` are
    TIER=MODEL_ID strings; `replaces` is the run this one starts again; `decider` is the stand-in's
    mode for this run (`--decider MODE`), "" for the layer's. Returns {"status": started | issue-created | confirm-duplicate |
    confirm-ships | refused | invalid | error, …}; EXIT maps the status to an exit code."""
    model_list = [str(m) for m in models]
    layer_dir = Path(layer.layer_dir)
    shown = {"kind": target.kind, "name": target.name}
    base = {"layer": layer.name, "target": shown}
    try:
        adw, over = _check(layer, target, prompt, request_key, klass, model_set or None, model_list, issue,
                           str(decider or ""))
    except (LayerError, ValueError) as exc:
        return _result("invalid", error=str(exc), **base)
    replaces = str(replaces or "")
    if replaces and not RUN_ID_RE.fullmatch(replaces):
        return _result("invalid", error="replaces: a run id of 8 hexadecimal characters", **base)
    if replaces and not (state_module.run_dir(layer_dir, replaces) / "state.json").is_file():
        return _result("invalid", error=f"replaces: the layer has no run {replaces}", **base)
    prompt = prompt.strip()
    digest, label = prompt_sha(prompt), f"{target.kind}:{target.name}"
    with _ledger_lock(layer):
        starts = _load_ledger(layer)
        seen = starts.get(request_key)
        if seen:
            status = "started" if seen.get("run_id") else "issue-created"
            return _result(status, run_id=seen.get("run_id") or "", issue=seen.get("issue"), existing=True,
                           queued="", **base)
        now = time.time()
        if not confirm_duplicate:
            recent = [v for v in starts.values() if v.get("target") == label and v.get("prompt_sha") == digest
                      and now - float(v.get("at") or 0) < DUPLICATE_SECONDS
                      and not (replaces and v.get("run_id") == replaces)]   # starting that run again is the point
            if recent:
                last = max(recent, key=lambda v: float(v.get("at") or 0))
                ago = int(now - float(last.get("at") or 0))
                what = f"run {last['run_id']}" if last.get("run_id") else "an issue for the trigger"
                return _result("confirm-duplicate", previous=last.get("run_id") or "", message=(
                    f"the same request to {target.label} started {what} {ago} s ago"), **base)
        ships = ships_unattended(layer, adw) if target.kind == "workflow" else ""
        if ships and not confirm_ships:
            verb = "merges" if ships == "merge" else "deploys"
            return _result("confirm-ships", ships=ships, message=(
                f"{target.name} {verb} without a person when every gate passes"), **base)
        refused = budget.refuse_start(layer, budget.limits(layer, over))
        if refused:
            return _result("refused", message=refused, **base)
        option = inputs.issue_input_for(layer.ir, target.name) if target.kind == "workflow" else None
        client = None
        if issue != "none":
            from . import forge as forge_mod

            client = forge or forge_mod.get_client(layer)
        if issue == "trigger":
            keyword = str(option.get("keyword") or "adw")
            labels = [*(option.get("labels") or []), *([klass] if klass else [])]
            try:
                created = client.create_issue(issue_title(prompt), f"{prompt}\n\n{keyword}: {target.name}", labels)
            except Exception as exc:  # the forge's own failure (ForgeError, HTTP): nothing was started
                return _result("error", error=f"creating the issue failed: {exc}", **base)
            starts[request_key] = {"run_id": "", "target": label, "prompt_sha": digest, "at": now, "issue": created}
            _save_ledger(layer, starts, now)
            return _result("issue-created", run_id="", issue=created, existing=False, queued="",
                           message="the layer's trigger will start it", **base)
        run_id = state_module.new_adw_id()
        while state_module.run_dir(layer_dir, run_id).exists():
            run_id = state_module.new_adw_id()
        run = state_module.run_dir(layer_dir, run_id)
        run.mkdir(parents=True)
        request = run / REQUEST_FILE
        handle = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(prompt + "\n")
        created = None
        if issue == "start":
            try:
                created = client.create_issue(issue_title(prompt), f"{prompt}\n\n{inputs.claim_marker(run_id)}",
                                              [klass] if klass else [])
            except Exception as exc:  # nothing starts without the issue the person asked for
                request.unlink(missing_ok=True)
                run.rmdir()
                return _result("error", error=f"creating the issue failed: {exc}", **base)
        starts[request_key] = {"run_id": run_id, "target": label, "prompt_sha": digest, "at": now, "issue": created}
        _save_ledger(layer, starts, now)
        # what a reader sees until the run writes its own state: running, without a process to
        # check, so a launch that never comes up shows as died (and launch.log says why)
        skeleton = state_module.ADWState(adw_id=run_id, adw=target.adw, prompt=prompt, source="cli",
                                         dry_run=dry_run, definition=adw if target.kind == "command" else {},
                                         replaces=replaces)
        skeleton.host, skeleton.heartbeat = "", state_module.now()
        skeleton.save(layer_dir)
    if target.kind == "workflow":
        source = f"{option['id']}:{created['number']}" if option and created and created.get("number") else None
        argv = inputs.adw_argv(layer_dir, target.name, source) + ["--adw-id", run_id]
        if source is None:
            argv += ["--prompt-file", str(request)]
    else:
        argv = ["uv", "run", "--quiet", str(layer_dir / "adws" / "dispatch.py"), "--command", target.name,
                "--adw-id", run_id, "--prompt-file", str(request)]
    argv += ["--class", klass] if klass else []
    argv += ["--model-set", model_set] if model_set else []
    for item in model_list:
        argv += ["--model", item]
    argv += ["--dry-run"] if dry_run else []
    argv += ["--replaces", replaces] if replaces else []
    argv += ["--decider", str(decider)] if decider else []
    limits = slots.limits(layer)
    queued = (f"all {max(1, limits['runs'])} run slots are in use; it waits up to {limits['wait']} s"
              if slots.free_slots(layer, "runs") == 0 else "")
    result = _result("started", run_id=run_id, issue=created, queued=queued, existing=False, **base)
    if detach:
        with (run / LAUNCH_LOG).open("ab") as log:
            try:
                spawn(argv, cwd=str(layer_dir), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                      start_new_session=True)
            except OSError as exc:
                log.write(f"could not start {argv[0]}: {exc}\n".encode("utf-8"))
                return _result("error", error=f"could not start {argv[0]}: {exc}", run_id=run_id, **base)
        return result
    try:
        process = spawn(argv, cwd=str(layer_dir))
    except OSError as exc:
        return _result("error", error=f"could not start {argv[0]}: {exc}", run_id=run_id, **base)
    while True:
        try:
            result["exit"] = int(process.wait())
            return result
        except KeyboardInterrupt:   # the run shares the terminal and got the interrupt too: wait for it to end
            continue
