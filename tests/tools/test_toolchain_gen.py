"""`al gen` and toolchains: one `(toolchain nix …)` declaration feeds the dev shell, the sandbox image and
the Home Manager module through one generated `env/toolchain-<id>.nix`, and a layer without a toolchain
generates no toolchain file and no `toolchains` key (golden files under tests/tools/golden/).

Regenerate the goldens after an intended change with AL_UPDATE_GOLDEN=1."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from conftest import KIT, LAYER_TEMPLATE, ROOT, al

GOLDEN = Path(__file__).resolve().parent / "golden"
UPDATE = bool(os.environ.get("AL_UPDATE_GOLDEN"))

ADW_LINE = '(adw change #:description "Plan, build, test; a person reviews"'

# Every environment kind in its old form: no toolchain anywhere.
OLD_FORMS = (LAYER_TEMPLATE
             .replace("  {placement}\n", "  {placement}\n"
                      "  (environment worktree)\n"
                      '  (environment nix-devshell #:packages ("python312" "uv"))\n'
                      '  (environment home-manager #:packages ("python312"))\n'
                      '  (environment podman #:id sandbox #:packages ("git") #:setup ("true"))\n')
             .replace(ADW_LINE, ADW_LINE + "\n    #:environment (worktree nix-devshell sandbox)"))

# One toolchain, used by all three consumers.
TOOLCHAIN_LAYER = (LAYER_TEMPLATE
                   .replace("  {placement}\n", "  {placement}\n"
                            '  (toolchain nix #:id dev #:packages ("python312" "uv" "git") #:features (browser agent-cli)\n'
                            '    #:env (("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" "1")))\n'
                            "  (environment worktree)\n"
                            "  (environment nix-devshell #:toolchain dev)\n"
                            "  (environment home-manager #:toolchain dev)\n"
                            '  (environment podman #:id sandbox #:toolchain dev #:setup ("true"))\n')
                   .replace(ADW_LINE, ADW_LINE + "\n    #:environment (worktree nix-devshell sandbox)"))
assert OLD_FORMS != LAYER_TEMPLATE and TOOLCHAIN_LAYER != LAYER_TEMPLATE


def generate(tmp_path: Path, source: str, app: Path) -> Path:
    layer = tmp_path / "notes.al"
    layer.mkdir()
    (layer / "agentic.rkt").write_text(source.format(root=os.path.relpath(app, layer), placement="(layer sibling)"))
    al("gen", layer)
    return layer


def normalised(text: str, layer: Path, app: Path) -> str:
    for needle, placeholder in ((str(layer.resolve()), "<LAYER>"), (str(layer), "<LAYER>"),
                                (str(app.resolve()), "<APP>"), (str(app), "<APP>"), (str(ROOT), "<AL>")):
        text = text.replace(needle, placeholder)
    return text


def ir_and_env(layer: Path, app: Path) -> dict[str, str]:
    files = [layer / "agentic-layer.json"] + sorted(p for p in (layer / "env").rglob("*") if p.is_file())
    return {p.relative_to(layer).as_posix(): normalised(p.read_text(), layer, app) for p in files}


def compare(actual: dict[str, str], golden: Path) -> None:
    if UPDATE:
        shutil.rmtree(golden, ignore_errors=True)
        for rel, text in actual.items():
            (golden / rel).parent.mkdir(parents=True, exist_ok=True)
            (golden / rel).write_text(text)
    assert golden.is_dir(), f"no golden files at {golden}: run with AL_UPDATE_GOLDEN=1"
    expected = {p.relative_to(golden).as_posix(): p.read_text() for p in sorted(golden.rglob("*")) if p.is_file()}
    assert sorted(actual) == sorted(expected), "the set of generated files changed (AL_UPDATE_GOLDEN=1 to accept)"
    for rel in actual:
        assert actual[rel] == expected[rel], f"{rel} differs from {golden / rel} (AL_UPDATE_GOLDEN=1 to accept)"


def test_layer_without_toolchain_generates_as_before(tmp_path, app) -> None:
    layer = generate(tmp_path, OLD_FORMS, app)
    compare(ir_and_env(layer, app), GOLDEN / "no-toolchain")


TOOLCHAIN_FILES = ("env/flake.nix", "env/toolchain-dev.nix", "env/home-manager.nix", "env/Containerfile")


def test_toolchain_files_match_golden(tmp_path, app) -> None:
    layer = generate(tmp_path, TOOLCHAIN_LAYER, app)
    compare({rel: normalised((layer / rel).read_text(), layer, app) for rel in TOOLCHAIN_FILES},
            GOLDEN / "toolchain")


def nix_lists(text: str) -> list[str]:
    """Every `with pkgs; [ … ]` list of a Nix file, as written."""
    return re.findall(r"with pkgs; \[([^\]]*)\]", text)


def test_three_consumers_import_one_package_set(tmp_path, app) -> None:
    layer = generate(tmp_path, TOOLCHAIN_LAYER, app)
    flake = (layer / "env/flake.nix").read_text()
    home = (layer / "env/home-manager.nix").read_text()
    toolchain = (layer / "env/toolchain-dev.nix").read_text()
    for package in ("python312", "uv", "git"):
        assert package in nix_lists(toolchain)[0].split(), toolchain
    assert '"browser"' in toolchain and '"agent-cli"' in toolchain and '"claude-code"' in toolchain
    assert 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1"' in toolchain
    # the dev shell and the image both take the toolchain from the one file …
    assert flake.count("./toolchain-dev.nix") == 1 and "toolchain_dev system" in flake
    devshell = flake[flake.index("devShells"):flake.index("packages = forLinux")]
    image = flake[flake.index("image-dev"):]
    assert "tc.packages" in devshell and "tc.env" in devshell
    assert "tc.imagePackages" in image and "imageBase" in image
    # … and so does the Home Manager module
    assert "import ./toolchain-dev.nix" in home and "tc.packages" in home and "tc.env" in home
    # nobody lists a package of their own, besides what the Home Manager module always had
    assert nix_lists(flake) == []
    assert [lst.split() for lst in nix_lists(home)] == [["uv", "git"]]
    assert "python312" not in flake and "python312" not in home and "chromium" not in flake + home
    assert (layer / "env/Containerfile").read_text().startswith(
        "# Generated by al from agentic.rkt — do not edit.\n"
        "# The base image is built from toolchain `dev` with Nix (`al sandbox LAYER build`).\n"
        "FROM localhost/al-notes-toolchain-dev:latest\n")


def test_features_nix_and_agent_cli_json_are_copied(tmp_path, app) -> None:
    layer = generate(tmp_path, TOOLCHAIN_LAYER, app)
    for name in ("features.nix", "agent-cli.json"):
        assert (layer / "env" / name).read_bytes() == (KIT / "toolchain" / name).read_bytes(), name


def test_gitignore_keeps_the_lock_only_with_a_toolchain(tmp_path, app) -> None:
    layer = generate(tmp_path, TOOLCHAIN_LAYER, app)
    lines = (layer / ".gitignore").read_text().splitlines()
    assert "!/env/flake.lock" in lines and lines.index("!/env/flake.lock") > lines.index("*.lock")
    other = tmp_path / "old"
    other.mkdir()
    old = generate(other, OLD_FORMS, app)
    assert "!/env/flake.lock" not in (old / ".gitignore").read_text()


def test_al_check_requires_toolchain_files(tmp_path, app) -> None:
    layer = generate(tmp_path, TOOLCHAIN_LAYER, app)
    assert "check: ok" in al("check", layer).stdout
    (layer / "env/toolchain-dev.nix").unlink()
    cp = al("check", layer, ok=False)
    assert cp.returncode == 1 and "PROBLEM: missing env/toolchain-dev.nix" in cp.stdout, cp.stdout
