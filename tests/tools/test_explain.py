"""Unit tests for the pure helpers of tools/explain.py (`al explain`): how a definition is split
into blocks, how lists are joined and how a workflow step is worded. No racket, network, agent or
subprocess; explain.py is loaded by its file path and nothing is written to disk.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

EXPLAIN = Path(__file__).resolve().parents[2] / "tools" / "explain.py"

# loaded under a name other than __main__, so main() never runs
_spec = importlib.util.spec_from_file_location("al_explain", EXPLAIN)
explain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(explain)  # type: ignore[union-attr]


# --- listing -------------------------------------------------------------------------------------

def test_listing_empty():
    assert explain.listing([]) == ""


def test_listing_one():
    assert explain.listing(["a"]) == "a"


def test_listing_two():
    assert explain.listing(["a", "b"]) == "a and b"


def test_listing_three():
    assert explain.listing(["a", "b", "c"]) == "a, b and c"


def test_listing_drops_empty_strings():
    assert explain.listing(["a", "", "b"]) == "a and b"
    assert explain.listing(["", "a", ""]) == "a"
    assert explain.listing(["", ""]) == ""


def test_listing_custom_last_word():
    assert explain.listing(["a", "b", "c"], last="or") == "a, b or c"
    assert explain.listing(["a", "b"], last="or") == "a or b"


# --- split_blocks --------------------------------------------------------------------------------

# synthetic, never compiled: split_blocks only looks at the shape of each line
DEFINITION = [
    "#lang al",                                        # 1  header
    '(agentic-layer #:name "demo" #:description "x"',  # 2  header
    '  (codebase #:root ".")',                         # 3  codebase
    "  ; when work arrives",                           # 4  trigger block starts at its comment
    "  (trigger issue-opened)",                        # 5
    "  (trigger issue-labelled)",                      # 6  same family, grouped -> one block
    "",                                                # 7  blank, trimmed from the trigger block
    "  (adw plan-build)",                              # 8  adw is not grouped
    "  (adw plan-only))",                              # 9
]


def _block(head: str) -> dict:
    return next(b for b in explain.split_blocks(DEFINITION) if b["head"] == head)


def test_split_blocks_layout():
    assert [(b["head"], b["start"], b["end"]) for b in explain.split_blocks(DEFINITION)] == [
        ("header", 1, 2), ("codebase", 3, 3), ("trigger", 4, 6), ("adw", 8, 8), ("adw", 9, 9)]


def test_split_blocks_header_first():
    blocks = explain.split_blocks(DEFINITION)
    assert blocks[0]["head"] == "header"
    assert blocks[0]["start"] == 1
    assert blocks[0]["first"] == ""


def test_split_blocks_comment_belongs_to_clause():
    block = _block("trigger")
    assert block["start"] == 4
    assert DEFINITION[block["start"] - 1] == "  ; when work arrives"
    assert block["first"] == "  (trigger issue-opened)"
    assert _block("codebase")["end"] == 3


def test_split_blocks_grouping():
    blocks = explain.split_blocks(DEFINITION)
    heads = [b["head"] for b in blocks]
    assert heads.count("trigger") == 1
    assert heads.count("adw") == 2
    assert _block("trigger")["end"] == 6


def test_split_blocks_one_based_inclusive():
    blocks = explain.split_blocks(DEFINITION)
    for b in blocks:
        assert DEFINITION[b["end"] - 1]
        assert DEFINITION[b["start"] - 1]
        if b["head"] != "header":
            # the clause line itself lies inside [start, end]
            assert b["first"] in DEFINITION[b["start"] - 1:b["end"]]
    for b in blocks:
        if b["head"] == "adw":
            assert DEFINITION[b["start"] - 1] == b["first"]


def test_split_blocks_without_clause():
    assert explain.split_blocks([]) == []
    assert explain.split_blocks(["#lang al", '(agentic-layer #:name "demo")']) == []
    # four spaces of indentation: not a top-level clause
    assert explain.split_blocks(["#lang al", "(agentic-layer", "    (deep-indented x))"]) == []


# --- describe_step -------------------------------------------------------------------------------

def test_describe_run_step_class_filter():
    s = explain.describe_step({"kind": "run", "name": "fix", "command": "bug", "when": "class-bug"})
    assert "only for bug work" in s
    assert "`fix`" in s
    assert "`bug`" in s
    assert "only for" not in explain.describe_step({"kind": "run", "name": "plan", "command": "chore"})
    assert "only for" not in explain.describe_step(
        {"kind": "run", "name": "plan", "command": "chore", "when": "always"})


def test_describe_check_with_resolve():
    s = explain.describe_step(
        {"kind": "check", "name": "test", "cmd": "test", "resolve": "resolve-failed-test", "retries": 3})
    assert "`resolve-failed-test`" in s
    assert "up to 3 tries" in s
    assert "fixes" in s
    assert s.startswith("check `test`")


def test_describe_check_without_resolve():
    s = explain.describe_step({"kind": "check", "name": "test", "cmd": "test"})
    assert "fix" not in s
    assert "tries" not in s
    assert s == "check `test`: run the `test` command"


def test_describe_review_with_patch():
    s = explain.describe_step({"kind": "review", "patch": "patch"})
    assert s.startswith("review")
    assert "patch plan" in s
    assert "reviewed again" in s


def test_describe_review_without_patch():
    assert explain.describe_step({"kind": "review"}) == "review"
    assert "patch" not in explain.describe_step({"kind": "review", "patch": None})


def test_describe_ship():
    assert explain.describe_step({"kind": "ship"}) == "ship"


# --- delivery ------------------------------------------------------------------------------------

import json  # noqa: E402

FULL_IR = json.loads((Path(__file__).resolve().parents[2] / "kit" / "runtime" / "tests" / "full.layer.json").read_text())


def test_explain_delivery_preset_lists_its_stages_and_expansion():
    heading, text, bullets = explain.explain({"head": "delivery", "first": "  (delivery sprint #:length-days 14)"}, FULL_IR)
    joined = " ".join([heading, text, *bullets])
    assert heading == "How work is delivered"
    assert "sprint/{cycle}" in joined
    assert "agents may not merge into main" in joined
    for clause in FULL_IR["delivery"]["expansion"]:
        assert clause in joined
    _, _, declared = explain.explain({"head": "stage", "first": "  (stage hotfix"}, FULL_IR)
    assert any("`hotfix`: branch `hotfix/{date}`" in line and "lands in production" in line for line in declared)


def test_explain_adw_says_where_it_delivers():
    _, text, _ = explain.explain({"head": "adw", "first": "  (adw hotfix #:description"}, FULL_IR)
    assert "delivers into hotfix (`hotfix/{date}`)" in text
    _, zte, _ = explain.explain({"head": "adw", "first": "  (adw zte #:description"}, FULL_IR)
    assert "delivers into sprint (`sprint/{cycle}`)" in zte


def test_explain_promotion_in_words():
    heading, text, bullets = explain.explain({"head": "promotion", "first": "  (promotion sprint -> main"}, FULL_IR)
    assert heading == "How a stage is promoted"
    sprint = next(line for line in bullets if line.startswith("sprint → main"))
    assert "a person approves (1)" in sprint and "version bump minor" in sprint and "tag v{version}" in sprint
    assert "requires checks and acceptance" in sprint and "merged by merge" in sprint
    hotfix = next(line for line in bullets if line.startswith("hotfix → main"))
    assert "two people approve" in hotfix and "merged back into sprint" in hotfix and "hotfix" in hotfix
    assert "al promote LAYER FROM TO" in text
    _, delivery, stage_lines = explain.explain({"head": "delivery", "first": "  (delivery sprint #:length-days 14)"},
                                               FULL_IR)
    assert any("promoted into main with `al promote LAYER sprint main`" in line for line in stage_lines)


# --- reused environments -------------------------------------------------------------------------

def test_explain_names_reused_environments():
    ir = {"codebase": {}, "environments": [
        {"id": "worktree", "kind": "worktree", "prepare": ["install", "fetch"]},
        {"id": "nix-devshell", "kind": "nix-devshell", "packages": ["jq"], "flake": ".", "shell": "ci", "browser": True},
        {"id": "box", "kind": "podman", "agentInside": False, "caches": [], "services": [], "publish": [],
         "baseImage": "docker.io/library/debian:stable-slim", "containerfile": "Containerfile", "target": "dev",
         "devcontainer": "", "browser": False},
        {"id": "dc", "kind": "podman", "agentInside": True, "caches": [], "services": [], "publish": [],
         "baseImage": "docker.io/library/debian:stable-slim", "containerfile": "", "target": "",
         "devcontainer": ".devcontainer/devcontainer.json", "browser": True}]}
    _title, _text, bullets = explain.explain({"head": "environment"}, ir)
    assert "before the first step: `install` and `fetch`" in bullets[0]
    assert "uses the repository's own dev shell `ci` from `flake.nix`; adds jq and a headless browser" in bullets[1]
    assert "image built from the repository's `Containerfile` (stage `dev`)" in bullets[2]
    assert "image from the repository's dev container `.devcontainer/devcontainer.json`" in bullets[3]
    assert "a headless browser" in bullets[3]
    plain = {"codebase": {}, "environments": [{"id": "worktree", "kind": "worktree", "prepare": []},
                              {"id": "nix-devshell", "kind": "nix-devshell", "packages": ["jq"], "flake": "",
                               "shell": "default", "browser": False}]}
    _title, _text, bullets = explain.explain({"head": "environment"}, plain)
    assert bullets == ["`worktree`: each run gets its own git worktree; your checkout is never touched",
                       "`nix-devshell`: provides jq"]
