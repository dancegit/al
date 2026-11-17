"""The stand-in of this layer: its mode and scope, its decisions and a person's answers.

  standin.py status [--json]
  standin.py set [--mode off|suggest|auto | --reset] [--may K,K | --may-reset] [--pause | --unpause]
                 [--via cli|dashboard] [--json]
  standin.py consider [--suggest-only] [--via loop|asked] [--json]   < item (JSON)
  standin.py apply RUN [--via …] [--json]
  standin.py answer RUN go|alter|explain|reject [--via cli|dashboard] [--json]   < text
  standin.py note [--json]   < entry (JSON)

The one entry for the dashboard's Stand-in panel, `al decider` and the stand-in's loop
(adw_modules/standin.py): the dashboard never imports a layer's runtime, it runs this script.
`consider` lets the stand-in look at one item of the "Needs you" list (a run, or a failed job) and
either act, suggest or leave it with a person; `apply` performs a stored suggestion as the stand-in;
`answer` is a person's answer (decide.answer, the same entry as `al answer`, mail and the webhook);
`note` records what a person did on the page. Exit 0 done, 1 refused or failed, 2 bad use. The
(PEP 723) script header is prepended by `al gen`.
"""
import sys
from pathlib import Path

LAYER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    from adw_modules import standin

    return standin.main(LAYER, sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
