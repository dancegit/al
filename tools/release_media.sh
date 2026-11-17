#!/usr/bin/env bash
# The videos of this repository are not tracked: they are assets of a release (docs/media.json).
# This script lists every file of the manifest with its state in the local media folder
# (present: the sha256 matches; missing; checksum differs) and prints the `gh release` commands that
# create the release and upload the files. It uploads nothing and never calls gh: a person runs the
# printed commands.
#
#   tools/release_media.sh                 # media folder: media/<release>/ (ignored by git)
#   AL_MEDIA_DIR=/some/folder tools/release_media.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$ROOT/docs/media.json"
[ -r "$MANIFEST" ] || { echo "release_media: cannot read $MANIFEST" >&2; exit 2; }
if command -v python3 >/dev/null 2>&1; then PY=(python3); else PY=(uv run --no-project python); fi
"${PY[@]}" - "$MANIFEST" "${AL_MEDIA_DIR:-}" <<'PY'
import hashlib
import json
import shlex
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
data = json.loads(manifest.read_text(encoding="utf-8"))
release, repository = data["release"], data["repository"]
folder = Path(sys.argv[2]) if sys.argv[2] else Path("media") / release
where = folder if folder.is_absolute() or sys.argv[2] else manifest.parent.parent / folder


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


print(f"release {release} of {repository}: {len(data['files'])} files; media folder: {folder}")
present, counts = [], {"present": 0, "missing": 0, "checksum differs": 0}
width = max((len(f["name"]) for f in data["files"]), default=0)
for entry in data["files"]:
    path = where / entry["name"]
    if not path.is_file():
        state = "missing"
    elif sha256(path) == entry["sha256"]:
        state = "present"
        present.append(entry["name"])
    else:
        state = "checksum differs"
    counts[state] += 1
    print(f"  {entry['name']:<{width}}  {entry['bytes']:>9} bytes  {state}")
print("  " + ", ".join(f"{n} {state}" for state, n in counts.items()))
print()
print("Nothing was uploaded. To publish the files, run:")
print()
print(f"  gh release create {release} --repo {repository} --title \"Media\" --notes \"Videos of the demos and the showcase\"")
if present:
    files = " ".join(shlex.quote(str(folder / name)) for name in present)
    print(f"  gh release upload {release} --repo {repository} {files}")
else:
    print(f"  gh release upload {release} --repo {repository} <no file is present in {folder}>")
if counts["missing"] or counts["checksum differs"]:
    print()
    print("Files that are missing or differ are not in the upload line: record them again, or take them from")
    print("the history of this repository (the manifest's \"was\" is the path each one had).")
print()
print(f"Then replace the tour placeholder ({data['tour']['placeholder']}) by hand: the [tour] link")
print("definition in README.md and \"tour\".\"placeholder\" in docs/media.json.")
PY
