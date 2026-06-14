"""Geocoding (Nominatim, cached, rate limited) and commute estimation
(Google Maps Distance Matrix with haversine-radius fallback — spec 6.2/22)."""
import logging
import math
import threading
import time

import httpx

from . import config, db

log = logging.getLogger("geo")

_nominatim_lock = threading.Lock()
_last_nominatim = [0.0]


def geocode(place: str):
    """Returns (lat, lon) or None. Cached in the DB; 1 req/s to Nominatim."""
    place = (place or "").strip()
    if not place:
        return None
    cached = db.query_one("SELECT latitude, longitude FROM geocode_cache WHERE place = ?", (place.lower(),))
    if cached:
        if cached["latitude"] is None:
            return None
        return cached["latitude"], cached["longitude"]
    result = None
    try:
        with _nominatim_lock:
            wait = 1.0 - (time.time() - _last_nominatim[0])
            if wait > 0:
                time.sleep(wait)
            _last_nominatim[0] = time.time()
        resp = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place, "format": "json", "limit": 1},
            headers={"User-Agent": "JobSearchAutomation/1.0 (local)"},
            timeout=15,
        )
        if resp.status_code == 200 and resp.json():
            hit = resp.json()[0]
            result = (float(hit["lat"]), float(hit["lon"]))
    except Exception as exc:
        log.warning("Geocode failed for %r: %s", place, exc)
        return None  # don't cache transient failures
    db.execute(
        "INSERT OR IGNORE INTO geocode_cache (place, latitude, longitude) VALUES (?, ?, ?)",
        (place.lower(), result[0] if result else None, result[1] if result else None),
    )
    return result


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


GOOGLE_MODES = {"driving": "driving", "transit": "transit", "walking": "walking", "biking": "bicycling"}


def commute_minutes(home_address: str, job_location: str, mode: str):
    """Real-world commute time via Google Maps for an ~8 AM arrival.
    Returns minutes (int) or None when the API is unavailable/fails."""
    if not config.GOOGLE_MAPS_API_KEY or not home_address or not job_location:
        return None
    try:
        # next weekday 8 AM arrival
        now = time.localtime()
        day = time.mktime((now.tm_year, now.tm_mon, now.tm_mday + 1, 8, 0, 0, 0, 0, -1))
        while time.localtime(day).tm_wday >= 5:  # skip weekend
            day += 86400
        resp = httpx.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins": home_address,
                "destinations": job_location,
                "mode": GOOGLE_MODES.get(mode, "driving"),
                "arrival_time": int(day),
                "key": config.GOOGLE_MAPS_API_KEY,
            },
            timeout=15,
        )
        data = resp.json()
        element = data["rows"][0]["elements"][0]
        if element.get("status") == "OK":
            return int(round(element["duration"]["value"] / 60))
    except Exception as exc:
        log.warning("Commute calculation failed (%s -> %s): %s", home_address, job_location, exc)
    return None
