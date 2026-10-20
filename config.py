"""
config.py — Default configuration constants for GreenCorridor AI.

All tuneable values live here.  The Settings API reads/writes overrides
to the SQLite `settings` table; values not overridden fall back to these defaults.
"""

# ── Simulation ──────────────────────────────────────────────
DEFAULT_SEED = 42                           # Deterministic RNG seed
TICK_INTERVAL_REAL_S = 1.0                  # Real-time seconds between ticks
DEFAULT_SPEED_MULTIPLIER = 1               # 1x, 2x, 5x, 10x

# ── ETA model (rule-based, NOT AI) ─────────────────────────
BASE_SPEED_KMH = 50                         # Ambulance base speed (km/h)
CONGESTION_LOW = 0.1                        # Congestion factor for Low risk
CONGESTION_MEDIUM = 0.3                     # Congestion factor for Medium risk
CONGESTION_HIGH = 0.5                       # Congestion factor for High risk
SIGNAL_DELAY_S = 20                         # Seconds of delay per signalised junction

# ── Alert rules ────────────────────────────────────────────
ALERT_START_S = 90                          # Create alert when ETA ≤ this (seconds)
ALERT_MIN_S = 60                            # Do not create alert when ETA < this
ESCALATION_TIMEOUT_S = 20                   # Simulated seconds before escalation

# ── Auto-officer (simulated) ──────────────────────────────
AUTO_OFFICER_ENABLED = True                 # Whether the simulated officer is active
AUTO_OFFICER_MIN_DELAY_S = 5               # Min simulated seconds before auto-ack
AUTO_OFFICER_MAX_DELAY_S = 15              # Max simulated seconds before auto-ack

# ── Roadblock ─────────────────────────────────────────────
ROADBLOCK_PENALTY_S = 120                   # Penalty seconds if reroute impossible

# ── Analytics ─────────────────────────────────────────────
CLEARANCE_FACTOR = 0.70                     # Wait reduction if alert acknowledged
ESCALATION_CLEARANCE_FACTOR = 0.30          # Wait reduction if alert escalated

# ── OSRM ──────────────────────────────────────────────────
OSRM_BASE_URL = "https://router.project-osrm.org"
OSRM_TIMEOUT_S = 5                          # Seconds before falling back

# ── Server ────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 5000
DEBUG = False
