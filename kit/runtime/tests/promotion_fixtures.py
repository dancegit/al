"""Helpers for the promotion tests: a layer that delivers into a sprint (the compiler's real stages
and promotion, data/delivery-sprint.json), real delivered runs (runner.main with mock scripts), and
a fake Forgejo that records pull requests, reviews, comments, assets and merges."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adw_modules import delivery, ir, promotion
from adw_modules.runner import main as run_main

DATA = Path(__file__).resolve().parent / "data"
SPRINT = "sprint/2026-s47"
NOW = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)
KEEPACHANGELOG = ("# Changelog\n\nAll notable changes are documented here; the format follows "
                  "[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).\n\n## [Unreleased]\n\n"
                  "## [0.1.0] - 2026-01-02\n\n### Added\n\n- The first release\n")


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                            capture_output=True, text=True, timeout=60)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit_on(app: Path, branch: str, files: dict[str, str], message: str = "") -> str:
    """A commit on `branch` of the app made in a throwaway worktree (the app checkout is not touched)."""
    tree = Path(tempfile.mkdtemp(prefix="helper-", dir=app.parent))
    tree.rmdir()
    git(app, "worktree", "add", str(tree), branch)
    for name, content in files.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(content, encoding="utf-8")
    git(tree, "add", "-A")
    git(tree, "commit", "-m", message or f"{branch}: {', '.join(files)}")
    commit = git(tree, "rev-parse", "HEAD")
    git(app, "worktree", "remove", "--force", str(tree))
    return commit


def delivering_adw(name: str = "demo", **extra: Any) -> dict[str, Any]:
    return {"name": name, "agent": "mock", "review": "policy", "environment": ["worktree"],
            "classes": ["chore", "bug", "feature"], "steps": [
                {"name": "plan", "kind": "run", "command": "plan"},
                {"name": "build", "kind": "run", "command": "build"},
                {"name": "test", "kind": "check", "cmd": "test"},
                {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"},
                {"name": "ship", "kind": "ship"}], **extra}


def sprint_layer(make_layer, app_repo: Path, *, promotion: dict[str, Any] | None = None,
                 stages: list[dict[str, Any]] | None = None, promotions: list[dict[str, Any]] | None = None,
                 adws: list[dict[str, Any]] | None = None, extra: dict[str, Any] | None = None,
                 files: dict[str, str] | None = None, open_cycle: bool = True, leave_main: bool = True) -> Path:
    """A layer with the sprint preset's stages and its compiled promotion (overridden by `promotion`),
    the app with VERSION and a keepachangelog CHANGELOG.md, the sprint cycle 2026-s47 open, and the
    app's checkout on a branch of its own (so main and the sprint are not checked out)."""
    data = json.loads((DATA / "delivery-sprint.json").read_text(encoding="utf-8"))
    promos = promotions if promotions is not None else [{**data["promotions"][0], "adw": "demo", **(promotion or {})}]
    overrides = {
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": n, "template": t, "modelSet": "base", "output": o} for n, t, o in (
            ("plan", "plan", "path"), ("build", "build", "text"), ("review", "review", "json"),
            ("patch", "plan-patch", "text"), ("implement", "implement", "text"))],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 20},
                                  {"name": "deploy", "run": "./deploy.sh", "timeout": 20}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore", "bug", "feature", "patch"],
                     "blockProtectedPaths": True, "maxDiffLines": 2000, "requireChecks": ["test"], "ship": "merge",
                     "deployCmd": ""}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                          "portBase": 27500, "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "concurrency": {"maxRuns": 8, "maxAgents": 4, "maxChecks": 4, "waitSeconds": 60},
        "stages": stages if stages is not None else data["stages"], "cycle": data["cycle"],
        "delivery": data["delivery"], "promotions": promos, "adws": adws or [delivering_adw()],
        **(extra or {}),
    }
    layer = make_layer(overrides)
    base = {"VERSION": "0.1.0\n", "CHANGELOG.md": KEEPACHANGELOG, **(files or {})}
    for name, content in base.items():
        (app_repo / name).write_text(content, encoding="utf-8")
    git(app_repo, "add", "-A")
    git(app_repo, "commit", "-q", "-m", "release 0.1.0")
    if open_cycle and any("{cycle}" in s["branch"] for s in overrides["stages"]):
        delivery.cycle_start(ir.load(layer), name="2026-s47", now=NOW)
    if leave_main:
        git(app_repo, "checkout", "-q", "-b", "work")
    return layer


