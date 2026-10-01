"""Scanstraat (privacy version): from a scanned pile of paper to a checked MijnKantoor dossier, Exact booking
and Excel register, built privacy by design (AVG) and with a human gate on every outgoing message (Wwft).

Flow per scan:
  1. pages       the scanner's PDF (or photos) becomes page images          scan.py, no AI
  2. sort        blank back sides removed, cover sheets split per client     scan.py, no AI (QR code)
  3. split       which pages form one document                                ai.py: local model, or hybrid
  4. read        fields + MijnKantoor folder + booking proposal                ai.py: local model, or hybrid
  5. check       arithmetic, VAT, IBAN, duplicates, deadlines                 checks.py, no AI
  6. review      a person approves, corrects, asks the client or discards
  7. deliver     MijnKantoor folder/ZIP, Exact API or import file, Excel      exports.py, exact_api.py
Nothing leaves step 6 without a person's approval.

Privacy controls (see README): local-first AI with an Apache 2.0 licence gate (models.py); in hybrid mode only
pseudonymised text goes to Claude through one checked door (egress.py), never a page image; everything stored is
encrypted (vault.py); originals and pseudonymisation mappings are deleted once the export is confirmed
(retention below); messages to clients are drafts until a staff member approves them (outbox.py); 2FA (auth.py);
EU-only production (region.py).
"""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session,
                   url_for)
from werkzeug.utils import secure_filename

import envfile  # noqa: F401  (loads .env before the modules below read their settings)
import region
import vault
import ai
import auth
import checks
import coversheet
import egress
import exports
import models
import outbox as outbox_mod
import privacy
import scan
import whatsapp
from exact_api import Exact, ExactError, auth_url as exact_auth_url, configured as exact_configured

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
DATA = Path(os.environ.get("SCAN_DATA", HERE / "data"))
DATA.mkdir(parents=True, exist_ok=True)
region.enforce()
vault.init(DATA)
BATCHES = DATA / "batches"
MAPS = DATA / "vault" / "maps"
EGRESS_LOG = DATA / "egress.log"
RETENTION_INTERVAL = int(os.environ.get("RETENTION_INTERVAL", "300"))
REJECTED_GRACE_HOURS = float(os.environ.get("REJECTED_GRACE_HOURS", "24"))
INBOX = Path(os.environ.get("SCAN_INBOX", DATA / "scanner-inbox"))
AUDIT = DATA / "audit.jsonl"
BRAND = os.environ.get("BRAND_NAME", "Scanstraat")
ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp"}

app = Flask(__name__)


def _secret():
    f = DATA / ".secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        os.chmod(f, 0o600)
    return f.read_text()


app.secret_key = os.environ.get("SECRET_KEY") or _secret()
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

lock = threading.RLock()


def now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- storage
class JsonFile:
    def __init__(self, path, default):
        self.path, self.default = path, default

    def load(self):
        if not self.path.exists():
            return json.loads(json.dumps(self.default))
        return vault.read_json(self.path)

    def save(self, data):
        vault.write_json(self.path, data)


STATE = JsonFile(DATA / "state.json", {"batches": {}, "docs": {}, "seq": 0})
CLIENTS = JsonFile(DATA / "clients.json", [])
SETTINGS = JsonFile(DATA / "settings.json", {
    "manual_minutes": None, "mk_route": "zip", "mk_sync_dir": "", "folders": dict(exports.DEFAULT_FOLDERS),
    "journal_purchase": "60", "journal_sales": "70", "vat_map": {}, "watch_inbox": True,
    "retention_required": ["mijnkantoor", "exact"]})
state = STATE.load()


def save_state():
    with lock:
        STATE.save(state)


def settings():
    s = SETTINGS.load()
    for k, v in SETTINGS.default.items():
        s.setdefault(k, v)
    return s


def clients():
    return {c["nr"]: c for c in CLIENTS.load()}


def charts():
    f = DATA / "charts.json"
    return json.loads((f if f.exists() else SAMPLES / "charts.json").read_text(encoding="utf-8"))


def chart_for(client):
    return charts().get((client or {}).get("chart", "standaard"), charts().get("standaard", []))


def reference_text(client_nr):
    folder = SAMPLES / "reference" / str(client_nr)
    if not folder.is_dir():
        return ""
    parts = []
    for name in ("client-profile.json", "supplier-map.csv", "coding-rules.md"):
        f = folder / name
        if f.exists():
            parts.append(f"<{name}>\n{f.read_text(encoding='utf-8').strip()}\n</{name}>")
    return "\n\n".join(parts)


def ledger_history(client_nr):
    f = SAMPLES / "reference" / str(client_nr) / "ledger_history.csv"
    if not f.exists():
        return []
    with f.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def audit(batch_id, doc_id, event, actor="Systeem", **details):
    entry = {"ts": now(), "batch": batch_id, "doc": doc_id, "event": event, "actor": actor, "details": details}
    with lock:
        vault.append_line(AUDIT, entry)


def read_audit():
    return vault.read_lines(AUDIT)


def log_egress(rec):
    with lock:
        vault.append_line(EGRESS_LOG, rec)
    audit("-", rec.get("doc") or "-", "egress_" + rec["result"], "Systeem", purpose=rec["purpose"],
          destination=rec["destination"], sha256=rec["sha256"][:16], reason=rec.get("reason"))


egress._log = log_egress


def first_start():
    if not CLIENTS.path.exists():
        CLIENTS.save(json.loads((SAMPLES / "clients.json").read_text(encoding="utf-8")))
    if not SETTINGS.path.exists():
        SETTINGS.save(SETTINGS.load())
    INBOX.mkdir(parents=True, exist_ok=True)


first_start()
users = auth.Users(DATA / "users.json")
auth.init(app, users, audit)
exact = Exact(DATA / "exact-token.json")
outbox = outbox_mod.Outbox(DATA / "outbox.json", audit)


def who():
    return (auth.current_user() or {}).get("name", "Systeem")


# ---------------------------------------------------------------- template helpers
@app.template_filter("eur")
def eur(v):
    if v in (None, ""):
        return "–"
    return "€ " + checks.nl(float(v))


@app.template_filter("dt")
def dt(v, fmt="%d-%m-%Y %H:%M"):
    if not v:
        return ""
    try:
        return datetime.fromisoformat(str(v)).strftime(fmt)
    except ValueError:
        return v


@app.template_filter("d")
def d_filter(v):
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except (TypeError, ValueError):
        return v or "–"


def open_docs():
    return [d for d in state["docs"].values() if d["status"] == "review"]


@app.context_processor
def globals_():
    return {"brand": BRAND, "user": auth.current_user(), "live": ai.live(), "ai_label": ai.label(), "local_ai": ai.local(), "open_count": len(open_docs()),
            "draft_count": len(outbox.drafts()) if auth.current_user() else 0,
            "KINDS": ai.KINDS, "VAT_CODES": ai.VAT_CODES, "feat": {}}


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def err(e):
    msgs = {400: getattr(e, "description", "Ongeldig verzoek"), 403: "Geen toegang", 404: "Niet gevonden",
            413: "Bestand te groot (max 200 MB)"}
    return render_template("error.html", code=e.code, message=msgs.get(e.code, "Fout")), e.code


# ---------------------------------------------------------------- pipeline
STEPS = ["Pagina's inlezen", "Lege pagina's en scheidingsbladen", "Documenten splitsen", "Lezen en voorstel maken", "Controles"]


def new_id(prefix):
    with lock:
        key = f"seq_{prefix}"
        state[key] = state.get(key, 0) + 1
        return f"{prefix}{state[key]:04d}"


def batch_dir(bid):
    return BATCHES / bid


def match_client(hint, cl):
    n = checks.norm(hint)
    if not n:
        return None
    for c in cl.values():
        cn = checks.norm(c["name"])
        core = checks.norm(re.sub(r"\b(b\.?v\.?|v\.?o\.?f\.?|holding)\b", "", c["name"], flags=re.I))
        if cn in n or n in cn or (core and len(core) > 5 and core in n):
            return c["nr"]
    return None


