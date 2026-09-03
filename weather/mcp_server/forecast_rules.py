"""
Derived judgment layer for the weather-prediction MCP server.

`weather_client.py` fetches facts; this module turns those facts into a
*decision* ("bring an umbrella", "pack a heavy coat", "this trip is risky")
using explicit, documented thresholds. It is deliberately pure - no HTTP,
no I/O, no MCP - so the reasoning can be read, tuned, and unit-tested on
its own.

All thresholds below are stated in imperial units (°F, mph, inches); metric
inputs are converted before comparison so the rules behave identically
whichever units the caller asked for.
"""

# Every number the prediction tools key off of, in one place so the
# docstrings, the tool output, and the README can all cite the same values.
THRESHOLDS = {
    "umbrella_yes_chance_pct": 40,
    "umbrella_maybe_chance_pct": 25,
    "umbrella_yes_amount_in": 0.10,
    "umbrella_maybe_amount_in": 0.02,
    "high_confidence_chance_pct": 70,
    "confident_dry_chance_pct": 20,
    "low_confidence_days_ahead": 7,
    "rainy_hour_chance_pct": 40,
    "windy_gusts_mph": 25,
    "jacket_temp_f": 60,
    "heavy_coat_temp_f": 40,
    "freezing_temp_f": 32,
    "layers_spread_f": 25,
    "heat_caution_temp_f": 95,
    "extreme_heat_temp_f": 105,
    "extreme_cold_temp_f": 10,
    "risky_wind_gusts_mph": 45,
    "risky_precip_in": 1.0,
    "moderate_precip_in": 0.25,
    "daytime_start_hour": 6,
    "daytime_end_hour": 22,
}


def _is_metric(units_label: dict) -> bool:
    return (units_label or {}).get("temperature") == "°C"


def _to_f(value, metric: bool):
    if value is None:
        return None
    return value * 9 / 5 + 32 if metric else value


def _to_mph(value, metric: bool):
    if value is None:
        return None
    return value / 1.609344 if metric else value


def _to_inches(value, metric: bool):
    if value is None:
        return None
    return value / 25.4 if metric else value


# Public aliases: the MCP layer occasionally needs the same conversions.
to_fahrenheit = _to_f
to_mph = _to_mph
to_inches = _to_inches


def _num(value, default=0.0) -> float:
    """Treat a missing measurement as the neutral value, never as a crash."""
    return default if value is None else float(value)


def rain_window(hours: list[dict], threshold_pct: int | None = None) -> dict | None:
    """
    Find the wettest stretch of the day.

    Scans the hourly precipitation probabilities between
    `daytime_start_hour` and `daytime_end_hour` and returns the first and
    last hour at or above `rainy_hour_chance_pct`, plus the peak hour.
    Returns None when no daytime hour crosses the threshold.
    """
    threshold = THRESHOLDS["rainy_hour_chance_pct"] if threshold_pct is None else threshold_pct
    start_h = THRESHOLDS["daytime_start_hour"]
    end_h = THRESHOLDS["daytime_end_hour"]

    wet = [
        h
        for h in hours or []
        if h.get("hour") is not None
        and start_h <= h["hour"] <= end_h
        and _num(h.get("precipitation_chance_pct")) >= threshold
    ]
    if not wet:
        return None

    peak = max(wet, key=lambda h: _num(h.get("precipitation_chance_pct")))
    return {
        "starts_hour": wet[0]["hour"],
        "ends_hour": wet[-1]["hour"],
        "peak_hour": peak["hour"],
        "peak_chance_pct": peak.get("precipitation_chance_pct"),
        "hours_at_or_above_threshold": len(wet),
        "threshold_pct": threshold,
    }


def _confidence(chance_pct: float, days_ahead: int) -> str:
    """Forecast skill decays with lead time; say so instead of implying certainty."""
    if days_ahead >= THRESHOLDS["low_confidence_days_ahead"]:
        return "low"
    decisive = (
        chance_pct >= THRESHOLDS["high_confidence_chance_pct"]
        or chance_pct <= THRESHOLDS["confident_dry_chance_pct"]
    )
    if decisive and days_ahead <= 2:
        return "high"
    return "medium"


