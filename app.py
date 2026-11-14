"""Scanstraat (privacy version): from a scanned pile of paper to a checked MijnKantoor dossier, Exact booking
and Excel register, built privacy by design (AVG) and with a human gate on every outgoing message (Wwft).

Flow per scan:
  1. pages       the scanner's PDF (or photos) becomes page images          scan.py, no AI
  2. sort        blank back sides removed, cover sheets split per client     scan.py, no AI (QR code)
  3. split       which pages form one document                                ai.py: local model, or hybrid
  4. read        fields + MijnKantoor folder + booking proposal                ai.py: local model, or hybrid
  5. check       arithmetic, VAT, IBAN, duplicates, deadlines                 checks.py, no AI
  6. review      a person approves, corrects, asks the client or discards
  7. deliver     MijnKantoor folder/ZIP, Exact API or import file, Excel,     exports.py, exact_api.py,
                 UBL 2.1 (NLCIUS) for SnelStart, Yuki and other packages      ubl.py
Nothing leaves step 6 without a person's approval.

Privacy controls (see README): local-first AI with an Apache 2.0 licence gate (models.py); in hybrid mode only
pseudonymised text goes to Claude through one checked door (egress.py), never a page image; everything stored is
encrypted (vault.py); originals and pseudonymisation mappings are deleted once the export is confirmed
(retention below); messages to clients are drafts until a staff member approves them (outbox.py); 2FA (auth.py);
EU-only production (region.py).

Demo instances (DEMO_INSTANCE=1, never production) may also run SCAN_AI=demo, where page images of fictional
documents go to Claude in an EU region, and may offer a read-only guest tour (GUEST_TOUR=1, auth.py) that only
shows the fictional sample scan.
"""
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from flask import (Flask, Response, abort, flash, has_request_context, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename

import envfile  # noqa: F401  (loads .env before the modules below read their settings)
import region
import vault
import ai
import auth
import bank
import checks
import coversheet
import egress
import exports
import models
import outbox as outbox_mod
import privacy
import ratelimit
import scan
import trust
import ubl
import whatsapp
from exact_api import Exact, ExactError, auth_url as exact_auth_url, configured as exact_configured

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
DATA = Path(os.environ.get("SCAN_DATA", HERE / "data"))
DATA.mkdir(parents=True, exist_ok=True)
region.enforce()
if os.environ.get("DEMO_INSTANCE") == "1" and region.production():
    raise SystemExit("DEMO_INSTANCE=1 kan niet samen met APP_ENV=production: een productie-omgeving is nooit een demo.")
vault.init(DATA)
BATCHES = DATA / "batches"
MAPS = DATA / "vault" / "maps"
EGRESS_LOG = DATA / "egress.log"
RETENTION_INTERVAL = int(os.environ.get("RETENTION_INTERVAL", "300"))
REJECTED_GRACE_HOURS = float(os.environ.get("REJECTED_GRACE_HOURS", "24"))
INBOX = Path(os.environ.get("SCAN_INBOX", DATA / "scanner-inbox"))
AUDIT = DATA / "audit.jsonl"
BRAND = os.environ.get("BRAND_NAME", "Scanstraat")
DEMO = os.environ.get("DEMO_INSTANCE") == "1"
DEMO_FIRM = "Maaskade Administraties"   # the fictional firm of the sample scan and the tour
BOOT = datetime.now()
ASSET_V = (os.environ.get("RENDER_GIT_COMMIT") or BOOT.strftime("%m%d%H%M%S"))[:10]   # busts caches after a deploy
ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp"}
UBL_KINDS = {"inkoopfactuur", "kassabon", "creditnota"}  # purchase side: what a package imports as UBL
TOUR_CONTACT = os.environ.get("TOUR_CONTACT", "").strip()

app = Flask(__name__)


def _secret():
    f = DATA / ".secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        os.chmod(f, 0o600)
    return f.read_text()


app.secret_key = os.environ.get("SECRET_KEY") or _secret()
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
mimetypes.add_type("text/javascript", ".mjs")   # pdf.js 4 is an ES module: browsers refuse it without a JS type

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
    "retention_required": ["mijnkantoor", "exact"], "firm_name": ""})
BANK = JsonFile(DATA / "bank.json", {})
state = STATE.load()


def save_state():
    with lock:
        STATE.save(state)


def settings():
    s = SETTINGS.load()
    for k, v in SETTINGS.default.items():
        s.setdefault(k, v)
    return s


def firm_name():
    """The firm this instance works for: on cover sheets and under messages to clients (white-label)."""
    return (settings().get("firm_name") or os.environ.get("FIRM_NAME") or (DEMO_FIRM if DEMO else BRAND)).strip()


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
        if (SAMPLES / "bank.json").exists():   # the fictional clients come with fictional bank lines
            BANK.save(json.loads((SAMPLES / "bank.json").read_text(encoding="utf-8")))
    if not SETTINGS.path.exists():
        SETTINGS.save(SETTINGS.load())
    INBOX.mkdir(parents=True, exist_ok=True)


first_start()
users = auth.Users(DATA / "users.json")

# Printed QR codes can't change: letters point at the Render address, and once the own domain works,
# CANONICAL_HOST (e.g. descanstraat.nl) sends every visitor there. 302, so a domain mistake is never cached.
CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "").strip().lower()


@app.before_request
def to_canonical_host():
    if CANONICAL_HOST and request.host.split(":")[0].lower() != CANONICAL_HOST and \
            request.endpoint not in ("health", "whatsapp_webhook", "retention_trigger"):
        return redirect(f"https://{CANONICAL_HOST}{request.full_path.rstrip('?')}", 302)
    return None


ratelimit.install(app)   # before auth: limits and the Origin check run first
auth.init(app, users, audit)
exact = Exact(DATA / "exact-token.json")
outbox = outbox_mod.Outbox(DATA / "outbox.json", audit)


def who():
    return (auth.current_user() or {}).get("name", "Systeem")


def guest():
    return has_request_context() and (auth.current_user() or {}).get("role") == "guest"


def visible_batches():
    """Guests on the tour see only the fictional sample scan, never anything a staff member uploaded."""
    batches = list(state["batches"].values())
    return [b for b in batches if b.get("source") == "voorbeeld"] if guest() else batches


def visible_docs():
    docs = list(state["docs"].values())
    if not guest():
        return docs
    ok = {b["id"] for b in visible_batches()}
    return [d for d in docs if d["batch"] in ok]


def guest_may_see(batch_id):
    if guest() and (state["batches"].get(batch_id) or {}).get("source") != "voorbeeld":
        abort(404)


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
    return [d for d in visible_docs() if d["status"] == "review"]


@app.context_processor
def globals_():
    return {"brand": BRAND, "user": auth.current_user(), "live": ai.live(), "ai_label": ai.label(), "local_ai": ai.local(), "open_count": len(open_docs()),
            "draft_count": len(outbox.drafts()) if auth.current_user() and not guest() else 0,
            "KINDS": ai.KINDS, "VAT_CODES": ai.VAT_CODES, "feat": {}, "guest": guest(), "demo_live": ai.demo(),
            "destination": egress.DESTINATION, "tour_contact": TOUR_CONTACT, "UBL_KINDS": UBL_KINDS,
            "tour_on": auth.GUEST_TOUR, "demo_instance": DEMO, "tour_firm": DEMO_FIRM, "asset_v": ASSET_V}


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def err(e):
    msgs = {400: getattr(e, "description", "Ongeldig verzoek"), 403: "Geen toegang", 404: "Niet gevonden",
            413: "Bestand te groot (max 200 MB)"}
    custom = getattr(e, "description", None)
    if e.code == 403 and custom and custom != getattr(type(e), "description", None):
        msgs[403] = custom  # e.g. the guest tour's "alleen bekijken" message
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


SAMPLE_SCAN = "schoenendoos_2026-11-12.pdf"
_manifests = None


def manifests():
    """Answer keys of the fictional sample scans, by sha256 of the scan file. A reply photo of the demo (the
    client's WhatsApp answer) is a one-document answer key of its own."""
    global _manifests
    if _manifests is None:
        found = {}
        for f in sorted(SAMPLES.glob("*.json")):
            try:
                m = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if not isinstance(m, dict) or not m.get("sha256") or not m.get("documents"):
                continue
            found[m["sha256"]] = m
            for name, r in (m.get("replies") or {}).items():
                if r.get("sha256"):
                    found[r["sha256"]] = {"file": r["image"], "sha256": r["sha256"], "as_of": m.get("as_of"),
                                          "documents": [{"key": f"R-{name}", "client": r.get("client"), "pages": [1],
                                                         "answer": r["answer"], "boxes": r.get("boxes") or {}}]}
        _manifests = found
    return _manifests


def sample_manifest(name="schoenendoos.json"):
    f = SAMPLES / name
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def manifest_specs(manifest, page_set):
    """The documents of an answer key whose first page is in page_set, as the pile is first offered: a staged
    merge (two receipts as one document, see make_demo.py) comes out as one document that a reviewer splits."""
    staged = {k: s for s in manifest.get("staged") or [] for k in s["keys"]}
    out, used = [], set()
    for d in manifest["documents"]:
        key = d.get("key")
        if d["pages"][0] not in page_set or key in used:
            continue
        st = staged.get(key)
        if st:
            parts = [x for x in manifest["documents"] if x.get("key") in st["keys"]]
            used.update(st["keys"])
            out.append({"key": "+".join(st["keys"]), "pages": [p for x in parts for p in x["pages"]],
                        "answer": st["answer"], "boxes": parts[0].get("boxes") or {}})
        else:
            out.append({"key": key, "pages": d["pages"], "answer": d["answer"], "boxes": d.get("boxes") or {}})
    return out


def manifest_answer(manifest, pages):
    """The answer for exactly these pages (after a reviewer split or merged documents), or None."""
    for d in manifest["documents"]:
        if d["pages"] == pages:
            return {"key": d.get("key"), "answer": d["answer"], "boxes": d.get("boxes") or {}}
    for spec in manifest_specs(manifest, {p for d in manifest["documents"] for p in d["pages"]}):
        if spec["pages"] == pages:
            return spec
    return None


