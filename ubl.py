"""UBL 2.1 purchase invoice / credit note per approved document, with the original PDF embedded.

The NLCIUS structure (CustomizationID urn:cen.eu:en16931:2017#compliant#urn:fdc:nen.nl:nlcius:v1.0) is the Dutch
e-invoicing standard most bookkeeping packages can import (SnelStart via the per-administration mailbox, and the
UBL import of other packages). Built from the fields a person approved on the review screen, never from raw AI
output. Not yet run through an official validator: validate a sample per package before a firm relies on it.
"""
import base64
from decimal import Decimal
from xml.sax.saxutils import escape


def _d(v):
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def _e(v):
    return escape(str(v or ""))


def _category(rate, foreign_reverse):
    if foreign_reverse:
        return "AE"
    return "Z" if _d(rate) == 0 else "S"


def build(fields: dict, customer: dict, pdf_bytes: bytes, filename: str) -> str:
    """fields: the approved document fields as produced by app.as_check_fields() (supplier_name, invoice_number,
    invoice_date, due_date, supplier_vat_number, supplier_kvk, iban, net_total, vat_lines, gross_total, currency,
    description, doc_type). customer: the office's client (name, address, vat)."""
    credit = fields.get("doc_type") == "credit_note"
    sign = Decimal(-1) if credit else Decimal(1)  # a UBL credit note carries positive amounts
    root, type_tag, line_tag, qty_tag = (("CreditNote", "CreditNoteTypeCode", "CreditNoteLine", "CreditedQuantity") if credit
                                         else ("Invoice", "InvoiceTypeCode", "InvoiceLine", "InvoicedQuantity"))
    vat_no = (fields.get("supplier_vat_number") or "").replace(" ", "").upper()
    foreign_reverse = bool(vat_no) and not vat_no.startswith("NL") and not any(
        _d(v.get("amount")) for v in fields.get("vat_lines") or [])
    lines = fields.get("vat_lines") or []
    if not lines:
        lines = [{"rate": 0, "base": fields.get("net_total") or fields.get("gross_total"), "amount": 0}]
    number = fields.get("invoice_number") or f"BON-{fields.get('invoice_date') or 'onbekend'}"

    subtotals, invoice_lines, net_sum, vat_sum = [], [], Decimal(0), Decimal(0)
    for i, v in enumerate(lines, 1):
        base = (_d(v.get("base")) if v.get("base") is not None else _d(fields.get("net_total"))) * sign
        amount = _d(v.get("amount")) * sign
        cat = _category(v.get("rate"), foreign_reverse)
        net_sum += base
        vat_sum += amount
        reason = "<cbc:TaxExemptionReason>Btw verlegd</cbc:TaxExemptionReason>" if cat == "AE" else ""
        subtotals.append(f"""
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">{base}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">{amount}</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>{cat}</cbc:ID><cbc:Percent>{_d(v.get('rate'))}</cbc:Percent>{reason}<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal>""")
        invoice_lines.append(f"""
  <cac:{line_tag}>
    <cbc:ID>{i}</cbc:ID>
    <cbc:{qty_tag} unitCode="C62">1</cbc:{qty_tag}>
    <cbc:LineExtensionAmount currencyID="EUR">{base}</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>{_e(fields.get('description') or 'Zie bijlage')}</cbc:Name>
      <cac:ClassifiedTaxCategory><cbc:ID>{cat}</cbc:ID><cbc:Percent>{_d(v.get('rate'))}</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="EUR">{base}</cbc:PriceAmount></cac:Price>
  </cac:{line_tag}>""")

    gross = _d(fields.get("gross_total")) * sign if fields.get("gross_total") is not None else net_sum + vat_sum
    attachment = base64.b64encode(pdf_bytes).decode()
    iban = (fields.get("iban") or "").replace(" ", "")
    payment = (f"""
  <cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode><cac:PayeeFinancialAccount><cbc:ID>{_e(iban)}</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"""
               if iban else "")
    due = f"\n  <cbc:DueDate>{_e(fields['due_date'])}</cbc:DueDate>" if fields.get("due_date") and not credit else ""
    legal = f"<cbc:RegistrationName>{_e(fields.get('supplier_name'))}</cbc:RegistrationName>"
    if fields.get("supplier_kvk"):
        legal += f"<cbc:CompanyID schemeID=\"0106\">{_e(fields['supplier_kvk'])}</cbc:CompanyID>"
    supplier_tax = (f"<cac:PartyTaxScheme><cbc:CompanyID>{_e(vat_no)}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID>"
                    "</cac:TaxScheme></cac:PartyTaxScheme>") if vat_no else ""
    cust_vat = (customer.get("vat") or customer.get("vat_id") or "").replace(" ", "")
    customer_tax = (f"<cac:PartyTaxScheme><cbc:CompanyID>{_e(cust_vat)}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID>"
                    "</cac:TaxScheme></cac:PartyTaxScheme>") if cust_vat else ""
    country = vat_no[:2] if vat_no[:2].isalpha() else "NL"
    ns = "urn:oasis:names:specification:ubl:schema:xsd:" + root + "-2"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="{ns}" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:fdc:nen.nl:nlcius:v1.0</cbc:CustomizationID>
  <cbc:ProfileID>urn:fdc:peppol.eu:2017:poacc:billing:01:1.0</cbc:ProfileID>
  <cbc:ID>{_e(number)}</cbc:ID>
  <cbc:IssueDate>{_e(fields.get('invoice_date'))}</cbc:IssueDate>{due}
  <cbc:{type_tag}>{'381' if credit else '380'}</cbc:{type_tag}>
  <cbc:Note>Opgesteld uit het originele document (bijlage) en goedgekeurd door een medewerker.</cbc:Note>
  <cbc:DocumentCurrencyCode>{_e(fields.get('currency') or 'EUR')}</cbc:DocumentCurrencyCode>
  <cac:AdditionalDocumentReference>
    <cbc:ID>{_e(filename)}</cbc:ID>
    <cac:Attachment><cbc:EmbeddedDocumentBinaryObject mimeCode="application/pdf" filename="{_e(filename)}">{attachment}</cbc:EmbeddedDocumentBinaryObject></cac:Attachment>
  </cac:AdditionalDocumentReference>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>{_e(fields.get('supplier_name'))}</cbc:Name></cac:PartyName>
    <cac:PostalAddress><cac:Country><cbc:IdentificationCode>{_e(country)}</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    {supplier_tax}
    <cac:PartyLegalEntity>{legal}</cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyName><cbc:Name>{_e(customer.get('name'))}</cbc:Name></cac:PartyName>
    <cac:PostalAddress><cbc:StreetName>{_e(customer.get('address'))}</cbc:StreetName><cac:Country><cbc:IdentificationCode>NL</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    {customer_tax}
    <cac:PartyLegalEntity><cbc:RegistrationName>{_e(customer.get('name'))}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>{payment}
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="EUR">{vat_sum}</cbc:TaxAmount>{''.join(subtotals)}
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">{net_sum}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">{net_sum}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">{gross}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">{gross}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>{''.join(invoice_lines)}
</{root}>
"""


# ---------------------------------------------------------------- what we check ourselves on every file
NS = {"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"}


def _nl(x):
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def verify(xml: str):
    """The arithmetic rules of EN 16931 and the mandatory core fields, checked on the file itself. This is our own
    check on every export, not the official validator: a first import per bookkeeping package is still tested by hand.
    Returns a list of {"rule", "text", "ok", "detail"}."""
    from xml.etree import ElementTree as ET
    root = ET.fromstring(xml.encode("utf-8"))
    credit = root.tag.endswith("CreditNote")

    def num(path, node=root):
        el = node.find(path, NS)
        try:
            return Decimal(el.text) if el is not None and el.text else None
        except Exception:  # noqa: BLE001 - an unreadable amount is a failed rule below
            return None

    def text(path):
        el = root.find(path, NS)
        return (el.text or "").strip() if el is not None else ""

    line_tag = "cac:CreditNoteLine" if credit else "cac:InvoiceLine"
    lines = [num("cbc:LineExtensionAmount", ln) for ln in root.findall(line_tag, NS)]
    subs = [(num("cbc:TaxableAmount", s), num("cbc:TaxAmount", s), num("cac:TaxCategory/cbc:Percent", s))
            for s in root.findall("cac:TaxTotal/cac:TaxSubtotal", NS)]
    line_sum = num("cac:LegalMonetaryTotal/cbc:LineExtensionAmount")
    excl = num("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount")
    incl = num("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount")
    due = num("cac:LegalMonetaryTotal/cbc:PayableAmount")
    tax = num("cac:TaxTotal/cbc:TaxAmount")
    out = []

    def rule(code, label, ok, detail):
        out.append({"rule": code, "text": label, "ok": bool(ok), "detail": detail})

    core = {"factuurnummer (BT-1)": text("cbc:ID"), "factuurdatum (BT-2)": text("cbc:IssueDate"),
            "valuta (BT-5)": text("cbc:DocumentCurrencyCode"),
            "leverancier (BT-27)": text("cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName"),
            "afnemer (BT-44)": text("cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName")}
    missing = [k for k, v in core.items() if not v]
    rule("Kernvelden", "Factuurnummer, datum, valuta, leverancier en afnemer zijn gevuld", not missing,
         "Alle vijf aanwezig." if not missing else "Ontbreekt: " + ", ".join(missing))
    rule("Profiel", "CustomizationID is NLCIUS (de Nederlandse invulling van EN 16931, in UBL: SI-UBL 2.0)",
         text("cbc:CustomizationID").endswith("nlcius:v1.0"), text("cbc:CustomizationID") or "ontbreekt")
    if None in lines or line_sum is None or excl is None or incl is None or tax is None or due is None or not lines:
        rule("Bedragen", "Alle totalen zijn leesbaar", False, "Een van de totaalbedragen ontbreekt.")
        return out
    rule("BR-CO-10", "Som van de regels = totaal van de regels", sum(lines) == line_sum,
         f"{' + '.join(_nl(x) for x in lines)} = {_nl(sum(lines))}; in het bestand {_nl(line_sum)}")
    rule("BR-CO-13", "Totaal excl. btw = som van de regels (geen kortingen of toeslagen)", excl == line_sum,
         f"{_nl(excl)} tegenover {_nl(line_sum)}")
    rule("BR-CO-14", "Totaal btw = som van de btw per tarief", sum(s[1] for s in subs) == tax,
         f"{' + '.join(_nl(s[1]) for s in subs) or '0,00'} = {_nl(sum(s[1] for s in subs))}; in het bestand {_nl(tax)}")
    rule("BR-CO-15", "Totaal incl. btw = totaal excl. btw + btw", excl + tax == incl,
         f"{_nl(excl)} + {_nl(tax)} = {_nl(excl + tax)}; in het bestand {_nl(incl)}")
    rule("BR-CO-16", "Te betalen = totaal incl. btw (niets vooruitbetaald)", due == incl, f"{_nl(due)} tegenover {_nl(incl)}")
    bad = [s for s in subs if None in s or abs((s[0] * s[2] / 100).quantize(Decimal("0.01")) - s[1]) > Decimal("0.01")]
    rule("BR-CO-17", "Btw per tarief = grondslag x tarief, afgerond op de cent", not bad,
         "; ".join(f"{s[2].normalize():f}% van {_nl(s[0])} = {_nl((s[0] * s[2] / 100).quantize(Decimal('0.01')))}"
                   + ("" if s not in bad else f", in het bestand {_nl(s[1])}") for s in subs if None not in s))
    return out


def shorten(xml: str):
    """The file for reading on screen: the embedded PDF (thousands of base64 lines) replaced by one line."""
    import re
    def cut(m):
        return f"{m.group(1)}[pdf-bijlage, {len(m.group(2)) * 3 // 4 // 1024} kB als base64: volledig in de download]{m.group(3)}"
    return re.sub(r"(<cbc:EmbeddedDocumentBinaryObject[^>]*>)([^<]{200,})(</cbc:EmbeddedDocumentBinaryObject>)", cut, xml)
