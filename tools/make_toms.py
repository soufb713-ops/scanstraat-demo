"""Generates static/docs/technische-beschrijving-veiligheid.pdf: the technical and organisational measures (TOMs,
art. 32 AVG) of a Scanstraat office environment, for prospects and as an annex to the verwerkersovereenkomst.

    .venv/bin/python tools/make_toms.py [extra output path ...]

Facts about sub-processor certificates were checked on the providers' own pages on CHECKED (see SOURCES). Update
CHECKED and the text when a provider changes its certificates. Every statement about the software must match the
code: privacy.py, egress.py, vault.py, backup.py, auth.py, region.py, retention.py, checks.py, ubl.py.
"""
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "static" / "docs" / "technische-beschrijving-veiligheid.pdf"
VERSION, DATE, CHECKED = "1.0", "14 november 2026", "14 november 2026"

pdfmetrics.registerFont(TTFont("Sans", str(HERE / "fonts" / "LiberationSans-Regular.ttf")))
pdfmetrics.registerFont(TTFont("Sans-Bold", str(HERE / "fonts" / "LiberationSans-Bold.ttf")))
pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans", boldItalic="Sans-Bold")

INK, MUTE, LINE, EM = colors.HexColor("#0b1f33"), colors.HexColor("#475569"), colors.HexColor("#cbd5e1"), colors.HexColor("#047857")
S = {
    "title": ParagraphStyle("t", fontName="Sans-Bold", fontSize=16, leading=19, textColor=INK, spaceAfter=3),
    "sub": ParagraphStyle("s", fontName="Sans", fontSize=9, leading=12, textColor=MUTE, spaceAfter=8),
    "h": ParagraphStyle("h", fontName="Sans-Bold", fontSize=10.5, leading=13, textColor=INK, spaceBefore=9, spaceAfter=3),
    "p": ParagraphStyle("p", fontName="Sans", fontSize=8.6, leading=11.6, textColor=INK, alignment=TA_LEFT, spaceAfter=3),
    "cell": ParagraphStyle("c", fontName="Sans", fontSize=8, leading=10.4, textColor=INK),
    "cellb": ParagraphStyle("cb", fontName="Sans-Bold", fontSize=8, leading=10.4, textColor=INK),
    "box": ParagraphStyle("b", fontName="Sans", fontSize=8.4, leading=11.2, textColor=INK),
    "fine": ParagraphStyle("f", fontName="Sans", fontSize=7.4, leading=9.6, textColor=MUTE),
}

STATUS = ("<b>Reikwijdte en stand van zaken.</b> Deze beschrijving geldt voor een kantooromgeving van Scanstraat: een eigen "
          "server per kantoor, ingericht na een getekende verwerkersovereenkomst. Scanstraat heeft zelf geen ISO-, SOC- of "
          "ander certificaat. De certificaten in paragraaf 3 zijn van de subverwerkers en gaan over hun datacenters en "
          "diensten, niet over de software of werkwijze van Scanstraat. De openbare demonstratie op descanstraat.nl bevat "
          "alleen fictieve gegevens en valt buiten deze beschrijving.")

PROCESSING = [
    ("Doel", "Binnengekomen stukken splitsen, lezen en controleren, klaarzetten voor het boekhoud- en archiefpakket van het "
             "Kantoor, en conceptberichten aan klanten over ontbrekende stukken opstellen. Scanstraat boekt niets, dient niets "
             "in en verstuurt niets zonder goedkeuring van een medewerker van het Kantoor."),
    ("Gegevens", "Naam-, adres- en contactgegevens, IBAN, factuur- en bongegevens, btw- en KvK-nummers, mogelijk een BSN op "
                 "post van de Belastingdienst; gebruikers- en auditgegevens van medewerkers. Bijzondere gegevens zijn niet de "
                 "bedoeling; zie maatregel M5."),
    ("Bewaren", "Origineel, paginabeelden en koppeltabel van de pseudonimisering: gewist zodra het Kantoor de export heeft "
                "bevestigd. Uit de stapel gehaalde stukken: na 24 uur. Gecontroleerde velden en auditlog: zolang de "
                "overeenkomst loopt. Versleutelde back-ups: 7 dagen."),
]

