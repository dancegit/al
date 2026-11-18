"""`al runs LAYER` and `al settle LAYER RUN`: the dashboard's runs list on the command line.

The list opens on what needs someone (open hand-overs, waiting runs, failures nothing replaces),
each with a one-line reason and the next action; --view running|landed|settled|all shows the others
with what landed or settled them. The work is the dashboard's own (kit/dashboard/dashboard.py,
loaded by tools/al dashboard_kit_module()): RunModel builds the summaries, Landing checks read-only
whether a handed-over run's work is on its target branch (git, never a fetch), and attention() puts
every run in a bucket. --forge also asks the layer's forge whether a run's pull request is merged,
through the layer's own runtime (adw_modules/forge.py); without it no network is used.

`al settle` writes agents/<id>/settlement.json (nothing else of the run): exit 0 written, 2 bad use,
3 refused (the run is running, or --by names neither a run nor a commit), 4 no such run.

`al runs LAYER prune --landed --older-than DAYS [--dry-run]` removes what finished runs leave behind,
for runs in the landed or settled bucket that ended more than DAYS ago: the worktree, the podman
container (only with podman on PATH) and the local branch. It never deletes the target branch, a
stage branch, the default branch or a branch checked out in another worktree, and it keeps
agents/<id> and artefacts/<id>; what it removed is noted in the run's settlement under "pruned".
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

VIEWS = ("needs-you", "running", "landed", "settled", "all")
SECONDS_PER_DAY = 86400
AL: Any = None   # the tools/al module (set by main): dashboard_kit_module, app_root_of, git, AlError
SHOWN = ""       # the layer as the person typed it, for the commands the list suggests


def kit() -> Any:
    return AL.dashboard_kit_module()


def _load(layer_dir: Path) -> Any:
    """The layer as the dashboard sees it (a dashboard.Layer); AlError when it is not a compiled layer."""
    d = kit()
    if not (layer_dir / d.LAYER_FILE).is_file():
        raise AL.AlError(f"{layer_dir} has no {d.LAYER_FILE}: run `al gen {layer_dir}` first")
    layer = d.load_layer(layer_dir, set())
    if layer is None:
        raise AL.AlError(f"cannot read {layer_dir / d.LAYER_FILE}")
    return layer


def summaries(layer: Any) -> list[dict[str, Any]]:
    d = kit()
    aggs = d.AggCache()
    out = []
    for entry in sorted((layer.path / "agents").iterdir()) if (layer.path / "agents").is_dir() else []:
        if entry.is_dir() and d.RUN_ID.match(entry.name):
            out.append(d.RunModel(layer, entry.name, aggs).summary())
    return out


def _forge_pr_state(layer_dir: Path) -> Callable[[dict[str, Any], Any], dict[str, Any]] | None:
    """pr_state(row, number) through the layer's own runtime (its forge client and credentials)."""
    if not (layer_dir / "adws/adw_modules/forge.py").is_file():
        raise AL.AlError(f"{layer_dir} is not a generated layer: run `al gen {layer_dir}` first")
    for name in [n for n in sys.modules if n == "adw_modules" or n.startswith("adw_modules.")]:
        del sys.modules[name]   # another layer's runtime may have been imported before
    sys.path.insert(0, str(layer_dir / "adws"))
    try:
        from adw_modules import forge, ir as ir_module
    except ImportError as exc:
        raise AL.AlError(f"cannot import the layer runtime from {layer_dir / 'adws'}: {exc}") from exc
    client = forge.get_client(ir_module.load(layer_dir))
    if not hasattr(client, "pr_state"):
        raise AL.AlError(f"{layer_dir} was generated before `al runs --forge`: run `al gen {layer_dir}`")

    def ask(_row: dict[str, Any], number: Any) -> dict[str, Any]:
        try:
            return dict(client.pr_state(int(number)))
        except Exception as exc:  # noqa: BLE001 — the forge is optional: the local result stands
            return {"state": "unknown", "error": str(exc)[:200]}
    return ask


def rows_of(layer: Any, forge: bool = False) -> list[dict[str, Any]]:
    """Every run of the layer with its attention (bucket, reason, next, landing, …), newest first."""
    d = kit()
    runs = summaries(layer)
    ask = _forge_pr_state(layer.path) if forge else None
    attention, _pending = d.layer_attention(layer, runs, d.Landing(), limit=None, pr_state=ask)
    rows = [{**run, **attention.get(run["key"], {})} for run in runs]
    return sorted(rows, key=lambda r: (-(r.get("started") or 0), r["id"]))


# ---------------------------------------------------------------- the list

def _day(epoch: Any) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%d") if isinstance(epoch, (int, float)) else ""


