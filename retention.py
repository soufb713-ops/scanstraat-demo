"""Scheduled clean-up, for cron or a systemd timer:

    */15 * * * *  cd /pad/naar/demo-privacy && .venv/bin/python retention.py

The app also runs the same clean-up itself every RETENTION_INTERVAL seconds and right after every confirmed
export; this script is the safety net if that thread ever stops. It asks the running app to do it (one process
owns the data), only from this machine and with a token derived from the app secret.
Deleted: the original scan, page images, document PDF and the pseudonymisation mapping of every document that
all required destinations confirmed (MijnKantoor, and Exact for invoices/receipts), and of documents taken out
of the pile more than REJECTED_GRACE_HOURS ago.
"""
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

import envfile  # noqa: F401

HERE = Path(__file__).parent
DATA = Path(os.environ.get("SCAN_DATA", HERE / "data"))
secret = os.environ.get("SECRET_KEY") or (DATA / ".secret").read_text()
token = hashlib.sha256(("retention:" + secret).encode()).hexdigest()
url = f"http://127.0.0.1:{os.environ.get('PORT', '5000')}/internal/retention"
req = urllib.request.Request(url, method="POST", headers={"X-Retention-Token": token})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        print("Gewist:", json.loads(r.read())["purged"], "documenten")
except Exception as e:  # noqa: BLE001
    print("Opruimen mislukt:", e, file=sys.stderr)
    sys.exit(1)
