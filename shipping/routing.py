"""
Delivery route optimization.

Apple Maps links can only carry ONE destination each, and Apple Maps never
re-orders stops itself — so SFL ERP System does the optimizing:

1. Geocode every address (free OpenStreetMap/Nominatim, cached in the DB
   forever so each address is looked up exactly once).
2. Order the stops with nearest-neighbor from the warehouse, then improve
   with 2-opt passes (classic traveling-salesman heuristics — optimal or
   near-optimal for a day's worth of local stops).
3. Hand the driver numbered stops, each with a one-tap Apple Maps
   "navigate" link, plus a single Google Maps link with the whole route
   in the optimized order as a fallback.

If geocoding fails (no internet, unparseable address), the original order
is kept — the buttons always work, optimization is best-effort.
"""
import math
from urllib.parse import quote, urlencode

import requests
from django.db import models

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "SFL-ERP-delivery-routing/1.0 (wholesale ERP; contact: admin)"


class GeocodeCache(models.Model):
    """Address -> lat/lng, cached permanently (addresses don't move)."""

    address = models.CharField(max_length=400, unique=True, db_index=True)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "shipping"

    def __str__(self):
        return f"{self.address} -> {self.lat},{self.lng}" if self.ok else f"{self.address} (failed)"


def geocode(address):
    """Return (lat, lng) or None. DB-cached; one network hit per new address."""
    address = " ".join((address or "").split())
    if not address:
        return None
    cached = GeocodeCache.objects.filter(address=address).first()
    if cached:
        return (cached.lat, cached.lng) if cached.ok else None
    lat = lng = None
    ok = False
    try:
        r = requests.get(NOMINATIM_URL,
                         params={"q": address, "format": "json", "limit": 1,
                                 "countrycodes": "us"},
                         headers={"User-Agent": USER_AGENT}, timeout=6)
        r.raise_for_status()
        results = r.json()
        if results:
            lat, lng = float(results[0]["lat"]), float(results[0]["lon"])
            ok = True
    except Exception:
        pass  # cache the failure too, so a bad address doesn't re-query forever
    GeocodeCache.objects.get_or_create(address=address,
                                       defaults={"lat": lat, "lng": lng, "ok": ok})
    return (lat, lng) if ok else None


def _dist(a, b):
    """Haversine miles between two (lat, lng) points."""
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def optimize_order(origin_pt, points):
    """Return the index order visiting all ``points`` starting from origin.

    Nearest-neighbor construction + 2-opt improvement (open route: the
    driver doesn't have to return to the warehouse).
    """
    n = len(points)
    if n <= 1:
        return list(range(n))
    # nearest neighbor
    unvisited = set(range(n))
    order = []
    cur = origin_pt
    while unvisited:
        nxt = min(unvisited, key=lambda i: _dist(cur, points[i]))
        order.append(nxt)
        unvisited.remove(nxt)
        cur = points[nxt]

    # 2-opt: reverse segments while it shortens the total path
    def total(o):
        d = _dist(origin_pt, points[o[0]])
        for a, b in zip(o, o[1:]):
            d += _dist(points[a], points[b])
        return d

    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if total(cand) < total(order) - 1e-9:
                    order = cand
                    improved = True
    return order


def plan_route(origin, destinations):
    """Optimize a day's stops.

    ``destinations``: list of address strings (may repeat).
    Returns (ordered_indexes, optimized) — ``optimized`` False when
    geocoding couldn't resolve enough points, in which case the original
    order is returned unchanged.
    """
    if len(destinations) < 2:
        return list(range(len(destinations))), False
    origin_pt = geocode(origin) if origin else None
    if origin_pt is None:
        return list(range(len(destinations))), False
    pts = [geocode(d) for d in destinations]
    if any(p is None for p in pts):
        return list(range(len(destinations))), False
    return optimize_order(origin_pt, pts), True


# ---------------------------------------------------------------------------
# Link builders
# ---------------------------------------------------------------------------
def apple_maps_url(destination, origin=None):
    """One-tap Apple Maps driving navigation to a single stop.

    (Apple's URL scheme supports exactly one destination per link.)
    Omitting saddr makes Apple Maps start from the driver's current
    location — which is what you want between stops.
    """
    params = {"daddr": destination, "dirflg": "d"}
    if origin:
        params["saddr"] = origin
    return "https://maps.apple.com/?" + urlencode(params)


def google_maps_route_url(origin, stops):
    """Full multi-stop route in one link (Google supports this; Apple doesn't)."""
    points = [origin] + [s for s in stops if s]
    if len(points) < 2:
        return None
    return "https://www.google.com/maps/dir/" + "/".join(quote(p) for p in points)
