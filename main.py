import os
import time
import sqlite3
import requests
from email.message import EmailMessage
import smtplib

CALLSIGN_PREFIX = "CMB"

OPENSKY_USER = os.environ["OPENSKY_USER"]
OPENSKY_PASS = os.environ["OPENSKY_PASS"]

SMTP_PASS = os.environ["SMTP_PASS"]

EMAIL_TO = os.environ["EMAIL_TO"]
EMAIL_FROM = os.environ["EMAIL_FROM"]

BASE = "https://opensky-network.org/api/states/all"

DB = "seen.db"

def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen (
        icao24 TEXT PRIMARY KEY
    )
    """)
    con.commit()
    con.close()

def already_seen(icao):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM seen WHERE icao24=?", (icao,))
    result = cur.fetchone() is not None
    con.close()
    return result

def mark_seen(icao):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO seen VALUES (?)", (icao,))
    con.commit()
    con.close()

def send_mail(subject, body):
    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(EMAIL_FROM, SMTP_PASS)
        s.send_message(msg)

def fetch_recent():
    r = requests.get(BASE, timeout=10)
    r.raise_for_status()
    return r.json()

def main():
    if not os.path.exists(DB):
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

        if not callsign.startswith(CALLSIGN_PREFIX):
            continue

        if already_seen(icao):
            continue

        if on_ground or velocity is None or velocity < 50:
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
