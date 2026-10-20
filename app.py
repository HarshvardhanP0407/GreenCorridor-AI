"""
app.py — Flask application for GreenCorridor AI.

Entry point: python app.py
Binds to 127.0.0.1:5000 only.  debug=False by default.
Uses threaded=True so the SSE stream does not block other requests.
"""

import csv
import io
import json
import queue
import threading
from datetime import datetime, timezone

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory)

from config import (
    HOST, PORT, DEBUG, DEFAULT_SEED, DEFAULT_SPEED_MULTIPLIER,
    BASE_SPEED_KMH, SIGNAL_DELAY_S, CONGESTION_LOW, CONGESTION_MEDIUM,
    CONGESTION_HIGH, ALERT_START_S, ALERT_MIN_S, ESCALATION_TIMEOUT_S,
    AUTO_OFFICER_ENABLED, AUTO_OFFICER_MIN_DELAY_S, AUTO_OFFICER_MAX_DELAY_S,
    ROADBLOCK_PENALTY_S, CLEARANCE_FACTOR, ESCALATION_CLEARANCE_FACTOR,
)
import database
from database import (
    init_db, get_db, get_all_settings, get_setting, set_setting,
    log_trip_event, utcnow,
)
from routing import get_route, load_sample_route
from simulation import engine, compute_eta
from alerts import (
    check_and_create_alerts, acknowledge_alert, check_escalations,
    clear_alert, expire_alerts_for_removed_junctions, get_alerts_for_trip,
)
from analytics import get_analytics, compute_baseline_trip, ASSUMPTIONS_TEXT

# ── Flask app ──────────────────────────────────────────────
app = Flask(__name__)

# ── SSE client queues ──────────────────────────────────────
sse_clients = []            # list of queue.Queue
sse_lock = threading.Lock()


def push_sse(data):
    """Send data to all connected SSE clients."""
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


# ── Wire simulation callbacks ─────────────────────────────

