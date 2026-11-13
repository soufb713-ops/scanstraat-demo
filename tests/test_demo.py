"""Demo instance end to end: live reading of page images through egress.send_images (against the stand-in, never
the real API), the read-only guest tour, the pre-enrolled admin 2FA key and the UBL export.
Run with: bash tests/run.sh"""
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ["STANDIN_LOG"] = str(TMP / "requests.jsonl")
sys.path.insert(0, str(HERE / "tests"))
import standins  # noqa: E402

srv = ThreadingHTTPServer(("127.0.0.1", 0), standins.Anthropic)
threading.Thread(target=srv.serve_forever, daemon=True).start()
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
os.environ.update(SCAN_DATA=str(TMP / "data"), VAULT_KEY=base64.b64encode(os.urandom(32)).decode(), SCAN_AI="demo",
                  DEMO_INSTANCE="1", GUEST_TOUR="1", AI_PROVIDER="anthropic", ANTHROPIC_API_KEY="test-standin",
                  ANTHROPIC_BASE_URL=f"http://127.0.0.1:{srv.server_port}", ADMIN_EMAIL="admin@demo.local",
                  ADMIN_PASSWORD="EenLangWachtwoord1", ADMIN_NAME="Soufyan", ADMIN_TOTP_SECRET=SECRET.lower(),
                  SCAN_ENV_FILE=str(TMP / "none.env"), RETENTION_INTERVAL="3600")
os.environ.pop("APP_ENV", None)
sys.path.insert(0, str(HERE))

import app as appmod  # noqa: E402
import auth  # noqa: E402
import egress  # noqa: E402
import ubl  # noqa: E402

CSRF = "t" * 24
NS = {"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"}


def wait(bid):
    for _ in range(120):
        if appmod.state["batches"][bid]["status"] != "processing":
            return appmod.state["batches"][bid]
        time.sleep(0.25)
    raise AssertionError("batch did not finish")


@pytest.fixture(scope="module")
def batches():
    sample = wait(next(iter(appmod.state["batches"])))   # first start seeds the fictional sample (replay)
    stack = HERE / "samples" / "scan_2026-09-30_0915.pdf"
    own = wait(appmod.create_batch([("eigen-stapel.pdf", stack.read_bytes())], "upload", "Soufyan"))
    return sample, own


def client():
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    return c


def post(c, url, **data):
    return c.post(url, data={"csrf": CSRF, **data})


_admin = {}


