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

CALLSIGN_PREFIXES = ["CMB", "ADB", "VDA", "CVK"]
POLAND_ICAO_PREFIX = "EP"
STATE_FILE = "seen_flights.json"
STATE_RETENTION_DAYS = 7

# Jak długo (godziny) trzymamy lot w kolejce pending zanim odpuścimy
PENDING_MAX_HOURS = 26

OPENSKY_USER   = os.environ.get("OPENSKY_USERNAME", "")
OPENSKY_PASS   = os.environ.get("OPENSKY_PASSWORD", "")
AVIATIONSTACK_KEY = os.environ.get("AVIATIONSTACK_KEY", "")   # darmowe: aviationstack.com
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")
EMAIL_PASS     = os.environ.get("EMAIL_PASSWORD", "")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "465"))

# ─── Polish airports lookup ───────────────────────────────────────────────────

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
    name = POLISH_AIRPORTS.get((code or "").upper())
    return f"{code} ({name})" if name else (code or "nieznane")


# ─── State management ─────────────────────────────────────────────────────────
# State JSON structure:
# {
#   "seen":    { "flight_key": "iso_timestamp", ... },   ← wysłane alerty
#   "pending": { "icao24": { "callsign", "first_added", "lat", "lon", ... }, ... }
# }

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # migracja ze starego formatu (plain dict seen)
        if "seen" not in data:
            data = {"seen": data, "pending": {}}
        return data
    return {"seen": {}, "pending": {}}


def save_state(state: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}

    pending_cutoff = (datetime.now(timezone.utc) - timedelta(hours=PENDING_MAX_HOURS)).isoformat()
    removed = [k for k, v in state["pending"].items() if v["first_added"] < pending_cutoff]
    for k in removed:
        print(f"[state] Dropping stale pending: {state['pending'][k]['callsign']} ({k})")
        del state["pending"][k]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print(f"[state] seen={len(state['seen'])}, pending={len(state['pending'])}")


# ─── OpenSky API ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "FlightAlertBot/1.0"})


def opensky_get(path: str, params: dict | None = None) -> dict | list | None:
    url = f"https://opensky-network.org/api{path}"
    auth = (OPENSKY_USER, OPENSKY_PASS) if OPENSKY_USER else None
    try:
        r = SESSION.get(url, params=params, auth=auth, timeout=30)
        if r.status_code == 429:
            print(f"[api] Rate limited, sleeping 60s...")
            time.sleep(60)
            r = SESSION.get(url, params=params, auth=auth, timeout=30)
        if r.status_code == 404:
            return None  # brak danych – nie loguj jako błąd
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        print(f"[api] HTTP {e.response.status_code} on {path}")
        return None
    except Exception as e:
        print(f"[api] Error: {e}")
        return None


def get_airborne_states() -> list:
    data = opensky_get("/states/all")
    if not data or "states" not in data:
        return []
    return [s for s in (data["states"] or []) if s and s[8] is False]


def get_flight_record(icao24: str) -> dict | None:
    """
    Szuka rekordu lotu w oknie 24h wstecz.
    Zwraca ostatni wpis lub None jeśli brak danych.
    404 = OpenSky jeszcze nie ma danych (lot za świeży lub nieznany).
    """
    end = int(time.time())
    begin = end - 24 * 3600  # 24h – wystarczy dla lotów transatlantyckich
    data = opensky_get(
        "/flights/aircraft",
        {"icao24": icao24.lower(), "begin": begin, "end": end}
    )
    if isinstance(data, list) and data:
        return data[-1]
    return None


# ─── Aviationstack API (fallback) ────────────────────────────────────────────

def as_get_destination(callsign: str) -> tuple[str, str] | tuple[None, None]:
    """
    Odpytaj aviationstack.com o cel lotu po callsignie (numerze lotu).
    Darmowy tier: 1000 zapytań/miesiąc, bez karty kredytowej.
    Rejestracja: https://aviationstack.com/signup/free
    Zwraca (dep_icao, arr_icao) lub (None, None).
    """
    if not AVIATIONSTACK_KEY:
        return None, None

    url = "https://api.aviationstack.com/v1/flights"
    params = {
        "access_key": AVIATIONSTACK_KEY,
        "flight_icao": callsign,   # np. CMB534
        "flight_status": "active",
        "limit": 1,
    }
    try:
        r = SESSION.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()

        if data.get("error"):
            print(f"[as] Błąd aviationstack: {data['error'].get('message', '?')}")
            return None, None

        flights = (data.get("data") or [])
        if not flights:
            # Spróbuj bez flight_status – lot może być już na finałowym podejściu
            params.pop("flight_status")
            r2 = SESSION.get(url, params=params, timeout=15)
            r2.raise_for_status()
            flights = (r2.json().get("data") or [])

        if not flights:
            print(f"[as] aviationstack: brak lotu {callsign}")
            return None, None

        fl  = flights[0]
        arr = ((fl.get("arrival") or {}).get("icao") or "").strip().upper()
        dep = ((fl.get("departure") or {}).get("icao") or "").strip().upper()

        if arr:
            print(f"[as] aviationstack: {callsign} → {dep or '?'} → {arr}")
            return dep, arr

        print(f"[as] aviationstack: brak lotniska docelowego dla {callsign}")
        return None, None

    except Exception as e:
        print(f"[as] Błąd: {e}")
        return None, None



