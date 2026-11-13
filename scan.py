"""Deterministic scan handling: pages, blank pages, cover sheets, output PDFs.

No AI in this file. Everything here is repeatable and cheap:
- a scanned stack (PDF, or photos) becomes page images;
- blank pages (the empty back side of duplex scans) are set aside;
- cover sheets are found by their QR code and split the stack per client;
- each final document is written as its own searchable-name PDF.
Every image and PDF is written to and read from the encrypted vault (vault.py), never as a plain file.
"""
import io
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

import coversheet
import vault

DPI = 150
PDF_LOCK = threading.Lock()  # pdfium is not thread-safe
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic"}


MAX_SIDE = 2600  # a phone photo of 12 megapixels is scaled down to roughly a 300 dpi A4 before anything else


def load_pages(name, data: bytes):
    """Yield RGB page images for a PDF, a (multi-page) TIFF or a photo, from bytes in memory.
    One page at a time: a scan of a hundred pages never sits in memory as a hundred images."""
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        import pypdfium2 as pdfium
        with PDF_LOCK:
            doc = pdfium.PdfDocument(data)
            n = len(doc)
        try:
            for i in range(n):
                with PDF_LOCK:
                    img = doc[i].render(scale=DPI / 72).to_pil().convert("RGB")
                yield img
        finally:
            with PDF_LOCK:
                doc.close()
    elif ext in IMAGE_EXT:
        img = Image.open(io.BytesIO(data))
        for i in range(getattr(img, "n_frames", 1)):
            img.seek(i)
            page = ImageOps.exif_transpose(img.copy()).convert("RGB")
            if max(page.size) > MAX_SIDE:
                page.thumbnail((MAX_SIDE, MAX_SIDE))
            yield page
    else:
        raise ValueError(f"Bestandstype {ext} wordt niet ondersteund")


def ink_ratio(img):
    """Share of dark pixels, ignoring a 4% margin where scanner edges and punch holes sit."""
    g = img.convert("L")
    g.thumbnail((400, 400))
    a = np.asarray(g)
    h, w = a.shape
    my, mx = max(1, h // 25), max(1, w // 25)
    core = a[my:h - my, mx:w - mx]
    return float((core < 170).mean())


def is_blank(img):
    return ink_ratio(img) < 0.0025


def read_cover_sheet(img):
    """Client number if this page is a cover sheet, else None."""
    import cv2
    det = cv2.QRCodeDetector()
    g = np.asarray(img.convert("L"))
    # A cover sheet's code is large: half size finds it fastest. The other sizes are for photos and odd scans, and
    # only tried when the page has the three finder squares of a QR code at all.
    half = cv2.resize(g, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    for a in (half, g):
        try:
            text, _, _ = det.detectAndDecode(a)
        except cv2.error:
            text = ""
        nr = coversheet.parse(text)
        if nr:
            return nr
    try:
        found, _ = det.detect(half)
    except cv2.error:
        found = False
    if found:
        for scale in (0.6, 1.5):
            try:
                text, _, _ = det.detectAndDecode(cv2.resize(g, None, fx=scale, fy=scale))
            except cv2.error:
                text = ""
            nr = coversheet.parse(text)
            if nr:
                return nr
    return None


def _jpeg(img, quality):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def save_page(img, folder: Path, n: int):
    vault.write(folder / f"p{n:03d}.jpg", _jpeg(img, 82))
    th = img.copy()
    th.thumbnail((220, 300))
    vault.write(folder / f"t{n:03d}.jpg", _jpeg(th, 75))


def page_image(folder: Path, n: int):
    return Image.open(io.BytesIO(vault.read(folder / f"p{n:03d}.jpg"))).convert("RGB")


def jpeg_bytes(img, long_side):
    im = img.copy()
    im.thumbnail((long_side, long_side))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def build_pdf(folder: Path, pages, out: Path):
    imgs = [page_image(folder, n) for n in pages]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", resolution=DPI, save_all=True, append_images=imgs[1:])
    vault.write(out, buf.getvalue())
    return out
