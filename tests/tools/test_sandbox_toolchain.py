"""`al sandbox LAYER build` and `al sandbox LAYER update` on a layer with a toolchain, the lock line of
`al gen` and `al verify` building the toolchain image first, against stub `nix` and `podman`
executables (tests/tools/conftest.py stub_bin): offline, nothing is built, every call the stubs see is
logged to a file. The image is the layer's recipe image, al-<layer>:<recipe hash>; the same commands
on a layer without a toolchain are in test_sandbox_cli.py."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from conftest import LAYER_TEMPLATE, al, stub_bin
from test_toolchain_gen import OLD_FORMS, TOOLCHAIN_LAYER, generate

# One program stands in for both: its behaviour depends on the name it is called by.
STUB = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
me = Path(sys.argv[0]).name
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps([me, *argv]) + "\\n")
store = Path(os.environ["STUB_STORE"])
store.mkdir(parents=True, exist_ok=True)


def lock(rev, modified):
    return json.dumps({{"nodes": {{"root": {{"inputs": {{"nixpkgs_dev": "nixpkgs_dev"}}}},
                                   "nixpkgs_dev": {{"locked": {{"rev": rev, "lastModified": modified, "type": "github"}}}}}},
                       "root": "root", "version": 7}}, indent=2) + "\\n"


def nix(args, src):
    while args and args[0] in ("--extra-experimental-features", "--option"):
        args = args[2 if args[0] == "--extra-experimental-features" else 3:]
    def where(ref):
        return Path(src if ref == "path:/src" else ref[len("path:"):])
    if args[:2] == ["flake", "lock"]:
        (where(args[-1]) / "flake.lock").write_text(lock("a" * 40, 1700000000))
    elif args[:2] == ["flake", "update"]:
        new = os.environ.get("STUB_NEW_REV", "b" * 40)
        (where(args[args.index("--flake") + 1]) / "flake.lock").write_text(lock(new, 1710000000 if new != "a" * 40 else 1700000000))
    elif args[:1] == ["build"]:
        for item in [a for a in args if "#" in a]:
            path = store / item.split("#", 1)[1]
            path.write_text("#!/bin/sh\\necho image-bytes\\n")
            path.chmod(0o755)
            print(path)
    elif args[:1] == ["path-info"]:
        paths = [a for a in args[1:] if not a.startswith("-")]
        print(json.dumps({{p: {{"closureSize": 1048576 * (i + 1)}} for i, p in enumerate(paths)}}))
    return 0


if me == "nix":
    sys.exit(nix(argv, None))
# podman: an image exists once a `podman build … -t REF` was seen (kept in STUB_STORE/images.json)
images_file = store / "images.json"
images = json.loads(images_file.read_text()) if images_file.exists() else {{}}


def find(ref):
    return images.get(ref[len("localhost/"):] if ref.startswith("localhost/") else ref)


def save():
    images_file.write_text(json.dumps(images))


if argv[:2] == ["image", "exists"]:
    sys.exit(0 if find(argv[2]) else 1)
elif argv[:2] == ["image", "inspect"] and "--format" in argv:
    fmt = argv[argv.index("--format") + 1]
    found = find(argv[-1])
    if "Labels" in fmt:
        if not found or "al.toolchain.key" not in found["Labels"]:
            sys.exit(125)
        print(found["Labels"]["al.toolchain.key"])
    else:
        print(123456789)
elif argv[:2] == ["image", "inspect"]:
    found = find(argv[2])
    print(json.dumps([found] if found else []))
    sys.exit(0 if found else 125)
elif argv[:1] == ["load"]:
    sys.stdin.read()
    print("Loaded image: localhost/al-notes-toolchain-dev:latest")
elif argv[:1] == ["build"] and "-t" in argv:
    ref = argv[argv.index("-t") + 1]
    labels = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--label")
    images[ref] = {{"Id": ("id" + ref.split(":")[1]).ljust(64, "0"), "Size": 123456789, "Created": 1700000000,
                   "Labels": labels}}
    save()
elif argv[:1] == ["tag"]:
    if find(argv[1]):
        images[argv[2]] = find(argv[1])
        save()
elif argv[:1] in (["untag"], ["rmi"]):
    images.pop(argv[1], None)
    save()
elif argv[:1] == ["images"]:
    rows = {{}}
    for ref, info in images.items():
        rows.setdefault(info["Id"], dict(info, Names=[]))["Names"].append("localhost/" + ref)
    print(json.dumps(list(rows.values())))
elif argv[:1] in (["ps"], ["pod"]) and "json" in argv:
    print("[]")
elif argv[:1] == ["run"] and any("nixos/nix@sha256" in a for a in argv):
    i = next(i for i, a in enumerate(argv) if "nixos/nix@sha256" in a)
    src = next(v.split(":")[0] for v in argv if v.endswith(":/src") or v.endswith(":/src:ro"))
    rest = argv[i + 1:]
    if rest[:1] == ["nix"]:
        sys.exit(nix(rest[1:], src))
    print("image-bytes")
"""