def _on_tick(state):
    """Called every simulation tick — push state to SSE clients."""
    push_sse({"type": "tick", "data": state})

    # Persist position in DB
    if state.get("trip_id"):
        try:
            conn = get_db()
            conn.execute(
                "UPDATE trips SET current_distance_m = ?, elapsed_sim_time_s = ?, "
                "current_position_json = ?, status = ? WHERE id = ?",
                (state["current_distance_m"], state["elapsed_sim_time_s"],
                 json.dumps({"lat": state["current_lat"], "lng": state["current_lng"]}),
                 state["status"], state["trip_id"]),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # If finished, compute baseline
    if state.get("status") == "FINISHED" and state.get("trip_id"):
        threading.Thread(
            target=_on_trip_finished, args=(state["trip_id"],), daemon=True
        ).start()


def _on_trip_finished(trip_id):
    """Compute baseline and push analytics when trip finishes."""
    try:
        compute_baseline_trip(trip_id)
        analytics = get_analytics(trip_id)
        push_sse({"type": "analytics", "data": analytics})
        push_sse({"type": "finished", "data": {"trip_id": trip_id}})
    except Exception:
        pass


def _on_alert_check(trip_id, sim_time_s):
    """Create alerts and check escalations during simulation tick."""
    settings = get_all_settings()

    # Get junction ETAs from engine
    junctions_eta = engine.get_junctions_with_eta()

    # Create new alerts
    new_ids = check_and_create_alerts(trip_id, junctions_eta, sim_time_s, settings)

    # Schedule auto-officer for newly created alerts
    for aid in new_ids:
        conn = get_db()
        alert = conn.execute("SELECT junction_id FROM alerts WHERE id = ?", (aid,)).fetchone()
        conn.close()
        if alert:
            engine.schedule_auto_officer(alert["junction_id"], sim_time_s)

    # Push new alerts to SSE
    if new_ids:
        alerts = get_alerts_for_trip(trip_id)
        push_sse({"type": "alerts", "data": alerts})

    # Check escalations
    escalated = check_escalations(trip_id, sim_time_s, settings)
    if escalated:
        alerts = get_alerts_for_trip(trip_id)
        push_sse({"type": "alerts", "data": alerts})


def _on_auto_ack(alert_id, officer_name, sim_time_s):
    """Auto-officer acknowledges an alert."""
    ok, msg = acknowledge_alert(alert_id, officer_name, sim_time_s, auto=True)
    if ok:
        # Push updated alerts
        conn = get_db()
        alert = conn.execute("SELECT trip_id FROM alerts WHERE id = ?",
                             (alert_id,)).fetchone()
        conn.close()
        if alert:
            alerts = get_alerts_for_trip(alert["trip_id"])
            push_sse({"type": "alerts", "data": alerts})


def _on_junction_passed(trip_id, junction_id, sim_time_s):
    """Handle ambulance passing a junction."""
    # Record wait time and arrival time in DB
    conn = get_db()
    wait = engine._junction_wait_remaining.get(junction_id, 0)
    # The actual wait was: signal_delay - remaining (already consumed)
    junc = conn.execute("SELECT * FROM junctions WHERE id = ?",
                        (junction_id,)).fetchone()
    if junc:
        signal_delay = float(get_all_settings().get("signal_delay_s", SIGNAL_DELAY_S))
        actual_wait = signal_delay  # default
        # Check alert status to determine actual wait
        alert = conn.execute(
            "SELECT status FROM alerts WHERE trip_id = ? AND junction_id = ?",
            (trip_id, junction_id),
        ).fetchone()
        if alert:
            clearance = float(get_all_settings().get("clearance_factor", CLEARANCE_FACTOR))
            esc_clear = float(get_all_settings().get("escalation_clearance_factor",
                                                      ESCALATION_CLEARANCE_FACTOR))
            if alert["status"] == "ACKNOWLEDGED":
                actual_wait = signal_delay * (1 - clearance)
            elif alert["status"] == "ESCALATED":
                actual_wait = signal_delay * (1 - esc_clear)
            # Clear the alert now
            clear_alert(conn.execute(
                "SELECT id FROM alerts WHERE trip_id = ? AND junction_id = ?",
                (trip_id, junction_id),
            ).fetchone()["id"], sim_time_s)

        conn.execute(
            "UPDATE junctions SET is_passed = 1, wait_time_s = ?, "
            "arrival_sim_time_s = ? WHERE id = ?",
            (actual_wait, sim_time_s, junction_id),
        )
        conn.commit()

    conn.close()

    log_trip_event(trip_id, "JUNCTION_PASSED",
                   f"Passed junction #{junction_id}",
                   sim_time_s=sim_time_s,
                   data={"junction_id": junction_id})

    # Push junction update
    junctions = engine.get_junctions_with_eta()
    push_sse({"type": "junctions", "data": junctions})


# Set callbacks
engine.on_tick = _on_tick
engine.on_alert_check = _on_alert_check
engine.on_auto_ack = _on_auto_ack
engine.on_junction_passed = _on_junction_passed


# ── Helper: load settings with defaults ────────────────────

def _get_settings_with_defaults():
    """Return settings dict with defaults for any unset keys."""
    defaults = {
        "base_speed_kmh": str(BASE_SPEED_KMH),
        "signal_delay_s": str(SIGNAL_DELAY_S),
        "congestion_low": str(CONGESTION_LOW),
        "congestion_medium": str(CONGESTION_MEDIUM),
        "congestion_high": str(CONGESTION_HIGH),
        "alert_start_s": str(ALERT_START_S),
        "alert_min_s": str(ALERT_MIN_S),
        "escalation_timeout_s": str(ESCALATION_TIMEOUT_S),
        "auto_officer_enabled": str(AUTO_OFFICER_ENABLED),
        "auto_officer_min_delay_s": str(AUTO_OFFICER_MIN_DELAY_S),
        "auto_officer_max_delay_s": str(AUTO_OFFICER_MAX_DELAY_S),
        "roadblock_penalty_s": str(ROADBLOCK_PENALTY_S),
        "clearance_factor": str(CLEARANCE_FACTOR),
        "escalation_clearance_factor": str(ESCALATION_CLEARANCE_FACTOR),
        "seed": str(DEFAULT_SEED),
        "speed_multiplier": str(DEFAULT_SPEED_MULTIPLIER),
    }
    stored = get_all_settings()
    merged = {**defaults, **stored}
    return merged


# ── Validation helper ──────────────────────────────────────

SETTINGS_VALIDATION = {
    "base_speed_kmh":              (10, 120),
    "signal_delay_s":              (0, 120),
    "congestion_low":              (0.0, 0.6),
    "congestion_medium":           (0.0, 0.6),
    "congestion_high":             (0.0, 0.6),
    "alert_start_s":               (10, 600),
    "alert_min_s":                 (5, 600),
    "escalation_timeout_s":        (5, 300),
    "auto_officer_min_delay_s":    (1, 60),
    "auto_officer_max_delay_s":    (1, 60),
    "roadblock_penalty_s":         (0, 600),
    "clearance_factor":            (0.0, 1.0),
    "escalation_clearance_factor": (0.0, 1.0),
    "seed":                        (0, 999999),
    "speed_multiplier":            (1, 10),
}


def _validate_setting(key, value):
    """Validate a setting value. Returns (parsed_value, error_msg)."""
    if key == "auto_officer_enabled":
        if str(value).lower() in ("true", "false", "1", "0", "yes", "no"):
            return str(value).lower(), None
        return None, f"Invalid boolean for {key}: {value}"

    if key in SETTINGS_VALIDATION:
        lo, hi = SETTINGS_VALIDATION[key]
        try:
            v = float(value)
        except (ValueError, TypeError):
            return None, f"Invalid number for {key}: {value}"
        if v < lo or v > hi:
            return None, f"{key} must be between {lo} and {hi}, got {v}"
        return str(v), None

    return str(value), None  # unknown key: accept as-is


# ══════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════

# ── Pages ──────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the single-page application."""
    return render_template("index.html")


# ── SSE stream ─────────────────────────────────────────────

@app.route("/api/sse")
def sse_stream():
    """Server-Sent Events endpoint for real-time updates."""
    q = queue.Queue(maxsize=100)
    with sse_lock:
        sse_clients.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ── Trip lifecycle ─────────────────────────────────────────

@app.route("/api/trip/start", methods=["POST"])
def trip_start():
    """Start a new simulation trip."""
    if engine.status in ("RUNNING", "PAUSED"):
        return jsonify({"error": "A trip is already active. Reset first."}), 400

    settings = _get_settings_with_defaults()
    seed = int(float(settings["seed"]))
    speed_mult = float(settings["speed_multiplier"])

    # Get route
    route_data = get_route()  # Uses OSRM or fallback

    # Create trip in DB
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO trips (status, seed, speed_multiplier, mode, "
        "route_json, total_distance_m, start_time) "
        "VALUES ('RUNNING', ?, ?, 'coordinated', ?, ?, ?)",
        (seed, speed_mult, json.dumps(route_data),
         route_data["distance_m"], utcnow()),
    )
    trip_id = cur.lastrowid
    conn.commit()

    # Insert junctions
    junctions_db = []
    for idx, junc in enumerate(route_data.get("junctions", [])):
        cur2 = conn.execute(
            "INSERT INTO junctions (trip_id, name, lat, lng, "
            "distance_from_start_m, order_index, is_signalised, approximate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (trip_id, junc["name"], junc["lat"], junc["lng"],
             junc["distance_from_start_m"], idx,
             1 if junc.get("is_signalised", True) else 0,
             1 if junc.get("approximate", False) else 0),
        )
        junctions_db.append({
            "id": cur2.lastrowid,
            "name": junc["name"],
            "lat": junc["lat"],
            "lng": junc["lng"],
            "distance_from_start_m": junc["distance_from_start_m"],
            "order_index": idx,
            "is_signalised": junc.get("is_signalised", True),
            "is_blocked": False,
            "is_passed": False,
        })
    conn.commit()
    conn.close()

    log_trip_event(trip_id, "TRIP_STARTED",
                   f"Trip started with seed={seed}, speed={speed_mult}x",
                   sim_time_s=0,
                   data={"seed": seed, "speed_multiplier": speed_mult,
                         "fallback": route_data.get("fallback", False)})

    # Start simulation engine
    engine.start(
        trip_id=trip_id,
        seed=seed,
        speed_multiplier=speed_mult,
        settings=settings,
        route_geometry=route_data["geometry"],
        junctions_db=junctions_db,
        using_fallback=route_data.get("fallback", False),
        fallback_reason=route_data.get("fallback_reason", ""),
    )

    return jsonify({
        "trip_id": trip_id,
        "status": "RUNNING",
        "seed": seed,
        "speed_multiplier": speed_mult,
        "fallback": route_data.get("fallback", False),
        "fallback_reason": route_data.get("fallback_reason"),
        "total_distance_m": route_data["distance_m"],
        "num_junctions": len(junctions_db),
    })


