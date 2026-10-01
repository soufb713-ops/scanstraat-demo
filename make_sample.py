"""Builds the sample scan: one 16-page PDF as an office scanner would produce it.

Two fictional clients, each behind a cover sheet, with blank back sides, a two-page invoice,
a receipt on the scanner glass, a tax letter, a bank statement, a sales invoice, a German
invoice and one invoice scanned twice. Also writes samples/scan-stack.json: which pages
form which document and the hand-made answers used in replay mode (no API key).

Run: python make_sample.py   (needs ../demo-data for the Studio Lindewerf documents)
"""
import hashlib
import io
import json
import random
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

import coversheet
import scan

HERE = Path(__file__).parent
DATA = HERE.parent / "demo-data"
OUT = HERE / "samples"
DPI = scan.DPI
A4PX = (int(210 / 25.4 * DPI), int(297 / 25.4 * DPI))
random.seed(7)

CLIENTS = [
    {"nr": "1001", "name": "Studio Lindewerf B.V.", "kvk": "90412276", "vat": "NL865512093B01",
     "city": "Rotterdam", "exact_division": "", "chart": "lindewerf"},
    {"nr": "1002", "name": "Fietsenmakerij De Spaak B.V.", "kvk": "93120458", "vat": "NL866230417B01",
     "city": "Delft", "exact_division": "", "chart": "standaard"},
    {"nr": "1003", "name": "Bakkerij Van Oord B.V.", "kvk": "91877302", "vat": "NL865990214B01",
     "city": "Schiedam", "exact_division": "", "chart": "standaard"},
]


# ---------- generated paper documents ----------
def _pdf_pages(draw_fns):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for fn in draw_fns:
        fn(c)
        c.showPage()
    c.save()
    return buf.getvalue()


def _footer(c):
    c.setFont("Helvetica-Oblique", 7)
    c.setFillGray(0.45)
    c.drawCentredString(A4[0] / 2, 10 * mm, "Fictief voorbeelddocument voor een demo. Bestaat niet echt.")
    c.setFillGray(0)


def _letterhead(c, name, lines, color=(0.1, 0.25, 0.45)):
    w, h = A4
    c.setFillColorRGB(*color)
    c.rect(0, h - 8 * mm, w, 8 * mm, stroke=0, fill=1)
    c.setFillGray(0)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(20 * mm, h - 25 * mm, name)
    c.setFont("Helvetica", 8.5)
    for i, l in enumerate(lines):
        c.drawString(20 * mm, h - 31 * mm - i * 4 * mm, l)


def _address(c, lines, y=None):
    y = y or A4[1] - 58 * mm
    c.setFont("Helvetica", 10)
    for i, l in enumerate(lines):
        c.drawString(20 * mm, y - i * 5 * mm, l)


def _table(c, y, rows, cols=(20, 110, 135, 160)):
    c.setFont("Helvetica-Bold", 9)
    heads = ["Omschrijving", "Aantal", "Prijs", "Bedrag"]
    for x, t in zip(cols, heads):
        c.drawString(x * mm, y, t)
    c.line(20 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)
    c.setFont("Helvetica", 9)
    for i, r in enumerate(rows):
        yy = y - 8 * mm - i * 6 * mm
        for x, t in zip(cols, r):
            c.drawString(x * mm, yy, str(t))
    return y - 8 * mm - len(rows) * 6 * mm


def _totals(c, y, items):
    for i, (label, val, bold) in enumerate(items):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        c.drawString(125 * mm, y - i * 6 * mm, label)
        c.drawRightString(190 * mm, y - i * 6 * mm, val)


def pandhuis_terms(c):
    _letterhead(c, "Pandhuis Rotterdam Beheer B.V.", ["Bijlage bij factuur 2026-10-HS118", "Pagina 2 van 2"], (0.42, 0.27, 0.2))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, A4[1] - 60 * mm, "Specificatie en voorwaarden")
    c.setFont("Helvetica", 9.5)
    text = [
        "Object: bedrijfsruimte Hoogstraat 118, 3011 PV Rotterdam (begane grond, 142 m2).",
        "Huurperiode van deze factuur: 1 oktober 2026 tot en met 31 oktober 2026.",
        "De huur is belast met btw op grond van de optie belaste verhuur (art. 11 lid 1 sub b Wet OB).",
        "Betaling uiterlijk op de eerste dag van de huurperiode op NL21 TEST 7001 8822 33.",
        "Bij te late betaling is de huurder de wettelijke handelsrente verschuldigd.",
        "Servicekosten worden jaarlijks afgerekend; voorschot is in deze factuur niet opgenomen.",
        "Vragen over deze factuur: administratie@pandhuis-voorbeeld.nl",
    ]
    for i, t in enumerate(text):
        c.drawString(20 * mm, A4[1] - 72 * mm - i * 7 * mm, t)
    _footer(c)


