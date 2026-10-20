"""
alerts.py — Alert lifecycle management for GreenCorridor AI.

Alert states: CREATED → ACKNOWLEDGED or ESCALATED → CLEARED / EXPIRED.
Each transition is logged in alert_events for full audit trail.
Duplicate alerts per (trip, junction) are prevented by a UNIQUE constraint
plus a code-level check.
"""

from datetime import datetime, timezone

from config import (
    ALERT_START_S, ALERT_MIN_S, ESCALATION_TIMEOUT_S,
)
from database import get_db, log_alert_event, log_trip_event, utcnow


# ── Alert creation ─────────────────────────────────────────

def check_and_create_alerts(trip_id, junctions_with_eta, sim_time_s, settings):
    """
    For each upcoming junction, create an alert if ETA falls inside
    [alert_min_s, alert_start_s] and no alert already exists.

    Args:
        trip_id:           current trip id
        junctions_with_eta: list of dicts with keys id, eta_s, is_passed
        sim_time_s:        current simulated time in seconds
        settings:          dict of settings overrides

    Returns:
        list of newly created alert IDs
    """
    alert_start = float(settings.get("alert_start_s", ALERT_START_S))
    alert_min = float(settings.get("alert_min_s", ALERT_MIN_S))

    new_ids = []
    conn = get_db()
    try:
        for junc in junctions_with_eta:
            if junc.get("is_passed"):
                continue

            eta = junc.get("eta_s")
            if eta is None:
                continue

            # Inside the alert window?
            if eta < alert_min or eta > alert_start:
                continue

            # Duplicate check (belt-and-suspenders on top of UNIQUE constraint)
            existing = conn.execute(
                "SELECT id FROM alerts WHERE trip_id = ? AND junction_id = ?",
                (trip_id, junc["id"]),
            ).fetchone()
            if existing:
                continue

            # Create the alert
            now = utcnow()
            try:
                cur = conn.execute(
                    "INSERT INTO alerts "
                    "(trip_id, junction_id, status, created_at, created_sim_time_s) "
                    "VALUES (?, ?, 'CREATED', ?, ?)",
                    (trip_id, junc["id"], now, sim_time_s),
                )
                conn.commit()
                alert_id = cur.lastrowid
                new_ids.append(alert_id)

                # Audit log
                log_alert_event(alert_id, None, "CREATED",
                                sim_time_s=sim_time_s,
                                details=f"ETA={eta:.1f}s for junction {junc.get('name','?')}")
                log_trip_event(trip_id, "ALERT_CREATED",
                               f"Alert #{alert_id} for {junc.get('name','?')} (ETA {eta:.0f}s)",
                               sim_time_s=sim_time_s,
                               data={"alert_id": alert_id, "junction_id": junc["id"]})
            except Exception:
                # UNIQUE constraint violation — already exists
                conn.rollback()
    finally:
        conn.close()

    return new_ids


# ── Acknowledge ────────────────────────────────────────────

def acknowledge_alert(alert_id, officer_name, sim_time_s=None, auto=False):
    """
    Acknowledge an alert.  Rejects double-acknowledge.

    Args:
        alert_id:     the alert row id
        officer_name: non-empty string (validated)
        sim_time_s:   simulated time of acknowledgement
        auto:         True if this is an auto-officer ack

    Returns:
        (True, message) on success, (False, error_message) on failure.
    """
    if not officer_name or not officer_name.strip():
        return False, "Officer name is required."

    conn = get_db()
    try:
        alert = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if not alert:
            return False, f"Alert {alert_id} not found."

        if alert["status"] == "ACKNOWLEDGED":
            return False, (
                "Cannot acknowledge: alert is already 'ACKNOWLEDGED'. "
                "Double-acknowledge rejected."
            )
        if alert["status"] in ("CLEARED", "EXPIRED"):
            return False, f"Cannot acknowledge: alert is already '{alert['status']}'."

        old_status = alert["status"]

        now = utcnow()
        conn.execute(
            "UPDATE alerts SET status = 'ACKNOWLEDGED', officer_name = ?, "
            "ack_at = ?, ack_sim_time_s = ?, auto_officer = ? WHERE id = ?",
            (officer_name.strip(), now, sim_time_s, 1 if auto else 0, alert_id),
        )
        conn.commit()

        log_alert_event(alert_id, old_status, "ACKNOWLEDGED",
                        sim_time_s=sim_time_s,
                        details=f"Officer: {officer_name.strip()}"
                                + (" (auto-simulated)" if auto else ""))
        log_trip_event(alert["trip_id"], "ALERT_ACK",
                       f"Alert #{alert_id} acknowledged by {officer_name.strip()}"
                       + (" [Auto-Officer]" if auto else ""),
                       sim_time_s=sim_time_s,
                       data={"alert_id": alert_id, "auto": auto})
        return True, "Acknowledged."
    finally:
        conn.close()


