"""Bonnenjacht: which bank lines have no document yet, and the question that goes to the client about them.

Plain code, no AI. A bank line is matched to a document of the same client on the exact amount; the date and the
name in the description only break ties. What stays unmatched is the list a bookkeeper normally discovers at the
VAT deadline. Asking the client is never automatic: text() only writes the draft that a staff member approves
in the outbox (Wwft, see outbox.py).

Bank lines come from a CAMT.053 file the firm exports from the bank (parse_camt), or, on a demo instance, from the
fictional samples/bank.json.
"""
import re
from datetime import date, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
TYPES = {"pin": "Pin", "incasso": "Incasso", "ideal": "iDEAL", "overboeking": "Overboeking", "bij": "Bijschrijving"}
STOP = {"bv", "b", "v", "nv", "vof", "the", "de", "het", "van", "en", "nl", "betaling", "pin", "ideal", "incasso"}


def _d(v):
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001 - a line without a usable amount simply never matches
        return None


def _date(v):
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in STOP}


def shop(line):
    """The name a client recognises: the merchant when the bank gives one, else the description without the
    reference the bank adds."""
    if isinstance(line, dict) and line.get("name"):
        return line["name"]
    description = line.get("description") if isinstance(line, dict) else line
    words = (description or "").split()
    keep = []
    for w in words:
        if re.fullmatch(r"[\d./-]{4,}", w):
            break
        keep.append(w)
    return " ".join(keep) or (description or "")


def match(lines, docs):
    """lines: bank lines of one client. docs: that client's documents as dicts with id, kind, status, counterparty,
    doc_date, gross_total. Returns {line id: doc id or None}; every document is used at most once."""
    free = [d for d in docs if d.get("status") != "rejected" and _d(d.get("gross_total")) is not None]
    out = {}
    for line in sorted(lines, key=lambda l: l.get("date") or ""):
        amount, when = _d(line.get("amount")), _date(line.get("date"))
        if amount is None:
            out[line["id"]] = None
            continue
        sales = amount > 0
        best, best_score = None, None
        for d in free:
            if (d.get("kind") == "verkoopfactuur") != sales or abs(_d(d["gross_total"])) != abs(amount):
                continue
            dd = _date(d.get("doc_date"))
            gap = abs((when - dd).days) if when and dd else 30
            if gap > 62:      # an invoice paid two months later is still the same invoice; later than that is a guess
                continue
            name = len(_words(line.get("description")) & _words(d.get("counterparty")))
            score = (-name, gap)
            if best_score is None or score < best_score:
                best, best_score = d, score
        out[line["id"]] = best["id"] if best else None
        if best:
            free.remove(best)
    return out


def quarter(d):
    return f"Q{(d.month - 1) // 3 + 1}"


def text(line, client, firm):
    """The WhatsApp or e-mail question about one bank line without a document. Short, factual, in the firm's name."""
    when = _date(line.get("date")) or date.today()
    amount = f"{abs(_d(line.get('amount')) or 0):.2f}".replace(".", ",")
    first = (client.get("contact") or "").split(" ")[0] or "daar"
    return (f"Hoi {first}, voor de btw-aangifte van {quarter(when)} mist nog 1 bon van € {amount} bij {shop(line)} "
            f"op {when.day} {MONTHS[when.month - 1]}. Stuur hier direct een foto terug, dan zetten wij hem klaar. Groet, {firm}")


def parse_camt(data: bytes, prefix="B"):
    """Booked entries of a CAMT.053 statement as bank lines. Namespace-agnostic: banks ship several versions."""
    root = ET.fromstring(data)

    def tag(el):
        return el.tag.rsplit("}", 1)[-1]

    def find(el, *names):
        for name in names:
            el = next((c for c in el if tag(c) == name), None) if el is not None else None
        return el

    lines = []
    for n, entry in enumerate((e for e in root.iter() if tag(e) == "Ntry"), 1):
        amt, sign = find(entry, "Amt"), find(entry, "CdtDbtInd")
        when = find(entry, "BookgDt", "Dt") or find(entry, "ValDt", "Dt")
        if amt is None or sign is None or when is None or _d(amt.text) is None:
            continue
        info = [e.text.strip() for e in entry.iter() if tag(e) in ("Ustrd", "AddtlNtryInf") and (e.text or "").strip()]
        names = [e.text.strip() for e in entry.iter() if tag(e) == "Nm" and (e.text or "").strip()]
        value = _d(amt.text) * (1 if sign.text.strip().upper() == "CRDT" else -1)
        desc = " ".join(dict.fromkeys(names[:1] + info))[:140]
        low = desc.lower()
        kind = "bij" if value > 0 else "pin" if "betaalautomaat" in low or "pin" in low.split() else \
            "ideal" if "ideal" in low else "incasso" if "incasso" in low else "overboeking"
        lines.append({"id": f"{prefix}-{n:03d}", "date": when.text.strip()[:10], "type": kind, "description": desc,
                      "amount": float(value)})
    return lines
