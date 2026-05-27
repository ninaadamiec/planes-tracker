"""
Flight Alert Tracker
Monitors flights by callsign prefix and sends email when destination is Poland.
"""

import os
import json
import smtplib
import requests
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── Configuration ────────────────────────────────────────────────────────────

# Callsign prefixes to track (CMB = all CMBxxx flights, ADB/VDA/CVK = Antonov operators)
CALLSIGN_PREFIXES = ["CMB", "ADB", "VDA", "CVK"]

# All Polish airports have ICAO code starting with "EP"
POLAND_ICAO_PREFIX = "EP"

# State file – tracks which flights already triggered alerts (committed to repo)
STATE_FILE = "seen_flights.json"

# How many days to keep entries in state file
STATE_RETENTION_DAYS = 7

# Credentials & config from GitHub Actions secrets
OPENSKY_USER = os.environ.get("OPENSKY_USER", "")
OPENSKY_PASS = os.environ.get("OPENSKY_PASS", "")
EMAIL_FROM   = os.environ.get("EMAIL_FROM", "")
EMAIL_TO     = os.environ.get("EMAIL_TO", "")
EMAIL_PASS   = os.environ.get("EMAIL_PASSWORD", "")
SMTP_HOST    = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "465"))

# ─── Polish airports lookup (ICAO → name) ────────────────────────────────────

POLISH_AIRPORTS: dict[str, str] = {
    "EPWA": "Warszawa Chopin",
    "EPMO": "Warszawa Modlin",
    "EPKK": "Kraków Balice",
    "EPGD": "Gdańsk Lech Wałęsa",
    "EPWR": "Wrocław Strachowice",
    "EPPO": "Poznań Ławica",
    "EPKT": "Katowice Pyrzowice",
    "EPRZ": "Rzeszów Jasionka",
    "EPBY": "Bydgoszcz Szwederowo",
    "EPLB": "Lublin Świdnik",
    "EPSC": "Szczecin Goleniów",
    "EPLL": "Łódź Lublinek",
    "EPZG": "Zielona Góra Babimost",
    "EPKP": "Kraków Rakowice-Czyżyny",
    "EPDE": "Dęblin",
    "EPML": "Mielec",
    "EPMI": "Mińsk Mazowiecki",
    "EPMB": "Malbork",
    "EPBO": "Bydgoszcz Biedaszkowo",
    "EPOK": "Ostrów Mazowiecka",
    "EPLY": "Łask",
}


def airport_label(code: str) -> str:
    """Return 'EPWA (Warszawa Chopin)' or just 'EPXX' if unknown."""
    name = POLISH_AIRPORTS.get((code or "").upper())
    return f"{code} ({name})" if name else (code or "nieznane")


# ─── State management ─────────────────────────────────────────────────────────

