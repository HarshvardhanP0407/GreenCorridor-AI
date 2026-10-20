"""
tests/test_analytics.py — Unit tests for the Analytics comparison engine.

Verifies:
- Baseline is computed as a separate simulated run with full signal delays
- Coordinated trip uses actual recorded simulation durations
- Time saved equals baseline_duration minus coordinated_duration
- All metrics are explicitly labelled 'Simulated'
- Assumptions list is populated and transparent
"""

import json
import pytest
import database
from database import get_db, init_db, set_db_path
from routing import load_sample_route
from analytics import compute_baseline_trip, get_analytics


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Setup an isolated test database with a completed coordinated trip."""
    db_file = str(tmp_path / "test_analytics.db")
    set_db_path(db_file)
    init_db()

    sample = load_sample_route()

    conn = get_db()
    # Insert test coordinated trip
    cur = conn.execute(
        "INSERT INTO trips (status, seed, speed_multiplier, mode, route_json, "
        "total_distance_m, elapsed_sim_time_s) "
        "VALUES ('FINISHED', 42, 1.0, 'coordinated', ?, ?, ?)",
        (json.dumps(sample), sample["distance_m"], 650.0),
    )
    trip_id = cur.lastrowid

    # Insert 8 junctions for this trip
    for idx, junc in enumerate(sample["junctions"]):
        conn.execute(
            "INSERT INTO junctions (trip_id, name, lat, lng, distance_from_start_m, "
            "order_index, is_signalised, is_passed, wait_time_s) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)",
            (trip_id, junc["name"], junc["lat"], junc["lng"],
             junc["distance_from_start_m"], idx, 6.0),  # 6s reduced wait
        )

    # Insert 8 alerts (acknowledged)
    for idx, junc in enumerate(sample["junctions"]):
        conn.execute(
            "INSERT INTO alerts (trip_id, junction_id, status, officer_name, "
            "created_sim_time_s, ack_sim_time_s) "
            "VALUES (?, ?, 'ACKNOWLEDGED', 'Officer Test', ?, ?)",
            (trip_id, idx + 1, 10.0 + idx * 50, 18.0 + idx * 50),
        )

    conn.commit()
    conn.close()
    yield trip_id


def test_baseline_computation(setup_test_db):
    """
    Verify baseline trip calculation produces a separate recorded trip
    with full signal delays.
    """
    trip_id = setup_test_db
    baseline = compute_baseline_trip(trip_id)

    assert baseline is not None
    assert "baseline_trip_id" in baseline
    assert baseline["num_signalised_junctions"] == 8
    # 8 signals * 20s default = 160s total wait in baseline
    assert baseline["baseline_total_wait_s"] == 160.0
    # Baseline duration must be greater than just travel time
    assert baseline["baseline_duration_s"] > baseline["baseline_total_wait_s"]

    # Verify a separate baseline trip record was saved in DB
    conn = get_db()
    baseline_trip = conn.execute(
        "SELECT * FROM trips WHERE id = ?", (baseline["baseline_trip_id"],)
    ).fetchone()
    conn.close()
    assert baseline_trip["mode"] == "baseline"
    assert baseline_trip["status"] == "FINISHED"


def test_analytics_time_saved_and_simulated_labels(setup_test_db):
    """
    Verify side-by-side analytics:
    - time_saved == baseline_duration - coordinated_duration
    - labels say 'Simulated'
    - assumptions list is present
    """
    trip_id = setup_test_db
    analytics = get_analytics(trip_id)

    assert analytics is not None

    # Check labels explicitly say simulated
    assert "Simulated result" in analytics["label"]
    assert "Simulated" in analytics["baseline"]["label"]
    assert "Simulated" in analytics["coordinated"]["label"]
    assert "Simulated" in analytics["time_saved_label"]

    base_dur = analytics["baseline"]["duration_s"]
    coord_dur = analytics["coordinated"]["duration_s"]
    saved = analytics["time_saved_s"]

    # In our fixture: coordinated = 650.0s, baseline has full delays (approx 750s+)
    assert pytest.approx(saved, 0.01) == round(base_dur - coord_dur, 2)
    assert saved > 0

    # Check alert metrics
    assert analytics["coordinated"]["alerts_sent"] == 8
    assert analytics["coordinated"]["alerts_acknowledged"] == 8
    assert analytics["coordinated"]["alerts_escalated"] == 0
    # Average response time = 18 - 10 = 8.0s
    assert pytest.approx(analytics["coordinated"]["avg_response_time_s"], 0.1) == 8.0

    # Check assumptions are documented
    assert len(analytics["assumptions"]) >= 5
    assert any("No real-world" in a or "simulated" in a.lower() for a in analytics["assumptions"])


def test_escalated_alert_never_counted_as_acknowledged(setup_test_db):
    """
    Assert that any alert marked ESCALATED (or with escalated_at set)
    is never counted as acknowledged in analytics.
    """
    trip_id = setup_test_db
    conn = get_db()
    # Mark half the alerts as ESCALATED
    conn.execute(
        "UPDATE alerts SET status = 'ESCALATED', escalated_at = '2026-10-20T10:00:00Z', "
        "escalated_sim_time_s = 30.0 WHERE junction_id > 4"
    )
    conn.commit()
    conn.close()

    analytics = get_analytics(trip_id)
    assert analytics["coordinated"]["alerts_sent"] == 8
    assert analytics["coordinated"]["alerts_acknowledged"] == 4
    assert analytics["coordinated"]["alerts_escalated"] == 4
