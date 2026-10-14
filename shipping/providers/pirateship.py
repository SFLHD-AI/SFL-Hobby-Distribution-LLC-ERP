"""
Pirate Ship has NO public API — so this adapter deliberately does not fake one.

Supported workflow:
1. Batch CSV export (build_batch_csv) in Pirate Ship's batch-import column
   format — upload it at ship.pirateship.com ("Import from Spreadsheet").
2. After buying labels, paste tracking number + label cost per order back
   into SFL ERP System (quick form on the shipping screen).
"""
import csv
import io

from .base import ProviderNotConfigured, ShippingProvider


class PirateShipProvider(ShippingProvider):
    key = "pirateship"
    label = "Pirate Ship"
    supports_push = False
    supports_pull = False

    def is_configured(self):
        return False  # never an API

    def push_order(self, shipment):
        raise ProviderNotConfigured(
            "Pirate Ship has no API. Export the batch CSV, import it at "
            "ship.pirateship.com, then paste tracking + cost back here.")

    def pull_tracking(self, shipment):
        raise ProviderNotConfigured("Pirate Ship has no API.")


# Pirate Ship batch-import spreadsheet columns
BATCH_COLUMNS = [
    "Recipient Name", "Company", "Email", "Phone",
    "Address 1", "Address 2", "City", "State", "Zipcode", "Country",
    "Weight (lb)", "Length (in)", "Width (in)", "Height (in)",
    "Order ID", "Order Value", "Rubber Stamp 1",
]


def build_batch_csv(shipments):
    """Return CSV text for Pirate Ship batch import from Shipment objects."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(BATCH_COLUMNS)
    for s in shipments:
        po = s.po
        addr = po.ship_to
        writer.writerow([
            (addr.attention if addr and addr.attention else po.customer.business_name),
            po.customer.business_name,
            po.customer.email,
            (addr.phone if addr else "") or po.customer.phone,
            addr.line1 if addr else "",
            addr.line2 if addr else "",
            addr.city if addr else "",
            addr.state if addr else "",
            addr.zip_code if addr else "",
            "US",
            s.weight_lbs or "",
            s.length_in or "", s.width_in or "", s.height_in or "",
            po.number,
            po.total,
            po.number,
        ])
    return buf.getvalue()
