"""Which tests a change concerns: the selection behind a cmd's `#:targeted` form.

A codebase cmd may declare a narrower form (`#:targeted "pytest -q {tests}"`) and how its tests are
found (`#:select`, default `(changed-tests imports)`). The strategies, tried in this order and united
(first seen first, each test once, counted for the strategy that found it first):

- `changed-tests` (always first, listed or not): test files the change added or modified;
- `path-map`: `#:path-map` pairs, `{name}` = one file name without extension, `{dir}` = directories;
- `imports`: tests that import a changed file, transitively: Python (`ast`), JavaScript/TypeScript
  (relative specifiers), Go (packages, `./dir`) and Rust (crates of a workspace, `-p name`); any
  other language contributes nothing;
- `coverage`: the map a passing full run recorded (coverage_map.py); a file changed since the map's
  commit is unknown to it;
- `command`: the codebase's own selector (`#:select-cmd`), one test per output line;
- `learned` (always last, always on): what earlier batch misses taught (testing.learned).

Every path is kept under the cmd's `#:covers` (when it has any) and made relative to `#:test-root`
(paths outside it are dropped, with a note). A changed path under `#:covers` that is not a test and
that no strategy turned into a test or package is `unmapped`: testing.plan_check escalates on it.
Selection never decides a depth: an empty selection is the caller's to turn into smoke or full.

Imports `affected` (for `matches`), `coverage_map` and, lazily, `testing`; never `review`, `ship`
or `steps`. Standard library only.
"""
from __future__ import annotations

import ast
import fnmatch
import os
import posixpath
import re
import shlex
import subprocess
import tomllib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from . import affected, coverage_map

DEFAULT_SELECT = ("changed-tests", "imports")
STRATEGIES = ("changed-tests", "path-map", "imports", "coverage", "command", "learned")
MAX_FILES = 5000          # files an import graph reads at most
COMMAND_TIMEOUT = 300     # seconds a #:select-cmd may take
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
             "target", "dist", "build", ".next", "vendor"}
JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_JS_TEST_EXT = "{js,jsx,ts,tsx,mjs,cjs}"
TEST_PATTERNS = ("test_*.py", "*_test.py", "*_test.go", f"*.test.{_JS_TEST_EXT}", f"*.spec.{_JS_TEST_EXT}",
                 "__tests__/**", "tests/*.rs")


# ---------------------------------------------------------------- what is a test

def _expand(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]
    return [alternative for choice in match.group(1).split(",")
            for alternative in _expand(pattern[:match.start()] + choice + pattern[match.end():])]


_NAME_PATTERNS = [pattern for raw in TEST_PATTERNS if "/" not in raw for pattern in _expand(raw)]


def _test_dirs(layer: Any) -> list[str]:
    try:
        return [str(item) for item in ((layer.ir.get("codebase") or {}).get("paths") or {}).get("tests") or []]
    except AttributeError:
        return []


def is_test(path: str, layer: Any) -> bool:
    """True for a test file: a name in TEST_PATTERNS, a file under `__tests__/`, a Rust crate's
    `tests/*.rs`, or a file under one of the codebase's `paths.tests` directories."""
    path = posixpath.normpath(str(path).split("::", 1)[0])
    parts = path.split("/")
    name = parts[-1]
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _NAME_PATTERNS):
        return True
    if "__tests__" in parts[:-1]:
        return True
    if name.endswith(".rs") and len(parts) >= 2 and parts[-2] == "tests":
        return True
    dirs = _test_dirs(layer) if layer is not None else []
    return bool(dirs) and affected.matches(path, dirs)


# ---------------------------------------------------------------- the result

@dataclass
class Selection:
    tests: list[str] = field(default_factory=list)       # test files or ids, ordered, unique, relative to #:test-root
    packages: list[str] = field(default_factory=list)    # Go packages as ./dir, Rust crates by name
    files: list[str] = field(default_factory=list)       # changed files under #:covers, relative to #:test-root
    by_strategy: dict[str, int] = field(default_factory=dict)  # tests and packages newly added, per strategy, in order
    unmapped: list[str] = field(default_factory=list)    # changed non-test paths under #:covers nothing mapped
    notes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tests) + len(self.packages)


