"""
Optional Lakebase audit log for weather predictions.

Every prediction tool call is written to a `weather_predictions` table so
the companion dashboard app can show what the Agent Bricks agent has been
asked and what it answered (see `../dashboard/`). This is the extra-credit
half of the assignment, so it is *strictly best effort*: if Lakebase isn't
configured, or the table doesn't exist, or the write fails, the tool still
returns its answer and only logs a warning. Weather answers must never
fail because the audit log is down.

Set `PREDICTION_LOG_ENABLED=false` in app.yaml to turn logging off entirely.

The Lakebase URL is read via `WorkspaceClient().secrets.get_secret()` in
lakebase.py - no connection string is ever hardcoded or committed.
"""

import json
import logging
import os

logger = logging.getLogger("weather-mcp-server.prediction_log")

TABLE = os.environ.get("PREDICTION_LOG_TABLE", "weather_predictions")
_ENABLED = os.environ.get("PREDICTION_LOG_ENABLED", "true").strip().lower() not in (
    "false",
    "0",
    "no",
)


def enabled() -> bool:
    return _ENABLED


def record(
    tool: str,
    location: str,
    target_date: str | None,
    verdict: str | None,
    user_email: str | None,
    payload: dict,
) -> bool:
    """
    Append one prediction to the Lakebase log. Returns True if it landed.

    Never raises: a logging failure is swallowed (and logged) so the caller
    can still return a good weather answer.
    """
    if not _ENABLED:
        return False
    try:
        import lakebase

        lakebase.run_write(
            f"""
            INSERT INTO {TABLE}
                (tool, location, target_date, verdict, user_email, payload, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                tool,
                location,
                target_date or None,
                verdict,
                user_email,
                json.dumps(payload, default=str),
            ),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - logging must never break a tool
        logger.warning("Prediction log write skipped (%s): %s", type(exc).__name__, exc)
        return False


def recent(limit: int = 25) -> list[dict]:
    """Read back the most recent logged predictions, newest first."""
    import lakebase

    return lakebase.run_query(
        f"""
        SELECT id, tool, location, target_date, verdict, user_email, payload, created_at
        FROM {TABLE}
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (int(limit),),
    )
