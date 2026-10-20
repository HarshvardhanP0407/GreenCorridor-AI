"""
simulation.py — Server-side ambulance simulation engine.

Moves the ambulance along the route geometry using simulated time.
Each tick: advance position → compute ETA → trigger alert checks → push state.
Uses a seeded RNG for deterministic behaviour.

This module does NOT import alerts.py — it uses callbacks set by app.py
to avoid circular imports.
"""

import math
import random
import threading
import time

from config import (
    DEFAULT_SEED, TICK_INTERVAL_REAL_S, DEFAULT_SPEED_MULTIPLIER,
    BASE_SPEED_KMH, CONGESTION_LOW, CONGESTION_MEDIUM, CONGESTION_HIGH,
    SIGNAL_DELAY_S, AUTO_OFFICER_ENABLED,
    AUTO_OFFICER_MIN_DELAY_S, AUTO_OFFICER_MAX_DELAY_S,
    CLEARANCE_FACTOR, ESCALATION_CLEARANCE_FACTOR,
)
from database import get_db


# ── Geometry helpers ───────────────────────────────────────

def haversine_m(p1, p2):
    """
    Distance in metres between two [lat, lng] points using the
    Haversine formula.
    """
    R = 6_371_000  # Earth radius in metres
    lat1, lng1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lng2 = math.radians(p2[0]), math.radians(p2[1])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(min(1.0, math.sqrt(a)))


def compute_cumulative_distances(geometry):
    """Return a list of cumulative distances (metres) at each vertex."""
    dists = [0.0]
    for i in range(1, len(geometry)):
        dists.append(dists[-1] + haversine_m(geometry[i - 1], geometry[i]))
    return dists


def interpolate_position(geometry, cumulative, distance_m):
    """
    Return (lat, lng, segment_index) at a given distance along the route.
    """
    if distance_m <= 0:
        return geometry[0][0], geometry[0][1], 0
    if distance_m >= cumulative[-1]:
        return geometry[-1][0], geometry[-1][1], len(geometry) - 1

    for i in range(1, len(cumulative)):
        if cumulative[i] >= distance_m:
            seg_len = cumulative[i] - cumulative[i - 1]
            if seg_len == 0:
                return geometry[i][0], geometry[i][1], i
            frac = (distance_m - cumulative[i - 1]) / seg_len
            lat = geometry[i - 1][0] + frac * (geometry[i][0] - geometry[i - 1][0])
            lng = geometry[i - 1][1] + frac * (geometry[i][1] - geometry[i - 1][1])
            return lat, lng, i

    return geometry[-1][0], geometry[-1][1], len(geometry) - 1


# ── ETA model (rule-based, NOT AI) ─────────────────────────

def get_congestion_factor(risk_level, settings=None):
    """Map a risk-level string to a numeric congestion factor."""
    s = settings or {}
    mapping = {
        "low":    float(s.get("congestion_low",    CONGESTION_LOW)),
        "medium": float(s.get("congestion_medium",  CONGESTION_MEDIUM)),
        "high":   float(s.get("congestion_high",    CONGESTION_HIGH)),
    }
    return mapping.get(risk_level, CONGESTION_LOW)


def compute_eta(remaining_distance_m,
                num_remaining_signalised_junctions,
                risk_level="low",
                settings=None):
    """
    Rule-based ETA model.

    Formula
    -------
    ETA_seconds = remaining_distance_m / effective_speed_mps
                  + expected_signal_delay_s

    effective_speed_mps = base_speed_kmh / 3.6 × (1 − congestion_factor)
    expected_signal_delay_s = num_remaining × signal_delay_per_junction

    Returns
    -------
    float : ETA in seconds (may be float('inf') if speed is zero).
    """
    if remaining_distance_m < 0:
        raise ValueError("remaining_distance_m cannot be negative")
    if num_remaining_signalised_junctions < 0:
        raise ValueError("num_remaining_signalised_junctions cannot be negative")

    s = settings or {}
    base_speed = float(s.get("base_speed_kmh", BASE_SPEED_KMH))
    if base_speed <= 0:
        raise ValueError("base_speed_kmh must be positive")

    signal_delay = float(s.get("signal_delay_s", SIGNAL_DELAY_S))
    if signal_delay < 0:
        raise ValueError("signal_delay_s cannot be negative")

    congestion = get_congestion_factor(risk_level, s)
    if congestion < 0.0 or congestion >= 1.0:
        raise ValueError("congestion factor must be in [0.0, 1.0)")

    effective_speed = (base_speed / 3.6) * (1 - congestion)
    if effective_speed <= 0:
        return float("inf")

    travel_time = remaining_distance_m / effective_speed
    signal_time = num_remaining_signalised_junctions * signal_delay
    return travel_time + signal_time


