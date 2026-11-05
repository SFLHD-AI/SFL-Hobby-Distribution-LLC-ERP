"""
Inbound shipment tracking for supplier orders.

Shippo's tracking API is free with the same token used for labels, covers
USPS/UPS/FedEx/DHL and more, and returns the current status, the latest scan
and the carrier's estimated delivery date. We refresh every open supplier
order with a tracking number hourly (``inventory.tasks.refresh_inbound_tracking``)
and on demand from the receiving document. Without a Shippo token the carrier
link still opens the carrier's own tracking page.
"""
import logging
from datetime import datetime

from django.utils import timezone

log = logging.getLogger(__name__)

CARRIER_URLS = {
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={n}",
    "ups": "https://www.ups.com/track?tracknum={n}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={n}",
    "dhl_express": "https://www.dhl.com/us-en/home/tracking/tracking-express.html?submit=1&tracking-id={n}",
    "ontrac": "https://www.ontrac.com/tracking/?number={n}",
    "lasership": "https://www.lasership.com/track/{n}",
    "amazon": "https://track.amazon.com/tracking/{n}",
}
# Shippo carrier tokens that support tracking
SHIPPO_CARRIERS = {"usps", "ups", "fedex", "dhl_express", "ontrac", "lasership"}


def carrier_url(carrier, number):
    if not number:
        return ""
    tpl = CARRIER_URLS.get(carrier)
    return tpl.format(n=number) if tpl else f"https://www.google.com/search?q={number}"


def guess_carrier(number):
    """Best-effort carrier from the number's shape (used when the owner leaves it blank)."""
    n = (number or "").replace(" ", "").upper()
    if n.startswith("1Z") and len(n) == 18:
        return "ups"
    if n.isdigit() and len(n) in (12, 15, 20, 22):
        return "fedex" if len(n) in (12, 15) else "usps"
    if n.startswith(("92", "93", "94", "95")) and len(n) >= 20:
        return "usps"
    if n.startswith("TBA"):
        return "amazon"
    return ""


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return dt


def fetch_status(carrier, number, provider=None):
    """Ask Shippo for the current tracking state. Returns a dict with status,
    detail, location, event_at, eta — or raises ProviderError/ProviderNotConfigured."""
    from shipping.providers import get_provider
    provider = provider or get_provider("shippo")
    data = provider._request("GET", f"/tracks/{carrier}/{number}")
    ts = data.get("tracking_status") or {}
    loc = ts.get("location") or {}
    location = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
    eta = data.get("eta")
    return {
        "status": (ts.get("status") or "UNKNOWN").upper(),
        "detail": (ts.get("status_details") or "")[:300],
        "location": location[:150],
        "event_at": _parse_dt(ts.get("status_date")),
        "eta": _parse_dt(eta).date() if eta else None,
    }


def refresh_package(pkg, provider=None):
    """Update one package's tracking fields. Returns (ok, message)."""
    from shipping.providers import ProviderError, ProviderNotConfigured, get_provider
    carrier = pkg.carrier or guess_carrier(pkg.tracking_number)
    if carrier and carrier != pkg.carrier:
        pkg.carrier = carrier
    if carrier not in SHIPPO_CARRIERS:
        pkg.checked_at = timezone.now()
        pkg.error = "Automatic tracking isn't available for this carrier; use the link."
        pkg.save(update_fields=["carrier", "checked_at", "error"])
        return False, pkg.error
    provider = provider or get_provider("shippo")
    try:
        info = fetch_status(carrier, pkg.tracking_number, provider)
    except ProviderNotConfigured:
        pkg.checked_at = timezone.now()
        pkg.error = "Add a Shippo token in Settings to track automatically."
        pkg.save(update_fields=["carrier", "checked_at", "error"])
        return False, pkg.error
    except ProviderError as e:
        pkg.checked_at = timezone.now()
        pkg.error = str(e)[:200]
        pkg.save(update_fields=["carrier", "checked_at", "error"])
        return False, pkg.error
    pkg.status, pkg.detail, pkg.location = info["status"], info["detail"], info["location"]
    pkg.event_at, pkg.eta = info["event_at"], info["eta"]
    pkg.checked_at, pkg.error = timezone.now(), ""
    pkg.save()
    label = pkg.badge[0]
    return True, f"{pkg.tracking_number[-8:]}: {label}" + (f" · ETA {info['eta']:%b %-d}" if info["eta"] else "")


def sync_expected_date(receipt):
    """While the order is open, 'Expected' follows the packages: the latest
    ETA among boxes still on the way, or the last delivery date."""
    if receipt.posted:
        return
    eta = receipt.tracking_eta
    if eta and receipt.expected_date != eta:
        receipt.expected_date = eta
        receipt.save(update_fields=["expected_date"])


def refresh_receipt(receipt, provider=None):
    """Refresh every package on a supplier order. Returns (ok, message)."""
    from shipping.providers import get_provider
    pkgs = list(receipt.packages.all())
    if not pkgs:
        return False, "No tracking number."
    provider = provider or get_provider("shippo")
    msgs, ok_any = [], False
    for pkg in pkgs:
        if pkg.is_delivered and not receipt.posted:
            msgs.append(f"{pkg.tracking_number[-8:]}: Delivered")
            ok_any = True
            continue
        ok, msg = refresh_package(pkg, provider)
        ok_any = ok_any or ok
        msgs.append(msg)
    sync_expected_date(receipt)
    return ok_any, " · ".join(msgs)
