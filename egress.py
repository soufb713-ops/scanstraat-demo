"""The only door to an external AI service. Nothing else in this app imports the Anthropic SDK (a test checks this).

Two providers (AI_PROVIDER):
  anthropic  the Anthropic API (processed in the US), needs ANTHROPIC_API_KEY
  bedrock    Claude in Amazon Bedrock in an EU region (AWS_REGION, must start with "eu-"), needs a Bedrock API key
             (AWS_BEARER_TOKEN_BEDROCK) or AWS access keys. Use a regional EU endpoint, not the global one, when
             the firm needs EU-only processing: check the endpoint type in the AWS console.

send() accepts plain strings only, so a page image cannot be passed in. Before anything leaves, every string
(system prompt, reference data, document text) is checked again, independently of the pseudonymiser:
  - no IBAN, e-mail address, phone number, date of birth or 11-proof 9-digit number (possible BSN);
  - no indicator of special category data (health, union, religion, politics, criminal);
  - only text; size limit.
If any check fails, nothing is sent and EgressBlocked is raised. Every attempt, sent or blocked, is written
to the egress log (sealed) with a SHA-256 of the exact payload, so the firm can see what left and when.
"""
import base64
import hashlib
import json
import os
from datetime import datetime

import privacy

MODEL = os.environ.get("SCAN_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("SCAN_EFFORT", "low")
MAX_PAYLOAD = 200_000
MAX_IMAGES = 20
_log = None  # set by app: callable(record)

PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").strip().lower()
if PROVIDER not in ("anthropic", "bedrock"):
    raise SystemExit(f"AI_PROVIDER={PROVIDER} is niet toegestaan. Kies anthropic of bedrock.")
REGION = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-1").strip()
if PROVIDER == "bedrock" and not REGION.startswith("eu-"):
    raise SystemExit(f"AI_PROVIDER=bedrock alleen in een EU-regio (eu-...), niet {REGION}.")
DESTINATION = (f"Claude in Amazon Bedrock ({REGION}, EU)" if PROVIDER == "bedrock" else "Anthropic API (VS)")


def model_id():
    if PROVIDER == "bedrock" and not MODEL.startswith("anthropic."):
        return "anthropic." + MODEL
    return MODEL


def images_allowed():
    """Page images may only leave on a demo instance that holds fictional or the presenter's own documents.
    A production instance (APP_ENV=production) never sends an image, whatever else is set."""
    return os.environ.get("DEMO_INSTANCE") == "1" and os.environ.get("APP_ENV", "development").lower() != "production"


class EgressBlocked(Exception):
    pass


class EgressError(Exception):
    pass


def configured():
    if PROVIDER == "bedrock":
        return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
                    or (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")))
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
              "destination": DESTINATION, "model": model_id(), "sha256": digest,
              "chars": sum(len(p) for p in all_parts if isinstance(p, str))}
    try:
        check_payload(all_parts)
    except EgressBlocked as e:
        _record(record | {"result": "blocked", "reason": str(e)})
        raise
    if not configured():
        _record(record | {"result": "blocked", "reason": "geen API-sleutel"})
        raise EgressError(_missing_key())
    data, usage = _call(system, ref, parts, schema)
    _record(record | {"result": "sent", "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]})
    return data, usage, record


def send_images(purpose, system, jpegs, parts, schema, ref="", doc=None):
    """Demo instance only (images_allowed()): page images plus short text instructions. Used to show live reading
    of the presenter's own or fictional documents. Never available with APP_ENV=production."""
    parts = list(parts or [])
    digest = hashlib.sha256(b"".join(jpegs) + "\x1e".join([system, ref] + parts).encode()).hexdigest()
    record = {"at": datetime.now().isoformat(timespec="seconds"), "purpose": purpose, "doc": doc,
              "destination": DESTINATION, "model": model_id(), "sha256": digest, "images": len(jpegs),
              "chars": sum(len(p) for p in [system, ref] + parts)}
    if not images_allowed():
        _record(record | {"result": "blocked", "reason": "paginabeelden alleen op een demo-omgeving"})
        raise EgressBlocked("Paginabeelden gaan alleen naar buiten op een demo-omgeving zonder klantgegevens")
    if not jpegs or not all(isinstance(j, (bytes, bytearray)) for j in jpegs) or len(jpegs) > MAX_IMAGES:
        _record(record | {"result": "blocked", "reason": "ongeldige of te veel afbeeldingen"})
        raise EgressBlocked(f"Tussen 1 en {MAX_IMAGES} paginabeelden per verzoek")
    if not configured():
        _record(record | {"result": "blocked", "reason": "geen API-sleutel"})
        raise EgressError(_missing_key())
    data, usage = _call(system, ref, parts, schema, images=jpegs)
    _record(record | {"result": "sent", "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]})
    return data, usage, record


def _missing_key():
    if PROVIDER == "bedrock":
        return "Geen Bedrock-sleutel ingesteld (AWS_BEARER_TOKEN_BEDROCK of AWS-toegangssleutels)"
    return "Geen ANTHROPIC_API_KEY ingesteld"


def _record(rec):
    if _log:
        _log(rec)


def _client():
    import anthropic  # only here
    if PROVIDER == "bedrock":
        return anthropic.AnthropicBedrockMantle(aws_region=REGION)
    return anthropic.Anthropic()


def _call(system, ref, parts, schema, images=()):
    import anthropic  # only here
    sys_blocks = [{"type": "text", "text": system}]
    if ref:
        sys_blocks.append({"type": "text", "text": ref, "cache_control": {"type": "ephemeral"}})
    content = [{"type": "text", "text": p} for p in parts]
    for i, jpg in enumerate(images, 1):
        content += [{"type": "text", "text": f"Page {i}:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(jpg).decode()}}]
    request = dict(model=model_id(), max_tokens=16000, system=sys_blocks,
                   output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
                   messages=[{"role": "user", "content": content}])
    client = _client()
    try:
        if PROVIDER == "bedrock":
            # Server-side fallbacks are not available on Bedrock: a refusal is reported below, not retried.
            resp = client.messages.create(**request)
        else:
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
    except anthropic.AuthenticationError as e:
        raise EgressError("API-sleutel ongeldig") from e
    except anthropic.PermissionDeniedError as e:
        raise EgressError("Geen toegang tot dit model (bij Bedrock: modeltoegang aanzetten in deze regio)") from e
    except anthropic.NotFoundError as e:
        raise EgressError(f"Model {model_id()} niet gevonden bij {DESTINATION}") from e
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
