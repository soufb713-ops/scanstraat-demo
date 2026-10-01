"""Exact Online REST API: connect once (OAuth 2), then post approved documents as a booking with the PDF attached.

Setup (free, own use): Exact App Center > Manage apps > Register a test app. Redirect URI:
https://<your address>/koppelingen/exact/callback. Put the Client ID and Secret in .env as
EXACT_CLIENT_ID / EXACT_CLIENT_SECRET (never in a chat).

Endpoints used (api/v1/{division}/...):
  crm/Accounts               find the supplier/customer by VAT number or name, create it if missing
  financial/GLAccounts       ledger account code -> ID
  documents/Documents        the document record (type 20 purchase invoice, 10 sales invoice)
  documents/DocumentAttachments   the PDF
  purchaseentry/PurchaseEntries   the booking (salesentry/SalesEntries for sales invoices)

Written from Exact's public API reference, not yet run against a real administration.
Only called after a person approved the document and pressed the button.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import vault

BASE = os.environ.get("EXACT_BASE", "https://start.exactonline.nl")
_lock = threading.Lock()


class ExactError(Exception):
    pass


def configured():
    return bool(os.environ.get("EXACT_CLIENT_ID") and os.environ.get("EXACT_CLIENT_SECRET"))


def auth_url(redirect_uri, state):
    q = urllib.parse.urlencode({"client_id": os.environ["EXACT_CLIENT_ID"], "redirect_uri": redirect_uri,
                                "response_type": "code", "state": state, "force_login": "0"})
    return f"{BASE}/api/oauth2/auth?{q}"


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ExactError(f"Exact weigert de aanmelding ({e.code})") from e
    except urllib.error.URLError as e:
        raise ExactError("Exact Online niet bereikbaar") from e


class Exact:
    def __init__(self, token_path):
        self.token_path = token_path

    # ---- tokens
    def token(self):
        return vault.read_json(self.token_path)

    def _save(self, tok):
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 600)) - 30
        vault.write_json(self.token_path, tok)

    def connect(self, code, redirect_uri):
        tok = _post_form(f"{BASE}/api/oauth2/token", {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": os.environ["EXACT_CLIENT_ID"], "client_secret": os.environ["EXACT_CLIENT_SECRET"]})
        self._save(tok)
        me = self.get("/api/v1/current/Me?$select=CurrentDivision,FullName")
        tok = self.token()
        tok["division"] = me[0]["CurrentDivision"] if me else None
        tok["user"] = me[0].get("FullName") if me else None
        vault.write_json(self.token_path, tok)
        return tok

    def disconnect(self):
        self.token_path.unlink(missing_ok=True)

    def _access(self):
        with _lock:
            tok = self.token()
            if not tok:
                raise ExactError("Exact Online is niet gekoppeld")
            if time.time() > tok.get("expires_at", 0):
                new = _post_form(f"{BASE}/api/oauth2/token", {
                    "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                    "client_id": os.environ["EXACT_CLIENT_ID"], "client_secret": os.environ["EXACT_CLIENT_SECRET"]})
                for k in ("division", "user"):
                    new[k] = tok.get(k)
                self._save(new)
                tok = self.token()
            return tok["access_token"]

    # ---- http
    def _req(self, method, path, body=None):
        req = urllib.request.Request(BASE + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self._access()}", "Accept": "application/json",
                                              "Content-Type": "application/json", "Prefer": "return=representation"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            raise ExactError(f"Exact API {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ExactError("Exact Online niet bereikbaar") from e
        d = data.get("d", data)
        return d.get("results", d) if isinstance(d, dict) else d

    def get(self, path):
        return self._req("GET", path)

    def post(self, path, body):
        return self._req("POST", path, body)

    # ---- booking
    def _account(self, div, r, sales):
        flt = []
        if r.get("supplier_vat_number"):
            flt.append(f"VATNumber eq '{r['supplier_vat_number']}'")
        name = (r.get("counterparty") or "").replace("'", "''")
        flt.append(f"Name eq '{name}'")
        for f in flt:
            found = self.get(f"/api/v1/{div}/crm/Accounts?$filter={urllib.parse.quote(f)}&$select=ID")
            if found:
                return found[0]["ID"]
        body = {"Name": r.get("counterparty") or "Onbekend"}
        if sales:
            body["Status"] = "C"
        else:
            body["IsSupplier"] = True
        if r.get("supplier_vat_number"):
            body["VATNumber"] = r["supplier_vat_number"]
        if r.get("supplier_kvk"):
            body["ChamberOfCommerce"] = r["supplier_kvk"]
        return self.post(f"/api/v1/{div}/crm/Accounts", body)["ID"]

    def _gl(self, div, code):
        flt = urllib.parse.quote(f"Code eq '{code}'")
        found = self.get(f"/api/v1/{div}/financial/GLAccounts?$filter={flt}&$select=ID")
        if not found:
            raise ExactError(f"Grootboekrekening {code} bestaat niet in deze Exact-administratie")
        return found[0]["ID"]

    def book(self, division, doc, pdf_bytes, journal, vat_map):
        div = division or (self.token() or {}).get("division")
        if not div:
            raise ExactError("Geen Exact-administratie (divisie) bekend voor deze klant")
        r = doc["result"]
        sales = doc["kind"] == "verkoopfactuur"
        account = self._account(div, r, sales)
        document = self.post(f"/api/v1/{div}/documents/Documents", {
            "Subject": doc["filing"]["filename"][:-4][:200], "Type": 10 if sales else 20, "Account": account})
        self.post(f"/api/v1/{div}/documents/DocumentAttachments", {
            "Document": document["ID"], "FileName": doc["filing"]["filename"],
            "Attachment": base64.b64encode(pdf_bytes).decode()})
        lines = [{"GLAccount": self._gl(div, l["account"]), "AmountFC": float(l["amount_excl"]),
                  "VATCode": vat_map.get(l["vat_code"], l["vat_code"]), "Description": (l.get("description") or "")[:60]}
                 for l in r.get("lines") or []]
        entry = {"Journal": journal, "EntryDate": r.get("doc_date"), "YourRef": (r.get("reference") or "")[:30],
                 "Description": (r.get("description") or "")[:60], "Currency": r.get("currency") or "EUR",
                 "Document": document["ID"]}
        if r.get("due_date"):
            entry["DueDate"] = r["due_date"]
        if sales:
            entry.update(Customer=account, SalesEntryLines=lines)
            res = self.post(f"/api/v1/{div}/salesentry/SalesEntries", entry)
        else:
            entry.update(Supplier=account, PurchaseEntryLines=lines)
            res = self.post(f"/api/v1/{div}/purchaseentry/PurchaseEntries", entry)
        return {"division": div, "entry_id": res.get("EntryID"), "entry_number": res.get("EntryNumber"),
                "document_id": document["ID"]}
