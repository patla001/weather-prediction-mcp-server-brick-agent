"""
Weather-prediction MCP server.

Exposes weather tools over MCP (Model Context Protocol) so a Databricks
Agent Bricks agent can call them like any other tool:

    Core (required):
        - get_current_weather(location, units)
        - get_forecast(location, days, units)
        - predict_umbrella_needed(location, date, units)

    Stretch:
        - get_travel_recommendation(location, date, units)
        - get_severe_weather_alerts(location)
        - compare_cities(locations, date, units)
        - get_recent_predictions(limit)
        - get_current_user()

Data comes from Open-Meteo (keyless, ~10k calls/day fair use) with severe
weather alerts from NOAA's National Weather Service API (keyless, US only).
Because neither API needs credentials, there is nothing to store in a
Databricks secret scope for the weather path itself; the one secret this
project does read - the Lakebase URL used by the optional prediction log -
goes through `WorkspaceClient().secrets.get_secret()` in lakebase.py,
never through a hardcoded string or a committed .env.

Layering (same split as Day 3's alpaca_mcp_server.py / alpaca_broker.py):
    weather_client.py  - every HTTP call and response parse (the adapter)
    forecast_rules.py  - the derived judgment: thresholds and reasoning
    weather_mcp_server.py (this file) - thin @mcp.tool wrappers + errors

Deploy this as its own Databricks App using the app.yaml next to this file
(https://docs.databricks.com/aws/en/agents/mcp-tools/custom-mcp), then
register the app URL as an external MCP server for your agent.

Run locally:
    python weather_mcp_server.py      # serves streamable HTTP on :8000
"""

import logging
import os
from contextvars import ContextVar

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

import forecast_rules
import prediction_log
import weather_client
from weather_client import WeatherError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

DEFAULT_UNITS = os.environ.get("WEATHER_DEFAULT_UNITS", "imperial")
MAX_COMPARE_CITIES = int(os.environ.get("WEATHER_MAX_COMPARE_CITIES", "5"))

# Databricks Apps forward the calling user's identity in request headers;
# capture it so predictions can be logged per user (same pattern as Day 3).
_request_context: ContextVar[dict] = ContextVar("request_context", default={})

mcp = FastMCP("weather-prediction")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Capture the HTTP headers carrying end-user identity."""

    async def dispatch(self, request: Request, call_next):
        _request_context.set(
            {
                "x-forwarded-user": request.headers.get("x-forwarded-user"),
                "x-forwarded-email": request.headers.get("x-forwarded-email"),
            }
        )
        return await call_next(request)


def _end_user_email() -> str | None:
    """The calling user's email when running as a Databricks App, else None."""
    headers = _request_context.get() or {}
    return headers.get("x-forwarded-user") or headers.get("x-forwarded-email")


def _error(exc: Exception, location: str = "") -> dict:
    """
    Turn any failure into a clean, agent-readable error dict.

    WeatherError messages are written for a person to read ("Could not find
    a location matching 'Chicagoo'"); anything else is logged with a
    traceback server-side and reported generically, so the agent never
    receives a stack trace.
    """
    if isinstance(exc, WeatherError):
        message = str(exc)
    else:
        logger.exception("Unexpected failure for location=%r", location)
        message = f"Unexpected error while fetching weather data: {exc}"
    return {
        "status": "error",
        "message": message,
        "location_requested": location or None,
        "hint": (
            "Ask the user to confirm the location (city name, US ZIP code, or "
            "'latitude,longitude') or to try again shortly. Do not guess the weather."
        ),
    }


def _ok(payload: dict) -> dict:
    return {"status": "success", **payload}


# --------------------------------------------------------------------------
# Required tools
# --------------------------------------------------------------------------

@mcp.tool
def get_current_weather(location: str, units: str = DEFAULT_UNITS) -> dict:
    """
    Get current observed weather conditions for a location.

    Args:
        location: City name ("Chicago", "Austin, TX", "Paris"), a US ZIP
            code ("60601"), or "latitude,longitude" ("41.85,-87.65").
        units: "imperial" (°F, mph, inches - default) or "metric"
            (°C, km/h, mm).

    Returns:
        A dict with status, location, observed_at (local ISO timestamp),
        temperature, feels_like, humidity_pct, conditions, wind_speed,
        wind_gusts, is_daytime, and the units used. On failure, a dict with
        status "error" and a human-readable message.
    """
    try:
        return _ok(weather_client.get_current_weather(location, units))
    except Exception as exc:  # noqa: BLE001 - tools return errors, never raise
        return _error(exc, location)


