"""Output after a person approved a document: MijnKantoor dossier, Excel register, Exact import file.

MijnKantoor: there is a Public API (developers.mijnkantoor.nl), but its documentation needs an
account, so it is not used here. What does work without it:
  - folder route: the approved PDF is written into the client's dossier folder in a folder that
    MijnKantoor synchronises (SharePoint/OneDrive two-way sync, set up in MijnKantoor itself);
  - ZIP route: a download with the same folder structure, dragged into the dossier by hand.
Exact Online: exact_api.py books through the REST API when connected. Without a connection
this file writes an XML import file and a CSV. The XML follows Exact's eExact GLEntries layout
from memory and has not been validated against a real administration: test one entry first.
"""
import csv
import io
import re
import zipfile
from datetime import date, datetime
from xml.sax.saxutils import escape

DEFAULT_FOLDERS = {
    "inkoopfactuur": "{jaar}/Inkoop/Q{kwartaal}",
    "kassabon": "{jaar}/Inkoop/Q{kwartaal}",
    "creditnota": "{jaar}/Inkoop/Q{kwartaal}",
    "verkoopfactuur": "{jaar}/Verkoop/Q{kwartaal}",
    "bankafschrift": "{jaar}/Bank",
    "belastingdienst": "{jaar}/Belastingdienst",
    "loonstrook": "{jaar}/Salaris",
    "contract": "Contracten",
    "overig": "{jaar}/Correspondentie",
}
DOSSIER = "{nr} - {naam}"


def _clean(text, n=60):
    text = re.sub(r"[/\\]", "-", str(text or ""))
    text = re.sub(r'[<>:"|?*\x00-\x1f]', "", text).strip()
    return re.sub(r"\s+", " ", text)[:n].strip()


def _date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def proposal(doc, client, folders):
    """Folder and file name inside the client's MijnKantoor dossier. Deterministic, editable by the reviewer."""
    r = doc["result"]
    d = _date(r.get("doc_date")) or date.today()
    tmpl = (folders or {}).get(doc["kind"]) or DEFAULT_FOLDERS.get(doc["kind"], "{jaar}/Overig")
    folder = tmpl.format(jaar=d.year, kwartaal=(d.month - 1) // 3 + 1, maand=f"{d.month:02d}")
    parts = [d.isoformat(), _clean(r.get("counterparty"), 40), _clean(r.get("reference"), 30)]
    name = " ".join(p for p in parts if p).rstrip(" .") or f"document {doc['id']}"
    return {"folder": folder, "filename": name + ".pdf"}


def dossier_name(client):
    return _clean(DOSSIER.format(nr=client["nr"], naam=client["name"]), 90)


def to_sync_folder(root, doc, client, pdf_bytes):
    """Copy into <root>/<nr - naam>/<folder>/<file>. Never overwrites: adds (2), (3)..."""
    target_dir = root / dossier_name(client) / doc["filing"]["folder"]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / doc["filing"]["filename"]
    i = 2
    while target.exists():
        target = target_dir / f"{target.stem} ({i}){target.suffix}"
        i += 1
    target.write_bytes(pdf_bytes)
    return target


def zip_bundle(items):
    """items: (doc, client, pdf_bytes). Returns zip bytes with the dossier folder structure."""
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for doc, client, pdf_bytes in items:
            base = f"{dossier_name(client)}/{doc['filing']['folder']}/{doc['filing']['filename']}"
            path, i = base, 2
            while path in used:
                path = base[:-4] + f" ({i}).pdf"
                i += 1
            used.add(path)
            z.writestr(path, pdf_bytes)
    return buf.getvalue()


# ---------------------------------------------------------------- Excel
REGISTER_COLS = ["Documentnr", "Gescand op", "Klantnr", "Klant", "Soort", "Wederpartij", "Kenmerk", "Documentdatum",
                 "Vervaldatum", "Excl. btw", "Btw", "Totaal", "Grootboek", "Btw-code", "MijnKantoor-map", "Bestandsnaam",
                 "Gecontroleerd door", "Gecontroleerd op", "MijnKantoor", "Exact", "Bijzonderheden"]


def register_rows(docs, clients, kinds):
    for d in docs:
        r, c = d["result"], clients.get(d.get("client_nr") or "", {})
        lines = r.get("lines") or []
        vat = sum(float(l.get("vat_amount") or 0) for l in lines) if lines else sum(
            float(v.get("amount") or 0) for v in r.get("vat_lines") or [])
        yield [
            d["id"], d["created"][:16].replace("T", " "), d.get("client_nr") or "", c.get("name", "onbekend"),
            kinds.get(d["kind"], d["kind"]), r.get("counterparty") or "", r.get("reference") or "",
            _date(r.get("doc_date")), _date(r.get("due_date")),
            r.get("net_total"), round(vat, 2) if (lines or r.get("vat_lines")) else None, r.get("gross_total"),
            ", ".join(sorted({l["account"] for l in lines})), ", ".join(sorted({l["vat_code"] for l in lines})),
            d["filing"]["folder"], d["filing"]["filename"],
            (d.get("approved") or {}).get("by", ""), (d.get("approved") or {}).get("at", "")[:16].replace("T", " "),
            "ja" if d.get("exports", {}).get("mijnkantoor") else "", "ja" if d.get("exports", {}).get("exact") else "",
            "; ".join(f["message"] for f in d.get("flags", []) if f["level"] in ("error", "warning"))[:250],
        ]


def excel_register(docs, clients, kinds):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    wb = Workbook()
    ws = wb.active
    ws.title = "Register"
    ws.append(REGISTER_COLS)
    for row in register_rows(docs, clients, kinds):
        ws.append(row)
    widths = [11, 16, 8, 28, 20, 30, 18, 13, 13, 11, 10, 11, 11, 12, 22, 44, 18, 16, 11, 8, 60]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="12355B")
        cell.alignment = Alignment(vertical="center")
    for row in ws.iter_rows(min_row=2):
        for idx in (7, 8):
            row[idx].number_format = "dd-mm-yyyy"
        for idx in (9, 10, 11):
            row[idx].number_format = "#,##0.00"
    if ws.max_row > 1:
        t = Table(displayName="Register", ref=f"A1:{get_column_letter(len(REGISTER_COLS))}{ws.max_row}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        ws.add_table(t)
    ws.freeze_panes = "A2"

    lines = wb.create_sheet("Boekingsregels")
    lines.append(["Documentnr", "Klantnr", "Dagboek", "Wederpartij", "Kenmerk", "Datum", "Grootboek", "Omschrijving",
                  "Bedrag excl.", "Btw-code", "Btw-bedrag"])
    for d in docs:
        r = d["result"]
        for l in r.get("lines") or []:
            lines.append([d["id"], d.get("client_nr"), "Verkoop" if d["kind"] == "verkoopfactuur" else "Inkoop",
                          r.get("counterparty"), r.get("reference"), _date(r.get("doc_date")), l["account"],
                          l.get("description"), l.get("amount_excl"), l["vat_code"], l.get("vat_amount")])
    for cell in lines[1]:
        cell.font = Font(bold=True)
    for i, w in enumerate([11, 8, 9, 30, 18, 12, 10, 34, 12, 14, 11], 1):
        lines.column_dimensions[get_column_letter(i)].width = w
    for row in lines.iter_rows(min_row=2):
        row[5].number_format = "dd-mm-yyyy"
        row[8].number_format = row[10].number_format = "#,##0.00"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- Exact import (no API)
def exact_csv(docs):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Dagboek", "Boekdatum", "Relatie", "Btw-nummer relatie", "Uw ref.", "Omschrijving", "Grootboek",
                "Bedrag excl.", "Btw-code", "Btw-bedrag", "Documentnr"])
    for d in docs:
        r = d["result"]
        for l in r.get("lines") or []:
            w.writerow([d["exact_journal"], r.get("doc_date"), r.get("counterparty"), r.get("supplier_vat_number") or "",
                        r.get("reference"), l.get("description") or r.get("description") or "", l["account"],
                        f"{float(l['amount_excl']):.2f}".replace(".", ","), d["exact_vat"].get(l["vat_code"], l["vat_code"]),
                        f"{float(l.get('vat_amount') or 0):.2f}".replace(".", ","), d["id"]])
    return buf.getvalue().encode("utf-8-sig")


