"""Loads KEY=value lines from .env next to this file into the environment (existing variables win)."""
import os
from pathlib import Path

_f = Path(os.environ.get("SCAN_ENV_FILE", Path(__file__).with_name(".env")))
if _f.exists():
    for line in _f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
