"""Selection of the tests a change concerns (adw_modules/selection.py): changed tests, the declared
path map, import graphs for Python, JavaScript/TypeScript, Go and Rust, the codebase's own selector,
their union and order, `#:test-root`, and filling `{tests}` / `{files}` / `{packages}`. Runs on the
tiny fixture repositories under tests/data/selection; no run, no agent."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from adw_modules import selection
from adw_modules.ir import load

DATA = Path(__file__).resolve().parent / "data" / "selection"


def _repo(tmp_path: Path, language: str, into: str = "") -> Path:
    """A copy of one fixture repository (no git needed: selection reads the tree as it is)."""
    repo = tmp_path / "repo"
    shutil.copytree(DATA / language, repo / into if into else repo, ignore=shutil.ignore_patterns("conftest.py"),
                    dirs_exist_ok=True)
    return repo


def _cmd(**over) -> dict:
    return {"name": "unit", "run": "pytest -q", "covers": ["pkg", "tests"], "always": False, "reuse": True,
            "targeted": "pytest -q {tests}", **over}


def _layer(make_layer, repo: Path, cmd: dict):
    return load(make_layer({"codebase": {"root": str(repo), "commands": [cmd]}}))


def _select(make_layer, repo: Path, changed: list[str], **over) -> selection.Selection:
    cmd = _cmd(**over)
    return selection.select(_layer(make_layer, repo, cmd), cmd, changed, root=repo)


# ---------------------------------------------------------------- strategies

def test_changed_tests_always_first_and_within_covers(tmp_path, make_layer):
    repo = _repo(tmp_path, "python")
    (repo / "other").mkdir()
    (repo / "other" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    result = _select(make_layer, repo, ["other/test_x.py", "pkg/core.py", "tests/test_plugin.py"], select=["imports"])
    assert result.tests == ["tests/test_plugin.py", "tests/test_core.py", "tests/test_util.py"]
    assert result.by_strategy == {"changed-tests": 1, "imports": 2}
    assert result.unmapped == []
    assert selection.is_test("tests/test_core.py", None) and not selection.is_test("pkg/core.py", None)


def test_changed_test_that_was_deleted_is_not_selected(tmp_path, make_layer):
    repo = _repo(tmp_path, "python")
    result = _select(make_layer, repo, ["tests/test_gone.py"], select=["changed-tests"])
    assert result.tests == [] and result.unmapped == []


def test_path_map_name_and_dir(tmp_path, make_layer):
    repo = _repo(tmp_path, "python")
    (repo / "lib" / "deep" / "tests").mkdir(parents=True)
    (repo / "lib" / "deep" / "thing.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "lib" / "deep" / "tests" / "test_thing.py").write_text("def test_thing():\n    pass\n", encoding="utf-8")
    path_map = [{"source": "pkg/{name}.py", "tests": "tests/test_{name}.py"},
                {"source": "lib/{dir}/{name}.py", "tests": "lib/{dir}/tests/test_{name}.py"}]
    result = _select(make_layer, repo, ["lib/deep/thing.py", "pkg/plugin.py", "pkg/__init__.py"],
                     covers=[], select=["path-map"], pathMap=path_map)
    assert result.tests == ["lib/deep/tests/test_thing.py", "tests/test_plugin.py"]
    assert result.by_strategy == {"path-map": 2}
    assert result.unmapped == ["pkg/__init__.py"]  # tests/test___init__.py does not exist: nothing selected


def test_python_imports_transitive(tmp_path, make_layer):
    repo = _repo(tmp_path, "python")
    result = _select(make_layer, repo, ["pkg/core.py"], select=["imports"])
    assert result.tests == ["tests/test_core.py", "tests/test_util.py"]
    assert "tests/test_plugin.py" not in result.tests  # loaded by name only: the batch catches what this misses
    assert result.unmapped == []


def test_python_from_package_import_module(tmp_path, make_layer):
    repo = _repo(tmp_path, "python")
    (repo / "pkg" / "rel.py").write_text("from .core import total\n\nX = total([1])\n", encoding="utf-8")
    (repo / "tests" / "test_rel.py").write_text("from pkg.rel import X\n\n\ndef test_x():\n    assert X == 1\n",
                                                encoding="utf-8")
    assert _select(make_layer, repo, ["pkg/util.py"], select=["imports"]).tests == ["tests/test_util.py"]
    assert _select(make_layer, repo, ["pkg/core.py"], select=["imports"]).tests == [
        "tests/test_core.py", "tests/test_rel.py", "tests/test_util.py"]  # a relative import counts too


def test_typescript_imports_relative_and_index(tmp_path, make_layer):
    repo = _repo(tmp_path, "typescript")
    (repo / "src" / "lib").mkdir()
    (repo / "src" / "lib" / "index.ts").write_text("export const one = 1;\n", encoding="utf-8")
    (repo / "test" / "lib.spec.ts").write_text("import { one } from '../src/lib';\n", encoding="utf-8")
    result = _select(make_layer, repo, ["src/a.ts"], covers=["src", "test"], select=["imports"])
    assert result.tests == ["src/a.test.ts", "test/b.spec.ts"]
    index = _select(make_layer, repo, ["src/lib/index.ts"], covers=["src", "test"], select=["imports"])
    assert index.tests == ["test/lib.spec.ts"]


def test_javascript_require(tmp_path, make_layer):
    repo = _repo(tmp_path, "typescript")
    result = _select(make_layer, repo, ["src/c.js"], covers=["src", "test"], select=["imports"])
    assert result.tests == ["src/c.test.js"]


def test_go_packages(tmp_path, make_layer):
    repo = _repo(tmp_path, "go")
    result = _select(make_layer, repo, ["cart/cart.go"], covers=[], select=["imports"], targeted="go test {packages}")
    assert result.packages == ["./cart", "./price"]
    assert result.tests == ["cart/cart_test.go", "price/price_test.go"]
    assert selection.fill("go test {packages}", result) == "go test ./cart ./price"
    only = _select(make_layer, repo, ["price/price.go"], covers=[], select=["imports"])
    assert only.packages == ["./price"] and only.unmapped == []


def test_rust_crates(tmp_path, make_layer):
    repo = _repo(tmp_path, "rust")
    result = _select(make_layer, repo, ["core/src/lib.rs"], covers=[], select=["imports"],
                     targeted="cargo test {packages}")
    assert result.packages == ["core", "app"]
    assert selection.fill("cargo test {packages}", result) == "cargo test -p core -p app"
    assert _select(make_layer, repo, ["app/src/main.rs"], covers=[], select=["imports"]).packages == ["app"]


def test_other_language_contributes_nothing(tmp_path, make_layer):
    repo = _repo(tmp_path, "python")
    (repo / "pkg" / "legacy.rb").write_text("puts 1\n", encoding="utf-8")
    result = _select(make_layer, repo, ["pkg/legacy.rb"], select=["changed-tests", "imports"])
    assert result.tests == [] and result.packages == []
    assert result.unmapped == ["pkg/legacy.rb"]


# ---------------------------------------------------------------- union, selector, test root, fill

def test_union_keeps_order_and_counts_new_per_strategy(tmp_path, make_layer):
    repo = _repo(tmp_path, "python")
    result = _select(make_layer, repo, ["pkg/core.py", "tests/test_util.py"],
                     select=["changed-tests", "path-map", "imports"],
                     pathMap=[{"source": "pkg/{name}.py", "tests": "tests/test_{name}.py"}])
    assert result.tests == ["tests/test_util.py", "tests/test_core.py"]  # imports found both again: counted once
    assert result.by_strategy == {"changed-tests": 1, "path-map": 1}


def test_learned_entries_are_always_added(tmp_path, make_layer):
    repo = _repo(tmp_path, "python")
    cmd = _cmd(select=["imports"])
    layer = _layer(make_layer, repo, cmd)
    from adw_modules import testing

    testing.learn(layer, "unit", ["pkg/plugin.py"], ["tests/test_plugin.py::test_plugin_by_name"], "b0000001")
    result = selection.select(layer, cmd, ["pkg/plugin.py"], root=repo)
    assert result.tests == ["tests/test_plugin.py::test_plugin_by_name"]
    assert result.by_strategy == {"learned": 1} and result.unmapped == []


def test_command_strategy_reads_lines_and_survives_failure(tmp_path, make_layer):
    repo = _repo(tmp_path, "python")
    script = ("printf '%s' \"$AL_CHANGED\" > seen.txt; printf '\\n%s' \"$AL_BASE\" >> seen.txt; "
              "printf 'tests/test_plugin.py\\n\\n'")
    cmd = _cmd(select=["command"], selectCmd=script)
    layer = _layer(make_layer, repo, cmd)
    result = selection.select(layer, cmd, ["pkg/plugin.py"], root=repo, base="abc123")
    assert result.tests == ["tests/test_plugin.py"] and result.by_strategy == {"command": 1}
    assert (repo / "seen.txt").read_text(encoding="utf-8") == "pkg/plugin.py\nabc123"
    failing = _cmd(select=["command"], selectCmd="echo broken >&2; exit 3")
    result = selection.select(_layer(make_layer, repo, failing), failing, ["pkg/plugin.py"], root=repo)
    assert result.tests == [] and result.unmapped == ["pkg/plugin.py"]
    assert any("select-cmd" in note and "exit 3" in note for note in result.notes)


def test_test_root_relativises_and_drops_outside(tmp_path, make_layer):
    repo = _repo(tmp_path, "python", into="app")
    (repo / "tools").mkdir()
    (repo / "tools" / "test_tool.py").write_text("def test_tool():\n    pass\n", encoding="utf-8")
    cmd = _cmd(covers=[], testRoot="app", select=["imports"], targeted="pytest -q {tests} --files {files}")
    result = selection.select(_layer(make_layer, repo, cmd), cmd, ["app/pkg/core.py", "tools/test_tool.py"], root=repo)
    assert result.tests == ["tests/test_core.py", "tests/test_util.py"]
    assert result.files == ["pkg/core.py"]
    assert any("tools/test_tool.py" in note and "test-root" in note for note in result.notes)


def test_fill_quotes_paths():
    chosen = selection.Selection(tests=["tests/test a.py", "tests/b.py"], files=["src/x y.py"])
    assert selection.fill("pytest -q {tests}", chosen) == "pytest -q 'tests/test a.py' tests/b.py"
    assert selection.fill("lint {files}", chosen) == "lint 'src/x y.py'"
    assert selection.fill("go test {packages}", selection.Selection()) == "go test "


@pytest.mark.parametrize("path, expected", [
    ("tests/test_core.py", True), ("pkg/core_test.py", True), ("cart/cart_test.go", True),
    ("src/a.test.ts", True), ("test/b.spec.tsx", True), ("src/__tests__/x.js", True),
    ("core/tests/basic.rs", True), ("pkg/core.py", False), ("src/a.ts", False), ("core/src/lib.rs", False),
])
def test_is_test_patterns(path, expected):
    assert selection.is_test(path, None) is expected
