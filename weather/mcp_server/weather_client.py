"""
Open-Meteo (+ NWS) weather adapter backing the weather-prediction MCP server.

Same role as Day 3's `alpaca_broker.py`: this module owns *all* HTTP calls
and response parsing, so the `@mcp.tool` functions in
`weather_mcp_server.py` stay thin and readable. Nothing in this file knows
about MCP, and nothing in the MCP server calls `requests` directly.

Data sources (both free, neither needs an API key or a signup):
    - Open-Meteo geocoding  https://geocoding-api.open-meteo.com/v1/search
    - Open-Meteo forecast   https://api.open-meteo.com/v1/forecast
    - NWS active alerts     https://api.weather.gov/alerts/active   (US only)
    - Open-Meteo archive    https://archive-api.open-meteo.com/v1/archive  (past dates)

Because Open-Meteo is keyless, there are no credentials to manage here -
see `lakebase.py` for the `WorkspaceClient().secrets.get_secret()` pattern
this project uses for the one secret it does need (the Lakebase URL used
by the optional prediction log).

Every failure path raises `WeatherError` with a human-readable message.
The MCP layer turns that into a clean `{"status": "error", ...}` dict, so
the agent never sees a stack trace and can ask the user to clarify.
"""

import os
from datetime import date as _date, datetime, timedelta

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Open-Meteo only publishes 16 days of forecast.
MAX_FORECAST_DAYS = 16

_TIMEOUT = float(os.environ.get("WEATHER_HTTP_TIMEOUT", "15"))
# weather.gov asks every client to identify itself with a contact address.
_USER_AGENT = os.environ.get(
    "WEATHER_USER_AGENT",
    "weather-prediction-mcp-server (Databricks Apps homework)",
)

# Geocoding results are stable; cache them per-process to keep tool calls fast
# and stay well under Open-Meteo's ~10k requests/day fair-use limit.
_GEOCODE_CACHE: dict[str, dict] = {}


class WeatherError(Exception):
    """Any recoverable failure: bad location, bad date, upstream API problem."""


# WMO weather interpretation codes -> plain English.
# https://open-meteo.com/en/docs (see "WMO Weather interpretation codes")
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

_SNOW_CODES = {71, 73, 75, 77, 85, 86}
_FREEZING_CODES = {56, 57, 66, 67}
_THUNDER_CODES = {95, 96, 99}


def describe_code(code) -> str:
    """Translate a WMO weather code into a short English description."""
    try:
        return WMO_CODES.get(int(code), f"Unknown conditions (WMO code {code})")
    except (TypeError, ValueError):
        return "Unknown conditions"


def precipitation_type(code) -> str:
    """Classify a WMO code as rain / snow / freezing / thunderstorm / none."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if code in _SNOW_CODES:
        return "snow"
    if code in _FREEZING_CODES:
        return "freezing"
    if code in _THUNDER_CODES:
        return "thunderstorm"
    if code >= 51:
        return "rain"
    return "none"


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

def _get(url: str, params: dict, headers: dict | None = None) -> dict:
    """GET a JSON endpoint, converting every failure mode into WeatherError."""
    request_headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    try:
        response = requests.get(
            url, params=params, headers=request_headers, timeout=_TIMEOUT
        )
    except requests.Timeout as exc:
        raise WeatherError(f"Weather service timed out after {_TIMEOUT:g}s") from exc
    except requests.RequestException as exc:
        raise WeatherError(f"Could not reach the weather service: {exc}") from exc

    if response.status_code != 200:
        # Open-Meteo returns {"error": true, "reason": "..."} on 4xx.
        reason = ""
        try:
            reason = response.json().get("reason", "")
        except ValueError:
            reason = response.text[:200]
        raise WeatherError(
            f"Weather service returned HTTP {response.status_code}"
            + (f": {reason}" if reason else "")
        )

    try:
        return response.json()
    except ValueError as exc:
        raise WeatherError("Weather service returned a non-JSON response") from exc


def _unit_params(units: str) -> dict:
    """Map a friendly units name onto Open-Meteo's unit query parameters."""
    units = (units or "imperial").strip().lower()
    if units in ("imperial", "us", "f", "fahrenheit"):
        return {
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        }
    if units in ("metric", "si", "c", "celsius"):
        return {
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
        }
    raise WeatherError(f"units must be 'imperial' or 'metric', got {units!r}")


