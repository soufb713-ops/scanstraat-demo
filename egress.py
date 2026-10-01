"""The only door to a US AI service. Nothing else in this app imports the Anthropic SDK (a test checks this).

send() accepts plain strings only, so a page image cannot be passed in. Before anything leaves, every string
(system prompt, reference data, document text) is checked again, independently of the pseudonymiser:
  - no IBAN, e-mail address, phone number, date of birth or 11-proof 9-digit number (possible BSN);
  - no indicator of special category data (health, union, religion, politics, criminal);
  - only text; size limit.
If any check fails, nothing is sent and EgressBlocked is raised. Every attempt, sent or blocked, is written
to the egress log (sealed) with a SHA-256 of the exact payload, so the firm can see what left and when.
"""
import hashlib
import json
import os
from datetime import datetime

import privacy

MODEL = os.environ.get("SCAN_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("SCAN_EFFORT", "low")
MAX_PAYLOAD = 200_000
_log = None  # set by app: callable(record)


class EgressBlocked(Exception):
    pass


class EgressError(Exception):
    pass


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def inspect(text):
    """Returns a list of reasons this text may not leave. Empty list = allowed."""
    if not isinstance(text, str):
        return [f"geen tekst maar {type(text).__name__}: alleen tekst mag naar buiten"]
    reasons = []
    pii = privacy.rule_findings(text, strict=True)
    if pii:
        kinds = sorted({k for k, _ in pii})
        reasons.append("onvervangen persoonsgegevens: " + ", ".join(kinds))
    special = privacy.special_categories(text)
    if special:
        reasons.append("mogelijk bijzondere persoonsgegevens: " + ", ".join(f"{c} ({', '.join(t)})" for c, t in special.items()))
    return reasons


def check_payload(parts):
    if not parts or not all(isinstance(p, str) for p in parts):
        raise EgressBlocked("Alleen tekst mag naar een externe AI: afbeeldingen en bestanden zijn geblokkeerd")
    if sum(len(p) for p in parts) > MAX_PAYLOAD:
        raise EgressBlocked("Te veel tekst voor één verzoek")
    reasons = []
    for p in parts:
        reasons += inspect(p)
    if reasons:
        raise EgressBlocked("; ".join(sorted(set(reasons))))


def send(purpose, system, parts, schema, ref="", doc=None):
    """purpose: 'split' or 'read'. parts: list of str. Returns (data, usage, record)."""
    all_parts = [system] + ([ref] if ref else []) + list(parts)
    digest = hashlib.sha256("\x1e".join(p if isinstance(p, str) else repr(p) for p in all_parts).encode()).hexdigest()
    record = {"at": datetime.now().isoformat(timespec="seconds"), "purpose": purpose, "doc": doc,
              "destination": "Anthropic API (VS)", "model": MODEL, "sha256": digest,
              "chars": sum(len(p) for p in all_parts if isinstance(p, str))}
    try:
        check_payload(all_parts)
    except EgressBlocked as e:
        _record(record | {"result": "blocked", "reason": str(e)})
        raise
    if not configured():
        _record(record | {"result": "blocked", "reason": "geen API-sleutel"})
        raise EgressError("Geen ANTHROPIC_API_KEY ingesteld")
    data, usage = _call(system, ref, parts, schema)
    _record(record | {"result": "sent", "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]})
    return data, usage, record


def _record(rec):
    if _log:
        _log(rec)


def _call(system, ref, parts, schema):
    import anthropic  # only here
    sys_blocks = [{"type": "text", "text": system}]
    if ref:
        sys_blocks.append({"type": "text", "text": ref, "cache_control": {"type": "ephemeral"}})
    content = [{"type": "text", "text": p} for p in parts]
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            system=sys_blocks,
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as e:
        raise EgressError("API-sleutel ongeldig") from e
    except anthropic.RateLimitError as e:
        raise EgressError("Te veel verzoeken tegelijk, probeer opnieuw") from e
    except anthropic.APIStatusError as e:
        raise EgressError(f"API-fout {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise EgressError("Geen verbinding met de API") from e
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise EgressError("Geen bruikbaar antwoord van het model")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise EgressError("Leeg antwoord")
    return json.loads(text), {"model": resp.model, "input_tokens": resp.usage.input_tokens,
                              "output_tokens": resp.usage.output_tokens}
