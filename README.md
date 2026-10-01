# Scanstraat, privacy version (`demo-privacy/`, 1 Oct 2026)

The Scanstraat scan demo (paper pile → QR cover sheets → split → read → check → approve → MijnKantoor / Exact / Excel), rebuilt so that privacy by design (AVG) and the Wwft human gate are enforced in code, not promised in a pitch. Fictional data only (Studio Lindewerf B.V.). The older versions (`demo/`, `demo-scan/`, backups) are untouched.

## The requirements and where they live

| Requirement | How it is done | File | Tested |
|---|---|---|---|
| Local-first AI | Default `SCAN_AI=local`: splitting, reading and OCR run on this machine via Ollama. Nothing goes to any AI service. | `ai.py` | Yes, with a stand-in Ollama |
| Apache 2.0 models only | Hardcoded allowlist (`qwen2.5vl:7b`, `qwen2.5:7b`) plus a check of the licence text Ollama ships with the model. `qwen2.5:3b` is blocked by name. | `models.py` | Yes |
| Claude only with pseudonymised text | `SCAN_AI=hybrid`: text is read locally (Tesseract or the local model); IBAN, e-mail, phone, BSN and date of birth are replaced by rules, names and home addresses by the local model; then it is sent as text. Real values are put back locally. | `privacy.py`, `ai.py` | Yes (a stand-in Claude server records what arrives) |
| Hard block: no images, BSN or special category data to the US | One exit (`egress.py`), the only file that imports the Anthropic SDK. It accepts strings only and re-checks every string; any hit blocks and is logged. Documents with health, union, religion, politics or criminal indicators never leave, not even anonymised: the local model reads them. The old "page images to Claude" mode stops the app if set. | `egress.py` | Yes |
| No automatic outbound messages (Wwft art. 23) | "Ask the client" creates a draft only. `REQUIRES_STAFF_APPROVAL = True` is a constant. Sending needs a logged-in person and two ticked statements; a Wwft-stop per client blocks sending entirely. No scheduler or webhook can reach the transport function. | `outbox.py`, Berichten page | Yes |
| 2FA for all firm staff | TOTP (any authenticator app), set up at first login, no way to switch it off. Password alone gives no access. A code can't be used twice; lockout after 5 failures. | `auth.py` | Yes |
| Encryption at rest | AES-256-GCM on every stored file: scans, page images, PDFs, extracted fields, pseudonymisation mappings, users (incl. 2FA secrets), messages, audit log. Key outside the data folder. | `vault.py` | Yes |
| EU-only hosting and backups | With `APP_ENV=production` the app refuses to start unless server and backups are Hetzner `fsn1`/`nbg1` (Germany; Hetzner has no Frankfurt site). Encrypted backup script with its own key. | `region.py`, `backup.py` | Region check yes; real server no |
| Delete originals after export | Right after an export is confirmed (sync folder or Exact API: automatic; ZIP or import file: a person clicks "Import gelukt"), the original, page images and mapping are deleted. Safety net every 5 minutes in the app, and `retention.py` for cron. Rejected documents go after 24 hours. | `app.py` (retention), `retention.py` | Yes |

Screens are in `screens/`: 2FA setup, the pseudonymised text shown in the review screen, a health invoice kept local, a draft awaiting approval, confirm-then-delete, the privacy page, phone view.

## Where the brief was legally imprecise (short; not legal advice, check with the lawyer)

