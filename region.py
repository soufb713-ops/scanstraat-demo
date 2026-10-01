"""EU-only hosting and backups, fixed in code.

The production server and its encrypted backups must both sit in Germany with Hetzner Online GmbH (a German
company, so no US parent that a US authority can compel). The locations are constants, not settings:
  server:  Hetzner Cloud, location fsn1 (Falkenstein) or nbg1 (Nuremberg)
  backups: Hetzner Storage Box in the same region
Hetzner has no Frankfurt data centre; Falkenstein and Nuremberg are its German sites.

With APP_ENV=production the app refuses to start unless HOSTING_LOCATION and BACKUP_LOCATION are one of these.
Honest limit: an app cannot see where its server physically stands. This check stops a misconfigured or copied
deployment; the provisioning script (which creates the server in fsn1) is what actually puts it there.
"""
import os

PROVIDER = "Hetzner Online GmbH (DE)"
EU_LOCATIONS = {"fsn1": "Falkenstein, Duitsland", "nbg1": "Neurenberg, Duitsland"}
DEFAULT_LOCATION = "fsn1"


def production():
    return os.environ.get("APP_ENV", "development").lower() == "production"


def enforce():
    if not production():
        return
    host = os.environ.get("HOSTING_LOCATION", "")
    backup = os.environ.get("BACKUP_LOCATION", "")
    bad = [f"{k}={v or '(leeg)'}" for k, v in (("HOSTING_LOCATION", host), ("BACKUP_LOCATION", backup))
           if v not in EU_LOCATIONS]
    if bad:
        raise SystemExit("Productie start alleen op Hetzner in Duitsland (fsn1 of nbg1). Fout: " + ", ".join(bad))


def status():
    if not production():
        return {"ok": True, "env": "ontwikkeling", "text": "Ontwikkelomgeving (deze computer). Productie alleen in "
                + " of ".join(EU_LOCATIONS.values()) + "."}
    host = os.environ.get("HOSTING_LOCATION")
    backup = os.environ.get("BACKUP_LOCATION")
    return {"ok": True, "env": "productie",
            "text": f"Server: {PROVIDER}, {EU_LOCATIONS[host]}. Back-ups: {EU_LOCATIONS[backup]}."}
