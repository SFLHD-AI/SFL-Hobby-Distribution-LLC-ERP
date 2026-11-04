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


def refresh_receipt(receipt, provider=None):
    """Update one receipt's tracking fields. Returns (ok, message)."""
    from shipping.providers import ProviderError, ProviderNotConfigured, get_provider
    if not receipt.tracking_number:
        return False, "No tracking number."
    carrier = receipt.tracking_carrier or guess_carrier(receipt.tracking_number)
    if carrier and carrier != receipt.tracking_carrier:
        receipt.tracking_carrier = carrier
    if carrier not in SHIPPO_CARRIERS:
        receipt.tracking_checked_at = timezone.now()
        receipt.tracking_error = "Automatic tracking isn't available for this carrier; use the link."
        receipt.save(update_fields=["tracking_carrier", "tracking_checked_at", "tracking_error"])
        return False, receipt.tracking_error
    provider = provider or get_provider("shippo")
    try:
        info = fetch_status(carrier, receipt.tracking_number, provider)
    except ProviderNotConfigured:
        receipt.tracking_checked_at = timezone.now()
        receipt.tracking_error = "Add a Shippo token in Settings to track automatically."
        receipt.save(update_fields=["tracking_carrier", "tracking_checked_at", "tracking_error"])
        return False, receipt.tracking_error
    except ProviderError as e:
        receipt.tracking_checked_at = timezone.now()
        receipt.tracking_error = str(e)[:200]
        receipt.save(update_fields=["tracking_carrier", "tracking_checked_at", "tracking_error"])
        return False, receipt.tracking_error
    receipt.tracking_status = info["status"]
    receipt.tracking_detail = info["detail"]
    receipt.tracking_location = info["location"]
    receipt.tracking_event_at = info["event_at"]
    receipt.tracking_eta = info["eta"]
    receipt.tracking_checked_at = timezone.now()
    receipt.tracking_error = ""
    fields = ["tracking_carrier", "tracking_status", "tracking_detail", "tracking_location",
              "tracking_event_at", "tracking_eta", "tracking_checked_at", "tracking_error"]
    # The carrier's ETA becomes the expected arrival while the order is still open
    if info["eta"] and not receipt.posted and receipt.expected_date != info["eta"]:
        receipt.expected_date = info["eta"]
        fields.append("expected_date")
    if info["status"] == "DELIVERED" and info["event_at"] and not receipt.posted:
        day = timezone.localtime(info["event_at"]).date()
        if receipt.expected_date != day:
            receipt.expected_date = day
            if "expected_date" not in fields:
                fields.append("expected_date")
    receipt.save(update_fields=fields)
    label = receipt.tracking_badge[0] if receipt.tracking_badge else info["status"]
    return True, f"{label}" + (f" · ETA {info['eta']:%b %-d}" if info["eta"] else "") + (f" · {info['detail']}" if info["detail"] else "")
