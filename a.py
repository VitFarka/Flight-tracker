"""
Live aircraft tracker.

Fetches ADS-B data from adsb.lol, enriches it with registered-operator and
IATA flight-number lookups, and renders the result on a self-refreshing
Folium map served locally over HTTP (so OpenStreetMap sees a proper
Referer header).
"""

import http.server
import os
import threading
import time
import webbrowser
from typing import List, Optional

import folium
import requests


class Flight:
    """A single tracked aircraft: its telemetry plus enrichment data."""

    CATEGORY_MAP = {
        "A0": "no category info",
        "A1": "light aircraft",
        "A2": "small aircraft",
        "A3": "large aircraft",
        "A4": "high vortex large (e.g. 757)",
        "A5": "heavy aircraft",
        "A6": "high performance / high speed",
        "A7": "rotorcraft",
        "B0": "no category info",
        "B1": "glider / sailplane",
        "B2": "lighter-than-air",
        "B3": "parachutist / skydiver",
        "B4": "ultralight / hang-glider / paraglider",
        "B6": "unmanned aerial vehicle",
        "B7": "space / trans-atmospheric vehicle",
        "C0": "no category info",
        "C1": "surface vehicle - emergency",
        "C2": "surface vehicle - service",
        "C3": "point obstacle",
        "C4": "cluster obstacle",
        "C5": "line obstacle",
    }

    def __init__(
        self,
        hex_code: str,
        flight: Optional[str] = None,
        registration: Optional[str] = None,
        aircraft_type: Optional[str] = None,
        category: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        alt_baro=None,
        ground_speed: Optional[float] = None,
    ):
        self.hex = (hex_code or "?").lower()
        self.flight = (flight or "?").strip()
        self.registration = registration or "?"
        self.aircraft_type = aircraft_type or "?"
        self.category = category
        self.lat = lat
        self.lon = lon
        self.alt_baro = alt_baro  # "ground" (str) when parked/taxiing, else feet (number)
        self.ground_speed = ground_speed

        # Filled in later by a FlightEnricher
        self.operator = "?"
        self.flight_number = "?"
        self.route = "?"

    @classmethod
    def from_api_record(cls, record: dict) -> "Flight":
        """Build a Flight from one raw element of adsb.lol's "ac" list."""
        return cls(
            hex_code=record.get("hex"),
            flight=record.get("flight"),
            registration=record.get("r"),
            aircraft_type=record.get("t"),
            category=record.get("category"),
            lat=record.get("lat"),
            lon=record.get("lon"),
            alt_baro=record.get("alt_baro"),
            ground_speed=record.get("gs"),
        )

    @property
    def label(self) -> str:
        """Callsign if known, otherwise the ICAO hex address."""
        return self.flight if self.flight != "?" else self.hex

    @property
    def altitude_numeric(self) -> Optional[float]:
        """Barometric altitude in feet, or None if on the ground / unknown."""
        try:
            return float(self.alt_baro)
        except (TypeError, ValueError):
            return None

    @property
    def altitude_text(self) -> str:
        if self.alt_baro == "ground":
            return "ground"
        alt = self.altitude_numeric
        return f"{alt:.0f}ft" if alt is not None else "unknown"

    @property
    def category_text(self) -> str:
        return self.CATEGORY_MAP.get(self.category, "unknown")

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def marker_color(self) -> str:
        """Map-marker color: gray on the ground, orange below 20,000ft, blue above."""
        if self.alt_baro == "ground":
            return "gray"
        alt = self.altitude_numeric
        if alt is not None and alt < 20000:
            return "orange"
        return "blue"

    def popup_html(self) -> str:
        gs_text = f"{self.ground_speed:.0f}kt" if self.ground_speed is not None else "?"
        return (
            f"<b>{self.label}</b><br>"
            f"flight#: {self.flight_number}<br>"
            f"type: {self.aircraft_type} ({self.category_text})<br>"
            f"reg: {self.registration}<br>"
            f"alt: {self.altitude_text}<br>"
            f"gs: {gs_text}<br>"
            f"route: {self.route}<br>"
            f"operator: {self.operator}"
        )

    def console_line(self) -> str:
        gs_text = f"{self.ground_speed:.0f}kt" if self.ground_speed is not None else "?kt"
        lat_text = f"{self.lat:.4f}" if self.lat is not None else "?"
        lon_text = f"{self.lon:.4f}" if self.lon is not None else "?"
        return (
            f"call: {self.label:10} | reg: {self.registration:8} | flight#: {self.flight_number:6}| "
            f"type: {self.aircraft_type:6} |  cat: {self.category_text:22} | "
            f"lat: {lat_text:>9} lon: {lon_text:>9} | alt: {self.altitude_text:>8} | "
            f"gs: {gs_text:>7} | route: {self.route:10} |"
            f"op: {self.operator:22}"
        )


class ADSBFetcher:
    """Fetches live aircraft positions near a point from the adsb.lol API."""

    BASE_URL = "https://api.adsb.lol/v2"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; flight-tracker/1.0)",
        "Accept": "application/json",
    }

    def __init__(self, lat: float, lon: float, dist_nm: float, timeout: int = 10):
        self.lat = lat
        self.lon = lon
        self.dist_nm = dist_nm
        self.timeout = timeout

    def fetch(self) -> List[Flight]:
        url = f"{self.BASE_URL}/lat/{self.lat}/lon/{self.lon}/dist/{self.dist_nm}"
        response = requests.get(url, headers=self.HEADERS, timeout=self.timeout)
        response.raise_for_status()
        records = response.json().get("ac", [])
        return [Flight.from_api_record(record) for record in records]


