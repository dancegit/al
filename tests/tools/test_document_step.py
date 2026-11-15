"""A workflow's `document` step: what it writes is run state (ignored) that the runtime puts in
front of the reviewer, never a tracked file of the layer in the main checkout; and
`conditional_docs.md` is generated from the definition, so a run that edits it is noticed."""
from __future__ import annotations

import json
import re

from conftest import LAYER_TEMPLATE, al, commit, sh, status

DOCUMENTED = LAYER_TEMPLATE.replace(
    "(paths #:source", '(conditional-doc "README.md" "When changing anything")\n    (paths #:source').replace(
    "(command commit #:template commit)", "(command commit #:template commit)\n  (command document #:template document)").replace(
    "(check test #:cmd test) (run commit", '(check test #:cmd test) (run document #:command document #:args ("$plan")) (run commit')


def _in_repo_with_document_step(app):
    al("init", app, "--in-repo")
    layer = app / "agentic"
    (layer / "agentic.rkt").write_text(DOCUMENTED.format(root="..", placement='(layer in-repo #:dir "agentic")'))
    al("gen", layer)
    commit(app, "the agentic layer", "agentic")
    assert status(app) == ""
    return layer


def _files_the_document_step_writes(layer) -> dict[str, str]:
    """Every layer file the generated document prompt names in its Variables, relative to the
    layer: what a real agent following the prompt writes (the mock agent does it for the test)."""
    prompt = (layer / "commands" / "document.md").read_text()
    variables = prompt.split("## Variables", 1)[1].split("\n## ", 1)[0]
    return {name: path for name, path in re.findall(r"^([A-Z_]+): \$\{layer_dir\}/(\S+)$", variables, re.M)}


def test_a_document_step_on_an_in_repo_layer_leaves_git_status_clean(app) -> None:
    layer = _in_repo_with_document_step(app)
    writes = _files_the_document_step_writes(layer)
    assert set(writes) == {"DOC_FILE"}, writes                       # the document, nothing else of the layer
    doc = writes["DOC_FILE"].replace("${class}", "chore").replace("${adw_id}", "d0c00001").replace(
        "<descriptive-name>", "docstring")
    assert sh(["git", "-C", app, "check-ignore", "-q", f"agentic/{doc}"]).returncode == 0, "DOC_FILE must be run state"
    files = {name: (layer / path).read_text() + "- `docs/x.md` — when x\n" if (layer / path).is_file() else "x\n"
             for name, path in writes.items()}
    files["DOC_FILE"] = "# Docstring\n\n**Read when:** changing app.py\n"
    script = {"layer_files": {doc if name == "DOC_FILE" else writes[name]: text for name, text in files.items()},
              "output": f"${{layer_dir}}/{doc}"}
    (layer / "mock" / "change").mkdir(parents=True)
    (layer / "mock" / "change" / "document.json").write_text(json.dumps(script))
    commit(app, "mock script", "agentic/mock")

    cp = sh(["uv", "run", "--quiet", layer / "adws" / "adw_change.py", "Add a docstring", "--dry-run",
             "--adw-id", "d0c00001"], cwd=app)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert status(app) == ""                                                       # runs leave git status clean
    assert al("check", layer).stdout.strip().endswith("check: ok")
    pr = (layer / "agents" / "d0c00001" / "PR.md").read_text()
    assert f"`agentic/{doc}`" in pr and "**Read when:** changing app.py" in pr    # travels to the reviewer
    assert str(app) not in pr


def test_the_document_prompt_no_longer_registers_into_the_layer(app) -> None:
    layer = _in_repo_with_document_step(app)
    prompt = (layer / "commands" / "document.md").read_text()
    assert "CONDITIONAL_DOCS" not in prompt and "Append exactly one line" not in prompt
    assert "Read when" in prompt


def test_conditional_docs_md_is_generated_from_the_definition(app) -> None:
    layer = _in_repo_with_document_step(app)
    manifest = json.loads((layer / ".al-manifest.json").read_text())["files"]
    assert "conditional_docs.md" in manifest
    assert "- `README.md` — When changing anything" in (layer / "conditional_docs.md").read_text()
    rkt = layer / "agentic.rkt"
    rkt.write_text(rkt.read_text().replace('"When changing anything"', '"When changing the notes format"'))
    al("gen", layer)
    assert "- `README.md` — When changing the notes format" in (layer / "conditional_docs.md").read_text()
    with (layer / "conditional_docs.md").open("a") as handle:
        handle.write("- `docs/chore-x.md` — when x\n")                             # what a run used to do
    out = al("check", layer, ok=False).stdout
    assert "PROBLEM: conditional_docs.md was edited by hand" in out


def test_an_untouched_conditional_docs_md_from_an_older_al_is_simply_regenerated(app) -> None:
    layer = _in_repo_with_document_step(app)
    manifest_path = layer / ".al-manifest.json"
    data = json.loads(manifest_path.read_text())
    del data["files"]["conditional_docs.md"]                                       # as an older al wrote it
    manifest_path.write_text(json.dumps(data, indent=1) + "\n")
    current = (layer / "conditional_docs.md").read_text()
    (layer / "conditional_docs.md").write_text("\n".join(line for line in current.split("\n")
                                                         if not line.startswith("Generated by `al gen`")))
    al("gen", layer)
    assert (layer / "conditional_docs.md").read_text() == current


def test_a_hand_extended_conditional_docs_md_from_an_older_al_is_not_overwritten_silently(app) -> None:
    layer = _in_repo_with_document_step(app)
    manifest_path = layer / ".al-manifest.json"
    data = json.loads(manifest_path.read_text())
    del data["files"]["conditional_docs.md"]                                       # as an older al wrote it
    manifest_path.write_text(json.dumps(data, indent=1) + "\n")
    with (layer / "conditional_docs.md").open("a") as handle:
        handle.write("- `docs/team.md` — when the team says so\n")
    cp = al("gen", layer, ok=False)
    assert cp.returncode == 3 and "conditional_docs.md" in cp.stderr and "(conditional-doc" in cp.stderr
    assert "docs/team.md" in (layer / "conditional_docs.md").read_text()
    al("gen", layer, "--discard-edits")
    assert "docs/team.md" not in (layer / "conditional_docs.md").read_text()
