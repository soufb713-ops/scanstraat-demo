"""Printable cover sheets (scheidingsbladen) with a QR code per client.

The office prints one sheet per client once and reuses it: put it on top of that client's
papers, scan the whole pile in one go, and the system knows for certain where each client
starts. Reading the QR code is plain code, not AI, so client assignment on a cover sheet
is never a guess.
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


def draw(c, client, brand="Scanstraat"):
    w, h = A4
    c.setFillGray(0.12)
    c.rect(0, h - 30 * mm, w, 30 * mm, stroke=0, fill=1)
    c.setFillGray(1)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(20 * mm, h - 19 * mm, "SCHEIDINGSBLAD")
    c.setFont("Helvetica", 11)
    c.drawRightString(w - 20 * mm, h - 19 * mm, brand)
    c.setFillGray(0)
    c.setFont("Helvetica", 12)
    c.drawString(20 * mm, h - 50 * mm, "Klant")
    c.setFont("Helvetica-Bold", 30)
    c.drawString(20 * mm, h - 64 * mm, client["name"][:34])
    c.setFont("Helvetica", 14)
    c.drawString(20 * mm, h - 76 * mm, f"Relatienummer {client['nr']}")
    size = 95 * mm
    c.drawImage(_qr_image(payload(client["nr"])), (w - size) / 2, h - 185 * mm, size, size)
    c.setFont("Helvetica", 11)
    lines = [
        "Leg dit blad bovenop de papieren van deze klant en scan de hele stapel in één keer.",
        "Alles na dit blad hoort bij deze klant, tot het volgende scheidingsblad.",
        "Dit blad wordt niet opgeslagen. Hergebruik het gerust.",
    ]
    for i, line in enumerate(lines):
        c.drawCentredString(w / 2, h - 200 * mm - i * 7 * mm, line)
    c.setFont("Helvetica", 8)
    c.setFillGray(0.4)
    c.drawCentredString(w / 2, 14 * mm, f"Code {payload(client['nr'])}")


def pdf(clients, brand="Scanstraat"):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Scheidingsbladen")
    for client in clients:
        draw(c, client, brand)
        c.showPage()
    c.save()
    return buf.getvalue()
