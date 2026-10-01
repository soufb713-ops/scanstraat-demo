#!/usr/bin/env bash
# Runs both test files, each in its own process (each sets its own AI mode before importing the app).
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
"$PY" -m pytest -q -p no:cacheprovider tests/test_app.py && "$PY" -m pytest -q -p no:cacheprovider tests/test_hybrid.py