import re

# ─── FlightAware public page scraper (bez API, bez rejestracji) ───────────────

_FA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Wzorce do szukania ICAO lotniska docelowego w HTML/JSON FlightAware
_FA_PATTERNS = [
    # JSON embedded w stronie: "destination":{"icao":"EPWA",...}
    r'"destination"\s*:\s*\{[^}]*"icao"\s*:\s*"([A-Z]{4})"',
    # alternatywna kolejność kluczy
    r'"icao"\s*:\s*"([A-Z]{4})"[^}]*"[^"]*destination[^"]*"',
    # data-icao na elemencie destination
    r'destination[^>]*data-icao[^>]*=\s*["\']([A-Z]{4})["\']',
    # flightPageDestination span
    r'flightPageDestination[^>]*>([A-Z]{4})<',
    # /airports/EPWA/... link w sekcji destination
    r'destination[^<]{0,300}/airports/([A-Z]{4})/',
]

_FA_DEP_PATTERNS = [
    r'"origin"\s*:\s*\{[^}]*"icao"\s*:\s*"([A-Z]{4})"',
    r'flightPageOrigin[^>]*>([A-Z]{4})<',
    r'origin[^<]{0,300}/airports/([A-Z]{4})/',
]


def fa_scrape_destination(callsign: str) -> tuple[str, str] | tuple[None, None]:
    """
    Pobiera publiczną stronę FlightAware i wyciąga lotnisko docelowe (ICAO).
    Nie wymaga API ani rejestracji.
    Zwraca (dep_icao, arr_icao) lub (None, None).
    """
    url = f"https://www.flightaware.com/live/flight/{callsign}"
    try:
        r = SESSION.get(url, headers=_FA_HEADERS, timeout=20)
        if r.status_code == 404:
            print(f"[fa_scrape] Brak lotu {callsign} na FlightAware")
            return None, None
        if r.status_code in (403, 429):
            print(f"[fa_scrape] FlightAware zablokował żądanie ({r.status_code})")
            return None, None
        r.raise_for_status()
    except Exception as e:
        print(f"[fa_scrape] Błąd połączenia: {e}")
        return None, None

    html = r.text

    arr = None
    for pattern in _FA_PATTERNS:
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            arr = m.group(1).upper()
            # Odfiltruj fałszywe trafienia (kody które nie są lotniskami)
            if len(arr) == 4 and arr.isalpha():
                break
            arr = None

    dep = None
    for pattern in _FA_DEP_PATTERNS:
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            dep = m.group(1).upper()
            if len(dep) == 4 and dep.isalpha():
                break
            dep = None

    if arr:
        print(f"[fa_scrape] FlightAware (scrape): {callsign} → {dep or '?'} → {arr}")
        return dep, arr

    # Ostatnia szansa: szukaj kodu EP** gdziekolwiek na stronie
    # (FlightAware może renderować EPWA/EPKK/etc. w różnych miejscach)
    ep_codes = re.findall(r'\bEP[A-Z]{2}\b', html)
    if ep_codes:
        arr = ep_codes[0]
        print(f"[fa_scrape] Znaleziono polski kod lotniska na stronie FA: {arr}")
        return dep, arr

    print(f"[fa_scrape] Nie udało się wyciągnąć celu z FlightAware dla {callsign}")
    return None, None


# ─── Email ────────────────────────────────────────────────────────────────────

