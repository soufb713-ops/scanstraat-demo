#!/usr/bin/env bash
# Development setup on your own computer (Linux, macOS or Windows WSL). Then it starts the app.
#   bash setup.sh          local AI (Ollama, Apache 2.0 models): no document or text leaves this computer
#   bash setup.sh hybrid   same, plus Claude for reading: only pseudonymised text goes out, never a scan
#   bash setup.sh demo     no AI at all, replays the sample scan (fast, for showing the screens)
# Production setup (EU server, backups, https) is not here yet: see README, "Before production".
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:-local}"
case "$MODE" in local|hybrid|demo) ;; *) echo "Gebruik: bash setup.sh [local|hybrid|demo]"; exit 1;; esac

command -v python3 >/dev/null || { echo "Python 3.10 of nieuwer is nodig (python.org)."; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10 of nieuwer is nodig."; exit 1; }

if [ "$MODE" != demo ]; then
  command -v ollama >/dev/null || { echo "Ollama installeren (lokale AI)..."; curl -fsSL https://ollama.com/install.sh | sh; }
  (ollama list >/dev/null 2>&1) || (ollama serve >/dev/null 2>&1 &) ; sleep 3
  for m in qwen2.5vl:7b qwen2.5:7b; do
    echo "Model $m downloaden (eenmalig, ca. 5-6 GB, Apache 2.0)..."; ollama pull "$m"
  done
  if ! command -v tesseract >/dev/null; then
    echo "Tip: installeer Tesseract voor snellere tekstherkenning (sudo apt install tesseract-ocr tesseract-ocr-nld"
    echo "     of brew install tesseract tesseract-lang). Zonder Tesseract leest het lokale model de tekst."
  fi
fi

if [ ! -d .venv ]; then
  echo "Programma's installeren (eenmalig, 1 à 2 minuten)..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

umask 077
for k in .vault-key .backup-key; do
  [ -f "$k" ] || { python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())' > "$k"; echo "  Sleutel $k aangemaakt. Bewaar een kopie op een veilige plek, los van de back-ups."; }
done

if [ ! -f .env ]; then
  echo; echo "== Eerste beheerder =="
  read -rp "  Je naam: " name
  read -rp "  Je e-mailadres (om in te loggen): " email
  while true; do
    read -rsp "  Kies een wachtwoord (min. 10 tekens): " pw; echo
    [ ${#pw} -ge 10 ] && break; echo "  Te kort."
  done
  key=""
  if [ "$MODE" = hybrid ]; then
    echo; echo "  Typ hier je Anthropic API-sleutel (wordt niet getoond; nooit in een chat plakken):"
    read -rsp "  API-sleutel: " key; echo
  fi
  ai=$([ "$MODE" = demo ] && echo replay || echo "$MODE")
  NAME="$name" EMAIL="$email" PW="$pw" KEY="$key" AI="$ai" python3 - <<'PY'
import os, re
text = open(".env.example", encoding="utf-8").read()
for k, env in (("ADMIN_NAME", "NAME"), ("ADMIN_EMAIL", "EMAIL"), ("ADMIN_PASSWORD", "PW"), ("ANTHROPIC_API_KEY", "KEY"), ("SCAN_AI", "AI")):
    text = re.sub(rf"^{k}=.*$", lambda m: f"{k}={os.environ[env]}", text, flags=re.M)
open(".env", "w", encoding="utf-8").write(text)
PY
  echo "  Opgeslagen in .env (alleen leesbaar voor jou)."
fi

echo
echo "Vangnet voor het automatisch wissen en de dagelijkse versleutelde back-up (optioneel, via crontab -e):"
echo "  */15 * * * * cd $PWD && .venv/bin/python retention.py"
echo "  30 2 * * *   cd $PWD && .venv/bin/python backup.py"
echo "Bij de eerste login stel je tweestapsverificatie in met een authenticator-app op je telefoon."
exec bash start.sh
