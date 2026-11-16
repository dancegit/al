"""The branching style a repository already uses, for `al scout`: which delivery preset fits it.

Read-only: only `git for-each-ref`, `git log`, `git tag`, `git merge-base` and `git rev-list`
(counting), plus reading tracked files (CI workflow files are scanned with regular expressions, no
YAML parser). With `al scout --layer LAYER` and a declared forge, the branch protection of the main
branch is asked through that layer's own forge module; nothing else goes over the network.

`detect` returns {"style", "preset", "proposal", "evidence", "against", "scores", "unverified"};
`render_section` writes the `## Delivery` section of the LLM.txt draft from the same dict, so the
text and `al scout --json` cannot drift. Standard library only.
"""
from __future__ import annotations

import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

import al_tools   # tools/ is on sys.path when tools/al (or the dashboard) loads this module

STYLES = ("trunk", "sprint", "gitflow", "release-branch", "phase-gate")
PRESET = {"trunk": "trunk", "sprint": "sprint", "gitflow": "gitflow", "release-branch": "release-train",
          "phase-gate": "phase-gate"}
PREFIXES = ("sprint/", "sprint-", "develop", "release/", "release-", "hotfix/", "phase/", "feature/", "train/")
REPEATING = ("sprint/", "sprint-", "release/", "release-", "train/", "phase/")
SEMVER = re.compile(r"^v?\d+\.\d+(\.\d+)?$")
DATE_TAG = re.compile(r"^v?\d{4}[-.]?\d{2}[-.]?\d{2}")
MERGE_SUBJECTS = (re.compile(r"^Merge branch '([^']+)'"), re.compile(r"^Merge pull request #\d+ from [^/\s]+/(\S+)"),
                  re.compile(r"^Merge remote-tracking branch '[^/']+/([^']+)'"))
CI_DIRS = (".github/workflows/", ".forgejo/workflows/", ".gitea/workflows/")
DOC_FILES = re.compile(r"^(CONTRIBUTING[^/]*|docs/contributing[^/]*|HACKING\.md|README[^/]*)$", re.I)
DOC_HINTS = {"develop": r"\bdevelop\b(?! ?(?:ment|er|ing))", "release branch": r"\brelease[ -]branch",
             "sprint": r"\bsprints?\b", "feature flag": r"\bfeature[ -]flags?\b", "trunk": r"\btrunk\b",
             "hotfix": r"\bhot ?fix"}
DAY = 86400


def _git(app: Path, *args: str) -> str:
    result = subprocess.run(al_tools.argv(["git", "-C", str(app), *args]), capture_output=True, text=True, timeout=120,
                            check=False)
    return result.stdout if result.returncode == 0 else ""