@app.route("/api/trip/pause", methods=["POST"])
def trip_pause():
    if engine.status != "RUNNING":
        return jsonify({"error": "Trip is not running."}), 400
    engine.pause()
    log_trip_event(engine.trip_id, "TRIP_PAUSED", "Trip paused",
                   sim_time_s=engine.elapsed_sim_time_s)
    return jsonify({"status": "PAUSED"})


@app.route("/api/trip/resume", methods=["POST"])
def trip_resume():
    if engine.status != "PAUSED":
        return jsonify({"error": "Trip is not paused."}), 400
    engine.resume()
    log_trip_event(engine.trip_id, "TRIP_RESUMED", "Trip resumed",
                   sim_time_s=engine.elapsed_sim_time_s)
    return jsonify({"status": "RUNNING"})


@app.route("/api/trip/reset", methods=["POST"])
def trip_reset():
    tid = engine.trip_id
    if tid:
        conn = get_db()
        conn.execute("UPDATE trips SET status = 'RESET', end_time = ? WHERE id = ?",
                      (utcnow(), tid))
        conn.commit()
        conn.close()
        log_trip_event(tid, "TRIP_RESET", "Trip reset",
                       sim_time_s=engine.elapsed_sim_time_s)
    engine.reset()
    return jsonify({"status": "IDLE"})


