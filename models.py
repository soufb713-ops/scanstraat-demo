"""Licence gate for local models: only weights under Apache 2.0 may run.

Checked on 1 Oct 2026 on the Hugging Face model cards of the upstream weights:
  Qwen/Qwen2.5-7B-Instruct       license: apache-2.0      -> allowed (anonymiser, text)
  Qwen/Qwen2.5-VL-7B-Instruct    license: apache-2.0      -> allowed (vision: split, read, transcribe)
  Qwen/Qwen2.5-3B-Instruct       license: qwen-research   -> BLOCKED (non-commercial research licence)
The 3B and 72B sizes of Qwen2.5 and Qwen2.5-VL do not ship under Apache 2.0, so they are blocked by name.

Two checks before a model is used:
1. its name is on ALLOWED (a hardcoded allowlist, not a setting);
2. the licence text Ollama ships with the model (POST /api/show) is read: if it is there and is not
   Apache 2.0, the model is refused. If Ollama ships no licence text, the allowlist decides and the
   security page says so.
"""
import json
import os
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

ALLOWED = {
    "qwen2.5:7b": {"upstream": "Qwen/Qwen2.5-7B-Instruct", "licence": "Apache-2.0",
                   "card": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct"},
    "qwen2.5vl:7b": {"upstream": "Qwen/Qwen2.5-VL-7B-Instruct", "licence": "Apache-2.0",
                     "card": "https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct"},
}
BLOCKED = {
    "qwen2.5:3b": "Qwen Research License (niet-commercieel)",
    "qwen2.5vl:3b": "geen Apache 2.0",
    "qwen2.5:72b": "Qwen License (geen Apache 2.0)",
    "qwen2.5vl:72b": "Qwen License (geen Apache 2.0)",
}

VISION_MODEL = os.environ.get("LOCAL_MODEL", "qwen2.5vl:7b")
ANON_MODEL = os.environ.get("ANON_MODEL", "qwen2.5:7b")


class LicenceError(Exception):
    pass


def _canon(name):
    name = (name or "").strip().lower()
    return name if ":" in name else name + ":latest"


def check(name):
    """Returns a status dict. ok=False means the model must not run."""
    n = _canon(name)
    if n in BLOCKED:
        return {"model": n, "ok": False, "why": f"Geblokkeerd: {BLOCKED[n]}"}
    if n not in ALLOWED:
        return {"model": n, "ok": False, "why": "Niet op de lijst met Apache 2.0-modellen"}
    info = {"model": n, "ok": True, "upstream": ALLOWED[n]["upstream"], "card": ALLOWED[n]["card"]}
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/show", data=json.dumps({"model": n}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            shown = json.loads(r.read())
    except Exception:
        info.update(installed=False, why="Apache 2.0 volgens modelkaart; Ollama niet bereikbaar of model niet gedownload")
        return info
    text = shown.get("license") or ""
    text = "\n".join(text) if isinstance(text, list) else str(text)
    info["installed"] = True
    if not text.strip():
        info["why"] = "Apache 2.0 volgens modelkaart (Ollama levert geen licentietekst mee)"
    elif "apache license" in text.lower() and "version 2.0" in text.lower():
        info["why"] = "Apache 2.0 (licentietekst in het model gecontroleerd)"
    else:
        info.update(ok=False, why="Licentietekst in het model is geen Apache 2.0: geweigerd")
    return info


_cache = {}


def require(name):
    """Raise LicenceError unless the model may run. Cached per model for the life of the process."""
    n = _canon(name)
    if n not in _cache or not _cache[n].get("installed"):
        _cache[n] = check(n)
    if not _cache[n]["ok"]:
        raise LicenceError(f"Model {n} niet toegestaan: {_cache[n]['why']}")
    return n


def status():
    return [check(VISION_MODEL), check(ANON_MODEL)]
