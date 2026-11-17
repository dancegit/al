"""What a repository already says about its environment, read statically for `al scout`.

Reads only files git tracks (`git ls-files`), each at most 256 KiB, and never executes, imports or
evaluates anything from the repository: flake.nix dev shells (by pattern, not by `nix eval`),
shell.nix and devenv, Containerfile / Dockerfile stages and ports, dev container files (JSON with
comments), compose services, version files (.tool-versions, mise.toml, .nvmrc, .python-version,
rust-toolchain, pyproject requires-python, package.json engines and packageManager, go.mod),
lockfiles, browser test dependencies, CI workflows (apt-get install lines, setup-* versions, job
services) and .env examples (variable NAMES only: the rest of each line is never stored).

`detect()` returns plain JSON data (the `environment` of `al scout --json`); `render_section()`
turns exactly that data into the `## Environment` section of the LLM.txt draft, so the text and
the JSON always agree. `proposal()` writes a declaration in the layer language whose every line
says where it comes from. `read_devcontainer()` and `devcontainer_plan()` are shared with the
generator (`al gen`), which reads the same file from the default branch.

Standard library plus `yaml` (imported when a YAML file is read); no subprocess.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex
import tomllib
from pathlib import Path
from typing import Any

MAX_BYTES = 256 * 1024

# lockfile → the command that installs exactly what it pins, the cache it fills, the tool
LOCK_PREPARE = {
    "uv.lock": "uv sync --locked",
    "bun.lock": "bun install --frozen-lockfile",
    "bun.lockb": "bun install --frozen-lockfile",
    "package-lock.json": "npm ci",
    "pnpm-lock.yaml": "pnpm install --frozen-lockfile",
    "yarn.lock": "yarn install --frozen-lockfile",
    "Cargo.lock": "cargo fetch --locked",
    "go.sum": "go mod download",
}
LOCK_CACHE = {"uv.lock": "uv", "bun.lock": "npm", "bun.lockb": "npm", "package-lock.json": "npm",
              "pnpm-lock.yaml": "npm", "yarn.lock": "npm", "Cargo.lock": "cargo", "go.sum": "go"}
LOCK_TOOL = {"uv.lock": "uv", "bun.lock": "bun", "bun.lockb": "bun", "package-lock.json": "npm",
             "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "Cargo.lock": "cargo", "go.sum": "go"}

# dependency name → it drives a browser (package.json and Python dependency lists)
BROWSER_DEPS = {
    "package.json": ("@playwright/test", "playwright", "playwright-core", "cypress", "puppeteer", "puppeteer-core",
                     "selenium-webdriver", "webdriverio"),
    "python": ("playwright", "pytest-playwright", "selenium", "pyppeteer", "splinter"),
}

# dev container feature (last path component, without the tag) → Debian packages that provide it
DEVCONTAINER_FEATURES = {
    "node": ["nodejs", "npm"],
    "python": ["python3", "python3-pip", "python3-venv"],
    "git": ["git"],
    "common-utils": ["ca-certificates", "curl", "git"],
    "go": ["golang"],
    "rust": ["cargo", "rustc"],
    "java": ["default-jdk-headless"],
    "github-cli": ["gh"],
}
DEVCONTAINER_REASONS = {
    "remoteUser": "a run uses the host user's uid inside the container (--userns=keep-id)",
    "containerUser": "a run uses the host user's uid inside the container (--userns=keep-id)",
    "updateRemoteUserUID": "a run uses the host user's uid inside the container (--userns=keep-id)",
    "customizations": "editor settings; no editor runs in the sandbox",
    "mounts": "a run mounts only its worktree, its git metadata and its caches",
    "runArgs": "the runtime sets the container's flags itself (hardening, network, limits)",
    "remoteEnv": "set by an editor when it attaches; use containerEnv",
    "workspaceFolder": "the run's worktree is the working directory",
    "workspaceMount": "the run's worktree is mounted at its own path",
    "initializeCommand": "only postCreateCommand runs (as the prepare step)",
    "onCreateCommand": "only postCreateCommand runs (as the prepare step)",
    "updateContentCommand": "only postCreateCommand runs (as the prepare step)",
    "postStartCommand": "only postCreateCommand runs (as the prepare step)",
    "postAttachCommand": "only postCreateCommand runs (as the prepare step)",
    "dockerComposeFile": "a dev container made of compose services is not reused; use #:containerfile or #:base-image",
    "service": "a dev container made of compose services is not reused; use #:containerfile or #:base-image",
    "runServices": "a dev container made of compose services is not reused; use #:containerfile or #:base-image",
    "shutdownAction": "the runtime removes the run's container itself",
    "overrideCommand": "the runtime starts the container with its own command",
    "privileged": "a run's container never gets privileges",
    "capAdd": "a run's container drops every capability",
    "securityOpt": "the runtime sets the container's security options",
    "hostRequirements": "the host is chosen by whoever runs the layer",
    "portsAttributes": "only forwardPorts is used (as published ports)",
    "otherPortsAttributes": "only forwardPorts is used (as published ports)",
}
DEVCONTAINER_HONOURED = {"image", "build", "dockerFile", "context", "features", "containerEnv", "postCreateCommand",
                         "forwardPorts", "name", "$schema"}

# image name (without registry, path and tag) → a readiness probe run inside the sidecar
SERVICE_READY = {
    "postgres": "pg_isready -U postgres",
    "postgis": "pg_isready -U postgres",
    "mysql": "mysqladmin ping -h 127.0.0.1",
    "mariadb": "mysqladmin ping -h 127.0.0.1",
    "redis": "redis-cli ping",
    "valkey": "valkey-cli ping",
}

ENV_EXAMPLES = (".env.example", ".env.sample", ".env.template", ".env.dist", "env.example")

# devenv `languages.X.enable` → nixpkgs attributes
DEVENV_LANGUAGES = {"python": ["python3"], "go": ["go"], "javascript": ["nodejs"], "typescript": ["nodejs"],
                    "rust": ["cargo", "rustc"], "ruby": ["ruby"], "php": ["php"], "java": ["jdk"],
                    "elixir": ["elixir"], "haskell": ["ghc"], "zig": ["zig"], "c": ["gcc"], "cplusplus": ["gcc"]}

# version-file tool names → one name per tool
TOOL_ALIASES = {"nodejs": "node", "node": "node", "python": "python", "golang": "go", "go": "go", "rust": "rust",
                "ruby": "ruby", "java": "java", "bun": "bun", "deno": "deno", "pnpm": "pnpm", "uv": "uv"}
# a base image per tool when the repository names a version but no container or Nix setup
BASE_IMAGES = (("python", "docker.io/library/python:{mm}-slim"), ("node", "docker.io/library/node:{major}-slim"),
               ("go", "docker.io/library/golang:{mm}"), ("rust", "docker.io/library/rust:{mm}-slim"))
DEFAULT_BASE = "docker.io/library/debian:stable-slim"

ENV_NAME = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
EXACT = re.compile(r"v?(\d+(?:\.\d+)*)(?:\.x)?")


# ------------------------------------------------------------------------------------------ helpers

class _Repo:
    """The tracked files of the application, read lazily, size-capped, never executed."""

    def __init__(self, app: Path, tracked: list[str]):
        self.app, self.tracked = Path(app), [t for t in tracked if t]
        self.set = set(self.tracked)
        self.notes: list[str] = []

    def has(self, rel: str) -> bool:
        return rel in self.set

    def read(self, rel: str) -> str | None:
        if rel not in self.set:
            return None
        path = self.app / rel
        try:
            if not path.is_file() or path.is_symlink():
                return None
            if path.stat().st_size > MAX_BYTES:
                self.notes.append(f"{rel}: larger than {MAX_BYTES // 1024} KiB, not read")
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.notes.append(f"{rel}: not readable ({exc.strerror or exc})")
            return None

    def yaml(self, rel: str) -> Any:
        text = self.read(rel)
        if text is None:
            return None
        import yaml
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            self.notes.append(f"{rel}: does not parse as YAML" + (f" (line {mark.line + 1})" if mark else ""))
            return None


def _line_of(text: str, pattern: str, start: int = 1) -> int | None:
    rx = re.compile(pattern)
    for number, line in enumerate(text.splitlines(), 1):
        if number >= start and rx.search(line):
            return number
    return None


def _at(file: str, line: int | None, key: str = "") -> str:
    return f"{file}:{line}" if line else (f"{file} {key}" if key else file)


def _q(text: str) -> str:
    """A string literal of the layer language (Racket): JSON escaping is a subset of it."""
    return json.dumps(text, ensure_ascii=False)


def _normalise_image(image: str) -> str:
    image = image.strip()
    first = image.split("/", 1)[0]
    if "/" not in image:
        return f"docker.io/library/{image}"
    if "." not in first and ":" not in first and first != "localhost":
        return f"docker.io/{image}"
    return image


def _image_base(image: str) -> str:
    name = image.rsplit("/", 1)[-1]
    return re.split(r"[:@]", name, 1)[0]


def _requirement_name(spec: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return m.group(1).lower() if m else ""


# ------------------------------------------------------------------------------------------ dev containers

def read_devcontainer(text: str) -> dict:
    """A devcontainer.json (JSON with comments and trailing commas) as a dict. Raises ValueError."""
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    stripped = "".join(out)
    # trailing commas, outside strings
    cleaned: list[str] = []
    in_string = False
    for j, c in enumerate(stripped):
        if in_string:
            cleaned.append(c)
            if c == '"' and not _escaped(stripped, j):
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == ",":
            rest = stripped[j + 1:].lstrip()
            if rest[:1] in ("}", "]"):
                continue
        cleaned.append(c)
    data = json.loads("".join(cleaned))
    if not isinstance(data, dict):
        raise ValueError("the top level is not an object")
    return data


def _escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _command_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return shlex.join(str(v) for v in value)
    if isinstance(value, dict):
        return " && ".join(_command_text(v) for v in value.values() if _command_text(v))
    return ""


def devcontainer_plan(dc: dict, base_dir: str = ".") -> dict:
    """What al honours of a dev container definition and what it does not, with a reason each.
    `base_dir`: the directory of the devcontainer file relative to the repository root (paths in
    `build` are relative to it)."""
    honoured: list[str] = []
    not_honoured: list[dict] = []
    build = None
    image = str(dc.get("image") or "")
    raw_build = dc.get("build") if isinstance(dc.get("build"), dict) else None
    if raw_build is None and dc.get("dockerFile"):
        raw_build = {"dockerfile": dc.get("dockerFile"), "context": dc.get("context", ".")}
    problems: list[str] = []
    if raw_build and raw_build.get("dockerfile"):
        def resolve(path: str) -> str:
            joined = posixpath.normpath(posixpath.join(base_dir, str(path)))
            if joined.startswith("..") or joined.startswith("/"):
                problems.append(f"build path {path!r} leaves the repository")
            return joined
        build = {"containerfile": resolve(raw_build["dockerfile"]), "context": resolve(raw_build.get("context") or "."),
                 "target": str(raw_build.get("target") or "")}
        honoured.append(f"build {build['containerfile']}" + (f" (stage {build['target']})" if build["target"] else ""))
        for key in sorted(k for k in raw_build if k not in ("dockerfile", "context", "target")):
            not_honoured.append({"item": f"build.{key}", "reason": "only dockerfile, context and target are used"})
        image = ""
    elif image:
        honoured.append(f"image {image}")
    compose = bool(dc.get("dockerComposeFile")) and not image and not build
    packages: list[str] = []
    for feature_id, options in (dc.get("features") or {}).items():
        short = re.split(r"[:@]", str(feature_id).rstrip("/").rsplit("/", 1)[-1], 1)[0]
        if short in DEVCONTAINER_FEATURES:
            apt = DEVCONTAINER_FEATURES[short]
            packages += [p for p in apt if p not in packages]
            honoured.append(f"features.{feature_id} → apt {' '.join(apt)}")
            for option in sorted((options or {}) if isinstance(options, dict) else {}):
                not_honoured.append({"item": f"features.{short}.{option}",
                                     "reason": "feature options are not honoured: the distribution's package is installed"})
        else:
            not_honoured.append({"item": f"features.{feature_id}",
                                 "reason": "no known translation to distribution packages; add what it installs with #:packages or #:setup"})
    env: dict[str, str] = {}
    for key, value in (dc.get("containerEnv") or {}).items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)) and "${" not in str(value):
            env[str(key)] = str(value)
        else:
            not_honoured.append({"item": f"containerEnv.{key}",
                                 "reason": "not a plain variable name and value (a ${…} reference is resolved by an editor)"})
    if env:
        honoured.append("containerEnv " + " ".join(sorted(env)))
    post_create = _command_text(dc.get("postCreateCommand"))
    if post_create:
        honoured.append("postCreateCommand (as the prepare step)")
    ports: list[int] = []
    for port in dc.get("forwardPorts") or []:
        if isinstance(port, int) and 0 < port < 65536:
            ports.append(port)
        elif isinstance(port, str) and port.isdigit():
            ports.append(int(port))
        else:
            not_honoured.append({"item": f"forwardPorts {port}", "reason": "only container ports (numbers) can be published"})
    if ports:
        honoured.append("forwardPorts " + " ".join(map(str, ports)) + " (as published ports)")
    for key in dc:
        if key in DEVCONTAINER_HONOURED or key == "features":
            continue
        not_honoured.append({"item": key, "reason": DEVCONTAINER_REASONS.get(key, "not supported by al")})
    return {"image": image, "build": build, "compose": compose, "packages": packages, "container_env": env,
            "post_create": post_create, "forward_ports": ports, "honoured": honoured, "not_honoured": not_honoured,
            "problems": problems}


# ------------------------------------------------------------------------------------------ detection

def _top(rel: str) -> bool:
    return "/" not in rel


def _depth(rel: str) -> int:
    return rel.count("/")


def _flake(repo: _Repo, rel: str) -> dict | None:
    text = repo.read(rel)
    if text is None:
        return None
    lines: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), 1):
        for m in re.finditer(r"([A-Za-z_][\w'-]*)\s*=\s*(?:[\w.]+\.)?(?:mkShell|mkShellNoCC)\b", line):
            if m.group(1) not in ("devShell", "shell"):
                lines.setdefault(m.group(1), number)
        if re.search(r"\bdevShell\s*=", line):
            lines.setdefault("default", number)
    if not lines and "devShells" not in text and "devShell" not in text:
        return None
    if not lines:
        lines["default"] = _line_of(text, r"devShells?") or 1
    directory = posixpath.dirname(rel) or "."
    shells = sorted(lines)
    first = min(lines.values())
    return {"file": rel, "kind": "nix-flake", "line": first, "dir": directory, "shells": shells,
            "shell_lines": {name: lines[name] for name in shells},
            "says": f"dev shells {', '.join(shells)} (reused with #:flake {_q(directory)})"}


def _nix_packages(text: str, attr: str) -> tuple[list[str], int | None]:
    m = re.search(attr + r"\s*=\s*(?:with\s+pkgs\s*;\s*)?\[([^\]]*)\]", text, re.S)
    if not m:
        return [], None
    names = [re.sub(r"^pkgs\.", "", token) for token in re.findall(r"[A-Za-z_][\w.-]*", m.group(1))]
    line = text[: m.start()].count("\n") + 1
    return [n for n in names if n not in ("with", "pkgs")], line


def _containerfile(repo: _Repo, rel: str) -> dict | None:
    text = repo.read(rel)
    if text is None:
        return None
    joined = re.sub(r"\\\n", " ", text)
    stages: list[str] = []
    base, first_line, ports = "", None, []
    for number, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+([\w.-]+))?", line, re.I)
        if m:
            base = base or m.group(1)
            first_line = first_line or number
            if m.group(2):
                stages.append(m.group(2))
    for m in re.finditer(r"^\s*EXPOSE\s+(.+)$", joined, re.M | re.I):
        for token in m.group(1).split():
            port = token.split("/", 1)[0]
            if port.isdigit() and int(port) not in ports:
                ports.append(int(port))
    if first_line is None:
        repo.notes.append(f"{rel}: no FROM line, not a Containerfile")
        return None
    target = next((s for s in ("dev", "development", "devcontainer") if s in stages), "")
    says = (f"stages {', '.join(stages)}" if stages else "one stage") + f", from {base}"
    if ports:
        says += f"; EXPOSE {' '.join(map(str, ports))} (not published unless listed in #:publish)"
    return {"file": rel, "kind": "containerfile", "line": first_line, "stages": stages, "target": target,
            "base": base, "ports": ports, "says": says}


def _add_tool(tools: dict, tool: str, version: str, file: str, line: int | None, key: str = "") -> None:
    version = str(version).strip()
    if not version:
        return
    tools.setdefault(tool, []).append({"version": version, "file": file, "line": line, "at": _at(file, line, key)})


def _versions(repo: _Repo, tools: dict) -> None:
    text = repo.read(".tool-versions")
    if text is not None:
        for number, line in enumerate(text.splitlines(), 1):
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2:
                _add_tool(tools, TOOL_ALIASES.get(parts[0], parts[0]), parts[1], ".tool-versions", number)
    for rel in ("mise.toml", ".mise.toml"):
        text = repo.read(rel)
        if text is None:
            continue
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            repo.notes.append(f"{rel}: does not parse as TOML")
            continue
        for key, value in (data.get("tools") or {}).items():
            if isinstance(value, list):
                value = value[0] if value else ""
            if isinstance(value, dict):
                value = value.get("version", "")
            _add_tool(tools, TOOL_ALIASES.get(key, key), str(value), rel,
                      _line_of(text, rf'^\s*"?{re.escape(key)}"?\s*='))
    for rel in (".nvmrc", ".node-version"):
        text = repo.read(rel)
        if text and text.strip():
            _add_tool(tools, "node", text.strip().splitlines()[0].lstrip("v"), rel, 1)
    text = repo.read(".python-version")
    if text and text.strip():
        _add_tool(tools, "python", text.strip().splitlines()[0], ".python-version", 1)
    for rel in ("rust-toolchain", "rust-toolchain.toml"):
        body = repo.read(rel)
        if not body or not body.strip():
            continue
        if rel.endswith(".toml") or body.lstrip().startswith("["):
            try:
                channel = (tomllib.loads(body).get("toolchain") or {}).get("channel", "")
            except tomllib.TOMLDecodeError:
                repo.notes.append(f"{rel}: does not parse as TOML")
                continue
            _add_tool(tools, "rust", str(channel), rel, _line_of(body, r"^\s*channel\s*="))
        else:
            _add_tool(tools, "rust", body.strip().splitlines()[0], rel, 1)
    text = repo.read("pyproject.toml")
    if text is not None:
        try:
            requires = (tomllib.loads(text).get("project") or {}).get("requires-python", "")
        except tomllib.TOMLDecodeError:
            requires = ""
        _add_tool(tools, "python", requires, "pyproject.toml", _line_of(text, r"^\s*requires-python\s*="))
    text = repo.read("go.mod")
    if text is not None:
        m = re.search(r"^go\s+(\S+)", text, re.M)
        if m:
            _add_tool(tools, "go", m.group(1), "go.mod", text[: m.start()].count("\n") + 1)


def _package_json(repo: _Repo, tools: dict, browser: dict) -> dict | None:
    text = repo.read("package.json")
    if text is None:
        return None
    try:
        pkg = json.loads(text)
    except json.JSONDecodeError:
        repo.notes.append("package.json: does not parse as JSON")
        return None
    if not isinstance(pkg, dict):
        return None
    engines = pkg.get("engines") or {}
    if isinstance(engines, dict):
        for key in ("node", "bun", "pnpm"):
            if isinstance(engines.get(key), str):
                _add_tool(tools, key, engines[key], "package.json", None, f"engines.{key}")
    if not browser["needed"]:
        for group in ("dependencies", "devDependencies", "optionalDependencies"):
            deps = pkg.get(group) or {}
            hit = next((d for d in BROWSER_DEPS["package.json"] if isinstance(deps, dict) and d in deps), None)
            if hit:
                browser.update(needed=True, why=f"package.json {group} {hit}", file="package.json")
                break
    manager = pkg.get("packageManager")
    if isinstance(manager, str) and "@" in manager:
        name, _, version = manager.partition("@")
        return {"name": name, "version": version.split("+", 1)[0]}
    return None


def _python_browser(repo: _Repo, browser: dict) -> None:
    if browser["needed"]:
        return
    text = repo.read("pyproject.toml")
    if text is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            repo.notes.append("pyproject.toml: does not parse as TOML")
            data = {}
        project = data.get("project") or {}
        groups = [("dependencies", project.get("dependencies") or [])]
        groups += [(f"optional-dependencies.{k}", v) for k, v in (project.get("optional-dependencies") or {}).items()]
        groups += [(f"dependency-groups.{k}", v) for k, v in (data.get("dependency-groups") or {}).items()]
        for label, specs in groups:
            for spec in specs if isinstance(specs, list) else []:
                name = _requirement_name(spec) if isinstance(spec, str) else ""
                if name in BROWSER_DEPS["python"]:
                    browser.update(needed=True, why=f"pyproject.toml {label} {name}", file="pyproject.toml")
                    return
    for rel in ("requirements.txt", "requirements-dev.txt", "requirements/dev.txt", "requirements/test.txt"):
        text = repo.read(rel)
        for line in (text or "").splitlines():
            name = _requirement_name(line.split("#", 1)[0])
            if name in BROWSER_DEPS["python"]:
                browser.update(needed=True, why=f"{rel} {name}", file=rel)
                return


def _service(sid: str, spec: dict, file: str, line: int | None) -> dict:
    image = _normalise_image(str(spec.get("image")))
    env = spec.get("environment", spec.get("env")) or {}
    if isinstance(env, dict):
        names = [str(k) for k in env]
    else:
        names = [str(item).split("=", 1)[0] for item in env if isinstance(item, (str, int))]
    ready = ""
    health = spec.get("healthcheck") or {}
    test = health.get("test") if isinstance(health, dict) else None
    if isinstance(test, list) and test:
        if test[0] == "CMD-SHELL":
            ready = " ".join(str(t) for t in test[1:])
        elif test[0] == "CMD":
            ready = shlex.join(str(t) for t in test[1:])
    elif isinstance(test, str):
        ready = test
    if not ready:
        ready = SERVICE_READY.get(_image_base(image), "")
    ports = [str(p) for p in spec.get("ports") or []]
    return {"id": sid, "image": image, "env_names": names, "ports": ports, "ready": ready, "file": file, "line": line}


def _compose(repo: _Repo, rel: str, services: list[dict], sources: list[dict]) -> None:
    data = repo.yaml(rel)
    if data is None:
        return
    text = repo.read(rel) or ""
    found, skipped = [], []
    for sid, spec in ((data.get("services") or {}) if isinstance(data, dict) else {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("build") or not spec.get("image"):
            skipped.append(str(sid))
            continue
        line = _line_of(text, rf"^\s+{re.escape(str(sid))}\s*:\s*$")
        services.append(_service(str(sid), spec, rel, line))
        found.append(str(sid))
    says = f"services {', '.join(found) or '(none with an image)'}"
    if skipped:
        says += f"; {', '.join(skipped)} built from the repository (not proposed)"
    sources.append({"file": rel, "kind": "compose", "line": _line_of(text, r"^services\s*:") or 1,
                    "services": found, "says": says + "; proposed as (service container …), never started from the file"})


APT_RX = re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\b([^&|;#\n]*)")


def _ci(repo: _Repo, rel: str, tools: dict, services: list[dict], system: list[dict], sources: list[dict]) -> None:
    text = repo.read(rel)
    if text is None:
        return
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        for m in APT_RX.finditer(line):
            for token in m.group(1).split():
                if token.startswith("-") or not re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", token):
                    continue
                if not any(p["name"] == token for p in system):
                    system.append({"name": token, "file": rel, "line": number})
        m = re.search(r"uses:\s*['\"]?actions/setup-(node|python|go)@", line)
        if m:
            tool, indent = m.group(1), len(line) - len(line.lstrip(" -"))
            for later in range(number, min(number + 12, len(lines))):
                follow = lines[later]
                if follow.strip().startswith("- ") and len(follow) - len(follow.lstrip(" -")) <= indent:
                    break
                v = re.search(rf"{tool}-version:\s*['\"]?([^'\"\s#]+)", follow)
                if v:
                    _add_tool(tools, tool, v.group(1), rel, later + 1)
                    break
    if rel.endswith(".gitlab-ci.yml"):
        sources.append({"file": rel, "kind": "ci", "line": 1, "says": "CI pipeline (apt-get install lines read)"})
        return
    data = repo.yaml(rel)
    found = []
    for job in ((data.get("jobs") or {}) if isinstance(data, dict) else {}).values():
        for sid, spec in ((job.get("services") or {}) if isinstance(job, dict) else {}).items():
            if isinstance(spec, str):
                spec = {"image": spec}
            if not isinstance(spec, dict) or not spec.get("image") or any(s["id"] == str(sid) for s in services):
                continue
            line = _line_of(text, rf"^\s*image:\s*['\"]?{re.escape(str(spec['image']))}")
            services.append(_service(str(sid), spec, rel, line))
            found.append(str(sid))
    sources.append({"file": rel, "kind": "ci", "line": 1,
                    "says": "CI workflow (apt-get install lines, setup-* versions" + (f", services {', '.join(found)}" if found else "")
                    + "); read, not run"})


def _env_examples(repo: _Repo) -> list[dict]:
    out = []
    for rel in repo.tracked:
        if posixpath.basename(rel) not in ENV_EXAMPLES or _depth(rel) > 1:
            continue
        text = repo.read(rel)
        names: list[str] = []
        for line in (text or "").splitlines():
            m = ENV_NAME.match(line)
            if m and m.group(1) not in names:
                names.append(m.group(1))
        if names:
            out.append({"file": rel, "names": names})
    return out


# ---------------------------------------------------------------------- versions and conflicts

def _parts(version: str) -> tuple[int, ...] | None:
    m = EXACT.fullmatch(version.strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _admits(spec: str, parts: tuple[int, ...]) -> bool | None:
    """Whether a range like `>=18 <19`, `^3.12`, `~1.2` or `>=3.9,<4` admits `parts` (None: not a range)."""
    def cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
        width = max(len(a), len(b))
        a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
        return (a > b) - (a < b)

    alternatives = []
    for alternative in spec.split("||"):
        clauses = re.findall(r"(>=|<=|==|!=|~=|>|<|\^|~|=)?\s*v?(\d+(?:\.\d+)*)(?:\.[x*])?", alternative)
        if not clauses:
            return None
        ok = True
        for op, version in clauses:
            want = tuple(int(x) for x in version.split("."))
            shared = parts[: len(want)]
            if op in (">=",):
                ok &= cmp(parts, want) >= 0
            elif op == ">":
                ok &= cmp(parts, want) > 0
            elif op == "<=":
                ok &= cmp(shared, want) <= 0
            elif op == "<":
                ok &= cmp(parts, want) < 0
            elif op == "!=":
                ok &= shared != want
            elif op == "^":
                ok &= cmp(parts, want) >= 0 and parts[:1] == want[:1]
            elif op in ("~", "~="):
                keep = max(1, len(want) - 1)
                ok &= cmp(parts, want) >= 0 and parts[:keep] == want[:keep]
            else:  # == or bare: a prefix match
                ok &= shared == want[: len(shared)] and len(parts) >= 1
        alternatives.append(ok)
    return any(alternatives)


def _conflicts(tools: dict) -> list[str]:
    out = []
    for tool, entries in tools.items():
        groups: list[dict] = []
        ranges = []
        for e in entries:
            parts = _parts(e["version"])
            if parts is None:
                if re.search(r"[<>=^~]", e["version"]):
                    ranges.append(e)
                continue
            for g in groups:
                if all(p[: len(parts)] == parts[: len(p)] for p in g["parts"]):
                    g["parts"].append(parts)
                    g["entries"].append(e)
                    break
            else:
                groups.append({"parts": [parts], "entries": [e]})
        sides = []
        for g in groups:
            shortest = min(g["entries"], key=lambda e: len(_parts(e["version"]) or ()))
            sides.append(f"{shortest['version'].lstrip('v')} ({', '.join(e['at'] for e in g['entries'])})")
        for r in ranges:
            if groups and not any(_admits(r["version"], p) for g in groups for p in g["parts"]):
                sides.append(f"{r['version']} ({r['at']})")
        if len(sides) > 1:
            out.append(f"{tool}: " + " vs ".join(sides))
    return out


def _exact(tools: dict, tool: str) -> dict | None:
    return next((e for e in tools.get(tool, []) if _parts(e["version"]) is not None), None)


# ------------------------------------------------------------------------------------------ detect

def detect(app: Path, tracked: list[str], commands: list[dict] | None = None, nix_guess: list[str] | None = None) -> dict:
    """Everything the repository says about its environment (see the module docstring). `commands`
    are scout's commands (a prepare cmd never takes a name they already use for another command);
    `nix_guess` is scout's nixpkgs guess from languages, proposed when nothing else is found."""
    repo = _Repo(app, tracked)
    sources: list[dict] = []
    tools: dict[str, list[dict]] = {}
    services: list[dict] = []
    system: list[dict] = []
    browser = {"needed": False, "why": "", "file": ""}

    flakes = sorted((rel for rel in repo.tracked if posixpath.basename(rel) == "flake.nix" and _depth(rel) <= 2),
                    key=lambda r: (_depth(r), r))
    for rel in flakes:
        found = _flake(repo, rel)
        if found:
            sources.append(found)
    if repo.has("shell.nix"):
        text = repo.read("shell.nix") or ""
        packages, line = _nix_packages(text, r"(?:buildInputs|nativeBuildInputs|packages)")
        sources.append({"file": "shell.nix", "kind": "shell-nix", "line": line or 1, "packages": packages,
                        "says": "listed, not reused: packages " + (" ".join(packages) or "(none found)")
                        + " (translated to #:packages)"})
    if repo.has("devenv.nix"):
        text = repo.read("devenv.nix") or ""
        packages, line = _nix_packages(text, r"\bpackages")
        langs = [(m.group(1), text[: m.start()].count("\n") + 1)
                 for m in re.finditer(r"languages\.(\w+)\.enable\s*=\s*true", text)]
        for lang, number in langs:
            _add_tool(tools, TOOL_ALIASES.get(lang, lang), "(devenv)", "devenv.nix", number)
        attrs = [a for lang, _ in langs for a in DEVENV_LANGUAGES.get(lang, [lang])] + packages
        sources.append({"file": "devenv.nix", "kind": "devenv", "line": (langs[0][1] if langs else line) or 1,
                        "packages": list(dict.fromkeys(attrs)),
                        "says": "listed, not reused: languages " + (" ".join(l for l, _ in langs) or "(none)")
                        + (f", packages {' '.join(packages)}" if packages else "") + " (translated to #:packages)"})
    devcontainer = None
    for rel in (".devcontainer/devcontainer.json", ".devcontainer.json",
                *sorted(r for r in repo.tracked if re.fullmatch(r"\.devcontainer/[^/]+/devcontainer\.json", r))):
        text = repo.read(rel)
        if text is None:
            continue
        try:
            dc = read_devcontainer(text)
        except ValueError as exc:
            repo.notes.append(f"{rel}: does not parse as JSON with comments ({str(exc).split(':')[0]})")
            continue
        plan = devcontainer_plan(dc, posixpath.dirname(rel) or ".")
        plan["container_env"] = sorted(plan["container_env"])  # names only: values stay in the file
        plan["file"] = rel
        devcontainer = devcontainer or plan
        what = (f"image {plan['image']}" if plan["image"] else f"build {plan['build']['containerfile']}" if plan["build"]
                else "compose services (not reused)" if plan["compose"] else "no image")
        sources.append({"file": rel, "kind": "devcontainer", "line": 1,
                        "says": f"{what}; {len(plan['honoured'])} honoured, {len(plan['not_honoured'])} not honoured"})
    for rel in sorted((r for r in repo.tracked if _depth(r) <= 2 and not r.startswith(".devcontainer")
                       and re.fullmatch(r"(?:[\w.-]+\.)?(?:Containerfile|Dockerfile)(?:\.[\w.-]+)?", posixpath.basename(r))),
                      key=lambda r: (_depth(r), r)):
        found = _containerfile(repo, rel)
        if found:
            sources.append(found)
    for rel in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
        if repo.has(rel):
            _compose(repo, rel, services, sources)
    _versions(repo, tools)
    manager = _package_json(repo, tools, browser)
    _python_browser(repo, browser)
    ci_files = sorted(r for r in repo.tracked
                      if re.fullmatch(r"\.(?:github|forgejo|gitea)/workflows/[^/]+\.ya?ml", r)) + \
        ([".gitlab-ci.yml"] if repo.has(".gitlab-ci.yml") else [])
    for rel in ci_files:
        _ci(repo, rel, tools, services, system, sources)

    lockfiles = [lock for lock in LOCK_PREPARE if repo.has(lock)]
    package_manager = None
    if manager:
        sources_pm = ["package.json packageManager"] + [l for l in lockfiles if LOCK_TOOL[l] == manager["name"]]
        package_manager = {**manager, "from": sources_pm}
    elif lockfiles:
        package_manager = {"name": LOCK_TOOL[lockfiles[0]], "version": "", "from": [lockfiles[0]]}

    known = {c["name"]: c["shell"] for c in commands or []}
    prepare: list[dict] = []
    seen_shells: set[str] = set()
    for lock in lockfiles:
        shell = LOCK_PREPARE[lock]
        if shell in seen_shells:
            continue
        seen_shells.add(shell)
        if not prepare:
            name = "install" if known.get("install", shell) == shell else "prepare-deps"
        else:
            name = f"install-{LOCK_TOOL[lock]}"
        prepare.append({"cmd_name": name, "shell": shell, "cache": LOCK_CACHE[lock], "file": lock,
                        "existing": known.get(name) == shell})

    env = {"sources": sources, "tools": tools, "package_manager": package_manager, "lockfiles": lockfiles,
           "services": services, "system_packages": system, "browser": browser, "env_files": _env_examples(repo),
           "devcontainer": devcontainer, "conflicts": _conflicts(tools), "prepare": prepare,
           "nix_guess": list(nix_guess or []), "notes": repo.notes}
    env["proposal"] = proposal(env)
    return env


