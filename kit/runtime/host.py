"""The entry of a host that runs this layer for another machine (a host of `(placement …)`).

    host.py load                  what the scheduler asks: runs, limits, load, memory, disk, image, logins
    host.py doctor                the tools a run needs here, and which login this host has
    host.py runs                  the runs in this host's storage
    host.py start < seed.json     clone the base commit and start one self-contained run
    host.py pull RUN < cursor     the part of the run's record the home machine does not have yet
    host.py clean RUN             remove the storage of a run that is not running

It prints one JSON document (`pull`: the record frames) on standard output and nothing else. The
home machine calls it over ssh (adw_modules/hostlink.py); everything it does is in
adw_modules/selfrun.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import selfrun  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(selfrun.tool_main(sys.argv[1:], Path(__file__).resolve().parents[1]))
