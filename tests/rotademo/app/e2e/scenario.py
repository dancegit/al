"""The end-to-end user scenario that e2e/capture.py walks in a real browser.

Every user-visible behaviour of the app is demonstrated here, so the screenshots and the video
in e2e-results/ show it. When you add or change something a user can see, add steps below.
The app starts on a fresh database seeded by seed.py, with "today" fixed at Monday 2 March 2026.

A step is a dict with exactly one ACTION key, plus optional keys:

  ACTIONS
    {"goto": "/shifts"}                             open a path of the app
    {"fill": "#field", "value": "text"}             set an input's value at once
    {"type": "#field", "value": "text"}             type key by key (visible in the video)
    {"click": "#signin-1"}                          click the first element matching a CSS selector
    {"press": "#field", "key": "Enter"}             press a key in an element
    {"select": "#assign-3", "label": "Daniel Reyes"}   choose a <select> option by its exact text
    {"select": "#assign-3", "value": "4"}           ... or by its value
    {"reveal": "#assign-3"}                         show a <select>'s options as an open list, so the
                                                    screenshot and the video show every option
                                                    (disabled ones greyed out); a page load undoes it
    {"wait": 800}                                   pause, in milliseconds

  OPTIONAL KEYS (on any step)
    "say": "..."                                    one line printed in the e2e log
    "expect": {...}                                 checked after the action; all must hold
        "text": "..." or [...]                      visible page text contains each string
        "no_text": "..." or [...]                   visible page text contains none of them
        "visible": "css"                            at least one matching element is visible
        "hidden": "css"                             no matching element is visible
        "count": {"selector": "css", "visible": 3}  exactly this many matching elements are visible
        "style": {"selector": "css", "property": "background-color", "equals": "rgb(248, 250, 252)"}
                                                    computed CSS value of the first match
        "options": {"selector": "#assign-3", "enabled": [...], "disabled": [...], "absent": [...]}
                                                    the <select>'s options, matched by text containing
                                                    each string: present and selectable / present and
                                                    disabled / not offered at all
    "screenshot": "name"                            then save e2e-results/NN-name.png

Expectations are retried for up to two seconds, so behaviour driven by JavaScript has time to
happen. Never loosen an expectation to make the scenario pass; fix the app instead.

Seed data used below (Northfield Hospital): open shifts 3 (Ash Ward, Tue 07:30-19:30, Registered
nurse), 5, 7 and 8; workers Amara Jones (1), Ben Clarke (2), Chloe Martin (3, registration expired
2026-02-14), Daniel Reyes (4), Sam Okafor (5, agency).
"""

STEPS = [
    {"goto": "/signin", "say": "open the demo sign-in page",
     "expect": {"text": ["Sign in", "Demo sign-in", "Priya Shah", "Tom Becker"]},
     "screenshot": "signin"},
    {"click": "#signin-1", "say": "sign in as Priya Shah, ward manager at Northfield Hospital",
     "expect": {"text": ["Shifts · Northfield Hospital", "Ash Ward", "Birch Ward", "4 open of 8"],
                "no_text": ["Riverside", "Willow Ward"],
                "count": {"selector": "#shifts tr.shift", "visible": 8}},
     "screenshot": "shifts-board"},
    {"select": "#assign-3", "label": "Daniel Reyes", "say": "choose Daniel Reyes for the open Tuesday day shift on Ash Ward"},
    {"click": "#assign-button-3", "say": "assign him",
     "expect": {"text": ["Assigned Daniel Reyes to Ash Ward, Tue 3 Mar 07:30–19:30.", "3 open of 8"],
                "visible": "#shift-3 .status-filled"},
     "screenshot": "assigned"},
    {"select": "#assign-8", "label": "Chloe Martin", "say": "try Chloe Martin, whose registration expired, for the Wednesday night shift"},
    {"click": "#assign-button-8", "say": "the assignment is refused",
     "expect": {"text": ["Cannot assign Chloe Martin: Registration expired.", "3 open of 8"],
                "visible": "#shift-8 .status-open"},
     "screenshot": "expired-refused"},
    {"click": "#unassign-button-3", "say": "unassign Daniel again",
     "expect": {"text": ["Shift is open again.", "4 open of 8"]}},
    {"click": ".nav a[href='/workers']", "say": "open the workers page",
     "expect": {"text": ["Workers · Northfield Hospital", "Registration expires", "Chloe Martin", "2026-02-14"],
                "no_text": ["Elena Novak"],
                "count": {"selector": "#workers tr.worker", "visible": 5}},
     "screenshot": "workers"},
    {"wait": 1200},
]
