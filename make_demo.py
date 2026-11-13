"""Builds the demo dataset "de schoenendoos": one 42-page scan as an office scanner would produce it.

Four fictional dossiers behind QR cover sheets, blank back sides, stapled multi-page invoices, receipts laid on
the scanner glass, a reverse-charge invoice, a supplier whose IBAN suddenly changed, an arithmetic error, a
duplicate and a tax letter. Every party on these documents is invented and every page says so in its footer.

Writes
  samples/schoenendoos_2026-11-12.pdf   the raw scan (what the app processes on first start)
  samples/schoenendoos.json             which pages form which document, the answer key used in replay mode
                                        (no AI call), and where each field sits on the page (normalised boxes)
  samples/reference/<nr>/ledger_history.csv   earlier bookings per dossier (duplicates, known IBANs, usual account)
  samples/bank.json                     fictional bank lines per dossier, for the missing-receipt hunt
  static/demo/...                       the same pages as small images for the public homepage, the client's
                                        reply photos and three files for the "test uw eigen factuur" sandbox

Run:  .venv/bin/python make_demo.py [--debug <folder>]   (debug writes every page with its boxes drawn in)
"""
import csv
import hashlib
import io
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

import checks
import coversheet
import scan

HERE = Path(__file__).parent
OUT = HERE / "samples"
STATIC = HERE / "static" / "demo"
DPI = scan.DPI
PT = DPI / 72
A4PX = (1240, 1754)
PAPER = (250, 250, 248)
AS_OF = "2026-11-13"          # the sample's checks are run as of this day, so the demo never ages
FIRM = "Maaskade Administraties"
STACK = "schoenendoos_2026-11-12.pdf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
rng = random.Random(1042)
W, H = A4


def nl(x):
    return checks.nl(float(x))


def iban(account, bank="TEST", country="NL"):
    bban = bank + account
    digits = "".join(str(int(ch, 36)) for ch in bban + country + "00")
    out = f"{country}{98 - int(digits) % 97:02d}{bban}"
    assert checks.iban_valid(out), out
    return out


def spaced(code):
    return " ".join(code[i:i + 4] for i in range(0, len(code), 4))


def d_nl(iso):
    y, m, d = iso.split("-")
    return f"{d}-{m}-{y}"


MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
          "november", "december"]


def d_long(iso):
    y, m, d = iso.split("-")
    return f"{int(d)} {MONTHS[int(m) - 1]} {y}"


# ---------------------------------------------------------------- parties (all fictional)
CLIENTS = [
    {"nr": "1042", "name": "Bakkerij Van Dijk B.V.", "kvk": "68120934", "vat": "NL859310447B01", "city": "Schiedam",
     "address": "Lange Haven 71, 3111 CD Schiedam", "exact_division": "", "chart": "mkb", "contact": "Sanne",
     "whatsapp": "31600001042", "legal_form": "B.V.", "activity": "Ambachtelijke bakkerij met winkel"},
    {"nr": "1057", "name": "Marco Bouw & Onderhoud", "kvk": "77412093", "vat": "NL003481207B41", "city": "Rotterdam",
     "address": "Schiedamseweg 212, 3025 AS Rotterdam", "exact_division": "", "chart": "mkb", "contact": "Marco",
     "whatsapp": "31600001057", "legal_form": "eenmanszaak", "activity": "Aannemer, renovatie en onderhoud"},
    {"nr": "1063", "name": "Eetcafé De Kade V.O.F.", "kvk": "70233418", "vat": "NL858877120B01", "city": "Rotterdam",
     "address": "Maaskade 48, 3071 NE Rotterdam", "exact_division": "", "chart": "mkb", "contact": "Yasmina",
     "whatsapp": "31600001063", "legal_form": "V.O.F.", "activity": "Eetcafé"},
    {"nr": "1001", "name": "Studio Lindewerf B.V.", "kvk": "90412276", "vat": "NL865512093B01", "city": "Rotterdam",
     "address": "Hoogstraat 118, 3011 PV Rotterdam", "exact_division": "", "chart": "lindewerf", "contact": "Noor",
     "whatsapp": "31600001001", "legal_form": "B.V.", "activity": "Grafisch ontwerp en webdesign"},
    {"nr": "1002", "name": "Fietsenmakerij De Spaak B.V.", "kvk": "93120458", "vat": "NL866230417B01", "city": "Delft",
     "address": "Oude Delft 212, 2611 HJ Delft", "exact_division": "", "chart": "standaard", "contact": "Ruben",
     "whatsapp": "", "legal_form": "B.V.", "activity": "Fietsenwinkel en werkplaats"},
]
C = {c["nr"]: c for c in CLIENTS}


def to_lines(nr, attn=None):
    c = C[nr]
    street, place = c["address"].split(", ")
    return [c["name"]] + ([attn] if attn else []) + [street, place]


S = {  # suppliers: name, address, kvk, vat, iban, colour, style
    "molen": dict(name="Molen De Vier Winden B.V.", addr="Molenkade 3, 2957 LG Nieuw-Lekkerland", kvk="23087714",
                  vat="NL804417290B01", iban=iban("0230877140"), color=(0.42, 0.30, 0.16), style="serif",
                  tag="Meel en bakkerijgrondstoffen sinds 1911"),
    "energie": dict(name="Stroomlijn Energie B.V.", addr="Energieweg 12, 3542 DZ Utrecht", kvk="30241175",
                    vat="NL812204551B01", iban=iban("0302411750"), color=(0.05, 0.45, 0.62), style="logo",
                    tag="Zakelijke energie"),
    "pakhuis": dict(name="Pakhuis Verpakkingen B.V.", addr="Havenstraat 88, 3115 HD Schiedam", kvk="24415520",
                    vat="NL818830127B01", iban=iban("0244155200"), color=(0.80, 0.42, 0.08), style="band",
                    tag="Verpakkingen voor bakker en horeca"),
    "techniek": dict(name="Bakkerijtechniek Zuid B.V.", addr="Ambachtsweg 31, 4906 CH Oosterhout", kvk="20119883",
                     vat="NL807766112B01", iban=iban("0201198830"), color=(0.20, 0.24, 0.32), style="side",
                     tag="Machines, onderhoud en keuring"),
    "belnet": dict(name="Belnet Mobiel B.V.", addr="Telecomplein 5, 2516 CK Den Haag", kvk="71829304",
                   vat="NL858104221B01", iban="NL70TEST4410020011", color=(0.10, 0.25, 0.45), style="band",
                   tag="Zakelijk bellen en internet"),
    "renova": dict(name="Renova Bedrijfsafval B.V.", addr="Vlaardingweg 62, 3044 CK Rotterdam", kvk="24330918",
                   vat="NL815590338B01", iban=iban("0243309180"), color=(0.13, 0.50, 0.30), style="logo",
                   tag="Inzameling en recycling"),
    "bgr": dict(name="Bouwgroothandel Rijnmond B.V.", addr="Waalhaven Oostzijde 77, 3087 BM Rotterdam", kvk="66011820",
                vat="NL856355118B01", iban=iban("0660118204"), color=(0.85, 0.20, 0.10), style="band",
                tag="Alles voor de vakman"),
    "wit": dict(name="Stukadoorsbedrijf Wit", addr="Van Swindenstraat 9, 3117 KA Schiedam", kvk="62204817",
                vat="NL002219874B35", iban=iban("0622048170"), color=(0.35, 0.35, 0.38), style="serif",
                tag="Stucwerk en sierpleister"),
    "polis": dict(name="Polderland Verzekeringen N.V.", addr="Stationsplein 45, 3013 AK Rotterdam", kvk="24099107",
                  vat="", iban=iban("0240991070"), color=(0.10, 0.20, 0.52), style="logo",
                  tag="Zakelijke verzekeringen"),
    "maashal": dict(name="Horecagroothandel Maashal B.V.", addr="Spaanse Polder 14, 3044 AT Rotterdam", kvk="24288530",
                    vat="NL809912446B01", iban=iban("0242885300"), color=(0.05, 0.30, 0.55), style="mono",
                    tag="Zelfbedieningsgroothandel voor horeca"),
    "kuip": dict(name="Slijterij-groothandel De Kuip B.V.", addr="Stadionweg 310, 3077 AS Rotterdam", kvk="24177265",
                 vat="NL803318804B01", iban=iban("0241772650"), color=(0.45, 0.10, 0.20), style="serif",
                 tag="Wijnen, bieren en gedistilleerd"),
    "blinkend": dict(name="Schoonmaakbedrijf Blinkend", addr="Bergweg 144, 3036 BJ Rotterdam", kvk="77120445",
                     vat="NL860998121B01", iban="NL13TEST7712044500", color=(0.10, 0.55, 0.60), style="logo",
                     tag="Schoon werken, beter werken"),
    "pinpunt": dict(name="PinPunt Betaaldiensten B.V.", addr="Kabelweg 21, 1014 BA Amsterdam", kvk="34280116",
                    vat="NL821145903B01", iban=iban("0342801160"), color=(0.35, 0.20, 0.60), style="band",
                    tag="Pinnen aan de toonbank"),
    "vektor": dict(name="Vektorhub Ltd", addr="14 Harbour Row, Dublin 2, D02 XK71, Ireland", kvk="",
                   vat="IE3668997OH", iban=iban("93115212345678", country="IE"), color=(0.25, 0.20, 0.70),
                   style="logo", tag="Design software for teams"),
    "drukkerij": dict(name="Drukkerij Van Wijk B.V.", addr="Zomerhofstraat 71, 3032 CK Rotterdam", kvk="24101199",
                      vat="NL801992311B01", iban="NL48TEST2410119900", color=(0.60, 0.10, 0.35), style="side",
                      tag="Drukwerk en signing"),
}
FRAUD_IBAN = iban("0990447123")


# ---------------------------------------------------------------- drawing A4 documents
class Pen:
    """A reportlab canvas that remembers where tagged fields were drawn (PDF points, per page)."""

    def __init__(self):
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=A4)
        self.pages = [{}]

    def put(self, x, y, text, font="Helvetica", size=10, field=None, align="l", gray=0.0, rgb=None):
        c = self.c
        c.setFont(font, size)
        if rgb:
            c.setFillColorRGB(*rgb)
        else:
            c.setFillGray(gray)
        w = c.stringWidth(text, font, size)
        x0 = x if align == "l" else (x - w if align == "r" else x - w / 2)
        c.drawString(x0, y, text)
        c.setFillGray(0)
        if field:
            self.pages[-1][field] = (x0 - 2.5, y - size * 0.30, x0 + w + 2.5, y + size * 0.86)
        return x0 + w

    def seq(self, x, y, parts, font="Helvetica", size=10, gray=0.0):
        """parts: (text, field|None) drawn one after another on one line."""
        for text, field in parts:
            x = self.put(x, y, text, font, size, field, gray=gray)
        return x

    def footer(self, n=None, total=None):
        self.put(W / 2, 10 * mm, "Fictief voorbeelddocument voor een demo. Bestaat niet echt.", "Helvetica-Oblique", 7,
                 align="c", gray=0.45)
        if total and total > 1:
            self.put(190 * mm, 10 * mm, f"Pagina {n} van {total}", "Helvetica", 7.5, align="r", gray=0.35)

    def page(self):
        self.c.showPage()
        self.pages.append({})

    def done(self):
        self.c.showPage()
        self.c.save()
        return self.buf.getvalue(), self.pages


FAMILY = {"serif": ("Times-Roman", "Times-Bold"), "mono": ("Courier", "Courier-Bold")}


def fonts(style):
    return FAMILY.get(style, ("Helvetica", "Helvetica-Bold"))


def header(p, s, purchase=True, show_iban=True):
    """Letterhead in the sender's style. Tags the sender's name, KvK, VAT number and IBAN when it is a purchase."""
    c, style, col = p.c, s["style"], s["color"]
    reg, bold = fonts(style)
    f = (lambda k: k) if purchase else (lambda k: None)
    ids = []
    if s.get("kvk"):
        ids += [("KvK ", None), (s["kvk"], f("supplier_kvk")), ("   ", None)]
    if s.get("vat"):
        ids += [("Btw " if s["vat"].startswith("NL") else "VAT ", None), (s["vat"], f("supplier_vat_number"))]
    bank = [("IBAN ", None), (spaced(s["iban"]), f("iban"))] if show_iban and s.get("iban") else []
    if style == "band":
        c.setFillColorRGB(*col)
        c.rect(0, H - 8 * mm, W, 8 * mm, stroke=0, fill=1)
        p.put(20 * mm, H - 25 * mm, s["name"], bold, 17, f("counterparty"))
        p.put(20 * mm, H - 30.5 * mm, s["tag"], reg, 8.5, gray=0.4)
        p.put(190 * mm, H - 21 * mm, s["addr"], reg, 8.5, align="r")
        p.seq(190 * mm - c.stringWidth("".join(t for t, _ in ids), reg, 8.5), H - 25.5 * mm, ids, reg, 8.5)
        if bank:
            p.seq(190 * mm - c.stringWidth("".join(t for t, _ in bank), reg, 8.5), H - 30 * mm, bank, reg, 8.5)
    elif style == "logo":
        c.setFillColorRGB(*col)
        c.roundRect(20 * mm, H - 34 * mm, 16 * mm, 16 * mm, 3 * mm, stroke=0, fill=1)
        initials = "".join(w[0] for w in s["name"].split()[:2]).upper()
        p.put(28 * mm, H - 28.4 * mm, initials, "Helvetica-Bold", 15, align="c", rgb=(1, 1, 1))
        p.put(40 * mm, H - 24 * mm, s["name"], bold, 15, f("counterparty"))
        p.put(40 * mm, H - 29.5 * mm, s["tag"], reg, 8.5, gray=0.4)
        p.put(190 * mm, H - 20 * mm, s["addr"], reg, 8, align="r", gray=0.25)
        p.seq(190 * mm - c.stringWidth("".join(t for t, _ in ids), reg, 8), H - 24.5 * mm, ids, reg, 8, gray=0.25)
        if bank:
            p.seq(190 * mm - c.stringWidth("".join(t for t, _ in bank), reg, 8), H - 29 * mm, bank, reg, 8, gray=0.25)
        c.setStrokeGray(0.75)
        c.setLineWidth(0.6)
        c.line(20 * mm, H - 39 * mm, 190 * mm, H - 39 * mm)
        c.setStrokeGray(0)
    elif style == "serif":
        p.put(W / 2, H - 22 * mm, s["name"], bold, 20, f("counterparty"), align="c")
        c.setLineWidth(1.1)
        c.line(20 * mm, H - 25.5 * mm, 190 * mm, H - 25.5 * mm)
        c.setLineWidth(0.4)
        c.line(20 * mm, H - 26.6 * mm, 190 * mm, H - 26.6 * mm)
        p.put(W / 2, H - 31 * mm, f"{s['tag']}  ·  {s['addr']}", reg, 9, align="c")
        line = ids + ([("   ", None)] + bank if bank else [])
        p.seq(W / 2 - c.stringWidth("".join(t for t, _ in line), reg, 9) / 2, H - 35.5 * mm, line, reg, 9)
    elif style == "side":
        c.setFillColorRGB(*col)
        c.rect(0, 0, 11 * mm, H, stroke=0, fill=1)
        p.put(22 * mm, H - 24 * mm, s["name"], bold, 18, f("counterparty"))
        p.put(22 * mm, H - 29.5 * mm, s["tag"], reg, 8.5, gray=0.4)
        p.put(22 * mm, H - 36 * mm, s["addr"], reg, 8.5)
        x = p.seq(22 * mm, H - 40.5 * mm, ids, reg, 8.5)
        if bank:
            p.seq(x + 8, H - 40.5 * mm, bank, reg, 8.5)
    else:  # mono: a wholesaler's system print
        c.setLineWidth(0.8)
        c.rect(20 * mm, H - 40 * mm, 170 * mm, 24 * mm, stroke=1, fill=0)
        p.put(24 * mm, H - 24 * mm, s["name"].upper(), bold, 15, f("counterparty"))
        p.put(24 * mm, H - 29 * mm, s["tag"], reg, 8.5)
        p.put(24 * mm, H - 33.5 * mm, s["addr"], reg, 8.5)
        x = p.seq(24 * mm, H - 37.5 * mm, ids, reg, 8.5)
        if bank:
            p.seq(x + 10, H - 37.5 * mm, bank, reg, 8.5)