def sample_manifest():
    f = SAMPLES / "scan-stack.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def create_batch(files, source, actor, replay=False):
    """files: list of (filename, bytes). Starts processing in the background."""
    bid = new_id("S")
    src = batch_dir(bid) / "source"
    src.mkdir(parents=True, exist_ok=True)
    names = []
    for i, (name, data) in enumerate(files, 1):
        safe = secure_filename(name) or f"scan{i}.pdf"
        vault.write(src / f"{i:02d}_{safe}", data)
        names.append(name)
    batch = {"id": bid, "name": names[0] if len(names) == 1 else f"{len(names)} bestanden", "files": names,
             "source": source, "by": actor, "created": now(), "status": "processing", "step": 0, "detail": "",
             "pages": [], "docs": [], "error": None, "seconds": None, "mode": "replay" if replay or not ai.live() else ai.MODE,
             "usage": {"input_tokens": 0, "output_tokens": 0}}
    with lock:
        state["batches"][bid] = batch
        save_state()
    audit(bid, "-", "received", actor, files=names, source=source)
    threading.Thread(target=process, args=(bid,), daemon=True).start()
    return bid


def _set(batch, **kw):
    with lock:
        batch.update(kw)
        save_state()


def process(bid):
    batch = state["batches"][bid]
    t0 = time.time()
    try:
        _process(batch)
        _set(batch, status="done", step=len(STEPS), detail="", seconds=round(time.time() - t0, 1))
        audit(bid, "-", "processed", "Systeem", documents=len(batch["docs"]), seconds=batch["seconds"])
    except Exception as e:  # noqa: BLE001 - shown to the user, logged in full
        traceback.print_exc()
        _set(batch, status="error", error=str(e) or e.__class__.__name__, seconds=round(time.time() - t0, 1))
        audit(bid, "-", "failed", "Systeem", error=str(e))


_texts = {}  # (batch, page) -> locally read text, hybrid mode only. In memory, never written to disk.


def page_texts(bid, pages_dir, nums):
    out = []
    for p in nums:
        if (bid, p) not in _texts:
            text, _how = ai.page_text(scan.jpeg_bytes(scan.page_image(pages_dir, p), 1600))
            _texts[(bid, p)] = text
        out.append(_texts[(bid, p)])
    return out


def _process(batch):
    bid = batch["id"]
    use_ai = batch["mode"] != "replay"
    pages_dir = batch_dir(bid) / "pages"
    cl = clients()
    sources = sorted((batch_dir(bid) / "source").iterdir())

    # 1 + 2: pages, blank pages, cover sheets (plain code)
    pages, n = [], 0
    source_hashes = []
    for f in sources:
        _set(batch, step=0, detail=f"{f.name[3:]} inlezen")
        data = vault.read(f)
        source_hashes.append(hashlib.sha256(data).hexdigest())
        for img in scan.load_pages(f.name, data):
            n += 1
            scan.save_page(img, pages_dir, n)
            if scan.is_blank(img):
                kind, nr = "blank", None
            else:
                nr = scan.read_cover_sheet(img)
                kind = "cover" if nr else "page"
            pages.append({"n": n, "kind": kind, "client_nr": nr, "doc": None})
            _set(batch, pages=pages, detail=f"{n} pagina's gelezen")
    _set(batch, step=1, detail="")
    groups, cur = [], None
    for p in pages:
        if p["kind"] == "cover":
            cur = {"client_nr": p["client_nr"] if p["client_nr"] in cl else None, "cover": p["client_nr"], "pages": []}
            groups.append(cur)
        elif p["kind"] == "page":
            if cur is None:
                cur = {"client_nr": None, "cover": None, "pages": []}
                groups.append(cur)
            cur["pages"].append(p["n"])
    groups = [g for g in groups if g["pages"]]
    for g in groups:
        if g["cover"] and not g["client_nr"]:
            batch.setdefault("warnings", []).append(f"Scheidingsblad met onbekend relatienummer {g['cover']}.")
    if not groups:
        raise ValueError("Geen pagina's met inhoud gevonden (alleen lege pagina's of scheidingsbladen).")

    # 3: split into documents
    _set(batch, step=2, detail="")
    manifest = sample_manifest()
    is_sample = (manifest and len(sources) == 1 and source_hashes[0] == manifest["sha256"])
    specs = []
    for g in groups:
        if use_ai:
            for start in range(0, len(g["pages"]), 20):
                chunk = g["pages"][start:start + 20]
                _set(batch, detail=f"AI bekijkt {len(chunk)} pagina's")
                jpgs = [scan.jpeg_bytes(scan.page_image(pages_dir, p), 1000) for p in chunk]
                texts = page_texts(bid, pages_dir, chunk) if ai.hybrid() else None
                docs, usage, trail = ai.split(jpgs, texts, [c["name"] for c in cl.values()], privacy.Pseudonymiser(), bid)
                _add_usage(batch, usage)
                batch.setdefault("split_trail", []).append(trail)
                for d in docs:
                    specs.append({"pages": [chunk[i - 1] for i in d["pages"]], "kind": d["kind"],
                                  "client_hint": d.get("client_hint"), "reason": d.get("reason", ""),
                                  "group_client": g["client_nr"]})
        elif is_sample:
            ai.replay_delay()
            for d in manifest["documents"]:
                if d["pages"][0] in g["pages"]:
                    specs.append({"pages": d["pages"], "kind": d["answer"]["kind"], "answer": d["answer"],
                                  "client_hint": d["answer"].get("client_hint"),
                                  "reason": d["answer"].get("split_reason") or "Zelfde afzender en kenmerk",
                                  "group_client": g["client_nr"]})
        else:
            for p in g["pages"]:
                specs.append({"pages": [p], "kind": "overig", "answer": ai.empty_result(), "client_hint": None,
                              "reason": "Replay-modus: elke pagina apart", "group_client": g["client_nr"]})

    # 4: read each document
    _set(batch, step=3, detail=f"0 van {len(specs)} documenten")
    doc_ids = []
    for spec in specs:
        did = new_id("D")
        client_nr = spec["group_client"] or match_client(spec.get("client_hint"), cl)
        doc = {"id": did, "batch": bid, "pages": spec["pages"], "client_nr": client_nr,
               "client_via": "scheidingsblad" if spec["group_client"] else ("herkend op document" if client_nr else None),
               "kind": spec["kind"], "split_reason": spec.get("reason", ""), "result": None, "status": "reading",
               "flags": [], "corrections": [], "exports": {}, "created": now(), "seconds": None, "filing": None}
        with lock:
            state["docs"][did] = doc
        for p in spec["pages"]:
            pages[p - 1]["doc"] = did
        doc_ids.append(did)
    _set(batch, docs=doc_ids, pages=pages)

    done = [0]

    def read_one(i):
        spec, doc = specs[i], state["docs"][doc_ids[i]]
        t = time.time()
        scan.build_pdf(pages_dir, doc["pages"], pdf_path(doc))
        if use_ai:
            result = read_doc(doc, cl)
        else:
            ai.replay_delay()
            result = json.loads(json.dumps(spec["answer"]))
            doc["trail"] = {"route": "replay (geen AI)"}
        with lock:
            doc["result"] = result
            doc["kind"] = result.get("kind") or doc["kind"]
            if not doc["client_nr"]:
                doc["client_nr"] = match_client(result.get("client_hint"), cl)
                doc["client_via"] = "herkend op document" if doc["client_nr"] else None
            doc["filing"] = exports.proposal(doc, cl.get(doc["client_nr"]) or {}, settings()["folders"])
            doc["status"] = "review"
            doc["seconds"] = round(time.time() - t, 1)
            done[0] += 1
            _set(batch, detail=f"{done[0]} van {len(specs)} documenten")
        audit(bid, doc["id"], "read", "Systeem", model=batch["mode"], route=(doc.get("trail") or {}).get("route"))

    with ThreadPoolExecutor(max_workers=1 if ai.live() else 3) as pool:
        list(pool.map(read_one, range(len(specs))))

    # 5: checks
    _set(batch, step=4, detail="")
    recheck_batch(bid)
    for key in [k for k in _texts if k[0] == bid]:
        _texts.pop(key, None)


