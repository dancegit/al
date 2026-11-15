"""PITER · P — prompt inputs.

`resolve(layer, source, prompt)` turns `--source KIND:REF` (+ optional prompt)
into a `Request`; `pick_adw(layer, request)` chooses the ADW for it.

Sources: None/cli, tasklist:<line_no>, issue:<n>, spec-file:<path>,
payload:<json file>, voice. KIND may also be a prompt-input option id.

The second half of the module is the polling side used by the triggers:
`poll(layer, trigger_id)` lists runnable `Candidate`s, `claim()` marks one
taken before launch so nothing starts twice, `adw_argv()` builds the command.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import forge as forge_mod
from . import tasklist as tl
from .forge import as_layer

SOURCE_KINDS = {"cli": "cli", "tasklist": "tasklist", "issue": "issue", "spec-file": "spec-file",
                "payload": "webhook-payload", "webhook-payload": "webhook-payload", "voice": "voice"}
CLAIM_ENV = "ADW_TASK_CLAIM"  # set by triggers: the claim id they wrote into tasks.md
VOICE_DIR = Path(__file__).resolve().parent.parent / "voice"  # adws/voice in a layer, kit/runtime/voice in the kit

DEFAULTS = {
    "cli": {"id": "cli", "kind": "cli", "allowStdin": True},
    "tasklist": {"id": "tasklist", "kind": "tasklist", **tl.DEFAULT_OPTION},
    "issue": {"id": "issue", "kind": "issue", "labels": [], "keyword": "adw", "classify": True},
    "spec-file": {"id": "spec-file", "kind": "spec-file", "dir": "specs/inbox", "pattern": "*.md"},
    "webhook-payload": {"id": "webhook-payload", "kind": "webhook-payload",
                        "promptField": "prompt", "workflowField": "workflow"},
    "voice": {"id": "voice", "kind": "voice", "confirm": True, "route": "auto", "appendToTasklist": True},
}


class InputError(RuntimeError):
    """The prompt input could not be resolved."""


def _noop(_state: Any) -> None:
    return None


@dataclass
class Request:
    prompt: str
    klass: str | None = None
    model_set: str | None = None
    workflow: str | None = None
    plan: str | None = None
    source_kind: str = "cli"  # prompt-input kind
    source_ref: str | None = None
    input_id: str = "cli"  # prompt-input option id
    option: dict = field(default_factory=dict)
    start_hook: Callable[[Any], None] = field(default=_noop, repr=False)
    finish_hook: Callable[[Any], None] = field(default=_noop, repr=False)

    def on_start(self, state: Any) -> None:
        self.start_hook(state)

    def on_finish(self, state: Any) -> None:
        self.finish_hook(state)


# --------------------------------------------------------------------------- helpers


def find_option(ir: Mapping[str, Any], kind_or_id: str) -> dict:
    """Prompt-input option by id first, then by kind; cli falls back to its default."""
    opts = ir.get("promptInputs") or []
    for o in opts:
        if o.get("id") == kind_or_id:
            return dict(o)
    kind = SOURCE_KINDS.get(kind_or_id, kind_or_id)
    for o in opts:
        if o.get("kind") == kind:
            return dict(o)
    if kind == "cli":
        return dict(DEFAULTS["cli"])
    raise InputError(f"the layer declares no prompt-input {kind_or_id!r}")


def option_by_id(ir: Mapping[str, Any], input_id: str) -> dict | None:
    for o in ir.get("promptInputs") or []:
        if o.get("id") == input_id:
            return {**DEFAULTS.get(o.get("kind"), {}), **o}
    if input_id == "cli":
        return dict(DEFAULTS["cli"])
    return None


def _attr(state: Any, name: str, default: Any = None) -> Any:
    if isinstance(state, Mapping):
        return state.get(name, default)
    return getattr(state, name, default)


def summarise(state: Any) -> str:
    adw_id = _attr(state, "adw_id", "?")
    status = _attr(state, "status", "unknown")
    branch = _attr(state, "branch")
    ship = _attr(state, "ship") or {}
    parts = [f"adw {adw_id} {status}"]
    if branch:
        parts.append(f"branch `{branch}`")
    ship = ship if isinstance(ship, Mapping) else {}
    pr = ship.get("pr") if isinstance(ship.get("pr"), Mapping) else {}
    url = ship.get("url") or ship.get("pr_url") or pr.get("url")
    if ship.get("action") == "handoff":   # a stopped run whose work was handed over (ship.handoff)
        parts.append(f"stopped at `{ship.get('stopped_at', '?')}`; the work needs a human: "
                     + (f"PR {url}" if url else str(ship.get("target") or "see the run")))
    elif url:
        parts.append(f"PR {url}")
    commit = ship.get("commit") if isinstance(ship, Mapping) else None
    if commit:
        parts.append(f"commit {commit}")
    return ", ".join(parts)


def keyword_selection(text: str, keyword: str) -> tuple[str | None, str | None]:
    """Find `<keyword>: <workflow> [heavy|base]` at the start of a line."""
    rx = re.compile(r"^[ \t]*" + re.escape(keyword) + r"[ \t]*:[ \t]*([\w.\-]+)(?:[ \t]+(heavy|base))?[ \t]*$",
                    re.IGNORECASE | re.MULTILINE)
    m = rx.search(text or "")
    if not m:
        return None, None
    return m.group(1), (m.group(2) or "").lower() or None


def dotted(data: Any, path: str) -> Any:
    cur = data
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, Mapping):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def run_listen(layer_dir: Path) -> str:
    """Record + transcribe with voice/listen.py; return the transcript."""
    script = VOICE_DIR / "listen.py"
    argv = ["uv", "run", "--quiet", str(script), "--layer", str(layer_dir)]
    proc = subprocess.run(argv, stdout=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise InputError(f"voice/listen.py exited {proc.returncode}")
    return proc.stdout.strip()


# --------------------------------------------------------------------------- resolve


def resolve(layer: Any, source: str | None, prompt: str | None,
            env: Mapping[str, str] | None = None, stdin=None, forge: Any = None,
            listen: Callable[[Path], str] | None = None) -> Request:
    lay = as_layer(layer)
    env = os.environ if env is None else env
    kind_or_id, _, ref = (source or "cli").partition(":")
    option = find_option(lay.ir, kind_or_id)
    kind = option.get("kind")
    option = {**DEFAULTS.get(kind, {}), **option}
    base = dict(source_kind=kind, source_ref=ref or None, input_id=option.get("id", kind), option=option)

    if kind == "cli":
        if prompt is None and option.get("allowStdin", True):
            prompt = (stdin or sys.stdin).read()
        if not (prompt or "").strip():
            raise InputError("no prompt given (pass it as an argument or on stdin)")
        return Request(prompt=prompt.strip(), **base)
    if kind == "tasklist":
        return _resolve_tasklist(lay, option, ref, env, base)
    if kind == "issue":
        return _resolve_issue(lay, option, ref, env, base, forge)
    if kind == "spec-file":
        return _resolve_spec(lay, option, ref, base)
    if kind == "webhook-payload":
        return _resolve_payload(lay, option, ref, base)
    if kind == "voice":
        if not (prompt or "").strip():
            prompt = (listen or run_listen)(lay.layer_dir)
        if not (prompt or "").strip():
            raise InputError("nothing was recognised")
        return Request(prompt=prompt.strip(), **base)
    raise InputError(f"prompt-input kind {kind!r} is not supported by this runtime")


def _resolve_tasklist(lay, option, ref, env, base) -> Request:
    if not ref or not ref.strip().isdigit():
        raise InputError("tasklist source needs a line number: --source tasklist:<line_no>")
    line_no = int(ref)
    path = tl.path_for(lay.layer_dir, option)
    if not path.exists():
        raise InputError(f"task list not found: {path}")
    tasks = tl.parse(path.read_text(encoding="utf-8"), option)
    task = next((t for t in tasks if t.line_no == line_no), None)
    if task is None:
        raise InputError(f"no task on line {line_no} of {path}")
    trigger_claim = env.get(CLAIM_ENV) or None
    if task.status == "running":
        if not (trigger_claim and task.adw_id == trigger_claim):
            raise InputError(f"task on line {line_no} is already running (adw {task.adw_id})")
    elif task not in tl.eligible(tasks):
        raise InputError(f"task on line {line_no} is {task.status}, not eligible")

    adw_names = [a.get("name") for a in lay.ir.get("adws") or []]
    holder: dict[str, str] = {}

    def start(state):
        adw_id = _attr(state, "adw_id")
        if task.status == "running":
            tl.retag(path, trigger_claim, adw_id, option)
        else:
            tl.claim(path, task, adw_id, option)
        holder["id"] = adw_id

    def finish(state):
        adw_id = holder.get("id") or _attr(state, "adw_id")
        if _attr(state, "status") == "passed":
            ship = _attr(state, "ship") or {}
            tl.complete(path, adw_id, (ship or {}).get("commit"), option)
        else:
            tl.fail(path, adw_id, _attr(state, "error") or f"adw {_attr(state, 'status', 'failed')}", option)

    return Request(prompt=task.text, klass=task.klass, model_set=task.model_set,
                   workflow=task.workflow(adw_names), start_hook=start, finish_hook=finish, **base)


def _resolve_issue(lay, option, ref, env, base, forge) -> Request:
    if not ref or not ref.strip().lstrip("#").isdigit():
        raise InputError("issue source needs a number: --source issue:<n>")
    n = int(ref.strip().lstrip("#"))
    client = forge or forge_mod.get_client(lay, env)
    issue = client.get_issue(n)
    keyword = option.get("keyword") or "adw"
    workflow = model_set = None
    for c in reversed(client.list_comments(n)):
        if client.is_bot(c.get("body")):
            continue
        workflow, model_set = keyword_selection(c.get("body", ""), keyword)
        break  # only the latest non-bot comment counts
    if workflow is None:
        workflow, model_set = keyword_selection(issue.get("body", ""), keyword)
    klass = next((lbl for lbl in issue.get("labels") or [] if lbl in tl.CLASSES), None)

    def start(state):
        client.comment(n, f"adw {_attr(state, 'adw_id')} started")

    def finish(state):
        client.comment(n, summarise(state))

    prompt = f"{issue.get('title', '')}\n\n{issue.get('body', '')}".strip()
    return Request(prompt=prompt, klass=klass, model_set=model_set, workflow=workflow,
                   start_hook=start, finish_hook=finish, **base)


def _resolve_spec(lay, option, ref, base) -> Request:
    if not ref:
        raise InputError("spec-file source needs a path: --source spec-file:<path>")
    candidates = [Path(ref)] if Path(ref).is_absolute() else [
        Path.cwd() / ref, lay.layer_dir / ref, lay.layer_dir / option.get("dir", "specs/inbox") / ref]
    spec = next((p.resolve() for p in candidates if p.is_file()), None)
    if spec is None:
        raise InputError(f"spec file not found: {ref}")
    return Request(prompt=f"Implement the spec at {spec}", plan=str(spec), **base)


def _resolve_payload(lay, option, ref, base) -> Request:
    if not ref:
        raise InputError("payload source needs a JSON file: --source payload:<file>")
    p = Path(ref)
    if not p.is_absolute() and not p.exists():
        p = lay.layer_dir / ref
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read payload {p}: {exc}") from exc
    prompt = dotted(data, option.get("promptField", "prompt"))
    if not isinstance(prompt, str) or not prompt.strip():
        raise InputError(f"payload has no prompt at {option.get('promptField')!r}")
    workflow = dotted(data, option.get("workflowField", "workflow"))
    return Request(prompt=prompt.strip(), workflow=workflow if isinstance(workflow, str) else None, **base)


# --------------------------------------------------------------------------- choose the ADW


def _accepts(adw: Mapping[str, Any], input_id: str, klass: str | None) -> bool:
    if input_id not in (adw.get("input") or []):
        return False
    classes = adw.get("classes") or []
    return not classes or klass is None or klass in classes


def pick_adw(layer: Any, request: Request, adws: Iterable[Mapping[str, Any]] | None = None) -> str:
    """Explicit workflow if it accepts this input; else the first ADW accepting input + class."""
    lay = as_layer(layer)
    pool = list(adws if adws is not None else lay.ir.get("adws") or [])
    if request.workflow:
        for a in pool:
            if a.get("name") == request.workflow and request.input_id in (a.get("input") or []):
                return a["name"]
    for a in pool:
        if _accepts(a, request.input_id, request.klass):
            return a["name"]
    raise InputError(f"no ADW accepts input {request.input_id!r}"
                     + (f" with class {request.klass}" if request.klass else ""))


# --------------------------------------------------------------------------- polling (triggers)


@dataclass
class Candidate:
    adw: str
    source: str  # value for --source
    input_id: str
    kind: str  # tasklist | issue | spec-file
    label: str  # one-line description for status output
    task: tl.Task | None = None
    path: Path | None = None
    claim_id: str | None = None


def adws_for_trigger(ir: Mapping[str, Any], trigger_id: str) -> list[dict]:
    return [a for a in ir.get("adws") or [] if trigger_id in (a.get("trigger") or [])]


def _choose(adws: list[dict], input_id: str, workflow: str | None, klass: str | None) -> str | None:
    req = Request(prompt="", workflow=workflow, klass=klass, input_id=input_id)
    if workflow and not any(a["name"] == workflow and input_id in a.get("input", []) for a in adws):
        return None  # the item asks for an ADW this trigger does not start
    try:
        return pick_adw(SimpleLayer({"adws": adws}), req, adws)
    except InputError:
        return None


class SimpleLayer:
    def __init__(self, ir: dict, layer_dir: Path | None = None):
        self.ir, self.layer_dir, self.app_root = ir, layer_dir or Path("."), None


def seen_path(layer_dir: Path) -> Path:
    return Path(layer_dir) / "agents" / "trigger-seen.json"


def load_seen(layer_dir: Path) -> dict:
    try:
        return json.loads(seen_path(layer_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def poll(layer: Any, trigger_id: str, forge: Any = None, env: Mapping[str, str] | None = None,
         log: Callable[[str], None] = lambda _m: None) -> list[Candidate]:
    """Every runnable item for the ADWs that list `trigger_id`, in a stable order."""
    lay = as_layer(layer)
    adws = adws_for_trigger(lay.ir, trigger_id)
    input_ids: list[str] = []
    for a in adws:
        for i in a.get("input") or []:
            if i not in input_ids:
                input_ids.append(i)
    out: list[Candidate] = []
    for input_id in input_ids:
        option = option_by_id(lay.ir, input_id)
        if not option:
            continue
        kind = option.get("kind")
        try:
            if kind == "tasklist":
                out += _poll_tasklist(lay, adws, option)
            elif kind == "issue":
                out += _poll_issues(lay, adws, option, forge, env)
            elif kind == "spec-file":
                out += _poll_specs(lay, adws, option)
        except Exception as exc:  # one broken input must not stop the others
            log(f"poll {input_id}: {exc}")
    return out


def _poll_tasklist(lay, adws, option) -> list[Candidate]:
    path = tl.path_for(lay.layer_dir, option)
    if not path.exists():
        return []
    names = [a["name"] for a in lay.ir.get("adws") or []]
    out = []
    for t in tl.eligible(tl.parse(path.read_text(encoding="utf-8"), option), serial_sections=True):
        adw = _choose(adws, option["id"], t.workflow(names), t.klass)
        if adw:
            out.append(Candidate(adw, f"{option['id']}:{t.line_no}", option["id"], "tasklist",
                                 f"{t.section}: {t.text[:60]}", task=t, path=path))
    return out


def _poll_issues(lay, adws, option, forge, env) -> list[Candidate]:
    client = forge or forge_mod.get_client(lay, env)
    out = []
    for issue in client.list_issues(option.get("labels") or []):
        comments = client.list_comments(issue["number"])
        if any(client.is_bot(c.get("body")) for c in comments):
            continue
        workflow = None
        for c in reversed(comments):
            workflow = keyword_selection(c.get("body", ""), option.get("keyword") or "adw")[0]
            break
        workflow = workflow or keyword_selection(issue.get("body", ""), option.get("keyword") or "adw")[0]
        klass = next((lbl for lbl in issue.get("labels") or [] if lbl in tl.CLASSES), None)
        adw = _choose(adws, option["id"], workflow, klass)
        if adw:
            out.append(Candidate(adw, f"{option['id']}:{issue['number']}", option["id"], "issue",
                                 f"#{issue['number']} {issue.get('title', '')[:60]}"))
    return out


def _poll_specs(lay, adws, option) -> list[Candidate]:
    inbox = Path(lay.layer_dir) / option.get("dir", "specs/inbox")
    if not inbox.is_dir():
        return []
    seen = load_seen(lay.layer_dir).get("spec-file", {})
    adw = _choose(adws, option["id"], None, None)
    out = []
    for p in sorted(inbox.iterdir()):
        if p.is_file() and fnmatch.fnmatch(p.name, option.get("pattern") or "*.md") and str(p) not in seen and adw:
            out.append(Candidate(adw, f"{option['id']}:{p}", option["id"], "spec-file", p.name, path=p))
    return out


def claim(layer: Any, cand: Candidate, forge: Any = None, env: Mapping[str, str] | None = None) -> bool:
    """Mark a candidate as taken before launching. False when someone else got there first."""
    lay = as_layer(layer)
    if cand.kind == "tasklist":
        cand.claim_id = "trg-" + uuid.uuid4().hex[:8]
        option = option_by_id(lay.ir, cand.input_id)
        try:
            tl.claim(cand.path, cand.task, cand.claim_id, option)
        except tl.TaskError:
            return False
        return True
    if cand.kind == "issue":
        client = forge or forge_mod.get_client(lay, env)
        n = int(cand.source.split(":", 1)[1])
        client.comment(n, f"queued for adw `{cand.adw}`")
        return True
    if cand.kind == "spec-file":
        path = seen_path(lay.layer_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tl.locked(path):
            data = load_seen(lay.layer_dir)
            seen = data.setdefault("spec-file", {})
            if str(cand.path) in seen:
                return False
            seen[str(cand.path)] = {"adw": cand.adw}
            tl._write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
        return True
    return True


def release(layer: Any, cand: Candidate, returncode: int) -> None:
    """After the child exits: fail a task the ADW never took over (it died before on_start)."""
    if cand.kind != "tasklist" or not cand.claim_id:
        return
    lay = as_layer(layer)
    try:
        tl.fail(cand.path, cand.claim_id, f"adw exited {returncode} before taking the task",
                option_by_id(lay.ir, cand.input_id))
    except tl.TaskError:
        pass  # normal case: the ADW retagged the claim with its own id


def adw_script(layer_dir: Path, name: str) -> Path:
    adws = Path(layer_dir) / "adws"
    for candidate in (adws / f"adw_{name}.py", adws / f"adw_{name.replace('-', '_')}.py"):
        if candidate.exists():
            return candidate
    return adws / f"adw_{name}.py"


def adw_argv(layer_dir: Path, name: str, source: str | None, prompt: str | None = None,
             dry_run: bool = False) -> list[str]:
    argv = ["uv", "run", "--quiet", str(adw_script(layer_dir, name))]
    if prompt:
        argv.append(prompt)
    if source:
        argv += ["--source", source]
    if dry_run:
        argv.append("--dry-run")
    return argv
