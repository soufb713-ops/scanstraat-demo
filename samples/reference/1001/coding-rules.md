# Coding rules for the demo client (Studio Lindewerf B.V.)

These are the rules the reasoning step gets in its prompt. They are **demo assumptions written from general knowledge, not checked against belastingdienst.nl yet**. Soufyan: check each rule marked "verify" before the meeting and put the source URL next to it.

| # | Rule | Status |
|---|---|---|
| R01 | A recurring supplier gets the same account and VAT code as last month (see `supplier-map.csv`) unless the document clearly differs. | demo policy |
| R02 | Food items (coffee, milk, fruit) are 9%; cleaning products and paper are 21%. One receipt with both rates gets one booking line per rate. | verify rates |
| R03 | Public transport tickets are 9%. Taxi is also 9% (passenger transport). Parking is 21%. | verify |
| R04 | Services bought from an EU business with "reverse charge" on the invoice: code `EU_DIENST_VERL`. Self-assess 21% in box 4b and deduct it in box 5b. The invoice itself shows 0% VAT. | verify |
| R05 | Coffee, milk and fruit bought for the office pantry: account 4530 Kantinekosten, VAT deductible. | verify (BUA) |
| R06 | Food and drink consumed in a restaurant or café (horeca): VAT is **not** deductible. Book the gross amount to 4520 Representatie with code `GEEN_AFTREK`. Ask for the business purpose. | verify |
| R07 | A purchase with no plausible link to the business (toys, clothing, groceries for home) goes to 1800 Rekening-courant DGA with code `PRIVE`. Never auto-book; always review. | demo policy |
| R08 | Equipment costing more than EUR 450 excl. VAT per item is a fixed asset (0210), not a cost. VAT is deductible as normal. | verify threshold |
| R09 | A credit note is booked as negative amounts on the account of the original invoice. Link it to that invoice number. | standard |
| R10 | The same supplier + invoice number + amount twice is a duplicate. Book once, flag the other. | standard |
| R11 | If a field cannot be read, say so. A value computed from other fields (e.g. VAT from total at 21%) is marked as computed and goes to review. | demo policy |
| R12 | If rate x base does not equal the stated VAT (tolerance EUR 0.02), or lines do not add up to the total, flag it. Never correct silently. Deduct at most the correct VAT. | demo policy |
| R13 | Rent invoiced in advance for next month: VAT follows the invoice date; the cost may be moved to 1400 Vooruitbetaalde kosten. Flag as informational. | verify with a bookkeeper |
| R14 | A bank payment with no document is a missing document. Recurring suppliers first, then card payments. Suggest a chase message, don't book. | demo policy |
| R15 | Invoices from a one-person business (eenmanszaak) contain a person's name, private e-mail and IBAN: these are masked before reasoning. Supplier trading names pass through. | privacy policy |
