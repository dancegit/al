"""Forge clients: issues, comments and pull requests (none / github / forgejo).

Stdlib only. `get_client(layer)` picks the client from `ir["forge"]["kind"]`.

    github  -> shells out to `gh` (GH_HOST for Enterprise, GH_TOKEN from tokenEnv)
    forgejo -> REST v1 via `http_request` (replace it in tests)
    none    -> no issues; comments go to <layer>/agents/forge-comments.log

Every comment the layer posts starts with the forge's `botMarker`, so triggers
can recognise (and ignore) the layer's own comments.

`client.commit_checks(sha)` reads the forge's CI on one commit (GitHub check runs and
commit statuses through `gh api`, Forgejo's combined status), normalised to
[{"name", "state": success | pending | failure | cancelled, "url"}]; `none` has no CI.
`wait_for_ci(client, sha, timeout_s)` polls it until everything succeeded (twice in a
row with the same names), something failed or was cancelled, or the time ran out
(`timeout` when something was reported, `missing` when nothing was). A promotion with
#:requires forge-ci uses it (see promotion.py).

`as_layer(x)` normalises what callers pass as "layer": an object with
`.ir/.layer_dir/.app_root` (what `ir.load` returns), or a layer directory path.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

DEFAULT_BOT_MARKER = "[ADW-BOT]"
CI_POLL_SECONDS = 30  # how often wait_for_ci asks the forge again
# seams, looked up at call time so tests can replace them
sleep = time.sleep
monotonic = time.monotonic


class ForgeError(RuntimeError):
    """A forge operation failed or is not supported by the configured forge."""


# --------------------------------------------------------------------------- layer


def as_layer(layer: Any) -> Any:
    """Return an object with `.ir` (dict), `.layer_dir` (Path), `.app_root` (Path|None).

    Accepts the core's loaded layer (or any duck-typed equivalent) unchanged,
    or a path to a layer directory, which is loaded with the core `ir.load`
    when available and by reading `agentic-layer.json` otherwise.
    """
    if hasattr(layer, "ir") and hasattr(layer, "layer_dir"):
        return layer
    if isinstance(layer, (str, os.PathLike)):
        layer_dir = Path(layer).resolve()
        try:
            from . import ir as ir_mod  # the core loader, when present

            return ir_mod.load(layer_dir)
        except ImportError:
            pass
        data = json.loads((layer_dir / "agentic-layer.json").read_text(encoding="utf-8"))
        root = (data.get("codebase") or {}).get("root") or ""
        app_root = (layer_dir / root).resolve() if root else None
        return SimpleNamespace(ir=data, layer_dir=layer_dir, app_root=app_root)
    raise TypeError(f"not a layer: {layer!r}")


# --------------------------------------------------------------------------- http


def http_request(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                 timeout: float = 30.0) -> tuple[int, Any]:
    """Perform one HTTP request; return (status, decoded JSON or None). Raises ForgeError on HTTP errors."""
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ForgeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ForgeError(f"{method} {url} failed: {exc.reason}") from exc
    if not raw.strip():
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- base


class ForgeClient:
    kind = "base"
    # how ship merges a pull request; a run that delivers into a stage gets the stage's #:entry
    merge_method = "squash"

    def __init__(self, option: Mapping[str, Any], layer_dir: Path | None = None,
                 env: Mapping[str, str] | None = None):
        self.option = dict(option)
        self.layer_dir = Path(layer_dir) if layer_dir else None
        self.env = dict(os.environ if env is None else env)
        self.bot_marker = self.option.get("botMarker") or DEFAULT_BOT_MARKER

    def mark(self, body: str) -> str:
        """Prefix a comment body with the bot marker (idempotent)."""
        return body if body.startswith(self.bot_marker) else f"{self.bot_marker} {body}"

    def is_bot(self, body: str | None) -> bool:
        return (body or "").lstrip().startswith(self.bot_marker)

    def get_issue(self, n: int) -> dict:
        raise NotImplementedError

    def list_issues(self, labels: Iterable[str] = ()) -> list[dict]:
        raise NotImplementedError

    def list_comments(self, n: int) -> list[dict]:
        raise NotImplementedError

    def comment(self, n: int, body: str) -> None:
        raise NotImplementedError

    def create_issue(self, title: str, body: str, labels: Iterable[str] = ()) -> dict:
        """Open an issue; return {"number", "url"}. The body is sent as given (no bot marker: a
        person asked for this issue)."""
        raise NotImplementedError

    def create_pr(self, branch: str, base: str, title: str, body: str, draft: bool = False,
                  reviewers: Iterable[str] = ()) -> dict:
        raise NotImplementedError

    def merge_pr(self, number: int, method: str | None = None) -> None:
        raise NotImplementedError

    def find_prs(self, head: str, base: str) -> list[dict]:
        """Every pull request from branch `head` into `base`, in any state, newest first:
        [{"number", "url", "state": open | merged | closed | unknown, "title", "body"}]."""
        raise ForgeError(f"forge {self.kind} cannot list pull requests")

    def update_pr(self, number: int, title: str, body: str) -> None:
        """Replace the title and body of pull request `number` (nothing else: never merges or closes)."""
        raise ForgeError(f"forge {self.kind} cannot update pull requests")

    def branch_protection(self, branch: str) -> dict:
        """{"protected": bool, "linear_history": bool|None, "push_allowed": bool|None} of a branch;
        {"protected": False} when it has no protection, {} when the forge cannot tell."""
        return {}

    def pr_approvals(self, number: int) -> list[dict]:
        """The approving reviews of pull request `number`: [{"by": login, "state": "APPROVED", "at": time}].
        [] when the forge cannot tell (see promotion.py, which counts them as approvals)."""
        return []

    def pr_state(self, number: int) -> dict:
        """Whether pull request `number` is merged, read only: {"state": open | merged | closed | unknown,
        "commit": merge commit sha, "at": merge time}. {"state": "unknown"} when the forge cannot tell."""
        return {"state": "unknown"}

    can_attach = False  # True when attach() really uploads (see ship.post_proof)

    def attach(self, n: int, path: Path, name: str | None = None, comment_id: Any = None) -> str:
        """Attach a file to issue / pull request `n` (or to one of its comments); return a URL
        usable in markdown (an image reference renders inline). "" = this forge cannot host
        files, callers leave the text as is."""
        return ""

    def edit_comment(self, comment_id: Any, body: str) -> None:
        raise ForgeError(f"forge {self.kind} cannot edit comments")

    def commit_checks(self, sha: str) -> list[dict]:
        """Every check run and commit status the forge reports for commit `sha`:
        [{"name", "state": success | pending | failure | cancelled, "url"}]."""
        raise ForgeError(f"forge {self.kind} has no CI status")


def multipart_file(field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    """(body, content type) of a multipart/form-data request carrying one file."""
    boundary = f"al-{secrets.token_hex(12)}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    quoted = filename.replace("\\", "\\\\").replace('"', '\\"')
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{quoted}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
    return head + data + f"\r\n--{boundary}--\r\n".encode("utf-8"), f"multipart/form-data; boundary={boundary}"


def _has_all_labels(issue: Mapping[str, Any], labels: Iterable[str]) -> bool:
    have = set(issue.get("labels") or [])
    return all(lbl in have for lbl in labels)


# --------------------------------------------------------------------------- none


class NoneForge(ForgeClient):
    kind = "none"

    def _unsupported(self, what: str):
        raise ForgeError(f"{what}: the layer declares (forge none), which has no issues; "
                         "declare (forge github …) or (forge forgejo …) to use issue input")

    def get_issue(self, n):
        self._unsupported("get_issue")

    def list_issues(self, labels=()):
        self._unsupported("list_issues")

    def list_comments(self, n):
        self._unsupported("list_comments")

    def comment(self, n, body):
        if self.layer_dir is None:
            raise ForgeError("forge none needs a layer directory to log comments")
        log = self.layer_dir / "agents" / "forge-comments.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} #{n} {self.mark(body)}\n")

    def create_issue(self, title, body, labels=()):
        self._unsupported("create_issue")

    def create_pr(self, branch, base, title, body, draft=False, reviewers=()):
        return {"number": 0, "url": ""}

    def merge_pr(self, number, method=None):
        return None

    def find_prs(self, head, base):
        return []

    def update_pr(self, number, title, body):
        self._unsupported("update_pr")

    def pr_approvals(self, number):
        return []


# --------------------------------------------------------------------------- github


class GitHubForge(ForgeClient):
    kind = "github"

    def __init__(self, option, layer_dir=None, env=None, run: Callable | None = None):
        super().__init__(option, layer_dir, env)
        self.repo = self.option["repo"]
        self.host = self.option.get("host") or "github.com"
        self._run = run

    def _env(self) -> dict:
        env = dict(self.env)
        token_env = self.option.get("tokenEnv") or ""
        if token_env and env.get(token_env):
            env["GH_TOKEN"] = env[token_env]
        if self.host != "github.com":
            env["GH_HOST"] = self.host
        return env

    def gh(self, *args: str, input: str | None = None) -> str:  # noqa: A002 — subprocess.run's own name
        argv = ["gh", *args, "--repo", self.repo]
        run = self._run or subprocess.run
        extra = {} if input is None else {"input": input}
        try:
            proc = run(argv, capture_output=True, text=True, env=self._env(), check=False, **extra)
        except FileNotFoundError as exc:
            raise ForgeError("the `gh` CLI is not installed or not on PATH") from exc
        if proc.returncode != 0:
            raise ForgeError(f"{' '.join(argv[:3])} failed ({proc.returncode}): {(proc.stderr or '').strip()[:500]}")
        return proc.stdout or ""

    @staticmethod
    def _issue(d: Mapping[str, Any]) -> dict:
        return {
            "number": int(d["number"]),
            "title": d.get("title") or "",
            "body": d.get("body") or "",
            "labels": [lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in d.get("labels") or []],
            "state": str(d.get("state") or "").lower(),
        }

    def get_issue(self, n):
        out = self.gh("issue", "view", str(n), "--json", "number,title,body,labels,state")
        return self._issue(json.loads(out))

    def list_issues(self, labels=()):
        args = ["issue", "list", "--state", "open", "--limit", "100",
                "--json", "number,title,body,labels,state"]
        for lbl in labels:
            args += ["--label", lbl]
        issues = [self._issue(d) for d in json.loads(self.gh(*args) or "[]")]
        return [i for i in issues if _has_all_labels(i, labels)]

    def list_comments(self, n):
        data = json.loads(self.gh("issue", "view", str(n), "--json", "comments") or "{}")
        return [{"id": c.get("id"), "body": c.get("body") or "",
                 "user": (c.get("author") or {}).get("login", "")} for c in data.get("comments") or []]

    def comment(self, n, body):
        self.gh("issue", "comment", str(n), "--body", self.mark(body))

    def create_issue(self, title, body, labels=()):
        """`gh issue create`; the body goes on stdin (--body-file -), never on the command line."""
        args = ["issue", "create", "--title", title, "--body-file", "-"]
        for lbl in labels:
            args += ["--label", lbl]
        out = self.gh(*args, input=body).strip()
        url = out.splitlines()[-1].strip() if out else ""
        m = re.search(r"/issues/(\d+)", url)
        return {"number": int(m.group(1)) if m else 0, "url": url}

    def create_pr(self, branch, base, title, body, draft=False, reviewers=()):
        args = ["pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body]
        if draft:
            args.append("--draft")
        for r in reviewers:
            args += ["--reviewer", r]
        out = self.gh(*args).strip()
        url = out.splitlines()[-1].strip() if out else ""
        m = re.search(r"/pull/(\d+)", url)
        return {"number": int(m.group(1)) if m else 0, "url": url}

    def merge_pr(self, number, method=None):
        method = method or self.merge_method
        if method not in ("squash", "merge", "rebase"):
            raise ForgeError(f"unknown merge method {method!r}")
        self.gh("pr", "merge", str(number), f"--{method}")

    def find_prs(self, head, base):
        out = self.gh("pr", "list", "--head", head, "--base", base, "--state", "all",
                      "--json", "number,url,state,title,body", "--limit", "20")
        data = json.loads(out or "[]")
        found = []
        for d in data if isinstance(data, list) else []:
            if not isinstance(d, dict) or not d.get("number"):
                continue
            state = str(d.get("state") or "").lower()
            found.append({"number": int(d["number"]), "url": str(d.get("url") or ""),
                          "state": state if state in ("open", "merged", "closed") else "unknown",
                          "title": d.get("title") or "", "body": d.get("body") or ""})
        return sorted(found, key=lambda p: p["number"], reverse=True)

    def update_pr(self, number, title, body):
        """`gh pr edit`; the body goes on stdin (--body-file -), never on the command line."""
        self.gh("pr", "edit", str(int(number)), "--title", title, "--body-file", "-", input=body)

    def pr_approvals(self, number):
        data = json.loads(self.gh("pr", "view", str(number), "--json", "reviews") or "{}")
        return [{"by": str((r.get("author") or {}).get("login") or ""), "state": "APPROVED",
                 "at": str(r.get("submittedAt") or "")}
                for r in (data.get("reviews") if isinstance(data, dict) else None) or []
                if isinstance(r, dict) and str(r.get("state") or "").upper() == "APPROVED"]

    def pr_state(self, number):
        data = json.loads(self.gh("pr", "view", str(int(number)), "--json", "state,mergedAt,mergeCommit") or "{}")
        if not isinstance(data, dict):
            return {"state": "unknown"}
        state = str(data.get("state") or "").lower()
        commit = str((data.get("mergeCommit") or {}).get("oid") or "") if isinstance(data.get("mergeCommit"), dict) else ""
        return {"state": state if state in ("open", "merged", "closed") else "unknown", "commit": commit,
                "at": str(data.get("mergedAt") or "")}

    def _api(self, path: str) -> Any:
        """`gh api PATH` (no --repo: the path names the repository); parsed JSON."""
        argv = ["gh", "api", path]
        run = self._run or subprocess.run
        try:
            proc = run(argv, capture_output=True, text=True, env=self._env(), check=False)
        except FileNotFoundError as exc:
            raise ForgeError("the `gh` CLI is not installed or not on PATH") from exc
        if proc.returncode != 0:
            raise ForgeError(f"gh api {path} failed ({proc.returncode}): {(proc.stderr or '').strip()[:500]}")
        try:
            return json.loads(proc.stdout or "null")
        except ValueError as exc:
            raise ForgeError(f"gh api {path} returned no JSON: {(proc.stdout or '')[:200]}") from exc

    def commit_checks(self, sha):
        runs = self._api(f"repos/{self.repo}/commits/{sha}/check-runs?per_page=100")
        status = self._api(f"repos/{self.repo}/commits/{sha}/status")
        checks: list[dict] = []
        for item in (runs.get("check_runs") if isinstance(runs, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            conclusion = str(item.get("conclusion") or "").lower()
            if str(item.get("status") or "").lower() != "completed":
                state = "pending"
            elif conclusion in ("success", "neutral", "skipped"):
                state = "success"
            elif conclusion == "cancelled":
                state = "cancelled"
            else:
                state = "failure"
            checks.append({"name": str(item.get("name") or ""), "state": state,
                           "url": str(item.get("html_url") or item.get("details_url") or "")})
        for item in (status.get("statuses") if isinstance(status, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("state") or "").lower()
            state = {"pending": "pending", "success": "success"}.get(raw, "failure")
            checks.append({"name": str(item.get("context") or ""), "state": state,
                           "url": str(item.get("target_url") or "")})
        return checks

    def branch_protection(self, branch):
        argv = ["gh", "api", f"repos/{self.repo}/branches/{urllib.parse.quote(branch, safe='')}/protection"]
        run = self._run or subprocess.run
        try:
            proc = run(argv, capture_output=True, text=True, env=self._env(), check=False)
        except FileNotFoundError as exc:
            raise ForgeError("the `gh` CLI is not installed or not on PATH") from exc
        if proc.returncode != 0:
            if "404" in (proc.stderr or "") or "Not Found" in (proc.stderr or "") or "not protected" in (proc.stderr or ""):
                return {"protected": False}
            raise ForgeError(f"gh api …/protection failed ({proc.returncode}): {(proc.stderr or '').strip()[:500]}")
        data = json.loads(proc.stdout or "{}")
        linear = (data.get("required_linear_history") or {}).get("enabled") if isinstance(data, dict) else None
        return {"protected": True, "linear_history": None if linear is None else bool(linear), "push_allowed": None}

    def attach(self, n, path, name=None, comment_id=None):
        # GitHub has no API (nor `gh` command) for uploading files to issues or pull requests.
        return ""


# --------------------------------------------------------------------------- forgejo


class ForgejoForge(ForgeClient):
    kind = "forgejo"

    def __init__(self, option, layer_dir=None, env=None, http: Callable | None = None):
        super().__init__(option, layer_dir, env)
        self.host = (self.option.get("host") or "").rstrip("/")
        if not self.host:
            raise ForgeError("forge forgejo needs #:host")
        self.repo = self.option["repo"]
        self._http = http

    @property
    def base(self) -> str:
        return f"{self.host}/api/v1/repos/{self.repo}"

    def headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        token = self.env.get(self.option.get("tokenEnv") or "FORGEJO_TOKEN")
        if token:
            h["Authorization"] = f"token {token}"
        return h

    def request(self, method: str, path: str, payload: Any = None, query: Mapping[str, Any] | None = None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        http = self._http or http_request  # looked up at call time so tests can patch the module
        _status, data = http(method, url, self.headers(), body)
        return data

    @staticmethod
    def _issue(d: Mapping[str, Any]) -> dict:
        return {
            "number": int(d["number"]),
            "title": d.get("title") or "",
            "body": d.get("body") or "",
            "labels": [lbl.get("name", "") for lbl in d.get("labels") or []],
            "state": str(d.get("state") or "").lower(),
        }

    def get_issue(self, n):
        return self._issue(self.request("GET", f"/issues/{int(n)}"))

    def list_issues(self, labels=()):
        query = {"state": "open", "type": "issues", "limit": 50}
        labels = list(labels)
        if labels:
            query["labels"] = ",".join(labels)
        issues = [self._issue(d) for d in self.request("GET", "/issues", query=query) or []]
        return [i for i in issues if _has_all_labels(i, labels)]

    def list_comments(self, n):
        data = self.request("GET", f"/issues/{int(n)}/comments") or []
        return [{"id": c.get("id"), "body": c.get("body") or "",
                 "user": (c.get("user") or {}).get("login", "")} for c in data]

    def comment(self, n, body):
        """Post a comment; returns its id (needed to attach files to it or edit it)."""
        data = self.request("POST", f"/issues/{int(n)}/comments", {"body": self.mark(body)})
        return data.get("id") if isinstance(data, dict) else None

    def edit_comment(self, comment_id, body):
        self.request("PATCH", f"/issues/comments/{int(comment_id)}", {"body": self.mark(body)})

    def create_issue(self, title, body, labels=()):
        """POST /issues; Forgejo takes label ids, so the names are looked up in the repository's labels."""
        wanted = [lbl for lbl in labels if lbl]
        ids = []
        if wanted:
            known = {str(item.get("name")): item.get("id") for item in self.request("GET", "/labels") or []
                     if isinstance(item, dict)}
            missing = [lbl for lbl in wanted if lbl not in known]
            if missing:
                raise ForgeError(f"the repository {self.repo} has no label {', '.join(missing)}; create it first")
            ids = [int(known[lbl]) for lbl in wanted]
        data = self.request("POST", "/issues", {"title": title, "body": body, "labels": ids})
        if not isinstance(data, dict) or not data.get("number"):
            raise ForgeError(f"creating an issue returned no number: {str(data)[:200]}")
        return {"number": int(data["number"]), "url": data.get("html_url") or data.get("url") or ""}

    def create_pr(self, branch, base, title, body, draft=False, reviewers=()):
        # The Forgejo API has no draft flag; a "WIP: " title prefix is the forge's draft convention.
        if draft and not title.startswith("WIP:"):
            title = f"WIP: {title}"
        data = self.request("POST", "/pulls", {"head": branch, "base": base, "title": title, "body": body})
        number = int(data.get("number") or 0)
        reviewers = list(reviewers)
        if reviewers and number:
            self.request("POST", f"/pulls/{number}/requested_reviewers", {"reviewers": reviewers})
        return {"number": number, "url": data.get("html_url") or data.get("url") or ""}

    def merge_pr(self, number, method=None):
        method = method or self.merge_method
        if method not in ("squash", "merge", "rebase"):
            raise ForgeError(f"unknown merge method {method!r}")
        self.request("POST", f"/pulls/{int(number)}/merge", {"Do": method})

    def find_prs(self, head, base):
        data = self.request("GET", "/pulls", query={"state": "all", "limit": 50}) or []
        found = []
        for d in data if isinstance(data, list) else []:
            if not isinstance(d, dict) or not d.get("number"):
                continue
            if ((d.get("head") or {}).get("ref"), (d.get("base") or {}).get("ref")) != (head, base):
                continue
            if d.get("merged"):
                state = "merged"
            else:
                state = str(d.get("state") or "").lower()
                state = state if state in ("open", "closed") else "unknown"
            found.append({"number": int(d["number"]), "url": str(d.get("html_url") or d.get("url") or ""),
                          "state": state, "title": d.get("title") or "", "body": d.get("body") or ""})
        return sorted(found, key=lambda p: p["number"], reverse=True)

    def update_pr(self, number, title, body):
        self.request("PATCH", f"/pulls/{int(number)}", {"title": title, "body": body})

    def pr_approvals(self, number):
        data = self.request("GET", f"/pulls/{int(number)}/reviews") or []
        return [{"by": str((r.get("user") or {}).get("login") or ""), "state": "APPROVED",
                 "at": str(r.get("submitted_at") or "")}
                for r in data if isinstance(r, dict) and str(r.get("state") or "").upper() == "APPROVED"]

    def pr_state(self, number):
        data = self.request("GET", f"/pulls/{int(number)}")
        if not isinstance(data, dict):
            return {"state": "unknown"}
        if data.get("merged"):
            state = "merged"
        else:
            state = str(data.get("state") or "").lower()
            state = state if state in ("open", "closed") else "unknown"
        return {"state": state, "commit": str(data.get("merge_commit_sha") or ""), "at": str(data.get("merged_at") or "")}

    def commit_checks(self, sha):
        data = self.request("GET", f"/commits/{sha}/status")
        checks: list[dict] = []
        for item in (data.get("statuses") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("status") or item.get("state") or "").lower()
            state = {"pending": "pending", "success": "success"}.get(raw, "failure")
            checks.append({"name": str(item.get("context") or ""), "state": state,
                           "url": str(item.get("target_url") or "")})
        return checks

    def branch_protection(self, branch):
        try:
            data = self.request("GET", f"/branch_protections/{urllib.parse.quote(branch, safe='')}")
        except ForgeError as exc:
            if "HTTP 404" in str(exc):
                return {"protected": False}
            raise
        repo = self.request("GET", "")
        merges = repo.get("allow_merge_commits") if isinstance(repo, dict) else None
        push = data.get("enable_push") if isinstance(data, dict) else None
        return {"protected": True, "linear_history": None if merges is None else not merges,
                "push_allowed": None if push is None else bool(push)}

    can_attach = True

    def attach(self, n, path, name=None, comment_id=None):
        """POST …/issues/{n}/assets (pull requests are issues too), or …/issues/comments/{id}/assets
        to attach to one comment (the file is then listed with that comment, not the description).
        The returned browser_download_url is public for a public repository."""
        path = Path(path)
        name = name or path.name
        body, content_type = multipart_file("attachment", name, path.read_bytes())
        headers = {**self.headers(), "Content-Type": content_type}
        target = f"/issues/comments/{int(comment_id)}" if comment_id is not None else f"/issues/{int(n)}"
        url = f"{self.base}{target}/assets?" + urllib.parse.urlencode({"name": name})
        http = self._http or http_request
        _status, data = http("POST", url, headers, body)
        link = (data or {}).get("browser_download_url") if isinstance(data, dict) else None
        if not link:
            raise ForgeError(f"attachment upload for #{n} returned no browser_download_url: {str(data)[:200]}")
        return str(link)