@app.route("/api/trip/speed", methods=["POST"])
def trip_speed():
    data = request.get_json(silent=True) or {}
    mult = data.get("speed_multiplier")
    if mult is None:
        return jsonify({"error": "speed_multiplier required."}), 400
    try:
        mult = float(mult)
        if mult not in (1, 2, 5, 10):
            return jsonify({"error": "speed_multiplier must be 1, 2, 5, or 10."}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid speed_multiplier."}), 400

    engine.set_speed(mult)
    if engine.trip_id:
        set_setting("speed_multiplier", str(mult))
    return jsonify({"speed_multiplier": mult})


@app.route("/api/trip/state")
def trip_state():
    return jsonify(engine.get_state())


# ── Junctions ──────────────────────────────────────────────

@app.route("/api/junctions")
def api_junctions():
    if engine.status == "IDLE":
        return jsonify([])
    return jsonify(engine.get_junctions_with_eta())


# ── Alerts ─────────────────────────────────────────────────

@app.route("/api/alerts")
def api_alerts():
    if not engine.trip_id:
        return jsonify([])
    return jsonify(get_alerts_for_trip(engine.trip_id))


@app.route("/api/alerts/<int:alert_id>/acknowledge", methods=["POST"])
def api_ack(alert_id):
    data = request.get_json(silent=True) or {}
    officer = data.get("officer_name", "").strip()
    if not officer:
        return jsonify({"error": "officer_name is required."}), 400

    ok, msg = acknowledge_alert(alert_id, officer, engine.elapsed_sim_time_s)
    if not ok:
        return jsonify({"error": msg}), 400

    # Push updated alerts
    if engine.trip_id:
        alerts = get_alerts_for_trip(engine.trip_id)
        push_sse({"type": "alerts", "data": alerts})

    return jsonify({"success": True, "message": msg})


# ── Roadblock ──────────────────────────────────────────────

@app.route("/api/roadblock/set", methods=["POST"])
def api_roadblock_set():
    """Block a junction and apply penalty or reroute."""
    if not engine.trip_id:
        return jsonify({"error": "No active trip."}), 400

    data = request.get_json(silent=True) or {}
    junction_id = data.get("junction_id")
    if junction_id is None:
        return jsonify({"error": "junction_id required."}), 400

    conn = get_db()
    junc = conn.execute("SELECT * FROM junctions WHERE id = ? AND trip_id = ?",
                        (junction_id, engine.trip_id)).fetchone()
    if not junc:
        conn.close()
        return jsonify({"error": "Junction not found."}), 404

    if junc["is_passed"]:
        conn.close()
        return jsonify({"error": "Junction already passed."}), 400

    # Mark as blocked
    conn.execute("UPDATE junctions SET is_blocked = 1 WHERE id = ?", (junction_id,))
    conn.commit()
    conn.close()

    # Update engine junction state
    for j in engine.junctions:
        if j["id"] == junction_id:
            j["is_blocked"] = True
            break

    # Try OSRM reroute from current position to destination
    settings = _get_settings_with_defaults()
    penalty = float(settings.get("roadblock_penalty_s", ROADBLOCK_PENALTY_S))

    # For the demo, apply penalty (OSRM public demo doesn't support avoid)
    engine.penalty_remaining_s = penalty

    log_trip_event(engine.trip_id, "ROADBLOCK_SET",
                   f"Junction #{junction_id} ({junc['name']}) blocked. "
                   f"Penalty: {penalty}s applied.",
                   sim_time_s=engine.elapsed_sim_time_s,
                   data={"junction_id": junction_id, "penalty_s": penalty})

    push_sse({"type": "roadblock", "data": {
        "junction_id": junction_id,
        "name": junc["name"],
        "action": "set",
        "penalty_s": penalty,
        "message": (f"Road blocked at {junc['name']}. "
                    f"Reroute not available — {penalty}s penalty applied."),
    }})

    return jsonify({
        "success": True,
        "rerouted": False,
        "penalty_s": penalty,
        "message": (f"Road blocked at {junc['name']}. OSRM public demo does not "
                    f"support avoidance — {penalty}s penalty delay applied."),
    })


@app.route("/api/roadblock/clear", methods=["POST"])
def api_roadblock_clear():
    """Clear a roadblock from a junction."""
    if not engine.trip_id:
        return jsonify({"error": "No active trip."}), 400

    data = request.get_json(silent=True) or {}
    junction_id = data.get("junction_id")
    if junction_id is None:
        return jsonify({"error": "junction_id required."}), 400

    conn = get_db()
    conn.execute("UPDATE junctions SET is_blocked = 0 WHERE id = ? AND trip_id = ?",
                 (junction_id, engine.trip_id))
    conn.commit()
    conn.close()

    for j in engine.junctions:
        if j["id"] == junction_id:
            j["is_blocked"] = False
            break

    engine.penalty_remaining_s = 0

    log_trip_event(engine.trip_id, "ROADBLOCK_CLEARED",
                   f"Junction #{junction_id} roadblock cleared.",
                   sim_time_s=engine.elapsed_sim_time_s,
                   data={"junction_id": junction_id})

    push_sse({"type": "roadblock", "data": {
        "junction_id": junction_id,
        "action": "clear",
    }})

    return jsonify({"success": True, "message": "Roadblock cleared."})


# ── Settings ───────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(_get_settings_with_defaults())


@app.route("/api/settings", methods=["POST"])
def api_settings_update():
    data = request.get_json(silent=True) or {}
    errors = {}
    updated = {}

    for key, value in data.items():
        parsed, err = _validate_setting(key, value)
        if err:
            errors[key] = err
        else:
            set_setting(key, parsed)
            updated[key] = parsed

    # Update engine settings cache if running
    if engine.status in ("RUNNING", "PAUSED"):
        engine.settings = _get_settings_with_defaults()

    if errors:
        return jsonify({"updated": updated, "errors": errors}), 400

    return jsonify({"updated": updated})


# ── Analytics ──────────────────────────────────────────────

@app.route("/api/analytics")
def api_analytics():
    if not engine.trip_id:
        return jsonify({"error": "No active or completed trip."}), 404

    # Find the most recent finished trip
    conn = get_db()
    trip = conn.execute(
        "SELECT id, status FROM trips WHERE mode = 'coordinated' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if not trip:
        return jsonify({"error": "No completed trip."}), 404

    result = get_analytics(trip["id"])
    if not result:
        return jsonify({"error": "Analytics not available yet."}), 404

    return jsonify(result)


# ── Events ─────────────────────────────────────────────────

@app.route("/api/events")
def api_events():
    """Return event history, optionally filtered."""
    event_type = request.args.get("type")
    trip_id = request.args.get("trip_id")

    conn = get_db()
    query = "SELECT * FROM trip_events WHERE 1=1"
    params = []

    if trip_id:
        query += " AND trip_id = ?"
        params.append(int(trip_id))
    elif engine.trip_id:
        query += " AND trip_id = ?"
        params.append(engine.trip_id)

    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)

    query += " ORDER BY id DESC LIMIT 500"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return jsonify([dict(r) for r in rows])


@app.route("/api/events/csv")
def api_events_csv():
    """Download event history as CSV."""
    trip_id = request.args.get("trip_id")

    conn = get_db()
    if trip_id:
        rows = conn.execute(
            "SELECT * FROM trip_events WHERE trip_id = ? ORDER BY id",
            (int(trip_id),),
        ).fetchall()
    elif engine.trip_id:
        rows = conn.execute(
            "SELECT * FROM trip_events WHERE trip_id = ? ORDER BY id",
            (engine.trip_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM trip_events ORDER BY id").fetchall()
    conn.close()

    # Build CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "trip_id", "event_type", "description",
                     "timestamp", "sim_time_s", "data_json"])
    for r in rows:
        writer.writerow([r["id"], r["trip_id"], r["event_type"],
                         r["description"], r["timestamp"],
                         r["sim_time_s"], r["data_json"]])

    csv_bytes = output.getvalue()
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=greencorridor_events.csv"},
    )


# ── Route info (for map) ──────────────────────────────────

@app.route("/api/route")
def api_route():
    """Return the current route geometry for the map."""
    if not engine.trip_id:
        return jsonify({"error": "No active trip."}), 404
    conn = get_db()
    trip = conn.execute("SELECT route_json FROM trips WHERE id = ?",
                        (engine.trip_id,)).fetchone()
    conn.close()
    if trip and trip["route_json"]:
        return jsonify(json.loads(trip["route_json"]))
    return jsonify({"error": "No route data."}), 404


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("  GreenCorridor AI — Predictive Ambulance Corridor")
    print("  SIMULATION ONLY — no real GPS, traffic, or police")
    print(f"  Open http://{HOST}:{PORT} in your browser")
    print("=" * 60)
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True)
