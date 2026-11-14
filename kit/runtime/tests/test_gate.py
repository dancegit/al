from __future__ import annotations

import pytest

from adw_modules.errors import LayerError
from adw_modules.review import decide, is_protected, parse_numstat


@pytest.mark.parametrize(("facts_update", "reason"), [
    ({"check_exits": {"test": 1}}, "check-failed:test"),
    ({"verdict": {"success": True, "issues": [{"severity": "blocker"}]}}, "review-failed"),
    ({"klass": "bug"}, "class-not-allowed:bug"),
    ({"diff_lines": 11}, "diff-too-large:11>10"),
    ({"changed_files": ["src/private/a.py"]}, "protected-path:src/private/a.py"),
])
def test_failed_gate_fact_falls_back_to_pr(facts_update, reason):
    option = {"kind": "auto", "ship": "merge", "allowedClasses": ["chore"],
              "maxDiffLines": 10, "blockProtectedPaths": True}
    facts = {"check_exits": {"test": 0}, "require_exits": {"lint": 0},
             "verdict": {"success": True, "issues": []}, "klass": "chore",
             "diff_lines": 2, "changed_files": ["src/a.py"], "protected": ["src/private"]}
    facts.update(facts_update)
    result = decide(option, facts)
    assert result["action"] == "pr" and result["ship"] is False
    assert reason in result["reasons"]


def test_good_facts_merge_and_human_agent_require_pr():
    facts = {"check_exits": {"test": 0}, "require_exits": {"lint": 0},
             "verdict": {"success": True, "issues": []}, "klass": "chore",
             "diff_lines": 2, "changed_files": [], "protected": []}
    option = {"kind": "auto", "ship": "merge", "allowedClasses": ["chore"],
              "maxDiffLines": 10, "blockProtectedPaths": True}
    assert decide(option, facts) == {"action": "merge", "ship": True, "reasons": []}
    for kind in ("human", "agent"):
        result = decide({"kind": kind}, facts)
        assert result["action"] == "pr" and result["ship"] is False


def test_numstat_binary_rows_and_protected_prefix_semantics():
    assert parse_numstat("3\t2\tfile.py\n-\t-\timage.bin\n") == (5, ["file.py", "image.bin"])
    assert is_protected(".github/x.yml", [".github"])
    assert not is_protected(".githubx", [".github"])


def test_unknown_review_kind_is_clear():
    with pytest.raises(LayerError, match="Unknown review kind"):
        decide({"kind": "mystery"}, {})