MEASURES = [
    ("M1", "Locatie", "Server en back-ups bij Hetzner Online GmbH in Falkenstein of Neurenberg (Duitsland). Met een productie-"
                      "instelling start de software alleen als hosting- en back-uplocatie een van die twee is (region.py). "
                      "De software kan de fysieke plek niet zelf zien: het inrichtingsscript zet de server daar neer."),
    ("M2", "Versleuteling in rust", "Alle bestanden (scans, paginabeelden, pdf's, velden, koppeltabellen, gebruikers met hun "
                                    "2FA-sleutels, berichten, auditlog) met AES-256-GCM; de sleutel staat buiten de datamap "
                                    "(vault.py). Back-ups apart versleuteld met een eigen sleutel (backup.py)."),
    ("M3", "Versleuteling onderweg", "Alle verbindingen via HTTPS (TLS). Sessiecookies HttpOnly, SameSite=Lax en Secure; "
                                     "HSTS. Elk formulier en elke wijzigende aanroep met een CSRF-token."),
    ("M4", "Toegang", "Persoonlijke accounts, rollen beheerder en medewerker, wachtwoord plus verplichte tweestapsverificatie "
                      "(TOTP). Na 5 foute pogingen 15 minuten blokkering. Een 2FA-code werkt maar één keer. Eerste inlog "
                      "dwingt een nieuw wachtwoord af (auth.py)."),
    ("M5", "Minimale gegevens naar AI", "Tekst wordt op de eigen server herkend. IBAN, e-mail, telefoon, BSN (met 11-proef) en "
                                        "geboortedatum worden met vaste regels vervangen, namen en woonadressen door een lokaal "
                                        "taalmodel (Apache 2.0). Wijst een stuk op gezondheid, vakbond, religie, politiek of "
                                        "strafrecht (AVG art. 9 en 10), dan blijft het hele stuk lokaal (privacy.py)."),
    ("M6", "Poort naar buiten", "Eén module mag met een externe AI-dienst praten (egress.py). Die controleert vlak voor "
                                "verzending opnieuw, los van het filter, en blokkeert bij elke gevonden persoonsgegeven of "
                                "aanwijzing voor bijzondere gegevens. Alleen tekst: paginabeelden verlaten een productieserver "
                                "nooit. Amazon Bedrock alleen in een EU-regio; een andere regio weigert de software."),
    ("M7", "Keuze per kantoor", "Stand lokaal: er gaat niets naar een AI-dienst. Stand hybride: alleen gefilterde tekst naar "
                                "Claude via Amazon Bedrock, regio eu-west-1 (Ierland)."),
    ("M8", "Geen AI-beslissingen over geld", "Btw per tarief, totalen, IBAN-controlegetal, IBAN tegenover eerdere facturen van "
                                             "dezelfde leverancier en de EN 16931-rekenregels op elk exportbestand zijn vaste "
                                             "code (checks.py, ubl.py). Het taalmodel doet alleen voorstellen."),
    ("M9", "Menselijke goedkeuring", "Geen export naar een pakket en geen bericht naar een klant zonder akkoord van een "
                                     "medewerker. Per klant een Wwft-stop. Dit is geen instelling en niet uit te zetten."),
    ("M10", "Vastleggen", "Auditlog van elke handeling (wie, wat, wanneer). Elke poging tot verzending naar de AI-dienst, "
                          "verstuurd of geblokkeerd, met bestemming en een SHA-256-vingerafdruk van precies wat er wegging."),
    ("M11", "Wissen", "Automatisch volgens de bewaartermijnen hierboven: direct na bevestiging en als vangnet elke 5 minuten "
                      "(retention.py). Na het einde van de overeenkomst alles binnen 30 dagen, back-ups 7 dagen later."),
    ("M12", "Onderhoud en organisatie", "Beveiligingsupdates maandelijks, kritieke updates binnen 72 uur. Geheimhouding. "
                                        "Datalek binnen 24 uur gemeld aan het Kantoor. Deze maatregelen worden jaarlijks herzien."),
]

SUBS = [
    ("Hetzner Online GmbH, Duitsland",
     "Server en versleutelde back-ups, Falkenstein of Neurenberg.",
     "ISO/IEC 27001:2022, gecertificeerd door SOCOTEC Certification. Bereik: infrastructuur, beheer en support van de "
     "datacenterparken Neurenberg, Falkenstein en Helsinki. Geen SOC 2 of BSI C5 vermeld.",
     "Verwerkersovereenkomst art. 28 AVG via het Hetzner-klantaccount."),
    ("Amazon Web Services EMEA SARL, Luxemburg (Amazon Bedrock)",
     "Voert Claude (Anthropic) uit op gefilterde tekst. Alleen in de stand hybride. Regio eu-west-1.",
     "ISO/IEC 27001:2022, 27017, 27018, 27701; SOC 1, 2 en 3; BSI C5. Amazon Bedrock staat op de AWS-lijsten van diensten "
     "binnen het bereik (Bedrock Marketplace uitgezonderd).",
     "AWS-verwerkersvoorwaarden met standaardcontractbepalingen. Invoer en uitvoer niet gebruikt voor training."),
]