def create_batch(files, source, actor, replay=False, fast=False, client_nr=None):
    """files: list of (filename, bytes). Starts processing in the background.
    fast: no pause between the replayed answers (the first-start seed). client_nr: the sender is known (the client's
    WhatsApp number or personal upload link), so pages without a cover sheet go to that dossier."""
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
             "usage": {"input_tokens": 0, "output_tokens": 0}, "fast": bool(fast), "client_nr": client_nr}
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
                known = batch.get("client_nr") if batch.get("client_nr") in cl else None
                cur = {"client_nr": known, "cover": None, "pages": [], "via": batch["source"] if known else None}
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
    manifest = manifests().get(source_hashes[0]) if len(sources) == 1 and not use_ai else None
    if manifest:
        _set(batch, manifest=manifest["sha256"], as_of=manifest.get("as_of"))
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
                                  "group_client": g["client_nr"], "via": g.get("via")})
        elif manifest:
            if not batch.get("fast"):
                ai.replay_delay()
            for d in manifest_specs(manifest, set(g["pages"])):
                specs.append({"pages": d["pages"], "kind": d["answer"]["kind"], "answer": d["answer"],
                              "client_hint": d["answer"].get("client_hint"), "boxes": d["boxes"], "key": d["key"],
                              "reason": d["answer"].get("split_reason") or "Zelfde afzender en kenmerk",
                              "group_client": g["client_nr"], "via": g.get("via")})
        else:
            for p in g["pages"]:
                specs.append({"pages": [p], "kind": "overig", "answer": ai.empty_result(), "client_hint": None,
                              "reason": "Replay-modus: elke pagina apart", "group_client": g["client_nr"],
                              "via": g.get("via")})

    # 4: read each document
    _set(batch, step=3, detail=f"0 van {len(specs)} documenten")
    doc_ids = []
    for spec in specs:
        did = new_id("D")
        client_nr = spec["group_client"] or match_client(spec.get("client_hint"), cl)
        via = {"whatsapp": "WhatsApp-nummer van de klant", "klantlink": "aanleverlink van de klant"}.get(spec.get("via"))
        doc = {"id": did, "batch": bid, "pages": spec["pages"], "client_nr": client_nr,
               "client_via": via or ("scheidingsblad" if spec["group_client"] else ("herkend op document" if client_nr else None)),
               "kind": spec["kind"], "split_reason": spec.get("reason", ""), "result": None, "status": "reading",
               "flags": [], "corrections": [], "exports": {}, "created": now(), "seconds": None, "filing": None,
               "boxes": spec.get("boxes") or {}, "key": spec.get("key"), "flag_notes": {}}
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
            if not batch.get("fast"):
                ai.replay_delay()
            result = json.loads(json.dumps(spec["answer"]))
            result.pop("split_reason", None)
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

    with ThreadPoolExecutor(max_workers=1 if ai.local() or ai.hybrid() else 3) as pool:
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
    f["no_vat_codes"] = [l.get("vat_code") for l in r.get("lines") or [] if l.get("vat_code")]
    return f


def check_fields(doc):
    """as_check_fields for the checks: a sales invoice is compared with other sales invoices only."""
    f = as_check_fields(doc)
    if doc["kind"] == "verkoopfactuur":
        f["supplier_name"] = f"verkoop {(doc['result'] or {}).get('counterparty')}"
    return f


def compute_flags(doc, earlier, history, today=None, chart=None):
    """chart: the client's chart of accounts (rows of account, name[, RGS]); default: looked up for the client."""
    D = checks.D
    r = doc["result"] or {}
    flags = []
    today = today or date.today()
    if chart is None:
        chart = chart_for(clients().get(doc.get("client_nr")))
    if not doc.get("client_nr"):
        flags.append(checks.flag("error", "client", "Klant onbekend: geen scheidingsblad en niet herkend op het document. Kies de klant."))
    if doc["kind"] in ai.BOOKABLE:
        f = check_fields(doc)
        flags += checks.run_checks(f, others=[check_fields(o) for o in earlier], history=history, today=today)
        lines = r.get("lines") or []
        if not lines:
            flags.append(checks.flag("error", "lines", "Geen boekingsregels. Vul grootboek en btw-code in."))
        else:
            accounts = {row[0] for row in chart}
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
        if due and due >= today and due <= today + timedelta(days=45) and \
                not any("termijn" in (x.get("message") or "").lower() for x in flags):
            flags.append(checks.flag("warning", "due_date", f"Termijn loopt af op {due.strftime('%d-%m-%Y')}."))
    if len(doc["pages"]) > 1:
        flags.append(checks.flag("info", None, f"{len(doc['pages'])} pagina's samengevoegd tot één document: {doc.get('split_reason') or 'zelfde document'}."))
    for x in flags:
        x["id"] = checks.flag_id(x)
    return flags


def insight(doc, earlier, history, chart):
    """What the reviewer sees next to the flags: the checks that came out right, and how the booking proposal
    compares with this supplier's earlier bookings. Both are counted by plain code."""
    if doc["kind"] not in ai.BOOKABLE:
        return [], None
    r = doc["result"] or {}
    names = {row[0]: row[1] for row in chart}
    found = checks.badges(check_fields(doc), doc["flags"], [check_fields(o) for o in earlier], history)
    proof = None if doc["kind"] == "verkoopfactuur" else checks.evidence(r.get("lines"), r.get("counterparty"), history, names)
    return found, proof


def handled(doc):
    """True when every error flag on the document carries a reviewer's note (what was checked, by whom)."""
    notes = doc.get("flag_notes") or {}
    return all(f.get("id") in notes for f in doc.get("flags") or [] if f["level"] == "error")


def recheck_batch(bid):
    with lock:
        batch = state["batches"][bid]
        docs = [state["docs"][d] for d in batch["docs"]]
        today = checks.parse_date(batch.get("as_of")) or date.today()   # a sample scan is checked as of its own day
        cl = clients()
        for i, doc in enumerate(docs):
            if doc["result"] is None or doc["status"] == "rejected":
                continue
            earlier = [o for o in docs[:i] if o["result"] and o["status"] != "rejected" and o.get("client_nr") == doc.get("client_nr")]
            history = ledger_history(doc.get("client_nr")) + [
                as_check_fields(o) | {"supplier_name": o["result"].get("counterparty"),
                                      "account": "+".join(sorted({l["account"] for l in o["result"].get("lines") or []}))}
                for o in state["docs"].values()
                if o["batch"] != bid and o["status"] == "approved" and o.get("client_nr") == doc.get("client_nr")]
            chart = chart_for(cl.get(doc.get("client_nr")))
            doc["flags"] = compute_flags(doc, earlier, history, today, chart)
            doc["light"] = checks.status_of(doc["flags"])
            doc["badges"], doc["evidence"] = insight(doc, earlier, history, chart)
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
    stack = SAMPLES / os.environ.get("SCAN_SEED_SAMPLE", SAMPLE_SCAN)
    if not state["batches"] and stack.exists() and os.environ.get("SCAN_SEED", "1") == "1":
        # The first-start sample replays the answer key (labelled replay): fast, and nothing is sent anywhere.
        create_batch([(stack.name, stack.read_bytes())], "voorbeeld", "Systeem", replay=True, fast=True)


# ---------------------------------------------------------------- pages
def kpis():
    docs = visible_docs()
    batches = visible_batches()
    today = date.today().isoformat()
    week = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    pages = [p for b in batches for p in b["pages"]]
    b_today = [b for b in batches if b["created"][:10] >= week]
    s = settings()
    approved = [d for d in docs if d["status"] == "approved"]
    secs = [b["seconds"] / max(1, len(b["docs"])) for b in batches if b.get("seconds") and b["docs"]]
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


def public_ctx():
    """What every public page of a demo instance shows: contact details, the tour link with the letter's code."""
    k = re.sub(r"[^a-z0-9-]", "", (request.args.get("k") or "").lower())[:60]
    phone_intl = re.sub(r"\D", "", os.environ.get("CONTACT_WHATSAPP", ""))
    wa_text = quote("Hallo, ik wil graag een demo van Scanstraat inplannen.")
    return dict(touring=guest(), k=k,
                tour_url=url_for("guest_tour", k=k) if k else url_for("guest_tour"),
                wa_url=f"https://wa.me/{phone_intl}?text={wa_text}" if phone_intl else "",
                contact_name=os.environ.get("CONTACT_NAME", ""), contact_phone=os.environ.get("CONTACT_PHONE", ""),
                contact_email=os.environ.get("CONTACT_EMAIL", ""), contact_place=os.environ.get("CONTACT_PLACE", ""),
                kvk=os.environ.get("KVK_NUMBER", ""), price_from=os.environ.get("PRICE_FROM", "295"),
                saving_bar=os.environ.get("SAVING_BAR", "40%"), toms_pdf=TOMS_PDF.is_file())


TOMS_PDF = HERE / "static" / "docs" / "technische-beschrijving-veiligheid.pdf"

# The public pages load only their own scripts (no inline code, no third parties): a strict policy that anyone can check,
# for example with securityheaders.com. The staff screens still have small inline scripts and keep the base headers.
PUBLIC_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
              "font-src 'self'; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; "
              "form-action 'self'; frame-ancestors 'self'")


def public_headers(html):
    resp = app.make_response(html)
    resp.headers["Content-Security-Policy"] = PUBLIC_CSP
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    return resp


@app.get("/welkom")
def public_home():
    """Public homepage of a demo instance (GUEST_TOUR=1): where the QR code on the letters lands. No client data here."""
    if not auth.GUEST_TOUR:
        return redirect(url_for("login"))
    return public_headers(render_template("home.html", hero=hero_data(), **public_ctx()))


@app.get("/veiligheid")
def public_security():
    """Public page: how the privacy step, the egress gate and the rule checks work, run live on fictional text,
    which certificates belong to which sub-processor, and the three-step proof without client data."""
    if not auth.GUEST_TOUR:
        return redirect(url_for("security_page"))
    return public_headers(render_template("veiligheid.html", samples=trust.samples(), code=trust.code_blocks(),
                                          ibans=trust.iban_demo(), ai_mode=ai.label(), where=hosting_label(), **public_ctx()))


@app.post("/api/veiligheid/filter")
def security_filter():
    """Stateless: the visitor's own test text through the real fixed rules and the egress gate. Nothing is stored or logged."""
    if not auth.GUEST_TOUR:
        abort(404)
    if (request.content_length or 0) > 64 * 1024:
        abort(413)
    body = request.get_json(silent=True) or {}
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    resp = jsonify(trust.live(text))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/veiligheid/technische-beschrijving.pdf")
def toms_pdf():
    if not TOMS_PDF.is_file():
        abort(404)
    return send_file(TOMS_PDF, mimetype="application/pdf", download_name="technische-beschrijving-veiligheid-scanstraat.pdf")


