"""The /veiligheid page: what the privacy step and the egress gate do, shown with the real code on fictional text.

Everything here runs the same functions the product uses (privacy.rule_findings, privacy.Pseudonymiser,
privacy.special_categories, egress.inspect). One honest exception: names of people and home addresses are found by
the local language model (privacy.model_findings), which does not run on this public website. For the two fixed
examples those findings are written out below and marked as such on the page; the live test field only runs the
fixed rules, and says so.
"""
import hashlib
import inspect as pyinspect
import re

import checks
import egress
import privacy
import ubl

MAX_LIVE = 4000

# Fictional documents. NL91ABNA0417164300 is the well-known example IBAN from the IBAN standard (it passes mod-97).
SAMPLES = {
    "fotograaf": {
        "title": "Factuur van een eenmanszaak",
        "sub": "Fotograaf, persoonsnaam in de bedrijfsnaam",
        "text": (
            "Fotografie Noorderlicht - Jan de Vries\n"
            "Kerkstraat 12, 1234 AB Lindewerf\n"
            "Tel. 06-12345678 | j.devries@noorderlicht.example\n"
            "KvK 81234567 | Btw-id NL001234567B01\n"
            "\n"
            "Factuur 2026-031          Datum 18-09-2026\n"
            "Aan: Studio Lindewerf B.V., t.a.v. administratie\n"
            "\n"
            "Productfotografie webshop, 1 dag      650,00\n"
            "Nabewerking 40 foto's                 180,00\n"
            "Subtotaal                             830,00\n"
            "Btw 21%                               174,30\n"
            "Totaal                              1.004,30\n"
            "\n"
            "Graag binnen 14 dagen op NL91ABNA0417164300 t.n.v. J. de Vries."
        ),
        # What the local model returns for this text (fixed outcome: the model does not run on this website).
        "model": [("PERSOON", "Jan de Vries"), ("PERSOON", "J. de Vries"), ("ADRES", "Kerkstraat 12, 1234 AB Lindewerf")],
    },
    "fysio": {
        "title": "Nota van een fysiotherapeut",
        "sub": "Gezondheidsgegevens (AVG art. 9)",
        "text": (
            "Fysiotherapie De Linde\n"
            "Lindelaan 4, 1234 CD Lindewerf\n"
            "\n"
            "Nota 2026-1180            Datum 22-09-2026\n"
            "Aan: Studio Lindewerf B.V.\n"
            "Betreft: bedrijfsfysiotherapie medewerker, 3 behandelingen\n"
            "\n"
            "3 x behandeling a 42,00               126,00\n"
            "Btw: vrijgesteld (art. 11 Wet OB)\n"
            "Totaal                                126,00\n"
            "\n"
            "IBAN NL91ABNA0417164300"
        ),
        "model": [],
    },
}


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run(text, model_findings=()):
    """The three views of one document: the original, what the gate would let out, and local-only mode."""
    rules = privacy.rule_findings(text)
    special = privacy.special_categories(text)
    gate_original = egress.inspect(text)
    p = privacy.Pseudonymiser()
    filtered = p.apply(text, list(rules) + list(model_findings))
    gate_filtered = egress.inspect(filtered)
    stays_local = bool(special)
    return {
        "original": text,
        "filtered": "" if stays_local else filtered,
        "replaced": [{"token": t, "kind": t.strip("[]").rsplit("_", 1)[0], "by": "model" if (k, v) in set(model_findings) else "regel"}
                     for t, v in p.mapping.items() for k in [t.strip("[]").rsplit("_", 1)[0]]],
        "special": special,
        "gate_original": gate_original,
        "gate_filtered": gate_filtered,
        "stays_local": stays_local,
        "sent_chars": 0 if stays_local or gate_filtered else len(filtered),
        "sha256": "" if stays_local or gate_filtered else _digest(filtered),
    }


def samples():
    return {key: {"title": s["title"], "sub": s["sub"], **run(s["text"], s["model"])} for key, s in SAMPLES.items()}


def live(text):
    """The visitor's own test text: fixed rules only (no local model on this website). Nothing is stored."""
    text = (text or "")[:MAX_LIVE]
    out = run(text)
    out["note"] = ("Alleen de vaste regels draaien hier (IBAN, e-mail, telefoon, BSN, geboortedatum, bijzondere gegevens). "
                   "Namen en woonadressen vindt bij een kantoor het lokale taalmodel; dat draait niet op deze website.")
    return out


def _source(fn, between=None):
    """Source of a function from the running code, or the part from one marker line up to (not including) another."""
    lines, start = pyinspect.getsourcelines(fn)
    mod = pyinspect.getmodule(fn).__name__ + ".py"
    if between:
        a = next(i for i, ln in enumerate(lines) if between[0] in ln)
        b = next(i for i, ln in enumerate(lines) if i > a and between[1] in ln)
        lines, start = lines[a:b], start + a
    return {"file": mod, "start": start, "code": "".join(lines).rstrip()}


def code_blocks():
    """The rules shown on /veiligheid are read from the running code, so the page cannot drift from the product."""
    return [
        {"title": "IBAN-controle (modulo 97)", "why": "Een overgetypt of vervalst rekeningnummer valt hier al af, zonder AI.",
         **_source(checks.iban_valid)},
        {"title": "Rekenregels uit EN 16931 op elk exportbestand", "why": "Elk SI-UBL-bestand wordt nagerekend voordat het uw pakket in gaat.",
         **_source(ubl.verify, between=('rule("BR-CO-10"', 'rule("BR-CO-16"'))},
        {"title": "De poort naar een extern taalmodel", "why": "Een tweede, losse controle vlak voor verzending. Vindt hij iets, dan gaat er niets weg.",
         **_source(egress.inspect)},
    ]


def iban_demo():
    """Three IBANs through the real check: valid, one digit off, and a changed account for a known supplier."""
    good = "NL91ABNA0417164300"
    typo = good[:-1] + "1"
    return [{"iban": good, "ok": checks.iban_valid(good), "label": "Geldig"},
            {"iban": typo, "ok": checks.iban_valid(typo), "label": "Eén cijfer anders"}]