def read_doc(doc, cl):
    """Read one document with the configured AI. Stores the pseudonymisation mapping sealed, and the trail."""
    client = cl.get(doc.get("client_nr")) if doc.get("client_nr") else None
    pages_dir = batch_dir(doc["batch"]) / "pages"
    pseudo = privacy.Pseudonymiser()
    try:
        jpgs = [scan.jpeg_bytes(scan.page_image(pages_dir, p), 1600) for p in doc["pages"]]
        texts = page_texts(doc["batch"], pages_dir, doc["pages"]) if ai.hybrid() else None
        result, usage, trail = ai.read(jpgs, texts, client or {"name": "onbekend"}, chart_for(client),
                                       reference_text(doc["client_nr"]) if client else "", pseudo, doc["id"])
        _add_usage(state["batches"][doc["batch"]], usage)
    except ai.AIError as e:
        result, trail = ai.empty_result(doc["kind"], f"AI kon dit document niet lezen: {e}"), {"route": "mislukt", "error": str(e)}
    if pseudo.mapping:
        vault.write_json(MAPS / f"{doc['id']}.json", pseudo.mapping)
        trail["mapping"] = "versleuteld in de kluis"
    doc["trail"] = trail
    return result


def _add_usage(batch, usage):
    with lock:
        batch["usage"]["input_tokens"] += usage.get("input_tokens", 0)
        batch["usage"]["output_tokens"] += usage.get("output_tokens", 0)


# ---------------------------------------------------------------- checks
CHECK_KIND = {"inkoopfactuur": "invoice", "kassabon": "receipt", "creditnota": "credit_note", "verkoopfactuur": "invoice"}
FIELD_ALIAS = {"counterparty": "supplier_name", "reference": "invoice_number", "doc_date": "invoice_date"}


def as_check_fields(doc):
    r = doc["result"] or {}
    f = {"doc_type": CHECK_KIND.get(doc["kind"], "other"), "supplier_name": r.get("counterparty"),
         "invoice_number": r.get("reference"), "invoice_date": r.get("doc_date")}
    for k in ("due_date", "supplier_vat_number", "supplier_kvk", "iban", "net_total", "vat_lines", "gross_total", "currency"):
        f[k] = r.get(k)
    f["uncertain_fields"] = [FIELD_ALIAS.get(x, x) for x in r.get("uncertain_fields") or []]
    f["issues"] = r.get("issues") or []
    return f


def compute_flags(doc, earlier, history):
    D = checks.D
    r = doc["result"] or {}
    flags = []
    cl = clients()
    if not doc.get("client_nr"):
        flags.append(checks.flag("error", "client", "Klant onbekend: geen scheidingsblad en niet herkend op het document. Kies de klant."))
    if doc["kind"] in ai.BOOKABLE:
        f = as_check_fields(doc)
        if doc["kind"] == "verkoopfactuur":
            f["supplier_name"] = f"verkoop {r.get('counterparty')}"
        flags += checks.run_checks(f, others=[as_check_fields(o) for o in earlier], history=history)
        lines = r.get("lines") or []
        if not lines:
            flags.append(checks.flag("error", "lines", "Geen boekingsregels. Vul grootboek en btw-code in."))
        else:
            accounts = {a for a, _ in chart_for(cl.get(doc.get("client_nr")))}
            for l in lines:
                if accounts and l.get("account") not in accounts:
                    flags.append(checks.flag("error", "lines", f"Grootboekrekening {l.get('account')} staat niet in het rekeningschema van deze klant."))
            total = sum((D(l.get("amount_excl")) or 0) + (D(l.get("vat_amount")) or 0) for l in lines)
            gross = D(r.get("gross_total"))
            math_error = any(x["level"] == "error" and x["field"] in ("vat_lines", "gross_total") for x in flags)
            if gross is not None and not math_error and abs(abs(total) - abs(gross)) > checks.TOL:
                flags.append(checks.flag("warning", "lines",
                                         f"Boekingsregels tellen op tot {checks.nl(total)}, het document zegt {checks.nl(gross)}. Klopt dit met de btw-correctie?"))
    else:
        flags += checks.model_flags({"uncertain_fields": [FIELD_ALIAS.get(x, x) for x in r.get("uncertain_fields") or []],
                                     "issues": r.get("issues") or []})
        due = checks.parse_date(r.get("due_date"))
        if due and due >= date.today() and due <= date.today() + timedelta(days=45) and \
                not any("termijn" in (x.get("message") or "").lower() for x in flags):
            flags.append(checks.flag("warning", "due_date", f"Termijn loopt af op {due.strftime('%d-%m-%Y')}."))
    if len(doc["pages"]) > 1:
        flags.append(checks.flag("info", None, f"{len(doc['pages'])} pagina's samengevoegd tot één document: {doc.get('split_reason') or 'zelfde document'}."))
    return flags


def recheck_batch(bid):
    with lock:
        docs = [state["docs"][d] for d in state["batches"][bid]["docs"]]
        for i, doc in enumerate(docs):
            if doc["result"] is None or doc["status"] == "rejected":
                continue
            earlier = [o for o in docs[:i] if o["result"] and o["status"] != "rejected" and o.get("client_nr") == doc.get("client_nr")]
            history = ledger_history(doc.get("client_nr")) + [
                as_check_fields(o) | {"supplier_name": o["result"].get("counterparty")}
                for o in state["docs"].values()
                if o["batch"] != bid and o["status"] == "approved" and o.get("client_nr") == doc.get("client_nr")]
            doc["flags"] = compute_flags(doc, earlier, history)
            doc["light"] = checks.status_of(doc["flags"])
        save_state()


# ---------------------------------------------------------------- scanner folder watcher
def watch_inbox():
    """Scanner 'scan to folder' target. Each new file (stable for 3 s) becomes a scan batch."""
    seen = {}
    while True:
        try:
            if settings().get("watch_inbox", True) and INBOX.is_dir():
                for f in INBOX.iterdir():
                    if f.suffix.lower() not in ALLOWED or not f.is_file():
                        continue
                    size = f.stat().st_size
                    if seen.get(f.name) != size:
                        seen[f.name] = size
                        continue
                    data = f.read_bytes()
                    create_batch([(f.name, data)], "scanner", "Scanner")  # stored encrypted from here on
                    f.unlink()  # no plain copy stays behind in the scan folder
                    seen.pop(f.name, None)
        except Exception:  # noqa: BLE001 - keep watching
            traceback.print_exc()
        time.sleep(3)


def seed():
    """First start: process the sample scan once so the first visitor sees a full dashboard."""
    stack = SAMPLES / "scan_2026-09-30_0915.pdf"
    if not state["batches"] and stack.exists() and os.environ.get("SCAN_SEED", "1") == "1":
        # The first-start sample replays the answer key (labelled replay): fast, and nothing is sent anywhere.
        create_batch([(stack.name, stack.read_bytes())], "voorbeeld", "Systeem", replay=True)


