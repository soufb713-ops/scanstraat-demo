"""The AI steps: split a scanned pile into documents, then read each document.

Three modes (SCAN_AI in .env):
  local   (default) Everything on this machine with Ollama and Apache 2.0 models (models.py). No page,
          text or value leaves the machine.
  hybrid  Page images stay local. Text is read locally (Tesseract, or the local vision model), screened for
          special category data, pseudonymised locally (privacy.py) and only then sent, as text, to Claude
          through egress.py. Claude's answer gets the real values back locally. A document with special
          category data, or one the egress gate blocks, is read by the local model instead.
  replay  No AI at all: replays the hand-made answers for the sample scan (labelled "Replay-modus").
The former "claude" mode, which sent page images to the US, no longer exists: setting it stops the app.
"""
import base64
import json
import os
import random
import time

import egress
import models
import privacy

MODE = os.environ.get("SCAN_AI", "local").strip().lower()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
if MODE not in ("local", "hybrid", "replay"):
    raise SystemExit(f"SCAN_AI={MODE} is niet toegestaan. Kies local, hybrid of replay. "
                     "Paginabeelden gaan nooit naar een Amerikaanse AI-dienst.")

KINDS = {
    "inkoopfactuur": "Inkoopfactuur",
    "kassabon": "Kassabon",
    "creditnota": "Creditnota (inkoop)",
    "verkoopfactuur": "Verkoopfactuur",
    "bankafschrift": "Bankafschrift",
    "belastingdienst": "Brief Belastingdienst",
    "loonstrook": "Loonstrook / salaris",
    "contract": "Contract / overeenkomst",
    "overig": "Overige correspondentie",
}
BOOKABLE = {"inkoopfactuur", "kassabon", "creditnota", "verkoopfactuur"}
VAT_CODES = {
    "V21": "Voorbelasting 21%", "V9": "Voorbelasting 9%", "V0": "Geen btw op factuur",
    "EU_DIENST_VERL": "Dienst uit EU, btw verlegd", "EU_GOED_VERL": "Goederen uit EU (ICV)",
    "NL_VERL": "Binnenlands verlegd", "GEEN_AFTREK": "Btw niet aftrekbaar", "PRIVE": "Privé, buiten btw",
    "GEEN": "Geen btw (bank, verzekering)", "O21": "Omzet 21%", "O9": "Omzet 9%", "O0": "Omzet 0% / vrijgesteld",
}
FIELDS = ["counterparty", "reference", "doc_date", "due_date", "supplier_vat_number", "supplier_kvk",
          "iban", "net_total", "vat_lines", "gross_total", "currency", "description"]


class AIError(Exception):
    pass


def local():
    return MODE == "local"


def hybrid():
    return MODE == "hybrid"


def live():
    return MODE in ("local", "hybrid")


def label():
    if local():
        return f"Lokale AI ({models.VISION_MODEL}), niets naar buiten"
    if hybrid():
        return "Hybride: lokaal anonimiseren, alleen tekst naar Claude"
    return "Replay-modus (geen AI)"


# ---------------------------------------------------------------- Ollama (local)
def _ollama(model, system, text, images=(), schema="json", timeout=600):
    import urllib.error
    import urllib.request
    model = models.require(model)  # licence gate
    msgs = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": text, **({"images": list(images)} if images else {})}]
    body = {"model": model, "stream": False, "options": {"temperature": 0}, "messages": msgs}
    if schema:
        body["format"] = schema
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        raise AIError(f"Lokale AI niet bereikbaar op {OLLAMA_URL} (draait Ollama?)") from e
    content = data.get("message", {}).get("content", "")
    usage = {"model": model, "input_tokens": data.get("prompt_eval_count", 0), "output_tokens": data.get("eval_count", 0)}
    if not schema:
        return content, usage
    try:
        return json.loads(content), usage
    except ValueError as e:
        raise AIError("Lokale AI gaf geen geldige JSON") from e