# ------------------------------------------------------------------------------------------ proposal

def _cite(*ats: str) -> str:
    ats = tuple(dict.fromkeys(a for a in ats if a))
    return " ; from " + ", ".join(ats) if ats else ""


def _base_image(env: dict) -> tuple[str, str]:
    tools = env["tools"]
    for tool, pattern in BASE_IMAGES:
        e = _exact(tools, tool)
        if e:
            parts = _parts(e["version"]) or (0,)
            mm = ".".join(map(str, parts[:2]))
            return pattern.format(mm=mm, major=parts[0]), e["at"]
    return DEFAULT_BASE, ""


def proposal(env: dict) -> dict:
    """A declaration in the layer language, built only from `env`: `codebase` goes inside
    (codebase …), `layer` at the top level; every line cites its file or is a comment."""
    sources = env["sources"]
    flake = next((s for s in sources if s["kind"] == "nix-flake"), None)
    dc = env["devcontainer"] if env["devcontainer"] and not env["devcontainer"]["compose"] else None
    cf = next((s for s in sources if s["kind"] == "containerfile"), None)
    nixsrc = next((s for s in sources if s["kind"] in ("shell-nix", "devenv") and s["packages"]), None)
    browser = env["browser"]
    services = env["services"]
    codebase: list[str] = []
    layer: list[str] = []

    for p in env["prepare"]:
        if p.get("existing"):
            codebase.append(f";; (cmd {p['cmd_name']} {_q(p['shell'])}) is already among the scouted commands; from {p['file']}")
        else:
            codebase.append(f"(cmd {p['cmd_name']} {_q(p['shell'])} #:description \"dependencies resolve\"){_cite(p['file'])}")

    if flake:
        primary = "flake"
    elif dc:
        primary = "devcontainer"
    elif cf:
        primary = "containerfile"
    elif nixsrc:
        primary = "nix"
    elif env["tools"] or services or env["system_packages"] or browser["needed"]:
        primary = "image"
    else:
        primary = "guess"
    podman = primary in ("devcontainer", "containerfile", "image")

    names = [p["cmd_name"] for p in env["prepare"]]
    post_create = bool(primary == "devcontainer" and dc["post_create"])
    worktree = ["(environment worktree"]
    cites: list[str] = []
    if names and not post_create:
        worktree.append(f"#:prepare ({' '.join(names)})")
        cites += [p["file"] for p in env["prepare"]]
    if post_create:
        layer.append(f";; prepare: the dev container's postCreateCommand runs once per run; #:prepare ({' '.join(names) or 'install'}) on the worktree would replace it")
    if env["env_files"]:
        variables = [n for f in env["env_files"] for n in f["names"]]
        files = [f["file"] for f in env["env_files"]]
        layer.append(f";; the run needs {', '.join(dict.fromkeys(variables))} (names from {', '.join(files)}): keep the values in the "
                     "app's untracked .env, copied into each run by #:copy-files, or in the layer's local.env / the environment; never in a committed file")
        worktree.append('#:copy-files (".env")')
        cites += files
    layer.append(" ".join(worktree) + ")" + (_cite(*cites) if cites else " ; from al (every run has its own worktree)"))
    ids = ["worktree"]

    caches = sorted({p["cache"] for p in env["prepare"]})
    extras = (" #:browser #t" if browser["needed"] else "")
    pod_extras = extras + (f" #:caches ({' '.join(_q(c) for c in caches)})" if caches else "")
    service_ids = [s["id"] for s in services]
    if services:
        prefix = "" if podman else ";; "
        if not podman:
            layer.append(";; services: they need a podman option (#:services) to run beside the checks")
        for s in services:
            env_items = " ".join(_q(f"{n}=SET-ME") for n in s["env_names"])
            line = f"{prefix}(service container #:id {s['id']} #:image {_q(s['image'])}"
            line += f" #:env ({env_items})" if env_items else ""
            line += f" #:ready-cmd {_q(s['ready'])}" if s["ready"] else ""
            layer.append(line + ")" + _cite(_at(s["file"], s["line"])))
    svc = f" #:services ({' '.join(service_ids)})" if services and podman else ""
    browser_cite = browser["file"] if browser["needed"] else ""

    if primary == "flake":
        shell = "default" if "default" in flake["shells"] else flake["shells"][0]
        layer.append(f"(environment nix-devshell #:flake {_q(flake['dir'])} #:shell {_q(shell)}{extras})"
                     + _cite(_at(flake["file"], flake["shell_lines"].get(shell)), browser_cite))
        ids.append("nix-devshell")
    elif primary == "devcontainer":
        layer.append(f"(environment podman #:id sandbox #:devcontainer {_q(dc['file'])}{pod_extras}{svc})"
                     + _cite(dc["file"], browser_cite, *(p["file"] for p in env["prepare"])))
        ids.append("sandbox")
    elif primary == "containerfile":
        target = f" #:target {_q(cf['target'])}" if cf["target"] else ""
        layer.append(f"(environment podman #:id sandbox #:containerfile {_q(cf['file'])}{target} #:context \".\"{pod_extras}{svc})"
                     + _cite(_at(cf["file"], cf["line"]), browser_cite, *(p["file"] for p in env["prepare"])))
        ids.append("sandbox")
    elif primary == "nix":
        packages = " ".join(_q(p) for p in nixsrc["packages"])
        layer.append(f"(environment nix-devshell #:packages ({packages}){extras})" + _cite(_at(nixsrc["file"], nixsrc["line"]), browser_cite))
        ids.append("nix-devshell")
    elif primary == "image":
        image, at = _base_image(env)
        apt = [p["name"] for p in env["system_packages"]]
        packages = f" #:packages ({' '.join(_q(p) for p in apt)})" if apt else ""
        system_cites = [_at(p["file"], p["line"]) for p in env["system_packages"][:1]]
        layer.append(f"(environment podman #:id sandbox #:base-image {_q(image)}{packages}{pod_extras}{svc})"
                     + _cite(at or "al (default base image)", *system_cites, *(_at(s["file"], s["line"]) for s in services[:1]),
                             browser_cite, *(p["file"] for p in env["prepare"])))
        ids.append("sandbox")
    else:
        guess = " ".join(_q(p) for p in env["nix_guess"]) or _q("git")
        layer.append(f"(environment nix-devshell #:packages ({guess})) ; from al (nixpkgs guess from the languages)")
        ids.append("nix-devshell")

    alternatives = []
    if flake and primary != "flake":
        alternatives.append(f'(environment nix-devshell #:flake {_q(flake["dir"])}){_cite(flake["file"])}')
    if dc and primary != "devcontainer":
        alternatives.append(f"(environment podman #:id sandbox #:devcontainer {_q(dc['file'])}){_cite(dc['file'])}")
    if cf and primary != "containerfile":
        target = f" #:target {_q(cf['target'])}" if cf["target"] else ""
        alternatives.append(f"(environment podman #:id sandbox #:containerfile {_q(cf['file'])}{target}){_cite(_at(cf['file'], cf['line']))}")
    layer += [f";; alternative: {a}" for a in alternatives]
    layer += [f";; conflict: {c} (decide which one the run uses)" for c in env["conflicts"]]
    return {"codebase": "\n".join(codebase), "layer": "\n".join(layer), "lines": codebase + layer, "ids": ids}


