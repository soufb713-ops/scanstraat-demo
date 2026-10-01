"""WhatsApp intake through Meta's WhatsApp Cloud API.

Meta calls /webhook/whatsapp when a client sends a photo or pdf to the firm's WhatsApp Business number.
We check Meta's signature, download the file with the firm's token and put it in the normal pipeline.

Settings (typed into the server's .env by the owner, never in a chat):
  WHATSAPP_TOKEN            permanent access token (System User) with whatsapp_business_messaging
  WHATSAPP_PHONE_NUMBER_ID  the id of the business number (not the number itself)
  WHATSAPP_VERIFY_TOKEN     any long random string; the same string goes into Meta's webhook form
  WHATSAPP_APP_SECRET       the Meta app's secret, used to check every incoming call really comes from Meta
"""
import hashlib
import hmac
import json
import os
import re
import urllib.request

GRAPH = os.environ.get("WHATSAPP_GRAPH_URL", "https://graph.facebook.com").rstrip("/") + "/" + os.environ.get("WHATSAPP_API_VERSION", "v21.0")
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}


def env(name):
    return os.environ.get(name, "").strip()


def configured():
    return all(env(k) for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET"))


def status():
    return {k: bool(env(k)) for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET")}


def verify(args):
    """Meta's one-time check when the webhook is saved. Returns the challenge or None."""
    token = env("WHATSAPP_VERIFY_TOKEN")
    if token and args.get("hub.mode") == "subscribe" and hmac.compare_digest(args.get("hub.verify_token", ""), token):
        return args.get("hub.challenge", "")
    return None


def signature_ok(raw: bytes, header: str):
    secret = env("WHATSAPP_APP_SECRET")
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def parse(payload):
    """Yields one dict per incoming message."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []):
                kind = m.get("type")
                item = {"id": m.get("id"), "from": "+" + m.get("from", "").lstrip("+"), "type": kind}
                if kind in ("image", "document"):
                    media = m[kind]
                    item.update(media_id=media.get("id"), mime=media.get("mime_type", ""),
                                filename=media.get("filename") or "", caption=media.get("caption", ""))
                elif kind == "text":
                    item["text"] = m.get("text", {}).get("body", "")
                yield item


def _get(url, raw=False):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + env("WHATSAPP_TOKEN")})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    return data if raw else json.loads(data)


def download(media_id):
    """Returns (bytes, mime). Meta hands out a short-lived url that also needs the token."""
    info = _get(f"{GRAPH}/{media_id}")
    return _get(info["url"], raw=True), info.get("mime_type", "")


def send_text(to, body):
    if not configured():
        return False
    data = json.dumps({"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text",
                       "text": {"body": body}}).encode()
    req = urllib.request.Request(f"{GRAPH}/{env('WHATSAPP_PHONE_NUMBER_ID')}/messages", data=data,
                                 headers={"Authorization": "Bearer " + env("WHATSAPP_TOKEN"),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception:
        return False


def normalise(number):
    digits = re.sub(r"\D", "", number or "")
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "31" + digits[1:]
    return "+" + digits if digits else ""


def mask(number):
    """+31612345678 -> +31 6 •• •• 56 78, so the audit log does not spread full numbers."""
    d = normalise(number)
    return f"{d[:3]} {d[3:4]} •• •• {d[-4:-2]} {d[-2:]}" if len(d) >= 10 else d
