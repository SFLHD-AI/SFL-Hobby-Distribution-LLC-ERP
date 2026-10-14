"""
Background jobs (Django-Q2). Schedule sync_shipstation_tracking hourly:
run `python manage.py setup_schedules` (done automatically by seed_demo).
"""
from django.core.mail import EmailMessage
from django.utils import timezone

from core.models import CompanySettings, log_activity
from shipping.models import Shipment
from shipping.providers import ProviderError, ProviderNotConfigured, get_provider


def sync_shipstation_tracking():
    """Pull tracking numbers and label costs for pushed ShipStation shipments."""
    provider = get_provider("shipstation")
    if not provider.is_configured():
        return "ShipStation not configured; skipped."
    updated = 0
    qs = Shipment.objects.filter(is_deleted=False, provider="shipstation",
                                 status__in=["pushed", "label_created"])
    for shipment in qs:
        try:
            info = provider.pull_tracking(shipment)
        except (ProviderError, ProviderNotConfigured):
            continue
        if not info or not info.tracking_number:
            continue
        shipment.tracking_number = info.tracking_number
        if info.cost is not None:
            shipment.cost = info.cost
        if info.service:
            shipment.service = shipment.service or info.service
        if info.ship_date:
            shipment.ship_date = info.ship_date
        shipment.status = "shipped"
        shipment.last_synced_at = timezone.now()
        shipment.save()
        shipment.po.mark_shipped()
        log_activity(None, "tracking_synced", shipment, note=info.tracking_number)
        email_tracking(shipment)
        updated += 1
    return f"{updated} shipment(s) updated."


def email_tracking(shipment):
    """Email the tracking number to the customer (once)."""
    if shipment.tracking_emailed_at or not shipment.tracking_number:
        return
    customer = shipment.po.customer
    if not customer.email:
        return
    company = CompanySettings.get()
    msg = EmailMessage(
        subject=f"Your order {shipment.po.number} has shipped",
        body=(f"Hi {customer.business_name},\n\n"
              f"Order {shipment.po.number} is on the way via "
              f"{shipment.get_provider_display()} {shipment.service}.\n"
              f"Tracking number: {shipment.tracking_number}\n\n"
              f"Thank you,\n{company.company_name}\n{company.phone}"),
        to=[customer.email])
    try:
        msg.send(fail_silently=True)
        shipment.tracking_emailed_at = timezone.now()
        shipment.save(update_fields=["tracking_emailed_at"])
    except Exception:
        pass


def send_low_stock_digest():
    """Daily email digest of low-stock SKUs (if enabled in Settings)."""
    from inventory.models import Product
    company = CompanySettings.get()
    if not (company.low_stock_email_digest and company.alert_email):
        return "Digest disabled."
    low = [p for p in Product.objects.filter(active=True)
           if p.is_low_stock and not p.low_stock_alert_muted]
    if not low:
        return "Nothing low."
    lines = [f"  {p.sku:<20} available {p.qty_available:>5}  (reorder at {p.reorder_point})"
             for p in low]
    EmailMessage(
        subject=f"[SFL ERP] {len(low)} SKU(s) below reorder point",
        body="Low stock report:\n\n" + "\n".join(lines),
        to=[company.alert_email]).send(fail_silently=True)
    return f"Sent digest for {len(low)} SKUs."
