"""
Shippo (goshippo.com) adapter — live rates + label purchase via REST API.

Flow: build a Shippo shipment from the order's saved address and the box
dims/weights (package builder boxes if present, else the shipment's own
weight/dims) -> Shippo returns rates from USPS/UPS/FedEx/etc -> the user
picks a rate -> we buy the label (transaction) and store the tracking
number, cost and 4x6 label PDF URL.

Auth: token from Settings screen (wins) or SHIPPO_API_TOKEN env.
Docs: https://docs.goshippo.com/
"""
from decimal import Decimal

import requests
from django.conf import settings

from .base import ProviderError, ProviderNotConfigured, ShippingProvider


class ShippoProvider(ShippingProvider):
    key = "shippo"
    label = "Shippo"
    supports_push = True
    supports_pull = False  # tracking/cost come back at purchase time

    def __init__(self):
        from core.models import CompanySettings
        cs = CompanySettings.get()
        self.token = getattr(cs, "shippo_api_token", "") or settings.SHIPPO_API_TOKEN
        self.base = settings.SHIPPO_BASE_URL.rstrip("/")

    def is_configured(self):
        return bool(self.token)

    def _request(self, method, path, payload=None):
        if not self.is_configured():
            raise ProviderNotConfigured(
                "Shippo token not set — add it on the Settings screen "
                "(goshippo.com → Settings → API → Generate Token).")
        try:
            resp = requests.request(
                method, f"{self.base}{path}", json=payload, timeout=45,
                headers={"Authorization": f"ShippoToken {self.token}"})
        except requests.RequestException as e:
            raise ProviderError(f"Shippo unreachable: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"Shippo {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ------------------------------------------------------------------
    def _address_from(self):
        from core.models import CompanySettings
        c = CompanySettings.get()
        return {
            "name": c.company_name, "street1": c.address_line1,
            "street2": c.address_line2, "city": c.city, "state": c.state,
            "zip": c.zip_code, "country": "US",
            "phone": c.phone, "email": c.email,
        }

    def _address_to(self, shipment):
        po = shipment.po
        addr = po.ship_to
        if not addr:
            raise ProviderError(f"{po.number} has no shipping address selected.")
        return {
            "name": addr.attention or po.customer.business_name,
            "company": po.customer.business_name,
            "street1": addr.line1, "street2": addr.line2,
            "city": addr.city, "state": addr.state, "zip": addr.zip_code,
            "country": "US",
            "phone": addr.phone or po.customer.phone,
            "email": po.customer.email,
        }

    def _parcels(self, shipment):
        parcels = []
        boxes = shipment.packages.filter(is_deleted=False)
        for box in boxes:
            if box.weight_lbs and box.length_in and box.width_in and box.height_in:
                parcels.append({
                    "length": str(box.length_in), "width": str(box.width_in),
                    "height": str(box.height_in), "distance_unit": "in",
                    "weight": str(box.weight_lbs), "mass_unit": "lb"})
        if not parcels and shipment.weight_lbs and shipment.length_in \
                and shipment.width_in and shipment.height_in:
            parcels.append({
                "length": str(shipment.length_in), "width": str(shipment.width_in),
                "height": str(shipment.height_in), "distance_unit": "in",
                "weight": str(shipment.weight_lbs), "mass_unit": "lb"})
        if not parcels:
            raise ProviderError(
                "No box dimensions/weights. Use the package builder below (or fill "
                "the shipment's weight and L×W×H) before getting rates.")
        return parcels

    def get_rates(self, shipment):
        """Create a Shippo shipment and return sorted purchasable rates."""
        payload = {
            "address_from": self._address_from(),
            "address_to": self._address_to(shipment),
            "parcels": self._parcels(shipment),
            "async": False,
        }
        data = self._request("POST", "/shipments/", payload)
        msgs = [m.get("text", "") for m in data.get("messages", []) if m.get("text")]
        rates = []
        for r in data.get("rates", []):
            rates.append({
                "rate_id": r["object_id"],
                "carrier": r.get("provider", ""),
                "service": (r.get("servicelevel") or {}).get("name", ""),
                "amount": r.get("amount", ""),
                "currency": r.get("currency", "USD"),
                "days": r.get("estimated_days"),
            })
        rates.sort(key=lambda x: Decimal(x["amount"] or "999999"))
        if not rates:
            raise ProviderError(
                "Shippo returned no rates" + (f" — {'; '.join(msgs[:3])}" if msgs else
                                              ". Check the address and box dims."))
        return rates

    def buy_label(self, rate_id):
        """Purchase the chosen rate. Returns dict with tracking/label/cost."""
        data = self._request("POST", "/transactions/", {
            "rate": rate_id, "label_file_type": "PDF_4x6", "async": False})
        if data.get("status") != "SUCCESS":
            msgs = [m.get("text", "") for m in data.get("messages", [])]
            raise ProviderError("Shippo label purchase failed: "
                                + ("; ".join(msgs) or data.get("status", "unknown")))
        return {
            "tracking_number": data.get("tracking_number", ""),
            "label_url": data.get("label_url", ""),
            "tracking_url": data.get("tracking_url_provider", ""),
        }

    # ShippingProvider interface compatibility
    def push_order(self, shipment):
        raise ProviderNotConfigured(
            "Shippo works through Get rates → Buy label on the shipment page.")

    def pull_tracking(self, shipment):
        return None
