"""
ShipStation REST API adapter.
Docs: https://www.shipstation.com/docs/api/
Auth: HTTP Basic with API key/secret (env: SHIPSTATION_API_KEY / _SECRET).
"""
from datetime import date
from decimal import Decimal

import requests
from django.conf import settings

from .base import ProviderError, ProviderNotConfigured, ShippingProvider, TrackingInfo


class ShipStationProvider(ShippingProvider):
    key = "shipstation"
    label = "ShipStation"
    supports_push = True
    supports_pull = True

    def __init__(self):
        # Keys entered on the Settings screen win; .env is the fallback.
        from core.models import CompanySettings
        cs = CompanySettings.get()
        self.api_key = cs.shipstation_api_key or settings.SHIPSTATION_API_KEY
        self.api_secret = cs.shipstation_api_secret or settings.SHIPSTATION_API_SECRET
        self.base = settings.SHIPSTATION_BASE_URL.rstrip("/")

    def is_configured(self):
        return bool(self.api_key and self.api_secret)

    def _auth(self):
        return (self.api_key, self.api_secret)

    def _request(self, method, path, **kwargs):
        if not self.is_configured():
            raise ProviderNotConfigured("Set SHIPSTATION_API_KEY / SHIPSTATION_API_SECRET.")
        try:
            resp = requests.request(method, f"{self.base}{path}", auth=self._auth(),
                                    timeout=30, **kwargs)
        except requests.RequestException as e:
            raise ProviderError(f"ShipStation unreachable: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"ShipStation {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.text else {}

    def push_order(self, shipment):
        po = shipment.po
        addr = po.ship_to
        name = addr.attention if addr and addr.attention else po.customer.business_name
        payload = {
            "orderNumber": po.number,
            "orderDate": po.finalized_at.strftime("%Y-%m-%dT%H:%M:%S") if po.finalized_at
                         else date.today().isoformat(),
            "orderStatus": "awaiting_shipment",
            "billTo": {"name": po.customer.business_name},
            "shipTo": {
                "name": name,
                "company": po.customer.business_name,
                "street1": addr.line1 if addr else "",
                "street2": addr.line2 if addr else "",
                "city": addr.city if addr else "",
                "state": addr.state if addr else "",
                "postalCode": addr.zip_code if addr else "",
                "country": "US",
                "phone": (addr.phone if addr else "") or po.customer.phone,
            },
            "items": [
                {"sku": l.product.sku, "name": l.product.name,
                 "quantity": l.fulfilled_qty,
                 "unitPrice": float(l.unit_price)}
                for l in po.live_lines.select_related("product")
                if l.fulfilled_qty > 0
            ],
        }
        if shipment.weight_lbs:
            payload["weight"] = {"value": float(shipment.weight_lbs), "units": "pounds"}
        if shipment.length_in and shipment.width_in and shipment.height_in:
            payload["dimensions"] = {
                "units": "inches", "length": float(shipment.length_in),
                "width": float(shipment.width_in), "height": float(shipment.height_in)}
        data = self._request("POST", "/orders/createorder", json=payload)
        return str(data.get("orderId", ""))

    def pull_tracking(self, shipment):
        """Look up shipments by our order number; return newest label."""
        data = self._request("GET", "/shipments",
                             params={"orderNumber": shipment.po.number,
                                     "includeShipmentItems": "false",
                                     "sortBy": "createDate", "sortDir": "DESC"})
        rows = data.get("shipments") or []
        if not rows:
            return None
        row = rows[0]
        info = TrackingInfo(
            tracking_number=row.get("trackingNumber") or "",
            service=row.get("serviceCode") or "",
            status="shipped" if not row.get("voided") else "cancelled",
        )
        if row.get("shipmentCost") is not None:
            info.cost = Decimal(str(row["shipmentCost"]))
        if row.get("shipDate"):
            info.ship_date = date.fromisoformat(row["shipDate"][:10])
        return info