- **Model licence.** The brief named `qwen2.5:3b`. Its Hugging Face card says *Qwen Research License* (non-commercial), so it is blocked and replaced by `qwen2.5:7b` (Apache 2.0). `qwen2.5vl:7b` is Apache 2.0. Checked 1 Oct 2026; re-check before the pilot.
- **Pseudonymised data is still personal data** (AVG recital 26). Sending it to Anthropic is still a transfer to the US: Anthropic must be in the DPA as sub-processor with a transfer basis (EU-US Data Privacy Framework if Anthropic is certified, otherwise SCCs). The design lowers the risk; it does not take Claude outside the AVG.
- **Name detection is only as good as the local model.** The gate catches structured identifiers every time; a person's name the model misses would still go out in hybrid mode. If that is unacceptable for a firm, run `local`.
- **A BV does not simply "shield you from AWR penalties".** Tax fines hit the taxpayer (the firm's client), not you. Your realistic exposure is a damages claim from the firm, which a BV does limit to the company's assets. But someone who knowingly helps can be fined personally as medepleger or medeplichtige (art. 5:1 Awb, art. 67o AWR), and directors stay personally liable for serious personal fault (art. 2:9 BW, 6:162 BW) and in bankruptcy (art. 2:248 BW).
- **Liability cap.** Between businesses a cap at 12 months' fees with indirect damage excluded is common, but it does not hold for intent or deliberate recklessness, and a court can set it aside as unreasonable (art. 6:248 lid 2 BW). Insurance still matters.
- **Deleting after export** fits AVG art. 5(1)(e) for *your* copy. The 7-year fiscal retention (art. 52 AWR) is the client's and the firm's duty, met in MijnKantoor/Exact. Backups made before a deletion still hold the document until they expire (`BACKUP_KEEP_DAYS`, default 7). Tell the firm.
- **Wwft.** The approval gate prevents an *automatic* tip-off. Whether your run-service makes you a Wwft obliged entity is still open (see `legal/`).

## Run it (development, your own computer)

```
bash setup.sh          # local AI: installs Ollama, downloads qwen2.5vl:7b + qwen2.5:7b (about 11 GB), creates keys, starts
bash setup.sh hybrid   # same, and asks for your Anthropic API key (typed in the terminal, hidden, never in a chat)
bash setup.sh demo     # no AI, replays the sample scan; quickest way to see the screens
```

Open http://localhost:5000, log in, scan the QR code with an authenticator app. Next time: `bash start.sh`. Docker for development: `deploy/docker-compose.dev.yml` (app + Ollama, listens on 127.0.0.1 only). Keys: `.vault-key` and `.backup-key`, mode 600. Lose the vault key and the data is gone; keep a copy apart from the backups. Windows: use WSL.

Tests: `bash tests/run.sh` (22 tests). `tests/standins.py` starts a fake Ollama and a fake Anthropic server that records every request, for trying hybrid mode without a GPU or key.

## What was tested and what was not

Tested in the build container: all 22 tests pass. The full app ran in hybrid mode with real Tesseract OCR on the 16-page sample scan, a stand-in Ollama, and a stand-in Anthropic endpoint reached through the real Anthropic SDK. The 11 recorded requests contained only text: no image, no IBAN. A photographed physiotherapy invoice stayed local.

**Not tested:** the real `qwen2.5vl:7b` and `qwen2.5:7b` models (reading quality and speed on a laptop CPU), a real Claude call, real SMTP or WhatsApp sending, the Docker build, a production server, restoring a backup on another machine, Exact and MijnKantoor against the real services.

## Before production (held back on purpose)

The production deployment (Hetzner server in `fsn1`, https, firewall, nightly encrypted backup to a Storage Box in Germany, cron for retention and backups) is not written until you confirm:

1. A finished **verwerkersovereenkomst** naming every sub-processor: Anthropic (only in hybrid mode), Hetzner, Meta (only with WhatsApp), your mail provider.
2. **Algemene voorwaarden** with a liability cap at the last 12 months' fees, indirect damage excluded.
3. The operating entity is a **BV** (or you consciously start as eenmanszaak with insurance).

Also before real client data: a security test, a breach procedure, and the Wwft question in `legal/` answered by a lawyer.

## Files

`app.py` pages and pipeline · `ai.py` local/hybrid/replay AI · `models.py` licence gate · `privacy.py` OCR, special category screen, pseudonymiser · `egress.py` the only exit to Claude · `vault.py` encryption at rest · `outbox.py` message drafts and approval · `auth.py` accounts and 2FA · `region.py` EU lock · `backup.py`, `retention.py` cron jobs · `scan.py`, `checks.py`, `exports.py`, `exact_api.py`, `coversheet.py`, `whatsapp.py` from earlier versions, adapted to the vault · `tests/`.
