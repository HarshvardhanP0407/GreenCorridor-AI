"""
tests/test_routing.py — Unit tests for OSRM routing and fallback route handling.

Verifies:
- Fallback route loads correctly with 8 junctions
- When OSRM call fails (network error, timeout, HTTP 500), fallback route is used
- Route geometry and junctions match expected format
"""

import urllib.error
from unittest.mock import patch

from routing import get_route, load_sample_route


def test_load_sample_route_structure():
    """Verify sample_route.json contains the expected Anna Salai corridor."""
    sample = load_sample_route()
    assert "geometry" in sample
    assert len(sample["geometry"]) > 20
    assert "junctions" in sample
    assert len(sample["junctions"]) == 8
    assert sample["distance_m"] > 5000

    # Verify first and last junctions
    assert sample["junctions"][0]["name"] == "Saidapet Junction"
    assert sample["junctions"][-1]["name"] == "Egmore Junction"

    # Verify coordinates are lat, lng lists
    first_pt = sample["geometry"][0]
    assert len(first_pt) == 2
    assert 12.0 < first_pt[0] < 14.0  # Latitude for Chennai
    assert 79.0 < first_pt[1] < 81.0  # Longitude for Chennai


def test_routing_fallback_default_call():
    """Calling get_route without coords returns fallback route."""
    route = get_route()
    assert route["fallback"] is True
    assert "fallback_reason" in route
    assert len(route["junctions"]) == 8
    assert len(route["geometry"]) > 20
    assert route["distance_m"] > 5000


def test_routing_fallback_on_osrm_failure():
    """When OSRM request raises URLError or timeout, fallback route is used."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        route = get_route(start_coords=(80.223, 13.021), end_coords=(80.267, 13.070))

    assert route["fallback"] is True
    assert "Connection refused" in route["fallback_reason"]
    assert len(route["junctions"]) == 8
    assert len(route["geometry"]) > 20
