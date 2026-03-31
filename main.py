import os
import time
import sqlite3
import requests
from email.message import EmailMessage
import smtplib

AN124_TYPECODES = {"A124", "AN-124", "AN124"}
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

DB_PATH = "seen.db"

def get_db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with get_db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS seen (
                icao24 TEXT PRIMARY KEY,
                last_alert INTEGER
            )
            """
        )

        cols = {
            row[1]
            for row in con.execute("PRAGMA table_info(seen)")
        }

        if "last_alert" not in cols:
            con.execute("ALTER TABLE seen ADD COLUMN last_alert INTEGER")

def already_seen(icao):
    COOLDOWN = 6 * 3600  # 6 godzin

    with get_db() as con:
        cur = con.execute(
            "SELECT last_alert FROM seen WHERE icao24=?",
            (icao,),
        )
        row = cur.fetchone()

    if not row or row[0] is None:
        return False

    return time.time() - row[0] < COOLDOWN

def mark_seen(icao):
    with get_db() as con:
        con.execute(
            "INSERT OR REPLACE INTO seen(icao24, last_alert) VALUES (?,?)",
            (icao, int(time.time())),
        )

def fetch_recent():
    r = requests.get(BASE, timeout=10)
    r.raise_for_status()
    return r.json()

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

        if callsign:
            flightaware_url = f"https://www.flightaware.com/live/flight/{callsign}"
        else:
            flightaware_url = "N/A"

        is_cmb = callsign.startswith(CALLSIGN_PREFIX)
        
        is_an124 = False
        
        callsign_upper = callsign.upper()
        
        is_an124 = (
            callsign_upper.startswith("ADB")
            or callsign_upper.startswith("CVK")
        )
        
        if not (is_cmb or is_an124):
            continue
        
        if already_seen(icao):
            continue
        
        if on_ground or velocity is None or velocity < MIN_SPEED:
            continue
        
        mark_seen(icao)

        maps_url = f"https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"

        body = f"""
        TAKEOFF detected
        
        Callsign: {callsign}
        FlightAware: {flightaware_url if flightaware_url else "N/A"}
        ICAO24: {icao}
        Origin country: {origin_country}
        
        Position:
          Latitude: {latitude}
          Longitude: {longitude}
          Map: {maps_url}
        
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

        subject = (
            "Antonov An-124 detected"
            if is_an124
            else "CMB aircraft departed"
        )

        send_mail(subject, body)
        print(f"Alert sent for {callsign} ({icao})")

if __name__ == "__main__":
    main()
