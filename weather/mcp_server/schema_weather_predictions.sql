-- Audit-log table for the weather-prediction MCP server.
-- Run this once against your Lakebase Postgres database if you want the
-- companion dashboard app to show recent agent questions and answers.
-- The MCP server works fine without it (logging is best-effort).

CREATE TABLE IF NOT EXISTS weather_predictions (
    id          SERIAL PRIMARY KEY,
    tool        VARCHAR(64)  NOT NULL,
    location    VARCHAR(255) NOT NULL,
    target_date VARCHAR(32),
    verdict     VARCHAR(255),
    user_email  VARCHAR(255),
    payload     JSONB,
    created_at  TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_weather_predictions_created_at
    ON weather_predictions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_weather_predictions_location
    ON weather_predictions (location);
CREATE INDEX IF NOT EXISTS idx_weather_predictions_user
    ON weather_predictions (user_email);