def _call_local(system, content, schema):
    texts = [b["text"] for b in content if b["type"] == "text"]
    images = [b["source"]["data"] for b in content if b["type"] == "image"]
    try:
        return _ollama(models.VISION_MODEL, system, "\n".join(texts) + "\nAnswer with JSON only.", images, schema)
    except models.LicenceError as e:
        raise AIError(str(e)) from e


def anon_call(model, prompt):
    """Used by privacy.py for name/address detection with the local text model."""
    out, _ = _ollama(model, None, prompt, schema="json", timeout=float(os.environ.get("OLLAMA_TIMEOUT", "180")))
    return out


TRANSCRIBE = ("Transcribe all text on this scanned page exactly as printed, line by line, in reading order. "
              "Output only the text.")


def page_text(jpeg):
    """Text of one page, read on this machine: Tesseract if installed, else the local vision model."""
    text = privacy.ocr_image(jpeg)
    if text is not None:
        return text, "Tesseract (lokaal)"
    try:
        out, _ = _ollama(models.VISION_MODEL, None, TRANSCRIBE, [base64.b64encode(jpeg).decode()], schema=None)
    except models.LicenceError as e:
        raise AIError(str(e)) from e
    return out.strip(), f"{models.VISION_MODEL} (lokaal)"


PAGE_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["kind", "client_hint", "continues_previous", "reason"],
               "properties": {"kind": {"type": "string", "enum": list(KINDS)}, "client_hint": {"type": ["string", "null"]},
                              "continues_previous": {"type": "boolean"}, "reason": {"type": "string"}}}
PAGE_SYSTEM = """You sort scanned paper for a Dutch bookkeeping office. You see one scanned page and, when given,
a small image of the page before it. continues_previous: true only if this page is clearly the next page of the
same document as the previous page (same sender, 'pagina 2', appendix to the same invoice); false for a new document.
kind: document type. client_hint: the office's client this paper belongs to (addressee of a purchase invoice or
letter, issuer of a sales invoice), or null. reason: one short Dutch sentence."""


def _split_local(page_jpegs, client_names):
    docs, usage = [], {"input_tokens": 0, "output_tokens": 0}
    for i, jpg in enumerate(page_jpegs, 1):
        content = [{"type": "text", "text": "Clients of this office: " + "; ".join(client_names)}]
        if i > 1:
            content += [{"type": "text", "text": "Previous page:"}, _img_block(page_jpegs[i - 2])]
        content += [{"type": "text", "text": "This page:"}, _img_block(jpg)]
        page, u = _call_local(PAGE_SYSTEM, content, PAGE_SCHEMA)
        usage["input_tokens"] += u["input_tokens"]
        usage["output_tokens"] += u["output_tokens"]
        if docs and page.get("continues_previous"):
            docs[-1]["pages"].append(i)
        else:
            docs.append({"pages": [i], "kind": page.get("kind", "overig"), "client_hint": page.get("client_hint"),
                         "reason": page.get("reason", "")})
    return docs, usage


_s = {"type": ["string", "null"]}
_n = {"type": ["number", "null"]}


def _split_schema():
    return {"type": "object", "additionalProperties": False, "required": ["documents"],
            "properties": {"documents": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["pages", "kind", "client_hint", "reason"],
                "properties": {
                    "pages": {"type": "array", "items": {"type": "integer"}},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "client_hint": _s,
                    "reason": {"type": "string"}}}}}}


def _read_schema(accounts):
    line = {"type": "object", "additionalProperties": False,
            "required": ["account", "vat_code", "amount_excl", "vat_amount", "description"],
            "properties": {"account": {"type": "string", "enum": accounts},
                           "vat_code": {"type": "string", "enum": list(VAT_CODES)},
                           "amount_excl": {"type": "number"}, "vat_amount": {"type": "number"},
                           "description": {"type": "string"}}}
    props = {k: _s for k in FIELDS}
    props.update({
        "kind": {"type": "string", "enum": list(KINDS)},
        "client_hint": _s,
        "net_total": _n, "gross_total": _n,
        "vat_lines": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                 "required": ["rate", "base", "amount"],
                                                 "properties": {"rate": {"type": "number"}, "base": _n, "amount": _n}}},
        "lines": {"type": "array", "items": line},
        "booking_reason": {"type": "string"},
        "uncertain_fields": {"type": "array", "items": {"type": "string", "enum": FIELDS}},
        "issues": {"type": "array", "items": {"type": "string"}},
    })
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


