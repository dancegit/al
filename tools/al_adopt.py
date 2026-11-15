"""Codebases that already have an agent setup: detect it (`al scout`) and bring it into a layer (`al adopt`).

Detection reads tracked files only and executes nothing. It classifies:

  rules      CLAUDE.md, AGENTS.md (also per directory), GEMINI.md, .cursorrules, .cursor/rules/**,
             .github/copilot-instructions.md, .windsurfrules
  prompts    .claude/commands/**, .codex/prompts/**, prompts/ (markdown with front matter);
             .claude/agents/** and .claude/skills/** are listed but are not prompts a layer can run
  workflows  scripts at the top of adws/ or adw/; CI workflows that run an agent; .claude/settings*.json
             (hooks, permissions, MCP servers); .mcp.json
  docs       specs/, ai_docs/, app_docs/, conditional_docs.md
  layers     a directory with an agentic.rkt (an existing al layer)

`al adopt LAYER` is deterministic and idempotent. It never touches the application. It writes:

  adopted/codebase.rkt   (conditional-doc …) per rule file and (cmd legacy-… …) per workflow script,
                         included by agentic.rkt inside (codebase …)
  adopted/layer.rkt      (command NAME #:template custom #:file …) per prompt, and per workflow script a
                         PROPOSED one-step ADW around it with human review; included at the top level
  adopted/RULES.md       every rule of every rule file with its source line, for the authoring LLM and people
  adopted/REPORT.md      what was adopted, what was not and why, and what a person must decide
  adopted/.al-adopt.json what was written (sha256), so a re-run changes nothing and a hand edit is not lost
  templates/custom/*.md  the prompts, copied with a header naming their origin

and, once, the two (include "adopted/…") lines in agentic.rkt. Why includes rather than a marked
region in agentic.rkt: the compiler resolves (include …) at expansion time with source locations
into the included file, so its errors still point at the exact clause; the author's file is
touched once (two lines) and never rewritten; and the adopted declarations live in files al owns
whole, regenerated as a unit and protected against hand edits like generated files.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

ADOPTED = "adopted"
CODEBASE_FILE = f"{ADOPTED}/codebase.rkt"
LAYER_FILE = f"{ADOPTED}/layer.rkt"
RECORD = f"{ADOPTED}/.al-adopt.json"
CUSTOM_DIR = "templates/custom"
CATEGORIES = ("rules", "prompts", "workflows")
VERSION = 1

RULE_FILES = {"CLAUDE.md": "claude", "AGENTS.md": "agents", "GEMINI.md": "gemini"}
ROOT_RULE_FILES = {".cursorrules": "cursor", ".windsurfrules": "windsurf",
                   ".github/copilot-instructions.md": "copilot"}
CI_DIRS = (".github/workflows/", ".forgejo/workflows/", ".gitea/workflows/")
AGENT_IN_CI = re.compile(r"anthropics/claude-code(?:-base)?-action|openai/codex-action|\bclaude\s+(?:-p\b|--print\b)"
                         r"|\bcodex\s+exec\b|\bnpx\s+@anthropic-ai/claude-code|\bclaude-code\b")
PROMPT_DIRS = ((".claude/commands/", "prompt", "claude"), (".codex/prompts/", "prompt", "codex"))
SCRIPT_SUFFIXES = (".py", ".sh", ".ts", ".js", ".rb", ".bash")
DOC_DIRS = ("specs", "ai_docs", "app_docs")
TEMPLATE_VARS = ("adw_id", "layer_dir", "specs_dir", "worktree", "artefacts_dir", "branch", "default_branch",
                 "class", "prompt", "plan")


# ------------------------------------------------------------------------------------------ reading

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def front_matter(text: str) -> tuple[dict[str, Any], str, int]:
    """(front matter, body, number of lines the front matter takes) of a markdown file."""
    if not text.startswith("---\n"):
        return {}, text, 0
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text, 0
    raw = text[4:end]
    after = text.find("\n", end + 4)
    body = text[after + 1:] if after >= 0 else ""
    data: dict[str, Any] = {}
    try:
        import yaml
        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            data = {str(k): v for k, v in loaded.items()}
    except Exception:  # noqa: BLE001 — front matter that does not parse is plain text
        for line in raw.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip():
                data[key.strip()] = value.strip().strip("'\"")
    return data, body, text[:after + 1].count("\n") if after >= 0 else text.count("\n")


def one_line(text: Any, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def describe(text: str) -> str:
    """A one-line description: front matter `description`, else the first heading, else the first line."""
    meta, body, _ = front_matter(text)
    if meta.get("description"):
        return one_line(meta["description"])
    for line in body.splitlines():
        if line.strip().startswith("#"):
            return one_line(line.strip().lstrip("#"))
    return next((one_line(line) for line in body.splitlines() if line.strip()), "")


def prompt_arguments(text: str) -> list[str]:
    """What a prompt expects: $ARGUMENTS, $1…$9 (in order), and its front matter's argument-hint."""
    meta, body, _ = front_matter(text)
    found = ["$ARGUMENTS"] if "$ARGUMENTS" in body else []
    found += sorted(set(re.findall(r"\$[1-9](?![0-9])", body)), key=lambda x: int(x[1:]))
    hint = meta.get("argument-hint")
    if isinstance(hint, list):  # `[pr-number]` is a YAML list
        hint = "[" + " ".join(map(str, hint)) + "]"
    if hint:
        found.append(f"hint: {one_line(hint, 80)}")
    return found


