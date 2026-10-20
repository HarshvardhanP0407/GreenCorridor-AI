"""
tests/test_alerts.py — Unit tests for the alert lifecycle management.

Verifies:
- Triggering inside the [60s, 90s] window
- Not triggering outside the window (<60s or >90s)
- Duplicate prevention (both constraint and logic)
- Acknowledging changes state to ACKNOWLEDGED and stores timestamp
- Double-acknowledgement is rejected
- Escalation fires after timeout (in simulated time)
- Escalation does not fire before timeout
- Escalation does not fire if alert is already ACKNOWLEDGED
"""

import pytest
import database
from database import get_db, init_db, set_db_path
from alerts import (
    check_and_create_alerts,
    acknowledge_alert,
    check_escalations,
    get_alerts_for_trip,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Point database to a temporary SQLite file for each test."""
    db_file = str(tmp_path / "test_alerts.db")
    set_db_path(db_file)
    init_db()

    # Create a test trip and junction
    conn = get_db()
    conn.execute(
        "INSERT INTO trips (id, status, seed, mode) VALUES (1, 'RUNNING', 42, 'coordinated')"
    )
    conn.execute(
        "INSERT INTO junctions (id, trip_id, name, lat, lng, distance_from_start_m, order_index) "
        "VALUES (101, 1, 'Saidapet Test Junction', 13.021, 80.223, 1000, 0)"
    )
    conn.execute(
        "INSERT INTO junctions (id, trip_id, name, lat, lng, distance_from_start_m, order_index) "
        "VALUES (102, 1, 'Nandanam Test Junction', 13.032, 80.234, 2000, 1)"
    )
    conn.commit()
    conn.close()
    yield


def test_alert_triggering_window():
    """
    Alerts must only trigger when ETA is within [60, 90] seconds.
    """
    settings = {"alert_start_s": 90, "alert_min_s": 60}

    # Case 1: ETA = 100s (> 90s) -> should NOT trigger
    juncs_too_far = [{"id": 101, "name": "Saidapet", "eta_s": 100.0, "is_passed": False}]
    created = check_and_create_alerts(trip_id=1, junctions_with_eta=juncs_too_far, sim_time_s=10.0, settings=settings)
    assert len(created) == 0

    # Case 2: ETA = 50s (< 60s) -> should NOT trigger
    juncs_too_close = [{"id": 101, "name": "Saidapet", "eta_s": 50.0, "is_passed": False}]
    created = check_and_create_alerts(trip_id=1, junctions_with_eta=juncs_too_close, sim_time_s=15.0, settings=settings)
    assert len(created) == 0

    # Case 3: ETA = 75s (within [60, 90]) -> MUST trigger
    juncs_in_window = [{"id": 101, "name": "Saidapet", "eta_s": 75.0, "is_passed": False}]
    created = check_and_create_alerts(trip_id=1, junctions_with_eta=juncs_in_window, sim_time_s=20.0, settings=settings)
    assert len(created) == 1
    alert_id = created[0]

    # Verify state in DB
    alerts = get_alerts_for_trip(1)
    assert len(alerts) == 1
    assert alerts[0]["id"] == alert_id
    assert alerts[0]["status"] == "CREATED"
    assert alerts[0]["created_sim_time_s"] == 20.0


def test_alert_duplicate_prevention():
    """
    Duplicate alerts for the same (trip_id, junction_id) must be prevented.
    """
    settings = {"alert_start_s": 90, "alert_min_s": 60}
    juncs = [{"id": 101, "name": "Saidapet", "eta_s": 80.0, "is_passed": False}]

    # First call creates alert
    created1 = check_and_create_alerts(trip_id=1, junctions_with_eta=juncs, sim_time_s=10.0, settings=settings)
    assert len(created1) == 1

    # Second call with the same junction in window must not create duplicate
    created2 = check_and_create_alerts(trip_id=1, junctions_with_eta=juncs, sim_time_s=12.0, settings=settings)
    assert len(created2) == 0

    # Check database only has 1 alert
    alerts = get_alerts_for_trip(1)
    assert len(alerts) == 1


def test_alert_acknowledgement_and_double_ack_rejected():
    """
    Acknowledging sets status to ACKNOWLEDGED, officer name, and timestamp.
    A second acknowledge must be rejected.
    """
    settings = {"alert_start_s": 90, "alert_min_s": 60}
    juncs = [{"id": 101, "name": "Saidapet", "eta_s": 80.0, "is_passed": False}]
    created = check_and_create_alerts(trip_id=1, junctions_with_eta=juncs, sim_time_s=10.0, settings=settings)
    alert_id = created[0]

    # Empty officer name must fail
    ok, msg = acknowledge_alert(alert_id, officer_name="", sim_time_s=15.0)
    assert not ok
    assert "Officer name is required" in msg

    # Valid acknowledge
    ok, msg = acknowledge_alert(alert_id, officer_name="Officer Kumar", sim_time_s=15.0)
    assert ok
    assert msg == "Acknowledged."

    # Check alert in DB
    conn = get_db()
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    conn.close()
    assert row["status"] == "ACKNOWLEDGED"
    assert row["officer_name"] == "Officer Kumar"
    assert row["ack_sim_time_s"] == 15.0
    assert row["ack_at"] is not None

    # Double-acknowledge must be rejected
    ok2, msg2 = acknowledge_alert(alert_id, officer_name="Officer Sharma", sim_time_s=18.0)
    assert not ok2
    assert "Double-acknowledge rejected" in msg2


def test_alert_escalation_timing():
    """
    Escalation must:
    - Not fire before timeout
    - Fire after timeout (simulated time)
    - Not fire after alert is already acknowledged
    """
    settings = {"alert_start_s": 90, "alert_min_s": 60, "escalation_timeout_s": 20.0}

    # Create alert at sim_time_s = 10.0
    juncs = [{"id": 101, "name": "Saidapet", "eta_s": 80.0, "is_passed": False}]
    created = check_and_create_alerts(trip_id=1, junctions_with_eta=juncs, sim_time_s=10.0, settings=settings)
    alert_id = created[0]

    # Before timeout: at sim_time_s = 25.0 (elapsed = 15s < 20s) -> no escalation
    esc = check_escalations(trip_id=1, sim_time_s=25.0, settings=settings)
    assert len(esc) == 0

    # At / after timeout: at sim_time_s = 31.0 (elapsed = 21s >= 20s) -> escalates
    esc = check_escalations(trip_id=1, sim_time_s=31.0, settings=settings)
    assert len(esc) == 1
    assert esc[0] == alert_id

    conn = get_db()
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    conn.close()
    assert row["status"] == "ESCALATED"
    assert row["escalated_sim_time_s"] == 31.0
    assert row["escalated_at"] is not None


def test_alert_escalation_does_not_fire_after_acknowledgement():
    """
    If alert is acknowledged before timeout, it should never escalate.
    """
    settings = {"alert_start_s": 90, "alert_min_s": 60, "escalation_timeout_s": 20.0}

    # Create alert at sim_time_s = 10.0
    juncs = [{"id": 102, "name": "Nandanam", "eta_s": 70.0, "is_passed": False}]
    created = check_and_create_alerts(trip_id=1, junctions_with_eta=juncs, sim_time_s=10.0, settings=settings)
    alert_id = created[0]

    # Acknowledge at sim_time_s = 18.0 (before 20s timeout)
    ok, _ = acknowledge_alert(alert_id, officer_name="Officer Priya", sim_time_s=18.0)
    assert ok

    # Check escalations at sim_time_s = 40.0 (30s after creation)
    esc = check_escalations(trip_id=1, sim_time_s=40.0, settings=settings)
    assert len(esc) == 0

    # Status remains ACKNOWLEDGED
    conn = get_db()
    row = conn.execute("SELECT status FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    conn.close()
    assert row["status"] == "ACKNOWLEDGED"