def address(p, lines, style, field=None, y=None):
    reg, _ = fonts(style)
    y = y or H - 58 * mm
    for i, l in enumerate(lines):
        p.put(20 * mm + (2 * mm if style == "side" else 0), y - i * 5 * mm, l, reg, 10, field if i == 0 else None)


def meta(p, rows, style, y=None):
    reg, _ = fonts(style)
    y = y or H - 92 * mm
    for i, (k, v, field) in enumerate(rows):
        p.put(120 * mm, y - i * 5 * mm, k, reg, 9, gray=0.25)
        p.put(155 * mm, y - i * 5 * mm, v, reg, 9, field)


COLS = (20, 112, 132, 152, 190)  # description, qty, price, vat, amount (mm; the last three right-aligned)


def table_head(p, y, style, vat_col):
    reg, bold = fonts(style)
    x0 = 22 if style == "side" else 20
    p.put(x0 * mm, y, "Omschrijving", bold, 9)
    p.put(COLS[1] * mm, y, "Aantal", bold, 9, align="r")
    p.put(COLS[2] * mm + 8 * mm, y, "Prijs", bold, 9, align="r")
    if vat_col:
        p.put(COLS[3] * mm + 8 * mm, y, "Btw", bold, 9, align="r")
    p.put(COLS[4] * mm, y, "Bedrag", bold, 9, align="r")
    p.c.setLineWidth(0.6)
    p.c.line(x0 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)


def table_row(p, y, r, style, vat_col):
    reg, _ = fonts(style)
    x0 = 22 if style == "side" else 20
    desc, qty, price, amount = r[:4]
    p.put(x0 * mm, y, desc, reg, 9)
    p.put(COLS[1] * mm, y, str(qty), reg, 9, align="r")
    p.put(COLS[2] * mm + 8 * mm, y, price, reg, 9, align="r")
    if vat_col and len(r) > 4:
        p.put(COLS[3] * mm + 8 * mm, y, r[4], reg, 9, align="r")
    p.put(COLS[4] * mm, y, amount, reg, 9, align="r")


def totals(p, y, items, style):
    reg, bold = fonts(style)
    for i, (label, val, strong, field) in enumerate(items):
        yy = y - i * 6 * mm
        if strong:
            p.c.setLineWidth(0.8)
            p.c.line(125 * mm, yy + 4.6 * mm, 190 * mm, yy + 4.6 * mm)
        p.put(125 * mm, yy, label, bold if strong else reg, 10.5 if strong else 10)
        p.put(190 * mm, yy, val, bold if strong else reg, 10.5 if strong else 10, field, align="r")
    return y - len(items) * 6 * mm


def notes(p, y, items, style):
    """items: a string, or a list of (text, field) parts for one line. Returns the y below the block."""
    reg, bold = fonts(style)
    x0 = (22 if style == "side" else 20) * mm
    for it in items:
        if isinstance(it, str):
            p.put(x0, y, it, reg, 9)
        elif isinstance(it, dict):  # a boxed notice
            p.c.setLineWidth(1)
            p.c.rect(x0, y - 9 * mm, 170 * mm - (x0 - 20 * mm), 12.5 * mm, stroke=1, fill=0)
            p.put(x0 + 3 * mm, y - 1.6 * mm, it["title"], bold, 9.5)
            p.seq(x0 + 3 * mm, y - 6.4 * mm, it["parts"], reg, 9)
            y -= 9 * mm
        else:
            p.seq(x0, y, it, reg, 9)
        y -= 5 * mm
    return y