# ---------------------------------------------------------------- pages
def kpis():
    docs = list(state["docs"].values())
    today = date.today().isoformat()
    week = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    pages = [p for b in state["batches"].values() for p in b["pages"]]
    b_today = [b for b in state["batches"].values() if b["created"][:10] >= week]
    s = settings()
    approved = [d for d in docs if d["status"] == "approved"]
    secs = [b["seconds"] / max(1, len(b["docs"])) for b in state["batches"].values() if b.get("seconds") and b["docs"]]
    k = {
        "pages_week": sum(len(b["pages"]) for b in b_today),
        "pages_total": len(pages),
        "blank_removed": sum(1 for p in pages if p["kind"] == "blank"),
        "covers": sum(1 for p in pages if p["kind"] == "cover"),
        "docs_total": len([d for d in docs if d["status"] != "reading"]),
        "review": len([d for d in docs if d["status"] == "review"]),
        "asked": len([d for d in docs if d["status"] == "asked"]),
        "approved": len(approved),
        "rejected": len([d for d in docs if d["status"] == "rejected"]),
        "mk_done": len([d for d in approved if d["exports"].get("mijnkantoor")]),
        "exact_done": len([d for d in approved if d["exports"].get("exact")]),
        "excel_done": len([d for d in approved if d["exports"].get("excel")]),
        "bookable": len([d for d in approved if d["kind"] in ai.BOOKABLE]),
        "sec_per_doc": round(sum(secs) / len(secs), 1) if secs else None,
        "today": len([d for d in docs if d["created"][:10] == today]),
        "manual_minutes": s.get("manual_minutes"),
        "errors": len([d for d in docs if d["status"] == "review" and d.get("light") == "red"]),
    }
    if k["manual_minutes"]:
        k["hours_saved"] = round(k["docs_total"] * float(k["manual_minutes"]) / 60, 1)
    return k


@app.get("/")
def dashboard():
    cl = clients()
    per_client = []
    for nr, c in cl.items():
        ds = [d for d in state["docs"].values() if d.get("client_nr") == nr and d["status"] != "reading"]
        per_client.append({"client": c, "total": len(ds), "review": sum(d["status"] == "review" for d in ds),
                           "red": sum(d["status"] == "review" and d.get("light") == "red" for d in ds),
                           "asked": sum(d["status"] == "asked" for d in ds),
                           "approved": sum(d["status"] == "approved" for d in ds),
                           "delivered": sum(bool(d["exports"].get("mijnkantoor")) for d in ds)})
    unknown = [d for d in state["docs"].values() if not d.get("client_nr") and d["status"] == "review"]
    batches = sorted(state["batches"].values(), key=lambda b: b["created"], reverse=True)[:8]
    queue = sorted(open_docs(), key=lambda d: ({"red": 0, "amber": 1, "green": 2}.get(d.get("light"), 3), d["id"]))[:8]
    hour = datetime.now().hour
    greeting = "Goedemorgen" if hour < 12 else "Goedemiddag" if hour < 18 else "Goedenavond"
    return render_template("dashboard.html", nav="overview", greeting=greeting, k=kpis(), per_client=per_client, unknown=unknown,
                           batches=batches, queue=queue, clients=cl, integ=integrations_status())


def integrations_status():
    s = settings()
    tok = exact.token()
    return {
        "scanner": {"ok": INBOX.is_dir() and s.get("watch_inbox", True), "text": "Scanmap bewaakt" if s.get("watch_inbox", True) else "Scanmap uit"},
        "mijnkantoor": {"ok": s["mk_route"] == "folder" and bool(s.get("mk_sync_dir")),
                        "text": "Synchronisatiemap" if s["mk_route"] == "folder" and s.get("mk_sync_dir") else "ZIP voor upload"},
        "exact": {"ok": bool(tok), "text": "Gekoppeld" if tok else ("Klaar om te koppelen" if exact_configured() else "Importbestand (XML/CSV)")},
        "excel": {"ok": True, "text": "Register (.xlsx)"},
        "vat_map": bool(s.get("vat_map")),
    }


