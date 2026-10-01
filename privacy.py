"""Local privacy step, used only in hybrid mode (SCAN_AI=hybrid) before anything goes to Claude.

1. Text: read on this machine with Tesseract OCR, or, without Tesseract, transcribed by the local vision model.
   Page images never leave the machine.
2. Special category screen (AVG art. 9 and 10): health, union, religion, politics, criminal data. A hit keeps
   the whole document local; it is never sent to a US service, not even anonymised.
3. Pseudonymisation: fixed rules (IBAN, e-mail, phone, BSN with the 11-test, date of birth) plus the local
   Apache 2.0 model (qwen2.5:7b, see models.py) for names of people and home addresses.
   Each value becomes a placeholder like [PERSOON_1]; the mapping is kept, encrypted, in the vault.
4. Restore: Claude's answer gets the real values back on this machine.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import models

MAX_CHARS = 12000


class PrivacyError(Exception):
    pass


# ---------------------------------------------------------------- 1. text, locally
def ocr_image(jpeg_bytes):
    """Tesseract on this machine. Returns text, or None when Tesseract is not installed."""
    if not shutil.which("tesseract"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "p.jpg"
        f.write_bytes(jpeg_bytes)
        out = subprocess.run(["tesseract", str(f), "-", "-l", "nld+eng", "--psm", "4"],
                             capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise PrivacyError("OCR mislukt: " + out.stderr.strip()[:200])
    return out.stdout.strip()


# ---------------------------------------------------------------- 2. special categories
# Word stems, matched case-insensitively at a word start. Deliberately broad: a false hit only means the
# document is read by the local model instead of Claude.
# Matched case-insensitively as whole words; a trailing * allows any ending (huisarts* = huisartsenpraktijk).
# Deliberately broad: a false hit only means the document is read by the local model instead of Claude.
SPECIAL = {
    "gezondheid": ["huisarts*", "apothe*", "tandarts*", "mondhygi*", "fysiotherap*", "ziekenhuis*", "ggz", "psycholo*",
                   "psychiat*", "zorgverzeker*", "eigen risico", "medicijn*", "medicatie", "diagnose", "dbc-*",
                   "orthodont*", "verloskund*", "kraamzorg", "logopedi*", "audicien", "opticien", "hoortoestel*",
                   "ziekteverzuim", "arbodienst", "bedrijfsarts", "wlz", "wmo", "pgb", "zorgtoeslag", "patiënt*", "patient*"],
    "vakbond": ["vakbond*", "fnv", "cnv", "vakcentrale"],
    "religie": ["kerk", "kerkelijk*", "parochie", "moskee", "synagoge", "kerkbalans", "diaconie", "zakat"],
    "politiek": ["politieke partij", "partijlidmaatschap"],
    "strafrecht": ["cjib", "strafbeschikking", "proces-verbaal", "openbaar ministerie", "dagvaarding", "reclassering"],
    "biometrie_genetisch": ["dna-test", "genetisch onderzoek"],
}


def _term(t):
    return re.escape(t[:-1]) + r"\w*" if t.endswith("*") else re.escape(t) + r"(?!\w)"


_SPECIAL_RE = {cat: re.compile(r"(?i)(?<!\w)(" + "|".join(_term(t) for t in terms) + r")")
               for cat, terms in SPECIAL.items()}


def special_categories(text):
    """Returns {category: [matched terms]} for AVG art. 9/10 indicators."""
    hits = {}
    for cat, rx in _SPECIAL_RE.items():
        found = sorted({m.group(1).lower() for m in rx.finditer(text or "")})
        if found:
            hits[cat] = found
    return hits


# ---------------------------------------------------------------- 3. personal data
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?\b")
IBAN_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{4}\d{7,}$")  # bank-code letters: not a VAT number
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PHONE_RE = re.compile(r"(?<![\w/])(?:\+31|0031|0)(?:[\s-]?\d){9}(?!\d)")
NINE_RE = re.compile(r"(?<![\w.])\d{4}\.?\d{2}\.?\d{3}(?![\w.])|(?<![\w.])\d{3}[ .]\d{3}[ .]\d{3}(?![\w.])|(?<![\w.])\d{9}(?![\w.])")
BIRTH_RE = re.compile(r"(?i)(geboortedatum|geb\.|geboren)\s*:?\s*\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")


def bsn_valid(digits):
    if len(digits) != 9 or digits == "000000000":
        return False
    total = sum(int(d) * w for d, w in zip(digits[:8], range(9, 1, -1))) - int(digits[8])
    return total % 11 == 0


def rule_findings(text, strict=False):
    """IBAN, e-mail, phone, BSN, date of birth. strict=True (the egress gate) also counts every IBAN-shaped
    string and every 9-digit number that passes the 11-test, wherever it sits."""
    from checks import iban_valid
    found = []
    for m in IBAN_RE.finditer(text):
        if iban_valid(m.group(0)) or (strict and IBAN_SHAPE.match(re.sub(r"\s", "", m.group(0)))):
            found.append(("IBAN", m.group(0)))
    for m in EMAIL_RE.finditer(text):
        found.append(("EMAIL", m.group(0)))
    for m in PHONE_RE.finditer(text):
        found.append(("TELEFOON", m.group(0).strip()))
    for m in NINE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        before = text[max(0, m.start() - 4):m.start()]
        if bsn_valid(digits) and (strict or "NL" not in before.upper()):  # NL123456789B01 is a VAT number
            found.append(("BSN", m.group(0)))
    for m in BIRTH_RE.finditer(text):
        found.append(("GEBOORTEDATUM", m.group(0)))
    return found


PROMPT = """Je krijgt de tekst van een factuur, bon of brief. Geef ALLEEN persoonsgegevens van natuurlijke personen:
namen van mensen (ook in een bedrijfsnaam van een eenmanszaak zoals "Fotografie Jan de Vries") en privé-woonadressen.
Geen bedrijfsnamen zonder persoonsnaam, geen bedrijfsadressen van bv's, geen bedragen.
Kopieer elke waarde exact zoals in de tekst. Antwoord als JSON: {"personen": [...], "adressen": [...]}

