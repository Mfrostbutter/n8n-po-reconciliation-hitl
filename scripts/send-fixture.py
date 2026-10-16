#!/usr/bin/env python3
"""POST a vendor email fixture to the workflow's webhook and print the decision.

Usage: python3 scripts/send-fixture.py data/fixtures/<name>.json [--url http://localhost:5678/webhook/om-po-response] [--test]
A fixture may name a PDF via "attachment_file" (relative to the fixture); it is base64-encoded on the way out.
--test targets the /webhook-test/ path (workflow open in the editor, "Execute workflow" clicked).
"""
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

path = Path(sys.argv[1])
url = "http://localhost:5678/webhook/om-po-response"
if "--url" in sys.argv:
    url = sys.argv[sys.argv.index("--url") + 1]
if "--test" in sys.argv:
    url = url.replace("/webhook/", "/webhook-test/")

body = json.loads(path.read_text(encoding="utf-8"))
att = body.pop("attachment_file", None)
if att:
    pdf = (path.parent / att).read_bytes()
    body["attachment_base64"] = base64.b64encode(pdf).decode()
    body.setdefault("attachment_name", Path(att).name)

req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        print(r.status)
        print(r.read().decode()[:2000])
except urllib.error.HTTPError as e:
    print(e.code)
    print(e.read().decode()[:2000])