@app.get("/.well-known/security.txt")
def security_txt():
    """RFC 9116: where to report a vulnerability. Only on the public demo, and only when a contact address is set."""
    mail = os.environ.get("SECURITY_CONTACT") or os.environ.get("CONTACT_EMAIL", "")
    if not auth.GUEST_TOUR or not mail:
        abort(404)
    expires = (datetime.now(timezone.utc) + timedelta(days=180)).strftime("%Y-%m-%dT00:00:00Z")
    host = os.environ.get("CANONICAL_HOST") or request.host
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}(:\d{1,5})?", host):    # never echo a crafted Host header
        abort(404)
    body = (f"Contact: mailto:{mail}\nExpires: {expires}\nPreferred-Languages: nl, en\n"
            f"Canonical: https://{host}/.well-known/security.txt\nPolicy: https://{host}/veiligheid#melden\n")
    return Response(body, mimetype="text/plain")


@app.get("/")
def dashboard():
    cl = clients()
    per_client = []
    for nr, c in cl.items():
        ds = [d for d in visible_docs() if d.get("client_nr") == nr and d["status"] != "reading"]
        per_client.append({"client": c, "total": len(ds), "review": sum(d["status"] == "review" for d in ds),
                           "red": sum(d["status"] == "review" and d.get("light") == "red" for d in ds),
                           "asked": sum(d["status"] == "asked" for d in ds),
                           "approved": sum(d["status"] == "approved" for d in ds),
                           "delivered": sum(bool(d["exports"].get("mijnkantoor")) for d in ds)})
    unknown = [d for d in visible_docs() if not d.get("client_nr") and d["status"] == "review"]
    batches = sorted(visible_batches(), key=lambda b: b["created"], reverse=True)[:8]
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
            stack = SAMPLES / SAMPLE_SCAN
            bid = create_batch([(stack.name, stack.read_bytes())], "voorbeeld", who(), replay=True)
            return redirect(url_for("batch_page", bid=bid))
        files = [(f.filename, f.read()) for f in request.files.getlist("files")
                 if f and f.filename and Path(f.filename).suffix.lower() in ALLOWED]
        if not files:
            flash("Kies een of meer scans (pdf of foto).", "error")
            return redirect(url_for("upload"))
        bid = create_batch(files, "upload", who())
        return redirect(url_for("batch_page", bid=bid))
    return render_template("upload.html", nav="scan", inbox=str(INBOX), sample=(SAMPLES / SAMPLE_SCAN).exists())


def batch_view(bid):
    batch = state["batches"].get(bid) or abort(404)
    guest_may_see(bid)
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


def regroup(batch, did, action, after=None):
    """Merge a document with the previous one, or split it after a page. Re-reads the changed documents.
    Returns (ok, message)."""
    bid, ids = batch["id"], batch["docs"]
    if batch["status"] != "done":
        return False, "Wacht tot de scan klaar is."
    if did not in ids or action not in ("merge", "split"):
        return False, "Dit document staat niet in deze scan."
    i = ids.index(did)
    doc = state["docs"][did]
    if doc["status"] == "approved" or (action == "merge" and i > 0 and state["docs"][ids[i - 1]]["status"] == "approved"):
        return False, "Goedgekeurde documenten kun je niet meer samenvoegen of splitsen."
    changed = []
    with lock:
        if action == "merge":
            if i == 0:
                return False, "Dit is het eerste document van de scan."
            prev = state["docs"][ids[i - 1]]
            if prev.get("client_nr") != doc.get("client_nr"):
                return False, "Het vorige document hoort bij een andere klant."
            prev["pages"] += doc["pages"]
            prev["split_reason"] = f"handmatig samengevoegd door {who()}"
            ids.remove(did)
            doc["status"] = "rejected"
            doc["rejected"] = {"by": who(), "at": now(), "note": f"samengevoegd met {prev['id']}"}
            changed = [prev]
        else:
            try:
                after = int(after or 0)
            except ValueError:
                after = 0
            if after not in doc["pages"] or after == doc["pages"][-1]:
                return False, "Kies de pagina waarna gesplitst wordt."
            k = doc["pages"].index(after) + 1
            nid = new_id("D")
            new = json.loads(json.dumps(doc)) | {"id": nid, "pages": doc["pages"][k:], "created": now(), "flag_notes": {},
                                                 "split_reason": f"handmatig gesplitst door {who()}", "exports": {}}
            doc["pages"] = doc["pages"][:k]
            doc["split_reason"] = f"handmatig gesplitst door {who()}"
            state["docs"][nid] = new
            ids.insert(i + 1, nid)
            changed = [doc, new]
        for p in batch["pages"]:
            for c in changed:
                if p["n"] in c["pages"]:
                    p["doc"] = c["id"]
        for c in changed:
            c["status"] = "reading"
            c["flag_notes"] = {}
        save_state()
    for c in changed:
        scan.build_pdf(batch_dir(bid) / "pages", c["pages"], batch_dir(bid) / "docs" / f"{c['id']}.pdf")
    audit(bid, did, "regrouped", who(), action=action)
    manifest = manifests().get(batch.get("manifest")) if batch.get("mode") == "replay" else None
    if manifest and all(manifest_answer(manifest, c["pages"]) for c in changed):
        for c in changed:      # the answer for these pages is known: no need to wait for a reader
            reread(c["id"])
    else:
        for c in changed:
            threading.Thread(target=reread, args=(c["id"],), daemon=True).start()
    return True, "Indeling aangepast. De betrokken documenten worden opnieuw gelezen."


@app.post("/scan/<bid>/indeling")
def batch_regroup(bid):
    batch, _ = batch_view(bid)
    ok, message = regroup(batch, request.form.get("doc"), request.form.get("action"), request.form.get("after"))
    if not ok:
        abort(400, message)
    flash(message)
    return redirect(url_for("batch_page", bid=bid))


def reread(did):
    doc = state["docs"][did]
    cl = clients()
    client = cl.get(doc.get("client_nr"))
    batch = state["batches"][doc["batch"]]
    manifest = manifests().get(batch.get("manifest")) if batch.get("mode") == "replay" else None
    known = manifest_answer(manifest, doc["pages"]) if manifest else None
    if known:
        result = json.loads(json.dumps(known["answer"]))
        result.pop("split_reason", None)
        doc["boxes"], doc["key"] = known["boxes"], known["key"]
    elif ai.live():
        result = read_doc(doc, cl)
    else:
        result = doc["result"] or ai.empty_result()
        note = "Replay-modus: na splitsen of samenvoegen niet opnieuw gelezen. Controleer de velden."
        result = dict(result, issues=[x for x in result.get("issues") or [] if x != note] + [note])
        doc["boxes"] = {k: v for k, v in (doc.get("boxes") or {}).items() if v.get("page") in doc["pages"]}
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
    guest_may_see(doc["batch"])
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


def doc_action(doc, form):
    """One reviewer action on one document, from the cockpit (keyboard) or a posted form. Returns (ok, message).
    Actions: approve, ask (draft a question), send (draft, approve and send it), reject, save, reopen,
    flagnote / unnote (what the reviewer checked about a red flag: it goes into the audit trail)."""
    did, action, cl = doc["id"], form.get("action"), clients()
    if doc["status"] == "reading":
        return False, "Dit document wordt nog gelezen."
    if action == "reopen":
        if any(doc["exports"].values()) or doc.get("purged"):
            return False, "Al doorgezet naar MijnKantoor of Exact: heropenen kan niet meer."
        with lock:
            doc["status"] = "review"
            for k in ("approved", "rejected"):
                doc.pop(k, None)
        audit(doc["batch"], did, "reopened", who())
        recheck_batch(doc["batch"])
        return True, "Heropend."
    if action in ("flagnote", "unnote"):
        flag = next((f for f in doc["flags"] if f.get("id") == form.get("flag")), None)
        if not flag:
            return False, "Deze vlag bestaat niet meer. Ververs het document."
        notes = doc.setdefault("flag_notes", {})
        if action == "unnote":
            notes.pop(flag["id"], None)
            audit(doc["batch"], did, "flag_note_removed", who(), flag=flag["message"][:160])
        else:
            note = " ".join(form.get("note", "").split())[:500]
            if len(note) < 3:
                return False, "Schrijf in een paar woorden wat je hebt gecontroleerd."
            notes[flag["id"]] = {"note": note, "by": who(), "at": now(), "flag": flag["message"]}
            audit(doc["batch"], did, "flag_noted", who(), flag=flag["message"][:160], note=note)
        save_state()
        return True, "Notitie vastgelegd in het controlespoor."
    if action not in ("approve", "ask", "send", "reject", "save"):
        return False, "Onbekende actie."
    changes = apply_form(doc, form, cl)
    if changes:
        audit(doc["batch"], did, "corrected", who(), changes=changes)
    recheck_batch(doc["batch"])
    ids = {f.get("id") for f in doc["flags"]}
    doc["flag_notes"] = {k: v for k, v in (doc.get("flag_notes") or {}).items() if k in ids}
    open_flags = [f["message"] for f in doc["flags"] if f["level"] in ("error", "warning")]
    message = "Opgeslagen."
    if action == "approve":
        if not doc.get("client_nr"):
            return False, "Kies eerst de klant."
        if doc.get("light") == "red" and not handled(doc) and not form.get("ack"):
            return False, ("Er staat nog een rode vlag open. Leg vast wat je hebt gecontroleerd (R), "
                           "of vraag het de klant (W).")
        doc["status"] = "approved"
        doc["approved"] = {"by": who(), "at": now(), "open_flags": open_flags,
                           "notes": [v["note"] for v in (doc.get("flag_notes") or {}).values()]}
        audit(doc["batch"], did, "approved", who(), open_flags=open_flags, notes=doc["approved"]["notes"])
        message = "Goedgekeurd."
    elif action in ("ask", "send"):
        c = cl.get(doc.get("client_nr")) or {}
        note = form.get("note", "").strip() or "Graag een leesbare kopie of toelichting."
        channel = form.get("channel") if form.get("channel") in outbox_mod.CHANNELS else "email"
        to = form.get("to", "").strip() or (("+" + c["whatsapp"]) if channel == "whatsapp" and c.get("whatsapp") else c.get("email") or "")
        body = form.get("body", "").strip() or client_question(doc, c, note, channel)
        mid = outbox.draft(channel, to, f"Vraag over uw administratie ({c.get('name', 'klant')})", body,
                           doc.get("client_nr"), did, who(), "vraag bij controle")
        doc["status"] = "asked"
        doc["asked"] = {"by": who(), "at": now(), "note": note, "message": mid, "channel": channel}
        audit(doc["batch"], did, "asked_client", who(), note=note, message=mid)
        message = "Conceptbericht klaargezet onder Berichten. Er is nog niets verstuurd."
        if action == "send":
            message = approve_message(mid, form, c)
    elif action == "reject":
        note = form.get("note", "").strip() or "Niet voor de administratie"
        doc["status"] = "rejected"
        doc["rejected"] = {"by": who(), "at": now(), "note": note}
        audit(doc["batch"], did, "rejected", who(), note=note)
        message = "Uit de stapel gehaald."
        recheck_batch(doc["batch"])   # a later document is no longer its duplicate
    save_state()
    return True, message


