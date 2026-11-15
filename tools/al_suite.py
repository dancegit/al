"""A hand-built ADW suite in a repository: detect it as one unit (`al scout`) for `al adopt`.

Some repositories already have agentic workflows of their own: executable scripts at the top of
adws/ (or adw/) that share a runtime package beside them, run prompts from .claude/commands/,
keep their own run journal outside git and use conventions such as "exit 75 = waiting, run me
again with --resume". `detect_suite` describes such a suite; `render_suite` turns that description
into the `### Existing ADW suite` part of `al scout`'s draft, from the same dict, so the text and
`al scout --json` cannot drift.

Everything is read statically from tracked files: Python with `ast.parse` (a text scan when it does
not parse), shell with a text scan, JSON with `json`. Nothing from the scanned repository is ever
imported or executed. Every fact says how it was found (`how`: file, ast, docstring, comment, text
scan, or "inferred from prose"); what cannot be determined is listed under `unknown`, not guessed.
Standard library only.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

ROOTS = ("adws", "adw")
SCRIPT_SUFFIXES = (".py", ".sh", ".bash")
PROMPT_DIR = ".claude/commands/"
JOURNAL_CONSTANT = re.compile(r"^(?:[A-Z]+_)*(?:JOURNAL|RUNS?|STATE|WORK|AGENTS?)_(?:ROOT|DIR|PATH|FOLDER)$")
JOURNAL_FILE = re.compile(r"^(?:state|events|prompt|output|transcript|result|raw_output)[\w-]*\.(?:json|jsonl|md|txt|log)$")
WAIT_FLAGS = ("--no-wait", "--wait")
WAITING = 75
WAITING_MEANING = "waiting: run again with --resume (convention, inferred)"
BLOCKS = ("usage", "commands", "subcommands", "steps", "exit codes")


# ------------------------------------------------------------------------------------------ reading

def _read(app: Path, rel: str) -> str:
    try:
        return (app / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _front_matter(text: str) -> dict[str, str]:
    """`key: value` lines of a markdown file's front matter (flat; enough for `schema:` and friends)."""
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    data = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            data[key.strip()] = value.strip().strip("'\"")
    return data


def docstring_parts(doc: str) -> dict[str, Any]:
    """{title, usage: [line], blocks: {name: [line]}} of a docstring (or a script's comment text).
    A block is a line `Name:` (Usage, Commands, Subcommands, Steps, Exit codes) and the indented or
    non-blank lines after it, up to a blank line; `Usage: X` on one line counts as a usage line."""
    lines = (doc or "").strip("\n").splitlines()
    title = lines[0].strip() if lines and not re.match(r"(?i)^\s*(?:%s)\s*:" % "|".join(BLOCKS), lines[0]) else ""
    blocks: dict[str, list[str]] = {}
    current = None
    for line in lines:
        head = re.match(r"(?i)^\s*(%s)\s*:\s*(.*)$" % "|".join(BLOCKS), line)
        if head:
            current = head.group(1).lower()
            blocks.setdefault(current, [])
            if head.group(2).strip():
                blocks[current].append(head.group(2).strip())
            continue
        if not line.strip():
            if current and blocks.get(current):
                current = None
            continue
        if current:
            blocks[current].append(line.strip())
    usage = blocks.pop("usage", [])
    return {"title": title, "usage": usage, "blocks": blocks}


