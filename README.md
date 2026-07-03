# ✈️ Flight Tracker

Monitors cargo and military flights by callsign and sends an email alert when a tracked aircraft is heading to Poland — or is airborne at all, for selected operators.

## Tracked callsigns

| Prefix | Operator | Alert condition |
|--------|----------|-----------------|
| `CMB`  | USTRANSCOM (operated by Atlas Air, Kalitta Air, etc.) | destination = Poland |
| `ADB`  | Antonov Airlines | destination = Poland, or destination unknown |
| `VDA`  | Volga-Dnepr Airlines | destination = Poland |
| `CVK`  | CargoLogicAir | destination = Poland |

## How it works

[cron-job.org](https://cron-job.org) triggers the GitHub Actions workflow every 20 minutes via `workflow_dispatch`. The script:

1. Fetches live positions from OpenSky Network
2. Filters by callsign prefix
3. Resolves the destination using a cascade of sources: **OpenSky → aviationstack → FlightAware scrape**
4. If no destination is found yet, the flight is queued and retried each run for up to 26 hours
5. Sends an HTML email alert (deduped per flight — one email per flight, no matter how long it's airborne)

## Setup

### 1. Accounts

- [OpenSky Network](https://opensky-network.org) — free account required (destination data not available anonymously)
- [aviationstack.com](https://aviationstack.com/signup/free) — optional, free tier (1 000 req/month), improves destination detection
- Gmail — enable 2-step verification, then create an [app password](https://myaccount.google.com/apppasswords)

### 2. GitHub Secrets

**Settings → Secrets and variables → Actions:**

| Secret | Value |
|--------|-------|
| `OPENSKY_USERNAME` | OpenSky login |
| `OPENSKY_PASSWORD` | OpenSky password |
| `AVIATIONSTACK_KEY` | aviationstack API key (optional) |
| `EMAIL_FROM` | sender address |
| `EMAIL_TO` | recipient address |
| `EMAIL_PASSWORD` | Gmail app password |

### 3. cron-job.org

1. Generate a GitHub personal access token (scope: `workflow`) at **Settings → Developer settings → Personal access tokens**
2. Create a new job on cron-job.org:
   - **URL:** `https://api.github.com/repos/YOUR_USERNAME/YOUR_REPO/actions/workflows/flight_alerts.yml/dispatches`
   - **Method:** `POST`  
   - **Body:** `{"ref":"main"}`
   - **Headers:** `Accept: application/vnd.github+json`, `Authorization: Bearer YOUR_TOKEN`
   - **Schedule:** every 20 minutes

## GitHub Actions minutes

Private repos get **2 000 free minutes/month**. With a 20-minute interval, that's ~1 440 minutes/month — comfortably within the limit, as long as cron-job.org is the only trigger. The built-in `schedule:` in the workflow is intentionally set to every 3 hours as a fallback only; running both at 20 minutes would double usage and exhaust the quota early.

Public repos have unlimited minutes.
