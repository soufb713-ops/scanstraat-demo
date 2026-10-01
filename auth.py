"""Accounts, login with mandatory two-factor authentication, CSRF and security headers.

Personal accounts (e-mail + password hash), two roles, forced password change on first login, lockout after
repeated failures. Two-factor login (TOTP, RFC 6238: any authenticator app) is required for every account
and cannot be switched off (REQUIRE_2FA is a constant). Stored sealed in data/users.json (see vault.py).
"""
import base64
import hashlib
import hmac
import struct
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import vault

ROLES = {"admin": "Beheerder", "reviewer": "Medewerker"}
MIN_PASSWORD = 10
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
REQUIRE_2FA = True
PENDING_SECONDS = 5 * 60
ISSUER = os.environ.get("BRAND_NAME", "Scanstraat")

_lock = threading.Lock()
_fails = {}  # email -> [timestamps]


class Users:
    def __init__(self, path: Path):
        self.path = path

    def all(self):
        return vault.read_json(self.path, {})

    def get(self, email):
        return self.all().get((email or "").strip().lower())

    def save(self, users):
        vault.write_json(self.path, users)

    def add(self, email, name, role, password, must_change=True):
        email = email.strip().lower()
        with _lock:
            users = self.all()
            users[email] = {"name": name.strip(), "role": role, "hash": generate_password_hash(password),
                            "must_change": must_change, "created": datetime.now().isoformat(timespec="seconds"),
                            "last_login": None, "active": True, "totp_secret": None, "totp_last": 0}
            self.save(users)

    def update(self, email, **fields):
        with _lock:
            users = self.all()
            if email in users:
                if "password" in fields:
                    users[email]["hash"] = generate_password_hash(fields.pop("password"))
                users[email].update(fields)
                self.save(users)


def temp_password():
    return "-".join(secrets.token_urlsafe(4) for _ in range(3))