def umbrella_verdict(day: dict, hours: list[dict], units_label: dict, days_ahead: int) -> dict:
    """
    Decide whether the user should carry an umbrella on a given day.

    Rule (all thresholds from THRESHOLDS):
      * YES    - daily precipitation chance >= 40%, or total precipitation
                 >= 0.10 in.
      * MAYBE  - chance >= 25%, or total precipitation >= 0.02 in.
      * NO     - anything below that.
    Two overrides sit on top of the core rule:
      * Snow/ice days return umbrella_needed = False with a "wear a
        waterproof coat and boots" note - an umbrella is the wrong tool.
      * Gusts >= 25 mph downgrade the advice to "rain jacket, not umbrella"
        because an umbrella will invert.
    """
    metric = _is_metric(units_label)
    chance = _num(day.get("precipitation_chance_pct"))
    amount_in = _num(_to_inches(day.get("precipitation_amount"), metric))
    gusts_mph = _num(_to_mph(day.get("wind_gusts_max"), metric))
    ptype = day.get("precipitation_type", "none")

    if chance >= THRESHOLDS["umbrella_yes_chance_pct"] or amount_in >= THRESHOLDS["umbrella_yes_amount_in"]:
        verdict, needed = "yes", True
    elif chance >= THRESHOLDS["umbrella_maybe_chance_pct"] or amount_in >= THRESHOLDS["umbrella_maybe_amount_in"]:
        verdict, needed = "maybe", True
    else:
        verdict, needed = "no", False

    reasons = [
        f"{chance:.0f}% chance of precipitation "
        f"(yes at >= {THRESHOLDS['umbrella_yes_chance_pct']}%, "
        f"maybe at >= {THRESHOLDS['umbrella_maybe_chance_pct']}%)",
        f"{day.get('precipitation_amount')} {units_label.get('precipitation')} total expected",
    ]
    notes = []

    window = rain_window(hours)
    if window:
        reasons.append(
            f"wet window roughly {window['starts_hour']:02d}:00-{window['ends_hour']:02d}:00 local, "
            f"peaking at {window['peak_hour']:02d}:00 ({window['peak_chance_pct']}%)"
        )
    elif verdict != "no":
        notes.append("Precipitation is expected outside normal daytime hours.")

    alternative = None
    if ptype in ("snow", "freezing"):
        needed = False
        alternative = "waterproof coat and boots"
        notes.append(
            f"Precipitation falls as {ptype}, so an umbrella is the wrong tool - "
            "wear a waterproof coat and boots instead."
        )
    elif needed and gusts_mph >= THRESHOLDS["windy_gusts_mph"]:
        alternative = "hooded rain jacket"
        notes.append(
            f"Gusts near {gusts_mph:.0f} mph will invert an umbrella - "
            "a hooded rain jacket is the better call."
        )
    if ptype == "thunderstorm":
        notes.append("Thunderstorms are in the forecast; plan to be indoors during the peak window.")

    return {
        "umbrella_needed": needed,
        "verdict": verdict,
        "confidence": _confidence(chance, days_ahead),
        "precipitation_chance_pct": day.get("precipitation_chance_pct"),
        "precipitation_amount": day.get("precipitation_amount"),
        "precipitation_type": ptype,
        "conditions": day.get("conditions"),
        "rain_window": window,
        "better_alternative": alternative,
        "reasoning": "; ".join(reasons) + ("." if reasons else ""),
        "notes": notes,
        "thresholds_used": {
            k: THRESHOLDS[k]
            for k in (
                "umbrella_yes_chance_pct",
                "umbrella_maybe_chance_pct",
                "umbrella_yes_amount_in",
                "umbrella_maybe_amount_in",
                "rainy_hour_chance_pct",
                "windy_gusts_mph",
            )
        },
    }


