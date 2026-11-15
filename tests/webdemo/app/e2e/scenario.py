"""The end-to-end user scenario that e2e/capture.py walks in a real browser.

Every user-visible behaviour of the app is demonstrated here, so the screenshots and the video
in e2e-results/ show it. When you add or change something a user can see, add steps below.

A step is a dict with exactly one ACTION key, plus optional keys:

  ACTIONS
    {"goto": "/"}                                   open a path of the app
    {"fill": "#note-text", "value": "Milk"}         set an input's value at once
    {"type": "#note-text", "value": "Milk"}         type key by key (visible in the video)
    {"click": "#add-note"}                          click the first element matching a CSS selector
    {"press": "#note-text", "key": "Enter"}         press a key in an element
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
    "screenshot": "name"                            then save e2e-results/NN-name.png

Expectations are retried for up to two seconds, so behaviour driven by JavaScript has time to
happen. Never loosen an expectation to make the scenario pass; fix the app instead.
"""

STEPS = [
    {"goto": "/", "say": "open the notes page",
     "expect": {"text": ["Notes", "Water the plants", "3 notes"],
                "count": {"selector": "#notes .note", "visible": 3},
                "style": {"selector": "body", "property": "background-color", "equals": "rgb(248, 250, 252)"}},
     "screenshot": "home"},
    {"type": "#note-text", "value": "Buy oat milk", "say": "type a new note"},
    {"click": "#add-note", "say": "add it",
     "expect": {"text": ["Buy oat milk", "4 notes"], "count": {"selector": "#notes .note", "visible": 4}},
     "screenshot": "note-added"},
    {"wait": 1200},
]
