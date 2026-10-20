"""
routing.py — OSRM route client with fallback to bundled sample route.

Calls the public OSRM demo server for driving directions.
If the call fails (timeout, network error, bad response), loads
data/sample_route.json as a fallback and shows a "Fallback route in use" badge.
"""

import json
import os
import urllib.request
import urllib.error

from config import OSRM_BASE_URL, OSRM_TIMEOUT_S

# Path to the bundled fallback route
SAMPLE_ROUTE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "sample_route.json"
)


# ── Public API ─────────────────────────────────────────────

def get_route(start_coords=None, end_coords=None, exclude_point=None):
    """
    Get a driving route, trying OSRM first, then falling back.

    Args:
        start_coords: (lng, lat) tuple — OSRM uses lng,lat order.
        end_coords:   (lng, lat) tuple.
        exclude_point: optional (lng, lat) to try to avoid during rerouting.

    Returns:
        dict with keys:
            geometry       – list of [lat, lng] pairs (Leaflet order)
            distance_m     – total route distance in metres
            duration_s     – estimated duration in seconds
            junctions      – list of junction dicts
            fallback       – bool, True if fallback was used
            fallback_reason – str or None
    """
    if start_coords and end_coords:
        result = _fetch_osrm_route(start_coords, end_coords)
        if not result.get("fallback"):
            # Build junctions from OSRM step data
            result["junctions"] = _extract_junctions_from_steps(
                result.pop("steps", [])
            )
            result["fallback_reason"] = None
            return result
        # OSRM failed — fall through to fallback
        return result

    return _load_fallback_route("No coordinates provided; using bundled route")


def load_sample_route():
    """Load and return the raw JSON from data/sample_route.json."""
    with open(SAMPLE_ROUTE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ── OSRM client ───────────────────────────────────────────

def _fetch_osrm_route(start_coords, end_coords):
    """
    Call the OSRM public demo server.

    Returns a standardised dict on success, or falls back on any error.
    """
    coords_str = (
        f"{start_coords[0]},{start_coords[1]};"
        f"{end_coords[0]},{end_coords[1]}"
    )
    url = (
        f"{OSRM_BASE_URL}/route/v1/driving/{coords_str}"
        f"?overview=full&geometries=geojson&steps=true"
    )

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "GreenCorridorAI/1.0"}
        )
        with urllib.request.urlopen(req, timeout=OSRM_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("code") != "Ok" or not data.get("routes"):
            raise ValueError(f"OSRM code: {data.get('code')}")

        route = data["routes"][0]
        # OSRM returns [[lng, lat], …] — convert to [[lat, lng], …]
        geometry = [[pt[1], pt[0]] for pt in route["geometry"]["coordinates"]]

        # Collect turn-by-turn steps
        steps = []
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                loc = step.get("maneuver", {}).get("location")
                if loc:
                    steps.append({
                        "name": step.get("name", "Unnamed"),
                        "lat": loc[1],
                        "lng": loc[0],
                        "distance_m": step.get("distance", 0),
                        "duration_s": step.get("duration", 0),
                        "maneuver_type": step.get("maneuver", {}).get("type", ""),
                    })

        return {
            "geometry": geometry,
            "distance_m": route.get("distance", 0),
            "duration_s": route.get("duration", 0),
            "steps": steps,
            "fallback": False,
        }

    except (urllib.error.URLError, urllib.error.HTTPError,
            ValueError, OSError, json.JSONDecodeError, KeyError) as exc:
        return _load_fallback_route(str(exc))


# ── Fallback loader ────────────────────────────────────────

def _load_fallback_route(reason=""):
    """Return the bundled sample route in a standardised dict."""
    sample = load_sample_route()
    return {
        "geometry": sample["geometry"],
        "distance_m": sample["distance_m"],
        "duration_s": sample.get("duration_s", 0),
        "junctions": sample["junctions"],
        "steps": [],
        "fallback": True,
        "fallback_reason": reason or "OSRM unavailable",
    }


# ── Helpers ────────────────────────────────────────────────

def _extract_junctions_from_steps(steps):
    """
    Pick junction-like waypoints from OSRM step data.
    Keeps at most 10 junctions, evenly spaced if there are more.
    """
    junction_types = {
        "turn", "end of road", "fork", "new name",
        "roundabout", "rotary", "traffic_signals",
    }
    junctions = []
    seen_names = set()
    cumulative_distance = 0.0

    for step in steps:
        cumulative_distance += step.get("distance_m", 0)
        mtype = step.get("maneuver_type", "")
        name = step.get("name", "Unnamed")

        if mtype in ("depart", "arrive"):
            continue
        if name in seen_names or not name:
            continue

        junctions.append({
            "name": name,
            "lat": step["lat"],
            "lng": step["lng"],
            "distance_from_start_m": cumulative_distance,
            "is_signalised": True,
            "approximate": False,
        })
        seen_names.add(name)

    # Cap at 10 junctions, evenly spaced
    if len(junctions) > 10:
        step_size = len(junctions) / 10
        junctions = [junctions[int(i * step_size)] for i in range(10)]

    return junctions
