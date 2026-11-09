"""Live extraction on real paper: drop in any messy PDF, scan, screenshot or phone photo and watch Claude read it.

    DEMO_INSTANCE=1 AI_PROVIDER=bedrock AWS_REGION=eu-west-1 AWS_BEARER_TOKEN_BEDROCK=... \\
        .venv/bin/python test_real_ingestion.py bon.jpg facturen.pdf screenshot.png --client 1001

Per run:
  1. pages   every file becomes page images (scan.py); blank pages are skipped
  2. split   Claude groups the pages into documents, from the page images (egress.send_images)
  3. read    Claude reads each document: fields, VAT lines, booking proposal (the app's own prompts and schema)
  4. check   the app's deterministic checks (checks.py): totals, VAT, IBAN, dates, duplicates within the run
  5. UBL     a UBL 2.1 (NLCIUS) file per purchase document, with its pages embedded as PDF (ubl.py)
Everything lands in --out (default ./ingestie-<date-time>): result-NN.json per document, UBL files, and egress.jsonl
with a SHA-256 of every request that left. Nothing is booked, filed or sent anywhere else.

Only fictional documents or your own papers: page images go to the AI provider (Claude in Amazon Bedrock in an EU
region with AI_PROVIDER=bedrock). Never a client's documents without a signed verwerkersovereenkomst.
Refuses to run with APP_ENV=production. --dry-run shows what would be sent without calling any API.
This is a command-line tool, not a pytest test: it has no test functions and spends real API credit.
"""
import argparse
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
UBL_KINDS = {"inkoopfactuur", "kassabon", "creditnota"}
CHECK_KIND = {"inkoopfactuur": "invoice", "kassabon": "receipt", "creditnota": "credit_note", "verkoopfactuur": "invoice"}
ALIAS = {"counterparty": "supplier_name", "reference": "invoice_number", "doc_date": "invoice_date"}


def check_fields(kind, r):
    """Same mapping as app.as_check_fields."""
    f = {"doc_type": CHECK_KIND.get(kind, "other"), "supplier_name": r.get("counterparty"),
         "invoice_number": r.get("reference"), "invoice_date": r.get("doc_date"), "description": r.get("description")}
    for k in ("due_date", "supplier_vat_number", "supplier_kvk", "iban", "net_total", "vat_lines", "gross_total", "currency"):
        f[k] = r.get(k)
    f["uncertain_fields"] = [ALIAS.get(x, x) for x in r.get("uncertain_fields") or []]
    f["issues"] = r.get("issues") or []
    return f