def invoice(spec):
    """A complete invoice in the sender's house style, over as many pages as the rows need.
    spec: sender (S entry), to, purchase, title, meta, rows, vat_col, totals, notes, extra (draw functions for
    following pages), row_h. Returns (pdf bytes, boxes per page)."""
    p = Pen()
    s, style = spec["sender"], spec["sender"]["style"]
    purchase = spec.get("purchase", True)
    rows, row_h = spec["rows"], spec.get("row_h", 6) * mm
    first_cap = int((H - 125 * mm - 8 * mm - spec.get("reserve", 62) * mm) // row_h)
    next_cap = int((H - 52 * mm - 30 * mm) // row_h)
    chunks, rest = [rows[:first_cap]], rows[first_cap:]
    while rest:
        chunks.append(rest[:next_cap])
        rest = rest[next_cap:]
    # the totals block needs room under the last rows: otherwise it gets a page of its own
    need = (len(spec["totals"]) * 6 + len(spec.get("notes", [])) * 5 + 26) * mm
    last_top = (H - 125 * mm) if len(chunks) == 1 else (H - 52 * mm)
    if last_top - 8 * mm - len(chunks[-1]) * row_h - need < 16 * mm:
        chunks.append([])
    total_pages = len(chunks) + len(spec.get("extra", []))
    for n, chunk in enumerate(chunks, 1):
        if n == 1:
            header(p, s, purchase, spec.get("show_iban", True))
            address(p, spec["to"], style, None if purchase else "counterparty")
            reg, bold = fonts(style)
            p.put((22 if style == "side" else 20) * mm, H - 92 * mm, spec.get("title", "FACTUUR"), bold, 14)
            meta(p, spec["meta"], style)
            y = H - 125 * mm
        else:
            p.page()
            reg, bold = fonts(style)
            p.put((22 if style == "side" else 20) * mm, H - 24 * mm, s["name"], bold, 12)
            p.put(190 * mm, H - 24 * mm, f"Vervolg {spec.get('title', 'factuur').lower()} {spec['number']}", reg, 9,
                  align="r")
            p.c.setLineWidth(0.4)
            p.c.line(20 * mm, H - 28 * mm, 190 * mm, H - 28 * mm)
            if style == "side":
                p.c.setFillColorRGB(*s["color"])
                p.c.rect(0, 0, 11 * mm, H, stroke=0, fill=1)
                p.c.setFillGray(0)
            y = H - 44 * mm
        if chunk:
            table_head(p, y, style, spec.get("vat_col", False))
            for i, r in enumerate(chunk):
                table_row(p, y - 8 * mm - i * row_h, r, style, spec.get("vat_col", False))
            y = y - 8 * mm - len(chunk) * row_h
            if n < len(chunks) and chunks[n]:
                p.put(190 * mm, y - 4 * mm, "Transporteren naar volgende pagina", reg, 8, align="r", gray=0.35)
        if n == len(chunks):
            y = totals(p, y - 8 * mm, spec["totals"], style)
            notes(p, min(y - 8 * mm, H - 205 * mm) if len(chunks) == 1 and not chunk else y - 8 * mm,
                  spec.get("notes", []), style)
        p.footer(n, total_pages)
    for k, fn in enumerate(spec.get("extra", []), len(chunks) + 1):
        p.page()
        fn(p, spec)
        p.footer(k, total_pages)
    return p.done()


# ---------------------------------------------------------------- special pages
def energy_page2(p, spec):
    s = spec["sender"]
    header(p, s, purchase=False, show_iban=False)
    p.put(20 * mm, H - 52 * mm, "Specificatie verbruik", "Helvetica-Bold", 13)
    p.put(20 * mm, H - 58 * mm, f"Bij factuur {spec['number']} · leveringsadres Lange Haven 71, Schiedam · periode oktober 2026",
          "Helvetica", 9, gray=0.25)
    heads = ("Meter", "Beginstand", "Eindstand", "Verbruik", "Tarief", "Bedrag")
    xs = (20, 78, 104, 130, 156, 190)
    y = H - 72 * mm
    for x, t in zip(xs, heads):
        p.put(x * mm, y, t, "Helvetica-Bold", 9, align="l" if x == 20 else "r")
    p.c.line(20 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)
    rows = [("Elektra normaaltarief (kWh)", "48.211", "49.102", "891", "0,2931", "261,15"),
            ("Elektra daltarief (kWh)", "31.870", "32.461", "591", "0,2555", "151,03"),
            ("Gas (m³)", "12.044", "12.342", "298", "0,9611", "286,40"),
            ("Vastrecht levering", "", "", "1 mnd", "8,90", "8,90"),
            ("Netbeheer en meterhuur", "", "", "1 mnd", "15,60", "15,60")]
    for i, r in enumerate(rows):
        for x, t in zip(xs, r):
            p.put(x * mm, y - 8 * mm - i * 6 * mm, t, "Helvetica", 9, align="l" if x == 20 else "r")
    yy = y - 8 * mm - len(rows) * 6 * mm - 4 * mm
    p.c.line(130 * mm, yy + 3 * mm, 190 * mm, yy + 3 * mm)
    p.put(130 * mm, yy - 2 * mm, "Totaal excl. btw", "Helvetica-Bold", 9.5)
    p.put(190 * mm, yy - 2 * mm, "723,08", "Helvetica-Bold", 9.5, align="r")
    text = ["Tarieven zijn inclusief energiebelasting en opslag duurzame energie, exclusief btw.",
            "Uw voorschot wordt jaarlijks in maart opnieuw berekend op basis van het werkelijke verbruik.",
            "Vragen over deze specificatie: zakelijk@stroomlijn-voorbeeld.nl of 030 - 555 01 42."]
    for i, t in enumerate(text):
        p.put(20 * mm, yy - 18 * mm - i * 5.5 * mm, t, "Helvetica", 9)


def polis_page2(p, spec):
    s = spec["sender"]
    header(p, s, purchase=False, show_iban=False)
    p.put(20 * mm, H - 52 * mm, "Polisblad Aansprakelijkheidsverzekering Bedrijven (AVB)", "Helvetica-Bold", 13)
    p.put(20 * mm, H - 58 * mm, f"Polisnummer {spec['policy']} · behorend bij premienota {spec['number']}", "Helvetica", 9,
          gray=0.25)
    rows = [("Verzekeringnemer", "Marco Bouw & Onderhoud, Schiedamseweg 212, 3025 AS Rotterdam"),
            ("Verzekerde hoedanigheid", "Aannemersbedrijf: renovatie, onderhoud en isolatie van woningen"),
            ("Verzekerd bedrag", "EUR 2.500.000 per aanspraak, EUR 5.000.000 per verzekeringsjaar"),
            ("Eigen risico", "EUR 500 per aanspraak (zaakschade)"),
            ("Verzekeringstermijn", "1 november 2026 tot 1 november 2027, daarna jaarlijks verlengd"),
            ("Premie", "EUR 640,00 per jaar, exclusief 21% assurantiebelasting"),
            ("Clausules", "AVB-2024, brandregresclausule, clausule onderaannemers"),
            ("Opzegging", "Dagelijks opzegbaar na het eerste jaar, opzegtermijn 1 maand")]
    y = H - 74 * mm
    for i, (k, v) in enumerate(rows):
        p.put(20 * mm, y - i * 9 * mm, k, "Helvetica-Bold", 9.5)
        p.put(70 * mm, y - i * 9 * mm, v, "Helvetica", 9.5)
        p.c.setStrokeGray(0.8)
        p.c.line(20 * mm, y - i * 9 * mm - 3 * mm, 190 * mm, y - i * 9 * mm - 3 * mm)
        p.c.setStrokeGray(0)
    text = ["Assurantiebelasting is geen omzetbelasting en kan niet als voorbelasting worden afgetrokken.",
            "Schade melden: binnen 3 werkdagen via schade@polderland-voorbeeld.nl."]
    for i, t in enumerate(text):
        p.put(20 * mm, y - len(rows) * 9 * mm - 8 * mm - i * 5.5 * mm, t, "Helvetica", 9)


def tax_letter():
    p = Pen()
    p.put(20 * mm, H - 25 * mm, "Belastingdienst", "Helvetica-Bold", 14, "counterparty")
    p.put(20 * mm, H - 30 * mm, "VOORBEELD - fictieve brief voor een demo", "Helvetica", 8, gray=0.3)
    address(p, to_lines("1063"), "band", y=H - 55 * mm)
    rows = [("Dagtekening", d_long("2026-11-03"), "doc_date"), ("Btw-identificatienummer", "NL858877120B01", None),
            ("Aanslagnummer", "8588.77.120.F.01.6090", "reference"), ("Tijdvak", "3e kwartaal 2026", None)]
    for i, (k, v, f) in enumerate(rows):
        p.put(112 * mm, H - 55 * mm - i * 5 * mm, k, "Helvetica", 9, gray=0.25)
        p.put(152 * mm, H - 55 * mm - i * 5 * mm, v, "Helvetica", 9, f)
    p.put(20 * mm, H - 92 * mm, "Naheffingsaanslag omzetbelasting", "Helvetica-Bold", 13)
    p.put(20 * mm, H - 98 * mm, "Boete wegens het niet op tijd doen van aangifte (aangifteverzuim)", "Helvetica", 10)
    body = ["Geachte heer, mevrouw,", "",
            "Uw aangifte omzetbelasting over het 3e kwartaal 2026 hebben wij te laat ontvangen. De aangifte",
            "moest uiterlijk 31 oktober 2026 bij ons binnen zijn. Daarom leggen wij u een verzuimboete op."]
    for i, t in enumerate(body):
        p.put(20 * mm, H - 112 * mm - i * 6 * mm, t, "Helvetica", 10)
    y = H - 142 * mm
    p.c.setLineWidth(0.6)
    p.c.rect(20 * mm, y - 15 * mm, 170 * mm, 21 * mm, stroke=1, fill=0)
    p.put(24 * mm, y, "Te betalen bedrag", "Helvetica", 10)
    p.put(186 * mm, y, "EUR 68,00", "Helvetica-Bold", 11, "gross_total", align="r")
    p.put(24 * mm, y - 9 * mm, "Uiterste betaaldatum", "Helvetica", 10)
    p.put(186 * mm, y - 9 * mm, d_long("2026-12-15"), "Helvetica-Bold", 11, "due_date", align="r")
    rest = [f"Maak het bedrag over op rekeningnummer {spaced(iban('0002445588'))} ten name van de Belastingdienst",
            "onder vermelding van het betalingskenmerk 2588 7712 0601 6090.", "",
            "Bent u het niet eens met deze aanslag? Dan kunt u binnen 6 weken na de dagtekening bezwaar maken.",
            "In uw bezwaarschrift vermeldt u het aanslagnummer en waarom u het niet eens bent met de boete.", "",
            "Hoogachtend,", "de inspecteur"]
    for i, t in enumerate(rest):
        p.put(20 * mm, y - 28 * mm - i * 6 * mm, t, "Helvetica", 10)
    p.footer()
    return p.done()


def packing_slip():
    p = Pen()
    s = S["bgr"]
    header(p, s, purchase=True, show_iban=False)
    address(p, to_lines("1057"), s["style"])
    p.put(20 * mm, H - 92 * mm, "PAKBON", "Helvetica-Bold", 14)
    meta(p, [("Pakbonnummer", "8811-204", "reference"), ("Leverdatum", d_nl("2026-11-04"), "doc_date"),
             ("Uw order", "MB-2611-07", None), ("Bij factuur", "26-118204", None)], s["style"])
    y = H - 125 * mm
    p.put(20 * mm, y, "Artikel", "Helvetica-Bold", 9)
    p.put(150 * mm, y, "Besteld", "Helvetica-Bold", 9, align="r")
    p.put(190 * mm, y, "Geleverd", "Helvetica-Bold", 9, align="r")
    p.c.line(20 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)
    rows = [("Gipsplaat 2600 x 1200 RK 12,5 mm", "60", "60"), ("Metalstud C-profiel 75 mm, 3 m", "80", "80"),
            ("Isolatieplaat PIR 100 mm", "14", "14"), ("Schroeven en bevestigingsmateriaal (set)", "1", "1")]
    for i, (a, b, c_) in enumerate(rows):
        p.put(20 * mm, y - 8 * mm - i * 6 * mm, a, "Helvetica", 9)
        p.put(150 * mm, y - 8 * mm - i * 6 * mm, b, "Helvetica", 9, align="r")
        p.put(190 * mm, y - 8 * mm - i * 6 * mm, c_, "Helvetica", 9, align="r")
    p.put(20 * mm, y - 48 * mm, "Dit is geen factuur. De factuur ontvangt u apart.", "Helvetica-Bold", 10)
    p.put(20 * mm, y - 56 * mm, "Afgeleverd op de bouwplaats Mathenesserlaan 214, Rotterdam. Zichtbare schade direct melden.",
          "Helvetica", 9)
    p.put(20 * mm, y - 78 * mm, "Voor ontvangst:", "Helvetica", 9)
    p.c.line(48 * mm, y - 78.5 * mm, 110 * mm, y - 78.5 * mm)
    p.put(120 * mm, y - 78 * mm, "Chauffeur:", "Helvetica", 9)
    p.c.line(140 * mm, y - 78.5 * mm, 190 * mm, y - 78.5 * mm)
    p.footer()
    return p.done()


# ---------------------------------------------------------------- receipts (thermal paper, drawn pixel by pixel)
class Receipt:
    """A till receipt drawn at double size. Remembers the pixel box of tagged values."""

    def __init__(self, cols=34, px=30, pad=44):
        self.cols, self.px, self.pad = cols, px, pad
        self.f = ImageFont.truetype(MONO, px)
        self.fb = ImageFont.truetype(MONO_B, px)
        self.fbig = ImageFont.truetype(MONO_B, int(px * 1.55))
        self.cw = self.f.getlength("0")
        self.w = int(self.cw * cols + pad * 2)
        self.rows = []

    def center(self, text, bold=False, big=False, field=None):
        self.rows.append(("c", text, bold, big, field))

    def lr(self, left, right, bold=False, field=None):
        self.rows.append(("lr", left, right, bold, field))

    def text(self, text, field=None, span=None):
        self.rows.append(("t", text, field, span))

    def rule(self, ch="-"):
        self.rows.append(("t", ch * self.cols, None, None))

    def gap(self, n=1):
        self.rows += [("g",)] * n

    def barcode(self):
        self.rows.append(("b",))

    def render(self, seed=1):
        r = random.Random(seed)
        lh = int(self.px * 1.34)
        height = self.pad * 2 + sum(int(lh * 1.6) if row[0] == "c" and row[3] else lh * 3 if row[0] == "b"
                                    else lh // 2 if row[0] == "g" else lh for row in self.rows)
        img = Image.new("RGB", (self.w, height), (252, 251, 246))
        d = ImageDraw.Draw(img)
        ink = (38, 38, 52)
        y, boxes = self.pad, {}

        def box(field, x0, x1, top, h):
            if field:
                boxes[field] = (x0 - 6, top - 2, x1 + 6, top + h + 4)

        for row in self.rows:
            kind = row[0]
            if kind == "g":
                y += lh // 2
            elif kind == "b":
                x = self.pad + 30
                while x < self.w - self.pad - 30:
                    bw = r.choice((2, 3, 5, 7))
                    if r.random() < 0.55:
                        d.rectangle([x, y + 6, x + bw, y + lh * 2 + 6], fill=ink)
                    x += bw + r.choice((2, 3))
                y += lh * 3
            elif kind == "c":
                _, text, bold, big, field = row
                font = self.fbig if big else self.fb if bold else self.f
                tw = font.getlength(text)
                x0 = (self.w - tw) / 2
                d.text((x0, y), text, font=font, fill=ink)
                hh = int(lh * 1.6) if big else lh
                box(field, x0, x0 + tw, y, int(font.size * 1.1))
                y += hh
            elif kind == "lr":
                _, left, right, bold, field = row
                font = self.fb if bold else self.f
                d.text((self.pad, y), left, font=font, fill=ink)
                tw = font.getlength(right)
                x0 = self.w - self.pad - tw
                d.text((x0, y), right, font=font, fill=ink)
                box(field, x0, x0 + tw, y, int(self.px * 1.1))
                y += lh
            else:
                _, text, field, span = row
                d.text((self.pad, y), text, font=self.f, fill=ink)
                if field:
                    a, b = span or (0, len(text))
                    box(field, self.pad + self.cw * a, self.pad + self.cw * b, y, int(self.px * 1.1))
                y += lh
        # thermal print: uneven darkness and a slightly ragged tear at the top and bottom
        a = np.asarray(img).astype(np.float32)
        fade = 1 + 0.10 * np.sin(np.linspace(0, r.uniform(5, 9), self.w))[None, :, None] * (a < 200)
        a = np.clip(255 - (255 - a) * (0.88 + 0.12 * fade), 0, 255)
        img = Image.fromarray(a.astype(np.uint8))
        return img, {k: quad(*v) for k, v in boxes.items()}


def quad(x0, y0, x1, y1):
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64)


def crumple(img, quads, seed, amp=5.0, folds=3, shade_k=0.055):
    """Paper that lived in a pocket: a soft warp, shading along the dents and a few sharp creases."""
    import cv2
    r = np.random.default_rng(seed)
    w, h = img.size
    small = (max(4, w // 90), max(4, h // 90))
    fx = cv2.resize(r.normal(0, 1, (small[1], small[0])).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    fy = cv2.resize(r.normal(0, 1, (small[1], small[0])).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    a = np.asarray(img)
    warped = cv2.remap(a, gx + fx * amp, gy + fy * amp, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    shade = 1 + shade_k * cv2.GaussianBlur(np.gradient(fx, axis=1) * 60 + np.gradient(fy, axis=0) * 60, (0, 0), 3)
    out = warped.astype(np.float32) * shade[..., None]
    for _ in range(folds):  # creases: a dark line with a light edge
        horizontal = r.random() < 0.7
        pos = int(r.uniform(0.15, 0.85) * (h if horizontal else w))
        tilt = r.uniform(-0.03, 0.03)
        line = (gy - pos - (gx - w / 2) * tilt) if horizontal else (gx - pos - (gy - h / 2) * tilt)
        out *= (1 - 2.9 * shade_k * np.exp(-(line ** 2) / 6) + 1.3 * shade_k * np.exp(-((line - 5) ** 2) / 14))[..., None]
    out = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))

    def move(q):
        xi = np.clip(q[:, 0].astype(int), 0, w - 1)
        yi = np.clip(q[:, 1].astype(int), 0, h - 1)
        return np.stack([q[:, 0] - fx[yi, xi] * amp, q[:, 1] - fy[yi, xi] * amp], 1)

    return out, {k: move(q) for k, q in quads.items()}


def scale_q(quads, s):
    return {k: q * s for k, q in quads.items()}


def rotate_expand(img, quads, angle, fill):
    w, h = img.size
    out = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=fill)
    return out, {k: rot_points(q, angle, (w, h)) + [(out.size[0] - w) / 2, (out.size[1] - h) / 2] for k, q in quads.items()}


def rot_points(pts, angle, size):
    """Where points land after PIL's img.rotate(angle) about the image centre (counter-clockwise on screen)."""
    th = math.radians(angle)
    cx, cy = size[0] / 2, size[1] / 2
    dx, dy = pts[:, 0] - cx, pts[:, 1] - cy
    return np.stack([dx * math.cos(th) + dy * math.sin(th) + cx, -dx * math.sin(th) + dy * math.cos(th) + cy], 1)


def on_glass(receipt, quads, seed):
    """A small receipt laid on the scanner glass: an A4 page that is mostly lid, with the bon near the top."""
    r = random.Random(seed)
    page = Image.new("RGB", A4PX, PAPER)
    s = (A4PX[0] * r.uniform(0.38, 0.42)) / receipt.size[0]
    rec = receipt.resize((int(receipt.size[0] * s), int(receipt.size[1] * s)), Image.LANCZOS)
    quads = scale_q(quads, s)
    rec, quads = rotate_expand(rec, quads, r.uniform(-2.2, 2.2), PAPER)
    ox, oy = int(r.uniform(70, 190)), int(r.uniform(60, 120))
    shadow = Image.new("RGB", A4PX, PAPER)
    ImageDraw.Draw(shadow).rectangle([ox + 5, oy + 7, ox + rec.size[0] + 3, oy + rec.size[1] + 5], fill=(205, 205, 203))
    page = Image.blend(page, shadow.filter(ImageFilter.GaussianBlur(7)), 0.9)
    page.paste(rec, (ox, oy))
    return page, {k: q + [ox, oy] for k, q in quads.items()}


def phone_photo(receipt, quads, seed, surface="wood", size=(1200, 1600)):
    """The client's reply: a receipt photographed with a phone, a little skewed, on a table or dashboard."""
    import cv2
    r = np.random.default_rng(seed)
    w, h = size
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    if surface == "wood":
        base = np.array([150, 112, 76], dtype=np.float32)
        grain = cv2.GaussianBlur(r.normal(0, 1, (h, w)).astype(np.float32), (0, 0), sigmaX=38, sigmaY=1.4) * 46
        planks = ((gx // 300) % 2) * 7 - np.exp(-((gx % 300) ** 2) / 8) * 42
        bg = base[None, None, :] + (grain + planks)[..., None] * np.array([1.0, 0.82, 0.6])
    else:  # dark dashboard / work bench
        base = np.array([52, 54, 60], dtype=np.float32)
        bg = base[None, None, :] + cv2.GaussianBlur(r.normal(0, 1, (h, w)).astype(np.float32), (0, 0), 1.2)[..., None] * 9
    rw = w * 0.60
    s = rw / receipt.size[0]
    rh = receipt.size[1] * s
    if rh > h * 0.86:
        s *= h * 0.86 / rh
        rw, rh = receipt.size[0] * s, h * 0.86
    x0, y0 = (w - rw) / 2 + r.uniform(-40, 40), (h - rh) / 2 + r.uniform(-20, 30)
    j = lambda k=1.0: float(r.uniform(-1, 1) * 26 * k)
    src = np.float32([[0, 0], [receipt.size[0], 0], [receipt.size[0], receipt.size[1]], [0, receipt.size[1]]])
    dst = np.float32([[x0 + j(), y0 + j()], [x0 + rw + j(), y0 + j(1.6)], [x0 + rw + j(0.6), y0 + rh + j()],
                      [x0 + j(0.6), y0 + rh + j(1.4)]])
    M = cv2.getPerspectiveTransform(src, dst)
    rec = cv2.warpPerspective(np.asarray(receipt), M, (w, h), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
    mask = cv2.warpPerspective(np.full((receipt.size[1], receipt.size[0]), 255, np.uint8), M, (w, h))
    shadow = cv2.GaussianBlur(np.roll(np.roll(mask, 14, 0), 10, 1).astype(np.float32) / 255, (0, 0), 16)
    bg = bg * (1 - 0.45 * shadow[..., None])
    m = (cv2.GaussianBlur(mask, (0, 0), 0.8).astype(np.float32) / 255)[..., None]
    out = bg * (1 - m) + rec.astype(np.float32) * m
    light = 1.08 - 0.34 * (((gx - w * 0.3) / w) ** 2 + ((gy - h * 0.25) / h) ** 2)   # a lamp up left, vignette
    out = out * light[..., None] * np.array([1.02, 1.0, 0.95])
    out += r.normal(0, 2.2, out.shape)
    img = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
    moved = {k: cv2.perspectiveTransform(q.astype(np.float32)[None], M)[0].astype(np.float64) for k, q in quads.items()}
    return img, moved


# ---------------------------------------------------------------- paper -> scan
def render_pdf(data, page=0):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(data)
    try:
        return doc[page].render(scale=PT).to_pil().convert("RGB").resize(A4PX)
    finally:
        doc.close()


def pt_quads(boxes):
    return {k: quad(x0 * PT, (H - y1) * PT, x1 * PT, (H - y0) * PT) for k, (x0, y0, x1, y1) in boxes.items()}


def scanned(img, quads, gray=False):
    """Slight skew, paper tone and blur, like a real feeder scan."""
    angle = rng.uniform(-0.7, 0.7)
    out = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=PAPER).filter(ImageFilter.GaussianBlur(0.45))
    if gray:
        out = out.convert("L").convert("RGB")
    return out, {k: rot_points(q, angle, img.size) for k, q in quads.items()}


def blank_page():
    page = Image.new("RGB", A4PX, (249, 249, 247))
    px = page.load()
    for _ in range(40):
        px[rng.randrange(A4PX[0]), rng.randrange(A4PX[1])] = (150, 150, 150)
    return page


def norm_boxes(quads, size, pad=3):
    out = {}
    for k, q in quads.items():
        x0, y0, x1, y1 = q[:, 0].min() - pad, q[:, 1].min() - pad, q[:, 0].max() + pad, q[:, 1].max() + pad
        out[k] = [round(max(0, x0) / size[0], 4), round(max(0, y0) / size[1], 4),
                  round(min(size[0], x1) / size[0], 4), round(min(size[1], y1) / size[1], 4)]
    return out


# ---------------------------------------------------------------- the documents
def _inv(kind, cp, ref, date, net, vat, gross, lines, desc, **kw):
    base = {"kind": kind, "client_hint": None, "counterparty": cp, "reference": ref, "doc_date": date,
            "net_total": net, "vat_lines": vat, "gross_total": gross, "lines": lines, "description": desc,
            "currency": "EUR", "due_date": None, "supplier_vat_number": None, "supplier_kvk": None, "iban": None,
            "booking_reason": "", "uncertain_fields": [], "issues": []}
    base.update(kw)
    return base


def _sup(key):
    s = S[key]
    return {"supplier_vat_number": s["vat"] or None, "supplier_kvk": s["kvk"] or None, "iban": s["iban"]}


def _line(account, code, excl, vat, desc):
    return {"account": account, "vat_code": code, "amount_excl": excl, "vat_amount": vat, "description": desc}


def std_meta(number, date, due=None, extra=()):
    rows = [("Factuurnummer", number, "reference"), ("Factuurdatum", d_nl(date), "doc_date")]
    if due:
        rows.append(("Vervaldatum", d_nl(due), "due_date"))
    return rows + list(extra)


def pay_note(s, due, number):
    return [[("Graag betalen vóór ", None), (d_nl(due), None), (" op ", None), (spaced(s["iban"]), None),
             (f" t.n.v. {s['name']}", None)], f"onder vermelding van factuurnummer {number}."]


def maashal_rows(target9, target21, n9, n21, seed):
    """Line items of a horeca wholesaler that add up exactly to the two VAT bases."""
    food = [("Friet 9 mm diepvries 10 kg", 16.95), ("Mayonaise emmer 10 L", 21.40), ("Kipdijfilet 2,5 kg", 23.75),
            ("Rundergehakt 2,5 kg", 24.90), ("Hamburger 30 x 120 g", 27.60), ("Eieren M tray 90", 15.80),
            ("Roomboter 2 kg", 19.20), ("Volle melk 10 L", 9.95), ("Slagroom 1 L x 6", 22.30),
            ("Jonge kaas plakken 1 kg", 10.45), ("Achterham 1 kg", 12.80), ("Tomaten 6 kg", 11.25),
            ("IJsbergsla 10 st", 9.40), ("Uien 10 kg", 7.85), ("Witte bol 60 st", 13.50), ("Appelgebak 12 punten", 14.90),
            ("Koffiebonen 1 kg x 8", 78.00), ("Frisdrank cola 24 x 33 cl", 13.95), ("Sinaasappelsap 12 x 1 L", 17.40),
            ("Bitterballen 100 st", 18.75), ("Kaassoufflé 40 st", 16.20), ("Satésaus 10 kg", 26.50),
            ("Tomatensoep 4 x 2,5 L", 19.80), ("Pasta penne 5 kg", 8.95), ("Zalmfilet 2 kg", 39.50),
            ("Spa blauw 28 x 25 cl", 12.60), ("Thee assorti 100 st", 8.75), ("Suikersticks 1000 st", 9.90)]
    nonfood = [("Pils krat 24 x 30 cl", 16.49), ("Speciaalbier 24 x 33 cl", 27.80), ("Huiswijn wit 6 x 75 cl", 28.50),
               ("Huiswijn rood 6 x 75 cl", 29.40), ("Servetten 2-laags 2000 st", 24.95), ("Afwasmiddel 10 L", 18.60),
               ("Vaatwastabletten 200 st", 31.20), ("Vuilniszakken 120 L x 200", 26.75), ("Handzeep 5 L", 11.90),
               ("Bierviltjes 1000 st", 14.30), ("Theelichtjes 500 st", 19.95), ("Rietjes papier 500 st", 7.80),
               ("Keukenpapier 6 rol", 15.40), ("Frituurvet 10 kg (non-food)", 22.10)]
    r = random.Random(seed)
    while True:
        rows, s9, s21 = [], 0, 0
        for cat, items, n in (("9%", food, n9), ("21%", nonfood, n21)):
            for name, price in r.sample(items, min(n - 1, len(items))) + r.choices(items, k=max(0, n - 1 - len(items))):
                qty = r.choice((1, 1, 2, 2, 3, 4, 6))
                cents = int(round(price * 100)) * qty
                rows.append((name, qty, price, cents, cat))
                if cat == "9%":
                    s9 += cents
                else:
                    s21 += cents
        rest9, rest21 = int(round(target9 * 100)) - s9, int(round(target21 * 100)) - s21
        if 900 <= rest9 <= 6500 and 900 <= rest21 <= 6500:
            rows.append(("Verse kruidenmix 500 g", 1, rest9 / 100, rest9, "9%"))
            rows.append(("Glazenset bistro 12 st", 1, rest21 / 100, rest21, "21%"))
            r.shuffle(rows)
            art = lambda: f"{r.randrange(100000, 999999)}"
            return [(f"{art()}  {n}", q, nl(p), nl(c / 100), cat) for n, q, p, c, cat in rows]


def build_documents():
    """Returns [(key, [(image, boxes) per page], answer)] in scan order, plus what the sandbox and homepage need."""
    docs = []

    def add(key, client, pdf_and_boxes, answer, gray=False, pages=None):
        data, boxes = pdf_and_boxes
        out = []
        for i in (pages if pages is not None else range(len(boxes))):
            img, quads = scanned(render_pdf(data, i), pt_quads(boxes[i]), gray)
            out.append((img, norm_boxes(quads, img.size)))
        answer["client_hint"] = C[client]["name"]
        docs.append({"key": key, "client": client, "pages": out, "answer": answer, "pdf": data})

    def add_receipt(key, client, rec, answer, seed, amp=2.2):
        img, quads = rec.render(seed)
        img, quads = crumple(img, quads, seed, amp=amp, folds=2, shade_k=0.022)
        page, quads = on_glass(img, quads, seed)
        page, quads = scanned(page, quads)
        answer["client_hint"] = C[client]["name"]
        docs.append({"key": key, "client": client, "pages": [(page, norm_boxes(quads, page.size, 4))], "answer": answer})

    # ---- dossier 1042: Bakkerij Van Dijk B.V.
    s = S["molen"]
    add("B-MOLEN", "1042", invoice(dict(
        sender=s, to=to_lines("1042", "t.a.v. de administratie"), number="2026-4471",
        meta=std_meta("2026-4471", "2026-11-03", "2026-11-17", [("Klantnummer", "D-1042", None)]),
        rows=[("Tarwebloem T65, zak 25 kg", 12, "19,80", "237,60"), ("Volkorenmeel, zak 25 kg", 6, "21,40", "128,40"),
              ("Roggemeel, zak 25 kg", 4, "22,90", "91,60"), ("Meergranenmix, zak 20 kg", 3, "34,60", "103,80"),
              ("Zonnebloempitten, 10 kg", 2, "25,50", "51,00")],
        totals=[("Subtotaal excl. btw", "612,40", False, "net_total"), ("Btw 9%", "55,12", False, "vat_9"),
                ("Totaal", "667,52", True, "gross_total")],
        notes=pay_note(s, "2026-11-17", "2026-4471"))),
        _inv("inkoopfactuur", s["name"], "2026-4471", "2026-11-03", 612.40, [{"rate": 9, "base": 612.40, "amount": 55.12}],
             667.52, [_line("7000", "V9", 612.40, 55.12, "Meel en grondstoffen")], "Meel en bakkerijgrondstoffen",
             due_date="2026-11-17", booking_reason="Vaste leverancier van grondstoffen: zelfde rekening en btw-code als de eerdere facturen.",
             **_sup("molen")))

    s = S["energie"]
    add("B-ENERGIE", "1042", invoice(dict(
        sender=s, to=to_lines("1042"), number="88120334-1026",
        meta=std_meta("88120334-1026", "2026-11-01", "2026-11-15", [("Klantnummer", "88120334", None),
                                                                 ("Periode", "oktober 2026", None)]),
        rows=[("Levering elektra (1.482 kWh)", 1, "412,18", "412,18"), ("Levering gas (298 m³)", 1, "286,40", "286,40"),
              ("Vastrecht, netbeheer en meterhuur", 1, "24,50", "24,50")],
        totals=[("Totaal excl. btw", "723,08", False, "net_total"), ("Btw 21%", "151,85", False, "vat_21"),
                ("Te betalen", "874,93", True, "gross_total")],
        notes=["Het bedrag wordt rond de vervaldatum automatisch van uw rekening afgeschreven.",
               "De specificatie van uw verbruik staat op pagina 2."],
        extra=[energy_page2])),
        _inv("inkoopfactuur", s["name"], "88120334-1026", "2026-11-01", 723.08, [{"rate": 21, "base": 723.08, "amount": 151.85}],
             874.93, [_line("4200", "V21", 723.08, 151.85, "Gas en elektra oktober")], "Gas en elektra oktober 2026",
             due_date="2026-11-15", booking_reason="Maandelijkse energienota: 4200, voorbelasting 21%.",
             split_reason="Pagina 2 is de verbruiksspecificatie bij dezelfde factuur (zelfde afzender en factuurnummer).",
             **_sup("energie")))

    s = S["pakhuis"]
    add("B-PAKHUIS", "1042", invoice(dict(
        sender=s, to=to_lines("1042"), number="F-26-10988", meta=std_meta("F-26-10988", "2026-10-28", "2026-11-27"),
        rows=[("Broodzakken kraft, 500 st", 2, "38,50", "77,00"), ("Taartdozen 26 cm, 100 st", 1, "64,00", "64,00"),
              ("Gebaksschaaltjes, 250 st", 1, "32,50", "32,50"), ("Verzendkosten", 1, "12,50", "12,50")],
        totals=[("Subtotaal excl. btw", "186,00", False, "net_total"), ("Btw 21%", "39,06", False, "vat_21"),
                ("Totaal", "225,06", True, "gross_total")],
        notes=pay_note(s, "2026-11-27", "F-26-10988"))),
        _inv("inkoopfactuur", s["name"], "F-26-10988", "2026-10-28", 186.00, [{"rate": 21, "base": 186.00, "amount": 39.06}],
             225.06, [_line("7100", "V21", 186.00, 39.06, "Broodzakken en taartdozen")], "Verpakkingsmateriaal",
             due_date="2026-11-27", booking_reason="Verpakkingen voor de winkel: 7100.", **_sup("pakhuis")), gray=True)

    s = S["techniek"]
    add("B-KNEDER", "1042", invoice(dict(
        sender=s, to=to_lines("1042"), number="BTZ-2611-0098", meta=std_meta("BTZ-2611-0098", "2026-11-05", "2026-12-05"),
        rows=[("Spiraalkneder SK-60, kuipinhoud 60 liter", 1, "2.290,00", "2.290,00"),
              ("Levering, plaatsing en instructie", 1, "160,00", "160,00")],
        totals=[("Subtotaal excl. btw", "2.450,00", False, "net_total"), ("Btw 21%", "514,50", False, "vat_21"),
                ("Totaal", "2.964,50", True, "gross_total")],
        notes=pay_note(s, "2026-12-05", "BTZ-2611-0098") + ["Garantie 24 maanden op onderdelen en arbeid."])),
        _inv("inkoopfactuur", s["name"], "BTZ-2611-0098", "2026-11-05", 2450.00, [{"rate": 21, "base": 2450.00, "amount": 514.50}],
             2964.50, [_line("0220", "V21", 2450.00, 514.50, "Spiraalkneder SK-60")], "Spiraalkneder SK-60 met plaatsing",
             due_date="2026-12-05",
             booking_reason="Nieuwe machine boven de activeringsgrens: voorstel 0220 Machines en installaties, niet de onderhoudsrekening.",
             issues=[{"level": "warning", "message": "Aanschaf van € 2.450 excl. btw: activeren als bedrijfsmiddel (0220)? "
                      "De twee eerdere facturen van deze leverancier waren onderhoud (4300). Denk aan de KIA."}],
             **_sup("techniek")))

    r = Receipt()
    r.center("SuperVers", big=True, field="counterparty")
    r.center("Schiedam Centrum")
    r.center("Broersvest 102, 3111 ED Schiedam")
    r.gap()
    r.text("Datum 06-11-2026  12:47", field="doc_date", span=(6, 16))
    r.text("Bon   0047-2231   Kassa 3", field="reference", span=(6, 15))
    r.rule()
    for a, b in (("Koffiebonen 1 kg", "11,49 A"), ("Halfvolle melk 6x1L", "6,54 A"), ("Appels Elstar 2 kg", "3,98 A"),
                 ("Bananen", "1,24 A"), ("Allesreiniger 1 L", "3,49 B"), ("Vuilniszakken 60 L", "2,99 B"),
                 ("Afwasmiddel", "3,68 B")):
        r.lr(a, b)
    r.rule()
    r.lr("TOTAAL", "33,41", bold=True, field="gross_total")
    r.lr("PIN", "33,41")
    r.gap()
    r.text("BTW      excl.     btw     incl.")
    r.text("A  9%    21,33    1,92    23,25", field="vat_9", span=(17, 22))
    r.text("B 21%     8,40    1,76    10,16", field="vat_21", span=(17, 22))
    r.gap()
    r.center("Bedankt en tot ziens")
    r.barcode()
    add_receipt("B-SUPERVERS", "1042", r, _inv(
        "kassabon", "SuperVers Schiedam", "0047-2231", "2026-11-06", 29.73,
        [{"rate": 9, "base": 21.33, "amount": 1.92}, {"rate": 21, "base": 8.40, "amount": 1.76}], 33.41,
        [_line("4530", "V9", 21.33, 1.92, "Koffie, melk, fruit"), _line("4530", "V21", 8.40, 1.76, "Schoonmaakmiddelen")],
        "Boodschappen kantine", booking_reason="Kantinekosten, één regel per btw-tarief."), seed=5)

    s = S["belnet"]
    add("B-BELNET", "1042", invoice(dict(
        sender=s, to=to_lines("1042"), number="BM26-1107733",
        meta=std_meta("BM26-1107733", "2026-11-01", "2026-11-15", [("Klantnummer", "4410020", None)]),
        rows=[("Zakelijk mobiel, 2 abonnementen november", 2, "17,50", "35,00"), ("Zakelijk internet glasvezel", 1, "20,00", "20,00")],
        totals=[("Subtotaal excl. btw", "55,00", False, "net_total"), ("Btw 21%", "11,55", False, "vat_21"),
                ("Totaal", "66,55", True, "gross_total")],
        notes=["Dit bedrag wordt rond de 15e automatisch afgeschreven."])),
        _inv("inkoopfactuur", s["name"], "BM26-1107733", "2026-11-01", 55.00, [{"rate": 21, "base": 55.00, "amount": 11.55}],
             66.55, [_line("4310", "V21", 55.00, 11.55, "Telefoon en internet november")], "Telefoon en internet november",
             due_date="2026-11-15", booking_reason="Vaste leverancier: zelfde rekening en btw-code als de eerdere maanden.",
             **_sup("belnet")))

    me = dict(name=C["1042"]["name"], addr=C["1042"]["address"], kvk=C["1042"]["kvk"], vat=C["1042"]["vat"],
              iban=iban("0681209340"), color=(0.62, 0.38, 0.12), style="serif", tag="Ambachtelijk brood en banket")
    add("B-VERKOOP", "1042", invoice(dict(
        sender=me, purchase=False, to=["Lunchroom Het Plein V.O.F.", "Grote Markt 6", "3111 NH Schiedam"], number="2026-0412",
        meta=std_meta("2026-0412", "2026-10-31", extra=[("Betalen binnen", "14 dagen", None)]),
        rows=[("Brood divers, week 44", 1, "312,00", "312,00"), ("Gebak en banket, week 44", 1, "174,00", "174,00")],
        totals=[("Subtotaal excl. btw", "486,00", False, "net_total"), ("Btw 9%", "43,74", False, "vat_9"),
                ("Totaal", "529,74", True, "gross_total")],
        notes=[f"Graag binnen 14 dagen overmaken op {spaced(me['iban'])}."])),
        _inv("verkoopfactuur", "Lunchroom Het Plein V.O.F.", "2026-0412", "2026-10-31", 486.00,
             [{"rate": 9, "base": 486.00, "amount": 43.74}], 529.74, [_line("8100", "O9", 486.00, 43.74, "Brood en gebak week 44")],
             "Brood en gebak week 44", booking_reason="Eigen verkoopfactuur van de klant: omzet laag tarief."))

    s = S["renova"]
    add("B-RENOVA", "1042", invoice(dict(
        sender=s, to=to_lines("1042"), number="RB-2610-55120", meta=std_meta("RB-2610-55120", "2026-10-31", "2026-11-14"),
        rows=[("Lediging rolcontainer 1100 L, oktober", 4, "16,50", "66,00"), ("Huur rolcontainer oktober", 1, "20,00", "20,00")],
        totals=[("Subtotaal excl. btw", "86,00", False, "net_total"), ("Btw 21%", "18,06", False, "vat_21"),
                ("Totaal", "104,06", True, "gross_total")],
        notes=pay_note(s, "2026-11-14", "RB-2610-55120"))),
        _inv("inkoopfactuur", s["name"], "RB-2610-55120", "2026-10-31", 86.00, [{"rate": 21, "base": 86.00, "amount": 18.06}],
             104.06, [_line("4250", "V21", 86.00, 18.06, "Bedrijfsafval oktober")], "Bedrijfsafval oktober",
             due_date="2026-11-14", booking_reason="Vaste leverancier: afvalverwerking op 4250.", **_sup("renova")), gray=True)

    # ---- dossier 1057: Marco Bouw & Onderhoud
    s = S["bgr"]
    add("M-BGR-KB", "1057", invoice(dict(
        sender=s, to=to_lines("1057"), number="26-118204",
        meta=std_meta("26-118204", "2026-11-04", "2026-12-04", [("Debiteurnummer", "30571", None), ("Uw referentie", "Mathenesserlaan", None)]),
        rows=[("Gipsplaat 2600 x 1200 RK 12,5 mm", 60, "7,95", "477,00"), ("Metalstud C-profiel 75 mm, 3 m", 80, "3,85", "308,00"),
              ("Isolatieplaat PIR 100 mm", 14, "26,50", "371,00"), ("Schroeven en bevestigingsmateriaal", 1, "84,00", "84,00")],
        totals=[("Goederen", "1.240,00", False, None), ("Kredietbeperking 2%", "24,80", False, None),
                ("Subtotaal excl. btw", "1.264,80", False, "net_total"), ("Btw 21%", "265,61", False, "vat_21"),
                ("Totaal", "1.530,41", True, "gross_total")],
        notes=["Kredietbeperking: bij betaling binnen 8 dagen na factuurdatum mag u € 30,01 (incl. btw) in mindering brengen."]
        + pay_note(s, "2026-12-04", "26-118204"))),
        _inv("inkoopfactuur", s["name"], "26-118204", "2026-11-04", 1264.80, [{"rate": 21, "base": 1264.80, "amount": 265.61}],
             1530.41, [_line("7000", "V21", 1240.00, 260.40, "Gipsplaat, profielen, isolatie"),
                       _line("4960", "V21", 24.80, 5.21, "Kredietbeperking 2%")],
             "Bouwmaterialen project Mathenesserlaan", due_date="2026-12-04",
             booking_reason="Vaste leverancier: materialen op 7000. Kredietbeperking apart op 4960, zodat de korting bij tijdig betalen zichtbaar blijft.",
             issues=[{"level": "info", "message": "Kredietbeperking € 24,80 apart geboekt (4960). Vervalt bij betaling binnen 8 dagen: uiterlijk 12-11-2026, dan € 30,01 minder betalen."}],
             **_sup("bgr")))

    add("M-BGR-IBAN", "1057", invoice(dict(
        sender=dict(s, iban=FRAUD_IBAN), to=to_lines("1057"), number="26-118977", show_iban=False,
        meta=std_meta("26-118977", "2026-11-07", "2026-11-21", [("Debiteurnummer", "30571", None)]),
        rows=[("Multiplex 18 mm 2440 x 1220", 30, "42,00", "1.260,00"), ("Vuren balk 71 x 196, 4,8 m", 24, "28,50", "684,00"),
              ("Tegellijm flex, zak 25 kg", 16, "14,75", "236,00")],
        totals=[("Subtotaal excl. btw", "2.180,00", False, "net_total"), ("Btw 21%", "457,80", False, "vat_21"),
                ("Totaal", "2.637,80", True, "gross_total")],
        notes=[{"title": "LET OP: ons rekeningnummer is gewijzigd",
                "parts": [("Maak dit bedrag over op ons nieuwe rekeningnummer ", None), (spaced(FRAUD_IBAN), "iban"),
                          (" t.n.v. Bouwgroothandel Rijnmond.", None)]},
               "Betaling binnen 14 dagen onder vermelding van factuurnummer 26-118977."])),
        _inv("inkoopfactuur", s["name"], "26-118977", "2026-11-07", 2180.00, [{"rate": 21, "base": 2180.00, "amount": 457.80}],
             2637.80, [_line("7000", "V21", 2180.00, 457.80, "Multiplex, balken, tegellijm")], "Bouwmaterialen",
             due_date="2026-11-21", booking_reason="Vaste leverancier: materialen op 7000.",
             supplier_vat_number=s["vat"], supplier_kvk=s["kvk"], iban=FRAUD_IBAN))

    s = S["wit"]
    add("M-WIT", "1057", invoice(dict(
        sender=s, to=to_lines("1057"), number="2026-087",
        meta=std_meta("2026-087", "2026-10-30", "2026-11-29", [("Btw-id afnemer", C["1057"]["vat"], None)]),
        rows=[("Stucwerk wanden en plafonds, Mathenesserlaan 214 (74 m²)", 74, "25,00", "1.850,00")],
        totals=[("Totaal excl. btw", "1.850,00", False, "net_total"), ("Btw verlegd", "0,00", False, "vat_0"),
                ("Te betalen", "1.850,00", True, "gross_total")],
        notes=["Btw verlegd naar de afnemer (verleggingsregeling bouw, art. 24b Uitvoeringsbesluit OB).",
               "Werkzaamheden in onderaanneming, uitgevoerd in week 43 en 44."] + pay_note(s, "2026-11-29", "2026-087"))),
        _inv("inkoopfactuur", s["name"], "2026-087", "2026-10-30", 1850.00, [], 1850.00,
             [_line("7200", "NL_VERL", 1850.00, 0.0, "Stucwerk in onderaanneming")], "Stucwerk Mathenesserlaan 214",
             due_date="2026-11-29",
             booking_reason="Onderaannemer in de bouw: btw verlegd. Geen voorbelasting op de factuur; aangeven in rubriek 2a en aftrekken in 5b.",
             issues=[{"level": "info", "message": "Btw verlegd (bouw): de afnemer geeft de btw zelf aan in rubriek 2a. Het btw-id van de klant staat op de factuur."}],
             **_sup("wit")))

    r = Receipt()
    r.center("TankPunt A20", big=True, field="counterparty")
    r.center("Station Nieuwerkerk")
    r.center("Rijksweg A20, 2913 LV")
    r.gap()
    r.text("02-11-2026 07:14      Pomp 4", field="doc_date", span=(0, 10))
    r.text("Bonnr 3391-0218", field="reference", span=(6, 15))
    r.rule()
    r.lr("DIESEL B7", "71,20")
    r.text("  38,93 L x 1,829 EUR/L")
    r.rule()
    r.lr("TOTAAL EUR", "71,20", bold=True, field="gross_total")
    r.gap()
    r.lr("Netto", "58,84", field="net_total")
    r.lr("BTW 21%", "12,36", field="vat_21")
    r.gap()
    r.lr("Betaald met", "PIN zakelijk")
    r.lr("Kaart", "xxxx xxxx 4471")
    r.gap()
    r.center("Goede reis!")
    add_receipt("M-TANK", "1057", r, _inv(
        "kassabon", "TankPunt A20", "3391-0218", "2026-11-02", 58.84, [{"rate": 21, "base": 58.84, "amount": 12.36}], 71.20,
        [_line("4410", "V21", 58.84, 12.36, "Diesel bestelbus")], "Diesel 38,93 liter",
        booking_reason="Tankbon op de zakelijke pas: brandstof 4410."), seed=11)

    r = Receipt()
    r.center("Bouwmarkt De Schie", big=True, field="counterparty")
    r.center("'s-Gravelandseweg 380, Schiedam")
    r.gap()
    r.text("Datum: 05-11-2026   16:32", field="doc_date", span=(7, 17))
    r.text("Bon:   118-44720", field="reference", span=(7, 16))
    r.rule()
    for a, b in (("2x Siliconenkit transparant", "13,90"), ("Schuurpapier set", "8,50"), ("2x Afdekfolie 4x5 m", "14,50"),
                 ("Kwastenset 5-delig", "11,50")):
        r.lr(a, b)
    r.rule()
    r.lr("Te betalen", "48,40", bold=True, field="gross_total")
    r.lr("Pinbetaling", "48,40")
    r.gap()
    r.lr("Excl. btw", "40,00", field="net_total")
    r.lr("Btw 21%", "8,40", field="vat_21")
    r.gap()
    r.center("Ruilen binnen 30 dagen met bon")
    r.barcode()
    add_receipt("M-BOUWMARKT", "1057", r, _inv(
        "kassabon", "Bouwmarkt De Schie", "118-44720", "2026-11-05", 40.00, [{"rate": 21, "base": 40.00, "amount": 8.40}], 48.40,
        [_line("7000", "V21", 40.00, 8.40, "Klein materiaal")], "Kit, schuurpapier, afdekfolie, kwasten",
        booking_reason="Klein materiaal voor projecten: 7000, zoals de eerdere bonnen van deze bouwmarkt."), seed=17)

    s = S["polis"]
    add("M-POLIS", "1057", invoice(dict(
        sender=s, to=to_lines("1057"), number="2026-771204", title="PREMIENOTA", policy="AVB-5571204",
        meta=[("Notanummer", "2026-771204", "reference"), ("Datum", d_nl("2026-11-01"), "doc_date"),
              ("Vervaldatum", d_nl("2026-11-15"), "due_date"), ("Polisnummer", "AVB-5571204", None)],
        rows=[("Premie Aansprakelijkheidsverzekering Bedrijven", 1, "640,00", "640,00"),
              ("Periode 01-11-2026 tot 01-11-2027", "", "", "")],
        totals=[("Premie", "640,00", False, None), ("Assurantiebelasting 21%", "134,40", False, None),
                ("Te betalen", "774,40", True, "gross_total")],
        notes=["Over verzekeringspremie wordt geen btw berekend. Het polisblad staat op pagina 2."] + pay_note(s, "2026-11-15", "2026-771204"),
        extra=[polis_page2])),
        _inv("inkoopfactuur", s["name"], "2026-771204", "2026-11-01", 774.40, [], 774.40,
             [_line("4700", "GEEN", 774.40, 0.0, "Premie AVB 2026-2027")], "Premie aansprakelijkheidsverzekering (AVB)",
             due_date="2026-11-15", booking_reason="Verzekeringspremie: geen btw, volledig op 4700.",
             split_reason="Pagina 2 is het polisblad bij dezelfde premienota (zelfde afzender en polisnummer).",
             issues=[{"level": "info", "message": "Assurantiebelasting (€ 134,40) is geen omzetbelasting: niet aftrekbaar, het hele bedrag gaat op kosten."}],
             **_sup("polis")))

    add("M-PAKBON", "1057", packing_slip(), {
        "kind": "overig", "client_hint": None, "counterparty": S["bgr"]["name"], "reference": "8811-204",
        "doc_date": "2026-11-04", "due_date": None, "description": "Pakbon bij factuur 26-118204, geen bedragen",
        "gross_total": None, "net_total": None, "vat_lines": [], "lines": [], "currency": "EUR",
        "supplier_vat_number": None, "supplier_kvk": None, "iban": None,
        "booking_reason": "Geen boeking: een pakbon is geen factuur.", "uncertain_fields": [],
        "issues": [{"level": "warning", "message": "Pakbon, geen factuur: niet boeken. Bewaren bij factuur 26-118204 of uit de stapel halen."}]},
        gray=True)

    me = dict(name=C["1057"]["name"], addr=C["1057"]["address"], kvk=C["1057"]["kvk"], vat=C["1057"]["vat"],
              iban=iban("0774120930"), color=(0.95, 0.55, 0.05), style="band", tag="Renovatie · onderhoud · isolatie")
    add("M-VERKOOP", "1057", invoice(dict(
        sender=me, purchase=False, to=["Familie De Graaf", "Burgemeester Knappertlaan 61", "3116 BB Schiedam"], number="2026-031",
        vat_col=True, meta=std_meta("2026-031", "2026-11-06", extra=[("Betalen binnen", "14 dagen", None), ("Bouwjaar woning", "1968", None)]),
        rows=[("Arbeid spouwmuurisolatie (28 uur)", 28, "50,00", "1.400,00", "9%"),
              ("Isolatiemateriaal en afwerking", 1, "620,00", "620,00", "21%")],
        totals=[("Subtotaal excl. btw", "2.020,00", False, "net_total"), ("Btw 9% over 1.400,00", "126,00", False, "vat_9"),
                ("Btw 21% over 620,00", "130,20", False, "vat_21"), ("Totaal", "2.276,20", True, "gross_total")],
        notes=["Verlaagd btw-tarief op arbeid: isoleren van een woning ouder dan twee jaar.",
               f"Graag binnen 14 dagen overmaken op {spaced(me['iban'])}."])),
        _inv("verkoopfactuur", "Familie De Graaf", "2026-031", "2026-11-06", 2020.00,
             [{"rate": 9, "base": 1400.00, "amount": 126.00}, {"rate": 21, "base": 620.00, "amount": 130.20}], 2276.20,
             [_line("8010", "O9", 1400.00, 126.00, "Arbeid isolatie"), _line("8000", "O21", 620.00, 130.20, "Isolatiemateriaal")],
             "Spouwmuurisolatie woning", booking_reason="Eigen verkoopfactuur: arbeid laag tarief, materiaal hoog tarief, één regel per tarief.",
             issues=[{"level": "info", "message": "Verlaagd tarief op arbeid kan alleen bij een woning ouder dan twee jaar. Het bouwjaar (1968) staat op de factuur."}]))

    # ---- dossier 1063: Eetcafé De Kade V.O.F.
    s = S["maashal"]
    add("K-MAASHAL", "1063", invoice(dict(
        sender=s, to=to_lines("1063"), number="MH-26-448120", vat_col=True, row_h=5.2, reserve=30,
        meta=std_meta("MH-26-448120", "2026-11-02", "2026-11-16", [("Pasnummer", "0063 4418 207", None)]),
        rows=maashal_rows(1846.20, 652.40, 46, 20, 3),
        totals=[("Grondslag 9%", "1.846,20", False, None), ("Btw 9%", "166,16", False, "vat_9"),
                ("Grondslag 21%", "652,40", False, None), ("Btw 21%", "137,00", False, "vat_21"),
                ("Totaal excl. btw", "2.498,60", False, "net_total"), ("Te betalen", "2.801,76", True, "gross_total")],
        notes=pay_note(s, "2026-11-16", "MH-26-448120"))),
        _inv("inkoopfactuur", s["name"], "MH-26-448120", "2026-11-02", 2498.60,
             [{"rate": 9, "base": 1846.20, "amount": 166.16}, {"rate": 21, "base": 652.40, "amount": 137.00}], 2801.76,
             [_line("7000", "V9", 1846.20, 166.16, "Inkoop keuken (laag tarief)"), _line("7010", "V21", 652.40, 137.00, "Dranken en non-food (hoog tarief)")],
             "Weekinkoop groothandel", due_date="2026-11-16",
             booking_reason="Vaste groothandel: laag tarief op 7000, hoog tarief op 7010, één regel per btw-tarief.",
             split_reason="Drie pagina's met hetzelfde factuurnummer en doorlopende paginanummering.", **_sup("maashal")))

    s = S["kuip"]
    kuip = dict(sender=s, to=to_lines("1063"), number="K-2611-0193", meta=std_meta("K-2611-0193", "2026-11-03", "2026-11-17"),
                rows=[("Huiswijn wit, doos 6 flessen", 8, "31,50", "252,00"), ("Huiswijn rood, doos 6 flessen", 6, "33,00", "198,00"),
                      ("Pils fust 50 liter", 2, "112,00", "224,00"), ("Jonge jenever 1 liter", 4, "15,00", "60,00")],
                totals=[("Subtotaal excl. btw", "734,00", False, "net_total"), ("Btw 21%", "154,14", False, "vat_21"),
                        ("Totaal", "888,14", True, "gross_total")],
                notes=pay_note(s, "2026-11-17", "K-2611-0193"))
    kuip_answer = lambda: _inv("inkoopfactuur", s["name"], "K-2611-0193", "2026-11-03", 734.00, [{"rate": 21, "base": 734.00, "amount": 154.14}],
                               888.14, [_line("7010", "V21", 734.00, 154.14, "Wijn, bier en gedistilleerd")], "Dranken",
                               due_date="2026-11-17", booking_reason="Vaste leverancier: dranken op 7010.", **_sup("kuip"))
    kuip_pdf = invoice(kuip)
    add("K-KUIP", "1063", kuip_pdf, kuip_answer())

    s2 = S["blinkend"]
    add("K-BLINKEND", "1063", invoice(dict(
        sender=s2, to=to_lines("1063"), number="2026-1104", meta=std_meta("2026-1104", "2026-11-04", "2026-11-18"),
        rows=[("Schoonmaak horecaruimte oktober (12 uur)", 12, "15,00", "180,00")],
        totals=[("Subtotaal excl. btw", "180,00", False, "net_total"), ("Btw 21%", "38,80", False, "vat_21"),
                ("Totaal", "218,80", True, "gross_total")],
        notes=pay_note(s2, "2026-11-18", "2026-1104"))),
        _inv("inkoopfactuur", s2["name"], "2026-1104", "2026-11-04", 180.00, [{"rate": 21, "base": 180.00, "amount": 38.80}],
             218.80, [_line("4210", "V21", 180.00, 37.80, "Schoonmaak oktober")], "Schoonmaak oktober",
             due_date="2026-11-18", booking_reason="Vaste leverancier. De btw op de factuur is fout; het boekvoorstel gebruikt de juiste 37,80.",
             **_sup("blinkend")))

    s3 = S["pinpunt"]
    add("K-PINPUNT", "1063", invoice(dict(
        sender=s3, to=to_lines("1063"), number="PP-1026-771", meta=std_meta("PP-1026-771", "2026-11-01", "2026-11-10"),
        rows=[("Huur betaalautomaat oktober", 1, "19,95", "19,95"), ("Abonnement en service", 1, "10,00", "10,00")],
        totals=[("Subtotaal excl. btw", "29,95", False, "net_total"), ("Btw 21%", "6,29", False, "vat_21"),
                ("Totaal", "36,24", True, "gross_total")],
        notes=["Dit bedrag wordt automatisch afgeschreven."])),
        _inv("inkoopfactuur", s3["name"], "PP-1026-771", "2026-11-01", 29.95, [{"rate": 21, "base": 29.95, "amount": 6.29}],
             36.24, [_line("4800", "V21", 29.95, 6.29, "Pinautomaat oktober")], "Huur en abonnement pinautomaat",
             due_date="2026-11-10", booking_reason="Vaste leverancier: pinkosten op 4800.", **_sup("pinpunt")), gray=True)

    add("K-KUIP-2", "1063", kuip_pdf, kuip_answer(), gray=True)

    add("K-BELASTING", "1063", tax_letter(), {
        "kind": "belastingdienst", "client_hint": None, "counterparty": "Belastingdienst",
        "reference": "8588.77.120.F.01.6090", "doc_date": "2026-11-03", "due_date": "2026-12-15",
        "description": "Naheffingsaanslag omzetbelasting 3e kwartaal 2026: verzuimboete € 68", "gross_total": 68.00,
        "net_total": None, "vat_lines": [], "lines": [], "currency": "EUR", "supplier_vat_number": None,
        "supplier_kvk": None, "iban": None,
        "booking_reason": "Geen inkoopboeking: de boete is niet aftrekbaar. Naar het dossier en de betaaldatum naar de agenda.",
        "uncertain_fields": [],
        "issues": [{"level": "warning", "message": "Verzuimboete € 68: betalen uiterlijk 15-12-2026. Bezwaartermijn zes weken na 03-11-2026."}]})

    # ---- dossier 1001: Studio Lindewerf B.V.
    s = S["vektor"]
    p = Pen()
    header(p, s, purchase=True, show_iban=False)
    address(p, to_lines("1001") + ["The Netherlands"], s["style"])
    p.put(20 * mm, H - 92 * mm, "INVOICE", "Helvetica-Bold", 14)
    meta(p, [("Invoice number", "INV-2026-1104", "reference"), ("Invoice date", "04 Nov 2026", "doc_date"),
             ("Customer VAT ID", C["1001"]["vat"], None), ("Payment", "Direct debit", None)], s["style"])
    y = H - 125 * mm
    p.put(20 * mm, y, "Description", "Helvetica-Bold", 9)
    p.put(190 * mm, y, "Amount", "Helvetica-Bold", 9, align="r")
    p.c.line(20 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)
    p.put(20 * mm, y - 8 * mm, "Design Suite Team, monthly subscription (5 seats), November 2026", "Helvetica", 9)
    p.put(190 * mm, y - 8 * mm, "EUR 54.00", "Helvetica", 9, align="r")
    totals(p, y - 24 * mm, [("Subtotal", "EUR 54.00", False, "net_total"), ("VAT 0% (reverse charge)", "EUR 0.00", False, "vat_0"),
                            ("Total due", "EUR 54.00", True, "gross_total")], s["style"])
    notes(p, y - 52 * mm, ["VAT reverse charged to the customer under Article 196 of Council Directive 2006/112/EC.",
                           [("Collected by direct debit from your account. Our bank: ", None), (spaced(s["iban"]), "iban")]], s["style"])
    p.footer()
    vektor_pdf = p.done()
    add("L-VEKTOR", "1001", vektor_pdf, _inv(
        "inkoopfactuur", s["name"], "INV-2026-1104", "2026-11-04", 54.00, [], 54.00,
        [_line("4330", "EU_DIENST_VERL", 54.00, 0.0, "Ontwerpsoftware november")], "Abonnement ontwerpsoftware november",
        booking_reason="Dienst uit Ierland, btw verlegd: zelfde rekening en btw-code als de eerdere maanden. Aangeven in rubriek 4b, aftrekken in 5b.",
        supplier_vat_number=s["vat"], iban=s["iban"]))

    r = Receipt()
    r.center("Brasserie Maaszicht", big=True, field="counterparty")
    r.center("Boompjes 701, Rotterdam")
    r.gap()
    r.text("Tafel 12      2 couverts")
    r.text("05-11-2026 21:48    Bon 0281", field="doc_date", span=(0, 10))
    r.rule()
    for a, b in (("2 Dagmenu 3 gangen", "65,00"), ("2 Mineraalwater 75 cl", "8,40"), ("2 Koffie", "6,50"),
                 ("2 Verse muntthee", "6,50")):
        r.lr(a, b)
    r.rule()
    r.lr("TOTAAL", "86,40", bold=True, field="gross_total")
    r.gap()
    r.text("BTW  9% over 79,27", field="net_total", span=(13, 18))
    r.lr("BTW-bedrag", "7,13", field="vat_9")
    r.lr("Betaald", "PIN")
    r.gap()
    r.center("Dank voor uw bezoek")
    add_receipt("L-BRASSERIE", "1001", r, _inv(
        "kassabon", "Brasserie Maaszicht", "0281", "2026-11-05", 79.27, [{"rate": 9, "base": 79.27, "amount": 7.13}], 86.40,
        [_line("4520", "GEEN_AFTREK", 86.40, 0.0, "Zakelijk diner, 2 personen")], "Diner 2 personen",
        booking_reason="Horeca: btw is niet aftrekbaar, het hele bedrag op representatie.",
        issues=[{"level": "warning", "message": "Horecabon: btw niet aftrekbaar. Zakelijk doel en naam van de gast ontbreken: navragen bij de klant."}]),
        seed=23, amp=2.6)

    s = S["drukkerij"]
    add("L-DRUKKERIJ", "1001", invoice(dict(
        sender=s, to=to_lines("1001"), number="F26-1342", meta=std_meta("F26-1342", "2026-10-30", "2026-11-13"),
        rows=[("Flyers A5, 2.500 stuks, 135 grams silk", 1, "245,00", "245,00"), ("Opmaak en drukproef", 1, "65,00", "65,00")],
        totals=[("Subtotaal excl. btw", "310,00", False, "net_total"), ("Btw 21%", "65,10", False, "vat_21"),
                ("Totaal", "375,10", True, "gross_total")],
        notes=pay_note(s, "2026-11-13", "F26-1342"))),
        _inv("inkoopfactuur", s["name"], "F26-1342", "2026-10-30", 310.00, [{"rate": 21, "base": 310.00, "amount": 65.10}],
             375.10, [_line("4600", "V21", 310.00, 65.10, "Flyers wintercampagne")], "Flyers wintercampagne",
             due_date="2026-11-13", booking_reason="Drukwerk voor reclame: 4600, zoals de eerdere factuur van deze drukkerij.",
             **_sup("drukkerij")))

    r = Receipt()
    r.center("Speelgoedplein", big=True, field="counterparty")
    r.center("Lijnbaan 44, Rotterdam")
    r.gap()
    r.text("Kassabon 7781-0932", field="reference", span=(9, 18))
    r.text("01-11-2026  14:05", field="doc_date", span=(0, 10))
    r.rule()
    for a, b in (("Bouwdoos ruimteschip", "34,99"), ("Knutselset glitter", "14,99"), ("Puzzel 500 stukjes", "10,00")):
        r.lr(a, b)
    r.rule()
    r.lr("TOTAAL", "59,98", bold=True, field="gross_total")
    r.lr("Waarvan btw 21%", "10,41", field="vat_21")
    r.lr("Excl. btw", "49,57", field="net_total")
    r.lr("PIN", "59,98")
    r.gap()
    r.center("Cadeaubon? Vraag ernaar!")
    r.barcode()
    add_receipt("L-SPEELGOED", "1001", r, _inv(
        "kassabon", "Speelgoedplein", "7781-0932", "2026-11-01", 49.57, [{"rate": 21, "base": 49.57, "amount": 10.41}], 59.98,
        [_line("1800", "PRIVE", 59.98, 0.0, "Privé-aankoop via de zaak")], "Speelgoed",
        booking_reason="Lijkt een privé-aankoop: voorstel rekening-courant, geen btw-aftrek.",
        issues=[{"level": "warning", "message": "Lijkt privé (speelgoed): voorstel rekening-courant DGA zonder btw-aftrek. Bevestigen of navragen bij de klant."}]),
        seed=29)

    return docs, {"vektor": vektor_pdf}


# page order of the pile: cover sheets, blank back sides and the documents
ORDER = ["cover:1042", "B-MOLEN", "blank", "B-ENERGIE", "B-PAKHUIS", "blank", "B-KNEDER", "B-SUPERVERS", "B-BELNET", "blank",
         "B-VERKOOP", "B-RENOVA",
         "cover:1057", "M-BGR-KB", "blank", "M-BGR-IBAN", "M-WIT", "M-TANK", "M-BOUWMARKT", "blank", "M-POLIS", "M-PAKBON",
         "M-VERKOOP",
         "cover:1063", "K-MAASHAL", "blank", "K-KUIP", "K-BLINKEND", "blank", "K-PINPUNT", "K-KUIP-2", "K-BELASTING",
         "cover:1001", "L-VEKTOR", "L-BRASSERIE", "blank", "L-DRUKKERIJ", "L-SPEELGOED"]


# ---------------------------------------------------------------- reference data
def history_rows():
    """Earlier bookings per dossier: what makes 'known supplier, usual account, known IBAN' checkable."""
    out = {nr: [] for nr in ("1042", "1057", "1063")}
    r = random.Random(77)

    def series(nr, key_or_name, n, account, code, amount, prefix, start_month=None, jitter=0.0, ibans=True, day=None):
        s = S.get(key_or_name)
        name = s["name"] if s else key_or_name
        months = list(range(11 - n, 11)) if n <= 10 else None
        for i in range(n):
            if months:
                m, dd = months[i], day or r.randint(1, 26)
            else:  # more often than monthly: spread over the year
                m, dd = 1 + (i * 10) // n, 2 + (i * 13) % 25
            gross = round(amount * (1 + r.uniform(-jitter, jitter)), 2)
            out[nr].append({"supplier_name": name, "invoice_number": f"{prefix}{m:02d}{i:02d}" if prefix else "",
                            "invoice_date": f"2026-{m:02d}-{dd:02d}", "gross_total": f"{gross:.2f}", "account": account,
                            "vat_code": code, "iban": s["iban"] if s and ibans else ""})

    series("1042", "molen", 14, "7000", "V9", 640.0, "2026-3", jitter=0.12)
    series("1042", "energie", 10, "4200", "V21", 850.0, "88120334-", jitter=0.15, day=1)
    series("1042", "belnet", 10, "4310", "V21", 66.55, "BM26-", day=1)
    series("1042", "renova", 10, "4250", "V21", 104.06, "RB-26", jitter=0.05)
    series("1042", "pakhuis", 6, "7100", "V21", 210.0, "F-26-0", jitter=0.2)
    series("1042", "techniek", 2, "4300", "V21", 286.0, "BTZ-26", jitter=0.1)
    series("1042", "SuperVers Schiedam", 5, "4530", "V9+V21", 31.0, "", jitter=0.3)
    series("1057", "bgr", 12, "7000", "V21", 1480.0, "26-10", jitter=0.4)
    series("1057", "wit", 3, "7200", "NL_VERL", 1620.0, "2026-0", jitter=0.2)
    series("1057", "TankPunt A20", 9, "4410", "V21", 78.0, "", jitter=0.15)
    series("1057", "Bouwmarkt De Schie", 7, "7000", "V21", 62.0, "", jitter=0.5)
    series("1057", "polis", 1, "4700", "GEEN", 742.50, "2025-77")
    series("1063", "maashal", 10, "7000+7010", "V9+V21", 2650.0, "MH-26-3", jitter=0.12)
    series("1063", "kuip", 8, "7010", "V21", 820.0, "K-26", jitter=0.2)
    series("1063", "blinkend", 10, "4210", "V21", 217.80, "2026-0", day=4)
    series("1063", "pinpunt", 10, "4800", "V21", 36.24, "PP-0", day=1)
    return out


LINDEWERF_HISTORY = [  # the Q3 history of the older sample, now with account, VAT code and IBAN where known
    ("Belnet Mobiel B.V.", "BM26-0715", "2026-07-01", "48.40", "4310", "V21", "NL70TEST4410020011"),
    ("Pandhuis Rotterdam Beheer B.V.", "2026-07-HS118", "2026-07-01", "1512.50", "4100", "V21", "NL21TEST7001882233"),
    ("Vektorhub Ltd", "INV-2026-0704", "2026-07-04", "54.00", "4330", "EU_DIENST_VERL", S["vektor"]["iban"]),
    ("Hostvlak B.V.", "HV-26705", "2026-07-05", "18.15", "4340", "V21", ""),
    ("TankPunt A20 Nieuwerkerk", "", "2026-07-09", "86.39", "4410", "V21", ""),
    ("SuperVers Kralingen", "BON 3102", "2026-07-14", "19.00", "4530", "V9", ""),
    ("Pandhuis Rotterdam Beheer B.V.", "2026-08-HS118", "2026-07-22", "1512.50", "4100", "V21", "NL21TEST7001882233"),
    ("RailNed Reizigers B.V.", "", "2026-07-22", "25.20", "4510", "V9", ""),
    ("Drukkerij Van Wijk B.V.", "F26-0962", "2026-07-24", "102.85", "4600", "V21", "NL48TEST2410119900"),
    ("Schoonmaakbedrijf Blinkend", "2026-0728", "2026-07-28", "217.80", "4110", "V21", "NL13TEST7712044500"),
    ("Belnet Mobiel B.V.", "BM26-0815", "2026-08-01", "48.40", "4310", "V21", "NL70TEST4410020011"),
    ("Vektorhub Ltd", "INV-2026-0804", "2026-08-04", "54.00", "4330", "EU_DIENST_VERL", S["vektor"]["iban"]),
    ("Hostvlak B.V.", "HV-26805", "2026-08-05", "18.15", "4340", "V21", ""),
    ("TankPunt A20 Nieuwerkerk", "", "2026-08-05", "79.98", "4410", "V21", ""),
    ("Kantoorhuis Maas B.V.", "KM-2026-0812", "2026-08-14", "150.04", "4320", "V21", ""),
    ("ParkeerCentrum Blaak", "", "2026-08-19", "10.41", "4420", "V21", ""),
    ("Pandhuis Rotterdam Beheer B.V.", "2026-09-HS118", "2026-08-22", "1512.50", "4100", "V21", "NL21TEST7001882233"),
    ("SuperVers Kralingen", "BON 3877", "2026-08-27", "20.93", "4530", "V9", ""),
    ("Schoonmaakbedrijf Blinkend", "2026-0828", "2026-08-28", "217.80", "4110", "V21", "NL13TEST7712044500"),
    ("Fotografie Noorderlicht", "2026-024", "2026-08-28", "363.00", "4700", "V21", ""),
    ("Vektorhub Ltd", "INV-2026-0904", "2026-09-04", "54.00", "4330", "EU_DIENST_VERL", S["vektor"]["iban"]),
    ("Vektorhub Ltd", "INV-2026-1004", "2026-10-04", "54.00", "4330", "EU_DIENST_VERL", S["vektor"]["iban"]),
]
HIST_COLS = ["supplier_name", "invoice_number", "invoice_date", "gross_total", "account", "vat_code", "iban"]

MKB_CHART = [  # [account, name, RGS code]. RGS codes only where checked against the published RGS table (3.7).
    ["0210", "Inventaris", "BMvaBeiVvp"], ["0220", "Machines en installaties", "BMvaMeiVvp"],
    ["1100", "Bank", "BLimBanRba"], ["1300", "Debiteuren", "BVorDebHad"], ["1400", "Vooruitbetaalde facturen", "BVorOvaVof"],
    ["1510", "Voorbelasting btw", "BSchBepBoo"], ["1520", "Te betalen omzetbelasting", "BSchBepBtw"],
    ["1600", "Crediteuren", "BSchCreHac"], ["1800", "Privé / rekening-courant ondernemer", ""],
    ["4100", "Huur bedrijfsruimte", "WBedHuiBeh"], ["4200", "Gas, water en elektra", "WBedHuiGwe"],
    ["4210", "Schoonmaakkosten", "WBedHuiSch"], ["4250", "Afvalverwerking", ""],
    ["4300", "Onderhoud machines en inventaris", "WBedEemOme"], ["4310", "Telefoon en internet", "WBedKanTef"],
    ["4320", "Kantoorbenodigdheden", "WBedKanKan"], ["4330", "Software en abonnementen", "WBedKanSof"],
    ["4410", "Brandstof", "WBedAutBra"], ["4420", "Parkeerkosten", "WBedAutPar"],
    ["4520", "Representatiekosten", "WBedVkkRep"], ["4530", "Kantinekosten", ""],
    ["4600", "Reclame- en advertentiekosten", "WBedVkkRea"], ["4700", "Verzekeringen bedrijf", "WBedAssBea"],
    ["4800", "Bank- en pinkosten", "WFbeOrlRkb"], ["4900", "Algemene kosten", "WBedAlkOal"],
    ["4960", "Kredietbeperking en betalingsverschillen", ""],
    ["7000", "Inkoop grond- en hulpstoffen", "WKprKvgKvg"], ["7010", "Inkoop handelsgoederen", "WKprInhInh"],
    ["7100", "Verpakkingsmaterialen", "WBedEemVpm"], ["7200", "Uitbesteed werk", "WKprKuwKuw"],
    ["8000", "Omzet diensten hoog tarief", "WOmzNodOdh"], ["8010", "Omzet diensten laag tarief", "WOmzNodOdl"],
    ["8100", "Omzet goederen laag tarief", "WOmzNopOlv"], ["8110", "Omzet goederen hoog tarief", "WOmzNopOlh"],
]
RGS_LINDEWERF = {"1100": "BLimBanRba", "1300": "BVorDebHad", "1400": "BVorOvaVof", "1510": "BSchBepBoo",
                 "1520": "BSchBepBtw", "1600": "BSchCreHac", "4100": "WBedHuiBeh", "4110": "WBedHuiSch",
                 "4310": "WBedKanTef", "4320": "WBedKanKan", "4330": "WBedKanSof", "4410": "WBedAutBra",
                 "4420": "WBedAutPar", "4520": "WBedVkkRep", "4600": "WBedVkkRea", "4700": "WKprKuwKuw",
                 "4800": "WFbeOrlRkb", "4900": "WBedAlkOal", "8000": "WOmzNodOdh"}
RGS_STANDARD = {"1100": "BLimBanRba", "1600": "BSchCreHac", "4100": "WBedHuiBeh", "4310": "WBedKanTef",
                "4320": "WBedKanKan", "4410": "WBedAutBra", "4600": "WBedVkkRea", "4800": "WFbeOrlRkb",
                "7000": "WKprInhInh", "8000": "WOmzNohOlh", "8100": "WOmzNohOlv"}

BANK = {  # fictional bank lines of early November; negative = paid from the account
    "1042": [("2026-11-02", "incasso", "Belnet Mobiel B.V. BM26-1107733", -66.55),
             ("2026-11-06", "pin", "SuperVers Schiedam Centrum", -33.41),
             ("2026-11-09", "pin", "Parkeergarage Koemarkt Schiedam", -12.50),
             ("2026-11-10", "bij", "Lunchroom Het Plein fact 2026-0412", 529.74)],
    "1057": [("2026-11-02", "pin", "TankPunt A20 Nieuwerkerk", -71.20),
             ("2026-11-03", "incasso", "Polderland Verzekeringen 2026-771204", -774.40),
             ("2026-11-05", "pin", "Bouwmarkt De Schie Schiedam", -48.40),
             ("2026-11-08", "pin", "Bouwmarkt De Schie Schiedam", -142.50),
             ("2026-11-12", "pin", "TankPunt A20 Nieuwerkerk", -84.50)],
    "1063": [("2026-11-03", "incasso", "PinPunt Betaaldiensten PP-1026-771", -36.24),
             ("2026-11-04", "ideal", "Bedrijfskleding Online B.V.", -219.00),
             ("2026-11-05", "overboeking", "Horecagroothandel Maashal MH-26-448120", -2801.76)],
    "1001": [("2026-11-01", "pin", "Speelgoedplein Rotterdam", -59.98),
             ("2026-11-04", "incasso", "Vektorhub Ltd INV-2026-1104", -54.00),
             ("2026-11-05", "pin", "Brasserie Maaszicht Rotterdam", -86.40)],
}


# ---------------------------------------------------------------- the client's reply photos and sandbox files
SHOPS = {  # the merchant as the client knows it (a pin line also carries the place)
    "TankPunt A20 Nieuwerkerk": "TankPunt A20", "Bouwmarkt De Schie Schiedam": "Bouwmarkt De Schie",
    "SuperVers Schiedam Centrum": "SuperVers", "Parkeergarage Koemarkt Schiedam": "Parkeergarage Koemarkt",
    "Speelgoedplein Rotterdam": "Speelgoedplein", "Brasserie Maaszicht Rotterdam": "Brasserie Maaszicht",
    "Bedrijfskleding Online B.V.": "Bedrijfskleding Online",
}


def reply_receipts():
    out = {}
    r = Receipt()
    r.center("TankPunt A20", big=True, field="counterparty")
    r.center("Station Nieuwerkerk")
    r.center("Rijksweg A20, 2913 LV")
    r.gap()
    r.text("12-11-2026 06:52      Pomp 2", field="doc_date", span=(0, 10))
    r.text("Bonnr 3391-0977", field="reference", span=(6, 15))
    r.rule()
    r.lr("DIESEL B7", "84,50")
    r.text("  46,20 L x 1,829 EUR/L")
    r.rule()
    r.lr("TOTAAL EUR", "84,50", bold=True, field="gross_total")
    r.gap()
    r.lr("Netto", "69,83", field="net_total")
    r.lr("BTW 21%", "14,67", field="vat_21")
    r.gap()
    r.lr("Betaald met", "PIN zakelijk")
    r.lr("Kaart", "xxxx xxxx 4471")
    r.gap()
    r.center("Goede reis!")
    img, q = r.render(41)
    img, q = crumple(img, q, 41, amp=4.5, folds=3)
    photo, q = phone_photo(img, q, 41, "dark")
    out["tankpunt"] = (photo, norm_boxes(q, photo.size, 5), _inv(
        "kassabon", "TankPunt A20", "3391-0977", "2026-11-12", 69.83, [{"rate": 21, "base": 69.83, "amount": 14.67}], 84.50,
        [_line("4410", "V21", 69.83, 14.67, "Diesel bestelbus")], "Diesel 46,20 liter",
        booking_reason="Tankbon op de zakelijke pas: brandstof 4410, zoals de eerdere bonnen."))

    r = Receipt()
    r.center("Bouwmarkt De Schie", big=True, field="counterparty")
    r.center("'s-Gravelandseweg 380, Schiedam")
    r.gap()
    r.text("Datum: 08-11-2026   09:18", field="doc_date", span=(7, 17))
    r.text("Bon:   118-45102", field="reference", span=(7, 16))
    r.rule()
    for a, b in (("6x Cementmortel 25 kg", "47,70"), ("3x PVC buis 110 mm 4 m", "43,80"), ("2x Spaanplaatschr. 5x70", "25,00"),
                 ("4x Purschuim 750 ml", "26,00")):
        r.lr(a, b)
    r.rule()
    r.lr("Te betalen", "142,50", bold=True, field="gross_total")
    r.lr("Pinbetaling", "142,50")
    r.gap()
    r.lr("Excl. btw", "117,77", field="net_total")
    r.lr("Btw 21%", "24,73", field="vat_21")
    r.gap()
    r.center("Ruilen binnen 30 dagen met bon")
    r.barcode()
    img, q = r.render(43)
    img, q = crumple(img, q, 43, amp=4.0, folds=2)
    photo, q = phone_photo(img, q, 43, "wood")
    out["deschie"] = (photo, norm_boxes(q, photo.size, 5), _inv(
        "kassabon", "Bouwmarkt De Schie", "118-45102", "2026-11-08", 117.77, [{"rate": 21, "base": 117.77, "amount": 24.73}], 142.50,
        [_line("7000", "V21", 117.77, 24.73, "Mortel, pvc, schroeven, purschuim")], "Bouwmaterialen klein",
        booking_reason="Klein materiaal voor projecten: 7000, zoals de eerdere bonnen van deze bouwmarkt."))
    return out


def sandbox_files(extra):
    """Three files a visitor can run through the in-browser sandbox: a crumpled horeca receipt (photo), a two-page
    wholesaler invoice with 9% and 21% (PDF with a text layer) and a foreign reverse-charge invoice (PDF)."""
    folder = STATIC / "sandbox"
    folder.mkdir(parents=True, exist_ok=True)
    r = Receipt(cols=32, px=32)
    r.center("Eetcafe 't Anker", big=True)
    r.center("Veerhaven 9, Rotterdam")
    r.center("KvK 24550817")
    r.gap()
    r.text("Tafel 7        Bon 004512")
    r.text("Datum 07-11-2026   13:26")
    r.rule()
    for a, b in (("2 Uitsmijter ham/kaas", "19,50"), ("2 Cappuccino", "6,60"), ("1 Appeltaart", "5,00"),
                 ("2 Speciaalbier", "11,20"), ("1 Huiswijn glas", "5,50")):
        r.lr(a, b)
    r.rule()
    r.lr("TOTAAL", "47,80", bold=True)
    r.gap()
    r.text("BTW      excl.     btw    incl.")
    r.text(" 9%     28,53    2,57   31,10")
    r.text("21%     13,80    2,90   16,70")
    r.gap()
    r.lr("Betaald", "PIN")
    r.center("Bedankt en tot ziens!")
    img, q = r.render(51)
    img, q = crumple(img, q, 51, amp=5.5, folds=4)
    photo, _ = phone_photo(img, q, 51, "wood", size=(1100, 1500))
    photo.save(folder / "kassabon-eetcafe.jpg", quality=84)

    s = S["maashal"]
    data, _ = invoice(dict(
        sender=s, to=["Eetcafé De Kade V.O.F.", "Maaskade 48", "3071 NE Rotterdam"], number="MH-26-447702", vat_col=True, row_h=5.2,
        reserve=30, meta=std_meta("MH-26-447702", "2026-10-26", "2026-11-09", [("Pasnummer", "0063 4418 207", None)]),
        rows=maashal_rows(842.60, 318.90, 30, 12, 9),
        totals=[("Grondslag 9%", "842,60", False, None), ("Btw 9%", "75,83", False, None),
                ("Grondslag 21%", "318,90", False, None), ("Btw 21%", "66,97", False, None),
                ("Totaal excl. btw", "1.161,50", False, None), ("Te betalen", "1.304,30", True, None)],
        notes=pay_note(s, "2026-11-09", "MH-26-447702")))
    (folder / "factuur-groothandel-9-21.pdf").write_bytes(data)
    (folder / "invoice-reverse-charge.pdf").write_bytes(extra["vektor"][0])


# ---------------------------------------------------------------- main
REPLY_FOR = {"tankpunt": ("1057", "B1057-05"), "deschie": ("1057", "B1057-04")}  # reply photo -> client, bank line


def staged_merge(by_key):
    """Two receipts offered as ONE document on purpose. The pile was scanned without a blank sheet between them, the
    reader doubts and says so, and the reviewer splits with one key (S). After the split each part matches its own
    answer in this manifest again."""
    first, second = by_key["M-TANK"]["answer"], by_key["M-BOUWMARKT"]["answer"]
    answer = json.loads(json.dumps(first))
    answer["issues"] = [{"level": "warning", "message":
                         f"Twee kassabonnen op één document: pagina 1 is van {first['counterparty']}, pagina 2 van "
                         f"{second['counterparty']}. Splits het document (toets S), dan krijgt elke bon een eigen voorstel."}]
    answer["booking_reason"] = "Alleen de eerste bon is gelezen. Na het splitsen krijgt elke bon een eigen voorstel."
    answer["split_reason"] = "twee bonnen direct na elkaar gescand, zonder leeg vel ertussen"
    return {"keys": ["M-TANK", "M-BOUWMARKT"], "answer": answer}


def main():
    debug = Path(sys.argv[sys.argv.index("--debug") + 1]) if "--debug" in sys.argv else None
    OUT.mkdir(exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)
    docs, extra = build_documents()
    by_key = {d["key"]: d for d in docs}
    assert [k for k in ORDER if k not in ("blank",) and not k.startswith("cover")] == [d["key"] for d in docs]

    pages, manifest_docs = [], []
    for key in ORDER:
        if key == "blank":
            pages.append(("blank", None, blank_page()))
        elif key.startswith("cover:"):
            nr = key.split(":")[1]
            img, _ = scanned(render_pdf(coversheet.pdf([C[nr]], "Scanstraat", FIRM)), {})
            pages.append(("cover", nr, img))
        else:
            d = by_key[key]
            first = len(pages) + 1
            boxes = {}
            for i, (img, b) in enumerate(d["pages"]):
                pages.append(("page", key, img))
                for field, box in b.items():
                    boxes.setdefault(field, {"page": first + i, "box": box})   # the first page a field appears on
            manifest_docs.append({"key": key, "client": d["client"], "pages": list(range(first, first + len(d["pages"]))),
                                  "answer": d["answer"], "boxes": boxes})
    assert len(pages) == 42, len(pages)

    stack = OUT / STACK
    imgs = [p[2] for p in pages]
    imgs[0].save(stack, "PDF", resolution=DPI, save_all=True, append_images=imgs[1:], quality=66)
    manifest = {"file": STACK, "sha256": hashlib.sha256(stack.read_bytes()).hexdigest(), "as_of": AS_OF, "firm": FIRM,
                "title": "De schoenendoos: 42 pagina's, 4 dossiers",
                "pages": [{"n": i, "kind": k, "ref": ref} for i, (k, ref, _) in enumerate(pages, 1)],
                "documents": manifest_docs}

    # reply photos (the client's WhatsApp answer) and their answers
    replies = reply_receipts()
    manifest["replies"] = {}
    for name, (photo, boxes, answer) in replies.items():
        path = STATIC / f"wa-bon-{name}.jpg"
        photo.save(path, quality=82)
        nr, line = REPLY_FOR[name]
        manifest["replies"][name] = {"image": f"demo/wa-bon-{name}.jpg", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                     "client": nr, "bank": line, "size": list(photo.size), "answer": answer,
                                     "boxes": {k: {"page": 1, "box": v} for k, v in boxes.items()}}
    manifest["staged"] = [staged_merge(by_key)]
    (OUT / "schoenendoos.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")

    # reference data
    (OUT / "clients.json").write_text(json.dumps(CLIENTS, indent=1, ensure_ascii=False), encoding="utf-8")
    charts = json.loads((OUT / "charts.json").read_text(encoding="utf-8"))
    charts["mkb"] = MKB_CHART
    charts["lindewerf"] = [[a[0], a[1], RGS_LINDEWERF.get(a[0], "")] for a in charts["lindewerf"]]
    charts["standaard"] = [[a[0], a[1], RGS_STANDARD.get(a[0], "")] for a in charts["standaard"]]
    (OUT / "charts.json").write_text(json.dumps(charts, indent=1, ensure_ascii=False), encoding="utf-8")
    for nr, rows in history_rows().items():
        folder = OUT / "reference" / nr
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "ledger_history.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, HIST_COLS)
            w.writeheader()
            w.writerows(sorted(rows, key=lambda x: x["invoice_date"]))
    with (OUT / "reference" / "1001" / "ledger_history.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HIST_COLS)
        w.writerows(LINDEWERF_HISTORY)
    bank = {nr: [{"id": f"B{nr}-{i:02d}", "date": d, "type": t, "description": desc, "name": SHOPS.get(desc, ""), "amount": amt}
                 for i, (d, t, desc, amt) in enumerate(rows, 1)] for nr, rows in BANK.items()}
    (OUT / "bank.json").write_text(json.dumps(bank, indent=1, ensure_ascii=False), encoding="utf-8")

    # homepage assets: one sprite with all 42 pages, and the pages the hero scenarios zoom in on
    tw, th = 120, 170
    sprite = Image.new("RGB", (tw * 14, th * 3), "white")
    for i, im in enumerate(imgs):
        sprite.paste(im.resize((tw, th), Image.LANCZOS), ((i % 14) * tw, (i // 14) * th))
    sprite.save(STATIC / "stapel-42.jpg", quality=72)
    for key in ("M-BGR-IBAN", "M-BGR-KB", "B-MOLEN", "K-MAASHAL"):
        img = by_key[key]["pages"][-1 if key == "K-MAASHAL" else 0][0]
        img.resize((900, 1273), Image.LANCZOS).save(STATIC / f"pagina-{key.lower()}.jpg", quality=78)
    sandbox_files({"vektor": extra["vektor"]})

    if debug:
        debug.mkdir(parents=True, exist_ok=True)
        colours = {"counterparty": "#475569", "reference": "#2563eb", "doc_date": "#0891b2", "due_date": "#7c3aed",
                   "iban": "#d97706", "vat_21": "#059669", "vat_9": "#65a30d", "vat_0": "#64748b", "net_total": "#4f46e5",
                   "gross_total": "#0b1f33", "supplier_kvk": "#be185d", "supplier_vat_number": "#be185d"}
        for d in manifest_docs:
            for n in d["pages"]:
                im = imgs[n - 1].copy()
                dr = ImageDraw.Draw(im)
                for f, b in d["boxes"].items():
                    if b["page"] == n:
                        x0, y0, x1, y1 = b["box"]
                        dr.rectangle([x0 * im.size[0], y0 * im.size[1], x1 * im.size[0], y1 * im.size[1]],
                                     outline=colours.get(f, "red"), width=3)
                im.save(debug / f"p{n:02d}-{d['key']}.jpg", quality=70)
        for name, (photo, boxes, _) in replies.items():
            im = photo.copy()
            dr = ImageDraw.Draw(im)
            for f, (x0, y0, x1, y1) in boxes.items():
                dr.rectangle([x0 * im.size[0], y0 * im.size[1], x1 * im.size[0], y1 * im.size[1]], outline=colours.get(f, "red"), width=3)
            im.save(debug / f"reply-{name}.jpg", quality=75)
        imgs[0].save(debug / "p01-cover.jpg", quality=75)

    print(f"{stack.name}: {stack.stat().st_size // 1024} KB, {len(pages)} pagina's, {len(manifest_docs)} documenten")


if __name__ == "__main__":
    main()