def travel_verdict(
    day: dict, hours: list[dict], alerts: dict | None, units_label: dict, days_ahead: int
) -> dict:
    """
    Turn one day's forecast (plus any active NWS alerts) into travel advice:
    a risk rating, what to wear, and what to watch out for.

    Risk rating:
      * HIGH     - a Severe/Extreme NWS alert is active, or >= 1.0 in of
                   precipitation, or gusts >= 45 mph, or a high >= 105 °F,
                   or a low <= 10 °F.
      * MODERATE - any active alert, or >= 0.25 in precipitation, or gusts
                   >= 25 mph, or a high >= 95 °F, or a low <= 32 °F.
      * LOW      - none of the above.
    Clothing advice keys off the daily low (jacket <= 60 °F, heavy coat
    <= 40 °F) and the high-to-low spread (layers when the spread >= 25 °F).
    """
    metric = _is_metric(units_label)
    high_f = _to_f(day.get("temp_high"), metric)
    low_f = _to_f(day.get("temp_low"), metric)
    feels_low_f = _to_f(day.get("feels_like_low"), metric)
    gusts_mph = _num(_to_mph(day.get("wind_gusts_max"), metric))
    amount_in = _num(_to_inches(day.get("precipitation_amount"), metric))
    chance = _num(day.get("precipitation_chance_pct"))

    alert_list = (alerts or {}).get("alerts") or []
    severe = [
        a for a in alert_list if str(a.get("severity", "")).lower() in ("severe", "extreme")
    ]

    packing, cautions = [], []
    effective_low = min(v for v in (low_f, feels_low_f) if v is not None) if (low_f is not None or feels_low_f is not None) else None

    if effective_low is not None:
        if effective_low <= THRESHOLDS["heavy_coat_temp_f"]:
            packing.append("heavy coat, hat, and gloves")
        elif effective_low <= THRESHOLDS["jacket_temp_f"]:
            packing.append("a jacket for the morning and evening")
    if high_f is not None and low_f is not None and (high_f - low_f) >= THRESHOLDS["layers_spread_f"]:
        packing.append(
            f"layers - the day swings about {high_f - low_f:.0f} °F "
            f"({day.get('temp_low')} to {day.get('temp_high')} {units_label.get('temperature')})"
        )
    if high_f is not None and high_f >= THRESHOLDS["heat_caution_temp_f"]:
        packing.append("water, sunscreen, and a shaded midday break")
        cautions.append(f"Heat: high near {day.get('temp_high')} {units_label.get('temperature')}.")
    if effective_low is not None and effective_low <= THRESHOLDS["freezing_temp_f"]:
        cautions.append("At or below freezing overnight - watch for ice on roads and walkways.")

    umbrella = umbrella_verdict(day, hours, units_label, days_ahead)
    if umbrella["umbrella_needed"]:
        packing.append(umbrella["better_alternative"] or "an umbrella")
    elif umbrella["better_alternative"]:
        packing.append(umbrella["better_alternative"])

    if gusts_mph >= THRESHOLDS["windy_gusts_mph"]:
        cautions.append(f"Wind gusts near {gusts_mph:.0f} mph - expect flight and bridge delays.")
    for alert in alert_list:
        cautions.append(f"NWS {alert.get('severity')} alert: {alert.get('event')}.")

    if (
        severe
        or amount_in >= THRESHOLDS["risky_precip_in"]
        or gusts_mph >= THRESHOLDS["risky_wind_gusts_mph"]
        or (high_f is not None and high_f >= THRESHOLDS["extreme_heat_temp_f"])
        or (effective_low is not None and effective_low <= THRESHOLDS["extreme_cold_temp_f"])
    ):
        risk = "high"
    elif (
        alert_list
        or amount_in >= THRESHOLDS["moderate_precip_in"]
        or gusts_mph >= THRESHOLDS["windy_gusts_mph"]
        or (high_f is not None and high_f >= THRESHOLDS["heat_caution_temp_f"])
        or (effective_low is not None and effective_low <= THRESHOLDS["freezing_temp_f"])
    ):
        risk = "moderate"
    else:
        risk = "low"

    verdicts = {
        "low": "Good conditions - no weather reason to change plans.",
        "moderate": "Workable, but pack for it and leave margin in your schedule.",
        "high": "Consider rescheduling or building in a serious buffer.",
    }

    return {
        "travel_risk": risk,
        "recommendation": verdicts[risk],
        "confidence": umbrella["confidence"],
        "conditions": day.get("conditions"),
        "temp_high": day.get("temp_high"),
        "temp_low": day.get("temp_low"),
        "precipitation_chance_pct": day.get("precipitation_chance_pct"),
        "precipitation_amount": day.get("precipitation_amount"),
        "wind_gusts_max": day.get("wind_gusts_max"),
        "jacket_needed": bool(effective_low is not None and effective_low <= THRESHOLDS["jacket_temp_f"]),
        "umbrella_needed": umbrella["umbrella_needed"],
        "packing_list": packing or ["nothing special - dress for the temperature"],
        "cautions": cautions,
        "active_alert_count": len(alert_list),
        "reasoning": (
            f"High {day.get('temp_high')} / low {day.get('temp_low')} "
            f"{units_label.get('temperature')}, {chance:.0f}% precipitation chance, "
            f"gusts to {day.get('wind_gusts_max')} {units_label.get('wind_speed')}, "
            f"{len(alert_list)} active NWS alert(s)."
        ),
        "thresholds_used": {
            k: THRESHOLDS[k]
            for k in (
                "jacket_temp_f",
                "heavy_coat_temp_f",
                "layers_spread_f",
                "heat_caution_temp_f",
                "risky_wind_gusts_mph",
                "risky_precip_in",
                "moderate_precip_in",
            )
        },
    }
