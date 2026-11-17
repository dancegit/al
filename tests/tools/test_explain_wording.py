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


def test_host_and_registry_are_explained_without_values() -> None:
    import importlib.util
    from conftest import ROOT
    spec = importlib.util.spec_from_file_location("explain_for_hosts", ROOT / "tools" / "explain.py")
    explain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(explain)  # type: ignore[union-attr]
    ir = {"hosts": [{"id": "server", "provision": "script", "units": ["cron", "dashboard"], "dashboardPort": 8770,
                     "linger": True, "images": "pull", "fallbackBuild": True},
                    {"id": "nix", "provision": "home-manager", "units": [], "dashboardPort": 0, "linger": True,
                     "images": "load", "fallbackBuild": False}],
          "registry": {"addressEnv": "AL_REGISTRY", "usernameEnv": "REG_USER", "passwordEnv": "REG_PASSWORD", "tlsVerify": True}}
    title, text, bullets = explain.host_text(ir)
    words = text + " ".join(bullets)
    assert "`server`" in words and "`nix`" in words
    assert "shell script for Debian/Ubuntu, Fedora or Arch" in words and "Home Manager module" in words
    assert "cron and dashboard" in words and "port 8770" in words and "every unit the layer declares" in words
    assert "pulls the tested sandbox image from the registry" in words and "over the ssh connection" in words
    assert "the deploy stops" in words
    title, text, bullets = explain.registry_text(ir)
    words = text + " ".join(bullets)
    assert "`AL_REGISTRY`" in words and "`REG_USER`" in words and "`REG_PASSWORD`" in words
    assert "://" not in words and "@" not in words                         # names of variables only, never a value
    assert explain.registry_text({"registry": None})[1].startswith("No registry")
    assert explain.host_text({"hosts": []})[1].startswith("No machine")


def test_a_workflow_without_commit_or_ship_keeps_its_result_on_a_branch(app, tmp_path) -> None:
    text = _explained(app, tmp_path, "(forge none)", "(review human)")
    plan_only = text[text.index("### Workflow: only-plan"):text.index("### Workflow: change")]
    change = text[text.index("### Workflow: change"):]
    assert "its result is kept on a local branch" in plan_only
    assert "its result is kept on a local branch" not in change.split("\n### ")[0]