@dataclass
class Found:
    """What one strategy found: test (or package key) → the changed paths it was found for."""
    tests: dict[str, set[str]] = field(default_factory=dict)
    packages: dict[str, set[str]] = field(default_factory=dict)  # "go:DIR" or "rust:NAME"
    notes: list[str] = field(default_factory=list)

    def add_test(self, test: str, sources: Iterable[str]) -> None:
        self.tests.setdefault(test, set()).update(sources)

    def add_package(self, key: str, sources: Iterable[str]) -> None:
        self.packages.setdefault(key, set()).update(sources)

    def mapped(self) -> set[str]:
        return {path for sources in (*self.tests.values(), *self.packages.values()) for path in sources}


def _covers(cmd: dict[str, Any]) -> list[str]:
    return [str(item) for item in cmd.get("covers") or []]


def _under_covers(path: str, cmd: dict[str, Any]) -> bool:
    covers = _covers(cmd)
    return not covers or affected.matches(path, covers)


def _cmd_name(cmd: Any) -> str:
    return str(cmd.get("name", "")) if isinstance(cmd, dict) else str(cmd)


# ---------------------------------------------------------------- walking the tree

def _walk(root: Path, cmd: dict[str, Any], extensions: tuple[str, ...]) -> tuple[list[str], bool]:
    """Repository-relative files with one of `extensions` under the cmd's #:covers (the whole
    repository without covers), sorted; and whether MAX_FILES cut the list short."""
    found: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS and not name.startswith("."))
        relative_dir = Path(current).relative_to(root).as_posix()
        for name in sorted(files):
            if not name.endswith(extensions):
                continue
            relative = name if relative_dir == "." else f"{relative_dir}/{name}"
            if _under_covers(relative, cmd):
                found.append(relative)
                if len(found) >= MAX_FILES:
                    return found, True
    return found, False


def _limit_note(language: str) -> str:
    return f"imports: the {language} import graph stopped after reading {MAX_FILES} files; tests beyond it are not selected"