@app.route("/scannen", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        if request.form.get("sample"):
            stack = SAMPLES / "scan_2026-09-30_0915.pdf"
            bid = create_batch([(stack.name, stack.read_bytes())], "voorbeeld", who())
            return redirect(url_for("batch_page", bid=bid))
        files = [(f.filename, f.read()) for f in request.files.getlist("files")
                 if f and f.filename and Path(f.filename).suffix.lower() in ALLOWED]
        if not files:
            flash("Kies een of meer scans (pdf of foto).", "error")
            return redirect(url_for("upload"))
        bid = create_batch(files, "upload", who())
        return redirect(url_for("batch_page", bid=bid))
    return render_template("upload.html", nav="scan", inbox=str(INBOX), sample=(SAMPLES / "scan-stack.json").exists())


def batch_view(bid):
    batch = state["batches"].get(bid) or abort(404)
    docs = [state["docs"][d] for d in batch["docs"]]
    return batch, docs


@app.get("/scan/<bid>")
def batch_page(bid):
    batch, docs = batch_view(bid)
    return render_template("batch.html", nav="scan", b=batch, docs=docs, steps=STEPS, clients=clients())


@app.get("/api/scan/<bid>")
def batch_api(bid):
    batch, docs = batch_view(bid)
    return jsonify({"status": batch["status"], "step": batch["step"], "detail": batch["detail"],
                    "pages": len(batch["pages"]), "docs": len(docs), "error": batch["error"],
                    "read": sum(1 for d in docs if d["result"] is not None)})


@app.post("/scan/<bid>/indeling")
def batch_regroup(bid):
    """Merge a document with the previous one, or split it after a page. Re-reads the changed documents."""
    batch, docs = batch_view(bid)
    if batch["status"] != "done":
        abort(400, "Wacht tot de scan klaar is.")
    action, did = request.form.get("action"), request.form.get("doc")
    ids = batch["docs"]
    if did not in ids:
        abort(404)
    i = ids.index(did)
    doc = state["docs"][did]
    if doc["status"] == "approved" or (action == "merge" and state["docs"][ids[i - 1]]["status"] == "approved"):
        abort(400, "Goedgekeurde documenten kun je niet meer samenvoegen of splitsen.")
    changed = []
    with lock:
        if action == "merge" and i > 0:
            prev = state["docs"][ids[i - 1]]
            prev["pages"] += doc["pages"]
            prev["split_reason"] = f"handmatig samengevoegd door {who()}"
            ids.remove(did)
            doc["status"] = "rejected"
            doc["rejected"] = {"by": who(), "at": now(), "note": f"samengevoegd met {prev['id']}"}
            changed = [prev]
        elif action == "split":
            after = int(request.form.get("after", 0))
            if after not in doc["pages"] or after == doc["pages"][-1]:
                abort(400)
            k = doc["pages"].index(after) + 1
            nid = new_id("D")
            new = json.loads(json.dumps(doc)) | {"id": nid, "pages": doc["pages"][k:], "created": now(),
                                                 "split_reason": f"handmatig gesplitst door {who()}", "exports": {}}
            doc["pages"] = doc["pages"][:k]
            state["docs"][nid] = new
            ids.insert(i + 1, nid)
            changed = [doc, new]
        for p in batch["pages"]:
            for c in changed:
                if p["n"] in c["pages"]:
                    p["doc"] = c["id"]
        save_state()
    for c in changed:
        scan.build_pdf(batch_dir(bid) / "pages", c["pages"], batch_dir(bid) / "docs" / f"{c['id']}.pdf")
        c["status"] = "reading"
        threading.Thread(target=reread, args=(c["id"],), daemon=True).start()
    audit(bid, did, "regrouped", who(), action=action)
    flash("Indeling aangepast. De betrokken documenten worden opnieuw gelezen.")
    return redirect(url_for("batch_page", bid=bid))


def reread(did):
    doc = state["docs"][did]
    cl = clients()
    client = cl.get(doc.get("client_nr"))
    if ai.live():
        result = read_doc(doc, cl)
    else:
        ai.replay_delay()
        result = doc["result"] or ai.empty_result()
        result = dict(result, issues=list(result.get("issues") or []) +
                      ["Replay-modus: na splitsen of samenvoegen niet opnieuw gelezen. Controleer de velden."])
    with lock:
        doc["result"] = result
        doc["kind"] = result.get("kind") or doc["kind"]
        doc["filing"] = exports.proposal(doc, client or {}, settings()["folders"])
        doc["status"] = "review"
    recheck_batch(doc["batch"])


@app.get("/pagina/<bid>/<int:n>.jpg")
def page_img(bid, n):
    batch_view(bid)
    f = batch_dir(bid) / "pages" / f"{'t' if request.args.get('t') else 'p'}{n:03d}.jpg"
    if not f.exists():
        abort(404)
    return send_file(io.BytesIO(vault.read(f)), mimetype="image/jpeg")


@app.get("/document/<did>.pdf")
def doc_pdf(did):
    doc = state["docs"].get(did) or abort(404)
    f = pdf_path(doc)
    if not f.exists():
        abort(404)
    return send_file(io.BytesIO(vault.read(f)), mimetype="application/pdf", download_name=(doc.get("filing") or {}).get("filename", f"{did}.pdf"),
                     as_attachment=bool(request.args.get("download")))


def _num(v):
    v = (v or "").strip().replace("€", "").replace(" ", "")
    if not v:
        return None
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    try:
        return round(float(v), 2)
    except ValueError:
        return None


@app.route("/document/<did>", methods=["GET", "POST"])
def review(did):
    doc = state["docs"].get(did) or abort(404)
    batch = state["batches"][doc["batch"]]
    cl = clients()
    if request.method == "POST":
        action = request.form.get("action")
        if doc["status"] == "reading":
            abort(400, "Dit document wordt nog gelezen.")
        if action == "reopen":
            if any(doc["exports"].values()) or doc.get("purged"):
                abort(400, "Al doorgezet naar MijnKantoor of Exact: heropenen kan niet meer.")
            doc["status"] = "review"
            doc.pop("approved", None)
            audit(doc["batch"], did, "reopened", who())
        else:
            changes = apply_form(doc, request.form, cl)
            if changes:
                audit(doc["batch"], did, "corrected", who(), changes=changes)
            recheck_batch(doc["batch"])
            open_flags = [f["message"] for f in doc["flags"] if f["level"] in ("error", "warning")]
            if action == "approve":
                if not doc.get("client_nr"):
                    flash("Kies eerst de klant.", "error")
                    return redirect(url_for("review", did=did))
                if doc.get("light") == "red" and not request.form.get("ack"):
                    flash("Er staan nog fouten open. Vink aan dat je ze gezien hebt, of vraag de klant.", "error")
                    return redirect(url_for("review", did=did))
                doc["status"] = "approved"
                doc["approved"] = {"by": who(), "at": now(), "open_flags": open_flags}
                audit(doc["batch"], did, "approved", who(), open_flags=open_flags)
            elif action == "ask":
                note = request.form.get("note", "").strip() or "Graag een leesbare kopie of toelichting."
                doc["status"] = "asked"
                c = cl.get(doc.get("client_nr")) or {}
                mid = outbox.draft(request.form.get("channel") if request.form.get("channel") in outbox_mod.CHANNELS else "email",
                                   c.get("email") or "", f"Vraag over uw administratie ({c.get('name', 'klant')})",
                                   client_question(doc, c, note), doc.get("client_nr"), did, who(), "vraag bij controle")
                doc["asked"] = {"by": who(), "at": now(), "note": note, "message": mid}
                audit(doc["batch"], did, "asked_client", who(), note=note, message=mid)
            elif action == "reject":
                note = request.form.get("note", "").strip() or "Niet voor de administratie"
                doc["status"] = "rejected"
                doc["rejected"] = {"by": who(), "at": now(), "note": note}
                audit(doc["batch"], did, "rejected", who(), note=note)
        save_state()
        if action in ("approve", "ask", "reject"):
            nxt = next((d for d in sorted(open_docs(), key=lambda d: d["id"]) if d["id"] != did), None)
            flash({"approve": "Goedgekeurd.", "ask": "Conceptbericht klaargezet onder Berichten. Er is nog niets verstuurd.",
                   "reject": "Uit de stapel gehaald."}[action])
            return redirect(url_for("review", did=nxt["id"]) if nxt else url_for("dashboard"))
        flash("Opgeslagen.")
        return redirect(url_for("review", did=did))
    siblings = batch["docs"]
    i = siblings.index(did) if did in siblings else 0
    client = cl.get(doc.get("client_nr"))
    return render_template("review.html", nav="scan", doc=doc, b=batch, clients=cl, client=client,
                           mapping_kinds=privacy.summary(vault.read_json(MAPS / f"{did}.json", {})),
                           chart=chart_for(client) if client else [],
                           prev=siblings[i - 1] if i > 0 else None, next=siblings[i + 1] if i + 1 < len(siblings) else None,
                           pos=i + 1, total=len(siblings), s=settings())


def client_question(doc, client, note):
    """Draft text for a question to the client. Plain and factual: only the document and what is missing."""
    r = doc.get("result") or {}
    what = " ".join(x for x in [ai.KINDS.get(doc["kind"], "document").lower(), r.get("counterparty") or "",
                                r.get("reference") or "", f"van {r['doc_date']}" if r.get("doc_date") else ""] if x)
    return (f"Beste {client.get('contact') or 'klant'},\n\nBij het verwerken van uw administratie hebben we een vraag over "
            f"het volgende stuk: {what}.\n\n{note}\n\nAlvast bedankt.\n\nMet vriendelijke groet,\n{who()}")


EDITABLE = ["counterparty", "reference", "doc_date", "due_date", "supplier_vat_number", "iban", "description"]


def apply_form(doc, form, cl):
    r = doc["result"]
    changes = {}

    def put(key, new, target=r):
        old = target.get(key)
        if (old if old not in ("", None) else None) != (new if new not in ("", None) else None):
            changes[key] = {"from": old, "to": new}
            target[key] = new

    if form.get("client_nr") is not None:
        nr = form.get("client_nr") or None
        if nr != doc.get("client_nr") and (nr is None or nr in cl):
            changes["client"] = {"from": doc.get("client_nr"), "to": nr}
            doc["client_nr"] = nr
            doc["client_via"] = f"gekozen door {who()}" if nr else None
    if form.get("kind") in ai.KINDS and form.get("kind") != doc["kind"]:
        changes["kind"] = {"from": doc["kind"], "to": form["kind"]}
        doc["kind"] = r["kind"] = form["kind"]
    for k in EDITABLE:
        if k in form:
            put(k, form.get(k, "").strip() or None)
    for k in ("net_total", "gross_total"):
        if k in form:
            put(k, _num(form.get(k)))
    if "line_account" in form:
        lines = []
        for acc, vc, ex, va, desc in zip(form.getlist("line_account"), form.getlist("line_vat"), form.getlist("line_excl"),
                                         form.getlist("line_vatamt"), form.getlist("line_desc")):
            if acc and _num(ex) is not None:
                lines.append({"account": acc, "vat_code": vc, "amount_excl": _num(ex), "vat_amount": _num(va) or 0.0,
                              "description": desc.strip()})
        if lines != (r.get("lines") or []):
            changes["lines"] = {"from": r.get("lines"), "to": lines}
            r["lines"] = lines
    if changes:
        for key, ch in changes.items():
            doc["corrections"].append({"field": key, "from": ch["from"], "to": ch["to"], "by": who(), "at": now()})
    if form.get("folder") is not None and form.get("filename") is not None:
        folder = form["folder"].strip().strip("/") or doc["filing"]["folder"]
        name = exports._clean(form["filename"], 120) or doc["filing"]["filename"]
        name = name if name.lower().endswith(".pdf") else name + ".pdf"
        if {"folder": folder, "filename": name} != doc["filing"]:
            doc["filing"] = {"folder": folder, "filename": name}
            changes["filing"] = {"to": f"{folder}/{name}"}
    if changes.keys() & {"client", "kind", "counterparty", "reference", "doc_date"} and not changes.get("filing"):
        doc["filing"] = exports.proposal(doc, cl.get(doc.get("client_nr")) or {}, settings()["folders"])
    return changes


# ---------------------------------------------------------------- delivery
def approved_docs(pending_key=None):
    ds = [d for d in state["docs"].values() if d["status"] == "approved"]
    if pending_key:
        ds = [d for d in ds if not d["exports"].get(pending_key)]
    return sorted(ds, key=lambda d: d["id"])


def pdf_path(doc):
    return batch_dir(doc["batch"]) / "docs" / f"{doc['id']}.pdf"


def _selected(key):
    ids = request.values.getlist("ids") or [d["id"] for d in approved_docs(key)]
    return [state["docs"][i] for i in ids if i in state["docs"] and state["docs"][i]["status"] == "approved"]


def _exact_doc(doc):
    s = settings()
    return doc | {"exact_journal": s["journal_sales"] if doc["kind"] == "verkoopfactuur" else s["journal_purchase"],
                  "exact_vat": s.get("vat_map") or {}}


@app.get("/afhandelen")
def deliver():
    ds = approved_docs()
    return render_template("deliver.html", nav="deliver", docs=ds, clients=clients(), s=settings(),
                           integ=integrations_status(), exact_tok=exact.token(), exact_ready=exact_configured(),
                           pending={k: len(approved_docs(k)) for k in ("mijnkantoor", "exact", "excel")},
                           bookable=[d for d in ds if d["kind"] in ai.BOOKABLE])


def _mark(docs, key, info):
    with lock:
        for d in docs:
            d["exports"][key] = info | {"at": now(), "by": who()}
        save_state()


@app.post("/afhandelen/mijnkantoor")
def deliver_mk():
    s = settings()
    docs, cl = _selected("mijnkantoor"), clients()
    if not docs:
        flash("Niets om te archiveren.", "error")
        return redirect(url_for("deliver"))
    missing = [d["id"] for d in docs if d.get("client_nr") not in cl]
    if missing:
        abort(400, f"Klant onbekend bij {', '.join(missing)}")
    if request.form.get("route") == "folder":
        root = Path(s.get("mk_sync_dir") or "")
        if not s.get("mk_sync_dir") or not root.is_dir():
            flash("Synchronisatiemap niet gevonden. Stel die in bij Koppelingen, of gebruik de ZIP.", "error")
            return redirect(url_for("deliver"))
        for d in docs:
            target = exports.to_sync_folder(root, d, cl[d["client_nr"]], vault.read(pdf_path(d)))
            _mark([d], "mijnkantoor", {"route": "map", "path": str(target.relative_to(root)), "confirmed": True})
            audit(d["batch"], d["id"], "exported", who(), format="MijnKantoor-map", path=str(target))
        n = purge_ready(who())
        flash(f"{len(docs)} documenten in de MijnKantoor-map gezet." + (f" {n} originelen gewist." if n else ""))
        return redirect(url_for("deliver"))
    if any(d.get("purged") for d in docs):
        abort(400, "Een of meer originelen zijn al gewist na doorzetten.")
    data = exports.zip_bundle([(d, cl[d["client_nr"]], vault.read(pdf_path(d))) for d in docs])
    _mark(docs, "mijnkantoor", {"route": "zip", "confirmed": False})
    for d in docs:
        audit(d["batch"], d["id"], "exported", who(), format="MijnKantoor-ZIP")
    return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True,
                     download_name=f"mijnkantoor-{datetime.now():%Y%m%d-%H%M}.zip")


