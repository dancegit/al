#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]
# ///
"""Explain a layer definition (agentic.rkt) in plain language, block by block.

  tools/explain.py LAYER_DIR_OR_RKT [--md OUT.md] [--gif OUT.gif] [--name NAME] [--subst KEY=VALUE …]

The explanation is derived, not hand-written: the file is compiled (so every default the author
left out is known), split into its top-level blocks, and each block is described from the
compiled result. `--md` writes a walkthrough with the source of each block; `--gif` writes an
animation that steps through the file with the current block highlighted and explained.
`--subst` fills placeholders in template files (e.g. @FORGE_HOST@=https://forge.example.org).
Also available as `al explain`.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUPED = {"prompt-input", "trigger", "environment", "artefact", "review", "agent", "command", "hop", "expert", "service"}
CLAUSE = re.compile(r"^  \(([a-z][a-z-]*)\b")
INCLUDED_CLAUSE = re.compile(r"^\(([a-z][a-z-]*)\b")       # an included file's clauses start in column 0
INCLUDE = re.compile(r'\(include\s+"([^"]+)"\s*\)')
# what kind of work a kit template does, in plain words (for "planning and review use MODEL")
WORK = {"plan-feature": "planning", "plan-bug": "planning", "plan-chore": "planning", "plan-patch": "patch plans",
        "plan-layer-change": "planning", "run-plan": "building", "implement": "building", "build": "building",
        "review": "review", "resolve-test": "fixing failed checks", "classify": "classifying the work",
        "commit": "commits", "document": "documentation", "pull-request": "pull requests", "branch-name": "branch names",
        "prime": "reading the codebase", "summarize": "summaries", "question": "answering questions",
        "e2e-test": "browser tests", "test": "tests", "install": "installing", "start": "starting the app",
        "self-improve": "expert notes", "track-kpis": "KPIs", "meta-command": "new prompts", "meta-adw": "new workflows",
        "meta-expert": "new experts", "plan-build-improve": "building"}


def compile_ir(source: Path) -> dict:
    cp = subprocess.run([str(ROOT / "tools" / "al"), "compile", str(source)], capture_output=True, text=True)
    if cp.returncode != 0:
        sys.exit(f"explain: {source} does not compile:\n{cp.stderr.strip()}")
    return json.loads(cp.stdout)


def split_blocks(lines: list[str], clause: re.Pattern = CLAUSE, comment: str = "  ;") -> list[dict]:
    """Top-level clauses of (agentic-layer …) with their leading comments; consecutive clauses of
    the same family form one block. Line numbers are 1-based and inclusive."""
    starts = [(i, m.group(1)) for i, line in enumerate(lines) if (m := clause.match(line))]
    if not starts:
        return []
    raw = []
    for n, (i, head) in enumerate(starts):
        begin = i
        while begin > 0 and lines[begin - 1].startswith(comment):
            begin -= 1
        end = (starts[n + 1][0] if n + 1 < len(starts) else len(lines)) - 1
        while end > i and (not lines[end].strip() or lines[end].startswith(comment)):
            end -= 1
        raw.append({"head": head, "start": begin + 1, "end": end + 1, "first": lines[i]})
    blocks = [{"head": "header", "start": 1, "end": raw[0]["start"] - 1, "first": ""}]
    for item in raw:
        last = blocks[-1]
        if item["head"] in GROUPED and last["head"] == item["head"]:
            last["end"] = item["end"]
        else:
            blocks.append(dict(item))
    return [b for b in blocks if b["end"] >= b["start"]]


def with_included(blocks: list[dict], base: Path) -> list[dict]:
    """After each top-level (include "FILE") block, the blocks of FILE (an included file's clauses
    start in column 0); includes inside (codebase …) are folded into the codebase description."""
    out = []
    for b in blocks:
        if b["head"] != "include":
            out.append(b)
            continue
        m = INCLUDE.search(b["first"])
        target = m.group(1) if m else "?"
        out.append({**b, "target": target})
        path = base / target
        if m and path.is_file():
            inc_lines = path.read_text().splitlines()
            for ib in split_blocks(inc_lines, INCLUDED_CLAUSE, ";"):
                if ib["head"] != "header":
                    out.append({**ib, "file": target, "lines": inc_lines})
    return out


def listing(items: list[str], last: str = "and") -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {last} " + items[-1]


def by_id(ir: dict, key: str) -> dict:
    return {o["id"]: o for o in ir.get(key) or []}


def describe_step(step: dict, ir: dict | None = None) -> str:
    kind = step["kind"]
    if kind == "script":
        cmd = next((c for c in (ir or {}).get("codebase", {}).get("commands", []) if c["name"] == step["cmd"]), None)
        given = f" with {listing(['`' + a + '`' for a in step.get('args', [])])}" if step.get("args") else ""
        return (f"script `{step['name']}`: runs the codebase's own `{step['cmd']}` command"
                + (f" (`{cmd['run']}`)" if cmd else "") + given
                + "; if it fails, the run stops (it is not a check: nothing fixes it and it proves nothing to the gate)")
    if kind == "run":
        only = "" if step.get("when", "always") == "always" else f" (only for {step['when'].removeprefix('class-')} work)"
        tier = f", on the {step['modelSet']} model tier" if step.get("modelSet", "inherit") != "inherit" else ""
        agent = f", by agent `{step['agent']}`" if step.get("agent") else ""
        return f"agent step `{step['name']}` using the `{step['command']}` prompt{tier}{agent}{only}"
    if kind == "check":
        fix = (f"; on failure the `{step['resolve']}` prompt fixes it, up to {step['retries']} tries" if step.get("resolve") else "")
        return f"check `{step['name']}`: run the `{step['cmd']}` command{fix}"
    if kind == "fan-out":
        return f"fan-out `{step['name']}`: {step['count']} runs of `{step['adw']}` race, pick {step['pick']}"
    if kind == "review":
        return "review" + (" (blockers go to a patch plan and a fix, then it is reviewed again)" if step.get("patch") else "")
    return "ship"


def where_it_runs(adw: dict, ir: dict) -> str:
    """Where a workflow's agent and its checks run, in plain words (the rule of the runtime's
    EnvStack: the first podman option of the stack decides, else a Nix shell wraps both)."""
    environments = by_id(ir, "environments")
    stack = [environments[i] for i in adw["environment"]]
    kinds = [o["kind"] for o in stack]
    box = next((o for o in stack if o["kind"] == "podman"), None)
    if box and box["agentInside"]:
        return "The agent runs inside the container; checks run inside the container."
    if box:
        return ("The agent runs on your machine, outside the container, with your own access "
                "(`#:agent-inside #f`); checks run inside the container.")
    if "worktree" not in kinds:
        return ("The agent runs on your machine, in the checkout itself; a workflow without a private copy "
                "has no checks.")
    shell = ", inside the Nix shell" if "nix-devshell" in kinds else ""
    return f"The agent and the checks run on your machine{shell}, in a private copy of the repository."


def explain(block: dict, ir: dict) -> tuple[str, str, list[str]]:
    """(heading, one-paragraph explanation, extra bullet points)."""
    head, cb = block["head"], ir["codebase"]
    if head == "header":
        return ("The file", f"One definition for the `{ir['name']}` codebase. An LLM wrote it from the codebase's "
                "description; the compiler checked it; the generator turns it into prompts, workflows and a sandbox.", [])
    if head == "codebase":
        cmds = cb["commands"]
        prot = cb["paths"]["protected"]
        text = (f"What the codebase is ({listing(cb['languages'])}) and the commands that prove things about it: "
                f"{listing(['`' + c['name'] + '`' for c in cmds])}. Agents show that something works by running these, "
                "never by saying so.")
        if prot:
            text += f" {len(prot)} protected path{'s' if len(prot) != 1 else ''}: agents may not ship changes there without a person."
        bullets = [f"`{c['name']}` runs `{c['run']}`" + (f" — {c['description']}" if c["description"] else "") for c in cmds]
        if prot:
            bullets.append("Protected: " + listing([f"`{p}`" for p in prot]))
        bullets += [f"Read `{d['path']}` {d['when'][0].lower() + d['when'][1:]}" for d in cb["conditionalDocs"]]
        return "The codebase and its commands", text, bullets
    if head == "prompt-input":
        names = {"cli": "the command line", "issue": "issues on the forge", "tasklist": "a plain-text task file",
                 "spec-file": "spec files dropped in a folder", "voice": "spoken requests", "webhook-payload": "a webhook payload"}
        return ("Where work comes from", "Prompt input: " + listing([names.get(o["kind"], o["kind"]) for o in ir["promptInputs"]], "or") + ".",
                [f"`{o['id']}`: {names.get(o['kind'], o['kind'])}" for o in ir["promptInputs"]])
    if head == "trigger":
        names = {"manual": "by hand", "cron": "a poller", "webhook": "a webhook from the forge", "forge-actions": "the forge's CI",
                 "file-watch": "a watcher on the task file", "voice": "a push-to-talk loop"}
        return "What starts a run", "Trigger: " + listing([names.get(o["kind"], o["kind"]) for o in ir["triggers"]], "or") + ".", []
    if head == "environment":
        parts, bullets = [], []
        for o in ir["environments"]:
            k = o["kind"]
            if k == "worktree":
                parts.append("a private copy of the repository for every run")
                bullets.append("`worktree`: each run gets its own git worktree; your checkout is never touched")
            elif k == "nix-devshell":
                parts.append("a Nix shell with the toolchain")
                bullets.append("`nix-devshell`: provides " + listing(o["packages"]))
            elif k == "podman":
                inside = "the agent itself runs inside it" if o["agentInside"] else "the checks run inside it"
                parts.append(f"a container ({inside})")
                extra = []
                if o["caches"]:
                    extra.append("caches for " + listing(o["caches"]))
                if o["services"]:
                    extra.append("its own " + listing(o["services"]) + " per run")
                if o["publish"]:
                    extra.append("port " + listing([str(p) for p in o["publish"]]) + " published to the host")
                bullets.append(f"`podman`: image based on `{o['baseImage']}`; {inside}" + ("; " + "; ".join(extra) if extra else ""))
            elif k == "home-manager":
                parts.append("a Home Manager module that installs the tools")
                bullets.append("`home-manager`: provides " + listing(o["packages"]))
            else:
                parts.append("the checkout itself, read-only")
        return "Where agents work", "Environment: " + listing(parts) + ".", bullets
    if head == "artefact":
        names = {"log": "command logs", "junit": "a test report", "coverage": "a coverage report", "image": "screenshots",
                 "video": "a recorded video, cut into frames for reviewers", "trace": "browser traces", "html": "HTML reports",
                 "audio": "audio", "file": "files"}
        return ("What counts as proof", "Artefacts collected after the checks and shown to whoever reviews: "
                + listing([names.get(o["kind"], o["kind"]) for o in ir["artefacts"]]) + ".",
                [f"`{o['id']}`: {names.get(o['kind'], o['kind'])}" + (f" from `{o['glob']}`" if o.get("glob") else "") for o in ir["artefacts"]])
    if head == "review":
        bullets, kinds = [], []
        for o in ir["reviews"]:
            body = f"; the description opens with the output of the `{o['bodyCmd']}` command" if o.get("bodyCmd") else ""
            if o["kind"] == "human":
                kinds.append(f"a person, through {for_review(ir)}")
                opening = "open a pull request" if has_forge(ir) else "keep the work on a branch, write `PR.md`"
                bullets.append(f"`{o['id']}`: {opening} and stop; a person reads it and merges{body}")
            elif o["kind"] == "agent":
                kinds.append("an agent that reviews on evidence, with a person merging")
                bullets.append(f"`{o['id']}`: {o['reviewers']} reviewer agent(s) judge the change against the plan; a person merges")
            else:
                kinds.append("reviewer agents plus hard rules, with no person")
                rules = [f"the commands {listing(['`' + c + '`' for c in o['requireChecks']])} pass again on the final result",
                         f"the work is a {listing(o['allowedClasses'], 'or')}", f"the change is at most {o['maxDiffLines']} lines"]
                if o["blockProtectedPaths"]:
                    rules.append("no protected path was touched")
                bullets.append(f"`{o['id']}`: {o['reviewers']} reviewer agent(s) must approve, and then every rule must hold: "
                               + "; ".join(rules) + f". If so it ships by `{o['ship']}` ({ship_words(ir, o['ship'])}); "
                               f"otherwise a person decides, through {for_review(ir)}")
        return "Who decides it is done", "Review: " + listing(kinds, "or") + ".", bullets
    if head == "agent":
        return models_text(ir)
    if head == "budget":
        return budget_text(ir)
    if head == "include":
        return ("More of the definition, from another file", f"These clauses are in `{block.get('target', '?')}`"
                + (", written by `al adopt` from the codebase's existing agent setup" if "adopted/" in block.get("target", "") else "")
                + "; they are explained below with the file's own blocks.", [])
    if head == "forge":
        f = ir["forge"]
        return ("The forge", "No forge: results stay as local branches." if f["kind"] == "none"
                else f"Issues and pull requests live on {f['kind']} (`{f['repo']}`).", [])
    if head == "storage":
        s = ir["storage"]
        return "Where proof is kept", ("Artefacts are kept in the layer folder." if s["kind"] == "local"
                                       else f"Artefacts are uploaded to the S3 bucket `{s['bucket']}` and kept locally too."), []
    if head == "concurrency":
        c = ir["concurrency"]
        return ("How much runs at once", f"At most {c['maxRuns']} runs, {c['maxAgents']} agent calls and {c['maxChecks']} checks at the same time, "
                "shared by everything that can start a run.", [])
    if head == "service":
        return ("Services each run gets", "Every run starts its own " + listing([f"`{o['id']}` ({o['image']})" for o in ir["services"]])
                + ", so parallel runs never share state.", [])
    if head == "dashboard":
        return dashboard_text(ir)
    if head == "voice":
        return "Voice", "Speech in and out, both running locally (Kokoro for speech, Whisper for recognition).", []
    if head == "layer":
        placement = ir.get("layer") or {}
        if placement.get("kind") == "in-repo":
            return ("Where the layer lives", f"The layer is the `{placement['dir']}/` directory of the repository itself and is "
                    "committed with the code. Runs keep their state in ignored directories, and an ordinary workflow that "
                    "changes the layer goes to a person.", [])
        where = {"central": "a folder under a central directory of layers", "separate-repo": "its own repository"}
        return ("Where the layer lives", f"The generated layer is {where.get(placement.get('kind'), 'a folder beside the repository')}, "
                "not inside the application.", [])
    if head == "command":
        cmds = ir["commands"]
        focus = [f for c in cmds for f in c["focus"]]
        text = (f"{len(cmds)} prompts written for this codebase, each for one kind of job: "
                + listing(["`" + c["name"] + "`" for c in cmds]) + ".")
        if focus:
            text += (f" {len(focus)} of their instructions are specific to this codebase"
                     + ("; the first 6:" if len(focus) > 6 else ":"))
        bullets = [describe_command(c) for c in cmds]
        bullets += [f"Codebase-specific rule: “{f}”" for f in focus[:6]]
        return "The prompts", text, bullets
    if head == "hop":
        return ("Prompts that take a prompt", "Higher-order prompts: " + listing([f"`{h['name']}` (takes a {h['takes']})" for h in ir["hops"]])
                + ". For example, one implements whatever plan it is given.", [])
    if head == "expert":
        return ("Agent experts", "Areas where an agent keeps its own notes and updates them after each change: "
                + listing([f"`{e['name']}` ({e['domain']})" for e in ir["experts"]]) + ".", [])
    if head == "adw":
        name = re.search(r"\(adw ([\w.-]+)", block["first"]).group(1)
        adw = next(a for a in ir["adws"] if a["name"] == name)
        pi, tr, en, rv = by_id(ir, "promptInputs"), by_id(ir, "triggers"), by_id(ir, "environments"), by_id(ir, "reviews")
        review = rv[adw["review"]]
        reviewed = "the pull request" if has_forge(ir) else "the branch and its description file (`PR.md`)"
        outcome = {"human": f"A person reviews {reviewed}.", "agent": f"An agent reviews on evidence; a person merges ({reviewed}).",
                   "auto": f"No person is involved if every rule holds; it ships by {ship_words(ir, review.get('ship', 'pr'))}."}[review["kind"]]
        if not any(s.get("kind") == "ship" for s in adw["steps"]):
            outcome = "No ship step: nothing is reviewed or published; the result stays with the run."
        steps = [s for s in adw["steps"]]
        short = " → ".join(s["name"] for s in steps)
        text = (f"The `{name}` workflow" + (f": {adw['description'].rstrip('.')}" if adw["description"] else "") + f". Steps: {short}. {outcome} {where_it_runs(adw, ir)}")
        if adw.get("changes") == "layer":
            text += " It changes the agentic layer itself, so a person always reviews it."
        bullets = [f"Work from {listing([pi[i]['kind'] for i in adw['input']], 'or')}; started by {listing([tr[i]['kind'] for i in adw['trigger']], 'or')}; "
                   f"runs in {listing([en[i]['kind'] for i in adw['environment']])}; agent `{adw['agent']}`"]
        bullets += [f"{n}. {describe_step(s, ir)}" for n, s in enumerate(steps, 1)]
        if review.get("agent") and review["kind"] in ("agent", "auto"):
            bullets.append(f"The reviewers run on agent `{review['agent']}`, not on the agent that did the work")
        return f"Workflow: {name}", text, bullets
    return head, f"`({head} …)`", []


def has_forge(ir: dict) -> bool:
    return (ir.get("forge") or {}).get("kind", "none") != "none"


def for_review(ir: dict) -> str:
    """What a person reviews: a pull request on the forge, else a branch and PR.md."""
    return "a pull request" if has_forge(ir) else "a branch and a description file (`PR.md`)"


def ship_words(ir: dict, ship: str) -> str:
    if ship == "pr":
        return for_review(ir)
    merged = "merging the pull request" if has_forge(ir) else "merging into the default branch (a remote, or locally)"
    return merged + (", then deploying" if ship == "deploy" else "")


def describe_command(c: dict) -> str:
    if c["template"] == "custom":
        desc = c.get("description") or ""
        if desc.startswith("adopted from "):
            origin = desc.removeprefix("adopted from ").split(":", 1)[0]
            return (f"`{c['name']}`: a prompt the team already had, adopted unchanged from `{origin}` "
                    f"(copied to `{c['file']}`)")
        return f"`{c['name']}`: a hand-written prompt (`{c['file']}`)"
    return (f"`{c['name']}`: the {c['template']} template"
            + (f", applying the Fabric pattern {listing(c['fabric'])}" if c["fabric"] else ""))


def tiers_of(agent: dict) -> list[dict]:
    if isinstance(agent.get("models"), list):
        return agent["models"]
    if "modelBase" in agent or "modelHeavy" in agent:   # compiled before named tiers
        return [{"tier": "base", "model": agent.get("modelBase", ""), "effort": "default"},
                {"tier": "heavy", "model": agent.get("modelHeavy", ""), "effort": "default"}]
    return []


def models_text(ir: dict) -> tuple[str, str, list[str]]:
    """Which agents work, and which model does which kind of work, in plain words."""
    agents = ir["agents"]
    first = agents[0]
    work: dict[str, list[str]] = {}
    for c in ir["commands"] + ir["hops"]:
        words = WORK.get(c["template"], f"the `{c['name']}` prompt") if c["template"] != "custom" else f"the `{c['name']}` prompt"
        bucket = work.setdefault(c["modelSet"], [])
        if words not in bucket:
            bucket.append(words)
    tiers = {t["tier"]: t for t in tiers_of(first)}
    sentences = []
    for tier, words in work.items():
        model = tiers.get(tier, {}).get("model") or ("whatever model the CLI defaults to" if tier in tiers else None)
        if model is None:
            continue
        effort = tiers[tier].get("effort", "default")
        verb = "uses" if len(words) == 1 else "use"
        sentences.append(f"{listing(words)} {verb} {model}" + (f" with {effort} reasoning effort" if effort != "default" else ""))
    text = "Agent: " + listing([f"{o['kind']}" + (f" (`{o['id']}`)" if o["id"] != o["kind"] else "") for o in agents]) + "."
    if sentences:
        text += (f" On `{first['id']}`, " if len(agents) > 1 else " ") + "; ".join(sentences) + "."
        if len(agents) == 1:
            text = text.replace(". " + sentences[0][0], ". " + sentences[0][0].upper(), 1)
    bullets = []
    for o in agents:
        shown = tiers_of(o)
        line = f"`{o['id']}`: {o['kind']}"
        if shown:
            line += ", model tiers " + listing([f"{t['tier']} = `{t['model'] or 'the CLI default'}`"
                                               + (f" (effort {t['effort']})" if t.get("effort", "default") != "default" else "")
                                               for t in shown])
        elif o["kind"] == "mock":
            line += ", a stand-in that runs no model"
        if o.get("permissionMode"):
            line += f", permission mode `{o['permissionMode']}`"
        bullets.append(line)
    bullets.append("For one run, `--model-set TIER`, `--model TIER=MODEL_ID` (or AL_MODEL_SET / AL_MODELS) change this "
                   "without editing the file")
    return "Which agents and models do the work", text, bullets


def budget_text(ir: dict) -> tuple[str, str, list[str]]:
    b = ir.get("budget")
    if not isinstance(b, dict):
        return ("What it may cost", "No spending limits. `--max-cost USD` (or AL_MAX_COST) still limits a single run.", [])
    parts = [f"${b[key]:g} {label}" for key, label in (("runUsd", "per run"), ("stepUsd", "per agent call"),
                                                        ("dayUsd", "a day for the whole layer")) if b.get(key) is not None]
    text = (f"At most {listing(parts) or 'no limit'}. A run that goes over is stopped after the agent call that crossed "
            "the limit and handed to a person, with the reason. Once the day's limit is reached, nothing new starts.")
    return ("What it may cost", text, ["Costs are what the agents report; codex and custom agents report none and count as $0",
                                       "`--max-cost USD` replaces the per-run limit for one run"])


def dashboard_text(ir: dict) -> tuple[str, str, list[str]]:
    """The (dashboard …) block, or the default a layer gets without one."""
    d = ir.get("dashboard") or {"kind": "local", "port": 8765, "bind": "127.0.0.1", "also": [], "pollMs": 750,
                                 "systemd": False, "tokenEnv": ""}
    if d["kind"] == "none":
        return "Watching the work", "No dashboard is generated.", []
    host = d["bind"] if d["kind"] == "local" else "127.0.0.1"
    host = f"[{host}]" if ":" in host else host
    text = f"A live dashboard on http://{host}:{d['port']} shows what the agents are doing"
    text += (", from its own locked-down container." if d["kind"] == "podman" else ".")
    placement = ir.get("layer") or {}
    prefix = f"{placement['dir']}/" if placement.get("kind") == "in-repo" else ""
    bullets = [f"Start it with `uv run {prefix}adws/dashboard.py`; it only reads the layer's run files and never changes anything"]
    if d["also"]:
        bullets.append("The same page also watches " + listing([f"`{x}`" for x in d["also"]]))
    if d["kind"] == "podman":
        bullets.append("The container sees the layers read-only, has no network route out and publishes only on 127.0.0.1"
                       + ("; it can also read container status through your podman socket, which gives it full control of podman"
                          if d.get("podmanSocket") else ""))
    if d.get("tokenEnv"):
        bullets.append(f"Every request needs the token in the environment variable `{d['tokenEnv']}`")
    if d.get("systemd"):
        bullets.append("A systemd user unit keeps it running")
    return "Watching the work", text, bullets


# ------------------------------------------------------------------------------------------ markdown

def markdown(name: str, source_label: str, lines: list[str], blocks: list[dict], ir: dict, gif: str | None) -> str:
    out = [f"# The `{name}` layer, explained", "",
           f"Source: `{source_label}` ({len(lines)} lines). This walkthrough is generated by `al explain` from the file "
           "and its compiled form, so it also states the defaults the author did not write down.", ""]
    if gif:
        out += [f"![The {name} layer, block by block]({gif})", ""]
    adws = ir["adws"]
    out += ["## In one paragraph", "",
            f"The `{name}` layer: {(ir['description'] or 'an agentic layer').rstrip('.')}. It declares {len(ir['codebase']['commands'])} codebase commands, "
            f"{len(ir['commands'])} prompts and {len(adws)} workflow{'s' if len(adws) != 1 else ''} "
            f"({listing(['`' + a['name'] + '`' for a in adws])}).", "", "## Block by block", ""]
    for b in blocks:
        heading, text, bullets = explain(b, ir)
        where = f"`{b['file']}` lines" if b.get("file") else "Lines"
        out += [f"### {heading}", "", f"{where} {b['start']}–{b['end']}. {text}", ""]
        out += [f"- {x}" for x in bullets] + ([""] if bullets else [])
        out += ["```racket", *b.get("lines", lines)[b["start"] - 1:b["end"]], "```", ""]
    for head, describe in (("agent", models_text), ("dashboard", dashboard_text)):
        if not any(b["head"] == head for b in blocks):
            heading, text, bullets = describe(ir)
            out += [f"### {heading} (not written down: the default)", "", text, ""]
            out += [f"- {x}" for x in bullets] + ([""] if bullets else [])
    return "\n".join(out)


# ------------------------------------------------------------------------------------------ gif

def render_gif(name: str, lines: list[str], blocks: list[dict], ir: dict, out: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    font_file = subprocess.run(["fc-match", "-f", "%{file}", "monospace"], capture_output=True, text=True, check=True).stdout
    bold_file = subprocess.run(["fc-match", "-f", "%{file}", "monospace:bold"], capture_output=True, text=True, check=True).stdout
    font, bold, big = ImageFont.truetype(font_file, 14), ImageFont.truetype(bold_file, 14), ImageFont.truetype(bold_file, 18)
    cw, lh, rows, cols, pad, gutter = int(font.getlength("M")), 19, 30, 112, 14, 44
    cap_h = 150
    width, height = pad * 2 + gutter + cw * cols, 34 + pad + lh * rows + cap_h
    bg, fg, dim, blue, green, yellow, band = "#0d1117", "#c9d1d9", "#6e7681", "#58a6ff", "#7ee787", "#d29922", "#1c2b41"
    token = re.compile(r'("(?:[^"\\]|\\.)*"?)|(;.*$)|(#:[\w-]+)|(\((?:[a-z][\w-]*))')

    def draw_line(d, x: int, y: int, text: str, faded: bool) -> None:
        pos = 0
        for m in token.finditer(text[:cols]):
            if m.start() > pos:
                d.text((x + pos * cw, y), text[pos:m.start()], font=font, fill=dim if faded else fg)
            colour = green if m.group(1) else dim if m.group(2) else yellow if m.group(3) else blue
            d.text((x + m.start() * cw, y), m.group(0), font=font, fill=dim if faded else colour)
            pos = m.end()
        if pos < len(text[:cols]):
            d.text((x + pos * cw, y), text[pos:cols], font=font, fill=dim if faded else fg)

    frames, durations = [], []
    source_lines = lines
    for b in blocks:
        heading, text, _ = explain(b, ir)
        lines = b.get("lines", source_lines)   # a block from an included file shows that file
        size = b["end"] - b["start"] + 1
        top = max(1, min(b["start"] - max(0, (rows - size) // 2), max(1, len(lines) - rows + 1)))
        top = min(top, b["start"])
        img = Image.new("RGB", (width, height), bg)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, width, 30], fill="#161b22")
        d.text((14, 7), f"{name} — {b.get('file') or 'agentic.rkt'}", font=bold, fill=dim)
        d.text((width - 14, 7), f"lines {b['start']}–{b['end']} of {len(lines)}", font=font, fill=dim, anchor="ra")
        y0 = 34 + pad // 2
        first, last = max(b["start"], top), min(b["end"], top + rows - 1)
        d.rectangle([0, y0 + (first - top) * lh - 1, width, y0 + (last - top + 1) * lh], fill=band)
        d.rectangle([0, y0 + (first - top) * lh - 1, 4, y0 + (last - top + 1) * lh], fill=blue)
        for row in range(rows):
            number = top + row
            if number > len(lines):
                break
            inside = b["start"] <= number <= b["end"]
            d.text((pad + gutter - 10, y0 + row * lh), str(number), font=font, fill=fg if inside else "#30363d", anchor="ra")
            draw_line(d, pad + gutter, y0 + row * lh, lines[number - 1].rstrip("\n"), not inside)
        cy = height - cap_h
        d.rectangle([0, cy, width, height], fill="#1f3a5f")
        d.rectangle([0, cy, 6, height], fill=blue)
        d.text((pad + 8, cy + 12), heading, font=big, fill="#ffffff")
        plain = text.replace("`", "")
        for i, part in enumerate(textwrap.wrap(plain, 104)[:5]):
            d.text((pad + 8, cy + 44 + i * 20), part, font=font, fill="#e6edf3")
        frames.append(img.quantize(colors=20, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
        durations.append(int(min(11000, 3200 + 38 * len(plain))))
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("layer", type=Path)
    ap.add_argument("--md", type=Path)
    ap.add_argument("--gif", type=Path)
    ap.add_argument("--name")
    ap.add_argument("--subst", action="append", default=[], metavar="KEY=VALUE")
    a = ap.parse_args()
    source = a.layer if a.layer.suffix in (".rkt", ".in") else a.layer / "agentic.rkt"
    text = source.read_text()
    for pair in a.subst:
        key, _, value = pair.partition("=")
        text = text.replace(key, value)
    included = {rel: source.parent / rel for rel in INCLUDE.findall(text)}
    with tempfile.TemporaryDirectory() as tmp:
        compiled = Path(tmp) / "agentic.rkt"
        compiled.write_text(text)
        for rel, path in included.items():  # (include "FILE") is relative to the definition
            if path.is_file():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, Path(tmp) / rel)
        ir = compile_ir(compiled)
    lines = text.splitlines()
    blocks = with_included(split_blocks(lines), source.parent)
    name = a.name or ir["name"]
    try:
        label = str(source.resolve().relative_to(ROOT))
    except ValueError:
        label = source.name
    if a.gif:
        a.gif.parent.mkdir(parents=True, exist_ok=True)
        render_gif(name, lines, blocks, ir, a.gif)
    if a.md:
        a.md.parent.mkdir(parents=True, exist_ok=True)
        gif_ref = a.gif.name if a.gif and a.gif.parent.resolve() == a.md.parent.resolve() else (str(a.gif) if a.gif else None)
        a.md.write_text(markdown(name, label, lines, blocks, ir, gif_ref))
    if not a.md and not a.gif:
        for b in blocks:
            heading, body, bullets = explain(b, ir)
            print(f"\n{heading}  (lines {b['start']}–{b['end']})\n  " + "\n  ".join(textwrap.wrap(body.replace('`', ''), 100)))
            for x in bullets:
                print("    - " + x.replace("`", ""))
        if not any(b["head"] == "agent" for b in blocks):
            heading, body, bullets = models_text(ir)
            print(f"\n{heading}  (not written down: the default)\n  " + "\n  ".join(textwrap.wrap(body.replace('`', ''), 100)))
            for x in bullets:
                print("    - " + x.replace("`", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
