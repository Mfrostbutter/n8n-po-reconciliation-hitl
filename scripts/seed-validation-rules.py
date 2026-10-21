#!/usr/bin/env python3
"""Create and seed the data table om_poc_validation_rules from data/validation-rules.json. Standard library only.

Usage: N8N_API_URL=http://localhost:5678 N8N_API_KEY=... python3 scripts/seed-validation-rules.py [--project "<team project name>"]
Without --project the table is created in your personal project. Re-running upserts every row by rule_id,
so edits made in the n8n UI to other columns survive only if you also change the seed file.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "validation-rules.json"
BASE = os.environ.get("N8N_API_URL", "http://localhost:5678").rstrip("/")
KEY = os.environ.get("N8N_API_KEY")
if not KEY:
    sys.exit("N8N_API_KEY not set")
PROJECT_NAME = sys.argv[sys.argv.index("--project") + 1] if "--project" in sys.argv else None


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}/api/v1{path}", data=data, method=method)
    req.add_header("X-N8N-API-KEY", KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:600]}


def page(path):
    out, cursor = [], None
    while True:
        code, body = api("GET", path + ("&" if "?" in path else "?") + "limit=100" + (f"&cursor={cursor}" if cursor else ""))
        if code != 200:
            sys.exit(f"GET {path} -> {code} {body}")
        out.extend(body.get("data", []))
        cursor = body.get("nextCursor")
        if not cursor:
            return out


seed = json.loads(SEED.read_text(encoding="utf-8"))
projects = page("/projects")
if PROJECT_NAME:
    match = [p for p in projects if p["name"] == PROJECT_NAME]
else:
    match = [p for p in projects if p.get("type") == "personal"]
if not match:
    sys.exit(f"project not found: {PROJECT_NAME or 'personal'}; projects on {BASE}: {[p['name'] for p in projects]}")
project = match[0]

tables = [t for t in page("/data-tables") if t["name"] == seed["table"] and t.get("projectId") == project["id"]]
if tables:
    table = tables[0]
    print(f"table {seed['table']} exists in '{project['name']}' ({table['id']})")
    have = {c["name"] for c in table.get("columns", [])}
    missing = [c["name"] for c in seed["columns"] if c["name"] not in have]
    if missing:
        sys.exit(f"table is missing columns {missing}; add them in the UI or recreate the table")
else:
    code, table = api("POST", "/data-tables", {"name": seed["table"], "columns": seed["columns"], "projectId": project["id"]})
    if code not in (200, 201):
        sys.exit(f"create table -> {code} {table}")
    print(f"created table {seed['table']} in '{project['name']}' ({table['id']})")

tid = table["id"]
for row in seed["rows"]:
    code, resp = api("POST", f"/data-tables/{tid}/rows/upsert",
                     {"filter": {"type": "and", "filters": [{"columnName": "rule_id", "condition": "eq", "value": row["rule_id"]}]},
                      "data": row})
    if code not in (200, 201):
        sys.exit(f"upsert {row['rule_id']} -> {code} {resp}")

rows = page(f"/data-tables/{tid}/rows")
print(f"{len(rows)} rows in {seed['table']} ({tid}). The GetValidationRules node binds the table by name; "
      f"if your instance cannot resolve it, open the node and pick the table from the list.")
for r in sorted(rows, key=lambda r: str(r.get("rule_id"))):
    print(f"  {r.get('rule_id'):<15} {r.get('kind'):<8} weight={r.get('weight')} hard={r.get('hard')} enabled={r.get('enabled')} threshold={r.get('threshold')}")