def _read(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _reverse_walk(edges: dict[str, set[str]], start: Iterable[str]) -> dict[str, set[str]]:
    """Breadth-first over the reverse of `edges` (file → files it imports): every node that
    reaches a start node, with the start nodes it reaches."""
    reverse: dict[str, set[str]] = {}
    for node, targets in edges.items():
        for target in targets:
            reverse.setdefault(target, set()).add(node)
    reached: dict[str, set[str]] = {}
    for origin in start:
        queue, seen = deque([origin]), {origin}
        while queue:
            node = queue.popleft()
            reached.setdefault(node, set()).add(origin)
            for importer in sorted(reverse.get(node, ())):
                if importer not in seen:
                    seen.add(importer)
                    queue.append(importer)
    return reached


# ---------------------------------------------------------------- Python

def _python_roots(files: list[str], test_root: str) -> list[str]:
    """Where absolute imports are looked up: the repository root, #:test-root, src/, and every
    directory that holds a top-level package (a package whose parent is not one)."""
    packages = {posixpath.dirname(path) for path in files if posixpath.basename(path) == "__init__.py"}
    roots = ["", test_root.strip("/"), "src"]
    for package in sorted(packages):
        parent = posixpath.dirname(package)
        if parent not in packages:
            roots.append(parent)
    return list(dict.fromkeys(roots))


def _module_file(known: set[str], base: str, dotted: str) -> str:
    stem = posixpath.join(base, *dotted.split(".")) if dotted else base
    for candidate in (f"{stem}.py", posixpath.join(stem, "__init__.py")):
        candidate = posixpath.normpath(candidate)
        if candidate in known:
            return candidate
    return ""


def _python_imports(source: str, path: str, known: set[str], roots: list[str]) -> set[str]:
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return set()
    here = posixpath.dirname(path)
    bases = list(dict.fromkeys([*roots, here]))
    result: set[str] = set()

    def absolute(dotted: str, with_parents: bool = True) -> str:
        for base in bases:
            found = _module_file(known, base, dotted)
            if found:
                if with_parents:  # importing a.b.c runs a/__init__.py and a/b/__init__.py too
                    parts = dotted.split(".")
                    for depth in range(1, len(parts)):
                        parent = _module_file(known, base, ".".join(parts[:depth]))
                        if parent:
                            result.add(parent)
                return found
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found = absolute(alias.name)
                if found:
                    result.add(found)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = here
                for _ in range(node.level - 1):
                    base = posixpath.dirname(base)
                module = node.module or ""
                package = _module_file(known, base, module) if module else _module_file(known, base, "")
                for alias in node.names:
                    sub = _module_file(known, base, f"{module}.{alias.name}" if module else alias.name)
                    if sub:
                        result.add(sub)
                    elif package:
                        result.add(package)
                if module and package:
                    result.add(package)
            else:
                module = node.module or ""
                package = absolute(module)
                for alias in node.names:
                    sub = absolute(f"{module}.{alias.name}", with_parents=False) if alias.name != "*" else ""
                    if sub:
                        result.add(sub)  # `from pkg import util`: pkg/util.py when it exists …
                    elif package:
                        result.add(package)  # … else pkg/__init__.py
    result.discard(path)
    return result


def _python_graph(root: Path, cmd: dict[str, Any], changed: list[str], layer: Any, found: Found) -> None:
    files, cut = _walk(root, cmd, (".py",))
    if cut:
        found.notes.append(_limit_note("Python"))
    known = set(files)
    roots = _python_roots(files, str(cmd.get("testRoot") or ""))
    edges = {path: _python_imports(_read(root, path), path, known, roots) for path in files}
    _tests_reaching(edges, changed, layer, found)


def _tests_reaching(edges: dict[str, set[str]], changed: list[str], layer: Any, found: Found) -> None:
    for node, origins in sorted(_reverse_walk(edges, changed).items()):
        if node not in changed and is_test(node, layer):
            found.add_test(node, origins)


# ---------------------------------------------------------------- JavaScript / TypeScript

_JS_SPECIFIERS = re.compile(
    r"""(?:\bimport\s[^'"]*?\bfrom\s*|\bexport\s[^'"]*?\bfrom\s*|\bimport\s*|\brequire\s*\(\s*|\bimport\s*\(\s*)"""
    r"""(['"])(\.{1,2}(?:/[^'"]*)?)\1""")


def _js_resolve(known: set[str], path: str, specifier: str) -> str:
    target = posixpath.normpath(posixpath.join(posixpath.dirname(path), specifier))
    candidates = [target, *(target + ext for ext in JS_EXTENSIONS),
                  *(posixpath.join(target, "index" + ext) for ext in JS_EXTENSIONS)]
    stem, ext = posixpath.splitext(target)
    if ext in (".js", ".jsx", ".mjs", ".cjs"):  # TypeScript ESM writes `./a.js` for a.ts
        candidates += [stem + ".ts", stem + ".tsx", stem + ".mts", stem + ".cts"]
    return next((candidate for candidate in candidates if candidate in known), "")


def _js_graph(root: Path, cmd: dict[str, Any], changed: list[str], layer: Any, found: Found) -> None:
    files, cut = _walk(root, cmd, JS_EXTENSIONS)
    if cut:
        found.notes.append(_limit_note("JavaScript/TypeScript"))
    known = set(files)
    edges: dict[str, set[str]] = {}
    for path in files:
        targets = {_js_resolve(known, path, match.group(2)) for match in _JS_SPECIFIERS.finditer(_read(root, path))}
        edges[path] = {target for target in targets if target and target != path}
    _tests_reaching(edges, changed, layer, found)


# ---------------------------------------------------------------- Go

_GO_IMPORT_BLOCK = re.compile(r"^import\s*\((.*?)\)", re.S | re.M)
_GO_IMPORT_LINE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
_GO_QUOTED = re.compile(r'"([^"]+)"')


def _go_modules(root: Path, files: list[str]) -> list[tuple[str, str]]:
    """(module path, directory) of every go.mod under the tree, deepest directory first."""
    modules: list[tuple[str, str]] = []
    dirs = {posixpath.dirname(path) for path in files} | {""}
    for directory in sorted(dirs, key=lambda item: (-item.count("/"), item)):
        text = _read(root, posixpath.join(directory, "go.mod") if directory else "go.mod")
        match = re.search(r"^module\s+(\S+)", text, re.M)
        if match:
            modules.append((match.group(1).strip('"'), directory))
    return modules


def _go_graph(root: Path, cmd: dict[str, Any], changed: list[str], layer: Any, found: Found) -> None:
    del layer
    files, cut = _walk(root, cmd, (".go", "go.mod"))
    if cut:
        found.notes.append(_limit_note("Go"))
    modules = _go_modules(root, files)
    go_files = [path for path in files if path.endswith(".go")]
    edges: dict[str, set[str]] = {}       # package dir → package dirs it imports
    for path in go_files:
        package = posixpath.dirname(path)
        text = _read(root, path)
        imports = set(_GO_IMPORT_LINE.findall(text))
        for block in _GO_IMPORT_BLOCK.findall(text):
            imports |= set(_GO_QUOTED.findall(block))
        targets = edges.setdefault(package, set())
        for imported in imports:
            for module, directory in modules:
                if imported == module or imported.startswith(module + "/"):
                    rest = imported[len(module):].strip("/")
                    targets.add(posixpath.normpath(posixpath.join(directory, rest)) if (directory or rest) else "")
                    break
    for package in edges:
        edges[package].discard(package)
    changed_packages: dict[str, set[str]] = {}
    for path in changed:
        if path.endswith(".go"):
            changed_packages.setdefault(posixpath.dirname(path), set()).add(path)
    if not changed_packages:
        return
    for package, origins in sorted(_reverse_walk(edges, list(changed_packages)).items()):
        sources = {path for origin in origins for path in changed_packages.get(origin, ())}
        found.add_package(f"go:{package}", sources)
        for test in sorted(path for path in go_files
                           if posixpath.dirname(path) == package and path.endswith("_test.go")):
            found.add_test(test, sources)


# ---------------------------------------------------------------- Rust

def _rust_crates(root: Path, cmd: dict[str, Any]) -> tuple[dict[str, str], dict[str, set[str]], bool]:
    """Crates (directory → package name) and their path dependencies (directory → directories)."""
    manifests, cut = _walk(root, {"covers": []}, ("Cargo.toml",))
    names: dict[str, str] = {}
    deps: dict[str, set[str]] = {}
    for manifest in manifests:
        if posixpath.basename(manifest) != "Cargo.toml":
            continue
        directory = posixpath.dirname(manifest)
        try:
            data = tomllib.loads(_read(root, manifest))
        except tomllib.TOMLDecodeError:
            continue
        package = data.get("package")
        if not isinstance(package, dict) or not package.get("name"):
            continue  # a workspace root is not a crate
        names[directory] = str(package["name"])
        for table in ("dependencies", "dev-dependencies", "build-dependencies"):
            for spec in (data.get(table) or {}).values():
                if isinstance(spec, dict) and spec.get("path"):
                    deps.setdefault(directory, set()).add(
                        posixpath.normpath(posixpath.join(directory, str(spec["path"]))))
    del cmd
    return names, deps, cut


def _rust_graph(root: Path, cmd: dict[str, Any], changed: list[str], layer: Any, found: Found) -> None:
    del layer
    names, deps, cut = _rust_crates(root, cmd)
    if cut:
        found.notes.append(_limit_note("Rust"))
    changed_crates: dict[str, set[str]] = {}
    for path in changed:
        owners = [directory for directory in names
                  if directory == "" or path == directory or path.startswith(directory + "/")]
        if owners:
            changed_crates.setdefault(max(owners, key=len), set()).add(path)
    for crate, origins in _reverse_walk(deps, list(changed_crates)).items():
        if crate in names:
            sources = {path for origin in origins for path in changed_crates.get(origin, ())}
            found.add_package(f"rust:{names[crate]}", sources)


# ---------------------------------------------------------------- the strategies

def changed_tests(changed: list[str], cmd: dict[str, Any], layer: Any, root: Path | None = None) -> Found:
    """Changed test files that still exist, under #:covers."""
    root = Path(root) if root is not None else Path(layer.app_root)
    found = Found()
    for path in changed:
        if is_test(path, layer) and _under_covers(path, cmd) and (root / path).is_file():
            found.add_test(path, [path])
    return found


def _source_regex(source: str) -> re.Pattern[str]:
    pattern, seen = "", set()
    for piece in re.split(r"(\{name\}|\{dir\})", source):
        if piece in ("{name}", "{dir}"):
            group = piece[1:-1]
            pattern += f"(?P={group})" if group in seen else (f"(?P<{group}>[^/]+)" if group == "name"
                                                               else f"(?P<{group}>.+)")
            seen.add(group)
        else:
            pattern += re.escape(piece)
    return re.compile(pattern + r"\Z")


def path_map(changed: list[str], cmd: dict[str, Any], root: Path) -> Found:
    """`#:path-map`: each source pattern matched against the changed paths, its test glob filled
    and expanded against the tree; a test file that does not exist selects nothing."""
    root, found = Path(root), Found()
    entries = [(_source_regex(str(entry["source"])), str(entry["tests"])) for entry in cmd.get("pathMap") or []
               if isinstance(entry, dict) and entry.get("source") and entry.get("tests")]
    for path in changed:
        for regex, tests in entries:
            match = regex.match(path)
            if not match:
                continue
            filled = tests
            for key, value in match.groupdict().items():
                filled = filled.replace("{" + key + "}", value)
            for hit in sorted(root.glob(filled)):
                if hit.is_file():
                    found.add_test(hit.relative_to(root).as_posix(), [path])
    return found


_LANGUAGES = (((".py",), _python_graph), (JS_EXTENSIONS, _js_graph), ((".go",), _go_graph), ((".rs",), _rust_graph))


def imports(changed: list[str], cmd: dict[str, Any], root: Path, layer: Any = None) -> Found:
    """Tests that depend on a changed file, by language (Python, JavaScript/TypeScript, Go, Rust);
    each language's graph is built once, only when a changed file is in that language."""
    found = Found()
    for extensions, graph in _LANGUAGES:
        mine = [path for path in changed if path.endswith(extensions)]
        if mine:
            graph(Path(root), cmd, mine, layer, found)
    return found


def coverage(changed: list[str], cmd: dict[str, Any], layer: Any, root: Path, *, state: Any = None,
             env_stack: Any = None) -> Found:
    """Tests whose recorded files hold a changed path; a path changed since the map's commit is unknown."""
    found = Found()
    name = _cmd_name(cmd)
    data = coverage_map.load(layer, name)
    if data is None:
        found.notes.append(f"coverage: no coverage map for {name} yet (a passing full run with #:coverage-run records one)")
        return found
    if state is None:
        state = SimpleNamespace(adw_id="", adw="", worktree=str(root))
    stale = coverage_map.stale_paths(layer, state, env_stack, data, changed)
    if stale:
        found.notes.append(f"coverage: {len(stale)} changed path(s) changed since the map's commit "
                           f"{str(data.get('commit', ''))[:8]} and are unknown to it")
    known = [path for path in changed if path not in stale]
    for test, files in (data.get("tests") or {}).items():
        hits = set(files or []) & set(known)
        if hits:
            found.add_test(str(test), hits)
    return found


def command(changed: list[str], cmd: dict[str, Any], root: Path, base: str, env_stack: Any) -> Found:
    """`#:select-cmd` run through the run's stack (as a check runs), with AL_CHANGED (one changed
    path per line) and AL_BASE; each output line is a test file or id relative to the repository
    root. When it names any test, every changed path counts as mapped (the codebase's selector
    answers for it); a failure is a note and selects nothing."""
    found = Found()
    script = str(cmd.get("selectCmd") or "")
    if not script:
        found.notes.append("command: the cmd has no #:select-cmd")
        return found
    variables = {"AL_CHANGED": "\n".join(changed), "AL_BASE": str(base or "")}
    exported = "".join(f"export {key}={shlex.quote(value)}; " for key, value in variables.items())
    wrap = getattr(env_stack, "wrap", None)
    argv = wrap(exported + script) if callable(wrap) else ["bash", "-c", exported + script]
    cwd = Path(getattr(env_stack, "cwd", None) or root) if callable(wrap) else Path(root)
    process_env = getattr(env_stack, "process_env", None)
    env = {**(process_env() if callable(process_env) else os.environ), **variables}
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
                                check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        found.notes.append(f"command: #:select-cmd could not run: {exc}"[:300])
        return found
    if result.returncode != 0:
        found.notes.append(f"command: #:select-cmd failed (exit {result.returncode}): "
                           f"{(result.stderr or result.stdout).strip()[-200:]}")
        return found
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for line in lines:
        found.add_test(posixpath.normpath(line), changed)
    return found


def learned(changed: list[str], cmd: dict[str, Any], layer: Any) -> Found:
    """What earlier batch misses taught: the tests of each lesson whose files intersect the change."""
    from . import testing  # testing imports this module

    found = Found()
    for entry in testing.learned(layer, _cmd_name(cmd)):
        hits = set(entry.get("files") or []) & set(changed)
        for test in entry.get("tests") or []:
            if hits:
                found.add_test(str(test), hits)
    return found


# ---------------------------------------------------------------- union and the filled form

def _relative(path: str, test_root: str) -> str | None:
    if not test_root:
        return path
    if path == test_root:
        return "."
    if path.startswith(test_root + "/"):
        return path[len(test_root) + 1:]
    return None


def _package_text(key: str, test_root: str) -> str | None:
    kind, _, value = key.partition(":")
    if kind == "rust":
        return value
    relative = _relative(value, test_root) if value else ("" if not test_root else None)
    if relative is None:
        return None
    return "./" + relative if relative not in ("", ".") else "."


def select(layer: Any, cmd: dict[str, Any], changed: list[str], *, root: Path | None = None, base: str = "",
           env_stack: Any = None, state: Any = None) -> Selection:
    """The tests one change concerns for `cmd` (the strategies in the module docstring)."""
    root = Path(root) if root is not None else Path(layer.app_root)
    changed = [posixpath.normpath(str(path)) for path in changed]
    order = ["changed-tests", *[s for s in (cmd.get("select") or DEFAULT_SELECT) if s != "changed-tests"], "learned"]
    test_root = posixpath.normpath(str(cmd.get("testRoot") or "")).strip("/")
    test_root = "" if test_root == "." else test_root
    result = Selection()
    mapped: set[str] = set()
    dropped: list[str] = []
    for strategy in dict.fromkeys(order):
        try:
            if strategy == "changed-tests":
                found = changed_tests(changed, cmd, layer, root)
            elif strategy == "path-map":
                found = path_map(changed, cmd, root)
            elif strategy == "imports":
                found = imports(changed, cmd, root, layer)
            elif strategy == "coverage":
                found = coverage(changed, cmd, layer, root, state=state, env_stack=env_stack)
            elif strategy == "command":
                found = command(changed, cmd, root, base, env_stack)
            elif strategy == "learned":
                found = learned(changed, cmd, layer)
            else:
                result.notes.append(f"unknown selection strategy {strategy!r} ignored")
                continue
        except Exception as exc:  # noqa: BLE001 — a strategy that breaks selects nothing; unmapped paths say so
            result.notes.append(f"{strategy}: failed ({type(exc).__name__}: {exc})"[:300])
            continue
        result.notes += found.notes
        added = 0
        for test, sources in found.tests.items():
            if not _under_covers(test.split("::", 1)[0], cmd):
                continue
            relative = _relative(test, test_root)
            if relative is None:
                dropped.append(test)
                continue
            mapped |= sources
            if relative not in result.tests:
                result.tests.append(relative)
                added += 1
        for key, sources in found.packages.items():
            text = _package_text(key, test_root)
            if text is None:
                dropped.append(key.partition(":")[2])
                continue
            mapped |= sources
            if text not in result.packages:
                result.packages.append(text)
                added += 1
        if added:
            result.by_strategy[strategy] = added
    for path in changed:
        if not _under_covers(path, cmd):
            continue
        if (root / path).is_file():
            relative = _relative(path, test_root)
            if relative is None:
                if not is_test(path, layer):
                    dropped.append(path)
            elif relative not in result.files:
                result.files.append(relative)
        if not is_test(path, layer) and path not in mapped:
            result.unmapped.append(path)
    if dropped:
        shown = ", ".join(list(dict.fromkeys(dropped))[:5])
        result.notes.append(f"outside #:test-root {test_root}, dropped: {shown}"
                            + (f" and {len(set(dropped)) - 5} more" if len(set(dropped)) > 5 else ""))
    return result


def fill(template: str, chosen: Selection) -> str:
    """The targeted form with `{tests}`, `{files}` and `{packages}` filled (shell-quoted, space
    separated); Rust crates become `-p a -p b`, Go packages stay `./a ./b`."""
    def joined(items: Iterable[str]) -> str:
        return " ".join(shlex.quote(item) for item in items)

    packages: list[str] = []
    for package in chosen.packages:
        if package == "." or package.startswith("./"):
            packages.append(shlex.quote(package))
        else:
            packages += ["-p", shlex.quote(package)]
    return (str(template).replace("{tests}", joined(chosen.tests)).replace("{files}", joined(chosen.files))
            .replace("{packages}", " ".join(packages)))