Tekst:
"""


def model_findings(text, call_local):
    """Names and home addresses via the local Apache 2.0 model. call_local(model, prompt) -> dict.
    Raises PrivacyError if the model cannot run: in hybrid mode a document is then not sent at all."""
    try:
        answer = call_local(models.require(models.ANON_MODEL), PROMPT + text[:MAX_CHARS])
    except models.LicenceError as e:
        raise PrivacyError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise PrivacyError(f"Lokaal anonimiseringsmodel niet beschikbaar ({type(e).__name__}): document blijft lokaal") from e
    found = []
    for key, label in (("personen", "PERSOON"), ("adressen", "ADRES")):
        for value in answer.get(key) or []:
            if isinstance(value, str) and len(value.strip()) > 2 and value.strip() in text:
                found.append((label, value.strip()))
    return found


class Pseudonymiser:
    """One mapping per document (or per batch for the split step), so the same value gets the same placeholder."""

    def __init__(self, mapping=None):
        self.mapping = dict(mapping or {})              # placeholder -> original
        self._by_value = {v: k for k, v in self.mapping.items()}
        self.counts = {}
        for k in self.mapping:
            kind = k.strip("[]").rsplit("_", 1)[0]
            self.counts[kind] = self.counts.get(kind, 0) + 1

    def apply(self, text, findings):
        for kind, value in sorted(findings, key=lambda kv: -len(kv[1])):  # longest first
            if value not in text:
                continue
            token = self._by_value.get(value)
            if not token:
                self.counts[kind] = self.counts.get(kind, 0) + 1
                token = f"[{kind}_{self.counts[kind]}]"
                self._by_value[value] = token
                self.mapping[token] = value
            text = text.replace(value, token)
        return text

    def anonymise(self, text, call_local):
        rules = rule_findings(text)
        named = model_findings(text, call_local)
        return self.apply(text, rules + named)

    def restore(self, value):
        if isinstance(value, str):
            return TOKEN_RE.sub(lambda m: self.mapping.get(m.group(0), m.group(0)), value)
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        return value


TOKEN_RE = re.compile(r"\[(?:IBAN|EMAIL|TELEFOON|BSN|GEBOORTEDATUM|PERSOON|ADRES)_\d+\]")


def summary(mapping):
    """Counts per kind, safe to show and to log (no values)."""
    out = {}
    for k in mapping:
        kind = k.strip("[]").rsplit("_", 1)[0]
        out[kind] = out.get(kind, 0) + 1
    return out


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)
