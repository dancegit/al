#!/usr/bin/env python3
"""Test helper (not a test): a stateful fake `podman`.

Images are kept in <state>/images.json ({id, labels, tags}); a registry is a directory of JSON files,
one per reference. Each call appends its argv as one JSON line to <state>/calls.log; the stdin of
`login --password-stdin` is never logged. Implements what al uses: --version, info, image exists,
image inspect [--format] (JSON with Id, Size, Created, Labels as the runtime's image_info reads it),
build --label -t, tag, untag, rmi, push, pull, save -o / image save -o, load [-i] / image load,
login --authfile --password-stdin, images --filter label=K=V --format json and ps --format json (no
containers): the calls of al_sandbox and of the runtime's ensure_image / build_image / _tag_latest /
prune_images (kit/runtime/adw_modules/sandbox.py).

    install(bin_dir, state_dir, registry_dir) writes bin_dir/podman that runs this file.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()


def install(bin_dir: Path, state: Path, registry: Path | None = None) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    path = Path(bin_dir) / "podman"
    if path.exists() or path.is_symlink():
        path.unlink()
    reg = f" --registry {registry}" if registry else ""
    path.write_text(f'#!/bin/sh\nexec {sys.executable} -I {HERE} --state {state}{reg} "$@"\n')
    path.chmod(0o755)
    return path


def calls(state: Path) -> list[list[str]]:
    log = Path(state) / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


def images(state: Path) -> list[dict]:
    path = Path(state) / "images.json"
    return json.loads(path.read_text()) if path.is_file() else []


def add_image(state: Path, tag: str, labels: dict[str, str], ident: str | None = None) -> str:
    """Put an image into the store directly (a test's arrangement)."""
    store = images(state)
    ident = ident or hashlib.sha256(json.dumps([tag, labels, len(store)]).encode()).hexdigest()
    for image in store:
        image["tags"] = [t for t in image["tags"] if norm(t) != norm(tag)]
    store.append({"id": ident, "labels": labels, "tags": [norm(tag)]})
    _save(state, store)
    return ident


def norm(ref: str) -> str:
    ref = ref.removeprefix("localhost/")
    return ref if ":" in ref.rsplit("/", 1)[-1] else ref + ":latest"


def _save(state: Path, store: list[dict]) -> None:
    store = [i for i in store if i["tags"] or i.get("keep")]
    (Path(state) / "images.json").write_text(json.dumps(store, indent=1))


def _find(store: list[dict], ref: str) -> dict | None:
    ref = norm(ref)
    return next((i for i in store if ref in i["tags"] or i["id"] == ref.removesuffix(":latest")), None)


def _registry_file(registry: Path, ref: str) -> Path:
    return registry / (norm(ref).replace("/", "__") + ".json")


def main(argv: list[str]) -> int:
    state, registry = None, None
    while argv and argv[0] in ("--state", "--registry"):
        if argv[0] == "--state":
            state = Path(argv[1])
        else:
            registry = Path(argv[1])
        argv = argv[2:]
    assert state is not None
    state.mkdir(parents=True, exist_ok=True)
    with (state / "calls.log").open("a") as handle:
        handle.write(json.dumps(argv) + "\n")
    store = images(state)
    if not argv:
        return 125
    if argv[0] == "--version":
        print("podman version 5.0.0-fake")
        return 0
    if argv[0] == "image" and len(argv) > 1 and argv[1] in ("exists", "inspect", "save", "load", "rm", "untag", "tag"):
        argv = argv[1:]
    command, rest = argv[0], argv[1:]
    options = [x for x in rest if x.startswith("-")]
    words = []
    skip = False
    for i, x in enumerate(rest):
        if skip:
            skip = False
            continue
        if x in ("--format", "--label", "-t", "-f", "-o", "-i", "--authfile", "--username", "-u", "--filter"):
            skip = True
            continue
        if x.startswith("-"):
            continue
        words.append(x)

    def value(flag: str) -> str | None:
        return rest[rest.index(flag) + 1] if flag in rest else None

    if command == "info":
        print("true" if "--format" in rest else "host:\n  security:\n    rootless: true")
        return 0
    if command == "exists":
        return 0 if _find(store, words[0]) else 1
    if command == "inspect":
        image = _find(store, words[0])
        if not image:
            print(f"Error: {words[0]}: image not known", file=sys.stderr)
            return 125
        fmt = value("--format") or ""
        if "al.recipe" in fmt:
            print(image["labels"].get("al.recipe", "<no value>"))
        elif ".Id" in fmt:
            print(image["id"])
        else:
            print(json.dumps([dict(image, Id=image["id"], Size=1000000, Created=image.get("created", 0),
                                   Labels=image["labels"])]))
        return 0
    if command == "build":
        labels = {}
        for i, x in enumerate(rest):
            if x == "--label":
                k, _, v = rest[i + 1].partition("=")
                labels[k] = v
        tag = value("-t") or "untagged"
        context = Path(words[-1]) if words else Path(".")
        recipe = (context / "Containerfile").read_bytes() if (context / "Containerfile").is_file() else b""
        ident = hashlib.sha256(recipe + json.dumps(labels, sort_keys=True).encode()).hexdigest()
        add_image(state, tag, labels, ident)
        print(ident)
        return 0
    if command == "images":
        wanted = []
        for i, x in enumerate(rest):
            if x == "--filter" and rest[i + 1].startswith("label="):
                k, _, v = rest[i + 1].removeprefix("label=").partition("=")
                wanted.append((k, v))
        rows = [{"Id": i["id"], "Names": ["localhost/" + t for t in i["tags"]], "Labels": i["labels"], "Size": 1000000,
                 "Created": i.get("created", 0)}
                for i in store if all(i["labels"].get(k) == v for k, v in wanted)]
        print(json.dumps(rows))
        return 0
    if command == "ps":
        print("[]")
        return 0
    if command == "tag":
        image = _find(store, words[0])
        if not image:
            print(f"Error: {words[0]}: image not known", file=sys.stderr)
            return 125
        for other in store:
            other["tags"] = [t for t in other["tags"] if t != norm(words[1])]
        image["tags"].append(norm(words[1]))
        _save(state, store)
        return 0
    if command in ("untag", "rmi", "rm"):
        image = _find(store, words[0])
        if not image:
            return 1
        if command == "untag":                  # untag NAME: that name goes; an image without a name goes too
            image["tags"] = [t for t in image["tags"] if t != norm(words[0])] if norm(words[0]) in image["tags"] else []
        else:
            store.remove(image)
        _save(state, store)
        return 0
    if command == "push":
        if registry is None:
            print("Error: no registry", file=sys.stderr)
            return 125
        image = _find(store, words[0])
        if not image:
            print(f"Error: {words[0]}: image not known", file=sys.stderr)
            return 125
        target = words[1] if len(words) > 1 else words[0]
        registry.mkdir(parents=True, exist_ok=True)
        _registry_file(registry, target).write_text(json.dumps({"id": image["id"], "labels": image["labels"]}))
        return 0
    if command == "pull":
        source = _registry_file(registry, words[0]) if registry else None
        if not source or not source.is_file():
            print(f"Error: initializing source {words[0]}: manifest unknown", file=sys.stderr)
            return 125
        data = json.loads(source.read_text())
        add_image(state, words[0], data["labels"], data["id"])
        print(data["id"])
        return 0
    if command == "save":
        image = _find(store, words[0])
        out = value("-o")
        if not image or not out:
            return 125
        Path(out).write_text(json.dumps({"id": image["id"], "labels": image["labels"], "tags": image["tags"]}))
        return 0
    if command == "load":
        source = value("-i")
        text = Path(source).read_text() if source else sys.stdin.read()
        data = json.loads(text)
        for tag in data["tags"]:
            add_image(state, tag, data["labels"], data["id"])
        print("Loaded image: " + ", ".join("localhost/" + t for t in data["tags"]))
        return 0
    if command == "login":
        if "--password-stdin" in rest:
            sys.stdin.read()
        authfile = value("--authfile")
        if authfile:
            Path(authfile).write_text(json.dumps({"auths": {words[-1] if words else "registry": {"auth": "stub"}}}))
        print("Login Succeeded!")
        return 0
    print(f"fake podman: unsupported: {argv}", file=sys.stderr)
    return 125


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