def approve_message(mid, form, client):
    """A logged-in person approves this exact text with both statements; only then the outbox sends it."""
    try:
        sent = outbox.approve_and_send(mid, auth.current_user(), bool(form.get("confirm_read")),
                                       bool(form.get("confirm_wwft")), bool(client.get("wwft_stop")))
    except outbox_mod.OutboxError as e:
        return f"Concept klaargezet, nog niet verstuurd: {e}."
    return "Goedgekeurd en verstuurd." if sent["status"] == "sent" else f"Goedgekeurd. {sent['sent']['route']}."


@app.route("/document/<did>", methods=["GET", "POST"])
def review(did):
    doc = state["docs"].get(did) or abort(404)
    guest_may_see(doc["batch"])
    if request.method == "POST":
        action = request.form.get("action")
        ok, message = doc_action(doc, request.form)
        if not ok:
            flash(message, "error")
            return redirect(url_for("review", did=did))
        flash(message)
        if action in ("approve", "ask", "send", "reject"):
            nxt = next((d for d in sorted(open_docs(), key=lambda d: d["id"]) if d["id"] != did), None)
            return redirect(url_for("review", did=nxt["id"]) if nxt else url_for("dashboard"))
        return redirect(url_for("review", did=did))
    return render_template("cockpit.html", nav="review", sim=False, focus=did, doc=doc, scan=None)


@app.get("/werkbank")
def cockpit():
    """The review cockpit. Staff work on their own documents; a tour guest gets the simulation of the fictional
    sample (kept in the visitor's browser, nothing is stored here)."""
    return render_template("cockpit.html", nav="review", sim=guest(), focus=request.args.get("doc"), doc=None,
                           scan=request.args.get("scan"))


@app.get("/stapel")
def stack():
    """Batch splitter studio of the fictional sample (tour). Staff see their own scans under /scan/<id>."""
    if not guest():
        latest = sorted(visible_batches(), key=lambda b: b["created"], reverse=True)
        return redirect(url_for("batch_page", bid=latest[0]["id"]) if latest else url_for("upload"))
    return render_template("stack.html", nav="stack", sim=True)


def audit_text(x):
    """One line of plain Dutch for an audit entry's details (the cockpit's trail and the audit page)."""
    if x.get("changes"):
        return "Aangepast: " + ", ".join(x["changes"].keys())
    if x.get("flag"):
        return f"{x['flag']}" + (f" Notitie: {x['note']}" if x.get("note") else "")
    if x.get("format"):
        return x["format"]
    if x.get("open_flags") is not None and "notes" in x:
        return (f"{len(x['open_flags'])} open punt(en) gezien" if x["open_flags"] else "Geen open punten") + \
            (". Notities: " + " | ".join(x["notes"]) if x.get("notes") else "")
    if x.get("deleted") is not None:
        return f"{x['deleted']} bestanden gewist" + (f" ({x['why']})" if x.get("why") else "")
    if x.get("message"):
        return f"Bericht {x['message']}" + (f" · {x['route']}" if x.get("route") else "") + (f" · {x['note']}" if x.get("note") else "")
    if x.get("model"):
        return "Antwoordsleutel van de voorbeeldscan (replay, geen AI)" if x["model"] == "replay" else (x.get("route") or x["model"])
    if x.get("destination"):
        return str(x["destination"])
    if x.get("action"):
        return "Samengevoegd" if x["action"] == "merge" else "Gesplitst"
    return x.get("note") or x.get("error") or ""


def doc_trails(ids):
    """Audit entries per document id, oldest first: the trail the cockpit shows next to a booking."""
    out = {i: [] for i in ids}
    for e in read_audit():
        if e.get("doc") in out:
            out[e["doc"]].append({"ts": e["ts"], "event": e["event"], "label": EVENT_LABEL.get(e["event"], e["event"]),
                                  "actor": e["actor"], "text": audit_text(e.get("details") or {})})
    return out


def cockpit_doc(doc, trail=None):
    notes = doc.get("flag_notes") or {}
    return {"id": doc["id"], "batch": doc["batch"], "key": doc.get("key"), "pages": doc["pages"],
            "client_nr": doc.get("client_nr"), "client_via": doc.get("client_via"), "kind": doc["kind"],
            "status": doc["status"], "light": doc.get("light") or "green", "handled": handled(doc),
            "result": doc["result"], "filing": doc.get("filing"), "split_reason": doc.get("split_reason"),
            "flags": [dict(f, note=notes.get(f.get("id"))) for f in doc.get("flags") or []],
            "badges": doc.get("badges") or [], "evidence": doc.get("evidence"), "boxes": doc.get("boxes") or {},
            "approved": doc.get("approved"), "asked": doc.get("asked"), "rejected": doc.get("rejected"),
            "purged": bool(doc.get("purged")), "exported": sorted(k for k, v in (doc.get("exports") or {}).items() if v),
            "route": (doc.get("trail") or {}).get("route"), "seconds": doc.get("seconds"), "image": doc.get("image"),
            "corrections": doc.get("corrections") or [], "trail": trail or [], "created": doc.get("created")}


def client_public(c):
    return {"nr": c["nr"], "name": c["name"], "city": c.get("city"), "contact": c.get("contact"), "chart": c.get("chart", "standaard"),
            "legal_form": c.get("legal_form"), "activity": c.get("activity"), "wwft_stop": bool(c.get("wwft_stop")),
            "whatsapp": whatsapp.mask("+" + c["whatsapp"]) if c.get("whatsapp") else "", "email": c.get("email") or "",
            "kvk": c.get("kvk"), "vat": c.get("vat"), "upload": upload_url(c["nr"])}


def cockpit_payload(focus=None, scan_id=None):
    cl, ch = clients(), charts()
    busy = {b["id"] for b in visible_batches() if b["status"] == "processing"}   # a scan shows up when its checks are done
    reading = len(busy) + sum(1 for d in visible_docs() if d["status"] == "reading" and d["batch"] not in busy)
    docs = [d for d in visible_docs() if d.get("result") and d["status"] != "reading" and d["batch"] not in busy]
    if scan_id:
        docs = [d for d in docs if d["batch"] == scan_id]
    else:
        keep = {d["batch"] for d in docs if d["status"] in ("review", "asked")}
        if focus:
            keep.add(focus["batch"])
        if not keep and docs:
            keep = {max(docs, key=lambda d: d["created"])["batch"]}
        docs = [d for d in docs if d["batch"] in keep]
    order = {bid: i for i, bid in enumerate(state["batches"])}
    docs.sort(key=lambda d: (order.get(d["batch"], 0), d["pages"][0] if d["pages"] else 0))
    trails = doc_trails([d["id"] for d in docs])
    s = settings()
    return {"mode": "live", "firm": firm_name(), "user": who(), "as_of": date.today().isoformat(), "reading": reading,
            "kinds": ai.KINDS, "vat_codes": ai.VAT_CODES, "clients": {nr: client_public(c) for nr, c in cl.items()},
            "charts": ch, "docs": [cockpit_doc(d, trails.get(d["id"])) for d in docs], "alternates": [], "staged": [],
            "replies": demo_replies(), "bank": bank_view(cl), "img": "/pagina/{batch}/{n}.jpg",
            "confirm": {"read": outbox_mod.CONFIRM_READ, "wwft": outbox_mod.CONFIRM_WWFT},
            "manual_minutes": s.get("manual_minutes"), "demo": DEMO, "ai": ai.label(),
            "channels": {"whatsapp": whatsapp.configured(), "email": bool(os.environ.get("SMTP_HOST"))}}


@app.get("/api/cockpit")
def cockpit_api():
    return jsonify(cockpit_payload(state["docs"].get(request.args.get("doc") or ""), request.args.get("scan")))


def _signature(bid):
    return {d: (state["docs"][d]["status"], state["docs"][d].get("light"), len(state["docs"][d].get("flags") or []),
                bool(state["docs"][d].get("flag_notes"))) for d in state["batches"][bid]["docs"] if d in state["docs"]}


@app.post("/api/document/<did>")
def doc_api(did):
    """The cockpit's keyboard actions. Same rules as the form: see doc_action."""
    doc = state["docs"].get(did) or abort(404)
    before = _signature(doc["batch"])
    ok, message = doc_action(doc, request.form)
    after = _signature(doc["batch"])
    changed = [i for i in after if after[i] != before.get(i) and i != did]
    trails = doc_trails([did])
    return jsonify({"ok": ok, "message": message, "doc": cockpit_doc(doc, trails[did]),
                    "docs": [cockpit_doc(state["docs"][i]) for i in changed], "bank": bank_view()})


@app.post("/api/scan/<bid>/indeling")
def regroup_api(bid):
    """Split a document after a page (S) or merge it with the previous one (M), from the cockpit."""
    batch, _ = batch_view(bid)
    ok, message = regroup(batch, request.form.get("doc"), request.form.get("action"), request.form.get("after"))
    return jsonify({"ok": ok, "message": message})


def client_question(doc, client, note, channel="email"):
    """Draft text for a question to the client. Plain and factual: only the document and what is missing."""
    r = doc.get("result") or {}
    when = checks.parse_date(r.get("doc_date"))
    what = " ".join(x for x in [ai.KINDS.get(doc["kind"], "document").lower(), "van" if r.get("counterparty") else "",
                                r.get("counterparty") or "", r.get("reference") or "",
                                f"({when.strftime('%d-%m-%Y')})" if when else ""] if x)
    if channel == "whatsapp":
        first = (client.get("contact") or "").split(" ")[0]
        return f"Hoi {first or 'daar'}, we hebben een vraag over de {what}. {note} Groet, {firm_name()}"
    return (f"Beste {client.get('contact') or 'klant'},\n\nBij het verwerken van uw administratie hebben we een vraag over "
            f"het volgende stuk: {what}.\n\n{note}\n\nAlvast bedankt.\n\nMet vriendelijke groet,\n{who()}\n{firm_name()}")


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
    ds = [d for d in visible_docs() if d["status"] == "approved"]
    if pending_key:
        ds = [d for d in ds if not d["exports"].get(pending_key)]
    return sorted(ds, key=lambda d: d["id"])


