"""Scenario B, checked independently of the agents: is each worker's pay band recorded and shown?

Run from an export of the app: python probe_pay_band.py. Exit 0 only when every check holds.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["ROTA_TODAY"] = "2026-03-02"
from app import create_app  # noqa: E402

results = {}


def check(name, ok, detail=""):
    results[name] = bool(ok)
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


directory = tempfile.mkdtemp()
app = create_app(os.path.join(directory, "rota.db"))
conn = sqlite3.connect(app.config["DATABASE"])
columns = [row[1] for row in conn.execute("PRAGMA table_info(workers)")]
band_columns = [name for name in columns if "band" in name.lower()]
check("the workers table has a pay band column (from a migration)", bool(band_columns), str(columns))
if band_columns:
    empty = conn.execute(f"SELECT COUNT(*) FROM workers WHERE {band_columns[0]} IS NULL OR {band_columns[0]} = ''").fetchone()[0]
    check("every seeded worker has a pay band", empty == 0, f"{empty} without")
conn.close()

client = app.test_client()
client.post("/signin", data={"user_id": 1})
page = client.get("/workers").get_data(as_text=True)
check("the workers page has a pay band column", re.search(r"pay band", page, re.IGNORECASE) is not None)
rows = re.findall(r'<tr class="worker"[^>]*>(.*?)</tr>', page, re.DOTALL)
check("the workers page lists Northfield's 5 workers", len(rows) == 5, str(len(rows)))
api = client.get("/api/workers").get_json() or {}
workers = api.get("workers", [])
check("GET /api/workers includes the pay band", bool(workers) and all(any("band" in key for key in w) for w in workers),
      str(sorted(workers[0]) if workers else api)[:160])
print(json.dumps(results))
sys.exit(0 if all(results.values()) else 1)
