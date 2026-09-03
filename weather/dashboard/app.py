"""
Weather-agent dashboard: a small Flask app to WATCH what the Agent Bricks
agent has been asked and what it predicted, via the `weather_predictions`
audit table the weather MCP server writes to Lakebase.

This app never calls the prediction tools on the agent's behalf - it reads
the same Lakebase table the MCP server writes (`prediction_log.py`), plus a
live "look up a location" panel that hits Open-Meteo directly through the
same `weather_client.py` adapter.

Deploy this as its OWN Databricks App, separate from the MCP server app:
one app serves MCP tool calls to the agent, this one serves humans.

Run locally:
    python app.py        # serves the UI on :8001
"""

import os

from flask import Flask, jsonify, render_template, request

import prediction_log
import weather_client

app = Flask(__name__)

DEFAULT_LOCATION = os.environ.get("WEATHER_DEFAULT_LOCATION", "San Diego, CA")
DEFAULT_UNITS = os.environ.get("WEATHER_DEFAULT_UNITS", "imperial")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page)."""
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/")
def index():
    """Dashboard UI: recent agent predictions plus a live lookup panel."""
    return render_template("index.html", default_location=DEFAULT_LOCATION)


@app.route("/api/predictions")
def api_predictions():
    """
    Recent predictions logged by the MCP server, newest first.

    Returns an empty list with a `note` (not a 500) when the Lakebase table
    is missing or unreachable, so the dashboard still renders on a fresh
    workspace where the schema hasn't been applied yet.
    """
    limit = int(request.args.get("limit", 25))
    try:
        rows = prediction_log.recent(limit)
        return jsonify({"count": len(rows), "predictions": rows})
    except Exception as exc:  # noqa: BLE001
        return jsonify(
            {
                "count": 0,
                "predictions": [],
                "note": (
                    "Prediction log unavailable "
                    f"({type(exc).__name__}: {exc}). Apply "
                    "schema_weather_predictions.sql to your Lakebase database "
                    "and confirm the LAKEBASE_URL secret is set."
                ),
            }
        )


@app.route("/api/current")
def api_current():
    """Live current conditions for a location, via the same adapter the agent uses."""
    location = request.args.get("location", DEFAULT_LOCATION)
    units = request.args.get("units", DEFAULT_UNITS)
    try:
        return jsonify(weather_client.get_current_weather(location, units))
    except weather_client.WeatherError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/forecast")
def api_forecast():
    """Short forecast for the location panel."""
    location = request.args.get("location", DEFAULT_LOCATION)
    days = int(request.args.get("days", 5))
    units = request.args.get("units", DEFAULT_UNITS)
    try:
        return jsonify(weather_client.get_forecast(location, days, units))
    except weather_client.WeatherError as exc:
        return jsonify({"error": str(exc)}), 400


if __name__ == "__main__":
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8001)))
    app.run(host="0.0.0.0", port=port)
