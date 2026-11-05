import json
import logging
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template
import requests

app = Flask(__name__)

CACHE_TTL = 30 * 60          # seconds a forecast counts as fresh
RETRY_DELAYS = [2, 5]        # seconds to wait after each 429 before retrying
FAILURE_COOLDOWN = 60        # seconds to skip Open-Meteo after a failed refresh

# crag id -> {"days": [...], "fetched_at": time.monotonic() value}
# Lives in this process's memory only, so it is empty after every restart.
FORECAST_CACHE = {}
last_failure = None

# Own logger with its own handler: the root logger only shows warnings and up,
# so plain logging.info() lines would be invisible under gunicorn.
# propagate = True also hands each record to the root logger's handlers, which
# is how OpenTelemetry's log handler picks them up for Dynatrace.
log = logging.getLogger("crag-weather")
log.setLevel(logging.INFO)
log.propagate = True
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_handler)

CRAGS_FILE = Path(__file__).parent / "crags.json"
REQUIRED_CRAG_KEYS = {"id", "name", "lat", "lon", "aspect", "region"}


def load_crags():
    """Read crags.json and check every crag has the fields the app needs."""
    try:
        with open(CRAGS_FILE, encoding="utf-8") as f:
            crags = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Crags file not found: {CRAGS_FILE}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{CRAGS_FILE.name} is not valid JSON: {e}")

    if not isinstance(crags, list):
        raise RuntimeError(f"{CRAGS_FILE.name} must contain a list of crags")
    for i, crag in enumerate(crags):
        missing = REQUIRED_CRAG_KEYS - set(crag)
        if missing:
            raise RuntimeError(
                f"Crag #{i + 1} in {CRAGS_FILE.name} is missing: {', '.join(sorted(missing))}"
            )
    return crags


# Edit crags.json to use your own crags, then restart the app.
CRAGS = load_crags()


def get_forecast(lat, lon):
    """Ask Open-Meteo for the next 3 days of weather at this location."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,wind_speed_10m_max",
        "timezone": "auto",
        "forecast_days": 3,
    }
    response = requests.get(url, params=params, timeout=5)
    response.raise_for_status()  # raise an error if the request failed
    return response.json()["daily"]


def get_forecast_with_retry(lat, lon):
    """Call get_forecast, retrying after 2s and then 5s if Open-Meteo says 429."""
    for delay in RETRY_DELAYS:
        try:
            return get_forecast(lat, lon)
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 429:
                raise
            log.warning("Open-Meteo returned 429, retrying in %ss", delay)
            time.sleep(delay)
    return get_forecast(lat, lon)  # last attempt: any error propagates


def verdict(temp_max, temp_min, rain_chance, wind):
    """Turn numbers into a simple decision."""
    if rain_chance > 60 or wind > 40:
        return "Too wet / windy"
    if rain_chance > 20 or wind > 25 or temp_max > 28 or temp_min < 2:
        return "Marginal"
    return "Climbable"


@app.route("/")
def home():
    return render_template("index.html", crags=CRAGS)


@app.route("/api/crags")
def list_crags():
    return jsonify(CRAGS)


def build_days(daily):
    days = []
    for i, date in enumerate(daily["time"]):
        rain = daily["precipitation_probability_max"][i]
        wind = daily["wind_speed_10m_max"][i]
        tmax = daily["temperature_2m_max"][i]
        tmin = daily["temperature_2m_min"][i]
        days.append({
            "date": date,
            "temp_max": tmax,
            "temp_min": tmin,
            "rain_chance": rain,
            "wind_kmh": wind,
            "verdict": verdict(tmax, tmin, rain, wind),
        })
    return days


def get_crag_days(crag):
    """Return (days, age_seconds, stale) for a crag, or None if we have nothing.

    Uses the cache while fresh, otherwise asks Open-Meteo. If that fails (or we
    are in the cooldown after a recent failure), falls back to stale data.
    """
    global last_failure
    entry = FORECAST_CACHE.get(crag["id"])
    now = time.monotonic()
    if entry and now - entry["fetched_at"] < CACHE_TTL:
        log.info("cache hit %s (age %ds)", crag["id"], now - entry["fetched_at"])
        return entry["days"], now - entry["fetched_at"], False

    in_cooldown = last_failure is not None and now - last_failure < FAILURE_COOLDOWN
    if not in_cooldown:
        log.info("cache miss, fetching %s", crag["id"])
        try:
            daily = get_forecast_with_retry(crag["lat"], crag["lon"])
            days = build_days(daily)
            FORECAST_CACHE[crag["id"]] = {"days": days, "fetched_at": time.monotonic()}
            return days, 0, False
        except (requests.RequestException, KeyError, IndexError) as e:
            last_failure = time.monotonic()
            log.warning("Forecast refresh failed for %s: %s", crag["id"], e)

    if entry:
        age = time.monotonic() - entry["fetched_at"]
        log.info("serving stale %s (age %ds)", crag["id"], age)
        return entry["days"], age, True
    return None


@app.route("/api/crags/<crag_id>/forecast")
def crag_forecast(crag_id):
    crag = next((c for c in CRAGS if c["id"] == crag_id), None)
    if crag is None:
        return jsonify({"error": "Unknown crag"}), 404

    result = get_crag_days(crag)
    if result is None:
        return jsonify({
            "error": "Forecast temporarily unavailable, please try again in a few minutes."
        }), 503

    days, age, stale = result
    body = {"crag": crag["name"], "days": days}
    if stale:
        body["stale"] = True
        body["age_seconds"] = round(age)
    return jsonify(body)

@app.route("/api/break")
def break_app():
       raise ValueError("Deliberate test error for Dynatrace")

def warm_cache():
    """Fetch every crag once so the first visitor doesn't wait on Open-Meteo."""
    for crag in CRAGS:
        get_crag_days(crag)


# Background thread: the app starts serving straight away instead of waiting.
threading.Thread(target=warm_cache, daemon=True).start()

if __name__ == "__main__":
    app.run(debug=True)