class FlightEnricher:
    """Adds registered-operator and IATA flight-number info to Flights."""

    def __init__(self, throttle_seconds: float = 0.05, timeout: int = 5):
        self.throttle_seconds = throttle_seconds
        self.timeout = timeout

    def enrich(self, flights: List[Flight]) -> List[Flight]:
        for flight in flights:
            flight.operator = self._lookup_operator(flight.hex)
            if flight.flight != "?":
                flight.flight_number, flight.route = self._lookup_flightroute(flight.flight)
            time.sleep(self.throttle_seconds)  # stay well under each API's rate limit
        return flights

    def _lookup_operator(self, icao24: str) -> str:
        """Look up registered operator/owner for an aircraft via hexdb.io."""
        try:
            r = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao24}", timeout=self.timeout)
            if r.status_code == 200:
                return r.json().get("RegisteredOwners", "?")
        except requests.RequestException:
            pass
        return "?"

    def _lookup_flightroute(self, callsign: str):
        """Get the IATA flight number and origin-destination route for a callsign via adsbdb.com.

        Returns a (flight_number, route) tuple, e.g. ("BA455", "AGP-LHR").
        """
        try:
            r = requests.get(f"https://api.adsbdb.com/v0/callsign/{callsign}", timeout=self.timeout)
            if r.status_code == 200:
                route_data = r.json().get("response", {}).get("flightroute", {}) or {}
                flight_number = route_data.get("callsign_iata") or "?"
                origin = (route_data.get("origin") or {}).get("iata_code") or "?"
                destination = (route_data.get("destination") or {}).get("iata_code") or "?"
                route = f"{origin}-{destination}" if not (origin == "?" and destination == "?") else "?"
                return flight_number, route
        except requests.RequestException:
            pass
        return "?", "?"


class FlightMap:
    """Renders a list of Flights onto a Folium map and saves it as a self-refreshing HTML file."""

    def __init__(self, center_lat: float, center_lon: float, zoom_start: int = 8):
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.zoom_start = zoom_start

    def render(self, flights: List[Flight], filename: str, refresh_seconds: int) -> str:
        m = folium.Map(
            location=[self.center_lat, self.center_lon],
            zoom_start=self.zoom_start,
            tiles="OpenStreetMap",
        )

        for flight in flights:
            if not flight.has_position:
                continue
            folium.CircleMarker(
                location=[flight.lat, flight.lon],
                radius=6,
                color=flight.marker_color,
                fill=True,
                fill_opacity=0.85,
                popup=folium.Popup(flight.popup_html(), max_width=250),
                tooltip=flight.label,
            ).add_to(m)

        # OSM's tile policy requires a Referer header - this ensures the browser sends one
        m.get_root().html.add_child(
            folium.Element('<meta name="referrer" content="strict-origin-when-cross-origin">')
        )
        # Auto-reload the already-open browser tab every refresh_seconds, so it stays live
        m.get_root().html.add_child(
            folium.Element(f'<meta http-equiv="refresh" content="{refresh_seconds}">')
        )

        m.save(filename)
        return os.path.abspath(filename)


class LocalMapServer:
    """Serves the working directory over http://127.0.0.1 so the browser sends a Referer header."""

    def __init__(self, port: int):
        self.port = port
        self._server: Optional[http.server.ThreadingHTTPServer] = None

    def start(self) -> None:
        handler = http.server.SimpleHTTPRequestHandler
        handler.log_message = lambda *args, **kwargs: None  # keep the console output clean
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()

    def url_for(self, filename: str) -> str:
        return f"http://127.0.0.1:{self.port}/{filename}"


class FlightTracker:
    """Ties fetching, enrichment, mapping, and serving together into a live-updating loop."""

    def __init__(
        self,
        lat: float,
        lon: float,
        dist_nm: float,
        map_filename: str = "aircraft_map.html",
        refresh_seconds: int = 15,
        server_port: int = 8765,
        location_label: str = "the search point",
    ):
        self.dist_nm = dist_nm
        self.map_filename = map_filename
        self.refresh_seconds = refresh_seconds
        self.location_label = location_label

        self.fetcher = ADSBFetcher(lat, lon, dist_nm)
        self.enricher = FlightEnricher()
        self.map_renderer = FlightMap(lat, lon)
        self.server = LocalMapServer(server_port)

    def run_once(self) -> List[Flight]:
        """Fetch, enrich, and print one snapshot of nearby aircraft."""
        flights = self.fetcher.fetch()
        print(f"{len(flights)} aircraft within {self.dist_nm}nm of {self.location_label}")

        flights = self.enricher.enrich(flights)
        for flight in flights:
            print(flight.console_line())

        return flights

    def update_map(self, flights: List[Flight]) -> str:
        return self.map_renderer.render(flights, self.map_filename, self.refresh_seconds)

    def start(self) -> None:
        flights = self.run_once()
        self.update_map(flights)

        self.server.start()
        webbrowser.open(self.server.url_for(self.map_filename))  # opens once; the page then auto-reloads itself

        try:
            while True:
                time.sleep(self.refresh_seconds)
                flights = self.run_once()
                self.update_map(flights)
        except KeyboardInterrupt:
            print("Stopped.")


def main():
    LAT, LON, DIST_NM = lat,lon,dist
    tracker = FlightTracker(LAT, LON, DIST_NM, location_label="Czechia")
    tracker.start()


if __name__ == "__main__":
    main()