"""
Flight Alert Tracker
Monitors flights by callsign prefix and sends an email alert when the
destination is Poland — or always, for selected operators (see config).
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests


# ─── Configuration ────────────────────────────────────────────────────────────

# Callsign prefixes to monitor.
CALLSIGN_PREFIXES: list[str] = ["CMB", "ADB", "VDA", "CVK"]

# Prefixes that trigger an alert regardless of destination (unknown dest = alert,
# confirmed non-Poland dest = no alert).
ALWAYS_ALERT_PREFIXES: list[str] = ["ADB"]

POLAND_ICAO_PREFIX = "EP"
STATE_FILE = "seen_flights.json"
STATE_RETENTION_DAYS = 7
PENDING_MAX_HOURS = 26  # drop from queue after this many hours without a known destination

OPENSKY_USER      = os.environ.get("OPENSKY_USERNAME", "")
OPENSKY_PASS      = os.environ.get("OPENSKY_PASSWORD", "")
AVIATIONSTACK_KEY = os.environ.get("AVIATIONSTACK_KEY", "")
EMAIL_FROM        = os.environ.get("EMAIL_FROM", "")
EMAIL_TO          = os.environ.get("EMAIL_TO", "")
EMAIL_PASS        = os.environ.get("EMAIL_PASSWORD", "")
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "465"))


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Aircraft:
    """Aircraft metadata gathered from various sources."""
    model:    str = ""
    reg:      str = ""
    operator: str = ""

    def is_empty(self) -> bool:
        return not any([self.model, self.reg, self.operator])

    def merge(self, other: Aircraft) -> None:
        """Fill in missing fields from another Aircraft (self takes priority)."""
        self.model    = self.model    or other.model
        self.reg      = self.reg      or other.reg
        self.operator = self.operator or other.operator


@dataclass
class FlightRoute:
    """Route information resolved from one or more data sources."""
    dep:    str       = ""   # departure airport ICAO
    arr:    str       = ""   # arrival airport ICAO
    eta_ts: int | None = None  # estimated arrival as Unix timestamp
    source: str       = ""   # which source provided the destination

    @property
    def dest_known(self) -> bool:
        return bool(self.arr)

    @property
    def dest_is_poland(self) -> bool:
        return self.arr.startswith(POLAND_ICAO_PREFIX)


@dataclass
class PendingEntry:
    """A flight whose destination is not yet known, stored between runs."""
    callsign:              str
    first_added:           str        # ISO timestamp
    fa_unconfirmed_dest:   str = ""   # FA-suggested Poland destination awaiting confirmation

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> PendingEntry:
        return PendingEntry(
            callsign=d["callsign"],
            first_added=d["first_added"],
            fa_unconfirmed_dest=d.get("fa_unconfirmed_dest", ""),
        )


# ─── Polish airports lookup ───────────────────────────────────────────────────

POLISH_AIRPORTS: dict[str, str] = {
    # Civil
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
    "EPRA": "Radom Sadków",
    "EPSY": "Olsztyn-Mazury Szymany",
    "EPRG": "Rzeszów Mielec",
    # Military / dual-use
    "EPKS": "Poznań Krzesiny",
    "EPKP": "Kraków Rakowice-Czyżyny",
    "EPDE": "Dęblin",
    "EPML": "Mielec",
    "EPMI": "Mińsk Mazowiecki",
    "EPMB": "Malbork",
    "EPBO": "Bydgoszcz Szwederowo (mil.)",
    "EPOK": "Ostrów Mazowiecka",
    "EPLY": "Łask",
    "EPLK": "Łęczyca",
    "EPPR": "Pruszcz Gdański",
    "EPCE": "Centrum Pruszcz",
    "EPOW": "Nowe Miasto nad Pilicą",
    "EPWT": "Warszawa Babice",
    "EPCH": "Chełm",
    "EPKB": "Krosno",
    "EPZD": "Świdwin",
    "EPIR": "Inowrocław",
    "EPPT": "Płock",
}


def airport_label(code: str) -> str:
    """Return 'EPWA (Warszawa Chopin)' or just the raw code if unknown."""
    name = POLISH_AIRPORTS.get((code or "").upper())
    return f"{code} ({name})" if name else (code or "nieznane")


# ─── Filtering helpers ────────────────────────────────────────────────────────

def matches_tracked_prefix(callsign: str) -> bool:
    return any(callsign.startswith(p) for p in CALLSIGN_PREFIXES)


def should_alert(callsign: str, route: FlightRoute) -> bool:
    """
    Decide whether to send an alert based on callsign and resolved route.

    Rules:
    - destination = Poland              → always alert
    - destination unknown               → alert only for ALWAYS_ALERT_PREFIXES
    - destination confirmed non-Poland  → never alert
    """
    if route.dest_is_poland:
        return True
    if not route.dest_known:
        return any(callsign.startswith(p) for p in ALWAYS_ALERT_PREFIXES)
    return False


# ─── State management ─────────────────────────────────────────────────────────

class State:
    """
    Persistent tracker state saved to seen_flights.json after every run.

    Structure:
      {
        "seen":    { "<flight_key>": "<iso_timestamp>", ... },
        "pending": { "<icao24>":    { "callsign": ..., "first_added": ... }, ... }
      }
    """

    def __init__(self, seen: dict[str, str], pending: dict[str, PendingEntry]) -> None:
        self.seen    = seen
        self.pending = pending

    @staticmethod
    def load() -> State:
        if not os.path.exists(STATE_FILE):
            return State(seen={}, pending={})
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[state] WARNING: could not read {STATE_FILE} ({e}) — starting fresh")
            return State(seen={}, pending={})
        # Migrate from legacy format (plain dict without "seen" key)
        if "seen" not in raw:
            raw = {"seen": raw, "pending": {}}
        pending = {}
        for icao24, v in raw.get("pending", {}).items():
            try:
                pending[icao24] = PendingEntry.from_dict(v)
            except (KeyError, TypeError) as e:
                print(f"[state] Skipping malformed pending entry {icao24}: {e}")
        return State(seen=raw.get("seen", {}), pending=pending)

    def save(self) -> None:
        cutoff_seen    = (datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
        cutoff_pending = (datetime.now(timezone.utc) - timedelta(hours=PENDING_MAX_HOURS)).isoformat()

        self.seen = {k: v for k, v in self.seen.items() if v > cutoff_seen}

        stale = [icao24 for icao24, e in self.pending.items() if e.first_added < cutoff_pending]
        for icao24 in stale:
            print(f"[state] Dropping stale pending: {self.pending[icao24].callsign} ({icao24})")
            del self.pending[icao24]

        payload = {
            "seen":    self.seen,
            "pending": {icao24: e.to_dict() for icao24, e in self.pending.items()},
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"[state] seen={len(self.seen)}, pending={len(self.pending)}")

    def already_alerted(self, flight_key: str) -> bool:
        return flight_key in self.seen

    def mark_alerted(self, flight_key: str) -> None:
        self.seen[flight_key] = datetime.now(timezone.utc).isoformat()

    def add_pending(self, icao24: str, callsign: str, fa_unconfirmed_dest: str = "") -> None:
        self.pending[icao24] = PendingEntry(
            callsign=callsign,
            first_added=datetime.now(timezone.utc).isoformat(),
            fa_unconfirmed_dest=fa_unconfirmed_dest,
        )

    def remove_pending(self, icao24: str) -> None:
        self.pending.pop(icao24, None)


# ─── HTTP session ─────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "FlightAlertBot/1.0"})


# ─── OpenSky data layer ───────────────────────────────────────────────────────

def _opensky_get(path: str, params: dict | None = None) -> dict | list | None:
    url  = f"https://opensky-network.org/api{path}"
    auth = (OPENSKY_USER, OPENSKY_PASS) if OPENSKY_USER else None
    try:
        r = SESSION.get(url, params=params, auth=auth, timeout=6)
        if r.status_code == 429:
            print("[opensky] Rate limited, sleeping 60s...")
            time.sleep(60)
            r = SESSION.get(url, params=params, auth=auth, timeout=6)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        print(f"[opensky] HTTP {e.response.status_code} on {path}")
        return None
    except Exception as e:
        print(f"[opensky] Error: {e}")
        return None


def fetch_airborne_states() -> list:
    """Return all currently airborne state vectors from OpenSky."""
    data = _opensky_get("/states/all")
    if not data or "states" not in data:
        return []
    return [s for s in (data["states"] or []) if s and s[8] is False]


def fetch_flight_record(icao24: str) -> dict | None:
    """
    Return the most recent flight record from OpenSky (24 h look-back).
    Returns None if the record is stale — i.e. the flight landed more than
    2 hours ago and is therefore not the flight currently airborne.
    """
    end   = int(time.time())
    begin = end - 24 * 3600
    data  = _opensky_get("/flights/aircraft", {"icao24": icao24.lower(), "begin": begin, "end": end})
    if not isinstance(data, list) or not data:
        return None
    record = data[-1]
    last_seen = record.get("lastSeen")
    if last_seen and (end - last_seen) > 2 * 3600:
        print(f"[opensky] Discarding stale record (lastSeen {int((end - last_seen) / 3600)}h ago) — previous flight")
        return None
    return record


def fetch_route_from_opensky_record(record: dict) -> FlightRoute:
    """Parse a FlightRoute from a raw OpenSky flight record dict."""
    arr = (record.get("estArrivalAirport")   or "").strip().upper()
    dep = (record.get("estDepartureAirport") or "").strip().upper()
    eta = record.get("lastSeen")
    return FlightRoute(dep=dep, arr=arr, eta_ts=eta, source="OpenSky")


def fetch_route_from_opensky(icao24: str) -> FlightRoute | None:
    """
    Query OpenSky for the flight route of the given aircraft.
    Returns a FlightRoute if a current record exists, None if OpenSky has no
    data yet or only has a stale record from a previous flight.
    """
    record = fetch_flight_record(icao24)
    if record is None:
        return None
    return fetch_route_from_opensky_record(record)


def fetch_aircraft_from_opensky(icao24: str) -> Aircraft:
    """Query OpenSky metadata for aircraft type/registration/operator."""
    data = _opensky_get(f"/metadata/aircraft/icao24/{icao24.lower()}")
    if not isinstance(data, dict):
        return Aircraft()
    return Aircraft(
        model    = (data.get("model")        or data.get("typecode") or "").strip(),
        reg      = (data.get("registration") or "").strip(),
        operator = (data.get("operator")     or data.get("owner")    or "").strip(),
    )


# ─── Aviationstack data layer (fallback) ──────────────────────────────────────

def fetch_route_from_aviationstack(callsign: str) -> FlightRoute | None:
    """
    Query aviationstack.com for the flight route.
    Free tier: 1 000 requests/month — https://aviationstack.com/signup/free
    Returns None if the key is not configured or no data is available.
    """
    if not AVIATIONSTACK_KEY:
        return None

    url    = "https://api.aviationstack.com/v1/flights"
    params = {"access_key": AVIATIONSTACK_KEY, "flight_icao": callsign, "flight_status": "active", "limit": 1}
    try:
        r = SESSION.get(url, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()

        if data.get("error"):
            print(f"[aviationstack] Error: {data['error'].get('message', '?')}")
            return None

        flights = data.get("data") or []
        if not flights:
            # Retry without status filter — flight may already be on final approach
            params.pop("flight_status")
            r2 = SESSION.get(url, params=params, timeout=8)
            r2.raise_for_status()
            flights = r2.json().get("data") or []

        if not flights:
            print(f"[aviationstack] No flight found for {callsign}")
            return None

        fl  = flights[0]
        arr = ((fl.get("arrival")   or {}).get("icao") or "").strip().upper()
        dep = ((fl.get("departure") or {}).get("icao") or "").strip().upper()

        if not arr:
            print(f"[aviationstack] No destination for {callsign}")
            return None

        eta_ts  = None
        eta_iso = ((fl.get("arrival") or {}).get("estimated") or
                   (fl.get("arrival") or {}).get("scheduled")  or "")
        if eta_iso:
            try:
                eta_ts = int(datetime.fromisoformat(eta_iso.replace("Z", "+00:00")).timestamp())
            except Exception:
                pass

        print(f"[aviationstack] {callsign} → {dep or '?'} → {arr}")
        return FlightRoute(dep=dep, arr=arr, eta_ts=eta_ts, source="aviationstack")

    except Exception as e:
        print(f"[aviationstack] Error: {e}")
        return None


# ─── FlightAware scraper (fallback, no API key required) ──────────────────────

_FA_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Embedded JSON patterns — most reliable
_RE_DEST_JSON   = re.compile(r'"destination"\s*:\s*\{[^}]{0,300}"icao"\s*:\s*"([A-Z]{4})"', re.S)
_RE_ORIGIN_JSON = re.compile(r'"origin"\s*:\s*\{[^}]{0,300}"icao"\s*:\s*"([A-Z]{4})"',      re.S)

# Link-based fallback
_RE_DEST_LINK   = re.compile(r'(?:destination|arrival)[^/]{0,200}/airports/([A-Z]{4})/',   re.S | re.I)
_RE_ORIGIN_LINK = re.compile(r'(?:origin|departure)[^/]{0,200}/airports/([A-Z]{4})/',      re.S | re.I)

# Aircraft metadata patterns
_RE_AIRCRAFT_TYPE = re.compile(r'"friendlyType"\s*:\s*"([^"]{3,60})"',                           re.S)
_RE_OPERATOR      = re.compile(r'"operator"\s*:\s*\{[^}]{0,200}"name"\s*:\s*"([^"]{2,60})"',     re.S)
_RE_REGISTRATION  = re.compile(r'"registration"\s*:\s*"([A-Z0-9\-]{3,10})"',                     re.S)


def _scrape_icao_pair(html: str, dest_re: re.Pattern, origin_re: re.Pattern) -> tuple[str, str]:
    """Extract (dep, arr) ICAO codes using the given regex pair."""
    dep = arr = ""
    m = dest_re.search(html)
    if m and m.group(1).isalpha():
        arr = m.group(1).upper()
    m = origin_re.search(html)
    if m and m.group(1).isalpha():
        dep = m.group(1).upper()
    return dep, arr


def _scrape_aircraft(html: str) -> Aircraft:
    """Extract aircraft metadata from FlightAware page HTML."""
    m_type = _RE_AIRCRAFT_TYPE.search(html)
    m_reg  = _RE_REGISTRATION.search(html)
    m_op   = _RE_OPERATOR.search(html)
    return Aircraft(
        model    = m_type.group(1).strip() if m_type else "",
        reg      = m_reg.group(1).strip()  if m_reg  else "",
        operator = m_op.group(1).strip()   if m_op   else "",
    )


class FlightAwareScrapeResult:
    """Bundles route and aircraft data from a single FlightAware page fetch."""
    def __init__(self, route: FlightRoute | None, aircraft: Aircraft) -> None:
        self.route    = route
        self.aircraft = aircraft


def fetch_from_flightaware(callsign: str) -> FlightAwareScrapeResult:
    """
    Scrape the public FlightAware flight page (no API key required).
    Returns a FlightAwareScrapeResult — route may be None if destination
    could not be extracted, but aircraft info may still be populated.
    """
    url = f"https://www.flightaware.com/live/flight/{callsign}"
    try:
        r = SESSION.get(url, headers=_FA_HEADERS, timeout=12)
        if r.status_code == 404:
            print(f"[flightaware] No flight found for {callsign}")
            return FlightAwareScrapeResult(route=None, aircraft=Aircraft())
        if r.status_code in (403, 429):
            print(f"[flightaware] Request blocked ({r.status_code})")
            return FlightAwareScrapeResult(route=None, aircraft=Aircraft())
        r.raise_for_status()
    except Exception as e:
        print(f"[flightaware] Connection error: {e}")
        return FlightAwareScrapeResult(route=None, aircraft=Aircraft())

    html     = r.text
    aircraft = _scrape_aircraft(html)

    # Attempt 1: embedded JSON (most reliable)
    dep, arr = _scrape_icao_pair(html, _RE_DEST_JSON, _RE_ORIGIN_JSON)

    # Attempt 2: airport links with surrounding context
    if not arr:
        dep2, arr2 = _scrape_icao_pair(html, _RE_DEST_LINK, _RE_ORIGIN_LINK)
        arr = arr2
        dep = dep2 or dep

    # Sanity check: destination cannot equal origin
    if arr and arr == dep:
        print(f"[flightaware] arr == dep ({arr}) — discarding route (scraper error)")
        return FlightAwareScrapeResult(route=None, aircraft=aircraft)

    if arr:
        print(f"[flightaware] {callsign} → {dep or '?'} → {arr}")
        return FlightAwareScrapeResult(
            route=FlightRoute(dep=dep or "", arr=arr, source="FlightAware"),
            aircraft=aircraft,
        )

    print(f"[flightaware] Could not extract destination for {callsign}")
    return FlightAwareScrapeResult(route=None, aircraft=aircraft)


# ─── Aircraft info aggregator ─────────────────────────────────────────────────

# NOTE: CMB (USTRANSCOM) and CVK use charter operators that change per flight
# (Atlas Air, Kalitta Air, National Airlines, CargoLogicAir, etc.) — hardcoding
# an operator would produce wrong results. We rely on real-time data sources only.

# ICAO24 prefix → (model, operator) for homogeneous fleets (Antonov operators)
# Safe to hardcode because these operators fly a single aircraft type.
_ICAO24_FLEET_HINTS: list[tuple[str, str, str]] = [
    ("5080", "An-124", "Antonov Airlines (ADB)"),
    ("508",  "An-124", "Antonov Airlines (ADB)"),
    ("152",  "An-124", "Volga-Dnepr (VDA)"),
    ("1540", "An-124", "Volga-Dnepr (VDA)"),
]


def _fetch_aircraft_from_hexdb(icao24: str) -> Aircraft:
    """
    Query hexdb.io — free, no registration or API key required.
    """
    try:
        r = SESSION.get(f"https://hexdb.io/api/v1/aircraft/{icao24.lower()}", timeout=5)
        if r.status_code != 200:
            return Aircraft()
        data = r.json()
        if not isinstance(data, dict):
            return Aircraft()
        return Aircraft(
            model    = (data.get("Type")             or data.get("ICAOTypeCode") or "").strip(),
            reg      = (data.get("Registration")     or "").strip(),
            operator = (data.get("RegisteredOwners") or "").strip(),
        )
    except Exception as e:
        print(f"[hexdb] Error: {e}")
        return Aircraft()


def _fleet_hint(icao24: str) -> Aircraft:
    """Return model/operator from known fleet prefixes (Antonov operators only)."""
    for prefix, model, operator in _ICAO24_FLEET_HINTS:
        if icao24.lower().startswith(prefix.lower()):
            return Aircraft(model=model, operator=operator)
    return Aircraft()


def resolve_aircraft(
    icao24:            str,
    fa_aircraft:       Aircraft | None = None,
    opensky_reachable: bool = True,
) -> Aircraft:
    """
    Build the best possible Aircraft record by merging data from multiple sources.
    Priority: OpenSky metadata > hexdb.io > FlightAware scrape > fleet prefix hints.

    Set opensky_reachable=False when the OpenSky flight endpoint already timed out
    this run — the metadata endpoint will likely be unavailable too, so we skip it.
    """
    result = Aircraft()
    if opensky_reachable:
        result = fetch_aircraft_from_opensky(icao24)
    if result.is_empty():
        result.merge(_fetch_aircraft_from_hexdb(icao24))
    if result.is_empty():
        result.merge(fa_aircraft or Aircraft())
    result.merge(_fleet_hint(icao24))
    return result


# ─── Route resolver ───────────────────────────────────────────────────────────

def resolve_route(callsign: str, icao24: str) -> tuple[FlightRoute | None, Aircraft, bool, dict | None]:
    """
    Try to determine the flight route from all available sources in order.
    Also returns any Aircraft metadata gathered as a side-effect of scraping FA,
    and the raw OpenSky flight record (for first_seen extraction) if available.

    Returns (route, aircraft, fa_only, opensky_record) where:
    - route is None if all sources failed
    - fa_only is True when the destination came from FlightAware scraping alone
    - opensky_record is the raw dict from OpenSky or None
    """
    fa_aircraft  = Aircraft()
    osn_record   = fetch_flight_record(icao24)

    # Source 1: OpenSky (most authoritative, but has ~1h delay and occasional downtime)
    if osn_record:
        route = fetch_route_from_opensky_record(osn_record)
        if route and route.dest_known:
            return route, fa_aircraft, False, osn_record

    partial_route = fetch_route_from_opensky_record(osn_record) if osn_record else None

    # Source 2: aviationstack
    route_as = fetch_route_from_aviationstack(callsign)
    if route_as and route_as.dest_known:
        # Preserve departure from OpenSky if aviationstack didn't have it
        if partial_route and partial_route.dep and not route_as.dep:
            route_as.dep = partial_route.dep
        return route_as, fa_aircraft, False, osn_record

    # Source 3: FlightAware scrape (also yields aircraft metadata for free)
    fa_result = fetch_from_flightaware(callsign)
    fa_aircraft = fa_result.aircraft
    if fa_result.route and fa_result.route.dest_known:
        if partial_route and partial_route.dep and not fa_result.route.dep:
            fa_result.route.dep = partial_route.dep
        return fa_result.route, fa_aircraft, True, osn_record   # fa_only=True

    # All sources exhausted — return partial route if at least dep is known
    partial = partial_route or FlightRoute()
    return (partial if partial.dep else None), fa_aircraft, False, osn_record


# ─── Alert email ──────────────────────────────────────────────────────────────

def _format_ts(ts: int | None) -> str:
    if not ts:
        return "–"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_email(
    callsign:     str,
    icao24:       str,
    route:        FlightRoute,
    aircraft:     Aircraft,
    first_seen:   int | None,
    pending_since: str | None = None,
) -> tuple[str, str]:
    """Return (subject, html_body) for the alert email."""

    is_poland = route.dest_is_poland
    dest_label = f"{route.arr} (Polska)" if is_poland else (route.arr or "unknown destination")
    subject    = f"✈️ [FlightTracker] {callsign} → {dest_label}"

    header_text    = "flight to Poland" if is_poland else f"flight ({route.arr or 'unknown destination'})"
    dest_row_label = "To 🇵🇱" if is_poland else "To"
    source_badge   = f'<span style="background:#6b7280;color:#fff;padding:2px 8px;border-radius:10px;font-size:11px">{route.source}</span>'
    now_str        = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def row(label: str, value: str, bg: str = "#f3f4f6", extra_td: str = "") -> str:
        return (
            f'<tr><td style="padding:9px 12px;background:{bg};font-weight:bold;width:40%">{label}</td>'
            f'<td style="padding:9px 12px;border:1px solid #e5e7eb{extra_td}">{value}</td></tr>'
        )

    rows = [
        row("Callsign", f'<strong style="font-size:16px">{callsign}</strong>'),
        row("ICAO24 (hex)", icao24, extra_td=";font-family:monospace"),
    ]

    if aircraft.model or aircraft.operator:
        label = aircraft.model or "unknown type"
        if aircraft.operator:
            label += f" – {aircraft.operator}"
        rows.append(row("Aircraft", label))

    if aircraft.reg:
        rows.append(row("Registration", aircraft.reg, extra_td=";font-family:monospace"))

    rows.append(row("From", route.dep or "unknown", bg="#dbeafe"))
    rows.append(row(dest_row_label, f"<strong>{airport_label(route.arr)}</strong> {source_badge}", bg="#dcfce7"))
    rows.append(row("Departure (est.)", _format_ts(first_seen)))
    rows.append(row("Arrival (est.)",   _format_ts(route.eta_ts)))

    if pending_since:
        rows.append(row(
            "Note",
            f"Destination confirmed after queuing (flight queued since {pending_since})",
            bg="#fef9c3",
        ))

    table_html = "\n".join(rows)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Flight Alert</title></head>
<body style="font-family:Arial,sans-serif;background:#f9fafb;margin:0;padding:20px">
  <div style="max-width:580px;margin:auto;background:#fff;border-radius:8px;
              box-shadow:0 1px 4px rgba(0,0,0,.12);overflow:hidden">
    <div style="background:#1a56db;padding:20px 24px">
      <h1 style="color:#fff;margin:0;font-size:20px">✈️ Flight Alert – {header_text}</h1>
    </div>
    <div style="padding:24px">
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        {table_html}
      </table>
      <div style="margin-top:20px">
        <a href="https://www.flightaware.com/live/flight/{callsign}"
           style="display:inline-block;background:#1a56db;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;margin-right:8px;font-size:13px">FlightAware</a>
        <a href="https://www.flightradar24.com/data/aircraft/{icao24}"
           style="display:inline-block;background:#e26f24;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;margin-right:8px;font-size:13px">FlightRadar24</a>
        <a href="https://globe.adsbexchange.com/?icao={icao24}"
           style="display:inline-block;background:#059669;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;margin-right:8px;font-size:13px">ADS-B Exchange</a>
        <a href="https://www.airnavradar.com/flight/{callsign}"
           style="display:inline-block;background:#374151;color:#fff;padding:9px 18px;
                  text-decoration:none;border-radius:5px;font-size:13px">AirNav</a>
      </div>
    </div>
    <div style="padding:12px 24px;background:#f3f4f6;font-size:11px;color:#9ca3af">
      Generated: {now_str}
    </div>
  </div>
</body></html>"""

    return subject, html