def exact_xml(docs):
    """eExact GLEntries. Not validated against Exact: import one entry in a test administration first."""
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<eExact xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="eExact-Schema.xsd">',
           "  <GLEntries>"]
    for d in docs:
        r = d["result"]
        dt = _date(r.get("doc_date")) or date.today()
        sales = d["kind"] == "verkoopfactuur"
        out += [f'    <GLEntry entry="" status="E">',
                f'      <Journal code="{escape(d["exact_journal"])}" type="{"S" if sales else "P"}"/>',
                f"      <Date>{dt.isoformat()}</Date>",
                f"      <DocumentDate>{dt.isoformat()}</DocumentDate>",
                f"      <Description>{escape(_clean(r.get('description') or r.get('counterparty'), 60))}</Description>",
                f"      <YourRef>{escape(_clean(r.get('reference'), 30))}</YourRef>",
                f'      <{"Debtor" if sales else "Creditor"}><Name>{escape(r.get("counterparty") or "")}</Name>'
                + (f"<VATNumber>{escape(r['supplier_vat_number'])}</VATNumber>" if r.get("supplier_vat_number") else "")
                + f'</{"Debtor" if sales else "Creditor"}>']
        for i, l in enumerate(r.get("lines") or [], 1):
            out += [f'      <FinEntryLine number="{i}" type="N" subtype="{"K" if sales else "T"}">',
                    f"        <Date>{dt.isoformat()}</Date>",
                    f'        <FinYear number="{dt.year}"/><FinPeriod number="{dt.month}"/>',
                    f'        <GLAccount code="{escape(l["account"])}"/>',
                    f"        <Description>{escape(_clean(l.get('description'), 60))}</Description>",
                    f'        <Amount><Currency code="{escape(r.get("currency") or "EUR")}"/>'
                    f'<Value>{float(l["amount_excl"]):.2f}</Value>'
                    f'<VAT code="{escape(d["exact_vat"].get(l["vat_code"], l["vat_code"]))}"/></Amount>',
                    "      </FinEntryLine>"]
        out.append("    </GLEntry>")
    out += ["  </GLEntries>", "</eExact>", ""]
    return "\n".join(out).encode("utf-8")
