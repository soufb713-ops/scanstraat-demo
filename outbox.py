"""Outgoing messages to clients (e-mail, WhatsApp): drafts only, sent after a named staff member approves.

Wwft art. 23 forbids telling a client (or anyone) that an unusual transaction was or will be reported, or that
an investigation is under way. An automatic reminder or bank-match question can do exactly that by accident.
So in this app:
  - REQUIRES_STAFF_APPROVAL is a constant in code. There is no setting, environment variable or admin switch
    that turns it off.
  - Nothing in the app sends on its own: no scheduler, no webhook and no pipeline step reaches the private transport function.
    The only caller is approve_and_send(), which is only reached from the dashboard by a logged-in person.
  - Approval needs two explicit confirmations and is bound to a hash of the exact text: editing the message
    afterwards voids the approval.
  - A client can be put on "Wwft-stop" by an administrator (e.g. while a report to FIU-Nederland is considered).
    Messages to that client cannot be approved at all until the stop is lifted.
Every step is in the audit log.
"""
import hashlib
import os
import smtplib
import threading
from datetime import datetime
from email.message import EmailMessage

import vault
import whatsapp

REQUIRES_STAFF_APPROVAL = True
CHANNELS = {"email": "E-mail", "whatsapp": "WhatsApp"}
CONFIRM_READ = "Ik heb de tekst en de ontvanger zelf gecontroleerd."
CONFIRM_WWFT = ("Dit bericht zegt niets over een (mogelijke) melding aan FIU-Nederland, een onderzoek of een "
                "ongebruikelijke transactie, en er geldt geen Wwft-stop voor deze klant.")

_lock = threading.Lock()


class OutboxError(Exception):
    pass


def _now():
    return datetime.now().isoformat(timespec="seconds")


def content_hash(m):
    return hashlib.sha256("\x1e".join([m["channel"], m["to"], m.get("subject") or "", m["body"]]).encode()).hexdigest()


class Outbox:
    def __init__(self, path, audit):
        self.path, self.audit = path, audit

    def all(self):
        return vault.read_json(self.path, {"seq": 0, "messages": {}})

    def _save(self, data):
        vault.write_json(self.path, data)

    def drafts(self):
        return [m for m in self.all()["messages"].values() if m["status"] == "draft"]

    def draft(self, channel, to, subject, body, client_nr, doc_id, created_by, reason):
        if channel not in CHANNELS:
            raise OutboxError("Onbekend kanaal")
        with _lock:
            data = self.all()
            data["seq"] += 1
            mid = f"M{data['seq']:04d}"
            data["messages"][mid] = {"id": mid, "channel": channel, "to": to.strip(), "subject": subject, "body": body,
                                     "client_nr": client_nr, "doc": doc_id, "reason": reason, "status": "draft",
                                     "requires_staff_approval": REQUIRES_STAFF_APPROVAL,
                                     "created": _now(), "created_by": created_by, "approval": None, "sent": None}
            self._save(data)
        self.audit("-", doc_id or "-", "message_drafted", created_by, message=mid, channel=channel, reason=reason)
        return mid

    def edit(self, mid, to, subject, body, user):
        with _lock:
            data = self.all()
            m = data["messages"].get(mid)
            if not m or m["status"] != "draft":
                raise OutboxError("Alleen concepten kun je wijzigen")
            m.update(to=to.strip(), subject=subject, body=body, approval=None)
            self._save(data)
        self.audit("-", m.get("doc") or "-", "message_edited", user, message=mid)

    def discard(self, mid, user):
        with _lock:
            data = self.all()
            m = data["messages"].get(mid)
            if not m or m["status"] != "draft":
                raise OutboxError("Alleen concepten kun je verwijderen")
            m.update(status="discarded", discarded={"by": user, "at": _now()})
            self._save(data)
        self.audit("-", m.get("doc") or "-", "message_discarded", user, message=mid)

    def approve_and_send(self, mid, user, confirm_read, confirm_wwft, wwft_stop):
        """user: the logged-in staff member (dict with email/name/role). Never called by the system itself."""
        if not REQUIRES_STAFF_APPROVAL:  # pragma: no cover - the constant is the point
            raise OutboxError("Configuratiefout")
        if not user or not user.get("email") or user.get("name") in (None, "", "Systeem"):
            raise OutboxError("Alleen een ingelogde medewerker kan een bericht goedkeuren")
        if not (confirm_read and confirm_wwft):
            raise OutboxError("Vink beide verklaringen aan voordat je verstuurt")
        with _lock:
            data = self.all()
            m = data["messages"].get(mid)
            if not m or m["status"] != "draft":
                raise OutboxError("Dit bericht is geen concept meer")
            if wwft_stop:
                raise OutboxError("Voor deze klant geldt een Wwft-stop: berichten kunnen niet worden verstuurd")
            if not m["to"] or not m["body"].strip():
                raise OutboxError("Ontvanger en tekst zijn verplicht")
            m["approval"] = {"by": user["name"], "email": user["email"], "at": _now(), "hash": content_hash(m),
                             "statements": [CONFIRM_READ, CONFIRM_WWFT]}
            how = _transport(m)
            m["status"] = "sent" if how["sent"] else "approved"
            m["sent"] = how | {"at": _now()}
            self._save(data)
        self.audit("-", m.get("doc") or "-", "message_approved", user["name"], message=mid, channel=m["channel"],
                   hash=m["approval"]["hash"][:16])
        self.audit("-", m.get("doc") or "-", "message_sent" if how["sent"] else "message_not_sent", user["name"],
                   message=mid, route=how["route"])
        return m


def _transport(m):
    """Private. Only approve_and_send() calls this, after a person approved this exact text."""
    assert m.get("approval") and m["approval"]["hash"] == content_hash(m), "geen geldige goedkeuring"
    if m["channel"] == "email" and os.environ.get("SMTP_HOST"):
        msg = EmailMessage()
        msg["From"] = os.environ.get("SMTP_FROM", "")
        msg["To"] = m["to"]
        msg["Subject"] = m.get("subject") or ""
        msg.set_content(m["body"])
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
        return {"sent": True, "route": "SMTP"}
    if m["channel"] == "whatsapp" and whatsapp.configured():
        ok = whatsapp.send_text(m["to"], m["body"])
        return {"sent": ok, "route": "WhatsApp Cloud API" if ok else "WhatsApp mislukt"}
    return {"sent": False, "route": "demo: niet verstuurd (geen verzendkanaal ingesteld)"}
