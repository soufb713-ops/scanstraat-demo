"""Printable cover sheets (scheidingsbladen) with a QR code per client dossier.

The office prints one sheet per client once and reuses it: put it on top of that client's
papers, scan the whole pile in one go, and the system knows for certain where each dossier
starts. Reading the QR code is plain code, not AI, so client assignment on a cover sheet
is never a guess.

The sheet is white-label: it carries the firm's own name (the reader of the sheet is the
firm's employee at the scanner, and sometimes the client who delivers the shoebox). The
black bar on the long edge makes the sheet easy to find when leafing through a pile.
"""
import io

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

PREFIX = "SCAN-CLIENT:"


def payload(client_nr):
    return f"{PREFIX}{client_nr}"


def parse(text):
    text = (text or "").strip()
    return text[len(PREFIX):] if text.startswith(PREFIX) else None


def _qr_image(data):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def _fit(c, text, font, size, max_width):
    """Largest size (down to 12 pt) at which text fits max_width; returns the size used."""
    while size > 12 and c.stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _corner(c, x, y, dx, dy, arm=9 * mm):
    c.setLineWidth(1.6)
    c.line(x, y, x + dx * arm, y)
    c.line(x, y, x, y + dy * arm)


def draw(c, client, brand="Scanstraat", firm=None):
    w, h = A4
    firm = (firm or brand or "").strip()
    name = client["name"]

    # edge bar: visible from the side of a pile, and a header band with the word every scanner operator looks for
    c.setFillGray(0)
    c.rect(w - 9 * mm, 0, 9 * mm, h, stroke=0, fill=1)
    c.rect(0, h - 26 * mm, w - 9 * mm, 26 * mm, stroke=0, fill=1)
    c.setFillGray(1)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(18 * mm, h - 17 * mm, "SCHEIDINGSBLAD")
    c.setFont("Helvetica", 11)
    c.drawRightString(w - 17 * mm, h - 16.5 * mm, firm[:48])
    c.saveState()
    c.translate(w - 2.8 * mm, h / 2)
    c.rotate(90)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(0, 0, f"SCHEIDINGSBLAD  ·  DOSSIER {client['nr']}  ·  {name[:40].upper()}")
    c.restoreState()

    # dossier block
    c.setFillGray(0.35)
    c.setFont("Helvetica", 11)
    c.drawString(18 * mm, h - 42 * mm, "DOSSIER")
    c.setFillGray(0)
    c.setFont("Helvetica-Bold", 54)
    c.drawString(17 * mm, h - 62 * mm, str(client["nr"]))
    size = _fit(c, name, "Helvetica-Bold", 26, w - 45 * mm)
    c.setFont("Helvetica-Bold", size)
    c.drawString(18 * mm, h - 76 * mm, name)
    sub = " · ".join(x for x in [client.get("city"), f"KvK {client['kvk']}" if client.get("kvk") else ""] if x)
    if sub:
        c.setFont("Helvetica", 11)
        c.setFillGray(0.35)
        c.drawString(18 * mm, h - 84 * mm, sub)
        c.setFillGray(0)
    c.setLineWidth(0.8)
    c.line(18 * mm, h - 90 * mm, w - 27 * mm, h - 90 * mm)

    # QR code with registration corners
    size_qr = 95 * mm
    qx, qy = (w - 9 * mm - size_qr) / 2, h - 197 * mm
    c.drawImage(_qr_image(payload(client["nr"])), qx, qy, size_qr, size_qr)
    pad = 5 * mm
    _corner(c, qx - pad, qy - pad, 1, 1)
    _corner(c, qx + size_qr + pad, qy - pad, -1, 1)
    _corner(c, qx - pad, qy + size_qr + pad, 1, -1)
    _corner(c, qx + size_qr + pad, qy + size_qr + pad, -1, -1)

    # how to use it, for the person at the scanner
    steps = [
        ("1", "Leg dit blad bovenop de papieren van dit dossier."),
        ("2", "Scan de hele stapel in één keer, dubbelzijdig mag."),
        ("3", "Alles na dit blad hoort bij dit dossier, tot het volgende scheidingsblad."),
    ]
    y = h - 218 * mm
    for n, text in steps:
        c.setFillGray(0)
        c.circle(22 * mm, y + 1.4 * mm, 3.4 * mm, stroke=0, fill=1)
        c.setFillGray(1)
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(22 * mm, y, n)
        c.setFillGray(0)
        c.setFont("Helvetica", 12)
        c.drawString(30 * mm, y, text)
        y -= 10 * mm
    c.setFont("Helvetica", 9)
    c.setFillGray(0.35)
    c.drawString(18 * mm, 21 * mm, "Dit blad wordt herkend aan de QR-code en zelf niet bewaard. Hergebruik het elke maand.")
    c.drawString(18 * mm, 15 * mm, f"Code {payload(client['nr'])}")
    c.drawRightString(w - 17 * mm, 15 * mm, f"Gemaakt met {brand}" if firm and firm != brand else brand)
    c.setFillGray(0)


def pdf(clients, brand="Scanstraat", firm=None):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Scheidingsbladen")
    c.setAuthor(firm or brand)
    for client in clients:
        draw(c, client, brand, firm)
        c.showPage()
    c.save()
    return buf.getvalue()