SPLIT_SYSTEM = """You sort scanned paper for a Dutch bookkeeping office (administratiekantoor).
You get the pages of one scanned pile in order, numbered from 1. Group consecutive pages into documents:
a multi-page invoice, a letter with its appendix or a contract stays together; a new letterhead, a new
invoice number or a different sender starts a new document. Never reorder pages; every page belongs to
exactly one document. Two identical copies of the same page are two documents (the office decides later).
kind: the type of document. client_hint: the name of the office's client this paper belongs to (usually
the addressee of a purchase invoice or letter, or the issuer of a sales invoice), or null if not visible.
reason: one short Dutch sentence."""

READ_SYSTEM = """You read one scanned document for a Dutch bookkeeping office and prepare it for a person to check.
Copy values exactly as printed. Never compute or repair a value that is not on the document: if a total, VAT
amount or number is missing or unreadable, return null and list the field in uncertain_fields.
Amounts are plain numbers (1234.56). Dates as YYYY-MM-DD.
counterparty: the other party (supplier on a purchase invoice, customer on a sales invoice, sender of a letter).
reference: invoice number, receipt number or the letter's reference number.
vat_lines: one entry per VAT rate printed on the document.
lines: the booking proposal, only for invoices, receipts and credit notes (empty otherwise). Use only accounts
from the client's chart of accounts. One line per VAT rate. If the VAT printed on the document is wrong,
book the correct amount and say so in issues. VAT a Dutch company cannot deduct (foreign VAT, restaurant) is
booked gross with GEEN_AFTREK. Credit notes have negative amounts.
booking_reason: one short Dutch sentence why this account and VAT code.
issues: short Dutch sentences a bookkeeper must know: deadlines in letters, a likely private expense, an asset
to capitalise, a payment reminder instead of an invoice, pages that seem to be missing. Empty if nothing."""


def _img_block(jpeg):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(jpeg).decode()}}


SPLIT_TEXT_NOTE = """The pages are given as text read from the scan, not as images. Personal data has been replaced by
placeholders such as [PERSOON_1] or [IBAN_2]; keep placeholders exactly as written if you repeat them."""


def split(page_jpegs, page_texts, client_names, pseudo, ref_id=None):
    """Returns (documents, usage, trail). page_texts: local text per page (hybrid only, else None)."""
    if local():
        docs, usage = _split_local(page_jpegs, client_names)
        return docs, usage, {"route": "lokaal", "model": models.VISION_MODEL}
    special = {i + 1: privacy.special_categories(t) for i, t in enumerate(page_texts)}
    special = {k: v for k, v in special.items() if v}
    if not special:
        try:
            parts = ["Clients of this office: " + "; ".join(client_names)]
            parts += [f"--- Page {i} ---\n" + pseudo.anonymise(t, anon_call) for i, t in enumerate(page_texts, 1)]
            data, usage, rec = egress.send("split", SPLIT_SYSTEM + "\n" + SPLIT_TEXT_NOTE, parts, _split_schema(), doc=ref_id)
            docs = pseudo.restore(_repair_split(data["documents"], len(page_texts)))
            return docs, usage, {"route": "Claude (tekst, geanonimiseerd)", "sha256": rec["sha256"]}
        except (privacy.PrivacyError, egress.EgressBlocked, egress.EgressError) as e:
            reason = str(e)
    else:
        reason = "bijzondere persoonsgegevens op pagina " + ", ".join(map(str, special))
    try:
        docs, usage = _split_local(page_jpegs, client_names)
        return docs, usage, {"route": "lokaal", "model": models.VISION_MODEL, "why_local": reason}
    except AIError:
        docs = [{"pages": [i], "kind": "overig", "client_hint": None, "reason": "Niet gesplitst: " + reason}
                for i in range(1, len(page_jpegs) + 1)]
        return docs, {"input_tokens": 0, "output_tokens": 0}, {"route": "geen AI", "why_local": reason}