def pdf_from(images):
    buf = io.BytesIO()
    images[0].save(buf, "PDF", resolution=150, save_all=True, append_images=images[1:])
    return buf.getvalue()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live extraction on real paper (demo use only).")
    ap.add_argument("files", nargs="+", type=Path, help="pdf, jpg, png, tif or webp files")
    ap.add_argument("--client", default="1001", help="client number or name from samples/clients.json (default 1001)")
    ap.add_argument("--out", type=Path, default=None, help="output folder (default ./ingestie-<date-time>)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent, call nothing")
    args = ap.parse_args(argv)

    if os.environ.get("APP_ENV", "").lower() == "production":
        sys.exit("Niet op een productie-omgeving: paginabeelden mogen daar nooit naar buiten.")
    os.environ.setdefault("DEMO_INSTANCE", "1")  # this tool is a demo context by definition (see docstring)
    sys.path.insert(0, str(HERE))
    import ai
    import checks
    import egress
    import scan
    import ubl

    clients = json.loads((HERE / "samples" / "clients.json").read_text(encoding="utf-8"))
    client = next((c for c in clients if args.client in (c["nr"], c["name"])), None)
    if not client:
        sys.exit(f"Klant {args.client} niet gevonden. Kies uit: " + ", ".join(f"{c['nr']} {c['name']}" for c in clients))
    charts = json.loads((HERE / "samples" / "charts.json").read_text(encoding="utf-8"))
    chart = charts.get(client.get("chart", "standaard")) or charts.get("standaard", [])
    accounts = [a for a, _ in chart] or ["4900"]

    # 1 pages
    pages, skipped, covers = [], 0, 0
    for f in args.files:
        try:
            imgs = scan.load_pages(f.name, f.read_bytes())
        except Exception as e:  # noqa: BLE001 - report and go on with the other files
            print(f"  ! {f.name}: niet te openen ({e})")
            continue
        for i, img in enumerate(imgs, 1):
            if scan.is_blank(img):
                skipped += 1
                continue
            if scan.read_cover_sheet(img):  # a Scanstraat cover sheet separates clients; it is not a document
                covers += 1
                continue
            pages.append({"file": f.name, "page": i, "img": img})
    if not pages:
        sys.exit("Geen pagina's met inhoud gevonden.")
    print(f"\n{len(pages)} pagina's uit {len(args.files)} bestand(en), {skipped} lege en {covers} scheidingsbladen overgeslagen.")
    print(f"Bestemming: {egress.DESTINATION} · model {egress.model_id()} · klant {client['nr']} {client['name']}")
    print("Alleen fictieve documenten of je eigen papieren: paginabeelden gaan naar de AI-dienst.\n")
    if args.dry_run:
        kb = sum(len(scan.jpeg_bytes(p["img"], 1600)) for p in pages) / 1024
        print(f"Dry run: {len(pages)} paginabeelden, samen ongeveer {kb:.0f} kB. Er is niets verstuurd.")
        return 0
    if not egress.configured():
        sys.exit(egress._missing_key() + ". Zet de sleutel in je omgeving (nooit in een bestand in de repo).")

    out = args.out or Path(f"ingestie-{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    egress._log = lambda rec: open(out / "egress.jsonl", "a", encoding="utf-8").write(json.dumps(rec) + "\n")
    usage = {"input_tokens": 0, "output_tokens": 0}

    def add(u):
        usage["input_tokens"] += u.get("input_tokens", 0)
        usage["output_tokens"] += u.get("output_tokens", 0)

    # 2 split, per 20 pages
    t0 = time.time()
    docs = []
    if len(pages) == 1:
        docs = [{"pages": [0], "kind": None}]
    for start in range(0, len(pages) if len(pages) > 1 else 0, egress.MAX_IMAGES):
        chunk = pages[start:start + egress.MAX_IMAGES]
        try:
            data, u, _ = egress.send_images("split", ai.SPLIT_SYSTEM, [scan.jpeg_bytes(p["img"], 1000) for p in chunk],
                                            ["Clients of this office: " + "; ".join(c["name"] for c in clients)],
                                            ai._split_schema())
            add(u)
            parts = ai._repair_split(data["documents"], len(chunk))
        except (egress.EgressBlocked, egress.EgressError) as e:
            print(f"  ! splitsen mislukt ({e}): elke pagina wordt een eigen document")
            parts = [{"pages": [i]} for i in range(1, len(chunk) + 1)]
        docs += [{"pages": [start + p - 1 for p in d["pages"]], "kind": d.get("kind")} for d in parts]
    print(f"Gesplitst in {len(docs)} document(en) in {time.time() - t0:.1f} s.")

    # 3 read, 4 check, 5 UBL
    ref = ai._reference(client, chart, "")
    done = []
    for n, d in enumerate(docs, 1):
        imgs = [pages[i]["img"] for i in d["pages"]]
        t = time.time()
        try:
            result, u, trail = ai._read_vision([scan.jpeg_bytes(im, 1600) for im in imgs], ref, accounts)
            add(u)
        except ai.AIError as e:
            print(f"  ! document {n}: niet gelezen ({e})")
            continue
        kind = result.get("kind") or d.get("kind") or "overig"
        f = check_fields(kind, result)
        flags = checks.model_flags(f)
        if kind in ai.BOOKABLE:
            flags = checks.run_checks(f, others=[x["fields"] for x in done]) + flags
        ubl_name = None
        if kind in UBL_KINDS:
            pdf_name = f"document-{n:02d}.pdf"
            xml = ubl.build(f, {"name": client["name"], "address": client.get("city"), "vat": client.get("vat")},
                            pdf_from(imgs), pdf_name)
            xml = xml.replace("goedgekeurd door een medewerker.", "NIET gecontroleerd: testuitvoer van test_real_ingestion.py.")
            ubl_name = f"ubl-{n:02d}.xml"
            (out / ubl_name).write_text(xml, encoding="utf-8")
        (out / f"result-{n:02d}.json").write_text(json.dumps(
            {"files": sorted({pages[i]["file"] for i in d["pages"]}), "pages": [pages[i]["page"] for i in d["pages"]],
             "kind": kind, "result": result, "flags": flags, "ubl": ubl_name, "sha256": trail.get("sha256")},
            indent=1, ensure_ascii=False), encoding="utf-8")
        done.append({"fields": f})
        light = checks.status_of(flags)
        print(f"\n[{n}] {ai.KINDS.get(kind, kind)} · {result.get('counterparty') or '?'} · {result.get('reference') or '-'} · "
              f"{result.get('doc_date') or '-'} · totaal {result.get('gross_total')} · {len(d['pages'])} pag. · "
              f"{time.time() - t:.1f} s · {'GROEN' if light == 'green' else 'ORANJE' if light == 'amber' else 'ROOD'}")
        for fl in flags:
            print(f"     {fl['level']:<7} {fl['message']}")
        if ubl_name:
            print(f"     UBL: {out / ubl_name}")
    print(f"\nKlaar in {time.time() - t0:.1f} s · {usage['input_tokens']} invoertokens, {usage['output_tokens']} uitvoertokens"
          f" · uitvoer in {out}/ · niets geboekt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
