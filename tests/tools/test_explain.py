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


# --- toolchains --------------------------------------------------------------------------------

TOOLCHAIN_IR = {
    "codebase": {"commands": [], "paths": {"protected": []}, "conditionalDocs": []},
    "toolchains": [{"id": "dev", "kind": "nix", "packages": ["python312", "uv"], "features": ["browser", "agent-cli"],
                    "nixpkgs": "github:NixOS/nixpkgs/nixos-25.05", "env": [{"name": "TZ", "value": "UTC"}],
                    "shellHook": "", "agentClis": ["claude-code"], "status": "stable"}],
    "environments": [
        {"id": "worktree", "kind": "worktree"},
        {"id": "nix-devshell", "kind": "nix-devshell", "packages": [], "toolchain": "dev"},
        {"id": "sandbox", "kind": "podman", "packages": [], "toolchain": "dev", "agentInside": True,
         "baseImage": "docker.io/library/debian:stable-slim", "caches": [], "services": [], "publish": []},
        {"id": "home-manager", "kind": "home-manager", "packages": ["uv"]},
    ],
}


def test_toolchain_block_lists_packages_features_and_pin():
    heading, text, bullets = explain.explain({"head": "toolchain"}, TOOLCHAIN_IR)
    assert heading == "Which tools the environments have"
    assert "`dev`" in text and "python312 and uv" in text and "pinned" in text
    joined = "\n".join(bullets)
    assert "`browser`: Chromium with fonts" in joined
    assert "`agent-cli`: The CLI of every agent kind" in joined and "claude-code" in joined
    assert "github:NixOS/nixpkgs/nixos-25.05" in joined and "env/flake.lock" in joined
    assert "Used by `nix-devshell` and `sandbox`" in joined
    assert "`TZ`" in joined


def test_environment_with_toolchain_names_it():
    _heading, _text, bullets = explain.explain({"head": "environment"}, TOOLCHAIN_IR)
    assert "`nix-devshell`: uses toolchain `dev`" in bullets
    podman = next(b for b in bullets if b.startswith("`podman`"))
    assert "built from toolchain `dev` with Nix" in podman and "debian" not in podman
    assert "`home-manager`: provides uv" in bullets


# --- the kit workflow integrate ------------------------------------------------------------------

INTEGRATE_IR = {
    "name": "pantry", "description": "A pantry", "codebase": {"commands": [], "paths": {"protected": []}},
    "commands": [], "agents": [{"id": "claude", "kind": "claude-code", "modelSets": {}}],
    "reviews": [{"id": "gate", "kind": "auto", "ship": "merge"}, {"id": "person", "kind": "human"}],
    "adws": [{"name": "ship-it", "review": "gate", "changes": "codebase",
              "steps": [{"kind": "check", "name": "test"}, {"kind": "review", "name": "review"},
                        {"kind": "ship", "name": "ship"}]},
             {"name": "plan-build", "review": "person", "changes": "codebase",
              "steps": [{"kind": "review", "name": "review"}, {"kind": "ship", "name": "ship"}]}],
}


def test_explain_names_the_integrate_workflow():
    heading, text, bullets = explain.integrate_text(INTEGRATE_IR)
    assert heading == "Kit workflow: integrate (every layer has it)"
    assert "It serves runs of `ship-it`" in text and "plan-build" not in text
    assert [b.split(":")[0] for b in bullets[:5]] == ["prepare", "resolve", "checks", "review", "ship"]
    joined = "\n".join(bullets)
    assert "`al integrate LAYER RUN`" in joined and "**Resolve with an agent**" in joined
    assert '`"enabled": false` switches it off' in joined and "templates/integrate.json" in joined
    assert "never force-pushes" in joined
    assert "pull request" not in text + joined     # true with and without a forge
    never = explain.integrate_text({**INTEGRATE_IR, "adws": INTEGRATE_IR["adws"][1:]})[1]
    assert "never offered" in never
    out: list[str] = []
    real = explain.explain
    explain.explain = lambda block, ir: ("A block", "words", [])
    try:
        out = explain.markdown("pantry", "agentic.rkt", ["#lang al"],
                               [{"head": "agent", "start": 1, "end": 1}, {"head": "dashboard", "start": 1, "end": 1}],
                               INTEGRATE_IR, None).splitlines()
    finally:
        explain.explain = real
    at = out.index("### Kit workflow: integrate (every layer has it)")
    assert out.index("### A block") < at, "the section follows the blocks"
    section = "\n".join(out[at:])
    for name in ("prepare", "resolve", "checks", "review", "ship"):
        assert f"\n- {name}: " in section
