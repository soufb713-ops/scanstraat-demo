"""Encryption at rest for everything the app stores (AVG art. 32).

Every file the app writes under DATA (scans, page images, document PDFs, state with extracted fields,
pseudonymisation mappings, users with 2FA secrets, outbox, audit log lines) goes through this module and
is stored as AES-256-GCM ciphertext. The file's path relative to DATA is bound in as associated data, so
an encrypted file cannot be swapped for another one without the decryption failing.

The key comes from VAULT_KEY (base64, 32 bytes) or from VAULT_KEY_FILE (default: .vault-key next to the app,
outside the data folder, mode 600). setup.sh creates it. Losing the key means losing the data; keep a copy
of the key apart from the backups.

What this protects against: a stolen disk, a copied data folder, a leaked backup. What it does not protect
against: someone who controls the running server, because the key is on that server.
"""
import base64
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"VLT1"
HERE = Path(__file__).parent


class VaultError(Exception):
    pass


def _load_key():
    raw = os.environ.get("VAULT_KEY", "").strip()
    if raw:
        key = base64.b64decode(raw)
    else:
        f = Path(os.environ.get("VAULT_KEY_FILE", HERE / ".vault-key"))
        if not f.exists():
            f.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
            os.chmod(f, 0o600)
            print(f"\n  Nieuwe kluissleutel aangemaakt: {f}  (bewaar een kopie los van de back-ups)\n")
        key = base64.b64decode(f.read_text().strip())
    if len(key) != 32:
        raise VaultError("VAULT_KEY moet 32 bytes zijn (base64)")
    return key


_aead = None
_root = None


def init(data_root: Path):
    global _aead, _root
    _aead = AESGCM(_load_key())
    _root = Path(data_root).resolve()


def _aad(path: Path):
    try:
        return str(Path(path).resolve().relative_to(_root)).encode()
    except ValueError:
        raise VaultError(f"{path} ligt buiten de datamap") from None


def seal(data: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + _aead.encrypt(nonce, data, aad)


def unseal(blob: bytes, aad: bytes) -> bytes:
    if blob[:4] != MAGIC:
        raise VaultError("Bestand is niet versleuteld (verwacht kluisformaat)")
    try:
        return _aead.decrypt(blob[4:16], blob[16:], aad)
    except Exception as e:  # InvalidTag: wrong key, tampered file or swapped path
        raise VaultError("Ontsleutelen mislukt (verkeerde sleutel of gewijzigd bestand)") from e


def write(path: Path, data: bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(seal(data, _aad(path)))
    tmp.replace(path)


def read(path: Path) -> bytes:
    path = Path(path)
    return unseal(path.read_bytes(), _aad(path))


def write_json(path, obj):
    write(path, json.dumps(obj, ensure_ascii=False, default=str).encode())


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(read(path))


# Append-only log: one sealed record per line (base64), bound to the log's path.
def append_line(path: Path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = base64.b64encode(seal(json.dumps(obj, ensure_ascii=False, default=str).encode(), _aad(path))).decode()
    with path.open("a", encoding="ascii") as fh:
        fh.write(line + "\n")


def read_lines(path: Path):
    path = Path(path)
    if not path.exists():
        return []
    aad = _aad(path)
    return [json.loads(unseal(base64.b64decode(l), aad)) for l in path.read_text(encoding="ascii").splitlines() if l.strip()]


def delete(path: Path):
    """Remove a sealed file. With encryption at rest, the ciphertext left on an SSD is unreadable without the key."""
    path = Path(path)
    if path.exists():
        path.unlink()
        return True
    return False
