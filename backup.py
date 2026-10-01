"""Encrypted backup of the data folder.

    python backup.py            -> writes backups/data-YYYYmmdd-HHMM.tar.vlt and removes copies older than
                                   BACKUP_KEEP_DAYS (default 7)

The data folder is already encrypted file by file (vault.py). The archive is encrypted once more with a separate
BACKUP_KEY, so a leaked backup plus a leaked vault key is still not enough. Copying the archive to the
Storage Box in Germany is done by the production deployment (not included yet; see README).

Retention note: a document deleted after export still exists in backups made before the deletion, until those
backups age out after BACKUP_KEEP_DAYS. Tell the firm this number.
"""
import base64
import io
import os
import secrets
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import envfile  # noqa: F401

HERE = Path(__file__).parent
DATA = Path(os.environ.get("SCAN_DATA", HERE / "data"))
OUT = Path(os.environ.get("BACKUP_DIR", HERE / "backups"))
KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "7"))


def _key():
    raw = os.environ.get("BACKUP_KEY", "").strip()
    if raw:
        return base64.b64decode(raw)
    f = Path(os.environ.get("BACKUP_KEY_FILE", HERE / ".backup-key"))
    if not f.exists():
        f.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
        os.chmod(f, 0o600)
    return base64.b64decode(f.read_text().strip())


def make():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(DATA, arcname="data")
    nonce = secrets.token_bytes(12)
    name = f"data-{datetime.now():%Y%m%d-%H%M}.tar.vlt"
    blob = b"BKP1" + nonce + AESGCM(_key()).encrypt(nonce, buf.getvalue(), name.encode())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_bytes(blob)
    removed = 0
    for f in OUT.glob("data-*.tar.vlt"):
        if time.time() - f.stat().st_mtime > KEEP_DAYS * 86400:
            f.unlink()
            removed += 1
    return OUT / name, removed


def restore(path, target):
    blob = Path(path).read_bytes()
    data = AESGCM(_key()).decrypt(blob[4:16], blob[16:], Path(path).name.encode())
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(target, filter="data")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "restore":
        restore(sys.argv[2], HERE)
        print("Teruggezet in", HERE / "data")
    else:
        path, removed = make()
        print(f"Back-up: {path} ({path.stat().st_size // 1024} KB); {removed} oude verwijderd")