def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    cleaned = {k: v for k, v in seen.items() if v > cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
    print(f"[state] Saved {len(cleaned)} entries (removed {len(seen) - len(cleaned)} old ones)")


# ─── OpenSky API helpers ──────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "FlightAlertBot/1.0"})


def opensky_get(path: str, params: dict | None = None) -> dict | list | None:
    url = f"https://opensky-network.org/api{path}"
    auth = (OPENSKY_USER, OPENSKY_PASS) if OPENSKY_USER else None
    try:
        r = SESSION.get(url, params=params, auth=auth, timeout=30)
        if r.status_code == 429:
            print(f"[api] Rate limited on {path}, sleeping 60s...")
            time.sleep(60)
            r = SESSION.get(url, params=params, auth=auth, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        print(f"[api] HTTP error {e.response.status_code} on {path}: {e}")
        return None
    except Exception as e:
        print(f"[api] Error on {path}: {e}")
        return None


def get_airborne_states() -> list:
    """Fetch all currently airborne aircraft state vectors from OpenSky."""
    data = opensky_get("/states/all")
    if not data or "states" not in data:
        return []
    # s[8] = on_ground flag
    return [s for s in (data["states"] or []) if s and s[8] is False]


def get_recent_flight(icao24: str) -> dict | None:
    """
    Fetch the most recent flight record for an aircraft.
    Returns dict with estDepartureAirport, estArrivalAirport, firstSeen, lastSeen.
    Note: OpenSky flight data has ~1h delay but destination is usually correct.
    """
    end = int(time.time())
    begin = end - 4 * 3600  # look back 4 hours to catch long flights already airborne
    data = opensky_get(
        "/flights/aircraft",
        {"icao24": icao24.lower(), "begin": begin, "end": end}
    )
    if isinstance(data, list) and data:
        return data[-1]  # most recent
    return None


# ─── Email ────────────────────────────────────────────────────────────────────

def build_email_html(
    callsign: str,
    icao24: str,
    dep: str,
    arr: str,
    lat: float | None,
    lon: float | None,
    alt_m: float | None,
    speed_ms: float | None,
    first_seen_ts: int | None,
) -> tuple[str, str]:
    """Build subject + HTML body for the alert email."""

    alt_ft  = f"{int(alt_m * 3.28084):,} ft"  if alt_m    else "–"
    spd_kts = f"{int(speed_ms * 1.94384):,} kts" if speed_ms else "–"
    pos_str = f"{lat:.4f}°, {lon:.4f}°" if lat and lon else "–"
    dep_time = (
        datetime.fromtimestamp(first_seen_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if first_seen_ts else "–"
    )
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    fr24_url  = f"https://www.flightradar24.com/{callsign}"
    adsbx_url = f"https://globe.adsbexchange.com/?icao={icao24}"
    osn_url   = f"https://opensky-network.org/aircraft-profile?icao24={icao24}"

    subject = f"✈️[FlightTracker] {callsign} → {arr} (Polska)"

    html = f"""<!DOCTYPE html>
<html lang="pl"><head><meta charset="utf-8">
<title>Alert lotniczy</title></head>
<body style="font-family:Arial,sans-serif;background:#f9fafb;margin:0;padding:20px">
  <div style="max-width:580px;margin:auto;background:#fff;border-radius:8px;
              box-shadow:0 1px 4px rgba(0,0,0,.12);overflow:hidden">

    <div style="background:#1a56db;padding:20px 24px">
      <h1 style="color:#fff;margin:0;font-size:20px">✈️ Alert lotniczy – lot do Polski</h1>
    </div>

    <div style="padding:24px">
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr>
          <td style="padding:9px 12px;background:#f3f4f6;font-weight:bold;width:40%;border-radius:4px 0 0 4px">Callsign</td>
          <td style="padding:9px 12px;border:1px solid #e5e7eb"><strong style="font-size:16px">{callsign}</strong></td>
        </tr>
        <tr>
          <td style="padding:9px 12px;background:#f3f4f6;font-weight:bold">ICAO24 (hex)</td>
          <td style="padding:9px 12px;border:1px solid #e5e7eb;font-family:monospace">{icao24}</td>
        </tr>
        <tr>
          <td style="padding:9px 12px;background:#dbeafe;font-weight:bold">Skąd</td>
          <td style="padding:9px 12px;border:1px solid #e5e7eb">{dep or "nieznane"}</td>
        </tr>
        <tr>
          <td style="padding:9px 12px;background:#dcfce7;font-weight:bold">Dokąd 🇵🇱</td>
          <td style="padding:9px 12px;border:1px solid #e5e7eb">
            <strong>{airport_label(arr)}</strong>
          </td>
        </tr>
        <tr>
          <td style="padding:9px 12px;background:#f3f4f6;font-weight:bold">Wylot (est.)</td>
          <td style="padding:9px 12px;border:1px solid #e5e7eb">{dep_time}</td>
        </tr>
        <tr>
          <td style="padding:9px 12px;background:#f3f4f6;font-weight:bold">Pozycja</td>
          <td style="padding:9px 12px;border:1px solid #e5e7eb">{pos_str}</td>
        </tr>
        <tr>
          <td style="padding:9px 12px;background:#f3f4f6;font-weight:bold">Wysokość</td>
          <td style="padding:9px 12px;border:1px solid #e5e7eb">{alt_ft}</td>
        </tr>
        <tr>
          <td style="padding:9px 12px;background:#f3f4f6;font-weight:bold">Prędkość</td>
          <td style="padding:9px 12px;border:1px solid #e5e7eb">{spd_kts}</td>
        </tr>
      </table>

      <div style="margin-top:20px">
        <a href="{fr24_url}"
           style="display:inline-block;background:#e26f24;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;margin-right:8px;font-size:13px">
          FlightRadar24
        </a>
        <a href="{adsbx_url}"
           style="display:inline-block;background:#1a56db;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;margin-right:8px;font-size:13px">
          ADS-B Exchange
        </a>
        <a href="{osn_url}"
           style="display:inline-block;background:#374151;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;font-size:13px">
          OpenSky
        </a>
      </div>
    </div>

    <div style="padding:12px 24px;background:#f3f4f6;font-size:11px;color:#9ca3af">
      Wygenerowano: {now_str}
    </div>
  </div>
</body></html>"""

    return subject, html


def send_email(subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as srv:
        srv.login(EMAIL_FROM, EMAIL_PASS)
        srv.send_message(msg)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"{'='*60}")
    print(f"Flight Alert Check — {run_time}")
    print(f"Tracking prefixes: {CALLSIGN_PREFIXES}")
    print(f"{'='*60}")

    seen = load_seen()

    # 1. Fetch all airborne state vectors
    states = get_airborne_states()
    if not states:
        print("[main] No state data received – OpenSky may be unavailable.")
        return
    print(f"[main] Airborne aircraft: {len(states)}")

    # 2. Filter by callsign prefix
    matches = [
        s for s in states
        if any((s[1] or "").strip().startswith(p) for p in CALLSIGN_PREFIXES)
    ]
    print(f"[main] Matching callsigns: {len(matches)}")
    for s in matches:
        print(f"       → {(s[1] or '').strip()} ({s[0]})")

    if not matches:
        save_seen(seen)
        return

    # 3. For each match, check destination
    alerts_sent = 0
    for s in matches:
        icao24   = s[0]
        callsign = (s[1] or "").strip()
        lon      = s[5]
        lat      = s[6]
        alt_m    = s[7]   # baro altitude in metres (can be None)
        speed_ms = s[9]   # velocity m/s (can be None)

        print(f"\n[{callsign}] Fetching flight details for icao24={icao24}...")
        flight = get_recent_flight(icao24)
        time.sleep(1)  # be polite to the API

        if not flight:
            print(f"[{callsign}] No flight record found (data may not be available yet)")
            continue

        arr = (flight.get("estArrivalAirport") or "").strip().upper()
        dep = (flight.get("estDepartureAirport") or "").strip().upper()
        first_seen = flight.get("firstSeen")

        print(f"[{callsign}] Route: {dep or '?'} → {arr or '?'}")

        # 4. Check if destination is in Poland
        if not arr.startswith(POLAND_ICAO_PREFIX):
            print(f"[{callsign}] Not heading to Poland — skipping")
            continue

        # 5. Build deduplication key using departure date (not today's date!)
        #    This correctly handles long-haul flights that span midnight.
        if first_seen:
            dep_date = datetime.fromtimestamp(first_seen, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            dep_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        flight_key = f"{callsign}_{dep}_{arr}_{dep_date}"

        if flight_key in seen:
            print(f"[{callsign}] Already alerted for this flight — skipping")
            continue

        # 6. Send alert
        print(f"[{callsign}] 🚨 ALERT: {dep} → {arr} — sending email...")
        subject, html = build_email_html(
            callsign, icao24, dep, arr, lat, lon, alt_m, speed_ms, first_seen
        )
        try:
            send_email(subject, html)
            seen[flight_key] = datetime.now(timezone.utc).isoformat()
            alerts_sent += 1
            print(f"[{callsign}] ✓ Email sent to {EMAIL_TO}")
        except Exception as e:
            print(f"[{callsign}] ✗ Email failed: {e}")

    save_seen(seen)
    print(f"\n{'='*60}")
    print(f"Done — {alerts_sent} alert(s) sent")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
