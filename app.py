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
PAST_DAYS = 2                # days before today that Open-Meteo also sends (for the rain rule)

# --- Scoring settings: change these numbers to retune the score. -------------
RAIN_WEIGHT = 0.8            # points lost per 1% rain probability
WIND_FREE_KMH = 20           # wind up to this costs nothing...
WIND_WEIGHT = 1.5            # ...above it, points lost per extra km/h
IDEAL_TEMP_C = 13            # the felt temperature that costs nothing
TEMP_WEIGHT = 2.5            # points lost per degree away from the ideal
SEEPAGE_MM = 5               # more rain than this over the 2 days before => wet rock...
SEEPAGE_PENALTY = 20         # ...on these aspects, which lose this many points
SEEPAGE_ASPECTS = {"N", "NE", "NW"}
CLIMBABLE_MIN = 70           # score 70 and up is "Climbable"
MARGINAL_MIN = 40            # 40 to 69 is "Marginal"; below that is "Poor"

# Degrees added to the air temperature by the sun on a clear day, per aspect.
ASPECT_ADJUSTMENT = {"S": 6, "SE": 4, "SW": 4, "E": 2, "W": 2, "NE": -1, "NW": -1, "N": -3}
# "all" means every aspect, in this order. The order also breaks ties between aspects.
ALL_ASPECTS = ["S", "SE", "SW", "E", "W", "NE", "NW", "N"]
ASPECT_NAMES = {
    "S": "south", "SE": "south-east", "SW": "south-west", "E": "east",
    "W": "west", "NE": "north-east", "NW": "north-west", "N": "north",
}

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
# "aspect" is not required: leaving it out (or empty) means the aspect is unknown.
REQUIRED_CRAG_KEYS = {"id", "name", "lat", "lon", "region", "timezone"}


def parse_aspects(value):
    """Turn the aspect from crags.json into a list of compass points.

    Accepts "SE", ["N", "S"], "all", or empty/missing (unknown, which gives []).
    Raises ValueError for anything else, so a typo fails loudly at startup.
    """
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str):
        if value.strip().lower() == "all":
            return list(ALL_ASPECTS)
        value = [value]  # a single direction is just a list of one
    if not isinstance(value, list):
        raise ValueError(
            f'aspect must be a direction, a list of directions, or "all", not {value!r}'
        )
    aspects = []
    for item in value:
        direction = item.strip().upper() if isinstance(item, str) else item
        if direction not in ALL_ASPECTS:
            raise ValueError(
                f'unknown aspect {item!r} (use {", ".join(ALL_ASPECTS)} or "all")'
            )
        if direction not in aspects:  # ignore repeats, keep the order given
            aspects.append(direction)
    return aspects


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
        try:
            parse_aspects(crag.get("aspect"))  # only checking it here
        except ValueError as e:
            raise RuntimeError(f"Crag #{i + 1} ({crag['id']}) in {CRAGS_FILE.name}: {e}")
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
                 "precipitation_probability_max,wind_speed_10m_max,"
                 "cloud_cover_mean,precipitation_sum",
        "timezone": "auto",
        "forecast_days": 3,
        "past_days": PAST_DAYS,  # also send the days before today
    }
    response = requests.get(url, params=params, timeout=5)
    response.raise_for_status()  # raise an error if the request failed
    return drop_past_days(response.json()["daily"])


def drop_past_days(daily):
    """Work out the rain over the 2 days before each forecast day, then remove the past days.

    Open-Meteo sends the past days first, so each list is 5 long (2 past + today + 2 more).
    Cutting them off leaves lists of 3, the same shape the rest of the code expects.
    """
    if len(daily["time"]) <= PAST_DAYS:
        raise ValueError("Open-Meteo returned no forecast days")
    rain = daily["precipitation_sum"]
    past_rain = []
    for i in range(PAST_DAYS, len(rain)):
        window = rain[i - PAST_DAYS:i]  # the 2 days just before day i
        past_rain.append(None if None in window else round(sum(window), 1))
    trimmed = {key: values[PAST_DAYS:] for key, values in daily.items()}
    trimmed["past_rain_mm"] = past_rain
    return trimmed


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

    Not a real probability: 0 / 40 / 80 are picked to land in clearly different score bands.
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
    by_date = {}  # local date -> first local time, temps, winds (m/s) and rain (mm)
    for point in timeseries:
        local = datetime.fromisoformat(point["time"].replace("Z", "+00:00")).astimezone(tz)
        day = by_date.setdefault(local.date().isoformat(), {
            "first": local.strftime("%H:%M"), "temps": [], "winds": [], "clouds": [], "rain": 0.0,
        })
        data = point["data"]
        details = data["instant"]["details"]
        day["temps"].append(details["air_temperature"])
        day["winds"].append(details["wind_speed"])
        if details.get("cloud_area_fraction") is not None:
            day["clouds"].append(details["cloud_area_fraction"])
        # Hourly points carry next_1_hours; the later 6-hourly ones only next_6_hours.
        period = data.get("next_1_hours") or data.get("next_6_hours") or {}
        day["rain"] += period.get("details", {}).get("precipitation_amount", 0.0)

    dates = sorted(by_date)[:FORECAST_DAYS]
    if not dates:
        raise ValueError("MET Norway returned no forecast data")
    rain_mm = [round(by_date[d]["rain"], 1) for d in dates]
    # MET starts at the current hour, so only the first day can be partial.
    first = by_date[dates[0]]["first"]
    from_time = [first if first != "00:00" else None] + [None] * (len(dates) - 1)
    return {
        "time": dates,
        "temperature_2m_max": [max(by_date[d]["temps"]) for d in dates],
        "temperature_2m_min": [min(by_date[d]["temps"]) for d in dates],
        "precipitation_probability_max": [rain_chance_from_mm(mm) for mm in rain_mm],
        "wind_speed_10m_max": [round(max(by_date[d]["winds"]) * 3.6, 1) for d in dates],  # m/s -> km/h
        "precipitation_sum": rain_mm,
        "cloud_cover_mean": [
            round(sum(c) / len(c)) if c else None
            for c in (by_date[d]["clouds"] for d in dates)
        ],
        # MET only forecasts, so there is no record of rain before now: unavailable.
        "past_rain_mm": [None] * len(dates),
        "from_time": from_time,
    }


