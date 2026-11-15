"""Scenario C, checked independently of the agents: what does the assign dropdown offer for Chloe Martin?

Run from an export of the app: python probe_dropdown.py. Prints JSON:
  {"offered": bool, "selectable": bool, "option": "<the option's HTML>", "workers_badge": bool}
Chloe Martin (worker 3) is a registered nurse whose registration expired on 2026-02-14; shift 8 is an
open registered-nurse shift. "selectable" is true when her option is present and not disabled.
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["ROTA_TODAY"] = "2026-03-02"
from app import create_app  # noqa: E402

client = create_app(os.path.join(tempfile.mkdtemp(), "rota.db")).test_client()
client.post("/signin", data={"user_id": 1})
board = client.get("/shifts").get_data(as_text=True)
select = re.search(r'<select[^>]*id="assign-8"[^>]*>(.*?)</select>', board, re.DOTALL)
options = re.findall(r"<option\b[^>]*>.*?</option>", select.group(1), re.DOTALL) if select else []
chloe = [option for option in options if "Chloe Martin" in option]
workers = client.get("/workers").get_data(as_text=True)
row = workers.split('id="worker-3"', 1)[1].split("</tr>", 1)[0] if 'id="worker-3"' in workers else ""
print(json.dumps({
    "offered": bool(chloe),
    "selectable": bool(chloe) and not re.search(r"\bdisabled\b", chloe[0]),
    "option": " ".join(chloe[0].split()) if chloe else "",
    "workers_badge": "Expired" in row,
}))
