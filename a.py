import requests
import pandas as pd
import time

LAT, LON, DIST_NM = 49.5666, 15.3794, 50 

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; flight-tracker/1.0)", "Accept": "application/json"}

# --- 1. Live positions + type/registration from adsb.lol ---
resp = requests.get(f"https://api.adsb.lol/v2/lat/{LAT}/lon/{LON}/dist/{DIST_NM}", headers=HEADERS, timeout=10)
resp.raise_for_status()
df = pd.DataFrame(resp.json()["ac"])
print(f"{len(df)} aircraft within {DIST_NM}nm of Czechia")

for col in ["flight", "r", "t"]:
    if col not in df.columns:
        df[col] = None
df[["flight", "r", "t"]] = df[["flight", "r", "t"]].fillna("?")
df["flight"] = df["flight"].str.strip()
df["hex"] = df["hex"].str.lower()

# --- 2. Operator + route from hexdb.io, per aircraft (their batch endpoint isn't needed here) ---
def lookup_hexdb(icao24: str) -> str:
    try:
        r = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao24}", timeout=5)
        return r.json().get("RegisteredOwners", "?") if r.status_code == 200 else "?"
    except requests.RequestException:
        return "?"

def lookup_route(callsign: str) -> str:
    try:
        r = requests.get(f"https://hexdb.io/api/v1/route/icao/{callsign}", timeout=5)
        if r.status_code == 200:
            d = r.json()
            return f"{d.get('origin', '?')}-{d.get('destination', '?')}"
        return "?"
    except requests.RequestException:
        return "?"

operators, routes = [], []
for _, row in df.iterrows():
    operators.append(lookup_hexdb(row["hex"]))
    routes.append(lookup_route(row["flight"]) if row["flight"] != "?" else "?")
    time.sleep(0.05)  # stay well under hexdb.io's rate limit

df["operator"] = operators
df["route"] = routes

# --- 3. Print ---
for _, row in df.iterrows():
    label = row["flight"] if row["flight"] != "?" else row["hex"]
    print(f"{label:10} | {row['r']:8} | {row['t']:6} | op: {row['operator']:22} | route: {row['route']}")