def tax_letter(c):
    w, h = A4
    c.setFont("Helvetica-Bold", 13)
    c.drawString(20 * mm, h - 25 * mm, "Belastingdienst")
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, h - 30 * mm, "VOORBEELD - fictieve brief voor een demo")
    _address(c, ["Studio Lindewerf B.V.", "Hoogstraat 118", "3011 PV Rotterdam"], h - 55 * mm)
    c.setFont("Helvetica", 9)
    for i, (k, v) in enumerate([("Datum", "22 september 2026"), ("Fiscaal nummer", "8655.12.093"),
                                ("Aanslagnummer", "8655.12.093.V.76.0001"), ("Dagtekening", "22-09-2026")]):
        c.drawString(120 * mm, h - 55 * mm - i * 5 * mm, k)
        c.drawString(152 * mm, h - 55 * mm - i * 5 * mm, v)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, h - 90 * mm, "Voorlopige aanslag vennootschapsbelasting 2026")
    c.setFont("Helvetica", 10)
    body = [
        "Geachte heer, mevrouw,",
        "",
        "Voor het jaar 2026 ontvangt u een voorlopige aanslag vennootschapsbelasting.",
        "Wij hebben de aanslag berekend op basis van een geschat belastbaar bedrag van EUR 18.000.",
        "",
        "Te betalen bedrag:  EUR 3.420,00",
        "U kunt in 3 termijnen betalen. De eerste termijn van EUR 1.140,00 moet uiterlijk",
        "31 oktober 2026 op onze rekening staan.",
        "",
        "Bent u het niet eens met deze aanslag? Dan kunt u binnen 6 weken na de dagtekening",
        "bezwaar maken of een verzoek om vermindering indienen.",
    ]
    for i, t in enumerate(body):
        c.drawString(20 * mm, h - 102 * mm - i * 6 * mm, t)
    _footer(c)


def linde_invoice(c, copy_note=None):
    _letterhead(c, "Fietsgroothandel Van der Linde B.V.",
                ["Industrieweg 40, 2651 BC Berkel en Rodenrijs", "KvK 60114587  Btw NL853997120B01",
                 "IBAN NL35TEST0600114587"], (0.55, 0.12, 0.1))
    _address(c, ["Fietsenmakerij De Spaak B.V.", "t.a.v. inkoop", "Oude Delft 212", "2611 HJ Delft"])
    h = A4[1]
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, h - 92 * mm, "FACTUUR")
    c.setFont("Helvetica", 9)
    for i, (k, v) in enumerate([("Factuurnummer", "VDL-2026-10442"), ("Factuurdatum", "15-09-2026"),
                                ("Vervaldatum", "15-10-2026"), ("Klantnummer", "D-3318")]):
        c.drawString(120 * mm, h - 92 * mm - i * 5 * mm, k)
        c.drawString(155 * mm, h - 92 * mm - i * 5 * mm, v)
    y = _table(c, h - 125 * mm, [
        ("Binnenband 28 inch (doos 25)", "4", "62,50", "250,00"),
        ("Remblokken V-brake set", "30", "4,20", "126,00"),
        ("Ketting 8-speed", "12", "11,75", "141,00"),
        ("Verzendkosten", "1", "12,50", "12,50"),
    ])
    _totals(c, y - 10 * mm, [("Subtotaal excl. btw", "529,50", False), ("Btw 21%", "111,20", False),
                              ("Totaal", "640,70", True)])
    _footer(c)