# ------------------------------------------------------------------------------------------ render

def render_section(env: dict) -> str:
    """The `## Environment` section of the LLM.txt draft, built only from `env`."""
    out = ["## Environment", "",
           "<!-- Read by `al scout` from the repository's own setup files: tracked files only, nothing executed. "
           "The scout-codebase command completes UNVERIFIED cells; the proposal changes only to correct a fact. -->", "",
           "### Found", ""]
    found = []
    for s in env["sources"]:
        found.append(f"- `{_at(s['file'], s['line'])}` ({s['kind']}): {s['says']}")
        if s["kind"] == "devcontainer" and env["devcontainer"] and env["devcontainer"]["file"] == s["file"]:
            dc = env["devcontainer"]
            found += [f"  - honoured: {h}" for h in dc["honoured"]]
            found += [f"  - not honoured: {n['item']} — {n['reason']}" for n in dc["not_honoured"]]
            found += [f"  - problem: {p}" for p in dc["problems"]]
            if dc["container_env"]:
                found.append(f"  - containerEnv names: {', '.join(dc['container_env'])}")
            if dc["post_create"]:
                found.append(f"  - postCreateCommand: `{dc['post_create']}`")
            if dc["forward_ports"]:
                found.append(f"  - forwardPorts: {', '.join(map(str, dc['forward_ports']))}")
    found += [f"- note: {n}" for n in env["notes"]]
    out += found or ["- (nothing found)"]
    out += ["", "### Tools and versions", ""]
    tools = [f"- {tool}: " + ", ".join(f"{e['version']} ({e['at']})" for e in entries) for tool, entries in env["tools"].items()]
    pm = env["package_manager"]
    if pm:
        tools.append(f"- package manager: {pm['name']}{(' ' + pm['version']) if pm['version'] else ''} ({', '.join(pm['from'])})")
    if env["lockfiles"]:
        tools.append(f"- lockfiles: {', '.join(f'`{l}`' for l in env['lockfiles'])}")
    for p in env["prepare"]:
        tools.append(f"- prepare: `{p['shell']}` (cmd `{p['cmd_name']}`, cache `{p['cache']}`; from `{p['file']}`)")
    out += tools or ["- (none found)"]
    out += ["", "### Services", ""]
    out += [f"- `{s['id']}`: `{s['image']}` ({_at(s['file'], s['line'])})"
            + (f"; variables {', '.join(s['env_names'])} (values not copied)" if s["env_names"] else "")
            + (f"; ports {', '.join(s['ports'])}" if s["ports"] else "")
            + (f"; ready `{s['ready']}`" if s["ready"] else "; ready UNVERIFIED") for s in env["services"]] or ["- (none found)"]
    out += ["", "### System packages (from CI)", ""]
    out += [f"- `{p['name']}` ({_at(p['file'], p['line'])})" for p in env["system_packages"]] or ["- (none found)"]
    out += ["", "### Browser", ""]
    b = env["browser"]
    out.append(f"- needed: {b['why']}" if b["needed"] else "- not needed (no browser test dependency found)")
    out += ["", "### Variables a run needs (names only)", ""]
    out += [f"- `{f['file']}`: {', '.join(f['names'])}" for f in env["env_files"]] or ["- (none found)"]
    out += ["", "### Conflicts", ""]
    out += [f"- {c}" for c in env["conflicts"]] or ["- (none)"]
    p = env["proposal"]
    out += ["", "### Proposed declaration", "",
            "Inside `(codebase …)`:", "", "```racket", p["codebase"] or ";; (nothing to add)", "```", "",
            "At the top level:", "", "```racket", p["layer"], "```"]
    return "\n".join(out)