def admin():
    """The logged-in admin client, made once: a TOTP code is never accepted twice, so a second login inside the
    same 30 seconds would be refused (as it should be)."""
    if "c" in _admin:
        with _admin["c"].session_transaction() as s:
            s["csrf"] = CSRF
        return _admin["c"]
    c = client()
    r = post(c, "/login", email="admin@demo.local", password="EenLangWachtwoord1")
    assert r.headers["Location"].endswith("/login/code")      # pre-enrolled: no QR set-up step
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    r = post(c, "/login/code", code=auth._hotp(SECRET, int(time.time() // 30)))
    assert r.status_code == 302 and c.get("/").status_code == 200
    with c.session_transaction() as s:   # a login starts a fresh session
        s["csrf"] = CSRF
    _admin["c"] = c
    return c


def guest():
    c = client()
    r = c.get("/rondleiding")
    assert r.status_code == 302
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    return c


def sent_requests():
    return [json.loads(line) for line in open(os.environ["STANDIN_LOG"])]


# ---------------------------------------------------------------- live reading (demo mode)
def test_sample_is_replayed_and_own_stack_read_from_images(batches):
    sample, own = batches
    assert sample["status"] == "done" and sample["mode"] == "replay"
    assert own["status"] == "done" and own["mode"] == "demo", own.get("error")
    reqs = sent_requests()
    assert reqs, "nothing reached the stand-in"
    for b in reqs:
        images = [c for m in b["messages"] for c in m["content"] if c["type"] == "image"]
        assert 1 <= len(images) <= egress.MAX_IMAGES
        assert b["model"] == "claude-opus-5-5" and b["output_config"]["format"]["type"] == "json_schema"
    docs = [appmod.state["docs"][d] for d in own["docs"]]
    assert docs and all(d["status"] == "review" for d in docs)
    assert all(d["trail"]["route"] == appmod.ai.VISION_ROUTE for d in docs)
    assert docs[0]["result"]["counterparty"] == "Belnet Mobiel B.V."


def test_every_image_request_is_logged_with_destination(batches):
    log = appmod.vault.read_lines(appmod.EGRESS_LOG)
    sent = [e for e in log if e["result"] == "sent"]
    assert sent and all(e["images"] >= 1 and e["destination"] == "Anthropic API (VS)" and len(e["sha256"]) == 64
                        for e in sent)


def test_images_never_leave_a_production_instance():
    code = ("import os; os.environ.update(APP_ENV='production'); import egress\n"
            "try:\n    egress.send_images('read', 's', [b'x'], [], {})\nexcept egress.EgressBlocked as e:\n"
            "    print('blocked', e)")
    env = dict(os.environ, DEMO_INSTANCE="1")
    r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
    assert "blocked" in r.stdout, r.stderr


def run_env(**env):
    e = dict(os.environ, **env, SCAN_DATA=str(TMP / "other"))
    for k, v in env.items():
        if v is None:
            e.pop(k, None)
    return subprocess.run([sys.executable, "-c", "import app"], cwd=HERE, env=e, capture_output=True, text=True, timeout=60)


def test_demo_mode_refused_outside_a_demo_instance():
    r = run_env(DEMO_INSTANCE=None)
    assert r.returncode != 0 and "DEMO_INSTANCE=1" in r.stderr
    r = run_env(APP_ENV="production", HOSTING_LOCATION="fsn1", BACKUP_LOCATION="fsn1")
    assert r.returncode != 0 and "DEMO_INSTANCE" in r.stderr


def test_bedrock_only_in_an_eu_region():
    code = "import egress; print(egress.DESTINATION, egress.model_id(), type(egress._client()).__name__)"
    env = dict(os.environ, AI_PROVIDER="bedrock", AWS_REGION="eu-west-1", AWS_BEARER_TOKEN_BEDROCK="test")
    r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "eu-west-1" in r.stdout and "anthropic.claude-opus-5-5" in r.stdout and "AnthropicBedrockMantle" in r.stdout
    env["AWS_REGION"] = "us-east-1"
    r = subprocess.run([sys.executable, "-c", "import egress"], cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "EU-regio" in r.stderr


def test_production_hybrid_only_through_bedrock_in_the_eu():
    prod = dict(APP_ENV="production", HOSTING_LOCATION="fsn1", BACKUP_LOCATION="fsn1", SCAN_AI="hybrid",
                DEMO_INSTANCE=None, GUEST_TOUR=None)
    r = run_env(**prod, AI_PROVIDER="anthropic")
    assert r.returncode != 0 and "AI_PROVIDER=bedrock" in r.stderr
    r = run_env(**prod, AI_PROVIDER="bedrock", AWS_REGION="eu-west-1", AWS_BEARER_TOKEN_BEDROCK="test")
    assert "AI_PROVIDER=bedrock" not in r.stderr


# ---------------------------------------------------------------- guest tour
def test_guest_works_only_in_the_simulation_of_the_fictional_sample(batches):
    """A guest never reads stored state: not the seeded sample batch and certainly not a staff member's own scan.
    The tour pages run on the sample file in the repository; the guest's actions stay in the browser."""
    sample, own = batches
    c = guest()
    home = c.get("/").get_data(as_text=True)
    assert "Bekijk de rondleiding" in home and "eigen-stapel.pdf" not in home      # the homepage stays the homepage
    for url in ("/stapel", "/werkbank", "/afhandelen", "/klanten", "/beveiliging"):
        r = c.get(url)
        assert r.status_code == 200, url
        page = r.get_data(as_text=True)
        assert "Rondleiding" in page and "eigen-stapel.pdf" not in page, url
    for b in (sample, own):     # stored batches, also the seeded one: same answer, so nothing leaks about what exists
        assert c.get(f"/scan/{b['id']}").status_code == 403
        assert c.get(f"/api/scan/{b['id']}").status_code == 403
        assert c.get(f"/document/{b['docs'][0]}").status_code == 403
        assert c.get(f"/document/{b['docs'][0]}.pdf").status_code == 403
        assert c.get(f"/document/{b['docs'][0]}/ubl.xml").status_code == 403
        assert c.get(f"/pagina/{b['id']}/1.jpg").status_code == 403
    assert c.get("/api/cockpit").status_code == 403
    for url in ("/berichten", "/scannen", "/"):
        assert c.get(url).status_code in (200, 403), url
    assert c.get("/berichten").status_code == 403 and c.get("/scannen").status_code == 403
    data = c.get("/api/voorbeeld.json").get_data(as_text=True)
    assert "eigen-stapel.pdf" not in data and "Bakkerij Van Dijk B.V." in data
    assert c.get("/voorbeeld/p/1.jpg").headers["Content-Type"] == "image/jpeg"
    assert c.get("/voorbeeld/p/999.jpg").status_code == 404


def test_tour_link_code_is_logged_without_visitor_details():
    c = client()
    assert c.get("/rondleiding?k=evertse-administraties<script>").status_code == 302
    e = [x for x in appmod.read_audit() if x["event"] == "guest_tour"][-1]
    assert e["details"] == {"via": "evertse-administratiesscript"} and "ip" not in e["details"]


def test_guest_cannot_change_or_download_anything(batches):
    sample, _ = batches
    c = guest()
    did = sample["docs"][0]
    assert post(c, f"/document/{did}", action="approve", ack="1").status_code == 403
    assert appmod.state["docs"][did]["status"] == "review"
    assert post(c, "/scannen", sample="1").status_code == 403
    for url in ("/auditlog", "/gebruikers", "/koppelingen", "/afhandelen/register.xlsx", "/account/wachtwoord"):
        r = c.get(url)
        assert r.status_code == 403 and "rondleiding" in r.get_data(as_text=True).lower(), url
    assert not any(d["exports"].get("excel") for d in appmod.state["docs"].values())


def test_guest_tour_off_by_default():
    code = ("import app; c = app.app.test_client(); r = c.get('/rondleiding'); "
            "print(r.status_code, r.headers.get('Location'))")
    env = dict(os.environ, GUEST_TOUR="0", SCAN_DATA=str(TMP / "off"), SCAN_SEED="0")
    r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[-2:] == ["302", "/login"]


# ---------------------------------------------------------------- UBL
def check_ubl(xml, credit=False):
    root = ET.fromstring(xml)
    assert root.find("cbc:CustomizationID", NS).text.endswith("nlcius:v1.0")
    total = root.find("cac:LegalMonetaryTotal", NS)
    net = float(total.find("cbc:TaxExclusiveAmount", NS).text)
    gross = float(total.find("cbc:PayableAmount", NS).text)
    tax = float(root.find("cac:TaxTotal/cbc:TaxAmount", NS).text)
    assert round(net + tax, 2) == round(gross, 2)
    pdf = root.find("cac:AdditionalDocumentReference/cac:Attachment/cbc:EmbeddedDocumentBinaryObject", NS)
    assert base64.b64decode(pdf.text).startswith(b"%PDF")
    return root


def test_ubl_only_from_approved_fields(batches):
    _, own = batches
    c = admin()
    did = own["docs"][0]
    assert c.get(f"/document/{did}/ubl.xml").status_code == 400   # not approved yet
    post(c, f"/document/{did}", action="approve", ack="1")
    assert appmod.state["docs"][did]["status"] == "approved"
    root = check_ubl(c.get(f"/document/{did}/ubl.xml").data)
    assert "goedgekeurd door een medewerker" in root.find("cbc:Note", NS).text
    r = c.post("/afhandelen/ubl", data={"csrf": CSRF, "ids": [did]})
    assert r.status_code == 200 and r.mimetype == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert len(names) == 1 and names[0].startswith("Studio_Lindewerf_B.V/") and names[0].endswith(".xml")
    assert {"route": "zip", "confirmed": False}.items() <= appmod.state["docs"][did]["exports"]["ubl"].items()


def test_confirmed_ubl_import_wipes_the_original(batches):
    _, own = batches
    c = admin()
    did = next(d for d in own["docs"][1:] if appmod.state["docs"][d]["kind"] in appmod.UBL_KINDS)
    post(c, f"/document/{did}", action="approve", ack="1")
    s = appmod.settings()
    keep = s["retention_required"]
    try:
        s["retention_required"] = ["ubl"]   # a SnelStart firm: UBL is the only destination
        appmod.SETTINGS.save(s)
        assert c.post("/afhandelen/ubl", data={"csrf": CSRF, "ids": [did]}).status_code == 200
        assert not appmod.state["docs"][did].get("purged")   # a download alone proves nothing
        post(c, "/afhandelen/bevestigen", key="ubl", ids=[did])
        doc = appmod.state["docs"][did]
        assert doc["exports"]["ubl"]["confirmed"] is True and doc["purged"]["why"] == "doorgezet en bevestigd"
        assert not appmod.pdf_path(doc).exists()
    finally:
        s = appmod.settings()
        s["retention_required"] = keep
        appmod.SETTINGS.save(s)


def sim_post(c, url, body):
    return c.post(url, json=body, headers={"X-CSRF": CSRF})


def test_guest_gets_concept_exports_of_the_simulation_and_nothing_is_stored(batches):
    c = guest()
    docs, audit_before = len(appmod.state["docs"]), len(appmod.read_audit())
    approved = [{"id": "B-MOLEN", "status": "approved"}, {"id": "B-VERKOOP", "status": "approved"},
                {"id": "M-BGR-KB", "status": "approved"}]
    r = sim_post(c, "/api/voorbeeld/export", {"want": "ubl", "id": "B-MOLEN", "docs": approved})
    root = check_ubl(r.data)
    assert root.find("cbc:Note", NS).text.startswith("CONCEPT")
    prev = sim_post(c, "/api/voorbeeld/export", {"docs": approved}).get_json()
    assert prev["count"] == 3 and len(prev["ubl"]) == 2                       # the sales invoice is no purchase UBL
    assert all(ch["ok"] for u in prev["ubl"] for ch in u["checks"]), prev["ubl"][0]["checks"]
    assert prev["skipped"][0]["id"] == "B-VERKOOP" and len(prev["register"]["rows"]) == 3
    for want, magic in (("ubl.zip", b"PK"), ("register.xlsx", b"PK"), ("mijnkantoor.zip", b"PK"), ("exact.xml", b"<?xml")):
        r = sim_post(c, "/api/voorbeeld/export", {"want": want, "docs": approved})
        assert r.status_code == 200 and r.data.startswith(magic), want
    # nothing approved, an unknown document, a document the visitor did not approve: no file
    assert sim_post(c, "/api/voorbeeld/export", {"want": "ubl.zip", "docs": [{"id": "B-MOLEN"}]}).status_code == 400
    assert sim_post(c, "/api/voorbeeld/export", {"want": "ubl", "id": "B-ENERGIE", "docs": approved}).status_code == 404
    assert sim_post(c, "/api/voorbeeld/export", {"want": "ubl", "id": "X", "docs": [{"id": "X", "status": "approved"}]}).status_code == 400
    assert len(appmod.state["docs"]) == docs and len(appmod.read_audit()) == audit_before


def test_sample_check_runs_the_real_checks_on_the_visitors_copy():
    c = guest()
    audit_before = len(appmod.read_audit())
    d = sim_post(c, "/api/voorbeeld/check", {"id": "M-BGR-IBAN"}).get_json()
    red = [f for f in d["flags"] if f["field"] == "iban"]
    assert d["light"] == "red" and red and red[0]["level"] == "error" and "terugbelcontrole" in red[0]["message"]
    # the visitor's note is attached to that flag, a note on an unknown flag is dropped
    d = sim_post(c, "/api/voorbeeld/check", {"id": "M-BGR-IBAN", "notes": {red[0]["id"]: "Teruggebeld", "zz": "x"}}).get_json()
    noted = [f for f in d["flags"] if f.get("note")]
    assert len(noted) == 1 and noted[0]["id"] == red[0]["id"] and noted[0]["note"]["note"] == "Teruggebeld"
    # a broken sum is found by the same rules as for a real document
    d = sim_post(c, "/api/voorbeeld/check", {"id": "B-MOLEN", "result": {"gross_total": 1.0}}).get_json()
    assert d["light"] == "red" and any(f["field"] in ("gross_total", "totals", "vat_lines") for f in d["flags"] if f["level"] == "error")
    # only known documents, only known clients and kinds, only pages of that document
    assert sim_post(c, "/api/voorbeeld/check", {"id": "../../etc"}).status_code == 404
    d = sim_post(c, "/api/voorbeeld/check", {"id": "B-MOLEN", "client_nr": "9999", "kind": "virus", "pages": [1, 2, 3, 40]}).get_json()
    assert d["client_nr"] == "1042" and d["kind"] == "inkoopfactuur" and d["pages"] == [2]
    assert c.post("/api/voorbeeld/check", data="nope", headers={"X-CSRF": CSRF, "Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/voorbeeld/check", json={"id": "B-MOLEN"}).status_code == 400            # no CSRF token
    big = {"id": "B-MOLEN", "pad": "x" * (appmod.SIM_MAX + 10)}
    assert sim_post(c, "/api/voorbeeld/check", big).status_code == 413
    assert len(appmod.read_audit()) == audit_before


def test_ubl_credit_note_and_reverse_charge():
    fields = {"doc_type": "credit_note", "supplier_name": "Adobe Systems Software Ireland Ltd",
              "supplier_vat_number": "IE6364992H", "invoice_number": "CN-1", "invoice_date": "2026-10-01",
              "net_total": 24.19, "vat_lines": [{"rate": 0, "base": 24.19, "amount": 0}], "gross_total": 24.19}
    xml = ubl.build(fields, {"name": "Studio Lindewerf B.V.", "vat": "NL865512093B01"}, b"%PDF-1.4 x", "cn.pdf")
    root = check_ubl(xml.encode())
    assert root.tag.endswith("CreditNote") and root.find("cbc:CreditNoteTypeCode", NS).text == "381"
    assert root.find(".//cac:TaxCategory/cbc:ID", NS).text == "AE"


# ---------------------------------------------------------------- checks
def test_iban_differs_from_earlier_documents_of_same_supplier():
    import checks
    f = {"doc_type": "invoice", "supplier_name": "Belnet Mobiel B.V.", "invoice_number": "X1", "invoice_date": "2026-10-01",
         "net_total": 40, "vat_lines": [{"rate": 21, "base": 40, "amount": 8.4}], "gross_total": 48.4,
         "iban": "NL91ABNA0417164300"}
    earlier = dict(f, invoice_number="X0", iban="NL02ABNA0123456789")
    flags = [x for x in checks.run_checks(f, others=[earlier]) if x["field"] == "iban"]
    # an error, not a warning: paying to a changed account number is the one mistake that cannot be undone
    assert len(flags) == 1 and flags[0]["level"] == "error" and "wijkt af" in flags[0]["message"]
    assert "terugbelcontrole" in flags[0]["message"] and checks.status_of(flags) == "red"
    same = dict(earlier, iban="NL91 ABNA 0417 1643 00")
    assert not [x for x in checks.run_checks(f, others=[same]) if x["field"] == "iban"]
    assert not [x for x in checks.run_checks(f, history=[dict(earlier, supplier_name="Andere B.V.")]) if x["field"] == "iban"]


def test_visitor_from_the_qr_code_sees_the_public_homepage(batches):
    c = client()
    r = c.get("/?k=kantoor-x")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Bekijk de rondleiding" in page and "Inloggen" in page
    assert "/rondleiding?k=kantoor-x" in page                      # the letter's code is passed on to the tour
    assert "niets ingediend" in page and "geen gegevens van klanten" in page
    assert "eigen-stapel.pdf" not in page and "admin@demo.local" not in page   # no stored data, only the sample
    assert "SCHEIDINGSBLAD" in page and "Bakkerij Van Dijk" in page             # the hero plays the fictional sample
    assert c.get("/scannen").status_code == 302                    # everything else still needs a login
    c = guest()
    assert "Rondleiding" in c.get("/").get_data(as_text=True)
    assert post(c, "/logout").headers["Location"].endswith("/")    # leaving the tour goes back to the homepage


def test_canonical_host_forwards_old_qr_links(monkeypatch):
    monkeypatch.setattr(appmod, "CANONICAL_HOST", "descanstraat.nl")
    c = appmod.app.test_client()
    r = c.get("/?k=kantoor-x", base_url="https://scanstraat-demo.onrender.com")
    assert r.status_code == 302 and r.headers["Location"] == "https://descanstraat.nl/?k=kantoor-x"
    assert c.get("/health", base_url="https://scanstraat-demo.onrender.com").status_code == 200
    assert c.get("/?k=kantoor-x", base_url="https://descanstraat.nl").status_code == 200


# ---------------------------------------------------------------- sign-in page, status, error pages
def test_status_badge_reports_only_what_the_app_can_check():
    r = client().get("/status.json")
    s = r.get_json()
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert s["ok"] and s["text"] == "Alle systemen operationeel" and len(s["checks"]) == 4
    assert s["eu"] is False and s["location"] == ""       # no HOSTING_LABEL here: never a guessed location
    assert "admin" not in r.get_data(as_text=True) and "key" not in json.dumps(s).lower().replace('"key": ', "")
    page = client().get("/login").get_data(as_text=True)
    assert 'data-src="/status.json"' in page and "Alle EU-verwerkingsnodes operationeel" in page   # only shown when s.ok && s.eu
    assert "Start direct de interactieve" in page and 'href="/rondleiding"' in page and "SSO" not in page


def test_error_pages_for_visitor_guest_and_staff():
    page = client().get("/bestaat-niet").get_data(as_text=True)
    assert "Fout 404" in page and "Naar de website" in page and "Bekijk de rondleiding" in page
    r = guest().get("/auditlog")
    page = r.get_data(as_text=True)
    assert r.status_code == 403 and "Niet beschikbaar in de rondleiding" in page and "Terug naar de rondleiding" in page
    page = admin().get("/bestaat-niet").get_data(as_text=True)
    assert "Niet gevonden" in page and "Naar het overzicht" in page


# ---------------------------------------------------------------- the client's upload link
def test_client_upload_link_is_signed_and_stores_nothing_on_a_demo():
    batches = len(appmod.state["batches"])
    with appmod.app.test_request_context():
        url = appmod.upload_url("1042")
    c = client()
    r = c.get(url)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Bakkerij Van Dijk B.V." in page and "Maaskade Administraties" in page
    assert "Demonstratie met een fictieve klant" in page
    assert c.get(url[:-1] + ("0" if url[-1] != "0" else "1")).status_code == 404      # a changed signature
    assert c.get("/aanleveren/1042-" + "0" * 20).status_code == 404
    assert c.get("/aanleveren/9999-" + url.rsplit("-", 1)[1]).status_code == 404         # another client's number
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    r = c.post(url, data={"csrf": CSRF, "files": (io.BytesIO(b"%PDF-1.4 x"), "bon.pdf")}, content_type="multipart/form-data")
    assert r.status_code == 403 and len(appmod.state["batches"]) == batches


# ---------------------------------------------------------------- dossiers page, firm name, bank import
CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
<Ntry><Amt Ccy="EUR">12.50</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-11-02</Dt></BookgDt>
 <NtryDtls><TxDtls><RltdPties><Cdtr><Nm>Bakkerij Testmans</Nm></Cdtr></RltdPties><RmtInf><Ustrd>Betaalautomaat 09:12 pasnr. 001</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
<Ntry><Amt Ccy="EUR">250.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-11-03</Dt></BookgDt><AddtlNtryInf>Factuur 2026-17</AddtlNtryInf></Ntry>
</Stmt></BkToCstmrStmt></Document>"""


def test_dossiers_page_shows_sheet_upload_link_and_imports_camt():
    c = admin()
    page = c.get("/klanten").get_data(as_text=True)
    assert "Dossiers en scheidingsbladen" in page and "/klanten/scheidingsblad.jpg?nr=1042" in page
    with appmod.app.test_request_context():
        assert appmod.upload_url("1042") in page
    assert 'action="/klanten/bank"' in page and 'name="camt"' in page
    r = c.post("/klanten/bank", data={"csrf": CSRF, "nr": "1042", "camt": (io.BytesIO(CAMT), "nov.xml")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    lines = appmod.BANK.load()["1042"]
    assert [(l["amount"], l["type"], l["date"]) for l in lines] == [(-12.5, "pin", "2026-11-02"), (250.0, "bij", "2026-11-03")]
    assert any(e["event"] == "bank_imported" for e in appmod.read_audit())
    r = c.post("/klanten/bank", data={"csrf": CSRF, "nr": "1042", "camt": (io.BytesIO(b"<x/>"), "leeg.xml")},
               content_type="multipart/form-data")
    assert r.status_code == 302 and len(appmod.BANK.load()["1042"]) == 2               # a file without entries changes nothing
    assert guest().post("/klanten/bank", data={"csrf": CSRF, "nr": "1042"}).status_code == 403


def test_firm_name_is_set_once_and_kept_by_other_forms():
    c = admin()
    assert post(c, "/koppelingen", section="general", firm_name="  Kantoor   De Test ", manual_minutes="3").status_code == 302
    assert appmod.firm_name() == "Kantoor De Test"
    assert post(c, "/koppelingen", section="general", manual_minutes="4").status_code == 302      # no name field
    assert appmod.firm_name() == "Kantoor De Test" and appmod.settings()["manual_minutes"] == 4
    assert "Kantoor De Test" in c.get("/klanten").get_data(as_text=True)
    sheet = c.get("/klanten/scheidingsblad.jpg?nr=1042")
    assert sheet.status_code == 200 and sheet.data[:2] == b"\xff\xd8"
    post(c, "/koppelingen", section="general", firm_name="", manual_minutes="")
    assert appmod.firm_name() == appmod.DEMO_FIRM


def test_guest_cover_sheet_with_own_firm_name_only_for_sample_dossiers():
    c = guest()
    r = c.get("/klanten/scheidingsblad.jpg?nr=1042&kantoor=Mijn <b>Kantoor</b>")
    assert r.status_code == 200 and r.data[:2] == b"\xff\xd8"
    assert c.get("/klanten/scheidingsblad.jpg?nr=1002").status_code == 404       # a stored dossier outside the sample
    pdf = c.get("/klanten/scheidingsbladen.pdf?kantoor=Mijn Kantoor")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")