# ── Escalation ─────────────────────────────────────────────

def check_escalations(trip_id, sim_time_s, settings):
    """
    Escalate any CREATED alerts whose escalation timeout has elapsed
    (measured in *simulated* time, so it works at 1×–10× speed).

    Returns:
        list of escalated alert IDs
    """
    timeout = float(settings.get("escalation_timeout_s", ESCALATION_TIMEOUT_S))
    conn = get_db()
    escalated = []
    try:
        rows = conn.execute(
            "SELECT id, created_sim_time_s, junction_id FROM alerts "
            "WHERE trip_id = ? AND status = 'CREATED'",
            (trip_id,),
        ).fetchall()

        for row in rows:
            created_at_sim = row["created_sim_time_s"] or 0
            if sim_time_s - created_at_sim >= timeout:
                now = utcnow()
                conn.execute(
                    "UPDATE alerts SET status = 'ESCALATED', "
                    "escalated_at = ?, escalated_sim_time_s = ? WHERE id = ?",
                    (now, sim_time_s, row["id"]),
                )
                conn.commit()
                escalated.append(row["id"])

                log_alert_event(row["id"], "CREATED", "ESCALATED",
                                sim_time_s=sim_time_s,
                                details=f"Escalated to Supervisor after {timeout}s timeout")
                log_trip_event(trip_id, "ALERT_ESCALATED",
                               f"Alert #{row['id']} escalated (timeout {timeout}s)",
                               sim_time_s=sim_time_s,
                               data={"alert_id": row["id"]})
    finally:
        conn.close()

    return escalated


# ── Clear / Expire ─────────────────────────────────────────

def clear_alert(alert_id, sim_time_s=None):
    """Clear an alert when the ambulance passes the junction."""
    conn = get_db()
    try:
        alert = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if not alert or alert["status"] in ("CLEARED", "EXPIRED"):
            return

        old = alert["status"]
        now = utcnow()
        conn.execute(
            "UPDATE alerts SET status = 'CLEARED', cleared_at = ?, cleared_sim_time_s = ? "
            "WHERE id = ?",
            (now, sim_time_s, alert_id),
        )
        conn.commit()

        log_alert_event(alert_id, old, "CLEARED",
                        sim_time_s=sim_time_s,
                        details="Ambulance passed the junction")
    finally:
        conn.close()


def expire_alerts_for_removed_junctions(trip_id, removed_junction_ids, sim_time_s=None):
    """
    Mark alerts as EXPIRED for junctions no longer on the route
    (e.g. after a reroute).
    """
    conn = get_db()
    try:
        for jid in removed_junction_ids:
            alert = conn.execute(
                "SELECT id, status FROM alerts WHERE trip_id = ? AND junction_id = ?",
                (trip_id, jid),
            ).fetchone()
            if alert and alert["status"] not in ("CLEARED", "EXPIRED"):
                old = alert["status"]
                now = utcnow()
                conn.execute(
                    "UPDATE alerts SET status = 'EXPIRED', cleared_at = ?, "
                    "cleared_sim_time_s = ? WHERE id = ?",
                    (now, sim_time_s, alert["id"]),
                )
                conn.commit()

                log_alert_event(alert["id"], old, "EXPIRED",
                                sim_time_s=sim_time_s,
                                details="Junction removed from route after reroute")
    finally:
        conn.close()


# ── Queries ────────────────────────────────────────────────

def get_alerts_for_trip(trip_id):
    """Return all alerts for a trip as a list of dicts."""
    conn = get_db()
    rows = conn.execute(
        "SELECT a.*, j.name AS junction_name, j.lat, j.lng "
        "FROM alerts a JOIN junctions j ON a.junction_id = j.id "
        "WHERE a.trip_id = ? ORDER BY a.created_sim_time_s",
        (trip_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
