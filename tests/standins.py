"""Stand-in servers for testing without a GPU or an API key (never for real use):
  Ollama on :11555 and an Anthropic-compatible /v1/messages on :11556 that records every request it gets.

    python tests/standins.py &
    OLLAMA_URL=http://127.0.0.1:11555 ANTHROPIC_BASE_URL=http://127.0.0.1:11556 ANTHROPIC_API_KEY=x SCAN_AI=hybrid python app.py

Requests that reach the fake Anthropic server are appended to STANDIN_LOG (default /tmp/standin-anthropic.jsonl),
so you can check that no image and no unmasked value ever arrived there.
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

LOG = os.environ.get("STANDIN_LOG", "/tmp/standin-anthropic.jsonl")
N = {"t": 0}


def page_text(i):
    if i % 7 == 3:
        return "Huisartsenpraktijk De Linde\nFactuur consult 12-09-2026\nTotaal 45,00"
    return ("Fotografie Jan de Vries\nKerkstraat 12, 2611 AB Delft\nIBAN NL91ABNA0417164300\nE-mail jan@devries-foto.nl\n"
            f"Factuur 2026-{i:03d}  Datum 2026-09-{(i % 27) + 1:02d}\nAan: Studio Lindewerf B.V., Hoogstraat 118 Rotterdam\n"
            "Fotoreportage nieuwe website 100,00\nBtw 21% 21,00\nTotaal 121,00")


class Base(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")


class Ollama(Base):
    def do_GET(self):
        self.send({"models": [{"name": "qwen2.5vl:7b"}, {"name": "qwen2.5:7b"}]})

    def do_POST(self):
        b = self.body()
        if self.path == "/api/show":
            return self.send({"license": "Apache License\nVersion 2.0, January 2004"})
        fmt = b.get("format")
        prompt = b["messages"][-1]["content"]
        if b["model"] == "qwen2.5:7b":
            out = {"personen": ["Jan de Vries"] if "Jan de Vries" in prompt else [],
                   "adressen": ["Kerkstraat 12, 2611 AB Delft"] if "Kerkstraat 12" in prompt else []}
        elif fmt is None:
            N["t"] += 1
            return self.send({"message": {"content": page_text(N["t"])}})
        elif "continues_previous" in fmt.get("required", []):
            out = {"kind": "inkoopfactuur", "client_hint": None, "continues_previous": False, "reason": "Nieuwe afzender (lokaal model)"}
        else:
            out = {k: None for k in fmt["required"]} | {
                "kind": "inkoopfactuur", "counterparty": "Fysiotherapie De Brug", "reference": "FB-2026-0412",
                "doc_date": "2026-09-24", "gross_total": 62.0, "net_total": 62.0, "currency": "EUR",
                "vat_lines": [{"rate": 0, "base": 62.0, "amount": 0.0}], "lines": [],
                "booking_reason": "Zorgkosten op naam van een persoon: waarschijnlijk privé.", "uncertain_fields": [],
                "issues": ["Zorgfactuur op naam van een persoon: waarschijnlijk privé, niet zakelijk boeken. Vraag het de klant."]}
        self.send({"message": {"content": json.dumps(out)}, "prompt_eval_count": 900, "eval_count": 120})


KEY = json.load(open(os.path.join(os.path.dirname(__file__), "..", "samples", "scan-stack.json")))["documents"]


def _norm(t):
    return re.sub(r"[\s.-]", "", t or "").lower()


def answer_for(text):
    """The hand-made answer whose reference number or counterparty appears in this (OCR) text, if any."""
    n = _norm(text)
    for d in KEY:
        ref = d["answer"].get("reference")
        if ref and _norm(ref) in n:
            return d["answer"]
    for d in KEY:
        cp = d["answer"].get("counterparty")
        if cp and _norm(cp.split()[0]) in n:
            return d["answer"]
    return None


def mask_like(ans, text):
    """Put back placeholders where the value was masked before sending (the real Claude would copy them)."""
    ans = json.loads(json.dumps(ans))
    tokens = re.findall(r"\[(?:IBAN|EMAIL|TELEFOON|BSN|PERSOON|ADRES)_\d+\]", text)
    if ans.get("iban") and ans["iban"].replace(" ", "") not in text.replace(" ", ""):
        ans["iban"] = next((t for t in tokens if t.startswith("[IBAN")), None)
    return ans


class Anthropic(Base):
    def do_POST(self):
        b = self.body()
        with open(LOG, "a") as fh:
            fh.write(json.dumps(b) + "\n")
        schema = b["output_config"]["format"]["schema"]
        text = "\n".join(c.get("text", "") for m in b["messages"] for c in m["content"])
        if "documents" in schema["properties"]:
            pages = re.split(r"--- Page (\d+) ---", text)[1:]
            docs, last = [], None
            for i in range(0, len(pages), 2):
                n, t = int(pages[i]), pages[i + 1]
                ans = answer_for(t)
                if docs and (ans is None or ans is last):
                    docs[-1]["pages"].append(n)
                else:
                    docs.append({"pages": [n], "kind": (ans or {}).get("kind", "overig"),
                                 "client_hint": (ans or {}).get("client_hint"), "reason": "Nieuwe afzender of nieuw kenmerk"})
                last = ans or last
            out = {"documents": docs}
        else:
            ans = answer_for(text) or {}
            out = {k: None for k in schema["required"]} | {"vat_lines": [], "lines": [], "uncertain_fields": [], "issues": [],
                                                          "booking_reason": "", "kind": "overig"}
            out.update({k: v for k, v in mask_like(ans, text).items() if k in schema["properties"]})
        self.send({"id": "msg_standin", "type": "message", "role": "assistant", "model": b["model"],
                   "content": [{"type": "text", "text": json.dumps(out)}], "stop_reason": "end_turn",
                   "stop_sequence": None, "usage": {"input_tokens": 1500, "output_tokens": 300}})


if __name__ == "__main__":
    a = ThreadingHTTPServer(("127.0.0.1", 11555), Ollama)
    b = ThreadingHTTPServer(("127.0.0.1", 11556), Anthropic)
    threading.Thread(target=a.serve_forever, daemon=True).start()
    print("Stand-ins: Ollama :11555, Anthropic :11556")
    b.serve_forever()
