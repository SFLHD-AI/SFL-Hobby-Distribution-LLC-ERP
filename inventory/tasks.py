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


def sync_tradepost():
    """Hourly: pull new Tradepost fills, match them to logged supplier orders,
    and refresh Shippo tracking on packages still on the way."""
    from core.models import CompanySettings
    from inventory import tradepost as tp
    from inventory.models import ReceiptLine, TradepostFill
    from shipping.providers import get_provider
    client = tp.TradepostClient(CompanySettings.get().tradepost_api_key)
    if not client.is_configured():
        return "Tradepost not configured; skipped."
    try:
        new, upd = tp.sync_fills(client)
    except tp.TradepostError as e:
        return f"Tradepost sync failed: {e}"
    linked = tp.link_logged_fills(Receipt, ReceiptLine)
    n = 0
    provider = get_provider("shippo")
    if provider.is_configured():
        for fill in TradepostFill.objects.filter(dismissed=False).exclude(packages=[]):
            n += tp.refresh_fill_tracking(fill, provider)
    return f"{new} new, {upd} updated, {linked} linked, {n} packages tracked."
