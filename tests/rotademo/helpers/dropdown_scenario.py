"""A one-off capture scenario (ROTA_E2E_SCENARIO) for scenario C's before/after pictures:
sign in as Northfield's manager and show the assign dropdown of open shift 8 (Wednesday night,
registered nurse) with every option, so Chloe Martin (registration expired) can be seen to be
selectable or not. Not part of the app's e2e check; the demo script runs it at two commits.

DROPDOWN_SELECT_CHLOE=1 (used where the probe found her selectable) also selects her, so the
picture shows that she can be chosen.
"""
import os

STEPS = [
    {"goto": "/signin", "say": "open the demo sign-in page"},
    {"click": "#signin-1", "say": "sign in as Priya Shah, ward manager at Northfield Hospital",
     "expect": {"text": ["Shifts · Northfield Hospital"]}},
    {"reveal": "#assign-8", "say": "show the assign dropdown of the Wednesday night shift with all its options"},
]
if os.environ.get("DROPDOWN_SELECT_CHLOE") == "1":
    STEPS.append({"select": "#assign-8", "value": "3", "say": "Chloe Martin (registration expired) can be selected"})
STEPS += [
    {"wait": 300, "screenshot": "assign-dropdown"},
    {"wait": 600},
]