def script(layer: Path, step: str, data: dict[str, Any], adw: str = "") -> Path:
    path = layer / "mock" / (adw or "") / f"{step}.json" if adw else layer / "mock" / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def plan_text(prompt: str, criteria: list[str] | None, done: str = "") -> str:
    lines = [f"# Plan: {prompt}", "", done or f"Adds what was asked: {prompt}.", ""]
    if criteria is not None:
        lines += ["## Acceptance Criteria", "", *[f"- {c}" for c in criteria], "", "## Notes", "", "- not a criterion"]
    return "\n".join(lines) + "\n"


def deliver(layer: Path, app: Path, adw_id: str, files: dict[str, str], *, prompt: str = "",
            criteria: list[str] | None = None, klass: str = "feature", shots: list[str] = (),
            video: str = "", adw: str = "demo", risks: list[str] = (), expect: int = 0) -> dict[str, Any]:
    """A real delivered run: plan (with acceptance criteria), build (the files), check, auto review
    (with `risks` as tech-debt issues), ship into the sprint. Pictures and a recording are then added
    to the run's artefacts as a check step would have collected them."""
    del app
    prompt = prompt or f"Add {', '.join(files)}"
    plan = f"specs/plan-{adw_id}.md"
    script(layer, "plan", {"layer_files": {plan: plan_text(prompt, criteria)}, "output": plan}, adw)
    script(layer, "build", {"files": files}, adw)
    verdict = {"success": True, "summary": "looks right",
               "issues": [{"severity": "tech_debt", "description": r, "resolution": "later"} for r in risks]}
    script(layer, "review-1", {"output": json.dumps(verdict)}, adw)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run_main(adw, layer, [prompt, "--class", klass, "--adw-id", adw_id])
    assert code == expect, out.getvalue()
    state_path = layer / "agents" / adw_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if shots or video:
        root = layer / "artefacts" / adw_id
        for name in shots:
            target = root / "screenshots" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(DATA / "tiny.png", target)
            state["artefacts"].append({"id": "screenshots", "type": "image", "step": "test",
                                       "path": target.relative_to(layer).as_posix(), "bytes": target.stat().st_size})
        if video:
            target = root / "recordings" / video
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(DATA / "tiny.webm", target)
            frames = []
            for index in (1, 2):
                frame = root / "recordings" / f"{Path(video).stem}.keyframes" / f"frame-{index}.png"
                frame.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(DATA / "tiny.png", frame)
                frames.append({"path": frame.relative_to(layer).as_posix(), "bytes": frame.stat().st_size})
            state["artefacts"].append({"id": "recordings", "type": "video", "step": "test",
                                       "path": target.relative_to(layer).as_posix(), "bytes": target.stat().st_size,
                                       "keyframes": frames})
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def promote(layer: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = promotion.main(layer, ["run", *argv])
    return code, out.getvalue() + err.getvalue()


def decide(layer: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = promotion.main(layer, ["decide", *argv])
    return code, out.getvalue() + err.getvalue()


def state_of(layer: Path, adw_id: str) -> dict[str, Any]:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text(encoding="utf-8"))


def events_of(layer: Path, adw_id: str) -> list[dict[str, Any]]:
    path = layer / "agents" / adw_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def open_id(layer: Path, from_: str = "sprint", to: str = "main") -> str:
    data = json.loads((layer / "agents" / "promotions" / f"{from_}-{to}.json").read_text(encoding="utf-8"))
    return str(data["id"])


def refs(app: Path) -> str:
    return git(app, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags")


class FakeForgejoPulls:
    """An `http_request` replacement for ForgejoForge: records pulls, reviews, comments, assets and
    merges; a merge is a real `git merge --no-ff` in the bare remote (through a scratch clone)."""

    def __init__(self, remote: Path, scratch: Path) -> None:
        self.remote, self.scratch = Path(remote), Path(scratch)
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.reviews: dict[int, list[dict[str, Any]]] = {}
        self.assets: list[dict[str, Any]] = []
        self.merges: list[dict[str, Any]] = []
        self.next_id = 100

    def _merge(self, number: int, method: str) -> None:
        pull = self.pulls[number]
        clone = self.scratch / f"merge-{number}"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(clone)], check=True, capture_output=True)
        git(clone, "checkout", "-q", pull["base"])
        if method == "squash":
            git(clone, "merge", "--squash", f"origin/{pull['head']}")
            git(clone, "commit", "-m", f"{pull['title']} (#{number})")
        else:
            git(clone, "merge", "--no-ff", "-m", f"Merge pull request #{number}", f"origin/{pull['head']}")
        git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{pull['base']}")
        shutil.rmtree(clone)

    def __call__(self, method: str, url: str, headers: Any, body: bytes | None, timeout: float = 30.0):
        path = url.split("/api/v1/repos/", 1)[1].split("/", 2)[2] if "/api/v1/repos/" in url else url
        path = "/" + path.split("?", 1)[0]
        payload = json.loads(body) if body and headers.get("Content-Type") == "application/json" else None
        if method == "POST" and path == "/pulls":
            number = len(self.pulls) + 1
            self.pulls[number] = {"number": number, **payload}
            return 201, {"number": number, "html_url": f"https://forge.example.org/acme/webshop/pulls/{number}"}
        parts = path.strip("/").split("/")
        if parts[:1] == ["issues"] and len(parts) == 3 and parts[2] == "comments":
            number = int(parts[1])
            if method == "GET":
                return 200, copy.deepcopy(self.comments.get(number, []))
            self.next_id += 1
            item = {"id": self.next_id, "body": payload["body"], "user": {"login": "al-bot"}}
            self.comments.setdefault(number, []).append(item)
            return 201, {"id": self.next_id}
        if parts[:2] == ["issues", "comments"] and method == "PATCH":
            for items in self.comments.values():
                for item in items:
                    if item["id"] == int(parts[2]):
                        item["body"] = payload["body"]
            return 200, {}
        if parts[-1] == "assets":
            name = url.split("name=", 1)[1] if "name=" in url else "file"
            self.assets.append({"path": path, "name": name, "bytes": len(body or b"")})
            return 201, {"browser_download_url": f"https://forge.example.org/attachments/{len(self.assets)}-{name}"}
        if parts[:1] == ["pulls"] and parts[-1] == "reviews":
            return 200, copy.deepcopy(self.reviews.get(int(parts[1]), []))
        if parts[:1] == ["pulls"] and parts[-1] == "merge":
            self.merges.append({"number": int(parts[1]), "method": payload["Do"]})
            self._merge(int(parts[1]), payload["Do"])
            return 200, None
        raise AssertionError(f"unexpected forge call {method} {path}")

    def person_comments(self, number: int, user: str, body: str) -> None:
        self.next_id += 1
        self.comments.setdefault(number, []).append({"id": self.next_id, "body": body, "user": {"login": user}})

    def approve(self, number: int, user: str) -> None:
        self.reviews.setdefault(number, []).append({"user": {"login": user}, "state": "APPROVED",
                                                    "submitted_at": "2026-11-16T10:00:00Z"})