def pdf_path(doc):
    return batch_dir(doc["batch"]) / "docs" / f"{doc['id']}.pdf"


def _selected(key):
    ids = request.values.getlist("ids") or [d["id"] for d in approved_docs(key)]
    return [state["docs"][i] for i in ids if i in state["docs"] and state["docs"][i]["status"] == "approved"]


def _exact_doc(doc, s=None):
    s = s or settings()
    return doc | {"exact_journal": s["journal_sales"] if doc["kind"] == "verkoopfactuur" else s["journal_purchase"],
                  "exact_vat": s.get("vat_map") or {}}


@app.get("/afhandelen")
def deliver():
    if guest():
        return render_template("deliver.html", nav="deliver", sim=True, docs=[], clients={}, s=settings(), pending={},
                               integ={}, exact_tok=None, exact_ready=False, bookable=[], sample_ubl=None)
    ds = approved_docs()
    return render_template("deliver.html", nav="deliver", sim=False, docs=ds, clients=clients(), s=settings(),
                           integ=integrations_status(), exact_tok=exact.token(), exact_ready=exact_configured(),
                           pending={k: len(approved_docs(k)) for k in ("mijnkantoor", "exact", "excel")}
                           | {"ubl": len([d for d in approved_docs("ubl") if d["kind"] in UBL_KINDS])},
                           bookable=[d for d in ds if d["kind"] in ai.BOOKABLE],
                           sample_ubl=next((d for d in sorted(visible_docs(), key=lambda d: d["id"])
                                            if d["kind"] in UBL_KINDS and ubl_allowed(d)), None))


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
    """A ZIP or import file is a download: only a person can tell us the import into MijnKantoor/Exact/the package
    that reads UBL (SnelStart, Yuki, ...) worked."""
    key = request.form.get("key")
    if key not in ("mijnkantoor", "exact", "ubl"):
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
    return [k for k in req if (k != "exact" or doc["kind"] in ai.BOOKABLE) and (k != "ubl" or doc["kind"] in UBL_KINDS)]


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


def ubl_allowed(doc):
    """A UBL file is importable, so it is only made from fields a person approved. The one exception is the
    fictional sample scan, which the guest tour may preview as a concept."""
    if doc["kind"] not in UBL_KINDS or doc.get("purged") or doc["status"] == "reading":
        return False
    return doc["status"] == "approved" or state["batches"][doc["batch"]].get("source") == "voorbeeld"


def ubl_xml(doc, cl, pdf_bytes, concept=False):
    c = cl.get(doc.get("client_nr")) or {"name": "Onbekende klant"}
    fields = as_check_fields(doc) | {"description": (doc["result"] or {}).get("description")}
    filename = (doc.get("filing") or {}).get("filename") or f"{doc['id']}.pdf"
    xml = ubl.build(fields, {"name": c["name"], "address": c.get("address") or c.get("city"), "vat": c.get("vat")},
                    pdf_bytes, filename)
    if concept:
        xml = xml.replace("Opgesteld uit het originele document (bijlage) en goedgekeurd door een medewerker.",
                          "CONCEPT uit de voorbeeldscan: nog niet goedgekeurd door een medewerker. Niet importeren.")
    return xml, filename.rsplit(".", 1)[0] + ".xml"


def ubl_for(doc, cl):
    return ubl_xml(doc, cl, vault.read(pdf_path(doc)), concept=doc["status"] != "approved")


@app.get("/document/<did>/ubl.xml")
def doc_ubl(did):
    doc = state["docs"].get(did) or abort(404)
    guest_may_see(doc["batch"])
    if not ubl_allowed(doc):
        abort(400, "UBL alleen voor goedgekeurde inkoopfacturen, bonnen en creditnota's.")
    xml, name = ubl_for(doc, clients())
    return send_file(io.BytesIO(xml.encode("utf-8")), mimetype="application/xml", as_attachment=True, download_name=name)


@app.post("/afhandelen/ubl")
def deliver_ubl():
    """UBL 2.1 (NLCIUS) per approved purchase document, PDF embedded: for SnelStart's UBL mailbox, Yuki, Twinfield
    and other packages that import UBL. One ZIP, one folder per client."""
    import zipfile
    docs = [d for d in _selected("ubl") if d["kind"] in UBL_KINDS and not d.get("purged")]
    if not docs:
        flash("Geen goedgekeurde inkoopfacturen, bonnen of creditnota's voor UBL.", "error")
        return redirect(url_for("deliver"))
    cl = clients()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in docs:
            xml, name = ubl_for(d, cl)
            folder = secure_filename((cl.get(d.get("client_nr")) or {}).get("name", "onbekend")) or "onbekend"
            z.writestr(f"{folder}/{name}", xml)
    _mark(docs, "ubl", {"route": "zip", "confirmed": False})  # a download: staff confirm the import
    for d in docs:
        audit(d["batch"], d["id"], "exported", who(), format="UBL")
    return send_file(io.BytesIO(buf.getvalue()), mimetype="application/zip", as_attachment=True,
                     download_name=f"ubl-{datetime.now():%Y%m%d-%H%M}.zip")


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
    if guest():
        sr = showroom()
        for d in sr["docs"]:
            counts[d["client_nr"]] = counts.get(d["client_nr"], 0) + 1
        return render_template("clients.html", nav="clients", clients=list(sr["clients"].values()), counts=counts,
                               charts=sr["charts"], firm=sr["firm"], bank={k: len(v) for k, v in sr["bank_lines"].items()},
                               upload={nr: upload_url(nr) for nr in sr["clients"]})
    for d in state["docs"].values():
        counts[d.get("client_nr")] = counts.get(d.get("client_nr"), 0) + 1
    return render_template("clients.html", nav="clients", clients=CLIENTS.load(), counts=counts, charts=charts(),
                           firm=firm_name(), bank={k: len(v) for k, v in BANK.load().items()},
                           upload={c["nr"]: upload_url(c["nr"]) for c in CLIENTS.load()})


@app.post("/klanten/bank")
@auth.admin_required
def bank_import():
    """Bank lines of one client from a CAMT.053 file (the bank's own export). They are only used to list payments
    that have no document yet."""
    nr = re.sub(r"\D", "", request.form.get("nr", ""))
    f = request.files.get("camt")
    if nr not in clients() or not f:
        flash("Kies een klant en een CAMT.053-bestand.", "error")
        return redirect(url_for("clients_page"))
    try:
        lines = bank.parse_camt(f.read(), prefix=f"B{nr}")
    except Exception:  # noqa: BLE001 - not a readable CAMT file
        lines = []
    if not lines:
        flash("Geen boekingen gevonden. Is dit een CAMT.053-bestand (xml) van de bank?", "error")
        return redirect(url_for("clients_page"))
    data = BANK.load()
    data[nr] = lines
    BANK.save(data)
    audit("-", "-", "bank_imported", who(), client=nr, lines=len(lines))
    flash(f"{len(lines)} bankregels ingelezen. De betalingen zonder document staan in de werkbank onder Ontbrekende bonnen.")
    return redirect(url_for("clients_page"))


def _sheet_request():
    """The dossiers and the firm name for a cover sheet request. White-label: the firm's own name is on the sheet;
    a tour guest may type a name to see their own firm on it (fictional dossiers only)."""
    cl = showroom()["clients"] if guest() else clients()
    if guest():
        cl = {nr: c for nr, c in cl.items() if nr in {p["ref"] for p in showroom()["pages"]}}
    nrs = request.args.getlist("nr") or list(cl)
    sel = [cl[n] for n in nrs if n in cl]
    if not sel:
        abort(404)
    typed = re.sub(r"[^\w .,&'()+-]", "", request.args.get("kantoor", ""), flags=re.UNICODE).strip()[:48]
    return sel, typed or (showroom()["firm"] if guest() else firm_name())


@app.get("/klanten/scheidingsbladen.pdf")
def cover_sheets():
    sel, firm = _sheet_request()
    return send_file(io.BytesIO(coversheet.pdf(sel, BRAND, firm)), mimetype="application/pdf",
                     download_name="scheidingsbladen.pdf")


@app.get("/klanten/scheidingsblad.jpg")
def cover_sheet_preview():
    """The first requested cover sheet as an image: the preview next to the print button, from the same code as the PDF."""
    import pypdfium2 as pdfium
    sel, firm = _sheet_request()
    with _pdf_lock:
        page = pdfium.PdfDocument(coversheet.pdf(sel[:1], BRAND, firm))[0]
        buf = io.BytesIO()
        page.render(scale=1.25).to_pil().convert("RGB").save(buf, "JPEG", quality=84)
    resp = send_file(io.BytesIO(buf.getvalue()), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


# ---------------------------------------------------------------- integrations
@app.route("/koppelingen", methods=["GET", "POST"])
@auth.admin_required
def integrations():
    s = settings()
    if request.method == "POST":
        section = request.form.get("section")
        if section == "general":
            if "firm_name" in request.form:   # a form without the field must not wipe the name
                s["firm_name"] = " ".join(request.form.get("firm_name", "").split())[:60]
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
                           integ=integrations_status(), live_key=ai.live(), firm=firm_name())


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
    if guest():  # only drafts about the fictional sample scan
        seen = {d["id"] for d in visible_docs()}
        ms = [m for m in ms if m.get("doc") in seen]
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
    s["retention_required"] = [k for k in ("mijnkantoor", "exact", "ubl") if request.form.get(k)] or ["mijnkantoor"]
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
                sender = whatsapp.normalise(item["from"])
                nr = next((c["nr"] for c in clients().values()
                           if c.get("whatsapp") and whatsapp.normalise("+" + c["whatsapp"].lstrip("+")) == sender), None)
                create_batch([(name, data)], "whatsapp", f"WhatsApp {whatsapp.mask(item['from'])}", client_nr=nr)
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
               "wwft_stop_on": "Wwft-stop aan", "wwft_stop_off": "Wwft-stop uit", "whatsapp_failed": "WhatsApp-bestand mislukt",
               "guest_tour": "Rondleiding geopend", "flag_noted": "Notitie bij rode vlag",
               "flag_note_removed": "Notitie bij vlag verwijderd", "chase_drafted": "Bon opgevraagd (concept)",
               "chase_closed": "Bankregel afgehandeld zonder bon", "bank_imported": "Bankregels ingelezen",
               "client_upload": "Aangeleverd via klantlink", "sorted": "Dossier bepaald", "checked": "Controles uitgevoerd"}



