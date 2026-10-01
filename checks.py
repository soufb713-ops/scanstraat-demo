"""Deterministic checks. No AI here: every flag comes from arithmetic or exact rules.

Each check returns flags of the form
    {"level": "error"|"warning"|"info", "field": str|None, "message": str}
Messages are in Dutch because the reviewer is.
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

TOL = Decimal("0.02")
LABELS = {"doc_type": "de soort document", "supplier_name": "de leverancier", "supplier_vat_number": "het btw-nummer",
          "supplier_kvk": "het KvK-nummer", "invoice_number": "het factuurnummer", "invoice_date": "de factuurdatum",
          "due_date": "de vervaldatum", "currency": "de valuta", "net_total": "het bedrag excl. btw",
          "vat_lines": "de btw-regels", "gross_total": "het totaalbedrag", "iban": "het IBAN",
          "description": "de omschrijving", "ledger_category": "de grootboekrekening"}
NL_VAT_RATES = {Decimal("0"), Decimal("9"), Decimal("21")}


def D(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def iban_valid(iban):
    s = re.sub(r"\s+", "", iban or "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    if s.startswith("NL") and len(s) != 18:
        return False
    moved = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in moved)
    return int(digits) % 97 == 1


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def nl(x):
    """Dutch money notation: 1.234,56"""
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def flag(level, field, message):
    return {"level": level, "field": field, "message": message}


def run_checks(fields, others=(), history=(), today=None):
    """fields: one document's current values.
    others: fields of the other documents in this batch.
    history: earlier bookings (dicts with supplier_name, invoice_number, invoice_date, gross_total)."""
    today = today or date.today()
    flags = []
    f = fields or {}
    doc_type = f.get("doc_type") or "invoice"

    if doc_type == "other":
        flags.append(flag("warning", "doc_type", "Geen factuur of bon herkend. Hoort dit document in de administratie?"))

    if doc_type == "other":
        return flags + model_flags(f)

    # Required fields
    for key, label in (("supplier_name", "Leverancier"), ("invoice_date", "Factuurdatum"), ("gross_total", "Totaalbedrag")):
        if f.get(key) in (None, ""):
            flags.append(flag("error", key, f"{label} ontbreekt."))
    if not f.get("invoice_number"):
        if doc_type in ("invoice", "credit_note"):
            flags.append(flag("error", "invoice_number", "Factuurnummer ontbreekt (verplicht op een factuur)."))
        else:
            flags.append(flag("info", "invoice_number", "Bon zonder nummer, duplicaatcontrole is minder zeker."))

    # VAT arithmetic
    net, gross = D(f.get("net_total")), D(f.get("gross_total"))
    lines = [l for l in (f.get("vat_lines") or []) if l]
    vat_sum = sum((D(l.get("amount")) or Decimal("0")) for l in lines) if lines else None
    if net is not None and gross is not None and vat_sum is not None:
        diff = net + vat_sum - gross
        if abs(diff) > TOL:
            flags.append(flag("error", "gross_total",
                              f"Rekenfout: excl. {nl(net)} + btw {nl(vat_sum)} = {nl(net + vat_sum)}, maar het totaal op de factuur is {nl(gross)}."))
    if net is not None and lines and all(D(l.get("base")) is not None for l in lines):
        base_sum = sum((D(l.get("base")) or Decimal("0")) for l in lines)
        if abs(base_sum - net) > TOL:
            flags.append(flag("warning", "net_total", f"Som van de btw-grondslagen ({nl(base_sum)}) wijkt af van het bedrag excl. btw ({nl(net)})."))
    for i, l in enumerate(lines, 1):
        rate, base, amount = D(l.get("rate")), D(l.get("base")), D(l.get("amount"))
        if rate is None:
            continue
        if base is not None and amount is not None:
            expected = (base * rate / 100).quantize(Decimal("0.01"))
            if abs(expected - amount) > TOL:
                flags.append(flag("error", "vat_lines",
                                  f"Btw klopt niet: {rate.normalize():f}% van {nl(base)} is {nl(expected)}, op de factuur staat {nl(amount)}. Niet stilzwijgend corrigeren: maximaal {nl(expected)} aftrekken en een correctiefactuur vragen."))

    # Supplier identifiers
    vat_no = re.sub(r"[\s.]", "", (f.get("supplier_vat_number") or "")).upper()
    foreign = bool(vat_no) and not vat_no.startswith("NL")
    if vat_no.startswith("NL") and not re.fullmatch(r"NL\d{9}B\d{2}", vat_no):
        flags.append(flag("warning", "supplier_vat_number", f"Btw-nummer {vat_no} heeft geen geldig NL-formaat (NL123456789B01)."))
    for l in lines:
        rate = D(l.get("rate"))
        if rate is not None and rate not in NL_VAT_RATES and not foreign:
            flags.append(flag("warning", "vat_lines", f"Btw-tarief {rate.normalize():f}% bestaat niet in Nederland."))
            break
    if foreign and vat_sum and vat_sum > 0:
        flags.append(flag("warning", "vat_lines",
                          "Buitenlandse leverancier rekent buitenlandse btw. Niet aftrekbaar in de NL-aangifte, controleer boeking."))
    elif foreign and not vat_sum:
        flags.append(flag("info", "vat_lines", "Buitenlandse leverancier zonder btw: mogelijk btw verlegd (rubriek 4)."))
    kvk = re.sub(r"\s", "", f.get("supplier_kvk") or "")
    if kvk and not re.fullmatch(r"\d{8}", kvk):
        flags.append(flag("warning", "supplier_kvk", f"KvK-nummer {kvk} heeft geen 8 cijfers."))
    if f.get("iban") and not iban_valid(f["iban"]):
        flags.append(flag("error", "iban", f"IBAN {f['iban']} klopt niet (controlegetal ongeldig)."))

    # Dates
    inv_date, due = parse_date(f.get("invoice_date")), parse_date(f.get("due_date"))
    if f.get("invoice_date") and not inv_date:
        flags.append(flag("error", "invoice_date", "Factuurdatum is geen geldige datum."))
    if inv_date:
        if inv_date > today:
            flags.append(flag("error", "invoice_date", "Factuurdatum ligt in de toekomst."))
        elif inv_date < today - timedelta(days=365):
            flags.append(flag("warning", "invoice_date", "Factuur is ouder dan een jaar. Hoort deze in deze periode?"))
    if inv_date and due and due < inv_date:
        flags.append(flag("warning", "due_date", "Vervaldatum ligt vóór de factuurdatum."))
    if (f.get("currency") or "EUR").upper() != "EUR":
        flags.append(flag("info", "currency", f"Valuta is {f.get('currency')}, omrekening naar EUR nodig."))

    # Duplicates
    sup, num = norm(f.get("supplier_name")), norm(f.get("invoice_number"))
    for label, pool in (("al in deze aanlevering", others), ("al in de administratie", history)):
        for o in pool:
            if not sup or norm(o.get("supplier_name")) != sup:
                continue
            if num and norm(o.get("invoice_number")) == num:
                flags.append(flag("error", "invoice_number",
                                  f"Dubbel: {f.get('supplier_name')} factuur {f.get('invoice_number')} staat {label}."))
                break
            if gross is not None and D(o.get("gross_total")) == gross and parse_date(o.get("invoice_date")) == inv_date:
                flags.append(flag("warning", "invoice_number",
                                  f"Mogelijk dubbel: zelfde leverancier, datum en bedrag staat {label}."))
                break

    if doc_type in ("invoice", "receipt") and gross is not None and not lines and not foreign:
        flags.append(flag("warning", "vat_lines", "Geen btw gevonden op het document. Zonder btw op de factuur geen aftrek."))
    for i, l in enumerate(lines, 1):
        if l.get("amount") in (None, "") and l.get("rate") not in (None, ""):
            flags.append(flag("warning", "vat_lines", f"Btw-bedrag regel {i} niet leesbaar. Niet zelf uitrekenen: controleren of opvragen."))

    return flags + model_flags(f)


def model_flags(f):
    """What the AI itself was unsure about or wants a person to decide."""
    flags = []
    for name in f.get("uncertain_fields") or []:
        label = LABELS.get(name, name)
        flags.append(flag("warning", name, f"AI twijfelt over {label}: controleer tegen het document."))
    for issue in f.get("issues") or []:
        if isinstance(issue, dict):
            flags.append(flag(issue.get("level", "warning"), None, issue.get("message", "")))
        else:
            flags.append(flag("warning", None, issue))
    return flags


def status_of(flags):
    levels = {x["level"] for x in flags}
    return "red" if "error" in levels else "amber" if "warning" in levels else "green"
