"""Scenario A, checked independently of the agents: does the app (cwd) enforce 11 hours of rest?

Run from an export of the app: python probe_rest_rule.py. Prints one line per criterion and a JSON
summary; exit 0 only when every criterion holds. Uses the seed data (today = 2026-03-02):
  shift 3 Ash Ward Tue 07:30-19:30 RN; Amara Jones (1) works the night before until Tue 07:30 -> 0 h rest
  shift 5 Birch Ward Tue 14:00-22:00 RN; Sam Okafor (5) starts Wed 07:00 -> 9 h rest before the next shift
  Daniel Reyes (4) finished Mon 19:30 -> 12 h rest before shift 3: allowed
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["ROTA_TODAY"] = "2026-03-02"
from app import create_app  # noqa: E402

results = {}


def check(name, ok, detail=""):
    results[name] = bool(ok)
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def manager():
    directory = tempfile.mkdtemp()
    client = create_app(os.path.join(directory, "rota.db")).test_client()
    client.post("/signin", data={"user_id": 1})
    return client


client = manager()
response = client.post("/api/shifts/3/assign", json={"worker_id": 1})
body = response.get_data(as_text=True)
check("API refuses a shift starting < 11 h after the previous one ends (409 + reason)",
      response.status_code == 409 and "11 hours" in body, f"{response.status_code} {body.strip()[:120]}")
response = manager().post("/api/shifts/5/assign", json={"worker_id": 5})
body = response.get_data(as_text=True)
check("API refuses a shift ending < 11 h before the next one starts (409 + reason)",
      response.status_code == 409 and "11 hours" in body, f"{response.status_code} {body.strip()[:120]}")
response = manager().post("/api/shifts/3/assign", json={"worker_id": 4})
check("API still assigns a worker with 12 h rest", response.status_code == 200, str(response.status_code))

client = manager()
board = client.get("/shifts").get_data(as_text=True)
row = board.split('id="shift-3"', 1)[1].split("</tr>", 1)[0] if 'id="shift-3"' in board else ""
amara = [option for option in re.findall(r"<option[^>]*>[^<]*</option>", row) if "Amara Jones" in option]
check("UI shows the worker as unavailable with 'Needs 11 hours rest' on the shifts board",
      "Needs 11 hours rest" in row, (amara[0] if amara else "no Amara option in shift 3's row")[:160])
page = client.post("/shifts/3/assign", data={"worker_id": 1}, follow_redirects=True).get_data(as_text=True)
check("UI refuses the assignment if it is submitted anyway", "11 hours" in page and "Assigned Amara" not in page)

print(json.dumps(results))
sys.exit(0 if all(results.values()) else 1)
