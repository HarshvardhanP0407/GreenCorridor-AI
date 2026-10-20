"""
tests/test_eta.py — Unit tests for the rule-based ETA calculation model.

Verifies:
- Normal case calculation
- Zero distance edge case
- High congestion vs Low congestion
- Validation of invalid inputs (negative values, zero speed, out-of-range congestion)
"""

import pytest
from simulation import compute_eta, get_congestion_factor


def test_eta_normal_case():
    """
    Normal case:
    Distance = 1000 m
    Signals = 2
    Base speed = 50 km/h
    Risk = low (congestion = 0.1)
    Signal delay = 20 s

    effective_speed = (50 / 3.6) * (1 - 0.1) = 12.5 m/s
    travel_time = 1000 / 12.5 = 80 s
    signal_delay = 2 * 20 = 40 s
    Total ETA = 120.0 s
    """
    settings = {
        "base_speed_kmh": 50,
        "signal_delay_s": 20,
        "congestion_low": 0.1,
    }
    eta = compute_eta(
        remaining_distance_m=1000,
        num_remaining_signalised_junctions=2,
        risk_level="low",
        settings=settings,
    )
    assert pytest.approx(eta, 0.01) == 120.0


def test_eta_zero_distance():
    """
    Zero distance edge cases:
    - 0 m with 0 signals -> 0.0 s
    - 0 m with 3 signals -> 3 * 20 = 60.0 s
    """
    settings = {"base_speed_kmh": 50, "signal_delay_s": 20}
    assert compute_eta(0, 0, risk_level="low", settings=settings) == 0.0
    assert compute_eta(0, 3, risk_level="low", settings=settings) == 60.0


def test_eta_high_congestion():
    """
    High congestion increases ETA compared to low congestion for the same distance.
    """
    settings = {
        "base_speed_kmh": 50,
        "signal_delay_s": 20,
        "congestion_low": 0.1,
        "congestion_high": 0.5,
    }
    eta_low = compute_eta(2000, 1, risk_level="low", settings=settings)
    eta_high = compute_eta(2000, 1, risk_level="high", settings=settings)

    # Low: 2000 / 12.5 + 20 = 160 + 20 = 180s
    # High: (50/3.6)*0.5 = 6.944 m/s -> 2000 / 6.944 = 288s + 20 = 308s
    assert eta_high > eta_low
    assert pytest.approx(eta_low, 0.1) == 180.0
    assert pytest.approx(eta_high, 0.1) == 308.0


def test_eta_invalid_inputs():
    """Invalid inputs must raise ValueError."""
    # Negative remaining distance
    with pytest.raises(ValueError, match="remaining_distance_m"):
        compute_eta(-100, 1)

    # Negative junction count
    with pytest.raises(ValueError, match="num_remaining_signalised_junctions"):
        compute_eta(500, -2)

    # Non-positive base speed
    with pytest.raises(ValueError, match="base_speed_kmh"):
        compute_eta(500, 1, settings={"base_speed_kmh": 0})

    with pytest.raises(ValueError, match="base_speed_kmh"):
        compute_eta(500, 1, settings={"base_speed_kmh": -30})

    # Negative signal delay
    with pytest.raises(ValueError, match="signal_delay_s"):
        compute_eta(500, 1, settings={"signal_delay_s": -10})

    # Congestion factor >= 1.0 or < 0
    with pytest.raises(ValueError, match="congestion factor"):
        compute_eta(500, 1, risk_level="high", settings={"congestion_high": 1.0})

    with pytest.raises(ValueError, match="congestion factor"):
        compute_eta(500, 1, risk_level="low", settings={"congestion_low": -0.2})
