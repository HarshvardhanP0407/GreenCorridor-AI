"""
analytics.py — Baseline vs Coordinated trip comparison for GreenCorridor AI.

The baseline is computed as a *separate simulated run* using the same route,
seed, and settings — but with **full signal waits** at every junction (no
alerts, no clearance).  The coordinated trip's actual wait times come from
what was recorded during the live simulation.

Both results are stored in the database as separate trip records so that
judges can inspect the raw data.

Every output is labelled "Simulated result".
"""

import json
import random

from config import (
    BASE_SPEED_KMH, SIGNAL_DELAY_S,
    CLEARANCE_FACTOR, ESCALATION_CLEARANCE_FACTOR,
)
from database import get_db, log_trip_event
from simulation import compute_cumulative_distances, get_congestion_factor, risk_level_from_seed


# ── Baseline computation ───────────────────────────────────

def compute_baseline_trip(trip_id):
    """
    Compute a baseline trip using the same route and seed as *trip_id*.

    Baseline rules
    ───────────────
    • Same effective speed (same seed → same congestion per segment).
    • At each signalised junction the ambulance waits the FULL signal delay.
    • No alerts, no clearance reductions.

    The result is stored as a new trip row with mode='baseline' and its
    duration is written to the original trip's baseline_duration_s.

    Returns:
        dict with baseline metrics, or None if trip not found.
    """
    conn = get_db()
    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not trip:
        conn.close()
        return None

    route_data = json.loads(trip["route_json"]) if trip["route_json"] else None
    if not route_data:
        conn.close()
        return None

    geometry = route_data.get("geometry", [])
    seed = trip["seed"]

    # Load junctions for this trip
    junctions = conn.execute(
        "SELECT * FROM junctions WHERE trip_id = ? ORDER BY order_index",
        (trip_id,),
    ).fetchall()
    junctions = [dict(j) for j in junctions]

    # Load settings that were active during the trip
    settings_rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in settings_rows}
    conn.close()

    # ── Simulate baseline ─────────────────────────────────
    base_speed_kmh = float(settings.get("base_speed_kmh", BASE_SPEED_KMH))
    signal_delay = float(settings.get("signal_delay_s", SIGNAL_DELAY_S))
    cumulative = compute_cumulative_distances(geometry)
    total_dist = cumulative[-1] if cumulative else 0

    # Compute travel time in small segments (same risk per segment as coordinated)
    segment_size = 200  # metres
    travel_time_s = 0.0
    pos = 0.0
    while pos < total_dist:
        progress = pos / max(total_dist, 1)
        risk = risk_level_from_seed(seed, progress)
        congestion = get_congestion_factor(risk, settings)
        speed_mps = (base_speed_kmh / 3.6) * (1 - congestion)
        if speed_mps <= 0:
            speed_mps = 0.1  # safety floor
        step = min(segment_size, total_dist - pos)
        travel_time_s += step / speed_mps
        pos += step

    # Full signal wait at every signalised junction
    n_signals = sum(1 for j in junctions if j.get("is_signalised"))
    total_wait_s = n_signals * signal_delay
    baseline_duration = travel_time_s + total_wait_s

    # ── Store baseline trip ───────────────────────────────
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO trips (status, seed, speed_multiplier, mode, "
        "route_json, total_distance_m, elapsed_sim_time_s, "
        "baseline_duration_s, coordinated_duration_s) "
        "VALUES ('FINISHED', ?, ?, 'baseline', ?, ?, ?, ?, ?)",
        (seed, trip["speed_multiplier"], trip["route_json"],
         total_dist, baseline_duration, baseline_duration, None),
    )
    baseline_trip_id = cur.lastrowid

    # Update original trip with baseline reference
    conn.execute(
        "UPDATE trips SET baseline_duration_s = ? WHERE id = ?",
        (baseline_duration, trip_id),
    )
    conn.commit()
    conn.close()

    log_trip_event(trip_id, "BASELINE_COMPUTED",
                   f"Baseline trip #{baseline_trip_id} computed: {baseline_duration:.1f}s",
                   data={"baseline_trip_id": baseline_trip_id,
                         "baseline_duration_s": baseline_duration})

    return {
        "baseline_trip_id": baseline_trip_id,
        "baseline_duration_s": round(baseline_duration, 2),
        "baseline_travel_time_s": round(travel_time_s, 2),
        "baseline_total_wait_s": round(total_wait_s, 2),
        "num_signalised_junctions": n_signals,
        "signal_delay_per_junction_s": signal_delay,
    }


# ── Analytics comparison ───────────────────────────────────