def build_email_html(
    callsign: str, icao24: str, dep: str, arr: str,
    lat, lon, alt_m, speed_ms, first_seen_ts,
    pending_since: str | None = None,
    dest_source: str = "OpenSky",
) -> tuple[str, str]:

    alt_ft  = f"{int(alt_m * 3.28084):,} ft"    if alt_m    else "–"
    spd_kts = f"{int(speed_ms * 1.94384):,} kts" if speed_ms else "–"
    pos_str = f"{lat:.4f}°, {lon:.4f}°"           if (lat and lon) else "–"
    dep_time = (
        datetime.fromtimestamp(first_seen_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if first_seen_ts else "–"
    )
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    pending_note = ""
    if pending_since:
        pending_note = f"""
        <tr>
          <td style="padding:9px 12px;background:#fef9c3;font-weight:bold">Uwaga</td>
          <td style="padding:9px 12px;border:1px solid #e5e7eb">
            Cel potwierdzony po oczekiwaniu (lot w kolejce od {pending_since})
          </td>
        </tr>"""

    subject = f"✈️ [FlightTracker] {callsign} → {arr} (Polska)"
    source_color = "#1a56db" if dest_source == "OpenSky" else "#e26f24"
    source_badge = f'<span style="background:{source_color};color:#fff;padding:2px 8px;border-radius:10px;font-size:11px">{dest_source}</span>'
    html = f"""<!DOCTYPE html>
<html lang="pl"><head><meta charset="utf-8"><title>Alert lotniczy</title></head>
<body style="font-family:Arial,sans-serif;background:#f9fafb;margin:0;padding:20px">
  <div style="max-width:580px;margin:auto;background:#fff;border-radius:8px;
              box-shadow:0 1px 4px rgba(0,0,0,.12);overflow:hidden">
    <div style="background:#1a56db;padding:20px 24px">
      <h1 style="color:#fff;margin:0;font-size:20px">✈️ Alert lotniczy – lot do Polski</h1>
    </div>
    <div style="padding:24px">
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr>
          <td style="padding:9px 12px;background:#f3f4f6;font-weight:bold;width:40%">Callsign</td>
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
          <td style="padding:9px 12px;border:1px solid #e5e7eb"><strong>{airport_label(arr)}</strong> {source_badge}</td>
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
        {pending_note}
      </table>
      <div style="margin-top:20px">
        <a href="https://www.flightradar24.com/{callsign}"
           style="display:inline-block;background:#e26f24;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;margin-right:8px;font-size:13px">FlightRadar24</a>
        <a href="https://globe.adsbexchange.com/?icao={icao24}"
           style="display:inline-block;background:#1a56db;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;margin-right:8px;font-size:13px">ADS-B Exchange</a>
        <a href="https://opensky-network.org/aircraft-profile?icao24={icao24}"
           style="display:inline-block;background:#374151;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;font-size:13px">OpenSky</a>
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


def try_send_alert(callsign, icao24, dep, arr, lat, lon, alt_m, speed_ms,
                   first_seen, state, pending_since=None, dest_source="OpenSky") -> bool:
    """Wyślij alert i dodaj do seen. Zwraca True jeśli sukces."""
    if first_seen:
        dep_date = datetime.fromtimestamp(first_seen, tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        dep_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    flight_key = f"{callsign}_{dep}_{arr}_{dep_date}"

    if flight_key in state["seen"]:
        print(f"[{callsign}] Już wysłano alert dla tego lotu — pomijam")
        return True  # traktuj jako sukces żeby usunąć z pending

    print(f"[{callsign}] 🚨 ALERT: {dep or '?'} → {arr} [{dest_source}] — wysyłam email...")
    subject, html = build_email_html(
        callsign, icao24, dep, arr, lat, lon, alt_m, speed_ms, first_seen,
        pending_since, dest_source=dest_source
    )
    try:
        send_email(subject, html)
        state["seen"][flight_key] = datetime.now(timezone.utc).isoformat()
        print(f"[{callsign}] ✓ Email wysłany na {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"[{callsign}] ✗ Błąd wysyłki: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"{'='*60}")
    print(f"Flight Alert Check — {run_time}")
    print(f"{'='*60}")

    state = load_state()
    alerts_sent = 0

    # ── Krok 1: Sprawdź loty w kolejce pending ────────────────────────────────
    if state["pending"]:
        print(f"\n[pending] Sprawdzam {len(state['pending'])} lotów bez potwierdzenia celu...")
        to_remove = []
        for icao24, info in list(state["pending"].items()):
            callsign = info["callsign"]
            print(f"[pending] {callsign} ({icao24}) — szukam rekordu...")
            flight = get_flight_record(icao24)
            time.sleep(1)

            arr, dep, first_seen = "", "", None
            dest_src = "OpenSky"

            if flight:
                arr = (flight.get("estArrivalAirport") or "").strip().upper()
                dep = (flight.get("estDepartureAirport") or "").strip().upper()
                first_seen = flight.get("firstSeen")
                print(f"[pending] {callsign} — OpenSky: {dep or '?'} → {arr or '?'}")
            else:
                print(f"[pending] {callsign} — OpenSky niedostępny lub brak rekordu, próbuję fallbacki...")

            # Fallbacki gdy OpenSky nie dał celu (lub w ogóle nie odpowiedział)
            if not arr:
                as_dep, as_arr = as_get_destination(callsign)
                if as_arr:
                    dep = as_dep or dep
                    arr = as_arr
                    dest_src = "aviationstack"
                else:
                    fa_dep, fa_arr = fa_scrape_destination(callsign)
                    if fa_arr:
                        dep = fa_dep or dep
                        arr = fa_arr
                        dest_src = "FlightAware"
                    else:
                        print(f"[pending] {callsign} — cel nieznany (OpenSky + aviationstack + FA)")
                        continue

            if not arr.startswith(POLAND_ICAO_PREFIX):
                print(f"[pending] {callsign} — cel {arr} to nie Polska, usuwam z kolejki")
                to_remove.append(icao24)
                continue

            # Cel to Polska!
            pending_since = info["first_added"][:16].replace("T", " ") + " UTC"
            ok = try_send_alert(
                callsign, icao24, dep, arr,
                info.get("lat"), info.get("lon"),
                info.get("alt_m"), info.get("speed_ms"),
                first_seen, state, pending_since=pending_since, dest_source=dest_src
            )
            if ok:
                to_remove.append(icao24)
                alerts_sent += 1

        for icao24 in to_remove:
            state["pending"].pop(icao24, None)

    # ── Krok 2: Sprawdź aktualne loty w powietrzu ─────────────────────────────
    print(f"\n[main] Pobieranie aktualnych pozycji...")
    states = get_airborne_states()
    if not states:
        print("[main] Brak danych z OpenSky — możliwa niedostępność API")
        save_state(state)
        return
    print(f"[main] Samolotów w powietrzu: {len(states)}")

    matches = [
        s for s in states
        if any((s[1] or "").strip().startswith(p) for p in CALLSIGN_PREFIXES)
    ]
    print(f"[main] Pasujące callsigny: {len(matches)}")
    for s in matches:
        print(f"       → {(s[1] or '').strip()} ({s[0]})")

    if not matches:
        save_state(state)
        return

    for s in matches:
        icao24   = s[0]
        callsign = (s[1] or "").strip()
        lon      = s[5]
        lat      = s[6]
        alt_m    = s[7]
        speed_ms = s[9]

        # Pomiń jeśli już jest w pending (będzie sprawdzone powyżej w następnym runie)
        if icao24 in state["pending"]:
            print(f"\n[{callsign}] Już w kolejce pending — czekam na dane OpenSky")
            continue

        print(f"\n[{callsign}] Szukam rekordu lotu (icao24={icao24})...")
        flight = get_flight_record(icao24)
        time.sleep(1)

        if not flight:
            # OpenSky nie ma jeszcze danych — dodaj do pending
            print(f"[{callsign}] Brak rekordu (lot za świeży lub nieznany) — dodaję do pending")
            state["pending"][icao24] = {
                "callsign":    callsign,
                "first_added": datetime.now(timezone.utc).isoformat(),
                "lat":         lat,
                "lon":         lon,
                "alt_m":       alt_m,
                "speed_ms":    speed_ms,
            }
            continue

        arr = (flight.get("estArrivalAirport") or "").strip().upper()
        dep = (flight.get("estDepartureAirport") or "").strip().upper()
        first_seen = flight.get("firstSeen")

        print(f"[{callsign}] Trasa: {dep or '?'} → {arr or '?'}")

        if not arr:
            # Fallback 1: aviationstack
            as_dep, as_arr = as_get_destination(callsign)
            if as_arr:
                dep = as_dep or dep
                arr = as_arr
            else:
                # Fallback 2: scrape publicznej strony FlightAware
                fa_dep, fa_arr = fa_scrape_destination(callsign)
                if fa_arr:
                    dep = fa_dep or dep
                    arr = fa_arr
                else:
                    # Wszystkie źródła zawiodły — dodaj do pending
                    print(f"[{callsign}] Cel nieznany (OpenSky + aviationstack + FA) — dodaję do pending")
                    state["pending"][icao24] = {
                        "callsign":    callsign,
                        "first_added": datetime.now(timezone.utc).isoformat(),
                        "lat":         lat,
                        "lon":         lon,
                        "alt_m":       alt_m,
                        "speed_ms":    speed_ms,
                    }
                    continue

        if not arr.startswith(POLAND_ICAO_PREFIX):
            print(f"[{callsign}] Cel nie jest Polska — pomijam")
            continue

        src = "aviationstack" if not (flight.get("estArrivalAirport") or "") else "OpenSky"
        if not (flight.get("estArrivalAirport") or "") and not as_arr:
            src = "FlightAware"
        ok = try_send_alert(
            callsign, icao24, dep, arr, lat, lon, alt_m, speed_ms, first_seen, state,
            dest_source=src
        )
        if ok:
            alerts_sent += 1

    save_state(state)
    print(f"\n{'='*60}")
    print(f"Gotowe — wysłano {alerts_sent} alert(ów)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