def _repair_split(docs, n):
    """Make sure every page is used exactly once and in order, whatever the model returned."""
    owner = {}
    for d_i, d in enumerate(docs):
        for p in d.get("pages") or []:
            if 1 <= p <= n and p not in owner:
                owner[p] = d_i
    out, cur = [], None
    for p in range(1, n + 1):
        d_i = owner.get(p, cur if cur is not None else 0)
        if not out or d_i != cur:
            src = docs[d_i] if docs else {"kind": "overig", "client_hint": None, "reason": ""}
            out.append({"pages": [], "kind": src.get("kind", "overig"), "client_hint": src.get("client_hint"),
                        "reason": src.get("reason", "")})
            cur = d_i
        out[-1]["pages"].append(p)
    return out


def _reference(client, chart, reference_text):
    ref = f"Client: {client['name']} (KvK {client.get('kvk')}, btw {client.get('vat')})\n\nChart of accounts:\n" + \
        "\n".join(f"{a} {n}" for a, n in chart) + "\n\nVAT codes:\n" + \
        "\n".join(f"{k} {v}" for k, v in VAT_CODES.items())
    if reference_text:
        ref += "\n\n" + reference_text
    return ref


READ_TEXT_NOTE = """You get the document as text read from the scan (not the image). Personal data has been replaced
by placeholders such as [PERSOON_1], [IBAN_1] or [ADRES_1]: copy a placeholder exactly into the field where the
real value belongs (for example iban: "[IBAN_1]"). Never guess what is behind a placeholder."""


def read(page_jpegs, page_texts, client, chart, reference_text, pseudo, doc_id=None):
    """Returns (result, usage, trail). trail says where the document was read and, for Claude, the exact text sent."""
    accounts = [a for a, _ in chart] or ["4900"]
    ref = _reference(client, chart, reference_text)
    if hybrid():
        text = "\n\n".join(f"--- Pagina {i} ---\n{t}" for i, t in enumerate(page_texts, 1))
        special = privacy.special_categories(text)
        if special:
            reason = "bijzondere persoonsgegevens: " + ", ".join(special)
        else:
            try:
                sent = pseudo.anonymise(text, anon_call)
                sent_ref = pseudo.anonymise(ref, anon_call)
                data, usage, rec = egress.send("read", READ_SYSTEM + "\n" + READ_TEXT_NOTE, [sent], _read_schema(accounts),
                                               ref=sent_ref, doc=doc_id)
                return pseudo.restore(data), usage, {"route": "Claude (tekst, geanonimiseerd)", "sent_text": sent,
                                                     "replaced": privacy.summary(pseudo.mapping), "sha256": rec["sha256"]}
            except (privacy.PrivacyError, egress.EgressBlocked, egress.EgressError) as e:
                reason = str(e)
        data, usage = _read_local(page_jpegs, ref, accounts)
        return data, usage, {"route": "lokaal", "model": models.VISION_MODEL, "why_local": reason}
    data, usage = _read_local(page_jpegs, ref, accounts)
    return data, usage, {"route": "lokaal", "model": models.VISION_MODEL}


def _read_local(page_jpegs, ref, accounts):
    content = [{"type": "text", "text": ref}]
    for i, jpg in enumerate(page_jpegs, 1):
        content += [{"type": "text", "text": f"Page {i}:"}, _img_block(jpg)]
    return _call_local(READ_SYSTEM, content, _read_schema(accounts))


# ---------------------------------------------------------------- replay
def replay_delay():
    time.sleep(random.uniform(0.6, 1.4))


def empty_result(kind="overig", note="Replay-modus leest alleen de voorbeeldscan. Zet SCAN_AI=local of hybrid voor echte documenten."):
    return {"kind": kind, "client_hint": None, "counterparty": None, "reference": None, "doc_date": None,
            "due_date": None, "supplier_vat_number": None, "supplier_kvk": None, "iban": None, "net_total": None,
            "vat_lines": [], "gross_total": None, "currency": "EUR", "description": None, "lines": [],
            "booking_reason": "", "uncertain_fields": [], "issues": [note]}