# ---------------------------------------------------------------- bonnenjacht: bank lines without a document
def bank_view(cl=None):
    """Every bank line of every client with its match and, when there is none, the question for the client."""
    cl = cl or clients()
    chase = state.setdefault("chase", {})
    firm = firm_name()
    out = []
    for nr, lines in BANK.load().items():
        if nr not in cl:
            continue
        mine = [{"id": d["id"], "kind": d["kind"], "status": d["status"], "counterparty": (d["result"] or {}).get("counterparty"),
                 "doc_date": (d["result"] or {}).get("doc_date"), "gross_total": (d["result"] or {}).get("gross_total")}
                for d in state["docs"].values() if d.get("client_nr") == nr and d.get("result") and d["status"] != "reading"]
        found = bank.match(lines, mine)
        for line in lines:
            out.append(bank_line(line, nr, found.get(line["id"]), chase.get(line["id"]), cl[nr], firm))
    return out


def bank_line(line, nr, doc_id, chase, client, firm):
    if chase and chase.get("closed"):
        status = "afgehandeld"
    elif doc_id:
        status = "ontvangen" if chase else "gekoppeld"
    else:
        status = "gevraagd" if chase else ("ontbreekt" if line["amount"] < 0 else "open")
    return dict(line, client_nr=nr, doc=doc_id, status=status, chase=chase, shop=bank.shop(line),
                text=bank.text(line, client, firm), type_label=bank.TYPES.get(line.get("type"), line.get("type") or ""))


def demo_replies():
    """Demo instance only: the fictional reply photos staff can play back as the client's answer."""
    if not DEMO:
        return {}
    m = sample_manifest() or {}
    return {r["bank"]: {"image": "/static/" + r["image"], "size": r["size"], "name": name}
            for name, r in (m.get("replies") or {}).items()}


@app.post("/api/bank/<line_id>")
def bank_api(line_id):
    """ask: draft the question (WhatsApp or e-mail). send: draft it and approve it with both statements.
    close: no document needed, with a note. reopen. demo_reply: play the fictional client's answer (demo instance)."""
    cl = clients()
    found = next(((nr, l) for nr, lines in BANK.load().items() for l in lines if l["id"] == line_id), None)
    if not found or found[0] not in cl:
        abort(404)
    nr, line = found
    c = cl[nr]
    action = request.form.get("action")
    chase = state.setdefault("chase", {})
    message = ""
    if action in ("ask", "send"):
        channel = request.form.get("channel") if request.form.get("channel") in outbox_mod.CHANNELS else "whatsapp"
        to = request.form.get("to", "").strip() or (("+" + c["whatsapp"]) if channel == "whatsapp" and c.get("whatsapp") else c.get("email") or "")
        body = request.form.get("body", "").strip() or bank.text(line, c, firm_name())
        mid = outbox.draft(channel, to, f"Ontbrekende bon ({c['name']})", body, nr, None, who(), "ontbrekende bon")
        chase[line_id] = {"message": mid, "by": who(), "at": now(), "channel": channel}
        audit("-", "-", "chase_drafted", who(), message=mid, note=f"{line['date']} {bank.shop(line)} {checks.nl(abs(line['amount']))}")
        message = "Conceptbericht klaargezet onder Berichten. Er is nog niets verstuurd."
        if action == "send":
            message = approve_message(mid, request.form, c)
    elif action == "close":
        note = " ".join(request.form.get("note", "").split())[:300] or "Geen bon nodig"
        chase[line_id] = {"closed": True, "by": who(), "at": now(), "note": note}
        audit("-", "-", "chase_closed", who(), note=f"{line['date']} {bank.shop(line)}: {note}")
        message = "Vastgelegd."
    elif action == "reopen":
        chase.pop(line_id, None)
    elif action == "demo_reply" and DEMO:
        m = sample_manifest() or {}
        reply = next((r for r in (m.get("replies") or {}).values() if r.get("bank") == line_id), None)
        if not reply:
            return jsonify({"ok": False, "message": "Voor deze regel is geen voorbeeldantwoord.", "bank": bank_view(cl)})
        data = (HERE / "static" / reply["image"]).read_bytes()
        create_batch([("whatsapp-bon.jpg", data)], "whatsapp", f"WhatsApp {whatsapp.mask('+' + c.get('whatsapp', ''))} (demo)",
                     replay=True, fast=True, client_nr=nr)
        chase.setdefault(line_id, {"by": who(), "at": now(), "channel": "whatsapp"})["replied"] = now()
        message = "Voorbeeldantwoord van de klant ingelezen."
    else:
        abort(400)
    save_state()
    return jsonify({"ok": True, "message": message, "bank": bank_view(cl)})


# ---------------------------------------------------------------- the client's own upload link
_uploads = {}  # token -> [timestamps], in memory: a simple brake on a leaked link


