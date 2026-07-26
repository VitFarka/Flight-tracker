"""
Live aircraft tracker.

Fetches ADS-B data from adsb.lol, enriches it with registered-operator and
IATA flight-number lookups, and renders the result on a self-refreshing
Folium map served locally over HTTP (so OpenStreetMap sees a proper
Referer header). Each aircraft is drawn as an arrow pointing in its
direction of travel (folium-arrow-icon: pip install folium-arrow-icon).
"""

import http.server
import math
import os
import threading
import time
import webbrowser
from typing import List, Optional

import folium
import requests
from folium_arrow_icon import ArrowIcon


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
        track: Optional[float] = None,
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
        self.track = track  # true track over the ground, degrees clockwise from north

        # Filled in later by a FlightEnricher
        self.operator = "?"
        self.flight_number = "?"
        self.route = ["?", None, None]

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
            track=record.get("track"),
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
    def route_text(self) -> str:
        return self.route[0]

    @property
    def origin_coords(self):
        """(lat, lon) tuple for the origin airport, or None if unknown."""
        return self.route[1]

    @property
    def destination_coords(self):
        """(lat, lon) tuple for the destination airport, or None if unknown."""
        return self.route[2]

    @property
    def has_route_coords(self) -> bool:
        """True only when both origin and destination coordinates are known."""
        return self.origin_coords is not None and self.destination_coords is not None

    @property
    def direction_radians(self) -> Optional[float]:
        """Track over the ground as an angle in radians for ArrowIcon.

        ArrowIcon's angle starts from the positive-latitude (north) axis and
        goes clockwise, which is exactly how "track" is already defined, so
        this is a plain degrees-to-radians conversion. Returns None when no
        track is known (e.g. the aircraft is stationary on the ground).
        """
        if self.track is None:
            return None
        return math.radians(self.track)

    @property
    def marker_color(self) -> str:
        """Marker color by aircraft category."""
        if self.category == "A3":
            return "blue"
        if self.category == "A5":
            return "red"
        if self.category == "A2":
            return "green"

        return "gray"

    def popup_html(self) -> str:
        gs_text = f"{self.ground_speed:.0f}kt" if self.ground_speed is not None else "?"
        return (
            f"<b>{self.label}</b><br>"
            f"flight#: {self.flight_number}<br>"
            f"type: {self.aircraft_type} ({self.category_text})<br>"
            f"reg: {self.registration}<br>"
            f"alt: {self.altitude_text}<br>"
            f"gs: {gs_text}<br>"
            f"route: {self.route_text}<br>"
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
            f"gs: {gs_text:>7} | route: {self.route_text:10} |"
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
        """Get the IATA flight number and route for a callsign via adsbdb.com.

        Returns a (flight_number, route) tuple where route is
        [route_text, origin_coords, destination_coords]. Always this
        3-element list shape, even on failure - callers rely on it.
        """
        try:
            r = requests.get(f"https://api.adsbdb.com/v0/callsign/{callsign}", timeout=self.timeout)
            if r.status_code == 200:
                route_data = r.json().get("response", {}).get("flightroute", {}) or {}
                flight_number = route_data.get("callsign_iata") or "?"
                route = self._format_route(route_data.get("origin") or {}, route_data.get("destination") or {})
                return flight_number, route
        except requests.RequestException:
            pass
        return "?", ["?", None, None]

    @staticmethod
    def _format_route(origin: dict, destination: dict) -> list:
        def airport_label(airport: dict) -> Optional[str]:
            name = airport.get("name")
            code = airport.get("iata_code") or airport.get("icao_code")
            if name and code:
                return f"{name} ({code})"
            return name or code

        def airport_coords(airport: dict):
            lat, lon = airport.get("latitude"), airport.get("longitude")
            return (lat, lon) if lat is not None and lon is not None else None

        origin_label = airport_label(origin)
        destination_label = airport_label(destination)
        if not origin_label and not destination_label:
            text = "?"
        else:
            text = f"{origin_label or '?'} \u2192 {destination_label or '?'}"

        return [text, airport_coords(origin), airport_coords(destination)]


class FlightMap:
    """Renders a list of Flights onto a Folium map and saves it as a self-refreshing HTML file.

    Aircraft with a known track are drawn as arrows pointing in their
    direction of travel; aircraft with no known track (e.g. parked) fall
    back to a plain colored dot. When both the origin and destination
    airport coordinates are known, a line is drawn between them.
    """

    def __init__(self, center_lat: float, center_lon: float, zoom_start: int = 8, arrow_length: int = 22):
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.zoom_start = zoom_start
        self.arrow_length = arrow_length  # pixels; constant regardless of zoom level

    def render(self, flights: List[Flight], filename: str, refresh_seconds: int) -> str:
        m = folium.Map(
            location=[self.center_lat, self.center_lon],
            zoom_start=self.zoom_start,
            tiles="OpenStreetMap",
        )

        for flight in flights:
            if not flight.has_position:
                continue
            self._add_marker(m, flight)
            #self._add_route_line(m, flight)

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

    def _add_marker(self, m: folium.Map, flight: Flight) -> None:
        popup = folium.Popup(flight.popup_html(), max_width=250)

        if flight.direction_radians is not None:
            icon = ArrowIcon(
                self.arrow_length,
                flight.direction_radians,
                color=flight.marker_color,
                anchor="mid",  # center the arrow on the aircraft's actual position
            )
            folium.Marker(
                location=[flight.lat, flight.lon],
                icon=icon,
                popup=popup,
                tooltip=flight.label,
            ).add_to(m)
        else:
            # No known heading - fall back to a plain dot rather than guessing a direction.
            folium.CircleMarker(
                location=[flight.lat, flight.lon],
                radius=6,
                color=flight.marker_color,
                fill=True,
                fill_opacity=0.85,
                popup=popup,
                tooltip=flight.label,
            ).add_to(m)

    def _add_route_line(self, m: folium.Map, flight: Flight) -> None:
        """Draw a line between the origin and destination airports, if both are known."""
        if not flight.has_route_coords:
            return
        folium.PolyLine(
            locations=[flight.origin_coords,(flight.lat, flight.lon) ,flight.destination_coords],
            color="#FF0000",
            weight=2,
            opacity=0.6,
        ).add_to(m)


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
    LAT, LON, DIST_NM = , 50
    tracker = FlightTracker(LAT, LON, DIST_NM, location_label="Czechia")
    tracker.start()


if __name__ == "__main__":
    main()