@app.post("/afhandelen/exact")
def deliver_exact():
    docs = [d for d in _selected("exact") if d["kind"] in ai.BOOKABLE]
    cl = clients()
    fmt = request.form.get("format", "api")
    if not docs:
        flash("Geen goedgekeurde facturen of bonnen om door te zetten.", "error")
        return redirect(url_for("deliver"))
    if fmt in ("xml", "csv"):
        xd = [_exact_doc(d) for d in docs]
        data = exports.exact_xml(xd) if fmt == "xml" else exports.exact_csv(xd)
        _mark(docs, "exact", {"route": fmt, "confirmed": False})
        for d in docs:
            audit(d["batch"], d["id"], "exported", who(), format=f"Exact-{fmt.upper()}")
        return send_file(io.BytesIO(data), as_attachment=True,
                         mimetype="application/xml" if fmt == "xml" else "text/csv",
                         download_name=f"exact-import-{datetime.now():%Y%m%d-%H%M}.{fmt}")
    s = settings()
    ok, failed = 0, []
    for d in docs:
        c = cl.get(d.get("client_nr")) or {}
        try:
            if d.get("purged"):
                raise ExactError("origineel al gewist")
            res = exact.book(c.get("exact_division"), d, vault.read(pdf_path(d)),
                             s["journal_sales"] if d["kind"] == "verkoopfactuur" else s["journal_purchase"], s.get("vat_map") or {})
            _mark([d], "exact", {"route": "api", "confirmed": True} | res)
            audit(d["batch"], d["id"], "exported", who(), format="Exact API", entry=res.get("entry_number"))
            ok += 1
        except ExactError as e:
            failed.append(f"{d['id']}: {e}")
            audit(d["batch"], d["id"], "export_failed", who(), error=str(e))
    if ok:
        n = purge_ready(who())
        flash(f"{ok} boekingen met document aangemaakt in Exact Online." + (f" {n} originelen gewist." if n else ""))
    for f in failed[:5]:
        flash(f, "error")
    return redirect(url_for("deliver"))


@app.post("/afhandelen/bevestigen")
def deliver_confirm():
    """A ZIP or import file is a download: only a person can tell us the import into MijnKantoor/Exact worked."""
    key = request.form.get("key")
    if key not in ("mijnkantoor", "exact"):
        abort(400)
    ids = request.form.getlist("ids")
    done = []
    with lock:
        for i in ids:
            d = state["docs"].get(i)
            if d and (d["exports"].get(key) or {}).get("confirmed") is False:
                d["exports"][key]["confirmed"] = True
                d["exports"][key]["confirmed_by"] = who()
                done.append(d)
        save_state()
    for d in done:
        audit(d["batch"], d["id"], "export_confirmed", who(), destination=key)
    n = purge_ready(who())
    flash(f"{len(done)} bevestigd als geïmporteerd." + (f" {n} originelen en koppeltabellen gewist." if n else ""))
    return redirect(url_for("deliver"))


# ---------------------------------------------------------------- retention (AVG art. 5(1)(c) and (e))
def required_destinations(doc):
    req = settings().get("retention_required") or ["mijnkantoor"]
    return [k for k in req if k != "exact" or doc["kind"] in ai.BOOKABLE]


def delivered(doc):
    """True when every required destination confirmed it holds this document."""
    req = required_destinations(doc)
    return doc["status"] == "approved" and bool(req) and all((doc["exports"].get(k) or {}).get("confirmed") for k in req)


def purge_doc(doc, actor, why):
    """Delete the original (PDF), its page images and the pseudonymisation mapping. Keeps the checked fields."""
    bdir = batch_dir(doc["batch"])
    files, digest = [], None
    if pdf_path(doc).exists():
        digest = hashlib.sha256(vault.read(pdf_path(doc))).hexdigest()
    in_use = {p for o in state["docs"].values() if o["batch"] == doc["batch"] and o["id"] != doc["id"]
              and not o.get("purged") for p in o["pages"]}  # a merged page still belongs to another document
    for f in [pdf_path(doc), MAPS / f"{doc['id']}.json"] + \
             [bdir / "pages" / f"{k}{p:03d}.jpg" for p in doc["pages"] if p not in in_use for k in ("p", "t")]:
        if vault.delete(f):
            files.append(f.name)
    if doc.get("trail"):
        doc["trail"].pop("sent_text", None)
    doc["purged"] = {"at": now(), "by": actor, "why": why, "files": len(files), "sha256": digest}
    audit(doc["batch"], doc["id"], "purged", actor, why=why, deleted=len(files), sha256=(digest or "")[:16])


