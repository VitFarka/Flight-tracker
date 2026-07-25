import requests
import pandas as pd
import time
import os
import webbrowser
import folium
import http.server
import threading
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



# --- Config ---
LAT, LON, DIST_NM = 49.5666, 15.3794, 50 

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; flight-tracker/1.0)",
    "Accept": "application/json",
}


def fetch_live_aircraft(lat: float, lon: float, dist_nm: float) -> pd.DataFrame:
    """Get live aircraft positions, type and registration near a point."""
    resp = requests.get(
        f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist_nm}",
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    df = pd.DataFrame(resp.json()["ac"])

    for col in ["flight", "r", "t", "lat", "lon", "alt_baro", "gs", "category"]:
        if col not in df.columns:
            df[col] = None
    df[["flight", "r", "t"]] = df[["flight", "r", "t"]].fillna("?")
    df["flight"] = df["flight"].str.strip()
    df["hex"] = df["hex"].str.lower()

    # alt_baro is "ground" (a string) when parked/taxiing, otherwise a number in feet
    df["alt_baro_numeric"] = pd.to_numeric(df["alt_baro"], errors="coerce")
    return df



def category_to_text(code: str) -> str:
    return CATEGORY_MAP.get(code, "unknown")

def lookup_operator(icao24: str) -> str:
    """Look up registered operator/owner for an aircraft via hexdb.io."""
    try:
        r = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao24}", timeout=5)
        if r.status_code == 200:
            return r.json().get("RegisteredOwners", "?")
    except requests.RequestException:
        pass
    return "?"

 
def lookup_flight_number(callsign: str) -> str:
    """Convert an ICAO callsign (e.g. 'BAW900A') to an IATA flight number (e.g. 'BA455') via adsbdb.com."""
    try:
        r = requests.get(f"https://api.adsbdb.com/v0/callsign/{callsign}", timeout=5)
        if r.status_code == 200:
            route = r.json().get("response", {}).get("flightroute", {})
            return route.get("callsign_iata", "?")
    except requests.RequestException:
        pass
    return "?"



def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Add operator and route columns, throttled to be polite to hexdb.io."""
    operators, flight_numbers = [], []
    for _, row in df.iterrows():
        operators.append(lookup_operator(row["hex"]))
        flight_numbers.append(lookup_flight_number(row["flight"]) if row["flight"] != "?" else "?")
        time.sleep(0.05)  # stay well under hexdb.io's rate limit
    df["operator"] = operators
    df["flight_number"] = flight_numbers
    return df


def color_for_altitude(alt_baro, alt_numeric) -> str:
    if alt_baro == "ground":
        return "gray"
    if pd.notna(alt_numeric) and alt_numeric < 20000:
        return "orange"
    return "blue"


def plot_aircraft_map(df: pd.DataFrame, filename: str = "aircraft_map.html", refresh_seconds: int = 10) -> str:
    """Plot each aircraft as a dot on a real map and save it as an HTML file."""
    m = folium.Map(location=[LAT, LON], zoom_start=8, tiles="OpenStreetMap")

    for _, row in df.iterrows():
        if pd.isna(row["lat"]) or pd.isna(row["lon"]):
            continue
        label = row["flight"] if row["flight"] != "?" else row["hex"]
        alt_text = "ground" if row["alt_baro"] == "ground" else (
            f"{row['alt_baro']:.0f}ft" if pd.notna(row["alt_baro_numeric"]) else "unknown"
        )
        popup_text = (
            f"<b>{label}</b><br>"
            f"flight#: {row['flight_number']}<br>"
            f"type: {row['t']} ({category_to_text(row['category'])})<br>"
            f"reg: {row['r']}<br>"
            f"alt: {alt_text}<br>"
            f"gs: {row['gs']:.0f}kt<br>"
            f"operator: {row['operator']}"
        )
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=6,
            color=color_for_altitude(row["alt_baro"], row["alt_baro_numeric"]),
            fill=True,
            fill_opacity=0.85,
            popup=folium.Popup(popup_text, max_width=250),
            tooltip=label,
        ).add_to(m)

    # OSM's tile policy requires a Referer header - this ensures the browser sends one
    m.get_root().html.add_child(folium.Element(
        '<meta name="referrer" content="strict-origin-when-cross-origin">'
    ))

    # Auto-reload the already-open browser tab every refresh_seconds, so it stays live
    m.get_root().html.add_child(folium.Element(
        f'<meta http-equiv="refresh" content="{refresh_seconds}">'
    ))

    m.save(filename)
    return os.path.abspath(filename)


REFRESH_SECONDS = 15
MAP_FILE = "aircraft_map.html"
SERVER_PORT = 8765


def start_local_server(port: int) -> None:
    """Serve the current directory over http://localhost so the browser sends a Referer header."""
    handler = http.server.SimpleHTTPRequestHandler
    handler.log_message = lambda *args, **kwargs: None  # keep the console output clean
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def run_once() -> pd.DataFrame:
    df = fetch_live_aircraft(LAT, LON, DIST_NM)
    print(f"{len(df)} aircraft within {DIST_NM}nm of Czechia")

    df = enrich(df)

    for _, row in df.iterrows():
        label = row["flight"] if row["flight"] != "?" else row["hex"]
        alt = "ground" if row["alt_baro"] == "ground" else f"{row['alt_baro']:.0f}ft" if pd.notna(row["alt_baro_numeric"]) else "?"
        print(
            f"call: {label:10} | reg: {row['r']:8} | flight#: {row['flight_number']:6}| "
            f"type: {row['t']:6} |  cat: {category_to_text(row['category']):22} | "
            f"lat: {row['lat']:.4f} lon: {row['lon']:.4f} | alt: {alt:>8} | "
            f"gs: {row['gs']:.0f}kt |"
            f"op: {row['operator']:22}"
        )

    return df


def main():
    df = run_once()
    map_path = plot_aircraft_map(df, filename=MAP_FILE, refresh_seconds=REFRESH_SECONDS)

    start_local_server(SERVER_PORT)
    webbrowser.open(f"http://127.0.0.1:{SERVER_PORT}/{MAP_FILE}")  # opens once; the page then auto-reloads itself

    try:
        while True:
            time.sleep(REFRESH_SECONDS)
            df = run_once()
            plot_aircraft_map(df, filename=MAP_FILE, refresh_seconds=REFRESH_SECONDS)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()