def fetch_daily(crag):
    """Return (daily, provider): Open-Meteo first, MET Norway if that fails."""
    try:
        return get_forecast_with_retry(crag["lat"], crag["lon"]), "open-meteo"
    except FETCH_ERRORS as e:
        log.warning("open-meteo failed for %s (%s), trying met-norway", crag["id"], e)
    return get_forecast_met(crag["lat"], crag["lon"], crag["timezone"]), "met-norway"


def score_aspect(temp_max, rain_chance, wind, cloud_cover, aspect, past_rain_mm):
    """Score one day for one aspect (aspect=None means unknown).

    Starts from 100 and works out how many points each factor takes off.
    Returns the penalties, the felt temperature and the unrounded total.
    """
    # Sun factor: 1.0 on a clear day, 0.0 under full cloud. No cloud data counts as no sun.
    sun = (100 - cloud_cover) / 100 if cloud_cover is not None else 0
    # Felt temperature: the sun only adds the aspect's bonus (or shade penalty);
    # clouds shrink that bonus, but they don't change the air temperature itself.
    felt = temp_max + ASPECT_ADJUSTMENT.get(aspect, 0) * sun

    # Wet rock: lots of rain recently AND a damp-prone aspect. If we don't know
    # how much rain fell (past_rain_mm is None), no penalty is applied.
    wet_rock = (
        aspect in SEEPAGE_ASPECTS and past_rain_mm is not None and past_rain_mm > SEEPAGE_MM
    )
    penalties = {
        "rain": rain_chance * RAIN_WEIGHT,
        "wind": max(0, wind - WIND_FREE_KMH) * WIND_WEIGHT,  # max(0, ...) stops negatives
        "temperature": abs(felt - IDEAL_TEMP_C) * TEMP_WEIGHT,  # too hot or too cold both cost
        "seepage": SEEPAGE_PENALTY if wet_rock else 0,
    }
    return {"total": 100 - sum(penalties.values()), "felt": felt, "penalties": penalties}


def band_for(score):
    """Turn a whole-number score into a band name."""
    if score >= CLIMBABLE_MIN:
        return "Climbable"
    if score >= MARGINAL_MIN:
        return "Marginal"
    return "Poor"


def score_day(temp_max, rain_chance, wind, cloud_cover, aspects, past_rain_mm):
    """Score a day for every aspect the crag has and report the best one.

    `aspects` is a list like ["N", "S"], or [] when the aspect is unknown.
    """
    # Unknown aspect: score once, with no sun adjustment and no wet-rock penalty.
    results = [
        (a, score_aspect(temp_max, rain_chance, wind, cloud_cover, a, past_rain_mm))
        for a in (aspects or [None])
    ]
    # max() returns the first of any equal scores, so the order in crags.json breaks ties.
    # Scores are compared to 1 decimal so tiny rounding noise doesn't count as a difference.
    best_aspect, best = max(results, key=lambda r: round(r[1]["total"], 1))

    # Every aspect scoring the same means orientation makes no difference today.
    all_equal = len(results) > 1 and all(
        round(r["total"], 1) == round(best["total"], 1) for _, r in results
    )
    if not aspects:
        aspect, label = None, None
    elif all_equal:
        aspect, label = None, "any sector"
    else:
        aspect, label = best_aspect, f"{ASPECT_NAMES[best_aspect]}-facing sectors"

    score = round(max(0, min(100, best["total"])))  # keep it between 0 and 100
    return {
        "score": score,
        "band": band_for(score),
        "penalties": {name: round(points, 1) for name, points in best["penalties"].items()},
        "felt_temp": round(best["felt"], 1),
        "aspect": aspect,
        "aspect_label": label,
        "aspect_known": bool(aspects),
    }


@app.route("/")
def home():
    return render_template("index.html", crags=CRAGS)


@app.route("/api/crags")
def list_crags():
    return jsonify(CRAGS)


def build_days(daily, aspects):
    """Turn a provider's daily lists into one dictionary per day, with its score."""
    precip_mm = daily["precipitation_sum"]
    past_rain = daily["past_rain_mm"]
    clouds = daily["cloud_cover_mean"]
    from_time = daily.get("from_time")  # only the MET fallback supplies this
    days = []
    for i, date in enumerate(daily["time"]):
        rain = daily["precipitation_probability_max"][i]
        wind = daily["wind_speed_10m_max"][i]
        tmax = daily["temperature_2m_max"][i]
        tmin = daily["temperature_2m_min"][i]
        days.append({
            "date": date,
            "from_time": from_time[i] if from_time else None,
            "temp_max": tmax,
            "temp_min": tmin,
            "rain_chance": rain,
            "precip_mm": precip_mm[i],
            "past_rain_mm": past_rain[i],  # None = unavailable from this provider
            "cloud_cover": clouds[i],
            "wind_kmh": wind,
            **score_day(tmax, rain, wind, clouds[i], aspects, past_rain[i]),
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
            days = build_days(daily, parse_aspects(crag.get("aspect")))
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