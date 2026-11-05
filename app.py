import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template
import requests

app = Flask(__name__)

CACHE_TTL = 30 * 60          # seconds a forecast counts as fresh
RETRY_DELAYS = [2, 5]        # seconds to wait after each 429 before retrying
FAILURE_COOLDOWN = 60        # seconds to skip all providers after both fail

MET_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
MET_USER_AGENT = "crag-weather/1.0 (https://github.com/pennydevelops/crag-weather)"
FORECAST_DAYS = 3
RAIN_WET_MM = 2              # MET fallback: this much rain in a day counts as "too wet"
FETCH_ERRORS = (requests.RequestException, KeyError, IndexError, ValueError)

# crag id -> {"days": [...], "fetched_at": time.monotonic() value, "provider": str}
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
REQUIRED_CRAG_KEYS = {"id", "name", "lat", "lon", "aspect", "region", "timezone"}


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


def rain_chance_from_mm(mm):
    """MET Norway has no rain probability here, so estimate one from the day's rain.

    Not a real probability: it just lands in the verdict() bands (20 / 60).
    """
    if mm <= 0:
        return 0
    if mm < RAIN_WET_MM:
        return 40
    return 80


def get_forecast_met(lat, lon, tz_name):
    """Ask MET Norway for hourly data and squash it into Open-Meteo's daily shape."""
    response = requests.get(
        MET_URL,
        params={"lat": round(lat, 4), "lon": round(lon, 4)},  # MET rejects 5+ decimals
        headers={"User-Agent": MET_USER_AGENT},  # MET returns 403 without one
        timeout=5,
    )
    response.raise_for_status()
    timeseries = response.json()["properties"]["timeseries"]

    tz = ZoneInfo(tz_name)
    by_date = {}  # local date -> temps, winds (m/s) and rain (mm) for that day
    for point in timeseries:
        local = datetime.fromisoformat(point["time"].replace("Z", "+00:00")).astimezone(tz)
        day = by_date.setdefault(local.date().isoformat(), {"temps": [], "winds": [], "rain": 0.0})
        data = point["data"]
        details = data["instant"]["details"]
        day["temps"].append(details["air_temperature"])
        day["winds"].append(details["wind_speed"])
        # Hourly points carry next_1_hours; the later 6-hourly ones only next_6_hours.
        period = data.get("next_1_hours") or data.get("next_6_hours") or {}
        day["rain"] += period.get("details", {}).get("precipitation_amount", 0.0)

    dates = sorted(by_date)[:FORECAST_DAYS]
    if not dates:
        raise ValueError("MET Norway returned no forecast data")
    rain_mm = [round(by_date[d]["rain"], 1) for d in dates]
    return {
        "time": dates,
        "temperature_2m_max": [max(by_date[d]["temps"]) for d in dates],
        "temperature_2m_min": [min(by_date[d]["temps"]) for d in dates],
        "precipitation_probability_max": [rain_chance_from_mm(mm) for mm in rain_mm],
        "wind_speed_10m_max": [round(max(by_date[d]["winds"]) * 3.6, 1) for d in dates],  # m/s -> km/h
        "precipitation_sum": rain_mm,
    }


def fetch_daily(crag):
    """Return (daily, provider): Open-Meteo first, MET Norway if that fails."""
    try:
        return get_forecast_with_retry(crag["lat"], crag["lon"]), "open-meteo"
    except FETCH_ERRORS as e:
        log.warning("open-meteo failed for %s (%s), trying met-norway", crag["id"], e)
    return get_forecast_met(crag["lat"], crag["lon"], crag["timezone"]), "met-norway"


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
    precip_mm = daily.get("precipitation_sum")  # only the MET fallback supplies this
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
            "precip_mm": precip_mm[i] if precip_mm else None,
            "wind_kmh": wind,
            "verdict": verdict(tmax, tmin, rain, wind),
        })
    return days


def get_crag_days(crag):
    """Return (days, age_seconds, stale, provider) for a crag, or None if we have nothing.

    Uses the cache while fresh, otherwise asks the providers. If both fail (or we
    are in the cooldown after a recent failure), falls back to stale data.
    """
    global last_failure
    entry = FORECAST_CACHE.get(crag["id"])
    now = time.monotonic()
    if entry and now - entry["fetched_at"] < CACHE_TTL:
        log.info("cache hit %s (age %ds, %s)", crag["id"], now - entry["fetched_at"], entry["provider"])
        return entry["days"], now - entry["fetched_at"], False, entry["provider"]

    in_cooldown = last_failure is not None and now - last_failure < FAILURE_COOLDOWN
    if not in_cooldown:
        log.info("cache miss, fetching %s", crag["id"])
        try:
            daily, provider = fetch_daily(crag)
            days = build_days(daily)
            FORECAST_CACHE[crag["id"]] = {
                "days": days, "fetched_at": time.monotonic(), "provider": provider,
            }
            log.info("fetched %s from %s", crag["id"], provider)
            return days, 0, False, provider
        except FETCH_ERRORS as e:
            # Only reached when every provider failed, so this starts the cooldown.
            last_failure = time.monotonic()
            log.warning("all providers failed for %s: %s", crag["id"], e)

    if entry:
        age = time.monotonic() - entry["fetched_at"]
        log.info("serving stale %s (age %ds, %s)", crag["id"], age, entry["provider"])
        return entry["days"], age, True, entry["provider"]
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

    days, age, stale, provider = result
    body = {"crag": crag["name"], "days": days, "provider": provider}
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