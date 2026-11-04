"""Background jobs for inventory (Django-Q)."""
from inventory.models import Receipt
from inventory.tracking import refresh_receipt


def refresh_inbound_tracking():
    """Hourly: refresh tracking on open supplier orders that have a number."""
    from shipping.providers import get_provider
    provider = get_provider("shippo")
    if not provider.is_configured():
        return "Shippo not configured; inbound tracking skipped."
    qs = Receipt.objects.filter(is_deleted=False, posted=False).exclude(tracking_number="") \
                        .exclude(tracking_status="DELIVERED")
    ok = fail = 0
    for r in qs:
        good, _ = refresh_receipt(r, provider)
        ok += good
        fail += (not good)
    return f"{ok} updated, {fail} failed."