# ---------------------------------------------------------------- TOTP (RFC 6238), standard library only
def totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_verify(secret, code, last_step=0, now=None):
    """Returns the matched time step, or None. Accepts one step of clock drift; a code is never accepted twice."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    step = int((now or time.time()) // 30)
    for s in (step - 1, step, step + 1):
        if s > (last_step or 0) and hmac.compare_digest(_hotp(secret, s), code):
            return s
    return None


def otpauth_uri(secret, email):
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(ISSUER)}:{quote(email)}?secret={secret}&issuer={quote(ISSUER)}"
            "&algorithm=SHA1&digits=6&period=30")


def qr_svg(text):
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return img.to_string(encoding="unicode")


def locked(email):
    now = time.time()
    recent = [t for t in _fails.get(email, []) if now - t < LOCK_SECONDS]
    _fails[email] = recent
    return len(recent) >= MAX_FAILS


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def current_user():
    return session.get("user")


def admin_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if (current_user() or {}).get("role") != "admin":
            abort(403)
        return view(*a, **kw)
    return wrapper


def init(app, users: Users, audit):
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES") == "1",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8))

    # First start: create the owner's account. On a server ADMIN_EMAIL / ADMIN_PASSWORD come from the host's
    # secret settings; locally a random password is printed once in the terminal.
    if not users.all():
        email = os.environ.get("ADMIN_EMAIL", "admin@demo.local")
        password = os.environ.get("ADMIN_PASSWORD") or temp_password()
        users.add(email, os.environ.get("ADMIN_NAME", "Beheerder"), "admin", password,
                  must_change=not os.environ.get("ADMIN_PASSWORD"))
        if not os.environ.get("ADMIN_PASSWORD"):
            print(f"\n  Eerste beheerder aangemaakt: {email}  wachtwoord: {password}\n")

    open_endpoints = {"login", "login_code", "setup_2fa", "static", "health", "whatsapp_webhook", "retention_trigger"}

    @app.before_request
    def guard():
        if request.method == "POST" and request.endpoint not in ("static", "whatsapp_webhook", "retention_trigger"):
            sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
            if not session.get("csrf") or not hmac.compare_digest(sent, session["csrf"]):
                abort(400, "Formulier verlopen. Ververs de pagina en probeer opnieuw.")
        if request.endpoint in open_endpoints:
            return None
        user = current_user()
        if not user:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("login", next=request.path))
        stored = users.get(user["email"])
        if not stored or not stored.get("active", True):
            session.clear()
            return redirect(url_for("login"))
        if stored.get("must_change") and request.endpoint not in ("change_password", "logout"):
            return redirect(url_for("change_password"))
        return None

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if current_user():
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.get("/health")
    def health():
        return "ok"

    def pending():
        p = session.get("pending")
        if not p or time.time() - p["at"] > PENDING_SECONDS:
            session.pop("pending", None)
            return None
        return p

    def finish_login(email, stored):
        _fails.pop(email, None)
        nxt = (session.get("pending") or {}).get("next", "")
        session.clear()
        session.permanent = True
        session["user"] = {"email": email, "name": stored["name"], "role": stored["role"]}
        users.update(email, last_login=datetime.now().isoformat(timespec="seconds"))
        audit("-", "-", "login", stored["name"], ip=request.remote_addr, second_factor="TOTP")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        email = request.form.get("email", "").strip().lower()
        if request.method == "POST":
            stored = users.get(email)
            if locked(email):
                error = "Te veel mislukte pogingen. Probeer het over 15 minuten opnieuw."
            elif not stored or not stored.get("active", True) or not check_password_hash(stored["hash"], request.form.get("password", "")):
                _fails.setdefault(email, []).append(time.time())
                audit("-", "-", "login_failed", email or "?", ip=request.remote_addr)
                error = "E-mailadres of wachtwoord klopt niet."
            else:
                # Password is step one of two. No access until the second factor is checked.
                session.clear()
                session["pending"] = {"email": email, "at": time.time(), "next": request.args.get("next", "")}
                return redirect(url_for("login_code" if stored.get("totp_secret") else "setup_2fa"))
        return render_template("login.html", error=error, email=email)

    @app.route("/login/code", methods=["GET", "POST"])
    def login_code():
        p = pending()
        if not p:
            return redirect(url_for("login"))
        email = p["email"]
        stored = users.get(email)
        if not stored or not stored.get("totp_secret"):
            return redirect(url_for("setup_2fa"))
        error = None
        if request.method == "POST":
            step = None if locked(email) else totp_verify(stored["totp_secret"], request.form.get("code"), stored.get("totp_last"))
            if step is None:
                _fails.setdefault(email, []).append(time.time())
                audit("-", "-", "login_failed", email, ip=request.remote_addr, step="2fa")
                error = ("Te veel mislukte pogingen. Probeer het over 15 minuten opnieuw." if locked(email)
                         else "Code klopt niet of is al gebruikt. Wacht op de volgende code.")
            else:
                users.update(email, totp_last=step)
                return finish_login(email, stored)
        return render_template("twofactor.html", mode="code", error=error)

    @app.route("/login/2fa-instellen", methods=["GET", "POST"])
    def setup_2fa():
        p = pending()
        if not p:
            return redirect(url_for("login"))
        email = p["email"]
        stored = users.get(email)
        if stored.get("totp_secret"):
            return redirect(url_for("login_code"))
        secret = session.get("new_totp") or totp_secret()
        session["new_totp"] = secret
        error = None
        if request.method == "POST":
            step = None if locked(email) else totp_verify(secret, request.form.get("code"))
            if step is None:
                _fails.setdefault(email, []).append(time.time())
                error = "Code klopt niet. Kijk of de tijd op je telefoon klopt en probeer de nieuwste code."
            else:
                users.update(email, totp_secret=secret, totp_last=step, totp_since=datetime.now().isoformat(timespec="seconds"))
                audit("-", "-", "2fa_enrolled", stored["name"])
                return finish_login(email, stored)
        uri = otpauth_uri(secret, email)
        return render_template("twofactor.html", mode="setup", error=error, qr=qr_svg(uri),
                               secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))

    @app.post("/logout")
    def logout():
        if current_user():
            audit("-", "-", "logout", current_user()["name"])
        session.clear()
        return redirect(url_for("login"))

    @app.route("/account/wachtwoord", methods=["GET", "POST"])
    def change_password():
        error = None
        user = current_user()
        stored = users.get(user["email"])
        if request.method == "POST":
            new, repeat = request.form.get("new", ""), request.form.get("repeat", "")
            if not check_password_hash(stored["hash"], request.form.get("current", "")):
                error = "Huidig wachtwoord klopt niet."
            elif len(new) < MIN_PASSWORD:
                error = f"Kies minstens {MIN_PASSWORD} tekens."
            elif new != repeat:
                error = "De twee nieuwe wachtwoorden zijn niet gelijk."
            elif check_password_hash(stored["hash"], new):
                error = "Kies een ander wachtwoord dan het huidige."
            else:
                users.update(user["email"], password=new, must_change=False)
                audit("-", "-", "password_changed", user["name"])
                flash("Wachtwoord gewijzigd.")
                return redirect(url_for("dashboard"))
        return render_template("password.html", error=error, forced=stored.get("must_change"))

    @app.route("/gebruikers", methods=["GET", "POST"])
    @admin_required
    def users_page():
        created = None
        error = None
        if request.method == "POST":
            action = request.form.get("action")
            email = request.form.get("email", "").strip().lower()
            if action == "add":
                name, role = request.form.get("name", "").strip(), request.form.get("role", "reviewer")
                if "@" not in email or not name or role not in ROLES:
                    error = "Vul naam, e-mailadres en rol in."
                elif users.get(email):
                    error = "Dit e-mailadres heeft al een account."
                else:
                    pw = temp_password()
                    users.add(email, name, role, pw)
                    created = {"email": email, "password": pw, "name": name}
                    audit("-", "-", "user_added", current_user()["name"], email=email, role=role)
            elif action in ("reset", "toggle", "reset2fa") and users.get(email) and email != current_user()["email"]:
                if action == "reset2fa":
                    users.update(email, totp_secret=None, totp_last=0)
                    audit("-", "-", "2fa_reset", current_user()["name"], email=email)
                elif action == "reset":
                    pw = temp_password()
                    users.update(email, password=pw, must_change=True)
                    created = {"email": email, "password": pw, "name": users.get(email)["name"]}
                    audit("-", "-", "password_reset", current_user()["name"], email=email)
                else:
                    active = not users.get(email).get("active", True)
                    users.update(email, active=active)
                    audit("-", "-", "user_enabled" if active else "user_disabled", current_user()["name"], email=email)
        return render_template("users.html", users=users.all(), roles=ROLES, created=created, error=error)
