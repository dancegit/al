"""The test suites' fixture builders, reused by the showcase without changing them: each module is loaded
by its file path. tests/tools and kit/dashboard/tests both have a `conftest.py` (and their modules
import their siblings by bare name), so each folder is put first on sys.path while its modules load,
and the two conftests are kept under distinct names in sys.modules."""
from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
TOOLS_TESTS = ROOT / "tests" / "tools"
DASH_TESTS = ROOT / "kit" / "dashboard" / "tests"

# bare names each folder's modules import from their siblings; they must resolve to that folder's copy
_SIBLINGS = {TOOLS_TESTS: ("conftest",), DASH_TESTS: ("conftest", "fixtures", "start_layer", "fake_forgejo")}
_SAVED: dict[Path, dict[str, ModuleType]] = {TOOLS_TESTS: {}, DASH_TESTS: {}}


def load(path: Path, name: str) -> ModuleType:
    """The module at `path`, executed once and registered as `name`, with its folder first on sys.path
    and that folder's own sibling modules (conftest, fixtures, …) in sys.modules while it runs."""
    if name in sys.modules:
        return sys.modules[name]
    folder = path.parent
    saved_path = list(sys.path)
    others = {n: sys.modules.pop(n) for n in ("conftest", "fixtures", "start_layer", "fake_forgejo") if n in sys.modules}
    sys.modules.update(_SAVED.get(folder, {}))
    sys.path.insert(0, str(folder))
    if folder == DASH_TESTS:
        sys.path.insert(1, str(folder.parent))      # `import dashboard`
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        if path.stem in _SIBLINGS.get(folder, ()):
            _SAVED[folder][path.stem] = module
        for sibling in _SIBLINGS.get(folder, ()):
            if sibling in sys.modules:
                _SAVED[folder].setdefault(sibling, sys.modules[sibling])
        return module
    finally:
        for sibling in _SIBLINGS.get(folder, ()):
            sys.modules.pop(sibling, None)
        sys.modules.update(others)
        sys.path[:] = saved_path


@lru_cache(maxsize=None)
def tools_conftest() -> ModuleType:
    return load(TOOLS_TESTS / "conftest.py", "showcase_tools_conftest")


def _tools(stem: str) -> ModuleType:
    tools_conftest()
    return load(TOOLS_TESTS / f"{stem}.py", f"showcase_tools_{stem}")


@lru_cache(maxsize=None)
def promote_test() -> ModuleType:
    return _tools("test_al_promote")


@lru_cache(maxsize=None)
def sandbox_test() -> ModuleType:
    return _tools("test_sandbox_cli")


@lru_cache(maxsize=None)
def existing_setup() -> ModuleType:
    return _tools("existing_setup")


@lru_cache(maxsize=None)
def deploy_host() -> ModuleType:
    return _tools("deploy_host")


@lru_cache(maxsize=None)
def workspace_test() -> ModuleType:
    return _tools("test_dashboard_workspace")


@lru_cache(maxsize=None)
def dashboard_helpers() -> ModuleType:
    return load(DASH_TESTS / "conftest.py", "showcase_dashboard_helpers")


def _dash(stem: str) -> ModuleType:
    dashboard_helpers()
    if stem in _SAVED[DASH_TESTS]:          # already imported by the conftest: the same module, not a copy
        return _SAVED[DASH_TESTS][stem]
    return load(DASH_TESTS / f"{stem}.py", f"showcase_dash_{stem}")


@lru_cache(maxsize=None)
def dash_fixtures() -> ModuleType:
    return _dash("fixtures")


@lru_cache(maxsize=None)
def start_layer() -> ModuleType:
    return _dash("start_layer")


@lru_cache(maxsize=None)
def workspace_fixtures() -> ModuleType:
    return _dash("workspace_fixtures")


@lru_cache(maxsize=None)
def review_model() -> ModuleType:
    return _dash("test_review_rejected_model")


@lru_cache(maxsize=None)
def promotion_fixtures() -> ModuleType:
    return _dash("promotion_fixtures")