def _usage_tokens(line: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(positional arguments, options) of one usage line without its program (or subcommand) name."""
    positionals: list[dict[str, Any]] = []
    options: dict[str, Any] = {}
    groups = re.findall(r"\[([^\[\]]*)\]", line)
    rest = re.sub(r"\[[^\[\]]*\]", " ", line)

    def take(tokens: list[str], optional: bool) -> None:
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.startswith("-"):
                value = tokens[i + 1] if i + 1 < len(tokens) and re.fullmatch(r"[A-Z][A-Z0-9_-]*", tokens[i + 1]) else ""
                options[token] = value
                i += 2 if value else 1
                continue
            if re.fullmatch(r"[A-Z][A-Z0-9_-]*(?:\.\.\.)?", token):
                positionals.append({"name": token.rstrip("."), "optional": optional})
            i += 1
    for group in groups:
        take(group.split(), True)
    take(rest.split(), False)
    return positionals, options


def _options(usage_lines: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """options {adw_id, resume, wait} and the positionals of the first usage line that has any."""
    found: dict[str, Any] = {}
    positionals: list[dict[str, Any]] = []
    for line in usage_lines:
        pos, opts = _usage_tokens(line)
        found.update({k: v for k, v in opts.items() if k not in found or v})
        if pos and not positionals:
            positionals = pos
    resume = None
    if "--resume" in found:
        resume = "with-id" if found["--resume"] else "flag"
    return ({"adw_id": "--adw-id" in found, "resume": resume, "wait": [f for f in WAIT_FLAGS if f in found]},
            positionals)


# ------------------------------------------------------------------------------------------ one script

def python_facts(text: str) -> dict[str, Any]:
    """What a Python script says about itself, read with `ast` (a text scan when it does not parse):
    docstring, imports (first components), string literals, module int/str constants, exit codes
    of sys.exit/exit/raise SystemExit, argparse options and subparsers."""
    facts: dict[str, Any] = {"doc": "", "imports": [], "strings": [], "constants": {}, "exits": [],
                             "argparse": {}, "subparsers": [], "functions": [], "parse_error": "", "how": "ast"}
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        facts.update(parse_error=f"line {exc.lineno}: {exc.msg}", how="text scan")
        m = re.search(r'^(?:#[^\n]*\n|\s*\n)*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', text, re.S)
        facts["doc"] = m.group(1) if m else ""
        facts["imports"] = sorted({m.group(2).split(".")[0] for m in re.finditer(r"^\s*(from|import)\s+([\w.]+)", text, re.M)})
        facts["strings"] = sorted(set(re.findall(r"[\"']([^\"'\n]{1,200})[\"']", text)))
        facts["exits"] = sorted({int(c) for c in re.findall(r"\b(?:sys\.)?exit\(\s*(\d+)\s*\)", text)})
        return facts
    facts["doc"] = ast.get_docstring(tree) or ""
    imports, strings = set(), set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, str)) \
                and not isinstance(node.value.value, bool):
            facts["constants"][node.targets[0].id] = node.value.value
    exits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) <= 200:
                strings.add(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            facts["functions"].append(node.name)
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in ("sys.exit", "exit", "SystemExit", "os._exit") and node.args:
                code = _int_value(node.args[0], facts["constants"])
                if code is not None:
                    exits.add(code)
            elif name.endswith("add_argument") and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) and node.args[0].value.startswith("--"):
                keywords = {k.arg: k.value for k in node.keywords}
                action = keywords.get("action")
                takes = not (isinstance(action, ast.Constant) and str(action.value).startswith("store_"))
                facts["argparse"][node.args[0].value] = takes
            elif name.endswith("add_parser") and node.args and isinstance(node.args[0], ast.Constant):
                help_kw = next((k.value for k in node.keywords if k.arg in ("help", "description")), None)
                facts["subparsers"].append({"name": str(node.args[0].value),
                                            "description": help_kw.value if isinstance(help_kw, ast.Constant) else ""})
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and _call_name(node.exc.func) == "SystemExit" and node.exc.args:
            code = _int_value(node.exc.args[0], facts["constants"])
            if code is not None:
                exits.add(code)
    facts.update(imports=sorted(imports), strings=sorted(strings), exits=sorted(exits))
    return facts


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_call_name(func.value)}.{func.attr}"
    return ""


def _int_value(node: ast.AST, constants: dict[str, Any]) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and isinstance(constants.get(node.id), int):
        return constants[node.id]
    return None


def shell_facts(text: str) -> dict[str, Any]:
    """A shell script's leading comment text (its "docstring") and its `exit N` codes, by a text scan."""
    comment = []
    for line in text.splitlines():
        if line.startswith("#!"):
            continue
        if line.startswith("#"):
            comment.append(line[1:].strip())
            continue
        if line.strip():
            break
    exits = sorted({int(c) for c in re.findall(r"\bexit\s+(\d+)\b", text)})
    return {"doc": "\n".join(comment), "imports": [], "strings": [], "constants": {}, "exits": exits,
            "argparse": {}, "subparsers": [], "functions": [], "parse_error": "", "how": "text scan"}


# ------------------------------------------------------------------------------------------ the suite

def routing(app: Path, root: str, files: list[str]) -> tuple[dict[str, Any] | None, list[str]]:
    """The suite's model-routing file ({path, tiers, default, steps, problems, how}) and unknowns."""
    candidates = [f for f in files if f.startswith(root + "/") and f.endswith(".json")
                  and re.search(r"rout|models?[^/]*\.json$", f.rsplit("/", 1)[-1])]
    if not candidates:
        return None, [f"model routing: no routing file found in {root}/"]
    path = sorted(candidates)[0]
    info: dict[str, Any] = {"path": path, "tiers": {}, "default": "", "steps": {}, "problems": [], "how": "file + json"}
    try:
        data = json.loads(_read(app, path))
    except ValueError as exc:
        info["problems"].append(f"does not parse as JSON ({exc})")
        return info, [f"model routing: {path} does not parse as JSON"]
    if not isinstance(data, dict):
        info["problems"].append("is not a JSON object")
        return info, [f"model routing: {path} is not a JSON object"]
    tiers = data.get("tiers") if isinstance(data.get("tiers"), dict) else {}
    info["tiers"] = {str(k): str(v) for k, v in sorted(tiers.items())}
    info["default"] = str(data.get("default") or "")
    steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
    info["steps"] = {str(k): str(v) for k, v in sorted(steps.items())}
    for step, tier in info["steps"].items():
        if info["tiers"] and tier not in info["tiers"]:
            info["problems"].append(f"step {step} names tier {tier}, which is not among the tiers")
    if info["default"] and info["tiers"] and info["default"] not in info["tiers"]:
        info["problems"].append(f"the default tier {info['default']} is not among the tiers")
    return info, []


def units(app: Path, files: list[str], targets: list[str]) -> list[dict[str, Any]]:
    """Tracked systemd units whose ExecStart names a suite script or wrapper, and the timers of those."""
    found: list[dict[str, Any]] = []
    services = []
    for rel in sorted(f for f in files if f.endswith(".service")):
        execs = [line.split("=", 1)[1].strip() for line in _read(app, rel).splitlines()
                 if line.strip().startswith("ExecStart=")]
        calls = sorted({t for t in targets for e in execs if t in e})
        if calls:
            found.append({"path": rel, "kind": "service", "exec": "; ".join(execs), "calls": calls,
                          "how": "file: ExecStart= (text scan)"})
            services.append(rel)
    for rel in sorted(f for f in files if f.endswith(".timer")):
        text = _read(app, rel)
        unit = re.search(r"^\s*Unit=(\S+)", text, re.M)
        service = next((s for s in services if s.rsplit("/", 1)[-1] == (unit.group(1) if unit else
                        rel.rsplit("/", 1)[-1][:-len(".timer")] + ".service")), "")
        if service:
            schedule = re.search(r"^\s*(On\w+)=(.+)$", text, re.M)
            found.append({"path": rel, "kind": "timer", "exec": f"starts {service}"
                          + (f" ({schedule.group(1)}={schedule.group(2).strip()})" if schedule else ""),
                          "calls": [service], "how": "file: timer of a unit above (text scan)"})
    return found


def detect_suite(app: Path, files: list[str]) -> dict[str, Any] | None:
    """The hand-built ADW suite among the tracked `files` of `app`, or None. A suite exists when at
    least two top-level Python scripts of adws/ (or adw/) import the same package beside them."""
    app = Path(app)
    files = sorted(f for f in files if f)
    for root in ROOTS:
        suite = _detect_in(app, files, root)
        if suite is not None:
            return suite
    return None


def _detect_in(app: Path, files: list[str], root: str) -> dict[str, Any] | None:
    candidates = [f for f in files if re.fullmatch(rf"{root}/[^/]+", f) and f.endswith(SCRIPT_SUFFIXES)
                  and not f.endswith("/__init__.py")]
    packages = {f.split("/")[1] for f in files if f.startswith(root + "/") and f.count("/") >= 2}
    facts = {}
    for rel in candidates:
        text = _read(app, rel)
        facts[rel] = python_facts(text) if rel.endswith(".py") else shell_facts(text)
        facts[rel]["runner"] = (("uv" if "# /// script" in text else "python3") if rel.endswith(".py")
                                else "bash" if rel.endswith(".bash") or text.startswith("#!/bin/bash")
                                or "bash" in text.split("\n", 1)[0] else "sh")
    importers: dict[str, list[str]] = {}
    for rel, f in facts.items():
        for name in f["imports"]:
            if name in packages:
                importers.setdefault(name, []).append(rel)
    shared = sorted(((len(v), k) for k, v in importers.items() if len(v) >= 2), reverse=True)
    if not shared:
        return None
    package = shared[0][1]
    package_dir = f"{root}/{package}/"
    unknown: list[str] = []

    # the runtime package
    modules = sorted(f for f in files if f.startswith(package_dir) and f.endswith(".py"))
    module_facts = {rel: python_facts(_read(app, rel)) for rel in modules}
    guards = []
    for rel, f in module_facts.items():
        stem = rel.rsplit("/", 1)[-1][:-3]
        named = [fn for fn in f["functions"] if fn.startswith("guard")]
        if stem.startswith("guard"):
            guards += [f"{rel}:{fn}" for fn in named] or [rel]
        else:
            guards += [f"{rel}:{fn}" for fn in named]
    runtime = {"path": package_dir, "modules": [m[len(package_dir):] for m in modules], "guards": guards,
               "imported_by": sorted(importers[package]),
               "how": f"ast: imported by {len(importers[package])} scripts of {root}/"}

    # prompts the suite can run
    prompt_files = {f[len(PROMPT_DIR):-3]: f for f in files if f.startswith(PROMPT_DIR) and f.endswith(".md")}
    by_stem = {}
    for name, rel in sorted(prompt_files.items()):
        by_stem.setdefault(name, rel)
        by_stem.setdefault(name.rsplit("/", 1)[-1], rel)

    scripts = [_script(app, files, rel, facts[rel], package, by_stem, root) for rel in candidates]
    for s in scripts:
        if s["parse_error"]:
            unknown.append(f"{s['path']}: does not parse as Python ({s['parse_error']}); its facts come from a text scan")
        if not s["title"] and not s["usage"]:
            unknown.append(f"{s['path']}: no docstring or leading comment, so its purpose and usage are unknown")

    # the entry script and its subcommands
    entry = None
    for s, rel in zip(scripts, candidates):
        parts = docstring_parts(facts[rel]["doc"])
        block = parts["blocks"].get("commands") or parts["blocks"].get("subcommands")
        subs = []
        how = ""
        if block and parts["usage"]:
            how = "docstring: Usage and Commands blocks"
            for line in block:
                m = re.match(r"^([a-z][\w-]*)\s*(.*?)(?:\s{2,}(.*))?$", line)
                if m:
                    subs.append({"name": m.group(1), "arguments": m.group(2).strip(), "description": (m.group(3) or "").strip()})
        elif facts[rel]["subparsers"]:
            how = "ast: argparse add_subparsers"
            subs = [{"name": p["name"], "arguments": "", "description": p["description"]} for p in facts[rel]["subparsers"]]
        if subs:
            stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            for sub in subs:
                target = next((c for c in candidates if c != rel and
                               c.rsplit("/", 1)[-1].rsplit(".", 1)[0].endswith("_" + sub["name"].replace("-", "_"))), "")
                sub["script"] = target
                options, positionals = _options([sub["arguments"]])
                sub["options"], sub["positionals"] = options, positionals
            entry = {"path": rel, "stem": stem, "runner": s["runner"], "usage": parts["usage"], "subcommands": subs, "how": how}
            break
    if entry is None:
        unknown.append(f"entry script: no script in {root}/ lists subcommands (a `Commands:` block or argparse subparsers)")

    # the journal
    journal = None
    for rel, f in [*module_facts.items(), *[(c, facts[c]) for c in candidates]]:
        for name, value in f["constants"].items():
            if isinstance(value, str) and JOURNAL_CONSTANT.match(name) and not value.startswith("/"):
                journal = {"root": value.rstrip("/"), "constant": name, "module": rel, "files": [],
                           "how": f"ast: module constant {name} in {rel}"}
                break
        if journal:
            break
    names = sorted({v for f in [*module_facts.values(), *facts.values()] for v in f["strings"] if JOURNAL_FILE.match(v)})
    if journal is None:
        unknown.append("journal root: no module constant names the folder the suite writes its run journal to")
    else:
        journal["files"] = names
        ignore = _read(app, ".gitignore").splitlines()
        top = journal["root"].split("/")[0]
        journal["outside_git"] = any(line.strip().rstrip("/") in (top, "/" + top, journal["root"]) for line in ignore)

    route, route_unknown = routing(app, root, files)
    unknown += route_unknown

    # wrappers and units
    script_paths = [*candidates]
    wrappers = []
    for rel in sorted(f for f in files if f.startswith(f"{root}/bin/")):
        text = _read(app, rel)
        calls = sorted({p for p in script_paths if p in text})
        wrappers.append({"path": rel, "calls": calls, "how": "text scan"})
    found_units = units(app, files, script_paths + [w["path"] for w in wrappers])

    return {"root": root + "/", "how": f"ast: {len(importers[package])} scripts of {root}/ import {package_dir}",
            "runtime_package": runtime, "entry": entry, "journal": journal, "routing": route,
            "wrappers": wrappers, "units": found_units, "scripts": scripts, "unknown": unknown}


def _script(app: Path, files: list[str], rel: str, f: dict[str, Any], package: str, by_stem: dict[str, str],
            root: str) -> dict[str, Any]:
    parts = docstring_parts(f["doc"])
    doc_how = "docstring" if rel.endswith(".py") and f["how"] == "ast" else (
        "comment" if not rel.endswith(".py") else "text scan of the docstring")
    options, positionals = _options(parts["usage"])
    how = {"title": doc_how if parts["title"] else "", "usage": doc_how if parts["usage"] else "",
           "options": f"{doc_how}: usage" if parts["usage"] else ""}
    if not parts["usage"] and f["argparse"]:
        options = {"adw_id": "--adw-id" in f["argparse"],
                   "resume": ("with-id" if f["argparse"]["--resume"] else "flag") if "--resume" in f["argparse"] else None,
                   "wait": [w for w in WAIT_FLAGS if w in f["argparse"]]}
        how["options"] = "ast: argparse add_argument"
    steps = []
    for line in parts["blocks"].get("steps", []):
        m = re.match(r"^(\d+)[.)]\s*(?:\((code|llm|LLM|agent)\)\s*)?(.*)$", line)
        if not m:
            continue
        kind = {"code": "code", "llm": "llm", "agent": "llm"}.get((m.group(2) or "").lower(), "unknown")
        prompt = re.search(r"(?:^|\s)/([a-z0-9][\w-]*(?:/[\w-]+)*)", m.group(3))
        steps.append({"number": int(m.group(1)), "text": m.group(3).strip(), "kind": kind,
                      "prompt": prompt.group(1) if prompt else "", "how": doc_how})
    prompts, unresolved, seen = [], [], set()
    for value in f["strings"]:
        name = value[1:] if value.startswith("/") else value
        if name in by_stem and name not in seen:
            seen.add(name)
            prompts.append({"name": name, "file": by_stem[name], "how": f"{f['how']}: string literal {value!r}"})
    for step in steps:
        name = step["prompt"]
        if not name or name in seen:
            continue
        seen.add(name)
        if name in by_stem:
            prompts.append({"name": name, "file": by_stem[name], "how": "docstring: steps"})
        else:
            unresolved.append({"name": name, "how": "docstring: steps (no file in .claude/commands/)"})
    schemas = []
    for p in prompts:
        schema = _front_matter(_read(app, p["file"])).get("schema", "")
        if schema:
            path = f"{root}/schemas/{schema}"
            schemas.append({"prompt": p["name"], "schema": schema, "file": path,
                            "exists": path in files or (app / path).is_file(), "how": f"front matter of {p['file']}"})
    meanings = {}
    for line in parts["blocks"].get("exit codes", []):
        m = re.match(r"^(\d+)\s*[:=-]?\s*(.*)$", line)
        if m:
            meanings[int(m.group(1))] = m.group(2).strip()
    codes = []
    found_how = "ast: sys.exit/SystemExit" if f["how"] == "ast" else "text scan: exit N"
    for code in sorted(set(f["exits"]) | set(meanings)):
        sources = [found_how] if code in f["exits"] else []
        if code in meanings:
            sources.append("docstring: Exit codes")
        meaning = meanings.get(code, "")
        if code == WAITING and not meaning:
            meaning, sources = WAITING_MEANING, [*sources, "inferred from prose"]
        codes.append({"code": code, "meaning": meaning, "how": " + ".join(sources)})
    facts_unknown = []
    if not parts["title"]:
        facts_unknown.append("title")
    if not parts["usage"] and not f["argparse"]:
        facts_unknown.append("usage")
    if not codes:
        facts_unknown.append("exit codes")
    return {"path": rel, "language": "python" if rel.endswith(".py") else "shell", "runner": f["runner"],
            "imports_runtime": package in f["imports"], "title": parts["title"], "usage": parts["usage"],
            "options": options, "positionals": positionals, "steps": steps, "prompts": prompts,
            "unresolved_prompts": unresolved, "schemas": schemas, "exit_codes": codes,
            "parse_error": f["parse_error"], "how": how, "facts_unknown": facts_unknown}


# ------------------------------------------------------------------------------------------ text

def _how(text: str) -> str:
    return f" ({text})" if text else ""


def render_suite(suite: dict[str, Any]) -> str:
    """The `### Existing ADW suite` part of `## Existing agent setup`, built only from `suite`."""
    out = ["### Existing ADW suite", "",
           f"A hand-built suite of agentic workflows in `{suite['root']}`{_how(suite['how'])}. Read statically; "
           "nothing was run. `al adopt` proposes one runnable workflow per subcommand or script.", ""]
    rt = suite["runtime_package"]
    out.append(f"- runtime package: `{rt['path']}` — {', '.join(f'`{m}`' for m in rt['modules']) or 'no modules'}{_how(rt['how'])}")
    if rt["guards"]:
        out.append(f"  - guards (not managed by al): {', '.join(f'`{g}`' for g in rt['guards'])} (ast: names starting with guard)")
    entry = suite["entry"]
    if entry:
        out.append(f"- entry script: `{entry['path']}`{_how(entry['how'])}")
        out += [f"  - usage: `{u}`" for u in entry["usage"]]
        for sub in entry["subcommands"]:
            out.append(f"  - `{sub['name']}` {('`' + sub['arguments'] + '`') if sub['arguments'] else '(no arguments)'}"
                       f" — {sub['description'] or 'no description'}" + (f"; runs `{sub['script']}`" if sub["script"] else ""))
    else:
        out.append("- entry script: none found")
    journal = suite["journal"]
    if journal:
        out.append(f"- journal root: `{journal['root']}/<adw_id>/`" + (" (ignored by git)" if journal.get("outside_git") else "")
                   + f"{_how(journal['how'])}; files {', '.join(f'`{n}`' for n in journal['files']) or 'unknown'} (ast: string literals)")
    else:
        out.append("- journal root: unknown")
    route = suite["routing"]
    if route:
        tiers = ", ".join(f"`{k}` = `{v}`" for k, v in route["tiers"].items()) or "none"
        steps = ", ".join(f"`{k}` → `{v}`" for k, v in route["steps"].items()) or "none"
        out.append(f"- model routing: `{route['path']}` — tiers {tiers}; default `{route['default'] or '?'}`; steps {steps}{_how(route['how'])}")
        out += [f"  - problem: {p}" for p in route["problems"]]
    else:
        out.append("- model routing: none found")
    for w in suite["wrappers"]:
        out.append(f"- wrapper: `{w['path']}` calls {', '.join(f'`{c}`' for c in w['calls']) or 'nothing found'}{_how(w['how'])}")
    for u in suite["units"]:
        out.append(f"- systemd {u['kind']} (not managed by al): `{u['path']}` — `{u['exec']}`{_how(u['how'])}")
    out += ["", "| script | runner | title | usage | --adw-id | --resume | wait flags | exit codes |",
            "|---|---|---|---|---|---|---|---|"]
    for s in suite["scripts"]:
        codes = "; ".join(f"{c['code']}" + (f" {c['meaning']}" if c["meaning"] else "") + f" ({c['how']})" for c in s["exit_codes"])
        out.append(f"| `{s['path']}` | {s['runner']} | {s['title'] or '—'}{_how(s['how']['title'])} | "
                   f"{'<br>'.join(f'`{u}`' for u in s['usage']) or '—'}{_how(s['how']['usage'])} | "
                   f"{'yes' if s['options']['adw_id'] else 'no'} | {s['options']['resume'] or 'no'} | "
                   f"{', '.join(s['options']['wait']) or '—'} | {codes or '—'} |")
    for s in suite["scripts"]:
        if not (s["steps"] or s["prompts"] or s["unresolved_prompts"] or s["schemas"]):
            continue
        out += ["", f"`{s['path']}`:"]
        out += [f"- step {st['number']} ({st['kind']}): {st['text']}{_how(st['how'])}" for st in s["steps"]]
        out += [f"- prompt `{p['name']}` → `{p['file']}`{_how(p['how'])}" for p in s["prompts"]]
        out += [f"- prompt `{p['name']}`: UNRESOLVED{_how(p['how'])}" for p in s["unresolved_prompts"]]
        out += [f"- schema of `{sc['prompt']}`: `{sc['file']}`" + ("" if sc["exists"] else " (MISSING)") + _how(sc["how"])
                for sc in s["schemas"]]
    out += ["", "Unknown:", ""] + ([f"- {u}" for u in suite["unknown"]] or ["- nothing"])
    return "\n".join(out)
