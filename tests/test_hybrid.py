"""Hybrid mode end to end with a stand-in Ollama server and a captured (not real) Claude call.
Checks that only pseudonymised text leaves, never an image, that special category documents stay local,
that values are restored locally, and the licence gate. Run with: bash tests/run.sh"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
IBAN, BSN, NAME, MAIL = "NL91ABNA0417164300", "111222333", "Jan de Vries", "jan@devries-foto.nl"
LICENCE = {"text": "Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/"}
CALLS = {"transcribe": 0, "anon": 0, "local_read": 0, "local_split": 0}


def page_text(i):
    if i == 1:  # first page read: looks like a GP invoice -> special category
        return "Huisartsenpraktijk De Linde\nFactuur consult\nTotaal 45,00"
    return (f"Fotografie {NAME}\nKerkstraat 12, Delft\nIBAN {IBAN}\nE-mail {MAIL}\nBSN {BSN}\n"
            f"Factuur 2026-{i:03d} aan Studio Lindewerf B.V.\nTotaal 121,00 waarvan btw 21,00")


class Ollama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._send({"models": [{"name": "qwen2.5vl:7b"}, {"name": "qwen2.5:7b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            return self._send({"license": LICENCE["text"]})
        model, fmt = body["model"], body.get("format")
        if model == "qwen2.5:7b":
            CALLS["anon"] += 1
            prompt = body["messages"][-1]["content"]
            out = {"personen": [NAME] if NAME in prompt else [], "adressen": ["Kerkstraat 12, Delft"] if "Kerkstraat 12" in prompt else []}
        elif fmt is None:
            CALLS["transcribe"] += 1
            return self._send({"message": {"content": page_text(CALLS["transcribe"])}})
        elif "continues_previous" in fmt.get("required", []):
            CALLS["local_split"] += 1
            out = {"kind": "inkoopfactuur", "client_hint": None, "continues_previous": False, "reason": "lokaal"}
        else:
            CALLS["local_read"] += 1
            out = {k: None for k in fmt["required"]} | {"kind": "inkoopfactuur", "vat_lines": [], "lines": [],
                                                          "booking_reason": "lokaal", "uncertain_fields": [], "issues": [],
                                                          "counterparty": "Huisartsenpraktijk De Linde"}
        self._send({"message": {"content": json.dumps(out)}, "prompt_eval_count": 10, "eval_count": 5})


srv = HTTPServer(("127.0.0.1", 0), Ollama)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(SCAN_DATA=str(TMP / "data"), VAULT_KEY=base64.b64encode(os.urandom(32)).decode(), SCAN_AI="hybrid",
                  OLLAMA_URL=f"http://127.0.0.1:{srv.server_port}", ANTHROPIC_API_KEY="test-not-used", SCAN_SEED="0",
                  ADMIN_PASSWORD="EenLangWachtwoord1", SCAN_ENV_FILE=str(TMP / "none.env"))
sys.path.insert(0, str(HERE))

import ai  # noqa: E402
import egress  # noqa: E402
import models  # noqa: E402

SENT = []


def fake_call(system, ref, parts, schema):
    SENT.append({"system": system, "ref": ref, "parts": parts})
    text = "\n".join(parts)
    if "documents" in schema["properties"]:
        n = text.count("--- Page ")
        return {"documents": [{"pages": [i], "kind": "inkoopfactuur", "client_hint": "Studio Lindewerf B.V.",
                               "reason": "test"} for i in range(1, n + 1)]}, {"model": "fake", "input_tokens": 1, "output_tokens": 1}
    out = {k: None for k in schema["required"]} | {
        "kind": "inkoopfactuur", "vat_lines": [], "lines": [], "booking_reason": "test", "uncertain_fields": [], "issues": [],
        "counterparty": "Fotografie [PERSOON_1]" if "[PERSOON_1]" in text else "?", "iban": "[IBAN_1]" if "[IBAN_1]" in text else None,
        "gross_total": 121.0}
    return out, {"model": "fake", "input_tokens": 1, "output_tokens": 1}


egress._call = fake_call
ai.privacy.ocr_image = lambda jpeg: None  # force the local vision model path, so the stand-in texts are used
import app as appmod  # noqa: E402


@pytest.fixture(scope="module")
def batch():
    stack = HERE / "samples" / "scan_2026-09-30_0915.pdf"
    bid = appmod.create_batch([(stack.name, stack.read_bytes())], "upload", "Test")
    for _ in range(240):
        if appmod.state["batches"][bid]["status"] != "processing":
            break
        time.sleep(0.5)
    b = appmod.state["batches"][bid]
    assert b["status"] == "done", b.get("error")
    return b


def test_only_pseudonymised_text_leaves(batch):
    assert SENT, "nothing was sent to the (fake) Claude"
    for s in SENT:
        for p in [s["system"], s["ref"]] + s["parts"]:
            assert isinstance(p, str)
            for secret in (IBAN, BSN, NAME, MAIL, "Kerkstraat 12", "Huisarts", "NL93DEMO0123456789"):
                assert secret not in p, f"{secret} leaked"
    assert any("[IBAN_1]" in "".join(s["parts"]) for s in SENT)


def test_special_category_stays_local(batch):
    docs = [appmod.state["docs"][d] for d in batch["docs"]]
    local = [d for d in docs if d["trail"]["route"] == "lokaal"]
    assert local and all("bijzondere" in d["trail"]["why_local"] for d in local)
    assert CALLS["local_read"] >= 1 and CALLS["local_split"] >= 1   # the group with the GP page was split locally
    assert any("bijzondere" in (t.get("why_local") or "") for t in batch["split_trail"])


def test_values_restored_locally_and_mapping_sealed(batch):
    docs = [appmod.state["docs"][d] for d in batch["docs"]]
    claude = [d for d in docs if d["trail"]["route"].startswith("Claude")]
    assert claude
    d = claude[0]
    assert d["result"]["iban"] == IBAN and d["result"]["counterparty"] == f"Fotografie {NAME}"
    f = appmod.MAPS / f"{d['id']}.json"
    assert f.read_bytes()[:4] == b"VLT1" and IBAN.encode() not in f.read_bytes()
    assert IBAN not in d["trail"]["sent_text"] and "[IBAN_1]" in d["trail"]["sent_text"]


def test_egress_log_and_gate(batch):
    log = appmod.vault.read_lines(appmod.EGRESS_LOG)
    assert any(e["result"] == "sent" for e in log)
    with pytest.raises(egress.EgressBlocked):
        egress.send("read", "sys", [f"IBAN {IBAN}"], {})
    with pytest.raises(egress.EgressBlocked):
        egress.send("read", "sys", [b"\xff\xd8\xff jpeg"], {})
    with pytest.raises(egress.EgressBlocked):
        egress.send("read", "sys", ["Contributie FNV september"], {})
    assert appmod.vault.read_lines(appmod.EGRESS_LOG)[-1]["result"] == "blocked"


def test_local_mode_never_calls_claude(batch):
    before = len(SENT)
    ai.MODE = "local"
    try:
        jpg = appmod.scan.jpeg_bytes(appmod.scan.page_image(appmod.batch_dir(batch["id"]) / "pages", 2), 800)
        res, _, trail = ai.read([jpg], None, {"name": "x"}, [("4000", "Kosten")], "", appmod.privacy.Pseudonymiser())
        assert trail["route"] == "lokaal"
    finally:
        ai.MODE = "hybrid"
    assert len(SENT) == before


def test_licence_gate():
    assert not models.check("qwen2.5:3b")["ok"]
    with pytest.raises(models.LicenceError):
        models.require("qwen2.5:3b")
    assert not models.check("llama3:8b")["ok"]
    assert models.check("qwen2.5:7b")["ok"]
    LICENCE["text"] = "Qwen RESEARCH LICENSE AGREEMENT"
    try:
        assert not models.check("qwen2.5vl:7b")["ok"]
    finally:
        LICENCE["text"] = "Apache License\nVersion 2.0"
