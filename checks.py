"""Deterministic checks. No AI here: every flag comes from arithmetic or exact rules.

Each check returns flags of the form
    {"level": "error"|"warning"|"info", "field": str|None, "message": str}
Messages are in Dutch because the reviewer is.
"""
import hashlib
import re
from collections import Counter
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


def flag_id(f):
    """Stable id of a flag: the same finding on the same document keeps its id, so a reviewer's note stays attached."""
    return hashlib.sha1(f"{f.get('level')}|{f.get('field')}|{f.get('message')}".encode()).hexdigest()[:10]


# Booking codes that say "no deductible VAT on purpose": then a document without VAT lines is not a warning.
NO_VAT_CODES = {"NL_VERL": "binnenlands verlegd", "EU_DIENST_VERL": "dienst uit de EU, verlegd", "EU_GOED_VERL": "goederen uit de EU",
                "GEEN": "geen btw (bank, verzekering)", "V0": "geen btw op de factuur", "PRIVE": "privé, buiten de btw",
                "GEEN_AFTREK": "btw niet aftrekbaar"}


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

    # IBAN against earlier documents of the same supplier: "our bank details have changed" is the classic invoice fraud
    iban = re.sub(r"\s+", "", f.get("iban") or "").upper()
    if sup and iban and iban_valid(iban):
        known = {re.sub(r"\s+", "", o.get("iban") or "").upper() for o in list(others) + list(history)
                 if norm(o.get("supplier_name")) == sup}
        known.discard("")
        if known and iban not in known:
            # An error, not a warning: paying to a changed account number is the one mistake that cannot be undone.
            flags.append(flag("error", "iban", f"IBAN {iban} wijkt af van eerdere stukken van {f.get('supplier_name')} "
                                               f"({', '.join(sorted(known))}). Niet betalen voordat het nummer is "
                                               "geverifieerd via een bekend telefoonnummer (terugbelcontrole)."))

    if doc_type in ("invoice", "receipt") and gross is not None and not lines and not foreign:
        codes = [c for c in f.get("no_vat_codes") or [] if c in NO_VAT_CODES]
        if codes:
            flags.append(flag("info", "vat_lines", "Geen btw op het document: geboekt zonder voorbelasting ("
                              + ", ".join(sorted({NO_VAT_CODES[c] for c in codes})) + ")."))
        else:
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


# ---------------------------------------------------------------- what a reviewer wants to see confirmed
def evidence(lines, supplier, history, names=None):
    """How the booking proposal relates to this supplier's earlier bookings. Counted from the ledger history, not an
    AI confidence: "14 of 14 earlier bookings of this supplier are on 7000". Returns None when there is no proposal."""
    proposed = sorted({str(l.get("account")) for l in lines or [] if l.get("account")})
    if not proposed:
        return None
    names = names or {}
    label = " + ".join(f"{a} {names[a]}" if names.get(a) else a for a in proposed)
    sup = norm(supplier)
    rows = [h for h in history or [] if sup and norm(h.get("supplier_name")) == sup and h.get("account")]
    if not rows:
        return {"status": "new", "same": 0, "total": 0, "share": None, "accounts": proposed, "usual": None,
                "text": "Nieuwe crediteur: nog geen eerdere boekingen om het voorstel mee te vergelijken."}
    used = Counter("+".join(sorted(str(h["account"]).split("+"))) for h in rows)
    key = "+".join(proposed)
    same, total = used.get(key, 0), len(rows)
    usual = used.most_common(1)[0][0]
    usual_label = " + ".join(f"{a} {names[a]}" if names.get(a) else a for a in usual.split("+"))
    if same == total:
        status, text = "match", f"{same}x eerder geboekt op {label}: alle eerdere facturen van deze crediteur."
    elif same:
        status, text = "partial", f"{same} van {total} eerdere boekingen op {label}; meestal op {usual_label}."
    else:
        status, text = "differs", f"Wijkt af van de historie: {total}x eerder geboekt op {usual_label}, nu voorgesteld op {label}."
    return {"status": status, "same": same, "total": total, "share": round(100 * same / total), "accounts": proposed,
            "usual": usual.split("+"), "text": text}