def purge_batch_sources(bid, actor):
    """When no document of a scan still needs its original, delete the source scan and leftover pages."""
    b = state["batches"].get(bid)
    if not b or b.get("purged") or b["status"] == "processing":
        return
    docs = [state["docs"][d] for d in b["docs"] if d in state["docs"]]
    if docs and all(d.get("purged") for d in docs):
        removed = 0
        for sub in ("source", "pages", "docs"):
            folder = batch_dir(bid) / sub
            if folder.is_dir():
                for f in folder.iterdir():
                    removed += vault.delete(f)
        b["purged"] = {"at": now(), "files": removed}
        audit(bid, "-", "batch_purged", actor, deleted=removed)


def purge_ready(actor="Systeem"):
    """Runs right after every confirmed export, and on a schedule (and from cron: python retention.py)."""
    n = 0
    with lock:
        for doc in list(state["docs"].values()):
            if doc.get("purged"):
                continue
            if delivered(doc):
                purge_doc(doc, actor, "doorgezet en bevestigd")
                n += 1
            elif doc["status"] == "rejected" and doc.get("rejected") and \
                    datetime.fromisoformat(doc["rejected"]["at"]) < datetime.now() - timedelta(hours=REJECTED_GRACE_HOURS):
                purge_doc(doc, actor, f"uit de stapel gehaald, {REJECTED_GRACE_HOURS:g} uur geleden")
                n += 1
        for bid in list(state["batches"]):
            purge_batch_sources(bid, actor)
        save_state()
    return n


def retention_loop():
    while True:
        time.sleep(RETENTION_INTERVAL)
        try:
            purge_ready()
        except Exception:  # noqa: BLE001 - keep running, show in logs
            traceback.print_exc()


def retention_token():
    return hashlib.sha256(("retention:" + app.secret_key).encode()).hexdigest()


@app.post("/internal/retention")
def retention_trigger():
    """For cron (python retention.py): only from this machine, with a token derived from the app secret."""
    if request.remote_addr not in ("127.0.0.1", "::1") or \
            not secrets.compare_digest(request.headers.get("X-Retention-Token", ""), retention_token()):
        abort(403)
    return jsonify({"purged": purge_ready("Planner (cron)")})


@app.get("/afhandelen/register.xlsx")
def deliver_excel():
    docs = approved_docs() if request.args.get("all") else (_selected("excel") or approved_docs())
    data = exports.excel_register(docs, clients(), ai.KINDS)
    _mark(docs, "excel", {"route": "xlsx"})
    audit("-", "-", "exported", who(), format="Excel-register", documents=len(docs))
    return send_file(io.BytesIO(data), as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     download_name=f"scanregister-{datetime.now():%Y%m%d-%H%M}.xlsx")


# ---------------------------------------------------------------- clients and cover sheets
@app.route("/klanten", methods=["GET", "POST"])
def clients_page():
    if request.method == "POST":
        if (auth.current_user() or {}).get("role") != "admin":
            abort(403)
        cl = CLIENTS.load()
        action = request.form.get("action")
        nr = re.sub(r"\D", "", request.form.get("nr", ""))
        if action == "add":
            name = request.form.get("name", "").strip()
            if not nr or not name or any(c["nr"] == nr for c in cl):
                flash("Vul een uniek relatienummer (cijfers) en een naam in.", "error")
            else:
                cl.append({"nr": nr, "name": name, "kvk": request.form.get("kvk", "").strip(),
                           "vat": request.form.get("vat", "").strip().upper(), "city": request.form.get("city", "").strip(),
                           "exact_division": request.form.get("exact_division", "").strip(),
                           "chart": request.form.get("chart", "standaard")})
                flash(f"{name} toegevoegd. Print het scheidingsblad en leg het bij de scanner.")
                audit("-", "-", "client_added", who(), client=name)
        elif action == "update":
            for c in cl:
                if c["nr"] == nr:
                    c["exact_division"] = request.form.get("exact_division", "").strip()
                    c["chart"] = request.form.get("chart", c.get("chart", "standaard"))
                    c["email"] = request.form.get("email", c.get("email", "")).strip()
            flash("Opgeslagen.")
        elif action == "wwft_stop":
            for c in cl:
                if c["nr"] == nr:
                    c["wwft_stop"] = not c.get("wwft_stop")
                    audit("-", "-", "wwft_stop_on" if c["wwft_stop"] else "wwft_stop_off", who(), client=nr)
                    flash(f"Wwft-stop {'aan' if c['wwft_stop'] else 'uit'} voor {c['name']}. "
                          + ("Berichten aan deze klant kunnen niet worden verstuurd." if c["wwft_stop"] else ""))
        CLIENTS.save(cl)
        return redirect(url_for("clients_page"))
    counts = {}
    for d in state["docs"].values():
        counts[d.get("client_nr")] = counts.get(d.get("client_nr"), 0) + 1
    return render_template("clients.html", nav="clients", clients=CLIENTS.load(), counts=counts, charts=charts())


@app.get("/klanten/scheidingsbladen.pdf")
def cover_sheets():
    cl = clients()
    nrs = request.args.getlist("nr") or list(cl)
    sel = [cl[n] for n in nrs if n in cl]
    if not sel:
        abort(404)
    return send_file(io.BytesIO(coversheet.pdf(sel, BRAND)), mimetype="application/pdf",
                     download_name="scheidingsbladen.pdf")


# ---------------------------------------------------------------- integrations
@app.route("/koppelingen", methods=["GET", "POST"])
@auth.admin_required
def integrations():
    s = settings()
    if request.method == "POST":
        section = request.form.get("section")
        if section == "general":
            s["manual_minutes"] = _num(request.form.get("manual_minutes"))
            s["watch_inbox"] = bool(request.form.get("watch_inbox"))
        elif section == "mijnkantoor":
            s["mk_route"] = request.form.get("mk_route", "zip")
            s["mk_sync_dir"] = request.form.get("mk_sync_dir", "").strip()
            for k in exports.DEFAULT_FOLDERS:
                v = request.form.get(f"folder_{k}", "").strip().strip("/")
                if v:
                    s["folders"][k] = v
            if s["mk_route"] == "folder" and s["mk_sync_dir"] and not Path(s["mk_sync_dir"]).is_dir():
                flash("Let op: deze map bestaat niet op de server.", "error")
        elif section == "exact":
            s["journal_purchase"] = request.form.get("journal_purchase", "").strip() or "60"
            s["journal_sales"] = request.form.get("journal_sales", "").strip() or "70"
            s["vat_map"] = {k: request.form.get(f"vat_{k}", "").strip() for k in ai.VAT_CODES
                            if request.form.get(f"vat_{k}", "").strip()}
        SETTINGS.save(s)
        audit("-", "-", "settings_changed", who(), section=section)
        flash("Instellingen opgeslagen.")
        return redirect(url_for("integrations") + f"#{section}")
    return render_template("integrations.html", nav="integrations", s=s, inbox=str(INBOX), exact_tok=exact.token(),
                           exact_ready=exact_configured(), redirect_uri=exact_redirect(), folders=exports.DEFAULT_FOLDERS,
                           integ=integrations_status(), live_key=ai.live())


def exact_redirect():
    return os.environ.get("EXACT_REDIRECT_URI") or url_for("exact_callback", _external=True)


@app.get("/koppelingen/exact/verbinden")
@auth.admin_required
def exact_connect():
    if not exact_configured():
        abort(400, "EXACT_CLIENT_ID en EXACT_CLIENT_SECRET ontbreken in .env")
    session["exact_state"] = secrets.token_urlsafe(16)
    return redirect(exact_auth_url(exact_redirect(), session["exact_state"]))


