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


def admin():
    c = client()
    r = post(c, "/login", email="admin@demo.local", password="EenLangWachtwoord1")
    assert r.headers["Location"].endswith("/login/code")      # pre-enrolled: no QR set-up step
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    r = post(c, "/login/code", code=auth._hotp(SECRET, int(time.time() // 30)))
    assert r.status_code == 302 and c.get("/").status_code == 200
    with c.session_transaction() as s:   # a login starts a fresh session
        s["csrf"] = CSRF
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


# ---------------------------------------------------------------- guest tour
def test_guest_sees_only_the_fictional_sample(batches):
    sample, own = batches
    c = guest()
    page = c.get("/").get_data(as_text=True)
    assert "Rondleiding" in page and "eigen-stapel.pdf" not in page
    assert c.get(f"/scan/{sample['id']}").status_code == 200
    assert c.get(f"/scan/{own['id']}").status_code == 404
    assert c.get(f"/document/{own['docs'][0]}").status_code == 404
    assert c.get(f"/document/{own['docs'][0]}.pdf").status_code == 404
    assert c.get(f"/pagina/{own['id']}/1.jpg").status_code == 404
    assert c.get(f"/document/{sample['docs'][0]}").status_code == 200
    for url in ("/afhandelen", "/berichten", "/beveiliging", "/klanten", "/scannen"):
        assert c.get(url).status_code == 200, url


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
    assert appmod.state["docs"][did]["exports"]["ubl"]["route"] == "zip"


def test_guest_may_preview_a_concept_ubl_of_the_sample(batches):
    sample, _ = batches
    c = guest()
    did = next(d for d in sample["docs"] if appmod.state["docs"][d]["kind"] in appmod.UBL_KINDS)
    root = check_ubl(c.get(f"/document/{did}/ubl.xml").data)
    assert root.find("cbc:Note", NS).text.startswith("CONCEPT")


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
    assert len(flags) == 1 and flags[0]["level"] == "warning" and "wijkt af" in flags[0]["message"]
    same = dict(earlier, iban="NL91 ABNA 0417 1643 00")
    assert not [x for x in checks.run_checks(f, others=[same]) if x["field"] == "iban"]
    assert not [x for x in checks.run_checks(f, history=[dict(earlier, supplier_name="Andere B.V.")]) if x["field"] == "iban"]