def spaak_sales(c):
    _letterhead(c, "Fietsenmakerij De Spaak B.V.",
                ["Oude Delft 212, 2611 HJ Delft", "KvK 93120458  Btw NL866230417B01", "IBAN NL49TEST0093120458"],
                (0.1, 0.4, 0.3))
    _address(c, ["Hotel De Vliet B.V.", "Vlietweg 3", "2611 AA Delft"])
    h = A4[1]
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, h - 92 * mm, "FACTUUR")
    c.setFont("Helvetica", 9)
    for i, (k, v) in enumerate([("Factuurnummer", "2026-0187"), ("Factuurdatum", "18-09-2026"),
                                ("Betalen binnen", "14 dagen")]):
        c.drawString(120 * mm, h - 92 * mm - i * 5 * mm, k)
        c.drawString(155 * mm, h - 92 * mm - i * 5 * mm, v)
    y = _table(c, h - 120 * mm, [
        ("Onderhoudsbeurt huurfietsen", "8", "45,00", "360,00"),
        ("Vervangen banden", "6", "38,00", "228,00"),
    ])
    _totals(c, y - 10 * mm, [("Subtotaal excl. btw", "588,00", False), ("Btw 21%", "123,48", False),
                              ("Totaal", "711,48", True)])
    _footer(c)


def bank_statement(c):
    w, h = A4
    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm, h - 25 * mm, "Demobank  Rekeningoverzicht")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, h - 32 * mm, "Fietsenmakerij De Spaak B.V.   NL49 TEST 0093 1204 58   Afschrift 9 / 2026")
    c.drawString(20 * mm, h - 37 * mm, "Periode 01-09-2026 t/m 30-09-2026")
    rows = [("01-09", "Beginsaldo", "", "8.412,33"), ("03-09", "Huur Oude Delft 212", "-1.450,00", ""),
            ("05-09", "Pin Tankstation Kruithuisweg", "-61,20", ""), ("09-09", "Hotel De Vliet B.V. fact 2026-0171", "+522,72", ""),
            ("15-09", "Belnet zakelijk", "-36,30", ""), ("22-09", "Pin Bouwmarkt Delft", "-48,95", ""),
            ("26-09", "Radteile Muller GmbH", "-386,58", ""), ("30-09", "Eindsaldo", "", "6.952,02")]
    c.setFont("Helvetica-Bold", 9)
    for x, t in zip((20, 40, 140, 170), ("Datum", "Omschrijving", "Bedrag", "Saldo")):
        c.drawString(x * mm, h - 52 * mm, t)
    c.setFont("Helvetica", 9)
    for i, r in enumerate(rows):
        for x, t in zip((20, 40, 140, 170), r):
            c.drawString(x * mm, h - 60 * mm - i * 6 * mm, t)
    _footer(c)


def german_invoice(c):
    _letterhead(c, "Radteile Müller GmbH", ["Hafenstraße 9, 26789 Leer, Deutschland", "USt-IdNr. DE298877341",
                                            "IBAN DE90285500000012345678"], (0.2, 0.2, 0.2))
    _address(c, ["Fietsenmakerij De Spaak B.V.", "Oude Delft 212", "2611 HJ Delft", "Niederlande"])
    h = A4[1]
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, h - 92 * mm, "RECHNUNG")
    c.setFont("Helvetica", 9)
    for i, (k, v) in enumerate([("Rechnungsnr.", "RM-26-5531"), ("Datum", "20.09.2026"), ("Fällig", "04.10.2026")]):
        c.drawString(120 * mm, h - 92 * mm - i * 5 * mm, k)
        c.drawString(155 * mm, h - 92 * mm - i * 5 * mm, v)
    y = _table(c, h - 120 * mm, [("Montageständer Profi", "1", "189,00", "189,00"),
                                 ("Speichenschlüssel-Set", "3", "46,00", "138,00")])
    _totals(c, y - 10 * mm, [("Netto", "327,00", False), ("MwSt 19%", "62,13", False), ("Gesamt", "389,13", True)])
    _footer(c)


# ---------- turning paper into a 'scan' ----------
def render_pdf(data_or_path, page=0):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(data_or_path)
    img = doc[page].render(scale=DPI / 72).to_pil().convert("RGB")
    doc.close()
    return img.resize(A4PX)


def on_glass(photo_path):
    """A small receipt laid on the scanner glass."""
    page = Image.new("RGB", A4PX, (250, 250, 248))
    im = ImageOps.exif_transpose(Image.open(photo_path)).convert("RGB")
    target_w = int(A4PX[0] * 0.42)
    im = im.resize((target_w, int(im.height * target_w / im.width)))
    if im.height > A4PX[1] - 80:
        im.thumbnail((A4PX[0], A4PX[1] - 80))
    page.paste(im, (60, 60))
    return page