@mcp.tool
def get_forecast(location: str, days: int = 3, units: str = DEFAULT_UNITS) -> dict:
    """
    Get a multi-day daily forecast for a location.

    Args:
        location: City name, US ZIP code, or "latitude,longitude".
        days: Number of days to forecast, starting today, 1-16 (default 3).
        units: "imperial" (default) or "metric".

    Returns:
        A dict with status, location, timezone, and days - a list of daily
        dicts each with date, conditions, temp_high, temp_low,
        precipitation_chance_pct, precipitation_amount, wind_speed_max,
        wind_gusts_max, sunrise and sunset. On failure, a dict with status
        "error" and a human-readable message.
    """
    try:
        return _ok(weather_client.get_forecast(location, days, units))
    except Exception as exc:  # noqa: BLE001
        return _error(exc, location)


@mcp.tool
def predict_umbrella_needed(
    location: str, date: str = "", units: str = DEFAULT_UNITS
) -> dict:
    """
    Predict whether someone should carry an umbrella, with the reasoning.

    This is a judgment call, not a passthrough of the raw forecast. It
    combines the daily precipitation probability, the daily precipitation
    total, the precipitation type, and the hour-by-hour probabilities:

      * "yes"   - precipitation chance >= 40%, or >= 0.10 in of rain expected.
      * "maybe" - chance >= 25%, or >= 0.02 in expected.
      * "no"    - below both thresholds.

    Two overrides apply. Snow or freezing precipitation returns
    umbrella_needed = false with a "waterproof coat and boots" note, because
    an umbrella is the wrong tool. Wind gusts >= 25 mph downgrade the advice
    to a hooded rain jacket, because an umbrella will invert. The tool also
    reports the wet window (which daytime hours are at or above a 40% hourly
    chance) so the answer can say *when* to expect rain, and a confidence
    level that drops for forecasts a week or more out.

    Args:
        location: City name, US ZIP code, or "latitude,longitude".
        date: "today" (default), "tomorrow", or an ISO date "YYYY-MM-DD"
            within the next 16 days, interpreted in the location's local
            timezone.
        units: "imperial" (default) or "metric".

    Returns:
        A dict with status, location, date, umbrella_needed (bool), verdict
        ("yes"/"maybe"/"no"), confidence, precipitation_chance_pct,
        precipitation_amount, rain_window, better_alternative, reasoning,
        notes, and thresholds_used. On failure, a dict with status "error"
        and a human-readable message.
    """
    try:
        day_data = weather_client.get_day_forecast(location, date, units)
        verdict = forecast_rules.umbrella_verdict(
            day_data["daily"],
            day_data["hourly"],
            day_data["units"],
            day_data["days_ahead"],
        )
        result = _ok(
            {
                "location": day_data["location"],
                "date": day_data["date"],
                "timezone": day_data["timezone"],
                "units": day_data["units"],
                "source": day_data["source"],
                **verdict,
            }
        )
        prediction_log.record(
            tool="predict_umbrella_needed",
            location=day_data["location"],
            target_date=day_data["date"],
            verdict=f"umbrella: {verdict['verdict']}",
            user_email=_end_user_email(),
            payload=result,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        return _error(exc, location)


# --------------------------------------------------------------------------
# Stretch tools
# --------------------------------------------------------------------------

@mcp.tool
def get_travel_recommendation(
    location: str, date: str = "", units: str = DEFAULT_UNITS
) -> dict:
    """
    Recommend whether to travel on a given day, what to wear, and what to
    watch out for - combining the forecast with active NWS alerts.

    Risk rating (thresholds returned in thresholds_used):
      * "high"     - a Severe/Extreme NWS alert is active, or >= 1.0 in of
                     precipitation, or gusts >= 45 mph, or a high >= 105 °F,
                     or a low <= 10 °F.
      * "moderate" - any active alert, or >= 0.25 in precipitation, or gusts
                     >= 25 mph, or a high >= 95 °F, or a low <= 32 °F.
      * "low"      - none of the above.

    Clothing advice keys off the daily low (jacket at <= 60 °F, heavy coat
    at <= 40 °F) and the high-to-low spread (layers when it exceeds 25 °F),
    and folds in the umbrella verdict from predict_umbrella_needed.

    Args:
        location: City name, US ZIP code, or "latitude,longitude".
        date: "today" (default), "tomorrow", or an ISO "YYYY-MM-DD" date
            within the next 16 days.
        units: "imperial" (default) or "metric".

    Returns:
        A dict with status, location, date, travel_risk, recommendation,
        jacket_needed, umbrella_needed, packing_list, cautions,
        active_alert_count, reasoning, and thresholds_used. On failure, a
        dict with status "error" and a human-readable message.
    """
    try:
        day_data = weather_client.get_day_forecast(location, date, units)
        try:
            alerts = weather_client.get_active_alerts(location)
        except WeatherError as exc:
            # Alerts are a bonus signal; a weather.gov outage must not sink
            # the recommendation, but say so rather than implying all-clear.
            logger.warning("NWS alert lookup failed for %s: %s", location, exc)
            alerts = {"alerts": [], "covered_by_nws": False, "alert_lookup_failed": True}

        verdict = forecast_rules.travel_verdict(
            day_data["daily"],
            day_data["hourly"],
            alerts,
            day_data["units"],
            day_data["days_ahead"],
        )
        result = _ok(
            {
                "location": day_data["location"],
                "date": day_data["date"],
                "timezone": day_data["timezone"],
                "units": day_data["units"],
                "alerts_checked": not alerts.get("alert_lookup_failed", False),
                "nws_coverage": alerts.get("covered_by_nws"),
                "source": "Open-Meteo + NOAA/NWS",
                **verdict,
            }
        )
        prediction_log.record(
            tool="get_travel_recommendation",
            location=day_data["location"],
            target_date=day_data["date"],
            verdict=f"travel risk: {verdict['travel_risk']}",
            user_email=_end_user_email(),
            payload=result,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        return _error(exc, location)


@mcp.tool
def get_severe_weather_alerts(location: str) -> dict:
    """
    Get active NOAA / National Weather Service watches, warnings, and
    advisories for a US location.

    weather.gov only covers the United States and its territories. For a
    location outside that coverage the result has covered_by_nws = false and
    an empty alert list - report that as "no US alert coverage here", not as
    "no alerts in effect".

    Args:
        location: City name, US ZIP code, or "latitude,longitude".

    Returns:
        A dict with status, location, covered_by_nws, alert_count, and
        alerts - a list of dicts with event, severity, urgency, certainty,
        headline, area, onset, expires, and instruction. On failure, a dict
        with status "error" and a human-readable message.
    """
    try:
        return _ok(weather_client.get_active_alerts(location))
    except Exception as exc:  # noqa: BLE001
        return _error(exc, location)


@mcp.tool
def compare_cities(
    locations: list[str], date: str = "", units: str = DEFAULT_UNITS
) -> dict:
    """
    Compare one day's forecast across several cities and rank them by how
    pleasant the day looks.

    The comfort score starts at 100 and subtracts penalties: distance of the
    daily high from a 72 °F ideal, the precipitation chance, and wind gusts
    above 15 mph. It is a rough heuristic for "which of these places has the
    nicer day", not a meteorological index - say so when reporting it.

    Args:
        locations: List of 2-5 city names, ZIP codes, or "lat,lon" strings.
        date: "today" (default), "tomorrow", or an ISO "YYYY-MM-DD" date.
        units: "imperial" (default) or "metric".

    Returns:
        A dict with status, date, ranked cities (best first, each with
        location, conditions, temp_high, temp_low, precipitation_chance_pct,
        comfort_score), a "best" pick, and failures - any locations that
        could not be resolved. On total failure, a dict with status "error".
    """
    try:
        if not locations:
            raise WeatherError("Pass at least two locations to compare")
        if len(locations) > MAX_COMPARE_CITIES:
            raise WeatherError(
                f"Compare at most {MAX_COMPARE_CITIES} locations at a time; "
                f"got {len(locations)}"
            )

        scored, failures = [], []
        for raw in locations:
            try:
                day_data = weather_client.get_day_forecast(raw, date, units)
            except WeatherError as exc:
                failures.append({"location_requested": raw, "message": str(exc)})
                continue

            day = day_data["daily"]
            metric = day_data["units"].get("temperature") == "°C"
            high_f = forecast_rules.to_fahrenheit(day.get("temp_high"), metric)
            gusts = forecast_rules.to_mph(day.get("wind_gusts_max"), metric) or 0
            chance = day.get("precipitation_chance_pct") or 0

            score = 100.0
            if high_f is not None:
                score -= abs(high_f - 72) * 1.5
            score -= chance * 0.5
            score -= max(0.0, gusts - 15) * 1.0

            scored.append(
                {
                    "location": day_data["location"],
                    "date": day_data["date"],
                    "conditions": day.get("conditions"),
                    "temp_high": day.get("temp_high"),
                    "temp_low": day.get("temp_low"),
                    "precipitation_chance_pct": day.get("precipitation_chance_pct"),
                    "wind_gusts_max": day.get("wind_gusts_max"),
                    "comfort_score": round(score, 1),
                }
            )

        if not scored:
            raise WeatherError(
                "None of the requested locations could be resolved: "
                + "; ".join(f["location_requested"] for f in failures)
            )

        scored.sort(key=lambda c: c["comfort_score"], reverse=True)
        return _ok(
            {
                "date": scored[0]["date"],
                "cities": scored,
                "best": scored[0]["location"],
                "failures": failures,
                "units": _units_for(units),
                "score_note": (
                    "comfort_score is a heuristic: 100 minus 1.5 per °F away from "
                    "72 °F, minus half the precipitation chance, minus 1 per mph "
                    "of gusts above 15 mph."
                ),
                "source": "Open-Meteo",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc, ", ".join(locations or []))


def _units_for(units: str) -> dict:
    try:
        return weather_client._units_label(units)
    except WeatherError:
        return {}


@mcp.tool
def get_recent_predictions(limit: int = 25) -> dict:
    """
    Read back recent predictions this MCP server has logged to Lakebase.

    Useful for "what did you tell me about Austin yesterday?" follow-ups and
    for the companion dashboard app. Returns an empty list with a note when
    the Lakebase prediction log is disabled or not set up - that is not an
    error condition.

    Args:
        limit: Maximum number of log entries to return, newest first
            (default 25).

    Returns:
        A dict with status, count, and predictions - each with tool,
        location, target_date, verdict, user_email, and created_at.
    """
    if not prediction_log.enabled():
        return _ok(
            {
                "count": 0,
                "predictions": [],
                "note": "Prediction logging is disabled (PREDICTION_LOG_ENABLED=false).",
            }
        )
    try:
        rows = prediction_log.recent(limit)
        return _ok({"count": len(rows), "predictions": rows})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read prediction log: %s", exc)
        return _ok(
            {
                "count": 0,
                "predictions": [],
                "note": (
                    "The Lakebase prediction log is unavailable "
                    f"({type(exc).__name__}). Weather tools are unaffected."
                ),
            }
        )


@mcp.tool
def get_current_user() -> dict:
    """
    Get the end user currently calling this MCP server.

    When running as a Databricks App this reads the X-Forwarded-User header
    Databricks injects, so it reflects the person chatting with the agent
    rather than the app's service principal.

    Returns:
        A dict with status, user_name, and source ("request_header" or
        "service_principal").
    """
    email = _end_user_email()
    if email:
        return _ok({"user_name": email, "source": "request_header"})
    try:
        from databricks.sdk import WorkspaceClient

        user = WorkspaceClient().current_user.me()
        return _ok(
            {
                "user_name": user.user_name,
                "display_name": user.display_name,
                "source": "service_principal",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "message": f"Could not determine the current user: {exc}",
        }


if __name__ == "__main__":
    # Capture end-user identity headers before serving (same as Day 3).
    if getattr(mcp, "app", None) is not None:
        mcp.app.add_middleware(RequestContextMiddleware)

    # Databricks Apps route external HTTP traffic to this port via app.yaml;
    # streamable-http is the transport Databricks' MCP gateway expects.
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    mcp.run(transport="http", host="0.0.0.0", port=port)