def _units_label(units: str) -> dict:
    return (
        {"temperature": "°F", "wind_speed": "mph", "precipitation": "in"}
        if _unit_params(units)["temperature_unit"] == "fahrenheit"
        else {"temperature": "°C", "wind_speed": "km/h", "precipitation": "mm"}
    )


# --------------------------------------------------------------------------
# Location resolution
# --------------------------------------------------------------------------

def _parse_latlon(location: str) -> tuple[float, float] | None:
    """Return (lat, lon) if `location` looks like "41.85,-87.65", else None."""
    if "," not in location:
        return None
    left, _, right = location.partition(",")
    try:
        lat, lon = float(left.strip()), float(right.strip())
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise WeatherError(
            f"Coordinates out of range: latitude {lat}, longitude {lon}"
        )
    return lat, lon


def geocode(location: str) -> dict:
    """
    Resolve a free-text location to coordinates.

    Accepts a city name ("Chicago", "Austin, TX", "Paris"), a US ZIP code
    ("60601"), or a raw "lat,lon" pair ("41.85,-87.65").

    Returns a dict with name, latitude, longitude, and (when the geocoder
    supplies them) admin1/country/timezone. Raises WeatherError if the
    location can't be resolved - the agent is instructed to ask the user to
    clarify rather than guess.
    """
    if not location or not location.strip():
        raise WeatherError("A location is required (city name, ZIP code, or 'lat,lon')")
    location = location.strip()

    coords = _parse_latlon(location)
    if coords:
        lat, lon = coords
        return {
            "query": location,
            "name": f"{lat:.4f}, {lon:.4f}",
            "latitude": lat,
            "longitude": lon,
            "admin1": None,
            "country": None,
            "timezone": None,
            "resolved_by": "coordinates",
        }

    cache_key = location.lower()
    if cache_key in _GEOCODE_CACHE:
        return dict(_GEOCODE_CACHE[cache_key])

    # The geocoder matches on a single place name, so drop a trailing
    # ", TX" / ", France" qualifier and use it to pick among the matches.
    name_part, _, qualifier = location.partition(",")
    payload = _get(
        GEOCODE_URL,
        {
            "name": name_part.strip(),
            "count": 10,
            "language": "en",
            "format": "json",
        },
    )
    results = payload.get("results") or []
    if not results:
        raise WeatherError(
            f"Could not find a location matching {location!r}. "
            "Try a city name, a US ZIP code, or 'latitude,longitude'."
        )

    match = results[0]
    qualifier = qualifier.strip().lower()
    if qualifier:
        for candidate in results:
            fields = [
                str(candidate.get("admin1") or ""),
                str(candidate.get("country") or ""),
                str(candidate.get("country_code") or ""),
            ]
            if any(qualifier == f.lower() for f in fields):
                match = candidate
                break

    resolved = {
        "query": location,
        "name": match.get("name"),
        "latitude": match.get("latitude"),
        "longitude": match.get("longitude"),
        "admin1": match.get("admin1"),
        "country": match.get("country"),
        "timezone": match.get("timezone"),
        "resolved_by": "geocoding",
    }
    _GEOCODE_CACHE[cache_key] = resolved
    return dict(resolved)


def _place_label(place: dict) -> str:
    """"Chicago, Illinois, United States" from a geocode() result."""
    parts = [place.get("name"), place.get("admin1"), place.get("country")]
    return ", ".join(str(p) for p in parts if p)


# --------------------------------------------------------------------------
# Forecast fetching + shaping
# --------------------------------------------------------------------------

_DAILY_FIELDS = (
    "weather_code,temperature_2m_max,temperature_2m_min,"
    "apparent_temperature_max,apparent_temperature_min,"
    "precipitation_sum,precipitation_probability_max,"
    "wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset"
)
_HOURLY_FIELDS = (
    "temperature_2m,precipitation_probability,precipitation,"
    "weather_code,wind_speed_10m"
)