def _flight_key(callsign: str, route: FlightRoute, first_seen: int | None) -> str:
    dep_date = (
        datetime.fromtimestamp(first_seen, tz=timezone.utc).strftime("%Y-%m-%d")
        if first_seen
        else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    return f"{callsign}_{route.dep}_{route.arr}_{dep_date}"


def send_alert(
    callsign:      str,
    icao24:        str,
    route:         FlightRoute,
    first_seen:    int | None,
    state:         State,
    aircraft:      Aircraft,
    pending_since: str | None = None,
) -> str:
    """
    Send an alert email if not already sent for this flight.
    Returns 'sent', 'dedup', or 'error'.
    """
    key = _flight_key(callsign, route, first_seen)

    if state.already_alerted(key):
        print(f"[{callsign}] Already alerted for this flight — skipping")
        return "dedup"

    print(f"[{callsign}] 🚨 ALERT: {route.dep or '?'} → {route.arr} [{route.source}] — sending email...")
    subject, html = build_email(callsign, icao24, route, aircraft, first_seen, pending_since)

    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as srv:
            srv.login(EMAIL_FROM, EMAIL_PASS)
            srv.send_message(msg)
        state.mark_alerted(key)
        print(f"[{callsign}] ✓ Email sent to {EMAIL_TO}")
        return "sent"
    except Exception as e:
        print(f"[{callsign}] ✗ Email error: {e}")
        return "error"


# ─── Per-flight processing ────────────────────────────────────────────────────

def process_flight(callsign: str, icao24: str, state: State) -> str | None:
    """
    Resolve route and aircraft for one flight, then send an alert if appropriate.

    Returns 'sent', 'dedup', 'error', 'pending' (added to queue), or 'skip'.
    """
    print(f"\n[{callsign}] Resolving route (icao24={icao24})...")
    route, fa_aircraft, fa_only, osn_record = resolve_route(callsign, icao24)
    time.sleep(1)   # be polite to APIs

    if route is None:
        print(f"[{callsign}] No route data from any source — adding to pending queue")
        state.add_pending(icao24, callsign)
        return "pending"

    print(f"[{callsign}] Route: {route.dep or '?'} → {route.arr or '?'} [{route.source}]")

    if not should_alert(callsign, route):
        print(f"[{callsign}] Destination {route.arr or '(unknown)'} — skipping")
        return "skip"

    # FA-only Poland destination: queue for one confirmation run before alerting
    if fa_only and route.dest_is_poland:
        print(f"[{callsign}] FA-only destination {route.arr} — queuing for confirmation")
        state.add_pending(icao24, callsign, fa_unconfirmed_dest=route.arr)
        return "pending"

    aircraft   = resolve_aircraft(icao24, fa_aircraft, opensky_reachable=osn_record is not None)
    first_seen = osn_record.get("firstSeen") if osn_record else None

    return send_alert(callsign, icao24, route, first_seen, state, aircraft)


def process_pending_flight(
    icao24: str, entry: PendingEntry, state: State, airborne_icao24s: set[str]
) -> str:
    """
    Re-check a flight from the pending queue.
    Returns 'sent', 'dedup', 'error', 'still_pending', or 'skip'.
    """
    callsign = entry.callsign

    # Don't alert about a flight that has already landed
    if icao24 not in airborne_icao24s:
        print(f"[pending] {callsign} — no longer airborne, removing from queue")
        state.remove_pending(icao24)
        return "skip"

    print(f"[pending] {callsign} ({icao24}) — retrying route lookup...")
    route, fa_aircraft, fa_only, osn_record = resolve_route(callsign, icao24)
    time.sleep(1)

    # If we had an FA-unconfirmed Poland destination, check if it's now confirmed
    if entry.fa_unconfirmed_dest:
        if route is None or not route.dest_known:
            # No data from any source this run — keep waiting
            print(f"[pending] {callsign} — FA destination {entry.fa_unconfirmed_dest} still unconfirmed, keeping in queue")
            return "still_pending"
        if not fa_only:
            # Confirmed by a reliable source
            print(f"[pending] {callsign} — FA destination confirmed by {route.source}: {route.arr}")
        else:
            # Still only FA after another run — alert anyway to avoid indefinite delay
            print(f"[pending] {callsign} — FA destination still unconfirmed after retry, alerting anyway: {route.arr}")

    if route is None or not route.dest_known:
        print(f"[pending] {callsign} — destination still unknown")
        return "still_pending"

    print(f"[pending] {callsign} — route: {route.dep or '?'} → {route.arr}")

    if not should_alert(callsign, route):
        print(f"[pending] {callsign} — destination {route.arr} is not Poland — removing from queue")
        state.remove_pending(icao24)
        return "skip"

    aircraft      = resolve_aircraft(icao24, fa_aircraft, opensky_reachable=osn_record is not None)
    first_seen    = osn_record.get("firstSeen") if osn_record else None
    pending_since = entry.first_added[:16].replace("T", " ") + " UTC"

    result = send_alert(callsign, icao24, route, first_seen, state, aircraft, pending_since)
    if result in ("sent", "dedup"):
        state.remove_pending(icao24)
    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"{'='*60}\nFlight Alert Check — {run_time}\n{'='*60}")

    state       = State.load()
    alerts_sent = 0

    # Step 1: fetch live positions (needed for both pending and new flight checks)
    print("\n[main] Fetching live positions...")
    airborne = fetch_airborne_states()
    if not airborne:
        print("[main] No data from OpenSky — API may be unavailable")
        state.save()
        return

    airborne_icao24s = {s[0] for s in airborne}
    print(f"[main] Aircraft airborne: {len(airborne)}")

    # Step 1 (deferred): retry pending flights — needs airborne set to detect landings
    if state.pending:
        print(f"\n[pending] Checking {len(state.pending)} queued flights...")
        for icao24, entry in list(state.pending.items()):
            result = process_pending_flight(icao24, entry, state, airborne_icao24s)
            if result == "sent":
                alerts_sent += 1

    matches = [s for s in airborne if matches_tracked_prefix((s[1] or "").strip())]
    print(f"\n[main] Matching callsigns: {len(matches)}")
    for s in matches:
        print(f"       → {(s[1] or '').strip()} ({s[0]})")

    for s in matches:
        icao24   = s[0]
        callsign = (s[1] or "").strip()

        if icao24 in state.pending:
            print(f"\n[{callsign}] Already in pending queue — will retry next run")
            continue

        result = process_flight(callsign, icao24, state)
        if result == "sent":
            alerts_sent += 1

    state.save()
    print(f"\n{'='*60}\nDone — {alerts_sent} alert(s) sent\n{'='*60}")


if __name__ == "__main__":
    main()
