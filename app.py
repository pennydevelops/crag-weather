import json
from pathlib import Path

from flask import Flask, jsonify, render_template
import requests

app = Flask(__name__)

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
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()  # raise an error if the request failed
    return response.json()["daily"]


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


@app.route("/api/crags/<crag_id>/forecast")
def crag_forecast(crag_id):
    crag = next((c for c in CRAGS if c["id"] == crag_id), None)
    if crag is None:
        return jsonify({"error": "Unknown crag"}), 404

    daily = get_forecast(crag["lat"], crag["lon"])
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
    return jsonify({"crag": crag["name"], "days": days})

@app.route("/api/break")
def break_app():
       raise ValueError("Deliberate test error for Dynatrace")

if __name__ == "__main__":
    app.run(debug=True)