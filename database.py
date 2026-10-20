"""
database.py — SQLite schema and helper functions for GreenCorridor AI.

Creates greencorridor.db automatically on first call to init_db().
All queries use parameterized statements (? placeholders) to prevent SQL injection.
"""

import sqlite3
import os
import json
from datetime import datetime, timezone

# ── Database path (overridable for tests) ──────────────────
_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "greencorridor.db")


def set_db_path(path):
    """Override the database file path (used by tests)."""
    global _db_path
    _db_path = path


def get_db_path():
    """Return the current database file path."""
    return _db_path


def get_db():
    """
    Get a database connection with Row factory.
    Caller is responsible for closing the connection.
    """
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row           # Access columns by name
    conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")   # Enforce FK constraints
    return conn


def init_db():
    """Create all tables and indexes if they do not already exist."""
    conn = get_db()
    cur = conn.cursor()

    cur.executescript("""
        -- Trips: one row per simulation run
        CREATE TABLE IF NOT EXISTS trips (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            status                  TEXT    NOT NULL DEFAULT 'IDLE',
            seed                    INTEGER NOT NULL DEFAULT 42,
            speed_multiplier        REAL    NOT NULL DEFAULT 1.0,
            mode                    TEXT    NOT NULL DEFAULT 'coordinated',
            start_time              TEXT,
            end_time                TEXT,
            baseline_duration_s     REAL,
            coordinated_duration_s  REAL,
            route_json              TEXT,
            current_position_json   TEXT,
            current_distance_m      REAL    DEFAULT 0.0,
            total_distance_m        REAL    DEFAULT 0.0,
            elapsed_sim_time_s      REAL    DEFAULT 0.0,
            created_at              TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Junctions along the route
        CREATE TABLE IF NOT EXISTS junctions (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id                 INTEGER NOT NULL,
            name                    TEXT    NOT NULL,
            lat                     REAL    NOT NULL,
            lng                     REAL    NOT NULL,
            distance_from_start_m   REAL    NOT NULL,
            order_index             INTEGER NOT NULL,
            is_signalised           INTEGER NOT NULL DEFAULT 1,
            is_blocked              INTEGER NOT NULL DEFAULT 0,
            is_passed               INTEGER NOT NULL DEFAULT 0,
            wait_time_s             REAL    DEFAULT 0.0,
            arrival_sim_time_s      REAL,
            approximate             INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (trip_id) REFERENCES trips(id)
        );
        CREATE INDEX IF NOT EXISTS idx_junctions_trip ON junctions(trip_id);

        -- Alerts: one per (trip, junction), enforced by UNIQUE constraint
        CREATE TABLE IF NOT EXISTS alerts (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id                 INTEGER NOT NULL,
            junction_id             INTEGER NOT NULL,
            status                  TEXT    NOT NULL DEFAULT 'CREATED',
            officer_name            TEXT,
            created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            created_sim_time_s      REAL,
            ack_at                  TEXT,
            ack_sim_time_s          REAL,
            escalated_at            TEXT,
            escalated_sim_time_s    REAL,
            cleared_at              TEXT,
            cleared_sim_time_s      REAL,
            auto_officer            INTEGER NOT NULL DEFAULT 0,
            UNIQUE(trip_id, junction_id),
            FOREIGN KEY (trip_id) REFERENCES trips(id),
            FOREIGN KEY (junction_id) REFERENCES junctions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_trip ON alerts(trip_id);

        -- Alert state-change audit log
        CREATE TABLE IF NOT EXISTS alert_events (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id                INTEGER NOT NULL,
            old_status              TEXT,
            new_status              TEXT    NOT NULL,
            timestamp               TEXT    NOT NULL DEFAULT (datetime('now')),
            sim_time_s              REAL,
            details                 TEXT,
            FOREIGN KEY (alert_id) REFERENCES alerts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_alert_events_alert ON alert_events(alert_id);

        -- Trip-level event log (start, pause, roadblock, etc.)
        CREATE TABLE IF NOT EXISTS trip_events (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id                 INTEGER NOT NULL,
            event_type              TEXT    NOT NULL,
            description             TEXT,
            timestamp               TEXT    NOT NULL DEFAULT (datetime('now')),
            sim_time_s              REAL,
            data_json               TEXT,
            FOREIGN KEY (trip_id) REFERENCES trips(id)
        );
        CREATE INDEX IF NOT EXISTS idx_trip_events_trip ON trip_events(trip_id);

        -- Key-value settings store
        CREATE TABLE IF NOT EXISTS settings (
            key                     TEXT PRIMARY KEY,
            value                   TEXT    NOT NULL,
            updated_at              TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """)

    conn.commit()
    conn.close()


# ── Helper functions ───────────────────────────────────────

def utcnow():
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def get_setting(key, default=None):
    """Read a single setting; return *default* if not found."""
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    """Insert or update a single setting."""
    conn = get_db()
    now = utcnow()
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), now),
    )
    conn.commit()
    conn.close()


def get_all_settings():
    """Return all settings as a plain dict."""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def log_trip_event(trip_id, event_type, description,
                   sim_time_s=None, data=None):
    """Append a row to the trip_events table."""
    conn = get_db()
    conn.execute(
        "INSERT INTO trip_events (trip_id, event_type, description, "
        "sim_time_s, data_json) VALUES (?, ?, ?, ?, ?)",
        (trip_id, event_type, description, sim_time_s,
         json.dumps(data) if data else None),
    )
    conn.commit()
    conn.close()


def log_alert_event(alert_id, old_status, new_status,
                    sim_time_s=None, details=None):
    """Append a row to the alert_events table."""
    conn = get_db()
    conn.execute(
        "INSERT INTO alert_events (alert_id, old_status, new_status, "
        "sim_time_s, details) VALUES (?, ?, ?, ?, ?)",
        (alert_id, old_status, new_status, sim_time_s, details),
    )
    conn.commit()
    conn.close()