def upload_token(nr):
    sig = hmac.new(app.secret_key.encode(), f"aanleveren:{nr}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"{nr}-{sig}"


def upload_url(nr):
    return f"/aanleveren/{upload_token(nr)}"


@app.route("/aanleveren/<token>", methods=["GET", "POST"])
def client_upload(token):
    """A page for the firm's client, in the firm's name: photograph a receipt, done. No account, no app. The link
    is personal (signed) and can only add documents to that client's dossier; it shows nothing of the dossier.
    On a demo instance nothing is accepted: the page plays the upload in the browser."""
    nr = token.rsplit("-", 1)[0]
    c = (showroom()["clients"] if DEMO else clients()).get(nr)
    if not c or not hmac.compare_digest(token, upload_token(nr)):
        abort(404)
    firm = showroom()["firm"] if DEMO else firm_name()
    done = None
    if request.method == "POST":
        if DEMO:
            abort(403, "Dit is een demonstratie: er wordt niets verstuurd of bewaard.")
        recent = [t for t in _uploads.get(token, []) if time.time() - t < 3600]
        files = [(f.filename, f.read()) for f in request.files.getlist("files")[:10]
                 if f and f.filename and Path(f.filename).suffix.lower() in ALLOWED]
        if len(recent) + len(files) > 40:
            abort(400, "Te veel bestanden in korte tijd. Probeer het later opnieuw of neem contact op met het kantoor.")
        if files:
            _uploads[token] = recent + [time.time()] * len(files)
            create_batch(files, "klantlink", f"Aanleverlink {c['name']}", client_nr=nr)
            audit("-", "-", "client_upload", "Klant", client=nr, files=len(files))
            done = len(files)
    return render_template("aanleveren.html", client=c, firm=firm, demo=DEMO, done=done, token=token)


# ---------------------------------------------------------------- showroom: the fictional sample of the tour
# The tour never touches stored state. Everything a guest sees is derived from the sample files in the repository
# (so it is there right after a restart, and nothing staff do can change it); what a guest "does" lives in the
# visitor's browser. The checks, the exports and the cover sheets a guest gets are made by the same code as for
# staff, through stateless requests.
_showroom = None
_sample_pdf = None
_sample_cache = {}
_pdf_lock = threading.Lock()


def showroom():
    global _showroom
    if _showroom is None:
        with lock:
            if _showroom is None:
                _showroom = build_showroom()
    return _showroom


def build_showroom():
    m = sample_manifest()
    cl = {c["nr"]: c for c in json.loads((SAMPLES / "clients.json").read_text(encoding="utf-8"))}
    ch = json.loads((SAMPLES / "charts.json").read_text(encoding="utf-8"))
    lines = json.loads((SAMPLES / "bank.json").read_text(encoding="utf-8")) if (SAMPLES / "bank.json").exists() else {}
    today = checks.parse_date(m.get("as_of")) or date.today()
    stamp = f"{m.get('as_of')}T09:15:00"
    owner, cur = {}, None
    for p in m["pages"]:
        cur = p["ref"] if p["kind"] == "cover" else cur
        owner[p["n"]] = cur

    def make(spec, nr, via="scheidingsblad"):
        result = deepcopy(spec["answer"])
        reason = result.pop("split_reason", None) or "Zelfde afzender en kenmerk"
        doc = {"id": spec["key"], "batch": "voorbeeld", "key": spec["key"], "pages": list(spec["pages"]), "client_nr": nr,
               "client_via": via, "kind": result["kind"], "split_reason": reason, "result": result, "status": "review",
               "flags": [], "corrections": [], "exports": {}, "created": stamp, "seconds": None, "boxes": spec["boxes"],
               "flag_notes": {}, "trail": {"route": "replay (geen AI)"}}
        doc["filing"] = exports.proposal(doc, cl.get(nr) or {}, exports.DEFAULT_FOLDERS)
        return doc

    by_key = {d["key"]: d for d in m["documents"]}
    every = {p for d in m["documents"] for p in d["pages"]}
    first = [make(sp, owner[sp["pages"][0]]) for sp in manifest_specs(m, every)]
    staged = [{"id": "+".join(st["keys"]), "parts": st["keys"]} for st in m.get("staged") or []]
    alt = [make({"key": k, "pages": by_key[k]["pages"], "answer": by_key[k]["answer"], "boxes": by_key[k].get("boxes") or {}},
                owner[by_key[k]["pages"][0]]) for st in staged for k in st["parts"]]
    sr = {"sha": m["sha256"], "as_of": m.get("as_of"), "today": today, "firm": m.get("firm") or DEMO_FIRM, "title": m.get("title"),
          "file": m["file"], "pages": m["pages"], "clients": cl, "charts": ch, "docs": first, "alternates": alt,
          "staged": staged, "bank_lines": lines, "stamp": stamp}
    whole = [d for d in first if d["id"] not in {st["id"] for st in staged}]
    for d in first:
        sample_recheck(sr, d, [o["id"] for o in first])
    for d in alt:
        sample_recheck(sr, d, [o["id"] for o in whole + alt])
    sr["replies"] = {}
    for name, r in (m.get("replies") or {}).items():
        d = make({"key": f"R-{name}", "pages": [1], "answer": r["answer"], "boxes": r.get("boxes") or {}}, r["client"],
                 "WhatsApp-nummer van de klant")
        d["batch"], d["image"], d["size"] = "antwoord", "/static/" + r["image"], r["size"]
        sample_recheck(sr, d, [o["id"] for o in whole + alt], reply=True)
        sr["replies"][r["bank"]] = {"name": name, "image": "/static/" + r["image"], "size": r["size"], "doc": d}
    return sr


def sample_pool(sr):
    pool = {d["id"]: d for d in sr["docs"] + sr["alternates"]}
    pool.update({r["doc"]["id"]: r["doc"] for r in (sr.get("replies") or {}).values()})
    return pool


def sample_recheck(sr, doc, active, reply=False):
    """Flags, badges and booking evidence for one document of the sample, with the real check code. active: the ids
    of the documents that are in the visitor's pile right now (a split or removed document is not compared with)."""
    pool = sample_pool(sr)
    earlier = [pool[i] for i in active if i in pool and i != doc["id"] and pool[i]["client_nr"] == doc["client_nr"]
               and pool[i]["batch"] == "voorbeeld" and (reply or doc["batch"] != "voorbeeld" or pool[i]["pages"][0] < doc["pages"][0])]
    history = ledger_history(doc["client_nr"]) if doc.get("client_nr") else []
    chart = sr["charts"].get((sr["clients"].get(doc.get("client_nr")) or {}).get("chart", "standaard"), [])
    doc["flags"] = compute_flags(doc, earlier, history, sr["today"], chart)
    doc["light"] = checks.status_of(doc["flags"])
    doc["badges"], doc["evidence"] = insight(doc, earlier, history, chart)
    return doc


def sample_trail(sr, doc):
    pages = ", ".join(str(p) for p in doc["pages"])
    c = sr["clients"].get(doc.get("client_nr")) or {}
    if doc["batch"] == "antwoord":
        start = [{"ts": sr["stamp"], "event": "received", "label": "Foto ontvangen", "actor": "WhatsApp",
                  "text": f"Antwoord van {c.get('contact') or 'de klant'} op de vraag van het kantoor"},
                 {"ts": sr["stamp"], "event": "sorted", "label": "Dossier bepaald", "actor": "Systeem",
                  "text": f"Dossier {doc['client_nr']} via het WhatsApp-nummer van de klant"}]
    else:
        start = [{"ts": sr["stamp"], "event": "received", "label": "Scan ontvangen", "actor": "Scanner",
                  "text": f"{sr['file']} · pagina {pages}"},
                 {"ts": sr["stamp"], "event": "sorted", "label": "Dossier bepaald", "actor": "Systeem",
                  "text": f"Dossier {doc['client_nr']} via het scheidingsblad (QR-code, geen AI)"}]
    flags = [f for f in doc["flags"] if f["level"] in ("error", "warning")]
    return start + [
        {"ts": sr["stamp"], "event": "read", "label": "Document gelezen", "actor": "Systeem",
         "text": "Vaste uitkomst van de voorbeeldstapel: de rondleiding leest niet live"},
        {"ts": sr["stamp"], "event": "checked", "label": "Controles uitgevoerd", "actor": "Systeem",
         "text": f"Rekenregels, btw, IBAN, dubbel en termijnen: {len(flags)} punt(en) voor de controleur"
                 if flags else "Rekenregels, btw, IBAN, dubbel en termijnen: geen bijzonderheden"}]


_showroom_json = None


def showroom_public():
    global _showroom_json
    if _showroom_json is None:
        sr = showroom()
        firm = sr["firm"]

        def out(d):
            return cockpit_doc(d, sample_trail(sr, d))

        docs = [{"id": d["id"], "kind": d["kind"], "status": "review", "counterparty": d["result"].get("counterparty"),
                 "doc_date": d["result"].get("doc_date"), "gross_total": d["result"].get("gross_total"), "client_nr": d["client_nr"]}
                for d in sr["docs"] + sr["alternates"] if d["id"] not in {st["id"] for st in sr["staged"]}]
        lines = []
        for nr, rows in sr["bank_lines"].items():
            found = bank.match(rows, [d for d in docs if d["client_nr"] == nr])
            for line in rows:
                lines.append(bank_line(line, nr, found.get(line["id"]), None, sr["clients"][nr], firm))
        _showroom_json = {
            "mode": "sim", "sha": sr["sha"][:12], "as_of": sr["as_of"], "firm": firm, "title": sr["title"], "file": sr["file"],
            "user": "U (rondleiding)", "kinds": ai.KINDS, "vat_codes": ai.VAT_CODES, "reading": 0,
            "clients": {nr: client_public(c) for nr, c in sr["clients"].items() if nr in {p["ref"] for p in sr["pages"]}},
            "charts": sr["charts"], "pages": sr["pages"], "docs": [out(d) for d in sr["docs"]],
            "alternates": [out(d) for d in sr["alternates"]], "staged": sr["staged"],
            "replies": {k: {"image": r["image"], "size": r["size"], "name": r["name"], "doc": out(r["doc"])}
                        for k, r in sr["replies"].items()},
            "bank": lines, "img": f"/voorbeeld/p/{{n}}.jpg?v={sr['sha'][:8]}", "sprite": "/static/demo/stapel-42.jpg",
            "confirm": {"read": outbox_mod.CONFIRM_READ, "wwft": outbox_mod.CONFIRM_WWFT}, "manual_minutes": None,
            "demo": True, "ai": "Vaste voorbeeldstapel (geen AI)", "channels": {"whatsapp": False, "email": False}}
    return _showroom_json


@app.get("/api/voorbeeld.json")
def showroom_json():
    resp = jsonify(showroom_public())
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


def sample_pdf():
    global _sample_pdf
    import pypdfium2 as pdfium
    if _sample_pdf is None:
        _sample_pdf = pdfium.PdfDocument(str(SAMPLES / SAMPLE_SCAN))
    return _sample_pdf


def sample_page_jpeg(n):
    """One page of the fictional sample scan as the JPEG that is inside the PDF (no re-encoding)."""
    import pypdfium2 as pdfium
    with _pdf_lock:
        if ("p", n) not in _sample_cache:
            pdf = sample_pdf()
            if not 1 <= n <= len(pdf):
                return None
            page = pdf[n - 1]
            images = list(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]))
            buf = io.BytesIO()
            if images:
                images[0].extract(buf, fb_format="jpeg")
            else:
                page.render(scale=scan.DPI / 72).to_pil().convert("RGB").save(buf, "JPEG", quality=80)
            _sample_cache[("p", n)] = buf.getvalue()
        return _sample_cache[("p", n)]


def sample_doc_pdf(pages):
    """The pages of one sample document as a PDF (the attachment of its UBL file)."""
    import pypdfium2 as pdfium
    key = ("d",) + tuple(pages)
    with _pdf_lock:
        if key not in _sample_cache:
            new = pdfium.PdfDocument.new()
            new.import_pages(sample_pdf(), [p - 1 for p in pages])
            buf = io.BytesIO()
            new.save(buf)
            _sample_cache[key] = buf.getvalue()
        return _sample_cache[key]


def sample_pdf_of(doc):
    if doc.get("image"):   # a reply photo: one image, wrapped as a PDF
        key = ("r", doc["id"])
        with _pdf_lock:
            if key not in _sample_cache:
                from PIL import Image
                buf = io.BytesIO()
                Image.open(HERE / doc["image"].lstrip("/")).convert("RGB").save(buf, "PDF", resolution=150)
                _sample_cache[key] = buf.getvalue()
            return _sample_cache[key]
    return sample_doc_pdf(doc["pages"])


@app.get("/voorbeeld/p/<int:n>.jpg")
def showroom_page(n):
    data = sample_page_jpeg(n)
    if data is None:
        abort(404)
    resp = send_file(io.BytesIO(data), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


SIM_TEXT = ["counterparty", "reference", "doc_date", "due_date", "supplier_vat_number", "supplier_kvk", "iban", "description"]
SIM_MAX = 300_000


def _simnum(v):
    try:
        return None if v in (None, "") else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def sim_doc(sr, item):
    """A copy of a sample document with what the visitor changed in the simulation. Only known documents, only the
    fields a reviewer can edit, everything bounded: nothing a visitor sends is stored or trusted beyond this copy."""
    pool = sample_pool(sr)
    base = pool.get(str(item.get("id", "")).split("#")[0])
    if not base:
        return None
    doc = deepcopy(base)
    doc["id"] = str(item.get("id"))[:60]
    pages = item.get("pages")
    if isinstance(pages, list) and pages and set(pages) <= set(base["pages"]) and pages != base["pages"]:
        doc["pages"] = [p for p in base["pages"] if p in pages]
        doc["boxes"] = {k: v for k, v in doc["boxes"].items() if v.get("page") in doc["pages"]}
        note = "Gesplitst in de rondleiding: dit deel is niet opnieuw gelezen. Met een eigen stapel leest Scanstraat beide delen opnieuw."
        doc["result"]["issues"] = list(doc["result"].get("issues") or []) + [note]
        doc["split_reason"] = "handmatig gesplitst (rondleiding)"
    if item.get("kind") in ai.KINDS:
        doc["kind"] = doc["result"]["kind"] = item["kind"]
    if "client_nr" in item and (item["client_nr"] is None or item["client_nr"] in sr["clients"]):
        if item["client_nr"] != doc["client_nr"]:
            doc["client_nr"], doc["client_via"] = item["client_nr"], "gekozen in de rondleiding" if item["client_nr"] else None
    r = item.get("result") if isinstance(item.get("result"), dict) else {}
    for k in SIM_TEXT:
        if k in r:
            doc["result"][k] = (str(r[k])[:120].strip() or None) if r[k] is not None else None
    for k in ("net_total", "gross_total"):
        if k in r:
            doc["result"][k] = _simnum(r[k])
    if isinstance(r.get("vat_lines"), list):
        doc["result"]["vat_lines"] = [{"rate": _simnum(v.get("rate")), "base": _simnum(v.get("base")), "amount": _simnum(v.get("amount"))}
                                      for v in r["vat_lines"][:6] if isinstance(v, dict)]
    if isinstance(r.get("lines"), list):
        doc["result"]["lines"] = [{"account": str(l.get("account") or "")[:8], "vat_code": l.get("vat_code") if l.get("vat_code") in ai.VAT_CODES else "V21",
                                   "amount_excl": _simnum(l.get("amount_excl")) or 0.0, "vat_amount": _simnum(l.get("vat_amount")) or 0.0,
                                   "description": str(l.get("description") or "")[:80]}
                                  for l in r["lines"][:12] if isinstance(l, dict) and l.get("account")]
    doc["filing"] = exports.proposal(doc, sr["clients"].get(doc["client_nr"]) or {}, exports.DEFAULT_FOLDERS)
    active = [str(i)[:60] for i in item.get("active") or [] if isinstance(i, str)][:60]
    sample_recheck(sr, doc, active or [d["id"] for d in sr["docs"]], reply=doc["batch"] != "voorbeeld")
    notes = item.get("notes") if isinstance(item.get("notes"), dict) else {}
    ids = {f["id"] for f in doc["flags"]}
    doc["flag_notes"] = {k: {"note": str(v)[:500], "by": "U (rondleiding)", "at": now()} for k, v in notes.items() if k in ids}
    if item.get("status") == "approved":
        doc["status"] = "approved"
        doc["approved"] = {"by": "U (rondleiding)", "at": str(item.get("at") or now())[:19], "open_flags": []}
    return doc


def _sim_body():
    if (request.content_length or 0) > SIM_MAX:
        abort(413)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400)
    return body