def blank_page():
    page = Image.new("RGB", A4PX, (249, 249, 247))
    px = page.load()
    for _ in range(40):
        x, y = random.randrange(A4PX[0]), random.randrange(A4PX[1])
        px[x, y] = (150, 150, 150)
    return page


def scanned(img, gray=False):
    """Slight skew, paper tone and blur, like a real feeder scan."""
    angle = random.uniform(-0.7, 0.7)
    img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(250, 250, 248))
    img = img.filter(ImageFilter.GaussianBlur(0.45))
    if gray:
        img = img.convert("L").convert("RGB")
    return img


def main():
    OUT.mkdir(exist_ok=True)
    inv = DATA / "invoices"
    clients = {c["nr"]: c for c in CLIENTS}
    covers = [render_pdf(coversheet.pdf([clients[n]]), 0) for n in ("1001", "1002")]
    gen = lambda fn: render_pdf(_pdf_pages([fn]))

    pages = [
        ("cover", covers[0]),
        ("L-BELNET", scanned(render_pdf(str(inv / "D01_belnet_factuur_sept2026.pdf")))),
        ("blank", blank_page()),
        ("L-PANDHUIS", scanned(render_pdf(str(inv / "D02_pandhuis_huur_oktober.pdf")))),
        ("L-PANDHUIS", scanned(gen(pandhuis_terms))),
        ("L-SUPERVERS", scanned(on_glass(inv / "D03_IMG_20260917_0911.jpg"))),
        ("L-DRUKKERIJ", scanned(render_pdf(str(inv / "D07_drukkerij_vanwijk_F26-1187.pdf")), gray=True)),
        ("L-BLINKEND", scanned(render_pdf(str(inv / "D14_blinkend_factuur_2026-0913.pdf")))),
        ("L-VPB", scanned(gen(tax_letter), gray=True)),
        ("blank", blank_page()),
        ("cover", covers[1]),
        ("S-LINDE", scanned(gen(linde_invoice))),
        ("S-LINDE-2", scanned(gen(linde_invoice), gray=True)),
        ("S-VERKOOP", scanned(gen(spaak_sales))),
        ("S-BANK", scanned(gen(bank_statement), gray=True)),
        ("S-MULLER", scanned(gen(german_invoice))),
    ]
    stack = OUT / "scan_2026-09-30_0915.pdf"
    imgs = [p[1] for p in pages]
    imgs[0].save(stack, "PDF", resolution=DPI, save_all=True, append_images=imgs[1:], quality=70)

    docs = {}
    for i, (key, _) in enumerate(pages, 1):
        if key in ("cover", "blank"):
            continue
        docs.setdefault(key, []).append(i)
    manifest = {
        "file": stack.name,
        "sha256": hashlib.sha256(stack.read_bytes()).hexdigest(),
        "documents": [{"key": k, "pages": v, "answer": ANSWERS[k]} for k, v in docs.items()],
    }
    (OUT / "scan-stack.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    import csv
    with (DATA / "reference" / "chart-of-accounts.csv").open(encoding="utf-8") as fh:
        linde = [[r["account"], r["name"]] for r in csv.DictReader(fh)]
    charts = {"lindewerf": linde, "standaard": STANDARD_CHART}
    (OUT / "charts.json").write_text(json.dumps(charts, indent=1, ensure_ascii=False), encoding="utf-8")
    (OUT / "clients.json").write_text(json.dumps(CLIENTS, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{stack} ({stack.stat().st_size // 1024} KB, {len(pages)} pagina's, {len(docs)} documenten)")


def _inv(kind, client, cp, ref, date, net, vat, gross, lines, desc, **kw):
    base = {"kind": kind, "client_hint": client, "counterparty": cp, "reference": ref, "doc_date": date,
            "net_total": net, "vat_lines": vat, "gross_total": gross, "lines": lines, "description": desc,
            "currency": "EUR", "due_date": None, "supplier_vat_number": None, "supplier_kvk": None, "iban": None,
            "booking_reason": "", "uncertain_fields": [], "issues": []}
    base.update(kw)
    return base


STANDARD_CHART = [["0210", "Inventaris en gereedschap"], ["1100", "Bank"], ["1600", "Crediteuren"], ["1800", "Rekening-courant DGA"],
                  ["4100", "Huur bedrijfsruimte"], ["4310", "Telefoon en internet"], ["4320", "Kantoorbenodigdheden"],
                  ["4410", "Brandstof auto"], ["4530", "Kantinekosten"], ["4600", "Reclamekosten"], ["4800", "Bankkosten"],
                  ["4900", "Klein gereedschap en overige kosten"], ["7000", "Inkoopwaarde onderdelen"], ["8000", "Omzet 21%"],
                  ["8100", "Omzet 9%"]]

L, S = "Studio Lindewerf B.V.", "Fietsenmakerij De Spaak B.V."
ANSWERS = {
    "L-BELNET": _inv("inkoopfactuur", L, "Belnet Mobiel B.V.", "BM26-0948817", "2026-09-01", 40.00,
                     [{"rate": 21, "base": 40.00, "amount": 8.40}], 48.40,
                     [{"account": "4310", "vat_code": "V21", "amount_excl": 40.00, "vat_amount": 8.40, "description": "Mobiel abonnement september"}],
                     "Mobiel abonnement september", supplier_vat_number="NL858104221B01", supplier_kvk="71829304",
                     iban="NL70TEST4410020011", booking_reason="Vaste leverancier: zelfde rekening en btw-code als juli en augustus."),
    "L-PANDHUIS": _inv("inkoopfactuur", L, "Pandhuis Rotterdam Beheer B.V.", "2026-10-HS118", "2026-09-22", 1250.00,
                       [{"rate": 21, "base": 1250.00, "amount": 262.50}], 1512.50,
                       [{"account": "4100", "vat_code": "V21", "amount_excl": 1250.00, "vat_amount": 262.50, "description": "Huur oktober 2026"}],
                       "Huur bedrijfsruimte oktober 2026", supplier_vat_number="NL811720934B01", supplier_kvk="24387716",
                       iban="NL21TEST7001882233", due_date="2026-10-01",
                       booking_reason="Vaste huur.", split_reason="Pagina 2 is de specificatie bij dezelfde factuur (zelfde afzender en factuurnummer).",
                       issues=[{"level": "info", "message": "Factuur in september voor huur oktober: kosten eventueel naar 1400 Vooruitbetaalde kosten."}]),
    "L-SUPERVERS": _inv("kassabon", L, "SuperVers Kralingen", "BON 4471", "2026-09-17", 29.73,
                        [{"rate": 9, "base": 21.33, "amount": 1.92}, {"rate": 21, "base": 8.40, "amount": 1.77}], 33.42,
                        [{"account": "4530", "vat_code": "V9", "amount_excl": 21.33, "vat_amount": 1.92, "description": "Koffie, melk, fruit"},
                         {"account": "4530", "vat_code": "V21", "amount_excl": 8.40, "vat_amount": 1.77, "description": "Schoonmaakmiddelen"}],
                        "Boodschappen pantry", booking_reason="Kantinekosten, één regel per btw-tarief (regel R02)."),
    "L-DRUKKERIJ": _inv("inkoopfactuur", L, "Drukkerij Van Wijk B.V.", "F26-1187", "2026-09-10", 310.00,
                        [{"rate": 21, "base": 310.00, "amount": 65.10}], 375.10,
                        [{"account": "4600", "vat_code": "V21", "amount_excl": 310.00, "vat_amount": 65.10, "description": "Flyers najaarscampagne"}],
                        "Flyers najaarscampagne", supplier_vat_number="NL801992311B01", supplier_kvk="24101199",
                        iban="NL48TEST2410119900", due_date="2026-09-24", booking_reason="Drukwerk voor reclame: 4600."),
    "L-BLINKEND": _inv("inkoopfactuur", L, "Schoonmaakbedrijf Blinkend", "2026-0913", "2026-09-26", 180.00,
                       [{"rate": 21, "base": 180.00, "amount": 38.80}], 218.80,
                       [{"account": "4110", "vat_code": "V21", "amount_excl": 180.00, "vat_amount": 37.80, "description": "Schoonmaak september"}],
                       "Schoonmaak kantoor september", supplier_vat_number="NL860998121B01", supplier_kvk="77120445",
                       iban="NL13TEST7712044500", due_date="2026-10-10",
                       booking_reason="Vaste leverancier. Btw op de factuur is fout; boekvoorstel gebruikt de juiste 37,80."),
    "L-VPB": {"kind": "belastingdienst", "client_hint": L, "counterparty": "Belastingdienst",
              "reference": "8655.12.093.V.76.0001", "doc_date": "2026-09-22", "due_date": "2026-10-31",
              "description": "Voorlopige aanslag vennootschapsbelasting 2026, EUR 3.420 in 3 termijnen",
              "gross_total": 3420.00, "net_total": None, "vat_lines": [], "lines": [], "currency": "EUR",
              "booking_reason": "Geen inkoopboeking: aanslag gaat naar het dossier en de termijnen naar de agenda.",
              "uncertain_fields": [], "issues": [{"level": "warning", "message": "Eerste termijn EUR 1.140 uiterlijk 31 oktober 2026. Bezwaartermijn 6 weken na 22 september."}]},
    "S-LINDE": _inv("inkoopfactuur", S, "Fietsgroothandel Van der Linde B.V.", "VDL-2026-10442", "2026-09-15", 529.50,
                    [{"rate": 21, "base": 529.50, "amount": 111.20}], 640.70,
                    [{"account": "7000", "vat_code": "V21", "amount_excl": 529.50, "vat_amount": 111.20, "description": "Onderdelen voorraad"}],
                    "Binnenbanden, remblokken, kettingen", supplier_vat_number="NL853997120B01", supplier_kvk="60114587",
                    iban="NL35TEST0600114587", due_date="2026-10-15", booking_reason="Onderdelen voor verkoop: inkoopwaarde 7000."),
    "S-LINDE-2": _inv("inkoopfactuur", S, "Fietsgroothandel Van der Linde B.V.", "VDL-2026-10442", "2026-09-15", 529.50,
                      [{"rate": 21, "base": 529.50, "amount": 111.20}], 640.70,
                      [{"account": "7000", "vat_code": "V21", "amount_excl": 529.50, "vat_amount": 111.20, "description": "Onderdelen voorraad"}],
                      "Binnenbanden, remblokken, kettingen", supplier_vat_number="NL853997120B01", supplier_kvk="60114587",
                      iban="NL35TEST0600114587", due_date="2026-10-15", booking_reason="Onderdelen voor verkoop: inkoopwaarde 7000."),
    "S-VERKOOP": _inv("verkoopfactuur", S, "Hotel De Vliet B.V.", "2026-0187", "2026-09-18", 588.00,
                      [{"rate": 21, "base": 588.00, "amount": 123.48}], 711.48,
                      [{"account": "8000", "vat_code": "O21", "amount_excl": 588.00, "vat_amount": 123.48, "description": "Onderhoud huurfietsen"}],
                      "Onderhoud en banden huurfietsen", booking_reason="Eigen verkoopfactuur van de klant: omzet 21%."),
    "S-BANK": {"kind": "bankafschrift", "client_hint": S, "counterparty": "Demobank", "reference": "Afschrift 9/2026",
               "doc_date": "2026-09-30", "due_date": None, "description": "Rekeningoverzicht september 2026, eindsaldo EUR 6.952,02",
               "gross_total": None, "net_total": None, "vat_lines": [], "lines": [], "currency": "EUR",
               "booking_reason": "Geen boeking: bankmutaties komen via de bankkoppeling. Alleen archiveren.",
               "uncertain_fields": [], "issues": [{"level": "info", "message": "2 pinbetalingen zonder bon op dit afschrift (Tankstation 61,20 en Bouwmarkt 48,95)."}]},
    "S-MULLER": _inv("inkoopfactuur", S, "Radteile Müller GmbH", "RM-26-5531", "2026-09-20", 327.00,
                     [{"rate": 19, "base": 327.00, "amount": 62.13}], 389.13,
                     [{"account": "4900", "vat_code": "GEEN_AFTREK", "amount_excl": 389.13, "vat_amount": 0, "description": "Montagestandaard en spakensleutels"}],
                     "Montageständer, Speichenschlüssel", supplier_vat_number="DE298877341", iban="DE90285500000012345678",
                     due_date="2026-10-04", booking_reason="Duitse btw is niet aftrekbaar in NL: bruto op kosten. Leverancier vragen om factuur met btw verlegd."),
}

if __name__ == "__main__":
    main()