def risk_level_from_seed(seed, progress_frac):
    """Deterministic risk level based on seed + route progress."""
    r = random.Random(seed + int(progress_frac * 5))
    v = r.random()
    if v < 0.5:
        return "low"
    elif v < 0.8:
        return "medium"
    return "high"


# ── Simulation engine (singleton) ──────────────────────────

class SimulationEngine:
    """
    Manages the ambulance simulation state and background tick loop.

    Callbacks (set by app.py to avoid circular imports):
        on_tick(state_dict)            — push SSE update
        on_alert_check(trip_id, sim_t) — create alerts / check escalations
        on_auto_ack(alert_id, name, sim_t) — auto-officer acknowledge
        on_junction_passed(trip_id, junction_id, sim_t) — handle junction pass
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._paused = False

        # ── Trip state ───────────────────
        self.trip_id = None
        self.status = "IDLE"               # IDLE / RUNNING / PAUSED / FINISHED
        self.seed = DEFAULT_SEED
        self.rng = random.Random(DEFAULT_SEED)
        self.speed_multiplier = DEFAULT_SPEED_MULTIPLIER

        # ── Route ────────────────────────
        self.geometry = []                  # [[lat, lng], …]
        self.cumulative = []                # cumulative distances
        self.total_distance_m = 0.0
        self.junctions = []                 # list of junction dicts (with 'id')

        # ── Position ─────────────────────
        self.current_distance_m = 0.0
        self.elapsed_sim_time_s = 0.0
        self.current_lat = 0.0
        self.current_lng = 0.0

        # ── Settings cache ───────────────
        self.settings = {}

        # ── Fallback ─────────────────────
        self.using_fallback = False
        self.fallback_reason = ""

        # ── Junction wait tracking ───────
        self._waiting_at_junction = None    # junction id
        self._junction_wait_remaining = {}  # {jid: remaining_s}

        # ── Roadblock ────────────────────
        self.penalty_remaining_s = 0.0

        # ── Auto-officer schedule ────────
        # {junction_id: sim_time_when_ack_should_fire}
        self.auto_officer_schedule = {}

        # ── Callbacks ────────────────────
        self.on_tick = None
        self.on_alert_check = None
        self.on_auto_ack = None
        self.on_junction_passed = None

    # ── Lifecycle ──────────────────────────────────────────

    def start(self, trip_id, seed, speed_multiplier, settings,
              route_geometry, junctions_db,
              using_fallback=False, fallback_reason=""):
        """Start a new simulation run."""
        with self._lock:
            self.trip_id = trip_id
            self.seed = seed
            self.rng = random.Random(seed)
            self.speed_multiplier = speed_multiplier
            self.settings = settings
            self.using_fallback = using_fallback
            self.fallback_reason = fallback_reason

            self.geometry = route_geometry
            self.cumulative = compute_cumulative_distances(route_geometry)
            self.total_distance_m = self.cumulative[-1] if self.cumulative else 0
            self.junctions = junctions_db

            self.current_distance_m = 0.0
            self.elapsed_sim_time_s = 0.0
            if self.geometry:
                self.current_lat = self.geometry[0][0]
                self.current_lng = self.geometry[0][1]

            self.status = "RUNNING"
            self._paused = False
            self._running = True
            self.penalty_remaining_s = 0.0
            self.auto_officer_schedule = {}
            self._junction_wait_remaining = {}
            self._waiting_at_junction = None

            self._thread = threading.Thread(target=self._tick_loop, daemon=True)
            self._thread.start()

    def pause(self):
        with self._lock:
            if self.status == "RUNNING":
                self._paused = True
                self.status = "PAUSED"

    def resume(self):
        with self._lock:
            if self.status == "PAUSED":
                self._paused = False
                self.status = "RUNNING"

    def reset(self):
        with self._lock:
            self._running = False
            self._paused = False
            self.status = "IDLE"
            self.trip_id = None
            self.current_distance_m = 0.0
            self.elapsed_sim_time_s = 0.0
            self.geometry = []
            self.junctions = []
            self.auto_officer_schedule = {}
            self._junction_wait_remaining = {}
            self._waiting_at_junction = None

    def set_speed(self, multiplier):
        with self._lock:
            self.speed_multiplier = multiplier

    def get_state(self):
        """Thread-safe snapshot of the simulation state."""
        with self._lock:
            return self._state_dict()

    def _state_dict(self):
        """Build the state dict (call while holding the lock)."""
        return {
            "trip_id": self.trip_id,
            "status": self.status,
            "seed": self.seed,
            "speed_multiplier": self.speed_multiplier,
            "current_lat": self.current_lat,
            "current_lng": self.current_lng,
            "current_distance_m": round(self.current_distance_m, 1),
            "total_distance_m": round(self.total_distance_m, 1),
            "elapsed_sim_time_s": round(self.elapsed_sim_time_s, 2),
            "using_fallback": self.using_fallback,
            "fallback_reason": self.fallback_reason,
        }

    # ── Tick loop ──────────────────────────────────────────

    def _tick_loop(self):
        """Background thread: sleep → tick → repeat."""
        while self._running:
            time.sleep(TICK_INTERVAL_REAL_S)
            if not self._running:
                break
            if self._paused:
                continue
            with self._lock:
                self._tick()

    def _tick(self):
        """One simulation tick (called while holding the lock)."""
        dt = TICK_INTERVAL_REAL_S * self.speed_multiplier  # simulated seconds

        # ── Determine speed ──────────────
        progress = self.current_distance_m / max(self.total_distance_m, 1)
        risk = risk_level_from_seed(self.seed, progress)
        base_speed = float(self.settings.get("base_speed_kmh", BASE_SPEED_KMH))
        congestion = get_congestion_factor(risk, self.settings)
        effective_speed_mps = (base_speed / 3.6) * (1 - congestion)

        # ── Waiting at a junction? ───────
        if self._waiting_at_junction is not None:
            jid = self._waiting_at_junction
            remaining = self._junction_wait_remaining.get(jid, 0)
            remaining -= dt
            if remaining <= 0:
                self._junction_wait_remaining[jid] = 0
                self._waiting_at_junction = None
                # Notify: junction passed
                if self.on_junction_passed:
                    self.on_junction_passed(self.trip_id, jid, self.elapsed_sim_time_s)
            else:
                self._junction_wait_remaining[jid] = remaining
            self.elapsed_sim_time_s += dt
            self._do_alert_check()
            self._check_auto_officer()
            self._notify()
            return

        # ── Roadblock penalty? ───────────
        if self.penalty_remaining_s > 0:
            self.penalty_remaining_s -= dt
            self.elapsed_sim_time_s += dt
            self._do_alert_check()
            self._check_auto_officer()
            self._notify()
            return

        # ── Move the ambulance ───────────
        move_m = effective_speed_mps * dt
        self.current_distance_m += move_m
        self.elapsed_sim_time_s += dt

        # Check finish
        if self.current_distance_m >= self.total_distance_m:
            self.current_distance_m = self.total_distance_m
            lat, lng, _ = interpolate_position(
                self.geometry, self.cumulative, self.current_distance_m
            )
            self.current_lat, self.current_lng = lat, lng
            self.status = "FINISHED"
            self._running = False
            self._notify()
            return

        # Update position
        lat, lng, _ = interpolate_position(
            self.geometry, self.cumulative, self.current_distance_m
        )
        self.current_lat, self.current_lng = lat, lng

        # ── Check junction arrivals ──────
        for junc in self.junctions:
            if junc.get("is_passed"):
                continue
            if self.current_distance_m >= junc["distance_from_start_m"]:
                junc["is_passed"] = True
                wait = self._junction_wait(junc)
                if wait > 0:
                    self._waiting_at_junction = junc["id"]
                    self._junction_wait_remaining[junc["id"]] = wait
                else:
                    if self.on_junction_passed:
                        self.on_junction_passed(
                            self.trip_id, junc["id"], self.elapsed_sim_time_s
                        )

        # ── Alert check & auto-officer ───
        self._do_alert_check()
        self._check_auto_officer()
        self._notify()

    # ── Junction wait (coordinated mode) ───────────────────

    def _junction_wait(self, junc):
        """
        Determine how long the ambulance waits at a signalised junction.
        Depends on whether the alert was acknowledged/escalated.
        """
        if not junc.get("is_signalised", True):
            return 0.0

        signal_delay = float(self.settings.get("signal_delay_s", SIGNAL_DELAY_S))

        # Look up alert status in DB
        conn = get_db()
        alert = conn.execute(
            "SELECT status FROM alerts WHERE trip_id = ? AND junction_id = ?",
            (self.trip_id, junc["id"]),
        ).fetchone()
        conn.close()

        if alert:
            clearance = float(self.settings.get("clearance_factor", CLEARANCE_FACTOR))
            esc_clearance = float(self.settings.get(
                "escalation_clearance_factor", ESCALATION_CLEARANCE_FACTOR
            ))
            if alert["status"] == "ACKNOWLEDGED":
                return signal_delay * (1 - clearance)        # 70 % reduction → 30 % wait
            elif alert["status"] == "ESCALATED":
                return signal_delay * (1 - esc_clearance)    # 30 % reduction → 70 % wait

        return signal_delay  # Full wait if no alert or not acted upon

    # ── Alert & escalation check ───────────────────────────

    def _do_alert_check(self):
        if self.on_alert_check:
            self.on_alert_check(self.trip_id, self.elapsed_sim_time_s)

    # ── Auto-officer ───────────────────────────────────────

    def schedule_auto_officer(self, junction_id, created_sim_time):
        """Schedule an auto-officer ack for a junction (if enabled)."""
        enabled = str(self.settings.get("auto_officer_enabled",
                                         AUTO_OFFICER_ENABLED)).lower()
        if enabled in ("true", "1", "yes"):
            min_d = float(self.settings.get("auto_officer_min_delay_s",
                                             AUTO_OFFICER_MIN_DELAY_S))
            max_d = float(self.settings.get("auto_officer_max_delay_s",
                                             AUTO_OFFICER_MAX_DELAY_S))
            delay = self.rng.uniform(min_d, max_d)
            self.auto_officer_schedule[junction_id] = created_sim_time + delay

    def _check_auto_officer(self):
        """Fire any due auto-officer acks."""
        due = [
            jid for jid, t in list(self.auto_officer_schedule.items())
            if self.elapsed_sim_time_s >= t
        ]
        for jid in due:
            del self.auto_officer_schedule[jid]
            # Look up the alert
            conn = get_db()
            alert = conn.execute(
                "SELECT id, status FROM alerts "
                "WHERE trip_id = ? AND junction_id = ?",
                (self.trip_id, jid),
            ).fetchone()
            conn.close()
            if alert and alert["status"] == "CREATED" and self.on_auto_ack:
                self.on_auto_ack(
                    alert["id"],
                    "Auto-Officer (Simulated)",
                    self.elapsed_sim_time_s,
                )

    # ── SSE notification ───────────────────────────────────

    def _notify(self):
        if self.on_tick:
            self.on_tick(self._state_dict())

    # ── ETA for junctions (used by app.py) ─────────────────

    def get_junctions_with_eta(self):
        """
        Return a list of junction dicts augmented with current ETA and risk.
        """
        result = []
        signal_delay = float(self.settings.get("signal_delay_s", SIGNAL_DELAY_S))
        progress = self.current_distance_m / max(self.total_distance_m, 1)
        risk = risk_level_from_seed(self.seed, progress)

        remaining_signalised = sum(
            1 for j in self.junctions
            if not j.get("is_passed") and j.get("is_signalised", True)
        )

        for junc in self.junctions:
            remaining_dist = junc["distance_from_start_m"] - self.current_distance_m
            if junc.get("is_passed") or remaining_dist < 0:
                eta_s = 0.0
                junc_remaining_signals = 0
            else:
                # Count signalised junctions between current pos and this junction
                junc_remaining_signals = sum(
                    1 for j2 in self.junctions
                    if not j2.get("is_passed")
                    and j2.get("is_signalised", True)
                    and j2["distance_from_start_m"] < junc["distance_from_start_m"]
                )
                eta_s = compute_eta(
                    remaining_dist, junc_remaining_signals, risk, self.settings
                )

            result.append({
                "id": junc["id"],
                "name": junc.get("name", ""),
                "lat": junc.get("lat"),
                "lng": junc.get("lng"),
                "distance_from_start_m": junc["distance_from_start_m"],
                "remaining_distance_m": max(0, junc["distance_from_start_m"] - self.current_distance_m),
                "eta_s": round(eta_s, 1),
                "risk_level": risk,
                "is_passed": junc.get("is_passed", False),
                "is_blocked": junc.get("is_blocked", False),
                "is_signalised": junc.get("is_signalised", True),
                "order_index": junc.get("order_index", 0),
            })

        return result


# ── Module-level singleton ─────────────────────────────────
engine = SimulationEngine()
