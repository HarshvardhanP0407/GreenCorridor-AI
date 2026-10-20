# 🚑 GreenCorridor AI — Predictive Ambulance Corridor Coordination

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-green.svg)](https://flask.palletsprojects.com/)
[![SQLite](https://img.shields.io/badge/Database-SQLite3-lightgrey.svg)](https://www.sqlite.org/)
[![Leaflet](https://img.shields.io/badge/Map-Leaflet%201.9-brightgreen.svg)](https://leafletjs.com/)
[![Tests](https://img.shields.io/badge/Tests-15%20Passed-success.svg)]()
[![License](https://img.shields.io/badge/License-MIT-purple.svg)](LICENSE)

> ⚠️ **SIMULATION NOTICE**  
> All GPS telemetry, traffic congestion, and police responses are **simulated**. No real emergency dispatch systems, actual emergency vehicles, or physical municipal traffic signal controllers are connected or altered.

---

## 📖 Table of Contents
1. [Overview](#-overview)
2. [Key Features](#-key-features)
3. [System Architecture](#-system-architecture)
4. [Technologies Used](#-technologies-used)
5. [Transparent Rule-Based ETA Model](#-transparent-rule-based-eta-model)
6. [Alert Lifecycle & Escalation State Machine](#-alert-lifecycle--escalation-state-machine)
7. [Analytics Integrity: Baseline vs. Coordinated](#-analytics-integrity-baseline-vs-coordinated)
8. [Local Installation & Setup](#-local-installation--setup)
9. [Running the Application](#-running-the-application)
10. [Automated Testing](#-automated-testing)
11. [Assumptions and Limitations](#-assumptions-and-limitations)
12. [Project Structure](#-project-structure)
13. [Screenshots & UI Walkthrough](#-screenshots--ui-walkthrough)
14. [License](#-license)

---

## 🌟 Overview

**GreenCorridor AI** is an open-source, local-first web application designed to demonstrate predictive emergency corridor clearance along a real-world arterial road stretch (**Anna Salai / Mount Road, Chennai, India**).

In traditional emergency transit, ambulances either struggle through red signals or rely on manual siren-based right-of-way, causing severe delays at intersections. GreenCorridor AI demonstrates how:
- Server-side simulation moves an ambulance along real road geometry.
- A **rule-based mathematical ETA formula** transparently computes vehicle arrival at every signalised intersection.
- Timely clearance alerts are dispatched to traffic officers within a **60–90 second arrival window**.
- Timely acknowledgements reduce simulated waiting times by 70%, while unacknowledged alerts automatically escalate to a supervisor.
- A **separate baseline run** (with full signal waits) is recorded side-by-side with identical seeds to measure simulated time saved without fabricated metrics.

---

## 🚀 Key Features

- **Real-Time Interactive Map (Leaflet):** Displays route polylines, moving ambulance markers, and color-coded junction status pins (Upcoming, Alerted, Acknowledged, Escalated, Cleared).
- **Offline Resilient Routing:** Calls the public OSRM server for turn-by-turn routing, with instant graceful fallback to a bundled 7.1 km Anna Salai corridor if offline.
- **Transparent Mathematical ETA Model:** Completely rule-based (no black-box AI claims). Live formula inputs and parameters are displayed directly in the UI.
- **Deterministic Simulation:** Uses a seeded random generator (default seed `42`) so that baseline and coordinated runs have identical route conditions.
- **5-State Police Alert Lifecycle:** Enforces `CREATED` $\rightarrow$ `ACKNOWLEDGED` or `ESCALATED` $\rightarrow$ `CLEARED` (or `EXPIRED`) with SQLite unique constraints preventing duplicate alerts.
- **Simulated Escalation Timeout:** Alerts not acted upon within 20 simulated seconds automatically escalate to a supervisor. Works accurately across 1x, 2x, 5x, and 10x simulation speeds.
- **Simulated Auto-Officer Mode:** Includes an automatic officer responder that acknowledges alerts after a seeded random delay, alongside manual officer input.
- **Dynamic Roadblock Simulation:** Allows blocking any junction, applying a configurable penalty delay (120 s) with clear UI notices when rerouting is unavailable.
- **Side-by-Side Analytics Integrity:** Records Baseline and Coordinated trips as two distinct database records; calculates simulated time saved strictly from recorded durations.
- **Audit Logging & CSV Export:** Every state transition is recorded with UTC timestamps. One-click CSV export ready for spreadsheet review.

---

## 🏗️ System Architecture

```mermaid
flowchart TD
    subgraph Client["Frontend (Browser — Vanilla JS + CSS)"]
        UI["Dashboard & 6 Views"]
        LeafletMap["Leaflet OSM Map"]
        SSEListener["SSE Stream Listener"]
        RESTClient["REST Client (Fetch)"]
    end

    subgraph Server["Flask Backend (Python 3.10+)"]
        App["app.py (REST API & SSE Dispatcher)"]
        Engine["simulation.py (Ambulance Tick & ETA Math)"]
        Alerts["alerts.py (Alert Lifecycle & Escalation)"]
        Analytics["analytics.py (Baseline vs Coordinated)"]
        Routing["routing.py (OSRM Client + Fallback)"]
    end

    subgraph Storage["Local Storage & Data"]
        DB[("SQLite — greencorridor.db")]
        SampleJSON["data/sample_route.json (Anna Salai, Chennai)"]
    end

    subgraph External["External Network (Optional)"]
        OSRM["OSRM Public Routing API"]
        OSMTiles["OpenStreetMap CDN Tiles"]
    end

    UI --> RESTClient
    UI --> LeafletMap
    SSEListener <-- "data: {tick, alerts, junctions}" --- App
    RESTClient <--> "HTTP REST Endpoints" <--> App

    App --> Engine
    App --> Alerts
    App --> Analytics
    App --> Routing

    Routing -.->|"HTTP (5s timeout)"| OSRM
    Routing -->|"Fallback on offline"| SampleJSON
    LeafletMap -.->|"CDN (tile warning on failure)"| OSMTiles

    Engine --> DB
    Alerts --> DB
    Analytics --> DB
```

---

## 🛠️ Technologies Used

| Layer | Technology | Details |
|---|---|---|
| **Backend** | Python 3.10+, Flask 3.x | Lightweight REST API and Server-Sent Events (`threaded=True`). |
| **Database** | SQLite 3 (Standard Library) | Auto-created `greencorridor.db` with parameterized queries and WAL mode. |
| **Frontend** | Vanilla HTML5, CSS3, JavaScript | Zero build step, zero Node.js dependencies, clean responsive dark navy theme. |
| **Mapping** | Leaflet 1.9 (OpenStreetMap CDN) | Polyline rendering, custom pulsing markers, offline tile warning badge. |
| **Routing** | OSRM Public API + Fallback | Public driving directions with bundled Anna Salai Chennai GeoJSON fallback. |
| **Testing** | pytest 8.x+ | 15 comprehensive unit tests covering math, state transitions, and fallbacks. |

---

## 📐 Transparent Rule-Based ETA Model

Evaluators can inspect and verify every parameter in the **Settings & Model** view:

$$\text{ETA}_{\text{seconds}} = \frac{\text{remaining\_distance\_m}}{\text{effective\_speed\_mps}} + \text{expected\_signal\_delay\_s}$$

$$\text{effective\_speed\_mps} = \frac{\text{base\_speed\_kmh}}{3.6} \times (1 - \text{congestion\_factor})$$

$$\text{expected\_signal\_delay\_s} = \text{remaining\_signalised\_junctions} \times \text{signal\_delay\_per\_junction\_s}$$

### Default Parameters
| Parameter | Default Value | Validated Range | Description |
|---|---|---|---|
| `base_speed_kmh` | `50 km/h` | 10 to 120 | Uncongested vehicle speed |
| `signal_delay_s` | `20 s` | 0 to 120 | Average red signal wait per junction |
| `congestion_factor` | Low: `0.1`, Med: `0.3`, High: `0.5` | 0.0 to 0.6 | Segment risk factor (seeded RNG) |
| `alert_start_s` | `90 s` | 10 to 300 | Trigger window upper bound |
| `alert_min_s` | `60 s` | 5 to 200 | Trigger window lower bound |
| `escalation_timeout_s` | `20 s` | 5 to 120 | Simulated seconds before supervisor escalation |
| `clearance_factor` | `0.70` (70%) | 0.0 to 1.0 | Delay reduction on acknowledged alert |
| `escalation_clearance_factor` | `0.30` (30%) | 0.0 to 1.0 | Delay reduction on escalated alert |
| `seed` | `42` | 0 to 999999 | Deterministic random seed |

---

## 🚨 Alert Lifecycle & Escalation State Machine

```
              Ambulance ETA enters [60s, 90s]
                            │
                            ▼
                    ┌───────────────┐
                    │    CREATED    │
                    └───────┬───────┘
                            │
           ┌────────────────┴────────────────┐
           │ Timeout (20s sim-time)           │ Officer / Auto-Officer
           ▼                                  ▼
    ┌──────────────┐                  ┌──────────────┐
    │  ESCALATED   │                  │ ACKNOWLEDGED │
    └──────┬───────┘                  └──────┬───────┘
           │                                 │
           └────────────────┬────────────────┘
                            │ Ambulance passes junction
                            ▼
                    ┌───────────────┐
                    │    CLEARED    │
                    └───────────────┘
```

- **Duplicate Prevention:** A SQLite `UNIQUE(trip_id, junction_id)` constraint combined with a code pre-check ensures only one alert exists per junction per trip.
- **Double-Acknowledge Rejection:** Once an alert is acknowledged, subsequent acknowledge attempts return HTTP 400.
- **Simulated Escalation Timing:** Measures time using **simulated seconds** (`elapsed_sim_time_s`), ensuring that escalation operates identically regardless of whether the simulation is running at 1x, 2x, 5x, or 10x speed.

---

## 📊 Analytics Integrity: Baseline vs. Coordinated

To prevent misleading claims, **Baseline** and **Coordinated** trips are stored as **two distinct rows in SQLite**:

1. **Baseline Run (`mode='baseline'`):**  
   Simulated across the same route geometry and seed. At every signalised intersection, the vehicle waits the **full signal delay (100%)** ($8 \times 20\text{ s} = 160\text{ s}$ total signal wait).
2. **Coordinated Run (`mode='coordinated'`):**  
   Wait times at junctions are reduced by the configured clearance factor:
   - Acknowledged alert: wait reduced by 70% ($20\text{ s} \times 0.3 = 6\text{ s}$).
   - Escalated alert: wait reduced by 30% ($20\text{ s} \times 0.7 = 14\text{ s}$).
3. **Simulated Time Saved:**
   $$\text{Time Saved} = \text{baseline\_duration\_s} - \text{coordinated\_duration\_s}$$
   Calculated strictly from recorded SQLite rows. Every number is explicitly labeled **"Simulated result"**.

---

## 💻 Local Installation & Setup

### Prerequisites
- Python 3.10 or higher (Python 3.10, 3.11, 3.12, 3.13, 3.14 supported).
- Git.

### 1. Clone the Repository
```bash
git clone https://github.com/YOUR_USERNAME/greencorridor-ai.git
cd greencorridor-ai
```

### 2. Setup Virtual Environment

#### On Windows (PowerShell):
```powershell
# Set execution policy for the current session (if blocked)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# Create virtual environment
py -3 -m venv .venv

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

#### On macOS / Linux:
```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## ⚡ Running the Application

Start the Flask server:
```powershell
python app.py
```

Open your browser at:
👉 **`http://127.0.0.1:5000`**

### Running Controls
- Click **▶ Start Simulation** to launch an ambulance trip.
- Adjust simulation speed: **1x, 2x, 5x, 10x**.
- Use **⏸ Pause**, **⏵ Resume**, or **⏹ Reset**.
- Switch views via the left sidebar: **Dashboard**, **Junctions**, **Police Alerts**, **Analytics**, **Event History**, and **Settings & Model**.

---

## 🧪 Automated Testing

Run the full pytest suite:
```powershell
pytest -v
```

All **15 tests** pass out-of-the-box:
- `tests/test_eta.py`: Normal case, zero distance, congestion comparisons, invalid inputs (raises `ValueError`).
- `tests/test_alerts.py`: Window trigger [60s, 90s], duplicate prevention, manual acknowledge validation, double-ack rejection, simulated escalation timing.
- `tests/test_routing.py`: Fallback Anna Salai route structure and graceful recovery on OSRM network failure.
- `tests/test_analytics.py`: Baseline trip generation, simulated time-saved formula, simulated labels, and assertion that an escalated alert is never counted as acknowledged.

---

## ⚠️ Assumptions and Limitations

1. **Simulated GPS:** Positions are mathematically interpolated along route vertices based on simulated velocity.
2. **Deterministic Randomness:** All congestion factors and auto-officer response delays are derived from Python's seeded random generator for strict scientific reproducibility.
3. **No Hardware Alteration:** The system does not connect to real city traffic signal relays or live emergency vehicle transponders.
4. **Offline Capability:** If project-osrm.org is down or offline, the app seamlessly runs using the bundled Anna Salai corridor with 8 real junctions.

---

## 📁 Project Structure

```
greencorridor-ai/
├── app.py                  # Flask web server, REST routes & SSE stream
├── config.py               # Central configuration constants
├── database.py             # SQLite schema, parameterized queries, audit helpers
├── routing.py              # OSRM routing client with bundled fallback loader
├── simulation.py           # Movement simulation, geometry math & rule-based ETA
├── alerts.py               # Alert state machine, acknowledge & escalation
├── analytics.py            # Baseline vs Coordinated analytics comparison
├── requirements.txt        # Minimal dependencies (flask, pytest)
├── README.md               # Project documentation
├── .gitignore              # Ignores .venv, __pycache__, *.db, secrets
├── data/
│   └── sample_route.json   # 7.1 km Anna Salai (Chennai) route & 8 named junctions
├── templates/
│   └── index.html          # Responsive single-page application
├── static/
│   ├── style.css           # Dark navy theme stylesheet
│   └── app.js              # Vanilla JS application and Leaflet map logic
└── tests/
    ├── __init__.py
    ├── test_eta.py         # ETA model unit tests
    ├── test_alerts.py      # Alert trigger & escalation unit tests
    ├── test_routing.py     # Routing & fallback unit tests
    └── test_analytics.py   # Baseline comparison & escalation exclusion tests
```

---

## 📸 Screenshots & UI Walkthrough

| View | Description |
|---|---|
| **Dashboard** | Dark navy telemetry view showing live ambulance position, route line, KPI cards, and active alert queue. |
| **Junctions** | Table of all 8 signalised intersections with real-time rule-based ETAs, segment risk levels, and Roadblock controls. |
| **Police Alerts** | Dispatch queue displaying active cards with simulated escalation countdowns and one-click Acknowledge buttons. |
| **Analytics** | Side-by-side comparison of Baseline (full signal wait) vs. Coordinated runs, simulated time saved, and model assumptions. |
| **Event History** | UTC timestamped audit trail of all alerts, acknowledgements, roadblocks, and trip states with instant CSV download. |
| **Settings & Model** | Transparent inspection of the rule-based ETA formula and editable simulation inputs with backend validation. |

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
