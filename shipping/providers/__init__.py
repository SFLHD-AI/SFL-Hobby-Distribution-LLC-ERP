from .base import ProviderError, ProviderNotConfigured, ShippingProvider, TrackingInfo
from .pirateship import PirateShipProvider, build_batch_csv
from .shippo import ShippoProvider
from .shipstation import ShipStationProvider
from .uship import UShipProvider

PROVIDERS = {
    "shipstation": ShipStationProvider,
    "shippo": ShippoProvider,
    "uship": UShipProvider,
    "pirateship": PirateShipProvider,
}


def get_provider(key):
    cls = PROVIDERS.get(key)
    return cls() if cls else None
