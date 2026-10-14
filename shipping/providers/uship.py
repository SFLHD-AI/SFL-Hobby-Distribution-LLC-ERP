"""
uShip adapter (freight / LTL pallets).

uShip's public API requires a partner account and OAuth credentials; where
that is available (env: USHIP_API_KEY) we create a shipment listing from
the order's weight/dims and saved address. Without credentials every call
raises ProviderNotConfigured and the UI drops to manual entry (paste the
booked shipment reference, cost and tracking), which is a fully supported
workflow — nothing is faked.
"""
import requests
from django.conf import settings

from .base import ProviderError, ProviderNotConfigured, ShippingProvider, TrackingInfo


class UShipProvider(ShippingProvider):
    key = "uship"
    label = "uShip (freight/LTL)"
    supports_push = True
    supports_pull = False   # tracking is entered manually after booking

    def __init__(self):
        # Key entered on the Settings screen wins; .env is the fallback.
        from core.models import CompanySettings
        cs = CompanySettings.get()
        self.api_key = cs.uship_api_key or settings.USHIP_API_KEY
        self.base = settings.USHIP_BASE_URL.rstrip("/")

    def is_configured(self):
        return bool(self.api_key)

    def push_order(self, shipment):
        if not self.is_configured():
            raise ProviderNotConfigured(
                "uShip API key not set (USHIP_API_KEY) — use manual entry: create the "
                "listing on uship.com and paste the shipment reference and cost here.")
        po = shipment.po
        addr = po.ship_to
        payload = {
            "title": f"Pallet shipment {po.number}",
            "commodity": "Sports cards & collectibles (palletized)",
            "items": [{
                "weightLbs": float(shipment.weight_lbs or 0),
                "lengthIn": float(shipment.length_in or 48),
                "widthIn": float(shipment.width_in or 40),
                "heightIn": float(shipment.height_in or 48),
                "quantity": 1,
            }],
            "destination": {
                "street": addr.line1 if addr else "",
                "city": addr.city if addr else "",
                "state": addr.state if addr else "",
                "postalCode": addr.zip_code if addr else "",
                "country": "US",
            },
            "externalReference": po.number,
        }
        try:
            resp = requests.post(f"{self.base}/v2/listings", json=payload,
                                 headers={"Authorization": f"Bearer {self.api_key}"},
                                 timeout=30)
        except requests.RequestException as e:
            raise ProviderError(f"uShip unreachable: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"uShip {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return str(data.get("listingId") or data.get("id") or "")

    def pull_tracking(self, shipment):
        return None