def tools(tmp_path: Path, *names: str) -> dict[str, str]:
    """Environment with only the given stubs: real nix and podman are taken off PATH."""
    directory = None
    for name in names:
        directory = stub_bin(tmp_path, name, STUB)
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not (Path(d) / "nix").exists() and not (Path(d) / "podman").exists()]
    path = os.pathsep.join(([str(directory)] if directory else []) + keep)
    return {"PATH": path, "STUB_LOG": str(tmp_path / "stub.log"), "STUB_STORE": str(tmp_path / "store"),
            "AL_SANDBOX_OFFLINE": ""}


def log(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "stub.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


REF = r"al-notes:[0-9a-f]{12}"   # the recipe image


def layer_of(tmp_path, app, source=TOOLCHAIN_LAYER) -> Path:
    return generate(tmp_path, source, app)   # generated offline: no lock yet


def test_gen_offline_says_the_lock_is_not_written(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    out = al("gen", layer).stdout
    assert "lock: env/flake.lock not written: AL_SANDBOX_OFFLINE=1" in out
    assert not (layer / "env/flake.lock").exists()


def test_gen_writes_lock_with_stub_nix_then_keeps_it(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    env = tools(tmp_path, "nix")
    out = al("gen", layer, env=env).stdout
    assert "lock: env/flake.lock written (built with Nix on this host)" in out
    lock = layer / "env/flake.lock"
    text, mtime = lock.read_text(), lock.stat().st_mtime_ns
    out = al("gen", layer, env=env).stdout
    assert "lock: env/flake.lock kept" in out
    assert lock.read_text() == text and lock.stat().st_mtime_ns == mtime
    assert sum(1 for c in log(tmp_path) if c[1:3] == ["--extra-experimental-features", "nix-command flakes"]
               and "lock" in c) == 1


def test_gen_without_nix_or_podman_explains(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    cp = al("gen", layer, env=tools(tmp_path))
    assert cp.returncode == 0
    assert "warning: lock: env/flake.lock not written: neither nix nor podman is on PATH" in cp.stdout
    assert f"al sandbox {layer} update" in cp.stdout


def test_gen_without_toolchain_prints_no_lock_line(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app, OLD_FORMS)
    assert "lock:" not in al("gen", layer, env=tools(tmp_path, "nix")).stdout


def test_sandbox_build_states_the_container_way_without_nix(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    out = al("sandbox", layer, "build", env=tools(tmp_path, "podman")).stdout
    assert re.search(r"^sandbox: " + REF + r" from toolchain `dev` \(no Nix on this host: built in the builder container "
                     r"docker\.io/nixos/nix@sha256:", out, re.M), out
    assert "store volume al-nix-store-" in out
    builder_calls = [c for c in log(tmp_path) if c[:2] == ["podman", "run"]]
    assert builder_calls and all(any("nixos/nix@sha256" in a for a in c) for c in builder_calls)
    assert (layer / "env/flake.lock").is_file()


def test_sandbox_build_states_the_host_way_with_nix(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    env = tools(tmp_path, "nix", "podman")
    out = al("sandbox", layer, "build", env=env).stdout
    assert re.search(r"^sandbox: " + REF + r" from toolchain `dev` \(built with Nix on this host\)$", out, re.M), out
    assert not [c for c in log(tmp_path) if c[:2] == ["podman", "run"]]
    report = json.loads((layer / "agents/sandbox/dev.json").read_text())
    assert report["way"] == "nix" and report["status"] == "built"


def test_sandbox_build_up_to_date_does_not_rebuild(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    env = tools(tmp_path, "nix", "podman")
    al("sandbox", layer, "build", env=env)
    (tmp_path / "stub.log").unlink()
    out = al("sandbox", layer, "build", env=env).stdout
    assert re.fullmatch(r"sandbox: " + REF + r" is up to date \(toolchain dev, lock and declaration unchanged\)",
                        out.strip()), out
    calls = log(tmp_path)
    assert not [c for c in calls if c[1:2] in (["load"], ["build"]) or "build" in c[1:4]], calls


def test_sandbox_build_reports_size_per_feature(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    out = al("sandbox", layer, "build", env=tools(tmp_path, "nix", "podman")).stdout
    lines = out.splitlines()
    assert "  base        1.0 MiB" in lines
    assert "  browser     2.0 MiB" in lines
    assert "  agent-cli   3.0 MiB" in lines
    assert "  image       117.7 MiB" in lines
    assert "report: agents/sandbox/dev.json" in lines


def test_sandbox_update_shows_what_changed(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    env = tools(tmp_path, "nix")
    al("gen", layer, env=env)   # writes the first lock (rev aaaaaaa)
    out = al("sandbox", layer, "update", env=env).stdout
    assert "sandbox: env/flake.lock re-pinned (built with Nix on this host)" in out
    assert "  nixpkgs_dev: aaaaaaa (2023-11-14) → bbbbbbb (2024-03-09)" in out
    assert f"run `al sandbox {layer} build` to rebuild" in out


def test_sandbox_update_no_change(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    env = tools(tmp_path, "nix")
    al("gen", layer, env=env)
    out = al("sandbox", layer, "update", env={**env, "STUB_NEW_REV": "a" * 40}).stdout
    assert "  nixpkgs_dev: aaaaaaa (2023-11-14), unchanged" in out
    assert "env/flake.lock unchanged: nothing to rebuild" in out


def test_transcript_of_the_toolchain_commands(tmp_path, app, capsys) -> None:
    """The user-visible output of the four toolchain commands, asserted and then written past pytest's
    capture, so the test-tools check log shows it."""
    layer = layer_of(tmp_path, app)
    runs = []

    def run(*args, env=None) -> str:
        cp = al(*args, env=env)
        assert cp.returncode == 0
        runs.append((args, cp.stdout))
        return cp.stdout

    out = run("gen", layer, env=tools(tmp_path, "nix"))
    assert "lock: env/flake.lock written" in out
    (tmp_path / "stub-bin" / "nix").unlink()   # podman only: the build takes the container way
    out = run("sandbox", layer, "build", env=tools(tmp_path, "podman"))
    assert "(no Nix on this host: built in the builder container " in out
    lines = out.splitlines()
    for start in ("  browser ", "  agent-cli ", "  image "):
        assert any(line.startswith(start) for line in lines), start
    assert "report: agents/sandbox/dev.json" in lines
    out = run("sandbox", layer, "update", env=tools(tmp_path, "nix"))
    assert "  nixpkgs_dev: aaaaaaa (2023-11-14) → bbbbbbb (2024-03-09)" in out
    out = run("explain", layer)
    assert "toolchain" in out and "dev" in out and "browser" in out and "agent-cli" in out
    report = (layer / "agents/sandbox/dev.json").read_text()
    with capsys.disabled():
        print("\n===== transcript: toolchain commands (stub nix/podman) =====")
        for args, stdout in runs:
            shown = " ".join("<layer>" if a == layer else str(a).replace(str(layer), "<layer>") for a in args)
            print(f"$ al {shown}")
            print(stdout.replace(str(layer), "<layer>"), end="" if stdout.endswith("\n") else "\n")
        print("$ cat agents/sandbox/dev.json")
        print(report, end="" if report.endswith("\n") else "\n")
        print("===== end transcript =====")


def test_sandbox_on_a_layer_without_toolchain_says_so(tmp_path, app) -> None:
    """`update` needs a toolchain; `build` on such a layer is the ordinary build (test_sandbox_cli.py)."""
    layer = layer_of(tmp_path, app, OLD_FORMS)
    cp = al("sandbox", layer, "update", env=tools(tmp_path, "nix", "podman"), ok=False)
    assert cp.returncode == 1
    assert "declares no toolchain" in cp.stderr and "(toolchain nix" in cp.stderr
    assert log(tmp_path) == []


def test_sandbox_build_toolchain_unknown_id_is_an_error(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    cp = al("sandbox", layer, "build", "--toolchain", "nope", env=tools(tmp_path, "nix", "podman"), ok=False)
    assert cp.returncode == 1
    assert "no toolchain `nope`; declared: dev" in cp.stderr
    assert not [c for c in log(tmp_path) if c[1:2] in (["load"], ["build"]) or "build" in c[1:4]]
    out = al("sandbox", layer, "build", "--toolchain", "dev", env=tools(tmp_path, "nix", "podman")).stdout
    assert re.search(r"^sandbox: " + REF + r" from toolchain `dev`", out, re.M), out


def test_verify_builds_the_toolchain_image_first(tmp_path, app) -> None:
    layer = layer_of(tmp_path, app)
    al("verify", layer, env=tools(tmp_path, "nix", "podman"), ok=False)
    podman = [c[1:] for c in log(tmp_path) if c[0] == "podman"]
    label_build = next(i for i, c in enumerate(podman) if c[:1] == ["build"] and "--label" in c)
    first_run = next(i for i, c in enumerate(podman) if c[:1] == ["run"] or c[:1] == ["exec"])
    assert label_build < first_run, podman


assert "(toolchain" not in LAYER_TEMPLATE
