"""
Common ShippingProvider interface. Add a new carrier by subclassing and
registering in shipping/providers/__init__.py.
"""
from dataclasses import dataclass


class ProviderNotConfigured(Exception):
    """Raised when API credentials are missing — callers fall back to manual entry."""


class ProviderError(Exception):
    """A call to the carrier failed."""


@dataclass
class TrackingInfo:
    tracking_number: str = ""
    cost: object = None          # Decimal | None
    service: str = ""
    ship_date: object = None     # date | None
    status: str = ""


class ShippingProvider:
    """Interface every carrier adapter implements."""

    key = "base"
    label = "Base"
    supports_push = False        # can we create orders/listings via API?
    supports_pull = False        # can we pull tracking/costs via API?

    def is_configured(self):
        return False

    def push_order(self, shipment):
        """Create the order/listing at the carrier. Returns external_id (str)."""
        raise ProviderNotConfigured(f"{self.label} has no API configured.")

    def pull_tracking(self, shipment):
        """Fetch tracking/cost for a pushed shipment. Returns TrackingInfo or None."""
        raise ProviderNotConfigured(f"{self.label} has no API configured.")