def _read(app: Path, rel: str) -> str:
    try:
        return (app / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _branches(app: Path) -> dict[str, str]:
    """Branch name → full ref, over refs/heads and refs/remotes (remote names stripped; local wins)."""
    found: dict[str, str] = {}
    for ref in _git(app, "for-each-ref", "--format=%(refname)", "refs/remotes", "refs/heads").split():
        if ref.endswith("/HEAD"):
            continue
        if ref.startswith("refs/heads/"):
            found[ref.removeprefix("refs/heads/")] = ref
        else:
            name = ref.removeprefix("refs/remotes/").split("/", 1)[-1]
            found.setdefault(name, ref)
    return found


def _prefix(name: str) -> str | None:
    for prefix in PREFIXES:
        if name == prefix or (prefix.endswith(("/", "-")) and name.startswith(prefix)):
            return prefix
    return None


def _cut_dates(app: Path, branches: dict[str, str], names: list[str], default_ref: str) -> list[tuple[str, int]]:
    """(branch, commit time of its merge base with the default branch), oldest first."""
    cuts = []
    for name in names:
        base = _git(app, "merge-base", default_ref, branches[name]).strip()
        stamp = _git(app, "log", "-1", "--format=%ct", base).strip() if base else ""
        if stamp.isdigit():
            cuts.append((name, int(stamp)))
    return sorted(cuts, key=lambda item: item[1])


def _merges(app: Path, ref: str) -> dict[str, Any]:
    """Branches merged into `ref` (first parent), and the share of merge commits there."""
    sources = []
    for line in _git(app, "log", "--first-parent", "--merges", "-n", "500", "--format=%s%x00%ct", ref).splitlines():
        subject = line.split("\x00", 1)[0]
        for pattern in MERGE_SUBJECTS:
            match = pattern.match(subject)
            if match:
                sources.append(match.group(1))
                break
    total = _git(app, "rev-list", "--first-parent", "--count", ref).strip()
    merges = len(_git(app, "log", "--first-parent", "--merges", "-n", "500", "--format=%H", ref).split())
    share = merges / int(total) if total.isdigit() and int(total) else 0.0
    return {"sources": sources, "share": round(share, 2), "merges": merges}


def _tags(app: Path) -> dict[str, Any]:
    names = _git(app, "tag", "--list").split()
    semver = [t for t in names if SEMVER.match(t)]
    dated = [t for t in names if DATE_TAG.match(t) and t not in semver]
    on_release = []
    for tag in semver:
        refs = _git(app, "for-each-ref", "--contains", f"refs/tags/{tag}", "--format=%(refname)",
                    "refs/heads/release", "refs/heads/release-*", "refs/remotes").split()
        if any("/release/" in ref or "/release-" in ref for ref in refs):
            on_release.append(tag)
    return {"semver": semver, "dated": dated, "on_release": on_release}


def _ci_branches(app: Path, tracked: list[str]) -> dict[str, list[str]]:
    """CI file → branch names its triggers list (`branches:` lists; GitLab only:/if rules)."""
    found: dict[str, list[str]] = {}
    for rel in tracked:
        if not ((rel.startswith(CI_DIRS) and rel.endswith((".yml", ".yaml"))) or rel == ".gitlab-ci.yml"):
            continue
        text = _read(app, rel)
        names: list[str] = []
        for match in re.finditer(r"branches\s*:\s*\[([^\]]*)\]", text):
            names += [n.strip().strip("'\"") for n in match.group(1).split(",") if n.strip()]
        for match in re.finditer(r"branches\s*:\s*\n((?:\s+-\s*.+\n?)+)", text):
            names += [n.strip().lstrip("-").strip().strip("'\"") for n in match.group(1).splitlines() if n.strip()]
        if rel == ".gitlab-ci.yml":
            names += re.findall(r"\$CI_COMMIT_BRANCH\s*==\s*['\"]([^'\"]+)['\"]", text)
            for match in re.finditer(r"only\s*:\s*\n((?:\s+-\s*.+\n?)+)", text):
                names += [n.strip().lstrip("-").strip() for n in match.group(1).splitlines() if n.strip()]
        if names:
            found[rel] = list(dict.fromkeys(names))
    return found


def _docs_hints(app: Path, tracked: list[str]) -> dict[str, list[str]]:
    """Contribution-doc mentions: hint → files that mention it."""
    found: dict[str, list[str]] = {}
    for rel in tracked:
        if not DOC_FILES.match(rel):
            continue
        text = _read(app, rel)
        for hint, pattern in DOC_HINTS.items():
            if re.search(pattern, text, re.I):
                found.setdefault(hint, []).append(rel)
    return found


def _protection(layer: Path | None, branches: list[str]) -> dict[str, Any]:
    """Branch protection through the layer's own forge module; {} without --layer or forge."""
    if layer is None:
        return {}
    layer = Path(layer).resolve()
    if not (layer / "adws/adw_modules/forge.py").is_file():
        return {"note": f"{layer} is not a generated layer; branch protection not asked"}
    for name in [n for n in sys.modules if n == "adw_modules" or n.startswith("adw_modules.")]:
        del sys.modules[name]
    sys.path.insert(0, str(layer / "adws"))
    try:
        from adw_modules import forge, ir as ir_module

        loaded = ir_module.load(layer)
        if (loaded.ir.get("forge") or {}).get("kind", "none") == "none":
            return {"note": "the layer declares no forge; branch protection not asked"}
        client = forge.get_client(loaded)
        return {branch: client.branch_protection(branch) for branch in branches}
    except Exception as exc:  # noqa: BLE001 — detection is advice; a forge that cannot answer is a note
        return {"note": f"branch protection unknown: {type(exc).__name__}: {exc}"[:300]}
    finally:
        with_path = str(layer / "adws")
        if with_path in sys.path:
            sys.path.remove(with_path)


def _score(facts: dict[str, Any]) -> tuple[dict[str, int], dict[str, list[str]], list[str]]:
    """Points, evidence per style, and what speaks against the leading style's rivals."""
    scores = {style: 0 for style in STYLES}
    evidence: dict[str, list[str]] = {style: [] for style in STYLES}
    groups, default = facts["groups"], facts["default_branch"]
    docs, ci, tags = facts["docs"], facts["ci"], facts["tags"]
    merged = facts["merges"]["sources"]

    sprints = groups.get("sprint/", []) + groups.get("sprint-", [])
    if len(sprints) >= 2:
        scores["sprint"] += 3 + len(sprints)
        evidence["sprint"].append(f"{len(sprints)} sprint branches: {', '.join(sprints[:8])}")
        if facts.get("gap_days", {}).get("sprint"):
            evidence["sprint"].append(f"cut every {facts['gap_days']['sprint']} days from {default} (median gap)")
    if "sprint" in docs:
        scores["sprint"] += 1
        evidence["sprint"].append(f"the contribution docs mention sprints ({', '.join(docs['sprint'])})")

    phases = groups.get("phase/", [])
    if len(phases) >= 2:
        scores["phase-gate"] += 3 + len(phases)
        evidence["phase-gate"].append(f"{len(phases)} phase branches: {', '.join(phases)}")

    has_develop = bool(groups.get("develop"))
    releases = groups.get("release/", []) + groups.get("release-", [])
    if has_develop:
        scores["gitflow"] += 3
        evidence["gitflow"].append("a `develop` branch exists")
        into_develop = facts.get("develop_merges", [])
        features = [s for s in into_develop if s.startswith("feature/")]
        if features:
            scores["gitflow"] += 1
            evidence["gitflow"].append(f"feature branches merged into develop: {', '.join(features[:5])}")
        if releases:
            scores["gitflow"] += 2
            evidence["gitflow"].append(f"release branches beside develop: {', '.join(releases[:5])}")
        if groups.get("hotfix/"):
            scores["gitflow"] += 1
            evidence["gitflow"].append(f"hotfix branches: {', '.join(groups['hotfix/'][:5])}")
        if "develop" in docs:
            scores["gitflow"] += 1
            evidence["gitflow"].append(f"the contribution docs mention develop ({', '.join(docs['develop'])})")
    elif releases:
        scores["release-branch"] += 2 + len(releases)
        evidence["release-branch"].append(f"release branches cut from {default}: {', '.join(releases[:8])}")
        if tags["on_release"]:
            scores["release-branch"] += 2
            evidence["release-branch"].append(f"version tags on release branches: {', '.join(tags['on_release'][:6])}")
        if "release branch" in docs:
            scores["release-branch"] += 1
            evidence["release-branch"].append("the contribution docs mention release branches")

    trains = groups.get("train/", [])
    if len(trains) >= 2:
        scores["sprint"] += 2 + len(trains)
        evidence["sprint"].append(f"{len(trains)} train branches: {', '.join(trains[:8])}")

    features_main = [s for s in merged if s.startswith("feature/")]
    if features_main:
        scores["trunk"] += 2
        evidence["trunk"].append(f"short-lived feature branches merged straight into {default}: {', '.join(features_main[:5])}")
    ci_main = [rel for rel, names in ci.items() if default in names]
    if ci_main and not any(n in ("develop",) or n.startswith(("release", "sprint")) for names in ci.values() for n in names):
        scores["trunk"] += 1
        evidence["trunk"].append(f"CI runs on {default} only ({', '.join(ci_main)})")
    if "trunk" in docs:
        scores["trunk"] += 1
        evidence["trunk"].append(f"the contribution docs mention trunk-based work ({', '.join(docs['trunk'])})")
    if "feature flag" in docs:
        scores["trunk"] += 1
        evidence["trunk"].append("the contribution docs mention feature flags")
    if not merged and facts["merges"]["share"] == 0 and not any(scores[s] for s in STYLES if s != "trunk"):
        evidence["trunk"].append(f"{default} has a linear history (no merge commits)")
    against = []
    for rel, names in ci.items():
        other = [n for n in names if n != default]
        if other:
            against.append(f"{rel} also runs on {', '.join(other)}")
    return scores, evidence, against


def _proposal(style: str, facts: dict[str, Any]) -> str:
    default = facts["default_branch"]
    main = f' #:main "{default}"' if default != "main" else ""
    if style == "sprint":
        days = facts.get("gap_days", {}).get("sprint") or 14
        return f"(delivery sprint{main} #:length-days {days})"
    if style == "gitflow":
        return f"(delivery gitflow{main})"
    if style == "release-branch":
        return f'(delivery release-train{main} #:length-days 0 #:name "{{version}}")'
    if style == "phase-gate":
        names = [n.removeprefix("phase/") for n in facts["groups"].get("phase/", [])]
        return f"(delivery phase-gate{main} #:phases ({' '.join(names)}))"
    return f"(delivery trunk{main})"


def detect(app: Path, tracked: list[str], forge_layer: Path | None = None, default_branch: str | None = None) -> dict[str, Any]:
    app = Path(app).resolve()
    branches = _branches(app)
    default = default_branch or ("main" if "main" in branches else "master" if "master" in branches else "main")
    default_ref = branches.get(default, f"refs/heads/{default}")
    groups: dict[str, list[str]] = {}
    for name in sorted(branches):
        prefix = _prefix(name)
        if prefix:
            groups.setdefault(prefix, []).append(name)
    gaps: dict[str, int] = {}
    cuts: dict[str, list[str]] = {}
    for kind, prefixes in (("sprint", ("sprint/", "sprint-", "train/")), ("release", ("release/", "release-"))):
        names = [n for p in prefixes for n in groups.get(p, [])]
        dated = _cut_dates(app, branches, names, default_ref)
        if len(dated) >= 2:
            steps = [(b - a) / DAY for (_, a), (_, b) in zip(dated, dated[1:])]
            gaps[kind] = max(1, round(statistics.median(steps)))
        cuts[kind] = [name for name, _ in dated]
    facts: dict[str, Any] = {
        "default_branch": default, "groups": groups, "gap_days": gaps, "cut_order": cuts,
        "merges": _merges(app, default_ref), "tags": _tags(app), "ci": _ci_branches(app, tracked),
        "docs": _docs_hints(app, tracked),
        "develop_merges": _merges(app, branches["develop"])["sources"] if "develop" in branches else [],
    }
    scores, evidence, against = _score(facts)
    best = max(STYLES, key=lambda s: (scores[s], -STYLES.index(s)))
    unverified = scores[best] == 0 or (best == "trunk" and not any(scores.values()))
    if unverified:
        best = "trunk"
    protection = _protection(forge_layer, [default, *(["develop"] if "develop" in branches else [])])
    return {"style": best, "preset": PRESET[best], "proposal": _proposal(best, facts),
            "evidence": evidence[best] or ["no branching signal in the history, CI files or contribution docs"],
            "against": against + [f"{style} scores {scores[style]}" for style in STYLES
                                  if style != best and scores[style] > 0],
            "scores": scores, "unverified": unverified, "default_branch": default,
            "branches": {prefix: names[:12] for prefix, names in groups.items()},
            "tags": facts["tags"], "ci": facts["ci"], "protection": protection}


def render_section(d: dict[str, Any]) -> str:
    """The `## Delivery` section of the LLM.txt draft."""
    mark = " UNVERIFIED" if d.get("unverified") else ""
    lines = [f"- Style: {d['style']}{mark}",
             f"- Proposed preset: `{d['proposal']}`{mark}",
             f"- Default branch: {d.get('default_branch', 'main')}",
             "- What speaks for it:"]
    lines += [f"  - {item}" for item in d.get("evidence") or []]
    if d.get("against"):
        lines.append("- What speaks against it:")
        lines += [f"  - {item}" for item in d["against"]]
    protection = d.get("protection") or {}
    if protection.get("note"):
        lines.append(f"- Branch protection: {protection['note']}")
    elif protection:
        lines += [f"- Branch protection of {branch}: {info}" for branch, info in protection.items()]
    else:
        lines.append("- Branch protection: UNVERIFIED (run `al scout --layer LAYER` with a declared forge to ask it)")
    lines.append("- Which branches agents may merge into: UNVERIFIED — confirm with the team")
    return "\n".join(lines)