@app.get("/koppelingen/exact/callback")
@auth.admin_required
def exact_callback():
    if request.args.get("state") != session.pop("exact_state", None) or not request.args.get("code"):
        abort(400, "Koppeling met Exact afgebroken. Probeer opnieuw.")
    try:
        tok = exact.connect(request.args["code"], exact_redirect())
        audit("-", "-", "exact_connected", who(), division=tok.get("division"))
        flash(f"Exact Online gekoppeld (administratie {tok.get('division')}).")
    except ExactError as e:
        flash(str(e), "error")
    return redirect(url_for("integrations") + "#exact")


@app.post("/koppelingen/exact/ontkoppelen")
@auth.admin_required
def exact_disconnect():
    exact.disconnect()
    audit("-", "-", "exact_disconnected", who())
    flash("Exact Online ontkoppeld.")
    return redirect(url_for("integrations") + "#exact")


# ---------------------------------------------------------------- outgoing messages (Wwft: staff approval only)
@app.get("/berichten")
def messages_page():
    data = outbox.all()["messages"]
    ms = sorted(data.values(), key=lambda m: m["id"], reverse=True)
    return render_template("messages.html", nav="messages", messages=ms, clients=clients(),
                           channels=outbox_mod.CHANNELS, confirm_read=outbox_mod.CONFIRM_READ,
                           confirm_wwft=outbox_mod.CONFIRM_WWFT, docs=state["docs"])


@app.post("/berichten/<mid>")
def message_action(mid):
    action = request.form.get("action")
    m = outbox.all()["messages"].get(mid) or abort(404)
    try:
        if action == "save":
            outbox.edit(mid, request.form.get("to", ""), request.form.get("subject", ""), request.form.get("body", ""), who())
            flash("Concept opgeslagen. Nog niet verstuurd.")
        elif action == "discard":
            outbox.discard(mid, who())
            flash("Concept verwijderd.")
        elif action == "send":
            outbox.edit(mid, request.form.get("to", ""), request.form.get("subject", ""), request.form.get("body", ""), who())
            stop = bool((clients().get(m.get("client_nr")) or {}).get("wwft_stop"))
            sent = outbox.approve_and_send(mid, auth.current_user(), bool(request.form.get("confirm_read")),
                                           bool(request.form.get("confirm_wwft")), stop)
            flash("Goedgekeurd en verstuurd." if sent["status"] == "sent" else
                  f"Goedgekeurd. {sent['sent']['route']}.")
    except outbox_mod.OutboxError as e:
        flash(str(e), "error")
    return redirect(url_for("messages_page") + f"#{mid}")


# ---------------------------------------------------------------- privacy and security status
@app.get("/beveiliging")
def security_page():
    egress_log = vault.read_lines(EGRESS_LOG)
    docs = list(state["docs"].values())
    routes = {}
    for d in docs:
        r = (d.get("trail") or {}).get("route", "onbekend")
        routes[r] = routes.get(r, 0) + 1
    return render_template("security.html", nav="security", mode=ai.MODE, label=ai.label(), models=models.status(),
                           egress=list(reversed(egress_log))[:50],
                           sent=sum(1 for e in egress_log if e["result"] == "sent"),
                           blocked=sum(1 for e in egress_log if e["result"] == "blocked"),
                           routes=routes, region=region.status(),
                           purged=sum(1 for d in docs if d.get("purged")),
                           waiting=sum(1 for d in docs if d["status"] == "approved" and not d.get("purged")),
                           s=settings(), interval=RETENTION_INTERVAL, grace=REJECTED_GRACE_HOURS,
                           users=users.all(), approval=outbox_mod.REQUIRES_STAFF_APPROVAL, require_2fa=auth.REQUIRE_2FA)


@app.post("/beveiliging/bewaren")
@auth.admin_required
def retention_settings():
    s = settings()
    s["retention_required"] = [k for k in ("mijnkantoor", "exact") if request.form.get(k)] or ["mijnkantoor"]
    SETTINGS.save(s)
    audit("-", "-", "settings_changed", who(), section="bewaren", required=s["retention_required"])
    flash("Opgeslagen.")
    return redirect(url_for("security_page") + "#bewaren")


# ---------------------------------------------------------------- WhatsApp intake (inbound only)
@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    """Clients send photos or PDFs to the firm's WhatsApp Business number. Nothing is ever replied automatically."""
    if request.method == "GET":
        challenge = whatsapp.verify(request.args)
        return (challenge, 200) if challenge is not None else abort(403)
    raw = request.get_data()
    if not whatsapp.signature_ok(raw, request.headers.get("X-Hub-Signature-256", "")):
        abort(403)
    for item in whatsapp.parse(json.loads(raw or b"{}")):
        if item["type"] in ("image", "document") and item.get("media_id"):
            try:
                data, mime = whatsapp.download(item["media_id"])
            except Exception:  # noqa: BLE001
                audit("-", "-", "whatsapp_failed", "WhatsApp", sender=whatsapp.mask(item["from"]))
                continue
            name = item.get("filename") or ("whatsapp" + whatsapp.EXT.get(mime or item.get("mime", ""), ".jpg"))
            if Path(name).suffix.lower() in ALLOWED:
                create_batch([(name, data)], "whatsapp", f"WhatsApp {whatsapp.mask(item['from'])}")
    return "ok"


@app.get("/auditlog")
def audit_page():
    entries = list(reversed(read_audit()))
    return render_template("audit.html", nav="audit", entries=entries[:500], total=len(entries), LABELS=EVENT_LABEL)


EVENT_LABEL = {"received": "Scan ontvangen", "processed": "Scan verwerkt", "failed": "Verwerking mislukt",
               "read": "Document gelezen", "corrected": "Gecorrigeerd", "approved": "Goedgekeurd",
               "asked_client": "Vraag aan klant", "rejected": "Uit stapel gehaald", "reopened": "Heropend",
               "regrouped": "Indeling aangepast", "exported": "Doorgezet", "export_failed": "Doorzetten mislukt",
               "login": "Ingelogd", "logout": "Uitgelogd", "login_failed": "Inlog mislukt",
               "password_changed": "Wachtwoord gewijzigd", "password_reset": "Wachtwoord gereset",
               "user_added": "Gebruiker toegevoegd", "user_enabled": "Gebruiker geactiveerd",
               "user_disabled": "Gebruiker geblokkeerd", "client_added": "Klant toegevoegd",
               "settings_changed": "Instellingen gewijzigd", "exact_connected": "Exact gekoppeld",
               "exact_disconnected": "Exact ontkoppeld", "2fa_enrolled": "Tweestaps ingesteld",
               "2fa_reset": "Tweestaps gereset", "egress_sent": "Tekst naar Claude (geanonimiseerd)",
               "egress_blocked": "Verzending naar Claude geblokkeerd", "purged": "Origineel gewist",
               "batch_purged": "Scan gewist", "export_confirmed": "Import bevestigd",
               "message_drafted": "Conceptbericht", "message_edited": "Bericht gewijzigd",
               "message_discarded": "Bericht verwijderd", "message_approved": "Bericht goedgekeurd",
               "message_sent": "Bericht verstuurd", "message_not_sent": "Goedgekeurd, niet verstuurd (demo)",
               "wwft_stop_on": "Wwft-stop aan", "wwft_stop_off": "Wwft-stop uit", "whatsapp_failed": "WhatsApp-bestand mislukt"}


# ---------------------------------------------------------------- start
def recover():
    """A restart during processing leaves batches half done: mark them so the user can rescan."""
    for b in state["batches"].values():
        if b["status"] == "processing":
            b["status"], b["error"] = "error", "Onderbroken door een herstart. Scan opnieuw aanleveren."
    for d in state["docs"].values():
        if d["status"] == "reading":
            d["status"] = "review" if d.get("result") else "rejected"
    save_state()


_started = False


def start_background():
    global _started
    if _started:
        return
    _started = True
    recover()
    threading.Thread(target=watch_inbox, daemon=True).start()
    threading.Thread(target=retention_loop, daemon=True).start()
    seed()


start_background()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)), threaded=True)