# --------------------------------------------------------------------------- CI


def wait_for_ci(client: ForgeClient, sha: str, timeout_s: float, poll_s: float | None = None) -> dict:
    """Poll the forge's CI on commit `sha` until it decides, or `timeout_s` seconds passed.

    failed: a run failed (at once); cancelled: a run was cancelled (at once); passed: runs were
    reported, all succeeded, and the same names were reported by the poll before; timeout: runs
    were reported but not all finished in time; missing: nothing was reported in time. A
    ForgeError counts as no answer; the last one is kept in `error`.
    """
    start = monotonic()
    deadline = start + max(0.0, float(timeout_s))
    runs: list[dict] = []
    error = ""
    previous: set[str] | None = None
    reported = False
    outcome = ""
    while True:
        try:
            runs = list(client.commit_checks(sha))
        except ForgeError as exc:
            error = str(exc)[:500]
            runs = []
        states = {run.get("state") for run in runs}
        names = {str(run.get("name") or "") for run in runs}
        reported = reported or bool(runs)
        if "failure" in states:
            outcome = "failed"
        elif "cancelled" in states:
            outcome = "cancelled"
        elif runs and states == {"success"} and names == previous:
            outcome = "passed"
        elif monotonic() >= deadline:
            outcome = "timeout" if reported else "missing"
        if outcome:
            break
        previous = names if runs and states == {"success"} else None
        sleep(max(0.0, min(float(poll_s or CI_POLL_SECONDS), deadline - monotonic())))
    return {"commit": sha, "outcome": outcome, "runs": runs, "waited_s": int(monotonic() - start), "error": error}


# --------------------------------------------------------------------------- factory

CLIENTS = {"none": NoneForge, "github": GitHubForge, "forgejo": ForgejoForge}


def get_client(layer: Any, env: Mapping[str, str] | None = None) -> ForgeClient:
    """Return the forge client for the layer's `forge` option (default: none)."""
    lay = as_layer(layer)
    option = (lay.ir.get("forge") or {"kind": "none", "id": "none", "remote": ""})
    kind = option.get("kind", "none")
    cls = CLIENTS.get(kind)
    if cls is None:
        raise ForgeError(f"forge kind {kind!r} is not supported by this runtime")
    client = cls(option, layer_dir=lay.layer_dir, env=os.environ if env is None else env)
    entry = (getattr(lay, "delivery", None) or {}).get("entry")
    if entry in ("merge", "squash", "rebase"):
        client.merge_method = entry  # the run delivers into a stage that takes changes this way
    return client