def _clip(text: str, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def why(row: dict[str, Any]) -> str:
    """What the row says after its title: the reason, or what landed or settled the run."""
    landing = row.get("landing") or {}
    if row.get("bucket") == "landed" and row.get("landed_by"):   # settled by an integration run (al integrate)
        return f"landed by {row['landed_by']} (integration)"
    if row.get("bucket") == "landed" and landing.get("state") == "landed":
        when = _day(landing.get("at"))
        return f"landed in {landing.get('commit') or '?'} ({landing.get('how')}) on {landing.get('target')}" + \
            (f", {when}" if when else "")
    return str(row.get("reason") or "")


def next_action(layer_dir: Path, row: dict[str, Any]) -> str:
    """The next action, as a command where there is one."""
    layer_dir = SHOWN or layer_dir
    nxt = str(row.get("next") or "")
    if nxt == "Start again":
        adw = str(row.get("adw") or "")
        target = f"/{adw}" if adw else "/NAME"   # a single-command run's adw is command:NAME, which al run takes too
        return f"al run {layer_dir} {target} --replaces {row['id']} < {layer_dir}/agents/{row['id']}/request.md"
    if nxt.endswith("or `al integrate`, or `al settle`"):
        nxt = nxt.replace("`al integrate`", f"al integrate {layer_dir} {row['id']}")
    if nxt.endswith("or `al settle`"):
        return nxt.replace("`al settle`", f"al settle {layer_dir} {row['id']} --as …")
    return nxt


def format_row(row: dict[str, Any], layer_dir: Path | None = None) -> str:
    """One line: id, status, workflow, request, why; then `→ next` when something is to be done."""
    line = f"{row['id']}  {str(row.get('status') or ''):<11}  {_clip(row.get('adw') or '', 14):<14}  " \
           f"{_clip(row.get('title') or row.get('problem') or '', 38):<38}  {why(row)}"
    nxt = next_action(layer_dir, row) if layer_dir is not None else str(row.get("next") or "")
    return line + (f"\n          → {nxt}" if nxt and row.get("bucket") == "needs-you" else "")


def header(rows: list[dict[str, Any]], view: str) -> str:
    count = {bucket: sum(1 for r in rows if r.get("bucket") == bucket) for bucket in ("needs-you", "running", "landed", "settled")}
    rest = f"{count['running']} running, {count['landed']} landed, {count['settled']} settled"
    if view == "needs-you":
        return f"{count['needs-you']} need you ({rest}; --view all for every run)"
    shown = len(rows) if view == "all" else sum(1 for r in rows if r.get("bucket") == view)
    return f"{shown} {view if view != 'all' else 'runs'} ({count['needs-you']} need you, {rest})"


def list_runs(layer_dir: Path, view: str = "needs-you", as_json: bool = False, forge: bool = False) -> int:
    layer = _load(layer_dir)
    rows = rows_of(layer, forge)
    chosen = rows if view == "all" else [r for r in rows if r.get("bucket") == view]
    if as_json:
        keep = ("id", "status", "adw", "title", "started", "ended", "branch", "target_branch", "replaces", "bucket",
                "reason", "next", "landing", "settled", "replaced_by", "superseded_by", "group", "integration",
                "landed_by")
        print(json.dumps([{k: r.get(k) for k in keep} for r in chosen], indent=1, sort_keys=True))
        return 0
    print(header(rows, view))
    shown: set[str] = set()
    for row in chosen:
        group = row.get("group") if view == "needs-you" else None
        if group:
            if group["id"] in shown:
                continue
            shown.add(group["id"])
            print(f"{group['count']} runs: {group['step']}, same reason")
            print(f"          {' '.join(group['runs'])}")
            print(f"          {_clip(group.get('reason') or '', 100)}")
            shown_dir = SHOWN or layer_dir
            print(f"          → start each again: al run {shown_dir} /{row.get('adw')} --replaces ID "
                  f"< {shown_dir}/agents/ID/request.md")
            continue
        print(format_row(row, layer_dir))
    return 0


# ---------------------------------------------------------------- settle

def settle(layer_dir: Path, run: str, as_: str, by: str = "", note: str = "", who: str = "") -> int:
    d = kit()
    layer = _load(layer_dir)
    if not d.RUN_ID.match(run or "") or not (layer.path / "agents" / run / "state.json").is_file():
        print(f"al settle: {layer.name} has no run {run}", file=sys.stderr)
        return 4
    repo = d.app_repo(layer.path, layer.ir)
    if not who:
        who = (AL.git(repo, "config", "user.name") if repo is not None and repo.is_dir() else "") or "al settle"
    status = str(d.RunModel(layer, run, d.AggCache()).summary().get("status") or "unknown")
    try:
        record = d.write_settlement(layer.path, run, as_=as_, by=by or "", note=note or "", who=who, via="cli",
                                    status=status, repo=repo)
    except d.SettleError as exc:
        print(f"al settle: {exc}", file=sys.stderr)
        return {"running": 3, "by": 3, "missing": 4}.get(exc.kind, 2)
    print(f"settled {run} as {record['as']}" + (f" ({record['by']})" if record["by"] else "")
          + f" by {record['who']}: agents/{run}/{d.SETTLEMENT_FILE}")
    return 0


# ---------------------------------------------------------------- prune

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    run = AL.run   # tools/al's funnel: git is the one tools/al_tools.py finds
    return run(["git", "-C", str(repo), *args], check=False)


def worktree_dir(layer: Any) -> str:
    for env in layer.ir.get("environments") or []:
        if isinstance(env, dict) and env.get("kind") == "worktree" and env.get("dir"):
            return str(env["dir"])
    return "trees"


def protected_branch(layer: Any, row: dict[str, Any], name: str) -> str:
    """Why `name` must not be deleted ("" when it may)."""
    codebase = layer.ir.get("codebase") if isinstance(layer.ir.get("codebase"), dict) else {}
    if name in {str(codebase.get("defaultBranch") or "main"), "main", "master"}:
        return "the default branch"
    if name == str(row.get("target_branch") or ""):
        return "the run's target branch"
    for stage in layer.ir.get("stages") or []:
        pattern = str((stage or {}).get("branch") or "") if isinstance(stage, dict) else ""
        if pattern and re.fullmatch(re.sub(r"\\\{[^}]*\\\}", ".+", re.escape(pattern)), name):
            return "a stage branch"
    return ""


def checked_out(repo: Path, branch: str, except_path: Path | None) -> str:
    """The worktree (other than `except_path`) that has `branch` checked out, else ""."""
    listed = _git(repo, "worktree", "list", "--porcelain")
    path = ""
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            if except_path is None or Path(path).resolve() != except_path.resolve():
                return path
    return ""


def prune(layer_dir: Path, older_than_days: float, dry_run: bool = False) -> int:
    d = kit()
    layer = _load(layer_dir)
    repo = d.app_repo(layer.path, layer.ir)
    if repo is None or not (repo / ".git").exists():
        raise AL.AlError(f"no application repository for {layer.name}: {repo}")
    limit = time.time() - older_than_days * SECONDS_PER_DAY
    podman = shutil.which("podman")
    verb = "would remove" if dry_run else "removed"
    pruned = 0
    for row in rows_of(layer):
        if row.get("bucket") not in ("landed", "settled") or not row.get("ended") or row["ended"] > limit:
            continue
        if (row.get("settled") or {}).get("pruned"):
            continue
        did: dict[str, Any] = {"at": d.utc_stamp(), "branch": "", "tip": "", "worktree": "", "container": ""}
        lines: list[str] = []
        tree = layer.path / worktree_dir(layer) / row["id"]
        if tree.exists():
            if not dry_run:
                done = _git(repo, "worktree", "remove", "--force", str(tree))
                if done.returncode != 0:
                    lines.append(f"{row['id']}: kept worktree {tree.relative_to(layer.path)}: {done.stderr.strip()[:200]}")
                    tree = None
            if tree is not None:
                did["worktree"] = str(tree.relative_to(layer.path))
                lines.append(f"{verb} worktree {did['worktree']}")
        container = f"al-{layer.name}-{row['id']}"
        if podman and subprocess.run([podman, "container", "exists", container], capture_output=True,
                                     timeout=30, check=False).returncode == 0:
            if dry_run or subprocess.run([podman, "rm", "-f", container], capture_output=True, timeout=60,
                                         check=False).returncode == 0:
                did["container"] = container
                lines.append(f"{verb} container {container}")
        branch = str(row.get("branch") or "")
        tip = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").stdout.strip() if branch else ""
        if tip:
            keep = protected_branch(layer, row, branch) or (
                f"checked out in {checked_out(repo, branch, layer.path / worktree_dir(layer) / row['id'])}"
                if checked_out(repo, branch, layer.path / worktree_dir(layer) / row["id"]) else "")
            if keep:
                lines.append(f"{row['id']}: kept branch {branch} ({keep})")
            elif dry_run or _git(repo, "branch", "-D", branch).returncode == 0:
                did.update(branch=branch, tip=tip)
                lines.append(f"{verb} branch {branch} (was {tip[:7]})")
        if not (did["worktree"] or did["container"] or did["branch"]):
            for line in lines:
                print(line)
            continue
        pruned += 1
        print(f"{row['id']}  {row['bucket']}: {why(row)}")
        for line in lines:
            print(f"  {line}")
        if not dry_run:
            d.add_pruned(layer.path, row["id"], did, row.get("landing"))
    if not dry_run and pruned:
        _git(repo, "worktree", "prune")
    print(f"{'would prune' if dry_run else 'pruned'} {pruned} run{'' if pruned == 1 else 's'}; "
          f"agents/<id> and artefacts/<id> are kept")
    return 0


# ---------------------------------------------------------------- tools/al

def main(a: Any, al: Any) -> int:
    global AL, SHOWN
    AL, SHOWN = al, str(a.layer)
    layer_dir = a.layer.resolve()
    if a.cmd == "settle":
        return settle(layer_dir, a.run, a.as_, a.by or "", a.note or "", a.who or "")
    if a.action == "prune":
        if not a.landed or a.older_than is None:
            print("usage: al runs LAYER prune --landed --older-than DAYS [--dry-run]", file=sys.stderr)
            return 2
        return prune(layer_dir, a.older_than, a.dry_run)
    return list_runs(layer_dir, a.view, a.json, a.forge)
