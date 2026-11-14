"""Per-IP limits for the public endpoints (the sandbox checks, the /veiligheid filter, sign-in), and the proxy setting
that makes request.remote_addr the visitor's address instead of the host's load balancer.

In memory and per process: enough for one small instance, and a restart resets the counters. A visitor who hits a
limit gets 429 with Retry-After; nothing about the visitor is stored beyond the timestamps in this window.
"""
import os
import threading
import time
from collections import defaultdict, deque

from urllib.parse import urlsplit

from flask import abort, request
from werkzeug.exceptions import TooManyRequests
from werkzeug.middleware.proxy_fix import ProxyFix

# bucket: (max requests, window in seconds). Generous for a person clicking through the tour, tight for a script.
LIMITS = {
    "filter": (30, 600),      # /api/veiligheid/filter
    "sample": (300, 600),     # /api/voorbeeld/check and /api/voorbeeld/export (the tour posts on every edit)
    "login": (20, 600),       # POST /login and the 2FA step, on top of the per-account lockout in auth.py
}
for _part in filter(None, os.environ.get("RATE_LIMITS", "").split(",")):   # e.g. "login=50/600" (tests use high values)
    _name, _, _val = _part.partition("=")
    _n, _, _w = _val.partition("/")
    LIMITS[_name.strip()] = (int(_n), int(_w))
ENDPOINTS = {"security_filter": "filter", "sample_check": "sample", "sample_export": "sample",
             "login": "login", "login_code": "login"}

_hits = defaultdict(deque)
_lock = threading.Lock()


def proxy_hops():
    """How many proxies in front of the app may set X-Forwarded-For. PROXY_HOPS wins; on Render one proxy is known."""
    v = os.environ.get("PROXY_HOPS")
    if v is not None:
        return max(0, int(v))
    return 1 if os.environ.get("RENDER") else 0


def install(app):
    hops = proxy_hops()
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=0)

    @app.before_request
    def _limit():
        bucket = ENDPOINTS.get(request.endpoint)
        if not bucket or request.method != "POST":
            return None
        origin = request.headers.get("Origin")
        if origin and origin != "null" and urlsplit(origin).netloc != request.host:
            abort(403)       # no cross-site use of these endpoints; the app never sends CORS headers either
        hit(bucket, request.remote_addr or "?")
        return None


def hit(bucket, ip, now=None):
    limit, window = LIMITS[bucket]
    now = time.monotonic() if now is None else now
    with _lock:
        q = _hits[(bucket, ip)]
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            raise TooManyRequests("Te veel verzoeken achter elkaar. Probeer het over een paar minuten opnieuw.",
                                  retry_after=int(window - (now - q[0])) + 1)
        q.append(now)
        if len(_hits) > 50_000:      # memory bound: drop idle keys
            for k in [k for k, v in _hits.items() if not v or now - v[-1] >= LIMITS[k[0]][1]]:
                del _hits[k]


def reset():
    with _lock:
        _hits.clear()
