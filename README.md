# Flight Tracker

Personal live aircraft tracking app. It fetches ADS-B data for aircraft near a chosen point, enriches each aircraft with operator and route info, prints a table to the console, and shows the aircraft on a self-refreshing map in your browser.

## Features

- Live aircraft positions from the [adsb.lol](https://api.adsb.lol) API, within a set radius (nautical miles) of a lat/lon point.
- Enrichment per aircraft:
  - Registered operator/owner from [hexdb.io](https://hexdb.io).
  - IATA flight number and origin–destination route (e.g. `AGP-LHR`) from [adsbdb.com](https://www.adsbdb.com).
- Console output: one line per aircraft with callsign, registration, flight number, type, category, position, altitude, ground speed, route and operator.
- Interactive [Folium](https://python-visualization.github.io/folium/) map (`aircraft_map.html`) on OpenStreetMap tiles:
  - Marker color: **gray** on the ground, **orange** below 20,000 ft, **blue** at or above 20,000 ft (or altitude unknown).
  - Click a marker for details; hover for the callsign.
- The map is served locally at `http://127.0.0.1:8765/aircraft_map.html`, so the browser sends the Referer header that the OpenStreetMap tile policy requires.
- Data and map refresh every 15 seconds; the open browser tab reloads itself.

## Requirements

- Python 3.9+
- `requests`
- `folium`

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install requests folium
```

## Usage

```bash
python a.py
```

The app opens the map in your default browser and keeps updating until you press `Ctrl+C`.

## Configuration

Edit `main()` in `a.py` to change the search area:

```python
LAT, LON, DIST_NM = 49.120, 15.501, 50
tracker = FlightTracker(LAT, LON, DIST_NM, location_label="Czechia")
```

Other `FlightTracker` options:

| Parameter         | Default              | Meaning                                  |
|-------------------|----------------------|------------------------------------------|
| `map_filename`    | `aircraft_map.html`  | Output map file                          |
| `refresh_seconds` | `15`                 | Fetch and page-reload interval           |
| `server_port`     | `8765`               | Port of the local HTTP server            |
| `location_label`  | `the search point`   | Name shown in the console summary line   |

## How it works

`a.py` is split into small classes:

- `Flight`: one aircraft, with telemetry, enrichment fields, and display helpers (popup HTML, console line, marker color).
- `ADSBFetcher`: calls `https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}` and builds `Flight` objects.
- `FlightEnricher`: looks up operator (hexdb.io) and flight number and route (adsbdb.com) for each aircraft, with a short delay between calls to stay under rate limits.
- `FlightMap`: renders flights to a Folium map with referrer and auto-refresh meta tags.
- `LocalMapServer`: serves the working directory on `127.0.0.1` in a background thread.
- `FlightTracker`: runs the fetch, enrich, print and render loop.

## Notes

- The local server serves the whole working directory, bound to `127.0.0.1` only.
- Enrichment makes 2 HTTP requests per aircraft each cycle, so large radii mean slower updates.
- Unknown values show as `?`.