def _fetch_forecast(
    place: dict, days: int, units: str, hourly: bool = False, current: bool = False
) -> dict:
    """Call Open-Meteo's forecast endpoint for an already-geocoded place."""
    params = {
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "daily": _DAILY_FIELDS,
        "timezone": "auto",
        "forecast_days": max(1, min(int(days), MAX_FORECAST_DAYS)),
        **_unit_params(units),
    }
    if hourly:
        params["hourly"] = _HOURLY_FIELDS
    if current:
        params["current"] = (
            "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "precipitation,weather_code,wind_speed_10m,wind_gusts_10m,"
            "wind_direction_10m,is_day"
        )
    return _get(FORECAST_URL, params)


def _daily_row(payload: dict, index: int) -> dict:
    """Flatten Open-Meteo's column-oriented daily arrays into one day's dict."""
    daily = payload.get("daily") or {}

    def col(name):
        values = daily.get(name) or []
        return values[index] if index < len(values) else None

    code = col("weather_code")
    return {
        "date": col("time"),
        "conditions": describe_code(code),
        "weather_code": code,
        "precipitation_type": precipitation_type(code),
        "temp_high": col("temperature_2m_max"),
        "temp_low": col("temperature_2m_min"),
        "feels_like_high": col("apparent_temperature_max"),
        "feels_like_low": col("apparent_temperature_min"),
        "precipitation_chance_pct": col("precipitation_probability_max"),
        "precipitation_amount": col("precipitation_sum"),
        "wind_speed_max": col("wind_speed_10m_max"),
        "wind_gusts_max": col("wind_gusts_10m_max"),
        "sunrise": col("sunrise"),
        "sunset": col("sunset"),
    }


