"""`al explain` says what really happens: with `(forge none)` there is no pull request but a branch
and `PR.md`; a workflow without a ship step publishes nothing; the summary reads as a sentence;
and the count of codebase-specific instructions matches what is listed."""
from __future__ import annotations

from conftest import LAYER_TEMPLATE, al

FOCUS = " #:focus (" + " ".join(f"\"rule {n}\"" for n in range(1, 9)) + ")"


def _explained(app, tmp_path, forge: str, review: str = "") -> str:
    layer = app.parent / "notes.al"
    layer.mkdir()
    source = LAYER_TEMPLATE.format(root="../notes", placement=f"(layer sibling)\n  {forge}\n  {review}")
    source = source.replace("(command commit #:template commit)", f"(command commit #:template commit{FOCUS})")
    source = source.replace("(adw change", '(adw only-plan #:description "Plan only"\n    (steps (run plan #:command plan #:args ("$prompt"))))\n  (adw change')
    (layer / "agentic.rkt").write_text(source)
    al("gen", layer)
    out = tmp_path / "explained.md"
    al("explain", layer, "--md", out)
    return out.read_text()


def test_without_a_forge_there_is_no_pull_request(app, tmp_path) -> None:
    text = _explained(app, tmp_path, "(forge none)", "(review human)")
    assert "pull request" not in text.replace("`pull-request`", "").replace("pull requests use", ""), text
    assert "a person, through a branch and a description file (`PR.md`)" in text
    assert "keep the work on a branch, write `PR.md` and stop; a person reads it and merges" in text
    assert "A person reviews the branch and its description file (`PR.md`)." in text


def test_with_a_forge_it_is_a_pull_request(app, tmp_path) -> None:
    text = _explained(app, tmp_path, '(forge github #:repo "o/notes")', "(review human)")
    assert "a person, through a pull request" in text and "A person reviews the pull request." in text
    assert "open a pull request and stop" in text


def test_a_workflow_without_a_ship_step_publishes_nothing(app, tmp_path) -> None:
    text = _explained(app, tmp_path, "(forge none)", "(review human)")
    plan_only = text[text.index("### Workflow: only-plan"):text.index("### Workflow: change")]
    assert "No ship step: nothing is reviewed or published" in plan_only and "reviews" not in plan_only.split("Steps:")[1].split("\n")[0]


def test_the_summary_is_a_sentence_and_the_rule_count_matches_the_list(app, tmp_path) -> None:
    text = _explained(app, tmp_path, "(forge none)", "(review human)")
    assert "`notes` is Agentic layer" not in text
    assert "The `notes` layer: Agentic layer of a tiny test app." in text
    assert "8 of their instructions are specific to this codebase; the first 6:" in text
    assert text.count("Codebase-specific rule:") == 6