def badges(f, flags, others=(), history=()):
    """The positive side of the checks: what was verified and came out right. Each badge is
    {"key", "ok": True|False|None, "label", "detail"}; ok None means "nothing to compare with yet"."""
    out = []
    f = f or {}
    if (f.get("doc_type") or "invoice") == "other":
        return out
    errors = {x.get("field") for x in flags if x["level"] == "error"}
    net, gross = D(f.get("net_total")), D(f.get("gross_total"))
    lines = [l for l in (f.get("vat_lines") or []) if l]
    vat_sum = sum((D(l.get("amount")) or Decimal("0")) for l in lines) if lines else None
    if gross is not None:
        if errors & {"gross_total", "vat_lines"}:
            out.append({"key": "btw", "ok": False, "label": "Btw-som klopt niet", "detail": "Zie de rode vlag."})
        elif lines and net is not None and net + vat_sum == gross:
            rates = ", ".join(f"{D(l.get('rate')).normalize():f}%" for l in lines if D(l.get("rate")) is not None)
            out.append({"key": "btw", "ok": True, "label": "Btw-som klopt op de cent",
                        "detail": f"{nl(net)} + {nl(vat_sum)} = {nl(gross)} ({rates})"})
        elif not lines:
            out.append({"key": "btw", "ok": None, "label": "Geen btw op het document",
                        "detail": "Verlegd, vrijgesteld of niet aftrekbaar: zie de btw-code van de boekingsregel."})
    sup = norm(f.get("supplier_name"))
    compared = [o for o in list(others) + list(history) if sup and norm(o.get("supplier_name")) == sup]
    dup = [x for x in flags if x.get("field") == "invoice_number" and "ubbel" in (x.get("message") or "")]
    year = (parse_date(f.get("invoice_date")) or date.today()).year
    if dup:
        out.append({"key": "dubbel", "ok": False, "label": "Mogelijk dubbel", "detail": dup[0]["message"]})
    elif f.get("invoice_number") or gross is not None:
        out.append({"key": "dubbel", "ok": True, "label": f"Geen dubbele factuur in {year}",
                    "detail": f"Vergeleken met {len(compared)} eerdere stukken van deze crediteur "
                              f"en {len(list(others))} stukken uit deze aanlevering."})
    iban = re.sub(r"\s+", "", f.get("iban") or "").upper()
    if iban:
        known = [re.sub(r"\s+", "", o.get("iban") or "").upper() for o in compared]
        known = [k for k in known if k]
        if "iban" in errors:
            out.append({"key": "iban", "ok": False, "label": "IBAN wijkt af of klopt niet", "detail": "Zie de rode vlag."})
        elif known:
            out.append({"key": "iban", "ok": True, "label": f"IBAN gelijk aan {len(known)} eerdere facturen",
                        "detail": "Zelfde rekeningnummer als op de eerdere stukken van deze crediteur."})
        else:
            out.append({"key": "iban", "ok": None, "label": "IBAN geldig, eerste factuur",
                        "detail": "Controlegetal klopt. Nog geen eerdere factuur van deze crediteur om mee te vergelijken."})
    vat_no = re.sub(r"[\s.]", "", (f.get("supplier_vat_number") or "")).upper()
    kvk = re.sub(r"\s", "", f.get("supplier_kvk") or "")
    if vat_no or kvk:
        bad = errors & {"supplier_vat_number", "supplier_kvk"} or any(
            x.get("field") in ("supplier_vat_number", "supplier_kvk") for x in flags if x["level"] == "warning")
        parts = []
        if kvk:
            parts.append(f"KvK {kvk}")
        if vat_no:
            parts.append(f"btw-nummer {vat_no}" + ("" if vat_no.startswith("NL") else f" ({vat_no[:2]}, buitenland)"))
        out.append({"key": "ids", "ok": not bad, "label": "KvK- en btw-nummer: geldig formaat" if not bad else
                    "KvK- of btw-nummer: formaat klopt niet",
                    "detail": ", ".join(parts) + ". Alleen het formaat is gecontroleerd, niet het register."})
    return out