def _hours_for_date(payload: dict, target: str) -> list[dict]:
    """Pull the hourly rows belonging to one local calendar date."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    rows = []
    for i, stamp in enumerate(times):
        if not stamp.startswith(target):
            continue

        def col(name, i=i):
            values = hourly.get(name) or []
            return values[i] if i < len(values) else None

        rows.append(
            {
                "time": stamp,
                "hour": int(stamp[11:13]) if len(stamp) >= 13 else None,
                "temperature": col("temperature_2m"),
                "precipitation_chance_pct": col("precipitation_probability"),
                "precipitation_amount": col("precipitation"),
                "weather_code": col("weather_code"),
                "conditions": describe_code(col("weather_code")),
                "wind_speed": col("wind_speed_10m"),
            }
        )
    return rows


def _normalize_date(value: str, local_today: _date) -> _date:
    """Turn "", "today", "tomorrow", or "YYYY-MM-DD" into a real date."""
    text = (value or "").strip().lower()
    if text in ("", "today"):
        return local_today
    if text == "tomorrow":
        return local_today + timedelta(days=1)
    try:
        return _date.fromisoformat(text)
    except ValueError as exc:
        raise WeatherError(
            f"Could not read the date {value!r}. Use 'today', 'tomorrow', "
            "or an ISO date like 2026-08-30."
        ) from exc


def get_current_weather(location: str, units: str = "imperial") -> dict:
    """Current observed conditions for a location, as a flat dict."""
    place = geocode(location)
    payload = _fetch_forecast(place, days=1, units=units, current=True)
    current = payload.get("current") or {}
    if not current:
        raise WeatherError("The weather service did not return current conditions")

    code = current.get("weather_code")
    return {
        "location": _place_label(place),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": payload.get("timezone"),
        "observed_at": current.get("time"),
        "temperature": current.get("temperature_2m"),
        "feels_like": current.get("apparent_temperature"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "precipitation_now": current.get("precipitation"),
        "conditions": describe_code(code),
        "weather_code": code,
        "wind_speed": current.get("wind_speed_10m"),
        "wind_gusts": current.get("wind_gusts_10m"),
        "wind_direction_deg": current.get("wind_direction_10m"),
        "is_daytime": bool(current.get("is_day")),
        "units": _units_label(units),
        "source": "Open-Meteo",
    }


def get_forecast(location: str, days: int = 3, units: str = "imperial") -> dict:
    """Multi-day daily forecast for a location."""
    try:
        days = int(days)
    except (TypeError, ValueError) as exc:
        raise WeatherError(f"days must be a whole number, got {days!r}") from exc
    if days < 1:
        raise WeatherError("days must be at least 1")
    if days > MAX_FORECAST_DAYS:
        raise WeatherError(
            f"Open-Meteo only forecasts {MAX_FORECAST_DAYS} days ahead; got days={days}"
        )

    place = geocode(location)
    payload = _fetch_forecast(place, days=days, units=units)
    count = len((payload.get("daily") or {}).get("time") or [])
    return {
        "location": _place_label(place),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": payload.get("timezone"),
        "days": [_daily_row(payload, i) for i in range(count)],
        "units": _units_label(units),
        "source": "Open-Meteo",
    }


def get_day_forecast(
    location: str, target_date: str = "", units: str = "imperial"
) -> dict:
    """
    Daily summary *plus* hour-by-hour detail for one specific local date.

    This is the raw material the prediction tools reason over: the daily row
    gives totals and extremes, the hourly rows say *when* during the day the
    weather actually happens.
    """
    place = geocode(location)

    # First pass (daily only, cheap) tells us what "today" means in the
    # location's own timezone and which dates are actually forecastable.
    calendar = _fetch_forecast(place, days=MAX_FORECAST_DAYS, units=units)
    dates = (calendar.get("daily") or {}).get("time") or []
    if not dates:
        raise WeatherError("The weather service did not return a forecast calendar")

    local_today = _date.fromisoformat(dates[0])
    target = _normalize_date(target_date, local_today)
    target_iso = target.isoformat()
    if target_iso not in dates:
        raise WeatherError(
            f"No forecast available for {target_iso} at {_place_label(place)}. "
            f"Forecasts run {dates[0]} through {dates[-1]}."
        )

    index = dates.index(target_iso)
    detailed = _fetch_forecast(place, days=index + 1, units=units, hourly=True)
    return {
        "place": place,
        "location": _place_label(place),
        "latitude": detailed.get("latitude"),
        "longitude": detailed.get("longitude"),
        "timezone": detailed.get("timezone"),
        "local_today": dates[0],
        "date": target_iso,
        "days_ahead": index,
        "daily": _daily_row(detailed, index),
        "hourly": _hours_for_date(detailed, target_iso),
        "units": _units_label(units),
        "source": "Open-Meteo",
    }


# --------------------------------------------------------------------------
# Historical observations (Open-Meteo archive API, also keyless)
# --------------------------------------------------------------------------

# The archive reports what actually happened, so there is no
# precipitation_probability_max here - a past day's rain is a fact, not a
# chance. It does break precipitation down by rain vs snowfall, which the
# forecast endpoint does not.
_ARCHIVE_DAILY_FIELDS = (
    "weather_code,temperature_2m_max,temperature_2m_min,"
    "apparent_temperature_max,apparent_temperature_min,"
    "precipitation_sum,rain_sum,snowfall_sum,precipitation_hours,"
    "wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset"
)

# The ERA5 archive lags real time by a few days, so recent history comes from
# the forecast endpoint's past_days window instead - it reaches 92 days back
# and covers yesterday, which the archive does not.
RECENT_PAST_DAYS = 92


def _archive_row(payload: dict, index: int = 0) -> dict:
    """Flatten one observed day out of the column-oriented daily arrays."""
    daily = payload.get("daily") or {}

    def col(name):
        values = daily.get(name) or []
        return values[index] if index < len(values) else None

    code = col("weather_code")
    return {
        "date": col("time"),
        "conditions": describe_code(code),
        "weather_code": code,
        "precipitation_type": precipitation_type(code),
        "temp_high": col("temperature_2m_max"),
        "temp_low": col("temperature_2m_min"),
        "feels_like_high": col("apparent_temperature_max"),
        "feels_like_low": col("apparent_temperature_min"),
        "precipitation_amount": col("precipitation_sum"),
        "rain_amount": col("rain_sum"),
        "snowfall_amount": col("snowfall_sum"),
        "precipitation_hours": col("precipitation_hours"),
        "wind_speed_max": col("wind_speed_10m_max"),
        "wind_gusts_max": col("wind_gusts_10m_max"),
        "sunrise": col("sunrise"),
        "sunset": col("sunset"),
    }


def get_historical_weather(
    location: str, target_date: str, units: str = "imperial"
) -> dict:
    """
    What the weather actually was on a past date.

    Uses Open-Meteo's archive endpoint rather than the forecast endpoint, so
    the numbers are observations, not predictions. Dates inside the forecast
    window are rejected with a pointer to `get_forecast`, because answering a
    future question from the archive would silently return nothing useful.
    """
    place = geocode(location)

    # One cheap forecast call establishes "today" in the location's own
    # timezone - the same trick get_day_forecast uses. A date is only
    # historical relative to where it is being asked about.
    calendar = _fetch_forecast(place, days=1, units=units)
    dates = (calendar.get("daily") or {}).get("time") or []
    if not dates:
        raise WeatherError("The weather service did not return a forecast calendar")
    local_today = _date.fromisoformat(dates[0])

    text = (target_date or "").strip().lower()
    if text == "yesterday":
        target = local_today - timedelta(days=1)
    elif text in ("", "today", "tomorrow"):
        raise WeatherError(
            f"{text or 'an empty date'!r} is not a past date. Use get_forecast or "
            "the prediction tools for today and the days ahead."
        )
    else:
        target = _normalize_date(target_date, local_today)

    if target >= local_today:
        raise WeatherError(
            f"{target.isoformat()} is not in the past at {_place_label(place)} "
            f"(local today is {local_today.isoformat()}). Use get_forecast or "
            "predict_umbrella_needed for today and future dates."
        )

    target_iso = target.isoformat()
    days_ago = (local_today - target).days

    if days_ago <= RECENT_PAST_DAYS:
        # Recent history: the forecast endpoint's past_days window. The ERA5
        # archive lags a few days and would return nothing for yesterday.
        payload = _get(FORECAST_URL, {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "daily": _ARCHIVE_DAILY_FIELDS,
            "timezone": "auto",
            "past_days": days_ago,
            "forecast_days": 1,
            **_unit_params(units),
        })
        source = "Open-Meteo (recent past days)"
        index = None
        times = (payload.get("daily") or {}).get("time") or []
        if target_iso in times:
            index = times.index(target_iso)
    else:
        payload = _get(ARCHIVE_URL, {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "start_date": target_iso,
            "end_date": target_iso,
            "daily": _ARCHIVE_DAILY_FIELDS,
            "timezone": "auto",
            **_unit_params(units),
        })
        source = "Open-Meteo archive (ERA5 reanalysis)"
        times = (payload.get("daily") or {}).get("time") or []
        index = 0 if times else None

    if index is None:
        raise WeatherError(
            f"No observations available for {target_iso} at {_place_label(place)}."
        )

    return {
        "location": _place_label(place),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": payload.get("timezone"),
        "date": target_iso,
        "days_ago": days_ago,
        "observed": _archive_row(payload, index),
        "units": _units_label(units),
        "source": source,
    }


# --------------------------------------------------------------------------
# National Weather Service alerts (US only, also keyless)
# --------------------------------------------------------------------------

def get_active_alerts(location: str) -> dict:
    """
    Active NWS watches/warnings/advisories for a US location.

    weather.gov only covers the United States and its territories; for a
    non-US point the API returns no alerts, which this reports as
    `covered: false` rather than pretending the forecast is all-clear.
    """
    place = geocode(location)
    lat, lon = place["latitude"], place["longitude"]
    payload = _get(NWS_ALERTS_URL, {"point": f"{lat:.4f},{lon:.4f}"})

    alerts = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        alerts.append(
            {
                "event": props.get("event"),
                "severity": props.get("severity"),
                "urgency": props.get("urgency"),
                "certainty": props.get("certainty"),
                "headline": props.get("headline"),
                "area": props.get("areaDesc"),
                "onset": props.get("onset"),
                "expires": props.get("expires"),
                "instruction": props.get("instruction"),
            }
        )

    country = (place.get("country") or "").lower()
    covered = country in ("", "united states", "usa") or place["resolved_by"] == "coordinates"
    return {
        "location": _place_label(place),
        "covered_by_nws": covered,
        "alert_count": len(alerts),
        "alerts": alerts,
        "checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source": "NOAA / National Weather Service (api.weather.gov)",
    }
