import os
import time
import sqlite3
import requests
from email.message import EmailMessage
import smtplib

CALLSIGN_PREFIX = "CMB"
MIN_SPEED = 50

OPENSKY_USER = os.environ["OPENSKY_USER"]
OPENSKY_PASS = os.environ["OPENSKY_PASS"]

SMTP_PASS = os.environ["SMTP_PASS"]
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_TO = os.environ["EMAIL_TO"]
EMAIL_FROM = os.environ["EMAIL_FROM"]

BASE = "https://opensky-network.org/api/states/all"
META_BASE = "https://opensky-network.org/api/metadata/aircraft/icao"

DB_PATH = "seen.db"

def get_db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with get_db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS seen (
                icao24 TEXT PRIMARY KEY
            )
            """
        )

def already_seen(icao):
    with get_db() as con:
        cur = con.execute("SELECT 1 FROM seen WHERE icao24=?", (icao,))
        return cur.fetchone() is not None

def mark_seen(icao):
    with get_db() as con:
        con.execute("INSERT OR IGNORE INTO seen VALUES (?)", (icao,))

def fetch_recent():
    r = requests.get(BASE, timeout=10)
    r.raise_for_status()
    return r.json()

def fetch_aircraft_meta(icao):
    try:
        r = requests.get(
            f"{META_BASE}/{icao}",
            auth=(OPENSKY_USER, OPENSKY_PASS),
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass

    return None

def send_mail(subject, body):
    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(EMAIL_FROM, SMTP_PASS)
        s.send_message(msg)
        
def main():
init_db()

    data = fetch_recent()
    flights = data.get("states", [])

    for f in flights:
        icao = f[0]
        callsign = (f[1] or "").strip()
        origin_country = f[2]
        longitude = f[5]
        latitude = f[6]
        baro_altitude = f[7]
        on_ground = f[8]
        velocity = f[9]
        heading = f[10]
        vertical_rate = f[11]
        geo_altitude = f[13]
        squawk = f[14]
        spi = f[15]
        position_source = f[16]

        if not callsign.startswith(CALLSIGN_PREFIX):
            continue

        if already_seen(icao):
            continue

        if on_ground or velocity is None or velocity < MIN_SPEED:
            continue

        mark_seen(icao)

        body = f"""
        TAKEOFF detected
        
        Callsign: {callsign}
        ICAO24: {icao}
        Origin country: {origin_country}
        
        Position:
          Latitude: {latitude}
          Longitude: {longitude}
          View on map: https://www.google.com/maps/search/?api=1&query={latitude},{longitude}
        
        Altitude:
          Barometric: {baro_altitude} m
          Geometric: {f[13]} m
        
        Velocity: {velocity} m/s
        Heading: {f[10]}°
        Vertical rate: {f[11]} m/s
        
        On ground: {on_ground}
        Squawk: {f[14]}
        SPI: {f[15]}
        Position source: {f[16]}
        
        Time detected: {time.ctime()}
        """

        send_mail("CMB aircraft departed", body)
        print(f"Alert sent for {callsign} ({icao})")

if __name__ == "__main__":
    main()
