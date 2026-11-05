"""Background jobs for inventory (Django-Q)."""
from inventory.models import Receipt
from inventory.tracking import refresh_receipt


def refresh_market_prices():
    """Top of every hour: TCGplayer market price for sealed TCG product."""
    from inventory.pricing import refresh_all
    return refresh_all()


def refresh_inbound_tracking():
    """Hourly: refresh tracking on open supplier orders that have packages on the way."""
    from shipping.providers import get_provider
    provider = get_provider("shippo")
    if not provider.is_configured():
        return "Shippo not configured; inbound tracking skipped."
    qs = (Receipt.objects.filter(is_deleted=False, posted=False, packages__isnull=False)
          .exclude(packages__status="DELIVERED").distinct()
          | Receipt.objects.filter(is_deleted=False, posted=False, packages__isnull=False)
          .filter(packages__status="").distinct())
    ok = fail = 0
    for r in qs.distinct():
        good, _ = refresh_receipt(r, provider)
        ok += good
        fail += (not good)
    return f"{ok} updated, {fail} failed."