def get_analytics(trip_id):
    """
    Build the side-by-side Baseline vs Coordinated comparison.

    Returns a dict with all metrics, each labelled "Simulated result".
    Returns None if the trip hasn't finished yet.
    """
    conn = get_db()
    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not trip:
        conn.close()
        return None

    # If baseline hasn't been computed yet, compute it now
    if trip["baseline_duration_s"] is None:
        conn.close()
        baseline = compute_baseline_trip(trip_id)
        if not baseline:
            return None
        conn = get_db()
        trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()

    coordinated_duration = trip["elapsed_sim_time_s"] or 0
    baseline_duration = trip["baseline_duration_s"] or 0

    # Update coordinated duration in DB
    conn.execute(
        "UPDATE trips SET coordinated_duration_s = ? WHERE id = ?",
        (coordinated_duration, trip_id),
    )
    conn.commit()

    # Alert statistics
    alerts = conn.execute(
        "SELECT * FROM alerts WHERE trip_id = ?", (trip_id,)
    ).fetchall()

    total_alerts = len(alerts)
    # An ESCALATED alert is NEVER counted as acknowledged
    acked = sum(
        1 for a in alerts
        if a["status"] != "ESCALATED"
        and a["escalated_at"] is None
        and (a["status"] == "ACKNOWLEDGED" or a["ack_at"] is not None or a["ack_sim_time_s"] is not None)
    )
    escalated = sum(
        1 for a in alerts
        if a["status"] == "ESCALATED"
        or a["escalated_at"] is not None
        or a["escalated_sim_time_s"] is not None
    )

    # Average response time (ack_sim_time - created_sim_time)
    response_times = []
    for a in alerts:
        if a["ack_sim_time_s"] is not None and a["created_sim_time_s"] is not None:
            response_times.append(a["ack_sim_time_s"] - a["created_sim_time_s"])
    avg_response = (sum(response_times) / len(response_times)) if response_times else 0

    # Junction wait times (coordinated)
    junctions = conn.execute(
        "SELECT * FROM junctions WHERE trip_id = ? ORDER BY order_index",
        (trip_id,),
    ).fetchall()
    coord_total_wait = sum(j["wait_time_s"] or 0 for j in junctions)

    # Baseline total wait
    settings = {r["key"]: r["value"] for r in
                conn.execute("SELECT key, value FROM settings").fetchall()}
    signal_delay = float(settings.get("signal_delay_s", SIGNAL_DELAY_S))
    n_signals = sum(1 for j in junctions if j["is_signalised"])
    baseline_total_wait = n_signals * signal_delay

    time_saved = baseline_duration - coordinated_duration

    conn.close()

    return {
        "label": "Simulated result",
        "disclaimer": (
            "All values are from a deterministic simulation with seeded "
            "randomness. They do not represent real-world performance."
        ),
        "baseline": {
            "label": "Baseline (Simulated)",
            "duration_s": round(baseline_duration, 2),
            "total_wait_s": round(baseline_total_wait, 2),
            "description": "Full signal delay at every junction, no alerts.",
        },
        "coordinated": {
            "label": "Coordinated (Simulated)",
            "duration_s": round(coordinated_duration, 2),
            "total_wait_s": round(coord_total_wait, 2),
            "alerts_sent": total_alerts,
            "alerts_acknowledged": acked,
            "alerts_escalated": escalated,
            "avg_response_time_s": round(avg_response, 2),
            "description": "Alert-based corridor clearance with reduced waits.",
        },
        "time_saved_s": round(time_saved, 2),
        "time_saved_label": "Simulated time saved",
        "assumptions": [
            f"Base speed: {settings.get('base_speed_kmh', BASE_SPEED_KMH)} km/h",
            f"Signal delay per junction: {signal_delay} s",
            f"Clearance factor (acknowledged): {settings.get('clearance_factor', CLEARANCE_FACTOR)}",
            f"Clearance factor (escalated): {settings.get('escalation_clearance_factor', ESCALATION_CLEARANCE_FACTOR)}",
            "Congestion is deterministic (seeded RNG, varies by route segment).",
            "Baseline waits full signal delay; coordinated reduces it if alert is acted on.",
            "Auto-officer acknowledgement delay is seeded-random (simulated, not real).",
            "No real-world traffic, GPS, or police data is used.",
        ],
    }


# ── Assumptions text (for UI and README) ───────────────────

ASSUMPTIONS_TEXT = """
### Assumptions and Limitations (Simulated)

1. **No real data** — GPS, traffic, and police responses are all simulated.
2. **Deterministic** — Same seed always produces the same trip.
3. **Baseline** — Ambulance waits the full signal delay at every junction.
4. **Coordinated** — If the junction alert was acknowledged before arrival,
   wait is reduced by the clearance factor (default 70%).  If escalated,
   reduction is smaller (default 30%).
5. **Congestion** — Varies by route segment using seeded randomness,
   not real traffic data.
6. **Time saved** — Computed as: baseline_duration − coordinated_duration,
   using durations recorded in SQLite.  It is NOT a real-world prediction.
7. **Auto-officer** — Simulates a police officer acknowledging after a
   random delay.  Real response times would differ.
"""
