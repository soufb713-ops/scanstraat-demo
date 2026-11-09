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