SOURCES = ("Bronnen, nagekeken op " + CHECKED + ": hetzner.com/unternehmen/zertifizierung; aws.amazon.com/compliance/iso-certified; "
           "aws.amazon.com/compliance/services-in-scope/SOC; aws.amazon.com/compliance/services-in-scope/C5.")

LIMITS = ("<b>Wat nog niet is gedaan.</b> Er is nog geen externe penetratietest uitgevoerd. De leeskwaliteit van de lokale "
          "stand is nog niet gemeten op echte stukken; dat gebeurt in de proef op facturen van het Kantoor zelf. Geen "
          "filter is foutloos: daarom zijn er twee lagen (M5 en M6) en blijft een twijfelgeval lokaal.")

PROOF = ("<b>Bewijs zonder klantdata.</b> 1. In de browsertest op descanstraat.nl leest de browser een pdf-factuur zonder "
         "upload. 2. Vijf facturen van het Kantoor zelf (internet, huur, kantoorartikelen) gaan door de hele straat tot in "
         "het eigen pakket, na ondertekening van de verwerkersovereenkomst. 3. Pas daarna drie klantdossiers.")


def table(rows, widths, head=None, zebra=False):
    data = ([[Paragraph(h, S["cellb"]) for h in head]] if head else []) + \
           [[Paragraph(c, S["cellb"] if i == 0 else S["cell"]) for i, c in enumerate(r)] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1 if head else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
          ("TOPPADDING", (0, 0), (-1, -1), 3.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
          ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]
    if head:
        st += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")), ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK)]
    t.setStyle(TableStyle(st))
    return t


def box(text):
    t = Table([[Paragraph(text, S["box"])]], colWidths=[180 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, EM), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ecfdf5")),
                           ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def footer(c, doc):
    c.saveState()
    c.setFont("Sans", 7)
    c.setFillColor(MUTE)
    c.drawString(15 * mm, 9 * mm, f"Scanstraat · Technische beschrijving en beveiligingsmaatregelen · versie {VERSION}, {DATE}")
    c.drawRightString(195 * mm, 9 * mm, f"pagina {doc.page}")
    c.restoreState()


def build(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title="Technische beschrijving en beveiligingsmaatregelen Scanstraat",
                            author="Scanstraat", subject="Technische en organisatorische maatregelen, artikel 32 AVG")
    f = [Paragraph("Technische beschrijving en beveiligingsmaatregelen", S["title"]),
         Paragraph(f"Technische en organisatorische maatregelen (TOMs) conform artikel 32 AVG · bijlage bij de "
                   f"verwerkersovereenkomst · versie {VERSION}, {DATE}", S["sub"]),
         box(STATUS),
         Paragraph("1. De verwerking in het kort", S["h"]),
         table(PROCESSING, [26 * mm, 154 * mm]),
         Paragraph("2. Maatregelen", S["h"]),
         table([(f"{n} {t}", d) for n, t, d in MEASURES], [42 * mm, 138 * mm], head=["Maatregel", "Hoe het werkt (bestand in de code)"]),
         KeepTogether([Paragraph("3. Subverwerkers en hun certificaten", S["h"]),
                       table(SUBS, [36 * mm, 38 * mm, 60 * mm, 46 * mm], head=["Partij", "Rol", "Certificaten (van de partij zelf)", "Afspraak"]),
                       Spacer(1, 3), Paragraph(SOURCES, S["fine"]),
                       Paragraph("Geen subverwerker van Scanstraat, wel van het Kantoor zelf: het WhatsApp Business-account "
                                 "en de mailserver van het Kantoor en het boekhoudpakket (zoals SnelStart of Exact Online).", S["fine"])]),
         KeepTogether([Paragraph("4. Grenzen en bewijs", S["h"]), Paragraph(LIMITS, S["p"]), Paragraph(PROOF, S["p"])]),
         Spacer(1, 4),
         Paragraph("Scanstraat bereidt voor: er wordt niets ingediend of definitief geboekt zonder een medewerker van het Kantoor. "
                   "Geen fiscaal of juridisch advies. Vragen over deze maatregelen of een controle volgens artikel 10 van de "
                   "verwerkersovereenkomst: neem contact op via descanstraat.nl. De werking van het privacyfilter is live te "
                   "zien op descanstraat.nl/veiligheid.", S["fine"])]
    doc.build(f, onFirstPage=footer, onLaterPages=footer)
    return path


if __name__ == "__main__":
    for p in [OUT] + [Path(a) for a in sys.argv[1:]]:
        print(build(p))
