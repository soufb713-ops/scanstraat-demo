"""App-level tests in replay mode: 2FA, encryption at rest, outbox approval gate, retention, region lock.
Run with: bash tests/run.sh"""
import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ.update(SCAN_DATA=str(TMP / "data"), VAULT_KEY=base64.b64encode(os.urandom(32)).decode(), SCAN_AI="replay",
                  ADMIN_EMAIL="admin@demo.local", ADMIN_PASSWORD="EenLangWachtwoord1", ADMIN_NAME="Soufyan",
                  REJECTED_GRACE_HOURS="0", RETENTION_INTERVAL="3600", SCAN_ENV_FILE=str(TMP / "none.env"))
sys.path.insert(0, str(HERE))

import app as appmod  # noqa: E402
import auth  # noqa: E402
import vault  # noqa: E402

CSRF = "t" * 24


@pytest.fixture(scope="module", autouse=True)
def seeded():
    for _ in range(60):
        b = list(appmod.state["batches"].values())
        if b and b[0]["status"] != "processing":
            break
        time.sleep(0.5)
    assert b[0]["status"] == "done", b[0].get("error")


def client():
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    return c


def post(c, url, **data):
    return c.post(url, data={"csrf": CSRF, **data})


def code_for(email, offset=0):
    secret = appmod.users.get(email)["totp_secret"]
    return auth._hotp(secret, int(time.time() // 30) + offset)


def login(c, email="admin@demo.local", pw="EenLangWachtwoord1"):
    r = post(c, "/login", email=email, password=pw)
    assert r.status_code == 302
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    return r


# ---------------------------------------------------------------- 2FA
def test_password_alone_gives_no_access():
    c = client()
    r = login(c)
    assert "/login/2fa-instellen" in r.headers["Location"]
    assert c.get("/").status_code == 302 and "/login" in c.get("/").headers["Location"]


def test_enrol_then_code_required_and_no_replay():
    c = client()
    login(c)
    page = c.get("/login/2fa-instellen").get_data(as_text=True)
    assert "<svg" in page
    with c.session_transaction() as s:
        secret = s["new_totp"]
    step = int(time.time() // 30)
    r = post(c, "/login/2fa-instellen", code=auth._hotp(secret, step))
    assert r.status_code == 302 and c.get("/").status_code == 200
    # second login: same code may not be reused, the next one works
    c2 = client()
    r = login(c2)
    assert "/login/code" in r.headers["Location"]
    assert "klopt niet" in post(c2, "/login/code", code=auth._hotp(secret, step)).get_data(as_text=True)
    assert post(c2, "/login/code", code=auth._hotp(secret, step + 1)).status_code == 302
    assert c2.get("/").status_code == 200


def logged_in():
    c = client()
    login(c)
    u = appmod.users.get("admin@demo.local")
    appmod.users.update("admin@demo.local", totp_last=0)
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    assert post(c, "/login/code", code=auth._hotp(u["totp_secret"], int(time.time() // 30) + 1)).status_code == 302
    appmod.users.update("admin@demo.local", totp_last=0)
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    return c


def test_wrong_codes_lock_out():
    auth._fails.clear()
    c = client()
    login(c)
    for _ in range(5):
        post(c, "/login/code", code="000000")
    assert "Te veel mislukte" in post(c, "/login/code", code=code_for("admin@demo.local", 1)).get_data(as_text=True)
    auth._fails.clear()


# ---------------------------------------------------------------- encryption at rest
def test_everything_on_disk_is_encrypted():
    data = Path(os.environ["SCAN_DATA"])
    plain = []
    for f in data.rglob("*"):
        if not f.is_file() or f.name == ".secret":
            continue
        raw = f.read_bytes()
        if f.suffix in (".jsonl", ".log"):
            ok = all(base64.b64decode(l)[:4] == b"VLT1" for l in raw.splitlines() if l.strip())
        else:
            ok = raw[:4] == b"VLT1"
        if not ok or b"Lindewerf" in raw or b"%PDF" in raw or b"\xff\xd8\xff" in raw[:3]:
            plain.append(str(f.relative_to(data)))
    assert not plain, plain


def test_tampered_or_swapped_file_fails():
    st = Path(os.environ["SCAN_DATA"]) / "settings.json"
    blob = bytearray(st.read_bytes())
    blob[-1] ^= 1
    with pytest.raises(vault.VaultError):
        vault.unseal(bytes(blob), b"settings.json")
    with pytest.raises(vault.VaultError):  # right bytes, wrong path
        vault.unseal(st.read_bytes(), b"clients.json")


# ---------------------------------------------------------------- outbox (Wwft)
def first_doc(kind=None):
    docs = [d for d in appmod.state["docs"].values() if d["status"] == "review" and (kind is None or d["kind"] == kind)]
    return docs[0]


def test_ask_client_only_creates_a_draft():
    c = logged_in()
    d = first_doc("belastingdienst")
    post(c, f"/document/{d['id']}", action="ask", note="Graag de bijlage.", channel="email")
    m = appmod.outbox.all()["messages"][d["asked"]["message"]]
    assert m["status"] == "draft" and m["requires_staff_approval"] is True and m["sent"] is None


def test_approval_needs_both_statements_and_respects_wwft_stop():
    c = logged_in()
    mid = appmod.outbox.drafts()[0]["id"]
    m = appmod.outbox.all()["messages"][mid]
    base = dict(action="send", to="klant@example.org", subject="Vraag", body=m["body"])
    assert "Vink beide" in c.post(f"/berichten/{mid}", data={"csrf": CSRF, **base, "confirm_read": "1"},
                                  follow_redirects=True).get_data(as_text=True)
    post(c, "/klanten", action="wwft_stop", nr=m["client_nr"])
    r = c.post(f"/berichten/{mid}", data={"csrf": CSRF, **base, "confirm_read": "1", "confirm_wwft": "1"}, follow_redirects=True)
    assert "Wwft-stop" in r.get_data(as_text=True)
    assert appmod.outbox.all()["messages"][mid]["status"] == "draft"
    post(c, "/klanten", action="wwft_stop", nr=m["client_nr"])
    post(c, f"/berichten/{mid}", **base, confirm_read="1", confirm_wwft="1")
    m = appmod.outbox.all()["messages"][mid]
    assert m["status"] == "approved" and m["approval"]["by"] == "Soufyan"
    assert "niet verstuurd" in m["sent"]["route"]  # no channel configured in the demo


def test_system_cannot_approve():
    import outbox
    mid = appmod.outbox.draft("email", "x@example.org", "s", "b", "1001", None, "Systeem", "test")
    with pytest.raises(outbox.OutboxError):
        appmod.outbox.approve_and_send(mid, {"name": "Systeem"}, True, True, False)


def test_nothing_calls_transport_except_approval():
    hits = []
    for f in HERE.glob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "_transport(" in line and "def _transport" not in line:
                hits.append((f.name, line.strip()))
            if "send_text(" in line and "def send_text" not in line:
                hits.append((f.name, line.strip()))
    assert sorted(hits) == sorted([("outbox.py", "how = _transport(m)"),
                                   ("outbox.py", 'ok = whatsapp.send_text(m["to"], m["body"])')]), hits


# ---------------------------------------------------------------- retention
def test_purge_only_after_confirmed_export():
    c = logged_in()
    belnet = next(d for d in appmod.state["docs"].values() if (d["result"] or {}).get("counterparty") == "Belnet Mobiel B.V.")
    did = belnet["id"]
    post(c, f"/document/{did}", action="approve", ack="1")
    assert appmod.state["docs"][did]["status"] == "approved"
    pdf = appmod.pdf_path(belnet)
    assert pdf.exists()
    r = c.post("/afhandelen/mijnkantoor", data={"csrf": CSRF, "ids": [did]})
    assert r.status_code == 200 and r.mimetype == "application/zip"
    assert pdf.exists() and not belnet.get("purged")             # ZIP downloaded, not yet confirmed
    post(c, "/afhandelen/bevestigen", key="mijnkantoor", ids=did)
    assert pdf.exists() and not belnet.get("purged")             # invoice: Exact still required
    c.post("/afhandelen/exact", data={"csrf": CSRF, "format": "xml", "ids": [did]})
    post(c, "/afhandelen/bevestigen", key="exact", ids=did)
    assert belnet.get("purged") and not pdf.exists()
    pages = appmod.batch_dir(belnet["batch"]) / "pages"
    assert not any((pages / f"{k}{p:03d}.jpg").exists() for p in belnet["pages"] for k in "pt")
    assert c.get(f"/document/{did}.pdf").status_code == 404
    assert "Origineel gewist" in c.get(f"/document/{did}").get_data(as_text=True)
    events = [e["event"] for e in appmod.read_audit() if e["doc"] == did]
    assert "purged" in events and events.index("purged") > events.index("export_confirmed")


def test_rejected_docs_purged_after_grace():
    c = logged_in()
    d = first_doc()
    post(c, f"/document/{d['id']}", action="reject", note="dubbel")
    appmod.purge_ready()
    assert d.get("purged")


def test_retention_endpoint_is_local_and_token_only():
    c = client()
    assert c.post("/internal/retention", environ_base={"REMOTE_ADDR": "10.0.0.5"},
                  headers={"X-Retention-Token": appmod.retention_token()}).status_code == 403
    assert c.post("/internal/retention", headers={"X-Retention-Token": "x"}).status_code == 403
    r = c.post("/internal/retention", headers={"X-Retention-Token": appmod.retention_token()})
    assert r.status_code == 200 and "purged" in r.get_json()


# ---------------------------------------------------------------- pages render
def test_pages_render():
    c = logged_in()
    for url in ("/", "/berichten", "/beveiliging", "/afhandelen", "/klanten", "/auditlog", "/gebruikers", "/scannen"):
        assert c.get(url).status_code == 200, url


# ---------------------------------------------------------------- region and modes
def run_env(**env):
    e = dict(os.environ, **env, SCAN_DATA=str(TMP / "other"))
    return subprocess.run([sys.executable, "-c", "import app"], cwd=HERE, env=e, capture_output=True, text=True, timeout=60)


def test_production_refuses_non_eu_location():
    r = run_env(APP_ENV="production", HOSTING_LOCATION="us-east", BACKUP_LOCATION="fsn1")
    assert r.returncode != 0 and "fsn1" in r.stderr


def test_old_image_mode_refused():
    r = run_env(SCAN_AI="claude")
    assert r.returncode != 0 and "nooit naar een Amerikaanse" in r.stderr


def test_anthropic_only_imported_in_egress():
    users = [f.name for f in HERE.glob("*.py") if re.search(r"^\s*(import|from)\s+anthropic", f.read_text(), re.M)]
    assert users == ["egress.py"], users
