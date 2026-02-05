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

BASE = "https://opensky-network.org/api"

DB = "seen.db"

def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen (
        icao24 TEXT PRIMARY KEY,
        ts INTEGER
    )
    """)
    con.commit()
    con.close()

def already_seen(icao):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM seen WHERE icao24=?", (icao,))
    return cur.fetchone() is not None

def mark_seen(icao, ts):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (icao, ts))
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
    now = int(time.time())
    begin = now - 180

    r = requests.get(
        f"{BASE}/states/all",
        # params={"begin": begin, "end": now},
        # auth=(OPENSKY_USER, OPENSKY_PASS),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

def main():
    init_db()

    flights = fetch_recent()

    for f in flights:
        callsign = (f[1] or "").strip()

        if not callsign.startswith(CALLSIGN_PREFIX):
            continue

        icao = f["icao24"]

        if already_seen(icao):
            continue

        mark_seen(icao, f["firstSeen"])

        body = (
            f"TAKEOFF detected\n\n"
            f"Callsign: {callsign}\n"
            f"From: {f['estDepartureAirport']}\n"
            f"To: {f['estArrivalAirport']}\n"
            f"Time: {time.ctime(f['firstSeen'])}\n"
        )

        send_mail("CMB aircraft departed", body)

if __name__ == "__main__":
    main()