def read(app: Path, rel: str) -> str:
    try:
        return (app / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------------------------------ detection

def item(app: Path, rel: str, kind: str, category: str, **extra: Any) -> dict[str, Any]:
    path = app / rel
    text = read(app, rel) if path.is_file() else ""
    entry = {"path": rel, "kind": kind, "category": category, "size": path.stat().st_size if path.is_file() else 0,
             "description": describe(text) if rel.endswith((".md", ".mdc")) or kind == "rules" else ""}
    entry.update(extra)
    return entry


def detect(app: Path, files: list[str]) -> list[dict[str, Any]]:
    """Every piece of existing agent material among the tracked `files` of `app`, classified."""
    app = Path(app)
    found: list[dict[str, Any]] = []
    tracked = set(files)
    for rel in sorted(files):
        name = rel.rsplit("/", 1)[-1]
        directory = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if name in RULE_FILES and (name != "GEMINI.md" or not directory):
            found.append(item(app, rel, "rules", "rules", tool=RULE_FILES[name], scope=directory or "."))
        elif rel in ROOT_RULE_FILES:
            found.append(item(app, rel, "rules", "rules", tool=ROOT_RULE_FILES[rel], scope="."))
        elif rel.startswith(".cursor/rules/") and rel.endswith((".md", ".mdc")):
            meta, _body, _ = front_matter(read(app, rel))
            globs = meta.get("globs") or ""
            found.append(item(app, rel, "rules", "rules", tool="cursor", scope=".",
                              globs=globs if isinstance(globs, str) else ",".join(map(str, globs)),
                              always=bool(meta.get("alwaysApply"))))
        elif any(rel.startswith(prefix) for prefix, _k, _t in PROMPT_DIRS) and rel.endswith(".md"):
            prefix, kind, tool = next(p for p in PROMPT_DIRS if rel.startswith(p[0]))
            text = read(app, rel)
            found.append(item(app, rel, kind, "prompts", tool=tool, arguments=prompt_arguments(text),
                              name=rel[len(prefix):-3]))
        elif rel.startswith(".claude/agents/") and rel.endswith(".md"):
            found.append(item(app, rel, "agent-definition", "prompts", tool="claude"))
        elif rel.startswith(".claude/skills/") and name == "SKILL.md":
            found.append(item(app, rel, "skill", "prompts", tool="claude"))
        elif re.match(r"(?:[^/]+/)*prompts/", rel) and rel.endswith(".md") and not rel.startswith("."):
            text = read(app, rel)
            meta, _body, _ = front_matter(text)
            if meta:
                start = rel.index("prompts/") + len("prompts/")
                found.append(item(app, rel, "prompt", "prompts", tool="generic", arguments=prompt_arguments(text),
                                  name=rel[start:-3]))
        elif re.fullmatch(r"adws?/[^/]+", rel) and rel.endswith(SCRIPT_SUFFIXES) and name != "__init__.py":
            text = read(app, rel)
            runner = ("uv" if "# /// script" in text else "python3") if rel.endswith(".py") else \
                ("bash" if rel.endswith((".sh", ".bash")) else "bun" if rel.endswith((".ts", ".js")) else "")
            found.append({**item(app, rel, "workflow-script", "workflows", runner=runner),
                          "description": script_description(text)})
        elif rel.startswith(CI_DIRS) and rel.endswith((".yml", ".yaml")) or rel == ".gitlab-ci.yml":
            text = read(app, rel)
            match = AGENT_IN_CI.search(text)
            if match:
                found.append({**item(app, rel, "ci-agent", "workflows", invokes=match.group(0), events=ci_events(text)),
                              "description": f"CI workflow that runs an agent ({match.group(0)}) on {', '.join(ci_events(text)) or '?'}"})
        elif rel in (".claude/settings.json", ".claude/settings.local.json"):
            found.append({**item(app, rel, "agent-settings", "workflows"), "description": settings_summary(read(app, rel))})
        elif rel == ".mcp.json":
            found.append({**item(app, rel, "mcp", "workflows"), "description": mcp_summary(read(app, rel))})
        elif name == "conditional_docs.md":
            found.append(item(app, rel, "doc-index", "docs"))
        elif name == "agentic.rkt":
            found.append({**item(app, rel, "al-layer", "layers"), "description": f"an al layer in `{directory or '.'}/`"})
    for d in DOC_DIRS:
        inside = [f for f in tracked if f.startswith(d + "/")]
        if inside:
            size = sum((app / f).stat().st_size for f in inside if (app / f).is_file())
            found.append({"path": d + "/", "kind": "doc-cache" if d != "specs" else "plans", "category": "docs",
                          "size": size, "description": f"{len(inside)} file(s) of {'plans/specs' if d == 'specs' else 'agent documentation'}"})
    return found


def script_description(text: str) -> str:
    """The docstring's or the leading comment's first sentence of a workflow script."""
    m = re.search(r'^(?:#![^\n]*\n)?(?:#[^\n]*\n)*\s*(?:"""|\'\'\')\s*(.+?)(?:"""|\'\'\'|\n\n)', text, re.S)
    if m:
        return one_line(m.group(1))
    for line in text.splitlines():
        if line.startswith("#") and not line.startswith(("#!", "# ///", "# requires-python", "# dependencies")):
            stripped = line.lstrip("# ").strip()
            if stripped and stripped != "///":
                return one_line(stripped)
    return ""


def ci_events(text: str) -> list[str]:
    try:
        import yaml
        doc = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001
        return []
    on = doc.get("on", doc.get(True)) if isinstance(doc, dict) else None
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [str(x) for x in on]
    if isinstance(on, dict):
        return [str(k) for k in on]
    return []


def settings_summary(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return "settings (does not parse as JSON)"
    parts = []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks, dict) and hooks:
        parts.append("hooks on " + ", ".join(sorted(hooks)))
    perms = data.get("permissions") if isinstance(data, dict) else None
    if isinstance(perms, dict):
        for key in ("allow", "deny", "ask"):
            if perms.get(key):
                parts.append(f"{len(perms[key])} {key} permission(s)")
    if isinstance(data, dict) and data.get("mcpServers"):
        parts.append("MCP servers " + ", ".join(sorted(data["mcpServers"])))
    return "Claude Code settings: " + ("; ".join(parts) or "nothing that changes behaviour")


def mcp_summary(text: str) -> str:
    try:
        servers = (json.loads(text) or {}).get("mcpServers") or {}
    except (ValueError, AttributeError):
        return "MCP configuration (does not parse as JSON)"
    return "MCP servers: " + (", ".join(sorted(servers)) or "none")


# ------------------------------------------------------------------------------------------ LLM.txt section

def render_section(items: list[dict[str, Any]]) -> str:
    """The `## Existing agent setup` section of the LLM.txt draft."""
    if not items:
        return "none found (no rule files, prompt libraries, agent workflow scripts or agent CI in the repository)"
    rows = []
    for it in items:
        args = ", ".join(f"`{a}`" for a in it.get("arguments") or []) or ("—" if it["category"] == "prompts" else "")
        rows.append(f"| `{it['path']}` | {it['kind']} | {it['size']} | {it['description'] or '—'} | {args} | UNVERIFIED |")
    return ("| path | kind | bytes | description | arguments | in use? |\n|---|---|---|---|---|---|\n" + "\n".join(rows)
            + "\n\n`al adopt LAYER` brings the rule files, prompts and workflow scripts into a layer (see `meta/commands/author-layer.md`).")


# ------------------------------------------------------------------------------------------ rules

RULE_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")
FORBIDS = re.compile(r"\b(never|do not|don't|must not|mustn't|avoid|no )\b", re.I)
REQUIRES = re.compile(r"\b(always|must|should|required?|only)\b", re.I)
# mechanical conflicts: a rule that forbids X, and the kit text that instructs X
TOPICS = [
    ("committing", re.compile(r"\b(never|do not|don't|must not)\b[^.\n]*\bcommit", re.I),
     "commands/commit.md", re.compile(r"git commit")),
    ("adding tests", re.compile(r"\b(never|do not|don't|must not)\b[^.\n]*\b(add|write|create)\b[^.\n]*\btests?\b", re.I),
     "commands/plan-feature.md", re.compile(r"\btests?\b", re.I)),
    ("adding dependencies", re.compile(r"\b(never|do not|don't|must not)\b[^.\n]*\b(add|install)\b[^.\n]*\bdependenc", re.I),
     "commands/build.md", re.compile(r"package_manager|dependenc", re.I)),
    ("pushing", re.compile(r"\b(never|do not|don't|must not)\b[^.\n]*\bpush", re.I),
     "runtime: the (ship) step", None),
    ("creating branches", re.compile(r"\b(never|do not|don't|must not)\b[^.\n]*\b(create|make|open)\b[^.\n]*\bbranch", re.I),
     "runtime: the worktree environment", None),
]
RUNTIME_TEXT = {"runtime: the (ship) step": "the (ship) step publishes the run's branch and opens a pull request (or merges, with an auto review)",
                "runtime: the worktree environment": "every run creates its own branch (and git worktree) for its work"}


def rule_lines(text: str) -> list[tuple[int, str]]:
    """(line number, rule) for every bullet or numbered line, and every other line that states a
    requirement or a prohibition; front matter and headings are skipped."""
    _meta, _body, skip = front_matter(text)
    out = []
    in_code = False
    for number, line in enumerate(text.splitlines(), 1):
        if number <= skip:
            continue
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line.strip() or line.lstrip().startswith(("#", "<!--")):
            continue
        m = RULE_LINE.match(line)
        if m:
            out.append((number, one_line(m.group(1), 400)))
        elif FORBIDS.search(line) or REQUIRES.search(line):
            out.append((number, one_line(line, 400)))
    return out


def rule_condition(it: dict[str, Any]) -> str:
    tool = {"claude": "Claude Code", "agents": "agents", "gemini": "Gemini", "cursor": "Cursor", "copilot": "Copilot",
            "windsurf": "Windsurf"}.get(it.get("tool", ""), "agents")
    if it.get("globs") and not it.get("always"):
        return f"When working on files matching {it['globs']}: the team's {tool} rules for them; follow them"
    if it.get("scope", ".") not in (".", ""):
        return f"When working under {it['scope']}/: the team's rules for that directory ({it['path']}); follow them"
    return (f"Always: the team's own rules for {tool} in this repository; follow them, and where an instruction here "
            "contradicts one of them, follow the rule and say so in your report")


def conflicts(rules: dict[str, list[tuple[int, str]]], kit: Path) -> tuple[list[dict], list[dict]]:
    """(conflicts found mechanically with both texts, every other prohibiting rule for review)."""
    found, review = [], []
    for path, lines in rules.items():
        for number, rule in lines:
            matched = False
            for topic, rule_rx, where, kit_rx in TOPICS:
                if not rule_rx.search(rule):
                    continue
                matched = True
                if kit_rx is None:
                    found.append({"topic": topic, "rule": rule, "source": f"{path}:{number}",
                                  "kit": RUNTIME_TEXT[where], "kit_source": where.removeprefix("runtime: ")})
                    continue
                text = (kit / "templates" / where).read_text(encoding="utf-8") if (kit / "templates" / where).is_file() else ""
                hit = next(((n, line.strip()) for n, line in enumerate(text.splitlines(), 1) if kit_rx.search(line)), None)
                if hit:
                    found.append({"topic": topic, "rule": rule, "source": f"{path}:{number}",
                                  "kit": one_line(hit[1], 300), "kit_source": f"kit/templates/{where}:{hit[0]}"})
            if not matched and FORBIDS.search(rule):
                review.append({"rule": rule, "source": f"{path}:{number}"})
    return found, review


# ------------------------------------------------------------------------------------------ prompts

PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
UNSUPPORTED = [
    (re.compile(r"\$[1-9][0-9]+"), "positional arguments beyond $9 are not substituted"),
    (re.compile(r"!`[^`\n]+`"), "Claude Code's !`command` (run a shell command while loading the prompt) is not run: the agent sees the text"),
    (re.compile(r"(?<![\w`])@[\w./-]+\.[A-Za-z0-9]+"), "Claude Code's @file reference is not expanded: the agent sees the path and must read it"),
]


def slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-") or "prompt"


def adopt_prompt(it: dict[str, Any], app: Path, taken: set[str], kit_names: set[str]) -> dict[str, Any]:
    """The command name, the copied template's text, and what the report must say about it."""
    text = read(app, it["path"])
    meta, body, _ = front_matter(text)
    base = slug(it.get("name", Path(it["path"]).stem).replace("/", "-"))
    name, notes = base, []
    if name in kit_names or name in taken:
        why = "a kit template" if name in kit_names else "a command or hop of the layer"
        name = f"team-{base}"
        n = 2
        while name in taken or name in kit_names:
            name, n = f"team-{base}-{n}", n + 1
        notes.append(f"renamed `{base}` → `{name}`: `{base}` is {why}")
    escaped = []

    def escape(m: re.Match) -> str:
        escaped.append(m.group(0))
        return "{{open_braces}}" + m.group(1) + "}}"
    body_out = PLACEHOLDER.sub(escape, body)
    if escaped:
        notes.append(f"{len(escaped)} `{{{{…}}}}` placeholder(s) ({', '.join(sorted(set(escaped)))}) kept literally "
                     "(written as `{{open_braces}}name}}`): al fills `{{…}}` itself")
    for rx, why in UNSUPPORTED:
        hits = sorted(set(rx.findall(body)))
        if hits:
            notes.append(f"{why} ({', '.join(f'`{h}`' for h in hits[:5])})")
    used = sorted(set(re.findall(r"\$\{([A-Za-z_]+)\}", body)) & set(TEMPLATE_VARS))
    if used:
        notes.append("`${" + "}`, `${".join(used) + "}` will be replaced by the run's value")
    for key in ("allowed-tools", "model"):
        if meta.get(key):
            notes.append(f"front matter `{key}: {one_line(meta[key], 60)}` is not honoured: the layer's agent options decide")
    origin = it["path"]
    header = (f"<!-- adopted by `al adopt` from {origin} (sha256 {sha256(text)[:12]}). Change the original and run "
              "`al adopt` again; an edit here is refused by the next `al adopt`. -->\n")
    head = text[:len(text) - len(body)] if meta else ""
    return {"name": name, "file": f"{CUSTOM_DIR}/{name}.md", "text": head + header + body_out, "origin": origin,
            "description": f"adopted from {origin}: {it.get('description') or name}", "notes": notes,
            "arguments": it.get("arguments") or []}


# ------------------------------------------------------------------------------------------ DSL text

def rkt(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def legacy_cmd(it: dict[str, Any]) -> str:
    rel = it["path"]
    runner = it.get("runner")
    if runner == "uv":
        return f"uv run {rel}"
    if runner == "python3":
        return f"python3 {rel}"
    if runner == "bash":
        return f"bash {rel}"
    if runner == "bun":
        return f"bun run {rel}"
    return f"./{rel}"


def option_id(ir: dict, family: str, kind: str) -> str | None:
    return next((o["id"] for o in ir.get(family) or [] if o.get("kind") == kind), None)


# ------------------------------------------------------------------------------------------ s-expressions

def form_spans(text: str) -> dict[str, tuple[int, int]]:
    """Offsets (open, close) of the (agentic-layer …) form and of its (codebase …) sub-form, read
    with Racket's lexical rules for strings, comments and character literals."""
    spans: dict[str, tuple[int, int]] = {}
    stack: list[tuple[int, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == ";":
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        if text.startswith("#|", i):
            j = text.find("|#", i + 2)
            i = n if j < 0 else j + 2
            continue
        if text.startswith("#\\", i):
            i += 3
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if c in "([":
            head = re.match(r"[(\[]\s*([^\s()\[\]\";]+)", text[i:])
            stack.append((i, head.group(1) if head else ""))
        elif c in ")]":
            if stack:
                start, head = stack.pop()
                if head == "agentic-layer" and not stack:
                    spans["layer"] = (start, i)
                elif head == "codebase" and len(stack) == 1 and stack[0][1] == "agentic-layer":
                    spans["codebase"] = (start, i)
        i += 1
    return spans


INCLUDE_CB = f'(include "{CODEBASE_FILE}")'
INCLUDE_LAYER = f'(include "{LAYER_FILE}")'


def with_includes(text: str) -> tuple[str, list[str]]:
    """agentic.rkt with the two include lines added where missing; (new text, what was added)."""
    added = []
    spans = form_spans(text)
    if "layer" not in spans or "codebase" not in spans:
        raise ValueError("agentic.rkt has no (agentic-layer … (codebase …) …) form to add the includes to")
    has = lambda what: re.search(re.escape(what).replace(r"\ ", r"\s+"), text) is not None  # noqa: E731
    if not has(INCLUDE_LAYER):
        close = spans["layer"][1]
        text = (text[:close] + "\n\n  ;; Declarations `al adopt` brought in from the codebase's existing agent setup\n"
                f"  ;; (regenerated by `al adopt`; see {ADOPTED}/REPORT.md)\n  {INCLUDE_LAYER}" + text[close:])
        added.append(INCLUDE_LAYER)
    spans = form_spans(text)
    if not has(INCLUDE_CB):
        close = spans["codebase"][1]
        text = text[:close] + f"\n    {INCLUDE_CB}" + text[close:]
        added.append(INCLUDE_CB)
    return text, added


# ------------------------------------------------------------------------------------------ adopt

class AdoptError(Exception):
    pass


def base_ir(al: Any, layer: Path) -> dict:
    """The layer compiled without what `al adopt` wrote before (its included files read as empty),
    so the names it adopted then do not count as collisions now."""
    text = (layer / "agentic.rkt").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agentic.rkt").write_text(text, encoding="utf-8")
        for rel in re.findall(r'\(include\s+"([^"]+)"\s*\)', text):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel in (CODEBASE_FILE, LAYER_FILE) or not (layer / rel).is_file():
                target.write_text("", encoding="utf-8")
            else:
                shutil.copyfile(layer / rel, target)
        return al.compile_layer(root)


def load_record(layer: Path) -> dict:
    try:
        return json.loads((layer / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def plan(al: Any, layer: Path, app: Path, only: set[str]) -> dict[str, Any]:
    """Everything `al adopt` would write and report, computed without writing anything."""
    files = [f for f in al.git(app, "ls-files").splitlines() if f]
    items = detect(app, files)
    has_definition = (layer / "agentic.rkt").is_file()
    ir = base_ir(al, layer) if has_definition else {}
    layer_rel = ""
    try:
        layer_rel = layer.resolve().relative_to(app.resolve()).as_posix()
    except ValueError:
        pass
    items = [it for it in items if not (layer_rel and it["path"].startswith(layer_rel + "/"))]   # an in-repo layer itself
    kit_names = set(al.kit_template_names())
    taken = {c["name"] for c in ir.get("commands", []) + ir.get("hops", [])}
    cmd_names = {c["name"] for c in (ir.get("codebase") or {}).get("commands", [])}
    adw_names = {a["name"] for a in ir.get("adws", [])}
    report: dict[str, Any] = {"rules": [], "prompts": [], "workflows": [], "not_adopted": [], "conflicts": [],
                              "review_rules": [], "notes": []}
    codebase_lines, layer_lines, extra_files = [], [], {}

    rules = [it for it in items if it["category"] == "rules"]
    if "rules" in only and rules:
        digest = ["# Rules of the existing agent setup", "",
                  "Every rule `al adopt` found in the codebase's rule files, with its source line. The files stay where",
                  "they are; the layer tells every agent to read them (adopted/codebase.rkt). Carry the ones that",
                  "govern one kind of work into `#:focus` lines of the command they govern, and never contradict one.", ""]
        by_file = {}
        for it in rules:
            codebase_lines.append(f"(conditional-doc {rkt(it['path'])} {rkt(rule_condition(it))})")
            lines = rule_lines(read(app, it["path"]))
            by_file[it["path"]] = lines
            digest += [f"## `{it['path']}` ({it['tool']}, {'directory ' + it['scope'] if it['scope'] not in ('.', '') else 'whole repository'})", ""]
            digest += [f"- {rule} (`{it['path']}:{number}`)" for number, rule in lines] or ["- (no rule lines found; read the file)"]
            digest.append("")
            report["rules"].append({"path": it["path"], "rules": len(lines), "as": "conditional-doc"})
        extra_files[f"{ADOPTED}/RULES.md"] = "\n".join(digest).rstrip() + "\n"
        found, review = conflicts(by_file, al.KIT)
        report["conflicts"] += [{"kind": "rule-vs-kit", **c} for c in found]
        report["review_rules"] = review

    templates: dict[str, str] = {}
    for it in [i for i in items if i["category"] == "prompts"]:
        if it["kind"] != "prompt":
            report["not_adopted"].append({"path": it["path"], "why": (
                "a Claude Code subagent definition" if it["kind"] == "agent-definition" else "a Claude Code skill")
                + ": the layer's headless agents run in safe mode and do not load it; turn what it does into a command "
                  "or a #:focus line by hand if the layer needs it"})
            continue
        if "prompts" not in only:
            continue
        adopted = adopt_prompt(it, app, taken | set(templates), kit_names)
        taken.add(adopted["name"])
        templates[adopted["file"]] = adopted["text"]
        layer_lines.append(f"(command {adopted['name']} #:template custom #:file {rkt(adopted['file'])}\n"
                           f"         #:description {rkt(adopted['description'])})")
        report["prompts"].append({k: adopted[k] for k in ("name", "file", "origin", "notes", "arguments")})

    manual = option_id(ir, "triggers", "manual") if ir else "manual"
    human = option_id(ir, "reviews", "human") if ir else "human"
    worktree = option_id(ir, "environments", "worktree") if ir else "worktree"
    declared = []
    for it in [i for i in items if i["category"] == "workflows"]:
        if it["kind"] == "workflow-script":
            if "workflows" not in only:
                continue
            base = slug(Path(it["path"]).stem)
            name = f"legacy-{base}"
            while name in cmd_names or name in adw_names or name in taken:
                name += "-x"
            cmd_names.add(name)
            adw_names.add(name)
            if not manual and "manual" not in declared:
                declared.append("manual")
                layer_lines.append("(trigger manual #:id adopted-manual)")
            if not human and "human" not in declared:
                declared.append("human")
                layer_lines.append("(review human #:id adopted-human)")
            if not worktree and "worktree" not in declared:
                declared.append("worktree")
                layer_lines.append("(environment worktree #:id adopted-worktree)")
            run = legacy_cmd(it)
            codebase_lines.append(f"(cmd {name} {rkt(run)} #:timeout 3600\n     #:description "
                                  f"{rkt('the team workflow script ' + it['path'] + ' (adopted; not verified by al)')})")
            layer_lines.append(
                f";; PROPOSAL by `al adopt`: run the team's {it['path']} as a workflow, with a person reviewing the result.\n"
                f";; A native equivalent written by an LLM needs a person's review: ask for one with `al improve`.\n"
                f"(adw {name} #:description {rkt('PROPOSAL (al adopt): runs ' + it['path'] + '; a person reviews the result')}\n"
                f"     #:trigger ({manual or 'adopted-manual'}) #:review {human or 'adopted-human'} "
                f"#:environment ({worktree or 'adopted-worktree'})\n"
                f"  (steps (script run #:cmd {name} #:args (\"$prompt\")) (review) (ship)))")
            report["workflows"].append({"path": it["path"], "cmd": name, "run": run, "adw": name,
                                        "note": "assumed to take the request as its first argument; check its usage"})
        elif it["kind"] == "ci-agent":
            listens = [t for t in ir.get("triggers", []) if t.get("kind") in ("webhook", "forge-actions")]
            overlap = [e for e in it.get("events") or [] if e.startswith(("issue", "pull_request"))]
            report["conflicts"].append({
                "kind": "ci-agent", "source": it["path"],
                "rule": f"{it['path']} already runs an agent ({it.get('invokes')}) on {', '.join(it.get('events') or ['?'])}",
                "kit": (f"this layer also starts runs from forge events ({', '.join(t['id'] for t in listens)})" if listens and overlap
                        else "decide whether the layer's workflows replace it or run beside it"),
                "kit_source": "agentic.rkt triggers" if listens else ""})
        else:
            report["not_adopted"].append({"path": it["path"], "why": (
                it["description"] + ": not adopted; the layer runs Claude Code with --safe-mode, which loads no hooks, "
                "MCP servers or permissions from project settings. A hook that enforces a rule belongs in a (check …) "
                "step or a #:focus line")})
    for it in items:
        if it["category"] == "layers":
            report["conflicts"].append({"kind": "al-layer", "source": it["path"], "rule": it["description"],
                                        "kit": "decide which layer is the one agents use; `al adopt` does not merge layers",
                                        "kit_source": ""})
        elif it["category"] == "docs":
            report["notes"].append(f"`{it['path']}` ({it['description']}): not adopted; an authoring LLM may declare "
                                   "(conditional-doc …) for the documents that matter in a specific situation")

    head = (";; Generated by `al adopt` from the codebase's existing agent setup. Do not edit: run `al adopt` again\n"
            ";; (an edit here is refused). agentic.rkt includes this file; see adopted/REPORT.md.\n")
    outputs = {CODEBASE_FILE: head + "".join(line + "\n" for line in codebase_lines),
               LAYER_FILE: head + "".join("\n" + line + "\n" for line in layer_lines),
               **templates, **extra_files}
    outputs[f"{ADOPTED}/REPORT.md"] = render_report(report, items)
    return {"items": items, "outputs": outputs, "report": report, "has_definition": has_definition}


def render_report(report: dict[str, Any], items: list[dict[str, Any]]) -> str:
    out = ["# What `al adopt` did", "",
           f"Found {len(items)} piece(s) of existing agent material. Nothing in the application was changed.", ""]
    out += ["## Rules", ""] + ([f"- `{r['path']}`: {r['rules']} rule line(s); every prompt tells the agent to read it "
                                f"((conditional-doc …) in adopted/codebase.rkt); digest in adopted/RULES.md" for r in report["rules"]]
                               or ["- none"]) + [""]
    out += ["## Prompts adopted as commands", ""]
    for p in report["prompts"]:
        out.append(f"- `{p['origin']}` → command `{p['name']}` (`{p['file']}`)"
                   + (f"; arguments {', '.join(p['arguments'])}" if p["arguments"] else ""))
        out += [f"  - {note}" for note in p["notes"]]
    out += (["- none"] if not report["prompts"] else []) + [""]
    out += ["## Workflow scripts: PROPOSALS", "",
            "These cannot be converted automatically, and are not pretended to be. Each is declared as a codebase",
            "cmd and wrapped in a one-step ADW that a person reviews. An LLM-proposed native equivalent needs a",
            "person's review: ask for one with `al improve LAYER \"replace legacy-… with a native workflow\"`.", ""]
    out += [f"- PROPOSAL `{w['path']}` → `(cmd {w['cmd']} \"{w['run']}\")` and `(adw {w['adw']} … (steps (script run …) (review) (ship)))`; "
            f"{w['note']}" for w in report["workflows"]] or ["- none"]
    out += ["", "## For a person to decide", ""]
    for c in report["conflicts"]:
        if c["kind"] == "rule-vs-kit":
            out += [f"- **{c['topic']}**: the rule says “{c['rule']}” (`{c['source']}`), while the layer says "
                    f"“{c['kit']}” (`{c['kit_source']}`)."]
        else:
            out += [f"- {c['rule']} (`{c['source']}`): {c['kit']}."]
    if report["review_rules"]:
        out += ["- Rules that forbid something; check that no prompt or workflow of the layer does it:"]
        out += [f"  - “{r['rule']}” (`{r['source']}`)" for r in report["review_rules"]]
    if not report["conflicts"] and not report["review_rules"]:
        out.append("- nothing found")
    out += ["", "## Not adopted", ""] + ([f"- `{n['path']}`: {n['why']}" for n in report["not_adopted"]]
                                        + [f"- {note}" for note in report["notes"]] or ["- nothing"])
    return "\n".join(out).rstrip() + "\n"


def adopt(al: Any, layer: Path, app: Path | None, only: set[str], dry_run: bool, force: bool = False,
          say=print) -> int:
    layer = layer.resolve()
    if not layer.is_dir():
        raise AdoptError(f"{layer} is not a directory (create the layer with `al init` first)")
    has_definition = (layer / "agentic.rkt").is_file()
    if app is None:
        if not has_definition:
            raise AdoptError(f"{layer} has no agentic.rkt yet: say where the application is with --app APP")
        app = al.app_root_of(layer, base_ir(al, layer))
    app = app.resolve()
    if not (app / ".git").exists():
        raise AdoptError(f"{app} is not a git checkout")
    before = al.app_fingerprint(app)
    result = plan(al, layer, app, only)
    record = load_record(layer)
    written = record.get("files") or {}
    edited = [rel for rel, digest in sorted(written.items())
              if (layer / rel).is_file() and sha256((layer / rel).read_text(encoding="utf-8")) != digest]
    if edited and not force:
        raise AdoptError("these files written by `al adopt` were edited by hand since; adopting again would lose "
                         "the edit:\n  " + "\n  ".join(edited) + "\nmove the change into agentic.rkt (or the original "
                         "file in the application) and run `al adopt` again, or pass --force to overwrite")
    changed = [rel for rel, text in sorted(result["outputs"].items())
               if not (layer / rel).is_file() or (layer / rel).read_text(encoding="utf-8") != text]
    stale = [rel for rel in sorted(written) if rel not in result["outputs"] and (layer / rel).is_file()]
    definition = layer / "agentic.rkt"
    new_definition, added = "", []
    if has_definition:
        try:
            new_definition, added = with_includes(definition.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise AdoptError(str(exc)) from exc
    say(report_text(result, changed, stale, added, dry_run, has_definition))
    if dry_run:
        return 0
    old = {rel: (layer / rel).read_text(encoding="utf-8") if (layer / rel).is_file() else None
           for rel in [*changed, *stale]}
    old_definition = definition.read_text(encoding="utf-8") if has_definition else None
    for rel in changed:
        target = layer / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result["outputs"][rel], encoding="utf-8")
    for rel in stale:
        (layer / rel).unlink()
    if added:
        definition.write_text(new_definition, encoding="utf-8")
    if has_definition and (changed or stale or added):
        try:
            al.compile_layer(layer)
        except Exception as exc:  # noqa: BLE001 — put everything back: the layer must keep compiling
            for rel, text in old.items():
                if text is None:
                    (layer / rel).unlink(missing_ok=True)
                else:
                    (layer / rel).write_text(text, encoding="utf-8")
            if old_definition is not None:
                definition.write_text(old_definition, encoding="utf-8")
            raise AdoptError(f"the adopted declarations do not compile, so nothing was changed:\n{exc}") from exc
    new_record = {"version": VERSION, "app": str(app), "only": sorted(only),
                  "files": {rel: sha256(text) for rel, text in sorted(result["outputs"].items())},
                  "sources": {it["path"]: sha256(read(app, it["path"])) for it in result["items"] if (app / it["path"]).is_file()}}
    record_text = json.dumps(new_record, indent=1, sort_keys=True) + "\n"
    if not (layer / RECORD).is_file() or (layer / RECORD).read_text(encoding="utf-8") != record_text:
        (layer / RECORD).parent.mkdir(parents=True, exist_ok=True)
        (layer / RECORD).write_text(record_text, encoding="utf-8")
    if al.app_fingerprint(app) != before:
        raise AdoptError(f"THE APPLICATION CHECKOUT CHANGED during `al adopt` — {app}")
    return 0


def report_text(result: dict[str, Any], changed: list[str], stale: list[str], added: list[str], dry_run: bool,
                has_definition: bool) -> str:
    rep = result["report"]
    lines = [f"al adopt: {len(result['items'])} piece(s) of existing agent material found"
             + (" (dry run: nothing is written)" if dry_run else "")]
    lines.append(f"  rules:     {len(rep['rules'])} file(s) → (conditional-doc …) + adopted/RULES.md")
    lines.append(f"  prompts:   {len(rep['prompts'])} → commands from templates/custom/")
    for p in rep["prompts"]:
        for note in p["notes"]:
            lines.append(f"             {p['name']}: {note}")
    lines.append(f"  workflows: {len(rep['workflows'])} script(s) → PROPOSED (cmd legacy-…) + one-step ADW with human review")
    if rep["conflicts"] or rep["review_rules"]:
        lines.append(f"  decide:    {len(rep['conflicts'])} conflict(s), {len(rep['review_rules'])} prohibiting rule(s) to review "
                     "(adopted/REPORT.md, \"For a person to decide\")")
    if rep["not_adopted"]:
        lines.append(f"  skipped:   {len(rep['not_adopted'])} (adopted/REPORT.md, \"Not adopted\")")
    verb = "would write" if dry_run else "wrote"
    if changed or stale or added:
        lines.append(f"  {verb}: {', '.join(changed) or 'nothing new'}" + (f"; {'would remove' if dry_run else 'removed'}: {', '.join(stale)}" if stale else ""))
        if added:
            lines.append(f"  {'would add' if dry_run else 'added'} to agentic.rkt: {'; '.join(added)}")
    else:
        lines.append("  nothing changed: the layer already has all of it")
    if not has_definition:
        lines.append("  agentic.rkt does not exist yet: when you author it, put (include \"adopted/codebase.rkt\") inside "
                     "(codebase …) and (include \"adopted/layer.rkt\") at the top level (meta/commands/author-layer.md says so), "
                     "then run `al adopt` again: it then fits the proposals to the options the layer declares")
    elif not dry_run and (changed or added):
        lines.append("  next: al gen LAYER (and read adopted/REPORT.md)")
    return "\n".join(lines)


def check(layer: Path, app: Path) -> tuple[list[str], list[str]]:
    """`al check`: (problems, notes) about what `al adopt` wrote."""
    record = load_record(layer)
    if not record:
        return [], []
    problems, notes = [], []
    for rel in sorted(record.get("files") or {}):
        if not (layer / rel).is_file():
            problems.append(f"{rel} (written by `al adopt`) is missing: run `al adopt` again")
    text = (layer / "agentic.rkt").read_text(encoding="utf-8") if (layer / "agentic.rkt").is_file() else ""
    for include in (INCLUDE_CB, INCLUDE_LAYER):
        if (layer / include.split('"')[1]).is_file() and include.split('"')[1] not in text:
            notes.append(f"{include.split(chr(34))[1]} is not included by agentic.rkt: its declarations are not part of the layer")
    for path, digest in sorted((record.get("sources") or {}).items()):
        source = app / path
        if not source.is_file():
            problems.append(f"{path} (adopted from the application) no longer exists there: run `al adopt` again")
        elif sha256(source.read_text(encoding="utf-8", errors="replace")) != digest:
            notes.append(f"{path} changed in the application since it was adopted: run `al adopt` again to take the change")
    cb = (layer / CODEBASE_FILE).read_text(encoding="utf-8") if (layer / CODEBASE_FILE).is_file() else ""
    for path in re.findall(r'\(conditional-doc "([^"]+)"', cb):
        if not (app / path).is_file():
            problems.append(f"adopted rule file {path} is missing from the application")
    return problems, notes


def glob_match(path: str, patterns: str) -> bool:
    return any(fnmatch.fnmatch(path, p.strip()) for p in patterns.split(",") if p.strip())
