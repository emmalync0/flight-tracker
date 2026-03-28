#!/usr/bin/env python3
"""
Flight price tracker for SFO -> Antwerp (BRU/AMS) business class.

Uses Google Flights data via RapidAPI (free tier: 150 req/month)
and Pushover for phone push notifications on price drops.

Usage:
    python check_prices.py              # Run price check
    python check_prices.py manual SFO-BRU_2026-06-09_2026-06-15 2800 United
                                        # Manually log a price you saw online
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DIR = Path(__file__).parent
CONFIG_PATH = DIR / "config.json"
DATA_JS_PATH = DIR / "data.js"
HISTORY_PATH = DIR / "price_history.json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    if not CONFIG_PATH.exists():
        print(f"Error: {CONFIG_PATH} not found.")
        print("Copy config.example.json -> config.json and fill in your API keys.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Google Flights via RapidAPI
# ---------------------------------------------------------------------------

class GoogleFlightsClient:
    HOST = "google-flights2.p.rapidapi.com"
    BASE = f"https://{HOST}/api/v1"

    def __init__(self, api_key):
        self.session = requests.Session()
        self.session.headers.update({
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": self.HOST,
        })

    def search_flights(self, origin, dest, outbound_date, return_date,
                       cabin="BUSINESS", adults=1, max_stops=0):
        resp = self.session.get(
            f"{self.BASE}/searchFlights",
            params={
                "departure_id": origin,
                "arrival_id": dest,
                "outbound_date": outbound_date,
                "return_date": return_date,
                "travel_class": cabin,
                "adults": adults,
                "stops": max_stops,
                "currency": "USD",
                "language_code": "en-US",
                "country_code": "US",
            },
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Pushover notifications
# ---------------------------------------------------------------------------

class PushoverNotifier:
    API_URL = "https://api.pushover.net/1/messages.json"

    def __init__(self, user_key, app_token):
        self.user_key = user_key
        self.app_token = app_token

    def send(self, title, message, priority=0, url=None):
        payload = {
            "token": self.app_token,
            "user": self.user_key,
            "title": title,
            "message": message,
            "priority": priority,
        }
        if url:
            payload["url"] = url
        resp = requests.post(self.API_URL, data=payload)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# History persistence
# ---------------------------------------------------------------------------

def load_history():
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return {
        "routes": {},
        "alerts": [],
        "stats": {"lowestEver": None, "checkCount": 0},
    }


def save_history(history):
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)


def write_data_js(history, config):
    """Write data.js that the dashboard HTML loads."""
    tracking = config["tracking"]
    destinations = list({r["dest"] for r in tracking["routes"]})
    data = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "config": {
            "origin": tracking["routes"][0]["origin"],
            "destinations": destinations,
            "cabin": tracking["cabin"],
            "passengers": tracking["passengers"],
            "targetPrice": tracking.get("target_price_per_person", 3000),
        },
        "routes": history["routes"],
        "alerts": history["alerts"][-30:],
        "stats": history["stats"],
    }
    with open(DATA_JS_PATH, "w") as f:
        f.write(f"window.FLIGHT_DATA = {json.dumps(data, indent=2)};\n")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_google_flights(data):
    """Extract best offers from Google Flights API response."""
    offers = []
    itineraries = data.get("data", {}).get("itineraries", {})

    for group_key in ("topFlights", "otherFlights"):
        flights = itineraries.get(group_key, [])
        if not flights:
            continue
        for flight in flights:
            price = flight.get("price")
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            duration = flight.get("duration", {})
            dur_text = duration.get("text", "") if isinstance(duration, dict) else str(duration)

            # Segments are in flight["flights"] (confusing naming by the API)
            segments = flight.get("flights", [])
            stops = max(0, len(segments) - 1)

            # Nonstop only
            if stops > 0:
                continue

            # Get airline from first segment
            carrier = "??"
            flight_numbers = []
            if segments:
                carrier = segments[0].get("airline", "??")
                if carrier == "??":
                    logo = segments[0].get("airline_logo", "")
                    if "/70px/" in logo:
                        carrier = logo.split("/70px/")[1].split(".")[0]
                flight_numbers = [s.get("flight_number", "") for s in segments]

            # Departure/arrival times
            dep_time = flight.get("departure_time", "")
            arr_time = flight.get("arrival_time", "")

            # Layover info
            layovers = flight.get("layovers", [])
            layover_text = ""
            skip = False
            if layovers:
                parts = []
                for lo in layovers:
                    city = lo.get("city", lo.get("airport_code", "?"))
                    dur = lo.get("duration_label", "")
                    parts.append(f"{dur} in {city}")
                    # Filter out long layovers (> 4 hours)
                    minutes = lo.get("duration", 0)
                    if not minutes and dur:
                        import re
                        h = re.search(r"(\d+)\s*hr", dur)
                        m = re.search(r"(\d+)\s*min", dur)
                        minutes = (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
                    if minutes > 240:
                        skip = True
                layover_text = "; ".join(parts)
            if skip:
                continue

            offers.append({
                "price": price,
                "outStops": stops,
                "outCarrier": carrier,
                "outDuration": dur_text,
                "departureTime": dep_time,
                "arrivalTime": arr_time,
                "flightNumbers": ", ".join(flight_numbers),
                "layover": layover_text,
            })

    offers.sort(key=lambda x: x["price"])
    return offers


# ---------------------------------------------------------------------------
# Main price check
# ---------------------------------------------------------------------------

def check_prices(config):
    tracking = config["tracking"]
    client = GoogleFlightsClient(config["rapidapi"]["api_key"])

    notifier = None
    po = config.get("pushover", {})
    if po.get("user_key") and po.get("app_token"):
        notifier = PushoverNotifier(po["user_key"], po["app_token"])

    history = load_history()
    now = datetime.now(timezone.utc).isoformat()
    history["stats"]["checkCount"] = history["stats"].get("checkCount", 0) + 1

    target_price = tracking.get("target_price_per_person", 3000)
    alert_drop_pct = tracking.get("alert_drop_percent", 10)
    alerts = []

    for route in tracking["routes"]:
        origin = route["origin"]
        dest = route["dest"]
        out_date = route["outbound"]
        ret_date = route["return"]
        route_key = f"{origin}-{dest}_{out_date}_{ret_date}"
        print(f"  Checking {route_key} ...")

        try:
            results = client.search_flights(
                origin, dest, out_date, ret_date,
                cabin=tracking["cabin"], adults=1,
            )

            if not results.get("status"):
                msg = results.get("message", "Unknown error")
                print(f"    API error: {msg}")
                continue

            offers = parse_google_flights(results)
            if not offers:
                print("    No offers found")
                continue

            best = offers[0]
            print(
                f"    Best: ${best['price']:,.0f}/person "
                f"({best['outCarrier']}, {best['outStops']} stop(s))"
            )

            # Find best United offer
            ua_offers = [
                o for o in offers
                if "united" in o["outCarrier"].lower()
                or o.get("flightNumbers", "").upper().startswith("UA")
            ]
            ua_best = ua_offers[0] if ua_offers else None
            if ua_best:
                print(
                    f"    Best UA: ${ua_best['price']:,.0f}/person "
                    f"({ua_best['outStops']} stop(s), {ua_best.get('layover', 'nonstop')})"
                )
            else:
                print("    No United flights found")

            # Ensure route entry exists
            if route_key not in history["routes"]:
                history["routes"][route_key] = {
                    "origin": origin,
                    "destination": dest,
                    "outbound": out_date,
                    "return": ret_date,
                    "history": [],
                }

            route_hist = history["routes"][route_key]
            prev_best = (
                route_hist["history"][-1]["price"]
                if route_hist["history"] else None
            )

            entry = {
                "timestamp": now,
                "price": best["price"],
                "carrier": best["outCarrier"],
                "outStops": best["outStops"],
                "outDuration": best.get("outDuration", ""),
                "departureTime": best.get("departureTime", ""),
                "arrivalTime": best.get("arrivalTime", ""),
                "flightNumbers": best.get("flightNumbers", ""),
                "layover": best.get("layover", ""),
            }
            if ua_best:
                entry["unitedPrice"] = ua_best["price"]
                entry["unitedStops"] = ua_best["outStops"]
                entry["unitedDuration"] = ua_best.get("outDuration", "")
                entry["unitedDepartureTime"] = ua_best.get("departureTime", "")
                entry["unitedArrivalTime"] = ua_best.get("arrivalTime", "")
                entry["unitedFlightNumbers"] = ua_best.get("flightNumbers", "")
                entry["unitedLayover"] = ua_best.get("layover", "")
            route_hist["history"].append(entry)
            # Keep last 180 entries
            route_hist["history"] = route_hist["history"][-180:]

            # ---- Alert: significant price drop ----
            if prev_best and best["price"] < prev_best:
                drop_pct = (prev_best - best["price"]) / prev_best * 100
                if drop_pct >= alert_drop_pct:
                    msg = (
                        f"Price drop {drop_pct:.0f}%: "
                        f"{origin}->{dest} {out_date} to {ret_date} "
                        f"now ${best['price']:,.0f}/person "
                        f"(was ${prev_best:,.0f})"
                    )
                    alerts.append(msg)
                    history["alerts"].append({
                        "timestamp": now, "message": msg,
                        "route": route_key,
                    })

            # ---- Alert: below target ----
            if best["price"] <= target_price:
                recent_target_alerts = [
                    a for a in history["alerts"][-10:]
                    if a.get("route") == route_key
                    and "Below target" in a.get("message", "")
                ]
                if not recent_target_alerts:
                    msg = (
                        f"Below target! {origin}->{dest} "
                        f"{out_date} to {ret_date}: "
                        f"${best['price']:,.0f}/person "
                        f"(target: ${target_price:,})"
                    )
                    alerts.append(msg)
                    history["alerts"].append({
                        "timestamp": now, "message": msg,
                        "route": route_key,
                    })

            # ---- Global lowest ----
            if (
                history["stats"]["lowestEver"] is None
                or best["price"] < history["stats"]["lowestEver"]
            ):
                history["stats"]["lowestEver"] = best["price"]

        except Exception as e:
            print(f"    Error: {e}")
            continue

        time.sleep(1)  # rate limiting between requests

    # ---- Send push notification ----
    if alerts and notifier:
        has_drop = any("drop" in a.lower() for a in alerts)
        title = f"Flight Alert: {'Price Drop!' if has_drop else 'Deal Found!'}"
        body = "\n".join(alerts[:5])
        try:
            notifier.send(title, body, priority=0)
            print(f"\n  Pushover notification sent ({len(alerts)} alert(s))")
        except Exception as e:
            print(f"\n  Pushover error: {e}")
    elif alerts:
        print(f"\n  {len(alerts)} alert(s) generated (Pushover not configured)")

    save_history(history)
    write_data_js(history, config)
    print(
        f"\nDone. Check #{history['stats']['checkCount']}. "
        f"Data written to {DATA_JS_PATH.name}"
    )


# ---------------------------------------------------------------------------
# Manual price entry
# ---------------------------------------------------------------------------

def add_manual_price(route_key, price, carrier="Manual"):
    """Log a price you found on United.com, Google Flights, etc."""
    history = load_history()
    config = load_config()
    now = datetime.now(timezone.utc).isoformat()

    parts = route_key.split("_")
    if len(parts) != 3:
        print("Route key format: ORIGIN-DEST_YYYY-MM-DD_YYYY-MM-DD")
        print("Example: SFO-BRU_2026-06-09_2026-06-15")
        return

    airports = parts[0].split("-")
    if route_key not in history["routes"]:
        history["routes"][route_key] = {
            "origin": airports[0],
            "destination": airports[1],
            "outbound": parts[1],
            "return": parts[2],
            "history": [],
        }

    history["routes"][route_key]["history"].append({
        "timestamp": now,
        "price": price,
        "carrier": carrier,
        "outStops": -1,
        "outDuration": "",
    })

    if history["stats"]["lowestEver"] is None or price < history["stats"]["lowestEver"]:
        history["stats"]["lowestEver"] = price

    save_history(history)
    write_data_js(history, config)
    print(f"Added ${price:,.0f}/person for {route_key} ({carrier})")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "manual":
        if len(sys.argv) < 4:
            print("Usage: python check_prices.py manual ROUTE_KEY PRICE [CARRIER]")
            print("  e.g. python check_prices.py manual SFO-BRU_2026-06-09_2026-06-15 2800 United")
            sys.exit(1)
        carrier = sys.argv[4] if len(sys.argv) > 4 else "Manual"
        add_manual_price(sys.argv[2], float(sys.argv[3]), carrier)
    else:
        config = load_config()
        print(f"Flight Price Check - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print("=" * 55)
        check_prices(config)