@app.post("/api/voorbeeld/check")
def sample_check():
    """Stateless: the real checks on a sample document as the visitor edited it. Nothing is stored."""
    sr = showroom()
    doc = sim_doc(sr, _sim_body())
    if not doc:
        abort(404)
    return jsonify(cockpit_doc(doc, sample_trail(sr, doc)))


def export_bundle(docs, cl, pdf_of, concept=False):
    """What "Exporteer batch" shows before anything is downloaded: the SI-UBL file per purchase document with our own
    checks, the rows of the Excel register, the MijnKantoor folders and the Exact import file."""
    out = {"count": len(docs), "ubl": [], "skipped": []}
    for d in docs:
        if d["kind"] not in UBL_KINDS or d.get("purged"):
            if d["kind"] in ai.BOOKABLE and not d.get("purged"):
                out["skipped"].append({"id": d["id"], "why": "Verkoopfactuur: gaat mee in het register en de boekingsregels, niet als inkoop-UBL."})
            continue
        xml, name = ubl_xml(d, cl, pdf_of(d), concept)
        out["ubl"].append({"id": d["id"], "name": name, "title": (d["result"] or {}).get("counterparty") or ai.KINDS[d["kind"]],
                           "client": (cl.get(d.get("client_nr")) or {}).get("name", "onbekend"), "xml": ubl.shorten(xml),
                           "kb": round(len(xml) / 1024), "checks": ubl.verify(xml)})

    def cell(v):
        return v.strftime("%d-%m-%Y") if isinstance(v, (date, datetime)) else v

    out["register"] = {"cols": exports.REGISTER_COLS,
                       "rows": [[cell(v) for v in row] for row in exports.register_rows(docs, cl, ai.KINDS)]}
    out["lines"] = {"cols": ["Documentnr", "Klantnr", "Dagboek", "Wederpartij", "Kenmerk", "Datum", "Grootboek", "Omschrijving",
                             "Bedrag excl.", "Btw-code", "Btw-bedrag"],
                    "rows": [[d["id"], d.get("client_nr"), "Verkoop" if d["kind"] == "verkoopfactuur" else "Inkoop",
                              d["result"].get("counterparty"), d["result"].get("reference"), cell(checks.parse_date(d["result"].get("doc_date"))),
                              l["account"], l.get("description"), l.get("amount_excl"), l["vat_code"], l.get("vat_amount")]
                             for d in docs for l in d["result"].get("lines") or []]}
    out["tree"] = [{"id": d["id"], "dossier": exports.dossier_name(cl[d["client_nr"]]) if d.get("client_nr") in cl else "Klant onbekend",
                    "folder": d["filing"]["folder"], "file": d["filing"]["filename"]} for d in docs if d.get("filing")]
    bookable = [_exact_doc(d, SETTINGS.default if concept else None) for d in docs if d["kind"] in ai.BOOKABLE]
    out["exact"] = exports.exact_xml(bookable).decode("utf-8") if bookable else ""
    return out


def _sim_docs(sr, body):
    items = body.get("docs") if isinstance(body.get("docs"), list) else []
    docs = [sim_doc(sr, i) for i in items[:60] if isinstance(i, dict)]
    return [d for d in docs if d and d["status"] == "approved" and d.get("client_nr") in sr["clients"]]


@app.post("/api/voorbeeld/export")
def sample_export():
    """Stateless: the export of the visitor's simulation, made by the same code as a real export. want: preview
    (json), ubl (one xml), ubl.zip, register.xlsx, exact.xml, exact.csv, mijnkantoor.zip. Every file is a concept."""
    import zipfile
    sr = showroom()
    body = _sim_body()
    docs = _sim_docs(sr, body)
    cl, want = sr["clients"], body.get("want") or "preview"
    if want == "preview":
        return jsonify(export_bundle(docs, cl, sample_pdf_of, concept=True))
    if not docs:
        abort(400, "Nog niets goedgekeurd in de rondleiding.")
    stamp = "voorbeeld"
    if want == "ubl":
        doc = next((d for d in docs if d["id"] == body.get("id") and d["kind"] in UBL_KINDS), None) or abort(404)
        xml, name = ubl_xml(doc, cl, sample_pdf_of(doc), concept=True)
        return send_file(io.BytesIO(xml.encode("utf-8")), mimetype="application/xml", as_attachment=True, download_name=name)
    if want == "ubl.zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in docs:
                if d["kind"] in UBL_KINDS:
                    xml, name = ubl_xml(d, cl, sample_pdf_of(d), concept=True)
                    z.writestr(f"{secure_filename(cl[d['client_nr']]['name']) or 'onbekend'}/{name}", xml)
        return send_file(io.BytesIO(buf.getvalue()), mimetype="application/zip", as_attachment=True, download_name=f"ubl-{stamp}.zip")
    if want == "register.xlsx":
        return send_file(io.BytesIO(exports.excel_register(docs, cl, ai.KINDS)), as_attachment=True,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         download_name=f"scanregister-{stamp}.xlsx")
    if want in ("exact.xml", "exact.csv"):
        xd = [_exact_doc(d, SETTINGS.default) for d in docs if d["kind"] in ai.BOOKABLE]
        data = exports.exact_xml(xd) if want.endswith("xml") else exports.exact_csv(xd)
        return send_file(io.BytesIO(data), as_attachment=True, download_name=f"exact-import-{stamp}.{want[-3:]}",
                         mimetype="application/xml" if want.endswith("xml") else "text/csv")
    if want == "mijnkantoor.zip":
        data = exports.zip_bundle([(d, cl[d["client_nr"]], sample_pdf_of(d)) for d in docs])
        return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True, download_name=f"mijnkantoor-{stamp}.zip")
    abort(400)


@app.get("/api/export/preview")
def export_preview():
    """Staff: the same preview for their own approved documents (nothing is marked as exported by looking)."""
    docs = [d for d in _selected(None) if not d.get("purged")]
    return jsonify(export_bundle(docs, clients(), lambda d: vault.read(pdf_path(d))))


def hero_data():
    """The three scenarios of the homepage hero, taken from the sample with the real check results."""
    sr = showroom()
    pub = showroom_public()
    by_id = {d["id"]: d for d in pub["docs"] + pub["alternates"]}
    fraud = by_id.get("M-BGR-IBAN")
    known = sorted({h["iban"] for h in ledger_history("1057") if h.get("iban") and
                    checks.norm(h.get("supplier_name")) == checks.norm(fraud["result"]["counterparty"])}) if fraud else []
    earlier = sum(1 for h in ledger_history("1057") if fraud and checks.norm(h.get("supplier_name")) == checks.norm(fraud["result"]["counterparty"]))
    line = next((l for l in pub["bank"] if l["id"] == "B1057-04"), None)
    reply = pub["replies"].get("B1057-04")
    groups = []
    for nr, c in pub["clients"].items():
        ds = [d for d in pub["docs"] if d["client_nr"] == nr]
        groups.append({"nr": nr, "name": c["name"], "docs": len(ds), "pages": sum(len(d["pages"]) for d in ds),
                       "red": sum(d["light"] == "red" for d in ds), "amber": sum(d["light"] == "amber" for d in ds),
                       "green": sum(d["light"] == "green" for d in ds)})
    return {"pages": pub["pages"], "groups": groups, "docs": [{"id": d["id"], "pages": d["pages"], "client_nr": d["client_nr"],
                                                               "light": d["light"], "title": d["result"].get("counterparty") or ai.KINDS[d["kind"]],
                                                               "kind": ai.KINDS[d["kind"]], "total": d["result"].get("gross_total")} for d in pub["docs"]],
            "fraud": {"doc": fraud, "known": known, "earlier": earlier} if fraud else None,
            "chase": {"line": line, "reply": reply, "client": pub["clients"].get("1057")} if line and reply else None,
            "firm": pub["firm"], "counts": {"pages": len(pub["pages"]), "blank": sum(p["kind"] == "blank" for p in pub["pages"]),
                                            "cover": sum(p["kind"] == "cover" for p in pub["pages"]), "docs": len(pub["docs"]),
                                            "multi": sum(len(d["pages"]) > 1 for d in pub["docs"]),
                                            "missing": sum(l.get("status") == "ontbreekt" for l in pub["bank"])}}


# ---------------------------------------------------------------- status (the login page's badge, and for monitoring)
def hosting_label():
    """Where this instance runs, as far as the app can know: a label the operator sets, or the region in the
    Render blueprint this instance was deployed from. Never a guess."""
    label = os.environ.get("HOSTING_LABEL", "").strip()
    if label:
        return label
    if region.production():
        return region.EU_LOCATIONS.get(os.environ.get("HOSTING_LOCATION", ""), "")
    if os.environ.get("RENDER") and (HERE / "render.yaml").exists():
        m = re.search(r"^\s*region:\s*(\w+)", (HERE / "render.yaml").read_text(encoding="utf-8"), re.M)
        return {"frankfurt": "Frankfurt, Duitsland (EU)", "amsterdam": "Amsterdam (EU)"}.get(m.group(1).lower(), "") if m else ""
    return ""


@app.get("/status.json")
def status_json():
    try:
        sealed = vault.unseal(vault.seal(b"ok", b"status"), b"status") == b"ok"
    except Exception:  # noqa: BLE001
        sealed = False
    failed = [b for b in state["batches"].values() if b["status"] == "error" and b["created"] >= BOOT.isoformat()]
    items = [{"key": "portaal", "label": "Kantoorportaal", "ok": True},
             {"key": "kluis", "label": "Versleutelde opslag (AES-256-GCM)", "ok": sealed},
             {"key": "2fa", "label": "Tweestapsverificatie verplicht", "ok": bool(auth.REQUIRE_2FA)},
             {"key": "straat", "label": "Verwerkingsstraat", "ok": not failed}]
    ok = all(i["ok"] for i in items)
    where = hosting_label()
    resp = jsonify({"ok": ok, "text": "Alle systemen operationeel" if ok else "Storing in een onderdeel",
                    "location": where, "eu": bool(where) and "EU" in where or region.production(), "checks": items,
                    "checked": now(), "up_since": BOOT.isoformat(timespec="seconds"), "ai": ai.label()})
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
